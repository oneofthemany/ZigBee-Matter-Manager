"""Background watchdog — auto-recover the app when it's unhealthy.

Runs as an asyncio task inside the manager (started from app.py's lifespan).
Conservative by design so it never makes things worse:

  - **Startup grace**: ignore health within STARTUP_GRACE of the app container's
    StartedAt (and after every restart), so a slow boot isn't mistaken for a
    failure and we can't restart-loop.
  - **Escalate slowly**: only after FAIL_THRESHOLD consecutive unhealthy checks do
    we restart the app container.
  - **Stand down during upgrades**: never act while a build/swap/rollback is in
    progress — that's the watcher's job, and we mustn't fight it.
  - **Cap restarts**: after MAX_RESTARTS we stop and report 'exhausted' (manual
    intervention needed) rather than thrash.

All thresholds are env-tunable. State is exposed via get_state() for the UI.
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Dict

import httpx

from manager import containers

logger = logging.getLogger("manager.watchdog")

APP_HEALTH_URL = os.environ.get("ZMM_APP_HEALTH_URL",
                                "https://127.0.0.1:8000/api/system/health")
APP_CONTAINER = os.environ.get("ZMM_CONTAINER_NAME", "zigbee-matter-manager")
DATA_DIR = os.environ.get("ZMM_DATA_DIR") or os.environ.get("DATA_DIR") \
    or "/opt/.zigbee-matter-manager"
STATUS_FILE = os.path.join(DATA_DIR, "data", "upgrade", "status.json")
# Written by the launcher's recovery standby (see launcher.py). While present
# the user is mid-repair via the manager's recovery UI — restarting the app
# container out from under them would destroy the session.
RECOVERY_MARKER = os.path.join(DATA_DIR, "data", ".recovery_active")

INTERVAL = float(os.environ.get("ZMM_WATCHDOG_INTERVAL", "20"))
STARTUP_GRACE = float(os.environ.get("ZMM_WATCHDOG_GRACE", "180"))
FAIL_THRESHOLD = int(os.environ.get("ZMM_WATCHDOG_THRESHOLD", "3"))
MAX_RESTARTS = int(os.environ.get("ZMM_WATCHDOG_MAX_RESTARTS", "3"))

_state: Dict[str, Any] = {
    "status": "starting",   # starting|ok|unhealthy|restarted|exhausted|standby|recovery|disabled
    "streak": 0,
    "restarts": 0,
    "last_action": None,
    "checked_at": None,
}


def get_state() -> Dict[str, Any]:
    return dict(_state)


def _set(status: str, **kw):
    _state["status"] = status
    _state["checked_at"] = datetime.utcnow().isoformat() + "Z"
    _state.update(kw)


_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")


def _epoch(rfc3339: str) -> float:
    """Parse podman's State.StartedAt (RFC3339, possibly 9-digit nanos) to epoch
    seconds. Returns 0 for the zero-time / on any failure."""
    if not rfc3339 or rfc3339.startswith("0001-01-01"):
        return 0.0
    m = _TS_RE.match(rfc3339.strip())
    if not m:
        return 0.0
    base, frac, tz = m.group(1), m.group(2), m.group(3)
    s = base
    if frac:
        s += "." + frac[:6]                 # datetime handles at most microseconds
    if tz in (None, "Z"):
        s += "+00:00"
    else:
        s += tz
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def _upgrade_in_progress() -> bool:
    try:
        with open(STATUS_FILE) as f:
            st = json.load(f) or {}
        return (st.get("state") or "") in ("building", "swapping", "rolling_back")
    except Exception:
        return False


async def _healthy(http: httpx.AsyncClient) -> bool:
    try:
        r = await http.get(APP_HEALTH_URL)
        return r.status_code == 200
    except Exception:
        return False


async def run_loop():
    """The watchdog loop. Cancel-safe; runs for the lifetime of the manager."""
    if os.environ.get("ZMM_WATCHDOG_DISABLED"):
        _set("disabled")
        logger.info("Watchdog disabled via ZMM_WATCHDOG_DISABLED")
        return

    logger.info("Watchdog active: interval=%ss grace=%ss threshold=%s max_restarts=%s",
                INTERVAL, STARTUP_GRACE, FAIL_THRESHOLD, MAX_RESTARTS)
    streak = 0
    restarts = 0
    async with httpx.AsyncClient(verify=False, timeout=5.0) as http:
        while True:
            try:
                await asyncio.sleep(INTERVAL)

                # Stand down while an upgrade operation runs (the watcher owns it).
                if _upgrade_in_progress():
                    _set("standby", streak=streak, restarts=restarts)
                    continue


                info = await containers.inspect_container(APP_CONTAINER)
                if info is None:
                    # Container absent (mid-swap rename / removed) — nothing to do.
                    _set("standby", streak=streak, restarts=restarts)
                    continue

                state = (info.get("State") or {})
                running = bool(state.get("Running"))
                started = _epoch(state.get("StartedAt") or "")

                # Within startup grace → too early to judge; reset the streak.
                if running and started and (time.time() - started) < STARTUP_GRACE:
                    streak = 0
                    _set("starting", streak=0, restarts=restarts)
                    continue

                healthy = running and await _healthy(http)

                # Recovery mode: the launcher's standby serves :8000 (health
                # WILL fail) while the user repairs files via the manager —
                # restarting the container would destroy their session. Only
                # honoured while the app is actually unhealthy, so a stale
                # marker (uncleanly killed session) can't disable us forever.
                if not healthy and os.path.isfile(RECOVERY_MARKER):
                    streak = 0
                    _set("recovery", streak=0, restarts=restarts)
                    continue

                if healthy:
                    if streak or restarts:
                        logger.info("Watchdog: app healthy again — incident cleared")
                    streak, restarts = 0, 0
                    _set("ok", streak=0, restarts=0)
                    continue

                streak += 1
                logger.warning("Watchdog: app unhealthy (running=%s, streak=%s)", running, streak)
                _set("unhealthy", streak=streak, restarts=restarts)
                if streak < FAIL_THRESHOLD:
                    continue

                if restarts < MAX_RESTARTS:
                    logger.warning("Watchdog: restarting %s (restart %s/%s)",
                                   APP_CONTAINER, restarts + 1, MAX_RESTARTS)
                    ok = await containers.restart_container(APP_CONTAINER, timeout=10)
                    restarts += 1
                    streak = 0
                    _set("restarted", streak=0, restarts=restarts,
                         last_action=("restart ok" if ok else "restart failed"))
                else:
                    logger.error("Watchdog: recovery exhausted — manual intervention needed")
                    _set("exhausted", streak=streak, restarts=restarts,
                         last_action="exhausted")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Watchdog loop error: %s", e)
