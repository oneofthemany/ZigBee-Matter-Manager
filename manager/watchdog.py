"""Background watchdog — auto-recover the app (and ollama) when unhealthy.

Runs as an asyncio task inside the manager (started from app.py's lifespan).
Conservative by design so it never makes things worse:

  - **Startup grace**: ignore health within STARTUP_GRACE of the container's
    StartedAt (and after every restart), so a slow boot isn't mistaken for a
    failure and we can't restart-loop.
  - **Escalate slowly**: only after FAIL_THRESHOLD consecutive unhealthy checks do
    we restart the container.
  - **Stand down during upgrades**: never act while a build/swap/rollback is in
    progress — that's the watcher's job, and we mustn't fight it. Likewise for
    the ollama container while manager.ollama runs an image update.
  - **Stand down during editor test deploys**: a restart-type test deploy
    restarts the app IN-PROCESS (os.execl), so the container's StartedAt never
    changes and the normal startup grace can't protect the boot. While
    ``data/.test_pending`` is fresh, the test-recovery machinery (confirm
    timer, boot_guard) owns the outcome — restarting the container mid-test
    destroys the confirm window and can push boot_guard into rolling back a
    perfectly healthy batch.
  - **Cap restarts**: after MAX_RESTARTS we stop and report 'exhausted' (manual
    intervention needed) rather than thrash.

Two independent targets: the app container (health = ZMM_APP_HEALTH_URL) and
the optional ollama sibling (health = its /api/version; silently skipped when
the container doesn't exist). Each keeps its own streak/restart counters — an
ollama incident never eats into the app's restart budget.

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

from manager import containers, ollama

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
# Written by modules/test_recovery.py for the editor's "Test deploy" feature
# (and consumed by boot_guard.py). See the module docstring: while fresh, the
# test cycle owns app health and the watchdog must not act.
TEST_PENDING_MARKER = os.path.join(DATA_DIR, "data", ".test_pending")

INTERVAL = float(os.environ.get("ZMM_WATCHDOG_INTERVAL", "20"))
STARTUP_GRACE = float(os.environ.get("ZMM_WATCHDOG_GRACE", "180"))
# How long a .test_pending marker keeps the watchdog standing down. The marker
# is rewritten on every app boot while a batch is pending (confirm window
# 120s + boot up to STARTUP_GRACE per cycle), so a live test cycle always
# stays fresh; a stale leftover can't disable the watchdog for good.
TEST_GRACE = float(os.environ.get("ZMM_WATCHDOG_TEST_GRACE", "600"))
FAIL_THRESHOLD = int(os.environ.get("ZMM_WATCHDOG_THRESHOLD", "3"))
MAX_RESTARTS = int(os.environ.get("ZMM_WATCHDOG_MAX_RESTARTS", "3"))
OLLAMA_MAX_RESTARTS = int(os.environ.get("ZMM_WATCHDOG_OLLAMA_MAX_RESTARTS",
                                         str(MAX_RESTARTS)))

# Top-level keys are the app target (the dashboard's watchdog cell reads
# them); the ollama target reports under its own sub-dict.
_state: Dict[str, Any] = {
    "status": "starting",   # starting|ok|unhealthy|restarted|exhausted|standby|recovery|test-deploy|disabled
    "streak": 0,
    "restarts": 0,
    "last_action": None,
    "checked_at": None,
    "ollama": {"status": "starting", "streak": 0, "restarts": 0,
               "last_action": None, "checked_at": None},
}


def get_state() -> Dict[str, Any]:
    out = dict(_state)
    out["ollama"] = dict(_state["ollama"])
    return out


def _set(status: str, **kw):
    _state["status"] = status
    _state["checked_at"] = datetime.utcnow().isoformat() + "Z"
    _state.update(kw)


def _set_ollama(status: str, **kw):
    _state["ollama"]["status"] = status
    _state["ollama"]["checked_at"] = datetime.utcnow().isoformat() + "Z"
    _state["ollama"].update(kw)


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


def _test_deploy_active() -> bool:
    """True while an editor test deploy is pending and fresh (mtime-based)."""
    try:
        age = time.time() - os.stat(TEST_PENDING_MARKER).st_mtime
    except OSError:
        return False
    return age < TEST_GRACE


def _upgrade_in_progress() -> bool:
    try:
        with open(STATUS_FILE) as f:
            st = json.load(f) or {}
        return (st.get("state") or "") in ("building", "swapping", "rolling_back")
    except Exception:
        return False


def alt_scheme_url(url: str) -> str:
    """Same URL with http<->https swapped (the app may serve either)."""
    if url.startswith("https://"):
        return "http://" + url[len("https://"):]
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


async def _healthy(http: httpx.AsyncClient) -> bool:
    # Try the configured URL, then the other scheme. The app normally serves
    # HTTPS but runs plain HTTP in some states (e.g. before a self-signed cert
    # exists on first run). A scheme mismatch must NOT be read as "unhealthy" —
    # otherwise the watchdog restarts a perfectly healthy app on a loop
    # (~STARTUP_GRACE + FAIL_THRESHOLD*INTERVAL after every boot).
    urls = [APP_HEALTH_URL]
    alt = alt_scheme_url(APP_HEALTH_URL)
    if alt != APP_HEALTH_URL:
        urls.append(alt)
    for url in urls:
        try:
            r = await http.get(url)
            if r.status_code == 200:
                return True
        except Exception:
            continue
    return False


async def _check_app(http: httpx.AsyncClient, t: Dict[str, int]):
    """One health check + heal step for the app container (counters in `t`)."""
    # Stand down while an upgrade operation runs (the watcher owns it).
    if _upgrade_in_progress():
        _set("standby", streak=t["streak"], restarts=t["restarts"])
        return

    info = await containers.inspect_container(APP_CONTAINER)
    if info is None:
        # Container absent (mid-swap rename / removed) — nothing to do.
        _set("standby", streak=t["streak"], restarts=t["restarts"])
        return

    state = (info.get("State") or {})
    running = bool(state.get("Running"))
    started = _epoch(state.get("StartedAt") or "")

    # Within startup grace → too early to judge; reset the streak.
    if running and started and (time.time() - started) < STARTUP_GRACE:
        t["streak"] = 0
        _set("starting", streak=0, restarts=t["restarts"])
        return

    healthy = running and await _healthy(http)

    # Recovery mode: the launcher's standby serves :8000 (health
    # WILL fail) while the user repairs files via the manager —
    # restarting the container would destroy their session. Only
    # honoured while the app is actually unhealthy, so a stale
    # marker (uncleanly killed session) can't disable us forever.
    if not healthy and os.path.isfile(RECOVERY_MARKER):
        t["streak"] = 0
        _set("recovery", streak=0, restarts=t["restarts"])
        return

    # Test-deploy mode: the editor's test-recovery cycle restarts the app
    # in-process, so health WILL fail during its boot with no container
    # startup grace to cover it. The confirm timer and boot_guard own both
    # success and rollback here — a container restart from us would eat the
    # confirm window and could trip boot_guard's failure counter into
    # rolling back good code. Fresh markers only (see TEST_GRACE).
    if not healthy and _test_deploy_active():
        t["streak"] = 0
        _set("test-deploy", streak=0, restarts=t["restarts"])
        return

    if healthy:
        if t["streak"] or t["restarts"]:
            logger.info("Watchdog: app healthy again — incident cleared")
        t["streak"] = t["restarts"] = 0
        _set("ok", streak=0, restarts=0)
        return

    t["streak"] += 1
    logger.warning("Watchdog: app unhealthy (running=%s, streak=%s)",
                   running, t["streak"])
    _set("unhealthy", streak=t["streak"], restarts=t["restarts"])
    if t["streak"] < FAIL_THRESHOLD:
        return

    if t["restarts"] < MAX_RESTARTS:
        logger.warning("Watchdog: restarting %s (restart %s/%s)",
                       APP_CONTAINER, t["restarts"] + 1, MAX_RESTARTS)
        ok = await containers.restart_container(APP_CONTAINER, timeout=10)
        t["restarts"] += 1
        t["streak"] = 0
        _set("restarted", streak=0, restarts=t["restarts"],
             last_action=("restart ok" if ok else "restart failed"))
    else:
        logger.error("Watchdog: recovery exhausted — manual intervention needed")
        _set("exhausted", streak=t["streak"], restarts=t["restarts"],
             last_action="exhausted")


async def _check_ollama(t: Dict[str, int]):
    """One health check + heal step for the optional ollama container.
    No upgrade/recovery-marker checks — those are app concerns."""
    # Stand down while manager.ollama recreates the container on purpose.
    if ollama.updating():
        t["streak"] = 0
        _set_ollama("standby", streak=0, restarts=t["restarts"])
        return

    info = await containers.inspect_container(ollama.CONTAINER)
    if info is None:
        # Never installed (or intentionally removed) — nothing to watch.
        t["streak"] = t["restarts"] = 0
        _set_ollama("absent", streak=0, restarts=0)
        return

    state = (info.get("State") or {})
    running = bool(state.get("Running"))
    started = _epoch(state.get("StartedAt") or "")

    if running and started and (time.time() - started) < STARTUP_GRACE:
        t["streak"] = 0
        _set_ollama("starting", streak=0, restarts=t["restarts"])
        return

    healthy = running and await ollama.is_healthy()

    if healthy:
        if t["streak"] or t["restarts"]:
            logger.info("Watchdog: ollama healthy again — incident cleared")
        t["streak"] = t["restarts"] = 0
        _set_ollama("ok", streak=0, restarts=0)
        return

    t["streak"] += 1
    logger.warning("Watchdog: ollama unhealthy (running=%s, streak=%s)",
                   running, t["streak"])
    _set_ollama("unhealthy", streak=t["streak"], restarts=t["restarts"])
    if t["streak"] < FAIL_THRESHOLD:
        return

    if t["restarts"] < OLLAMA_MAX_RESTARTS:
        logger.warning("Watchdog: restarting %s (restart %s/%s)",
                       ollama.CONTAINER, t["restarts"] + 1, OLLAMA_MAX_RESTARTS)
        ok = await containers.restart_container(ollama.CONTAINER, timeout=10)
        t["restarts"] += 1
        t["streak"] = 0
        _set_ollama("restarted", streak=0, restarts=t["restarts"],
                    last_action=("restart ok" if ok else "restart failed"))
    else:
        logger.error("Watchdog: ollama recovery exhausted — manual intervention needed")
        _set_ollama("exhausted", streak=t["streak"], restarts=t["restarts"],
                    last_action="exhausted")


async def run_loop():
    """The watchdog loop. Cancel-safe; runs for the lifetime of the manager."""
    if os.environ.get("ZMM_WATCHDOG_DISABLED"):
        _set("disabled")
        _set_ollama("disabled")
        logger.info("Watchdog disabled via ZMM_WATCHDOG_DISABLED")
        return

    logger.info("Watchdog active: interval=%ss grace=%ss threshold=%s max_restarts=%s",
                INTERVAL, STARTUP_GRACE, FAIL_THRESHOLD, MAX_RESTARTS)
    app_t = {"streak": 0, "restarts": 0}
    ollama_t = {"streak": 0, "restarts": 0}
    async with httpx.AsyncClient(verify=False, timeout=5.0) as http:
        while True:
            await asyncio.sleep(INTERVAL)
            # Each target in its own try/except so one can't starve the other.
            try:
                await _check_app(http, app_t)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Watchdog app-check error: %s", e)
            try:
                await _check_ollama(ollama_t)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Watchdog ollama-check error: %s", e)
