"""
Alarm panel: arm home / away / night, exit and entry delays, sirens, and an
urgent alert to the household. See docs/house-mode-and-alarm.md §Alarm.

Only a person's PIN disarms. A house-mode change can arm but never disarm, and
an automation disarms only where the admin allowed it — otherwise anyone who can
flip a worker could switch the alarm off.

Sensor events arrive on the engine's state listener, which must not block: the
state machine moves synchronously, and the side effects (sirens, alerts,
persistence, websocket) run as tasks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("alarm")

DATA_PATH = Path("./data/alarm.json")
IEEE = "alarm::panel"

DISARMED, ARMING, PENDING, TRIGGERED = "disarmed", "arming", "pending", "triggered"
ARM_MODES = ("home", "away", "night")
ARMED = {m: f"armed_{m}" for m in ARM_MODES}
STATES = (DISARMED, ARMING, *ARMED.values(), PENDING, TRIGGERED)

PIN_RE = re.compile(r"^[0-9]{4,8}$")
MAX_PIN_FAILURES = 5
LOCKOUT_S = 300
MAX_ZONES = 100

CONFIG_DEFAULTS: Dict[str, Any] = {
    "zones": [],                     # [{ieee, entry: bool, modes: [home|away|night]}]
    "sirens": [],                    # ieees switched on while triggered
    "exit_delay_s": {"home": 0, "away": 60, "night": 0},
    "entry_delay_s": 30,
    "siren_minutes": 3,
    "notify_users": [],              # empty = every account
    "arm_requires_pin": False,
    "allow_automation_disarm": False,
    "follow_house_mode": False,      # house mode away/night arms (never disarms)
    "set_house_mode": True,          # arming/disarming sets the house mode
}


def _tripped(changed: Dict[str, Any]) -> Optional[str]:
    """What an update reports, if it is an intrusion signal. Contact False is
    open in the ZCL convention used throughout."""
    if changed.get("contact") is False:
        return "opened"
    for k in ("occupancy", "motion", "presence"):
        if changed.get(k) is True:
            return "motion"
    if changed.get("tamper") is True:
        return "tampered"
    return None


def _is_open(state: Dict[str, Any]) -> bool:
    return state.get("contact") is False


class _Caps:
    def has_capability(self, cap: str) -> bool:
        return cap == "worker"          # what makes rule targets offer it

    def get_capabilities(self) -> List[str]:
        return ["worker"]


class AlarmDevice:
    """The panel as an engine device, so rules trigger on and command it."""

    def __init__(self, panel: "AlarmPanel"):
        self._panel = panel
        self.ieee = IEEE
        self.friendly_name = "Alarm"
        self.manufacturer = "ZMM"
        self.model = "Alarm panel"
        self.capabilities = _Caps()
        self.last_seen = time.time()

    @property
    def state(self) -> Dict[str, Any]:
        s = self._panel.state
        return {"state": s, "armed": 1 if s.startswith("armed_") else 0,
                "triggered": 1 if s == TRIGGERED else 0, "available": True}

    def is_available(self) -> bool:
        return True

    def get_control_commands(self) -> List[Dict[str, Any]]:
        opts = list(ARMED.values()) + ([DISARMED] if self._panel.config["allow_automation_disarm"] else [])
        return [{"command": "set", "label": "Set alarm", "type": "select", "options": opts}]

    def value_options(self, attribute: str) -> Optional[List[str]]:
        return list(STATES) if attribute == "state" else None

    async def send_command(self, command: str, value: Any = None, endpoint_id=None) -> Dict[str, Any]:
        if command != "set":
            return {"success": False, "error": f"Alarm takes 'set', not '{command}'"}
        v = str(value or "")
        if v == DISARMED:
            return await self._panel.disarm(None, None, source="automation")
        for mode, state in ARMED.items():
            if v in (state, mode):
                return await self._panel.arm(mode, None, None, source="automation")
        return {"success": False, "error": f"Unknown alarm state '{v}'"}


class AlarmPanel:
    def __init__(self, get_devices: Callable[[], Dict[str, Any]],
                 get_names: Callable[[], Dict[str, str]] = lambda: {},
                 notify: Optional[Callable[[List[str], Dict[str, Any]], Awaitable[None]]] = None,
                 broadcast: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
                 evaluate: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
                 get_users: Callable[[], List[str]] = lambda: [],
                 path: Path = DATA_PATH, clock: Callable[[], float] = time.time):
        self._get_devices = get_devices
        self._get_names = get_names
        self._notify = notify
        self._broadcast = broadcast
        self._evaluate = evaluate
        self._get_users = get_users
        self.path = path
        self._clock = clock
        self.config: Dict[str, Any] = json.loads(json.dumps(CONFIG_DEFAULTS))
        self.pins: Dict[str, str] = {}
        self.state = DISARMED
        self.armed_mode: Optional[str] = None     # the mode being armed / that is armed
        self.deadline: Optional[float] = None     # end of exit/entry delay or siren
        self.bypassed: List[str] = []             # open at arming; live once closed
        self.cause: Optional[Dict[str, Any]] = None
        self.changed_at = self._clock()
        self.changed_by = ""
        self._failures: Dict[str, List[float]] = {}
        self._mode_hook: Optional[Callable[[str, str], Awaitable[None]]] = None
        self._task: Optional[asyncio.Task] = None
        self._save_lock = asyncio.Lock()
        self.device = AlarmDevice(self)
        self.load()

    # Persistence
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.error("[alarm] unreadable %s: %s", self.path, e)
            return
        try:
            self.config = normalise_config(raw.get("config") or {}, self.config)
        except ValueError as e:
            logger.error("[alarm] bad saved config, using defaults: %s", e)
        self.pins = {u: h for u, h in (raw.get("pins") or {}).items() if isinstance(h, str)}
        st = raw.get("state") or {}
        if st.get("state") in STATES:
            # An armed house stays armed across a restart; deadlines are wall
            # clock, so a delay that ran out while the hub was down has ended.
            self.state = st["state"]
            self.armed_mode = st.get("armed_mode")
            self.deadline = st.get("deadline")
            self.bypassed = list(st.get("bypassed") or [])
            self.cause = st.get("cause")
            self.changed_at = st.get("changed_at") or self._clock()
            self.changed_by = st.get("changed_by") or ""

    def _snapshot(self) -> Dict[str, Any]:
        return {"config": self.config, "pins": self.pins, "state": {
            "state": self.state, "armed_mode": self.armed_mode, "deadline": self.deadline,
            "bypassed": self.bypassed, "cause": self.cause,
            "changed_at": self.changed_at, "changed_by": self.changed_by}}

    def _write(self, snapshot: Dict[str, Any]) -> None:
        """PIN hashes live here, so 0600 from the moment it exists."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(snapshot, fh, indent=1)
        os.replace(tmp, self.path)

    async def _save(self) -> None:
        # Writes share one temp file; two in flight would race on the rename.
        async with self._save_lock:
            try:
                await asyncio.to_thread(self._write, self._snapshot())
            except OSError as e:
                logger.error("[alarm] could not save %s: %s", self.path, e)

    # Lifecycle
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(1)
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception as e:                        # noqa: BLE001
                logger.error("[alarm] tick failed: %s", e)

    def set_mode_hook(self, hook: Callable[[str, str], Awaitable[None]]) -> None:
        """hook(house_mode, source) — how arming and disarming set the house mode."""
        self._mode_hook = hook

    # Config
    def update_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self.config = normalise_config(data, self.config)
        self._spawn(self._save())
        return self.config

    # Status
    def _zones_for(self, mode: Optional[str]) -> List[Dict[str, Any]]:
        return [z for z in self.config["zones"] if mode in z["modes"]]

    def _name(self, ieee: str) -> str:
        try:
            return self._get_names().get(ieee) or ieee
        except Exception:                                 # noqa: BLE001
            return ieee

    def _open_zones(self, mode: str) -> List[str]:
        devices = self._get_devices()
        return [z["ieee"] for z in self._zones_for(mode)
                if _is_open(dict(getattr(devices.get(z["ieee"]), "state", None) or {}))]

    def status(self) -> Dict[str, Any]:
        devices = self._get_devices()
        zones = []
        for z in self.config["zones"]:
            dev = devices.get(z["ieee"])
            st = dict(getattr(dev, "state", None) or {})
            online = bool(dev is not None and getattr(dev, "is_available", lambda: True)())
            zones.append({**z, "name": self._name(z["ieee"]), "online": online,
                          "open": _is_open(st), "bypassed": z["ieee"] in self.bypassed})
        now = self._clock()
        return {
            "state": self.state, "armed_mode": self.armed_mode,
            "seconds_left": max(0, round(self.deadline - now)) if self.deadline else None,
            "cause": self.cause, "changed_at": self.changed_at, "changed_by": self.changed_by,
            "zones": zones, "sirens": [{"ieee": i, "name": self._name(i)} for i in self.config["sirens"]],
            "arm_requires_pin": self.config["arm_requires_pin"],
        }

    # PINs
    def has_pin(self, user: str) -> bool:
        return user in self.pins

    def _locked(self, user: str) -> bool:
        now = self._clock()
        recent = [t for t in self._failures.get(user, []) if now - t < LOCKOUT_S]
        self._failures[user] = recent
        return len(recent) >= MAX_PIN_FAILURES

    async def _check_pin(self, user: str, pin: Optional[str]) -> None:
        from modules.auth import verify_password
        if self._locked(user):
            raise PermissionError("Too many wrong PINs; try again in a few minutes")
        encoded = self.pins.get(user)
        if not encoded:
            raise PermissionError("Set your alarm PIN first")
        ok = await asyncio.to_thread(verify_password, str(pin or ""), encoded)
        if not ok:
            self._failures.setdefault(user, []).append(self._clock())
            logger.warning("[alarm] wrong PIN for %s", user)
            raise PermissionError("Wrong PIN")
        self._failures.pop(user, None)

    async def set_pin(self, user: str, pin: str, current: Optional[str] = None) -> None:
        from modules.auth import hash_password
        if not PIN_RE.match(str(pin or "")):
            raise ValueError("A PIN is 4 to 8 digits")
        if user in self.pins:
            await self._check_pin(user, current)
        self.pins[user] = await asyncio.to_thread(hash_password, pin)
        await self._save()

    async def clear_pin(self, user: str) -> bool:
        existed = self.pins.pop(user, None) is not None
        self._failures.pop(user, None)
        await self._save()
        return existed

    # Transitions
    def _move(self, state: str, by: str, deadline: Optional[float] = None) -> None:
        old = self.state
        self.state, self.deadline = state, deadline
        self.changed_at, self.changed_by = self._clock(), by
        logger.info("[alarm] %s -> %s (%s)", old, state, by)
        self._spawn(self._after_move(old))

    async def _after_move(self, old: str) -> None:
        await self._save()
        if self._broadcast:
            try:
                await self._broadcast("alarm_state", self.status())
            except Exception as e:                        # noqa: BLE001
                logger.debug("[alarm] broadcast failed: %s", e)
        if self._evaluate:
            try:
                await self._evaluate(IEEE, {"state": self.state, **self.device.state})
            except Exception as e:                        # noqa: BLE001
                logger.warning("[alarm] rule evaluation failed: %s", e)

    async def arm(self, mode: str, user: Optional[str], pin: Optional[str],
                  force: bool = False, source: str = "manual") -> Dict[str, Any]:
        if mode not in ARM_MODES:
            return {"success": False, "error": f"Arm mode must be one of {list(ARM_MODES)}"}
        if user and source == "manual" and self.config["arm_requires_pin"]:
            try:
                await self._check_pin(user, pin)
            except PermissionError as e:
                return {"success": False, "error": str(e)}
        if self.state in (ARMED[mode],) or (self.state == ARMING and self.armed_mode == mode):
            return {"success": True, "status": self.status()}
        if self.state in (PENDING, TRIGGERED):
            return {"success": False, "error": "Disarm first — the alarm is going off"}
        open_now = self._open_zones(mode)
        if open_now and not force:
            return {"success": False, "open": [{"ieee": i, "name": self._name(i)} for i in open_now],
                    "error": "Open: " + ", ".join(self._name(i) for i in open_now)}
        self.bypassed = open_now
        self.cause = None
        self.armed_mode = mode
        delay = int(self.config["exit_delay_s"].get(mode, 0))
        by = user or source
        if delay > 0:
            self._move(ARMING, by, self._clock() + delay)
        else:
            self._move(ARMED[mode], by)
        if self.config["set_house_mode"] and self._mode_hook and source != "house_mode":
            self._spawn(self._mode_hook(mode, "alarm"))
        return {"success": True, "status": self.status()}

    async def disarm(self, user: Optional[str], pin: Optional[str],
                     source: str = "manual") -> Dict[str, Any]:
        if source == "automation":
            if not self.config["allow_automation_disarm"]:
                return {"success": False, "error": "Automations may not disarm the alarm"}
        else:
            if not user:
                return {"success": False, "error": "Disarming needs a person"}
            try:
                await self._check_pin(user, pin)
            except PermissionError as e:
                return {"success": False, "error": str(e)}
        was = self.state
        if was == DISARMED:
            return {"success": True, "status": self.status()}
        self.bypassed, self.armed_mode = [], None
        self._move(DISARMED, user or source)
        self._spawn(self._sirens(False))
        if was in (PENDING, TRIGGERED):
            self._spawn(self._alert("Alarm disarmed", f"Disarmed by {user or source}.", urgent=False))
        if self.config["set_house_mode"] and self._mode_hook:
            self._spawn(self._mode_hook("home", "alarm"))
        return {"success": True, "status": self.status()}

    def on_house_mode(self, new_mode: str) -> None:
        """House mode away/holiday or night arms; nothing here disarms."""
        if not self.config["follow_house_mode"]:
            return
        target = {"away": "away", "holiday": "away", "night": "night"}.get(new_mode.lower())
        if target and self.state not in (PENDING, TRIGGERED):
            self._spawn(self._arm_for_mode(target))

    async def _arm_for_mode(self, mode: str) -> None:
        r = await self.arm(mode, None, None, source="house_mode")
        if not r.get("success"):
            await self._alert("Alarm not armed",
                              f"House mode changed to {mode} but the alarm could not arm: {r.get('error')}",
                              urgent=True)

    # Sensors
    def observe(self, ieee: str, changed: Dict[str, Any]) -> None:
        """Engine state listener; synchronous and non-blocking."""
        if ieee == IEEE:
            return
        if ieee in self.bypassed and changed.get("contact") is True:
            self.bypassed.remove(ieee)          # closed again: back in the zone
            return
        if not self.state.startswith("armed_") and self.state != ARMING:
            return
        zone = next((z for z in self._zones_for(self.armed_mode) if z["ieee"] == ieee), None)
        if zone is None or ieee in self.bypassed:
            return
        what = _tripped(changed)
        if what is None or self.state == ARMING:
            return                              # leaving the house trips sensors
        self.cause = {"ieee": ieee, "name": self._name(ieee), "event": what, "at": self._clock()}
        delay = int(self.config["entry_delay_s"]) if zone["entry"] else 0
        if delay > 0:
            self._move(PENDING, "sensor", self._clock() + delay)
        else:
            self._trigger()

    def _trigger(self) -> None:
        minutes = int(self.config["siren_minutes"])
        self._move(TRIGGERED, "sensor", self._clock() + minutes * 60 if minutes else None)
        c = self.cause or {}
        self._spawn(self._sirens(True))
        self._spawn(self._alert("ALARM", f"{c.get('name', 'A sensor')} {c.get('event', 'tripped')} "
                                         f"while armed {self.armed_mode}.", urgent=True))

    async def tick(self) -> None:
        if self.deadline is None or self._clock() < self.deadline:
            return
        if self.state == ARMING:
            self._move(ARMED[self.armed_mode], self.changed_by)
        elif self.state == PENDING:
            self._trigger()
        elif self.state == TRIGGERED:
            # Siren window over: quiet again, still armed, still on record.
            await self._sirens(False)
            self._move(ARMED.get(self.armed_mode or "away", ARMED["away"]), "siren timeout")

    # Effects
    async def _sirens(self, on: bool) -> None:
        devices = self._get_devices()
        for ieee in self.config["sirens"]:
            dev = devices.get(ieee)
            if dev is None:
                logger.warning("[alarm] siren %s not found", ieee)
                continue
            try:
                await dev.send_command("on" if on else "off")
            except Exception as e:                        # noqa: BLE001
                logger.warning("[alarm] siren %s failed: %s", ieee, e)

    async def _alert(self, title: str, body: str, urgent: bool) -> None:
        if not self._notify:
            return
        users = self.config["notify_users"] or self._get_users()
        try:
            await self._notify(users, {"title": title, "body": body, "urgent": urgent})
        except Exception as e:                            # noqa: BLE001
            logger.warning("[alarm] alert failed: %s", e)

    def _spawn(self, coro) -> None:
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()


def normalise_config(data: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(current))
    if "zones" in data:
        zones, seen = [], set()
        for z in data["zones"] or []:
            ieee = str((z or {}).get("ieee") or "").strip()
            if not ieee or ieee in seen or ieee == IEEE:
                continue
            modes = [m for m in (z.get("modes") or []) if m in ARM_MODES]
            zones.append({"ieee": ieee, "entry": bool(z.get("entry")), "modes": modes})
            seen.add(ieee)
        if len(zones) > MAX_ZONES:
            raise ValueError(f"At most {MAX_ZONES} zones")
        cfg["zones"] = zones
    if "sirens" in data:
        cfg["sirens"] = list(dict.fromkeys(str(i).strip() for i in data["sirens"] or [] if str(i).strip()))
    if "exit_delay_s" in data:
        ed = data["exit_delay_s"] or {}
        cfg["exit_delay_s"] = {m: _bounded(ed.get(m, cfg["exit_delay_s"].get(m, 0)), 0, 600, "Exit delay")
                               for m in ARM_MODES}
    if "entry_delay_s" in data:
        cfg["entry_delay_s"] = _bounded(data["entry_delay_s"], 0, 600, "Entry delay")
    if "siren_minutes" in data:
        cfg["siren_minutes"] = _bounded(data["siren_minutes"], 0, 30, "Siren time")
    if "notify_users" in data:
        cfg["notify_users"] = [str(u) for u in data["notify_users"] or [] if str(u).strip()]
    for k in ("arm_requires_pin", "allow_automation_disarm", "follow_house_mode", "set_house_mode"):
        if k in data:
            cfg[k] = bool(data[k])
    return cfg


def _bounded(value: Any, lo: int, hi: int, label: str) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number") from None
    if not lo <= v <= hi:
        raise ValueError(f"{label} must be {lo}-{hi}")
    return v


_panel: Optional[AlarmPanel] = None


def get_alarm() -> Optional[AlarmPanel]:
    return _panel


def set_alarm(p: Optional[AlarmPanel]) -> None:
    global _panel
    _panel = p
