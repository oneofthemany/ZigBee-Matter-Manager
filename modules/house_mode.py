"""
House mode: one Mode worker designated as the household's mode, optionally
following presence. The worker stays an ordinary worker — rules read and set it
as before; this adds the designation, the presence follow and the header switch.
See docs/house-mode-and-alarm.md §House mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("house_mode")

CONFIG_PATH = Path("./data/house_mode.json")
DEFAULT_WORKER = "house_mode"           # the id the swarm suggestions already use
DEFAULT_OPTIONS = ["home", "away", "night", "holiday"]
DEFAULTS: Dict[str, Any] = {"worker": DEFAULT_WORKER, "follow_presence": False,
                            "away_after_minutes": 10}
# Arrival ends these; a household that comes back is home, whatever it was.
ARRIVAL_ENDS = ("away", "holiday")


class HouseMode:
    def __init__(self, get_workers: Callable[[], Any], get_presence: Callable[[], Any],
                 path: Path = CONFIG_PATH, clock: Callable[[], float] = time.monotonic):
        self._get_workers = get_workers
        self._get_presence = get_presence
        self.path = path
        self._clock = clock
        self.config: Dict[str, Any] = dict(DEFAULTS)
        self._away_since: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self.load()

    # Config
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.error("[house_mode] unreadable %s: %s", self.path, e)
            return
        self.config = {**DEFAULTS, **{k: raw[k] for k in DEFAULTS if k in raw}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.config, indent=1))
        os.replace(tmp, self.path)

    def update_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        cfg = dict(self.config)
        if "worker" in data:
            wid = str(data["worker"] or "").strip().lower()
            w = self._workers().get(wid) if wid else None
            if wid and (w is None or w.type != "mode"):
                raise ValueError(f"'{wid}' is not a Mode worker")
            cfg["worker"] = wid
        if "follow_presence" in data:
            cfg["follow_presence"] = bool(data["follow_presence"])
        if "away_after_minutes" in data:
            m = int(data["away_after_minutes"])
            if not 0 <= m <= 240:
                raise ValueError("away_after_minutes must be 0-240")
            cfg["away_after_minutes"] = m
        self.config = cfg
        self._save()
        return self.config

    # The worker
    def _workers(self):
        m = self._get_workers()
        if m is None:
            raise ValueError("Workers are not running")
        return m

    def worker(self):
        try:
            w = self._workers().get(self.config.get("worker") or "")
        except ValueError:
            return None
        return w if w is not None and w.type == "mode" and w.enabled else None

    def ensure_worker(self) -> Dict[str, Any]:
        """Create the default house-mode worker, or adopt an existing one."""
        workers = self._workers()
        w = workers.get(DEFAULT_WORKER)
        if w is None:
            r = workers.create({"id": DEFAULT_WORKER, "name": "House Mode", "type": "mode",
                                "options": DEFAULT_OPTIONS, "initial": "home",
                                "icon": "fa-house"})
            if not r.get("success"):
                raise ValueError(r.get("error") or "Could not create the worker")
        elif w.type != "mode":
            raise ValueError(f"A '{DEFAULT_WORKER}' worker exists but is not a Mode worker")
        self.update_config({"worker": DEFAULT_WORKER})
        return self.status()

    def _option(self, wanted: str) -> Optional[str]:
        """The worker's own spelling of an option, matched case-insensitively."""
        w = self.worker()
        for o in (w.cfg.get("options") or []) if w else []:
            if o.lower() == wanted.lower():
                return o
        return None

    def current(self) -> Optional[str]:
        w = self.worker()
        return str(w.state.get("value")) if w else None

    def status(self) -> Dict[str, Any]:
        w = self.worker()
        return {
            "configured": w is not None,
            "worker": self.config.get("worker") or "",
            "name": w.friendly_name if w else "",
            "mode": self.current(),
            "options": list(w.cfg.get("options") or []) if w else [],
            "follow_presence": self.config["follow_presence"],
            "away_after_minutes": self.config["away_after_minutes"],
        }

    async def set(self, mode: str, source: str = "manual") -> Dict[str, Any]:
        w = self.worker()
        if w is None:
            raise ValueError("No house mode is set up")
        option = self._option(mode)
        if option is None:
            raise ValueError(f"'{mode}' is not one of {w.cfg.get('options')}")
        old = self.current()
        if old == option:
            return self.status()
        r = await self._workers().command(w.id, "set", option)
        if not r.get("success"):
            raise ValueError(r.get("error") or "Could not set the mode")
        logger.info("[house_mode] %s -> %s (%s)", old, option, source)
        return self.status()

    # Presence follow
    def observe(self, ieee: str, changed: Dict[str, Any]) -> None:
        """Engine state listener; must not block. Reacts to presence users only."""
        from modules.presence_users import USER_IEEE_PREFIX
        if not self.config["follow_presence"] or not ieee.startswith(USER_IEEE_PREFIX):
            return
        if "presence" not in changed:
            return
        self._spawn(self.check_presence())

    def _spawn(self, coro) -> None:
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()

    def _household(self) -> Dict[str, Any]:
        p = self._get_presence()
        hh = getattr(p, "household", None) if p else None
        return dict(getattr(hh, "state", None) or {})

    async def check_presence(self) -> None:
        """Arrival is acted on at once; leaving only once everyone has been away
        `away_after_minutes`. `unknown` is neither — a silent phone is not a
        departure (presence_users.HouseholdDevice)."""
        if not self.config["follow_presence"] or self.worker() is None:
            return
        hh = self._household()
        total, home, away = hh.get("total", 0), hh.get("home_count", 0), hh.get("away_count", 0)
        mode = (self.current() or "").lower()
        if home > 0:
            self._away_since = None
            self._cancel_timer()
            if mode in ARRIVAL_ENDS and self._option("home"):
                await self.set("home", source="presence")
            return
        if not (total > 0 and away == total):
            self._away_since = None
            self._cancel_timer()
            return
        if self._away_since is None:
            self._away_since = self._clock()
        wait = self.config["away_after_minutes"] * 60 - (self._clock() - self._away_since)
        if wait > 0:
            if self._task is None or self._task.done():
                self._task = asyncio.get_running_loop().create_task(self._recheck_after(wait))
            return
        # Only from home: night or holiday is a choice someone made deliberately.
        if mode == "home" and self._option("away"):
            await self.set("away", source="presence")

    async def _recheck_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self._task = None
        await self.check_presence()

    def _cancel_timer(self) -> None:
        t, self._task = self._task, None
        if t is not None and not t.done() and t is not asyncio.current_task():
            t.cancel()


_house: Optional[HouseMode] = None


def get_house_mode() -> Optional[HouseMode]:
    return _house


def set_house_mode(h: Optional[HouseMode]) -> None:
    global _house
    _house = h
