"""
Refuses app-initiated restarts while an upgrade is being verified.

After a container swap the host watcher health-checks the new version and then
soaks it for a stability period (scripts/upgrade.sh). To that watcher a
deliberate restart is indistinguishable from a crash, so a "Save & Restart" in
that window rolls back a release that was actually fine.

This is the single place that answers "may the app restart itself right now?".
Every restart entry point asks it first, so the answer cannot be bypassed by
adding another button.

The window is read from the watcher's own status.json rather than timed
locally, because only the watcher knows when it started probing and when it
stopped. See docs/upgrades.md.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("modules.restart_guard")

#: Watcher states during which the app must not restart itself. `swapping`
#: covers the health check and the stability soak; `rolling_back` is the
#: recovery those failures trigger, and restarting into it is worse still.
BLOCKING_STATES = ("swapping", "rolling_back")

#: Mirrors ZMM_HEALTH_TIMEOUT / ZMM_STABILITY_SOAK in scripts/upgrade.sh.
#: The app is under observation for BOTH phases — the health check AND the
#: stability soak that follows it — so the window is the sum, never just the
#: health timeout. With the defaults that is 300 + 180 = 480s (8 minutes).
HEALTH_TIMEOUT_S = int(os.environ.get("ZMM_HEALTH_TIMEOUT", "300"))
STABILITY_SOAK_S = int(os.environ.get("ZMM_STABILITY_SOAK", "180"))
VERIFY_WINDOW_S = HEALTH_TIMEOUT_S + STABILITY_SOAK_S

#: The watcher writes status.json once when it begins health-checking and then
#: not again for the whole verify window, so "stale" must be comfortably longer
#: than that window. Past it we assume the watcher died mid-swap and stop
#: blocking, because a hub that can never restart is worse than one that can
#: restart at an awkward moment.
#:
#: These env vars are read by upgrade.sh on the HOST; the container only sees
#: them if they are passed through. If they are raised on the host without
#: being mirrored here, this floor — not the sum above — is what keeps the
#: guard closed for the real window, so it is deliberately generous and
#: separately overridable.
STALE_AFTER_S = max(
    int(os.environ.get("ZMM_RESTART_GUARD_STALE_S", "0")) or 0,
    VERIFY_WINDOW_S + 420,
    1800,
)


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse the watcher's UTC timestamps (`2026-08-30T10:26:36Z`)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(status: Dict[str, Any]) -> Optional[float]:
    ts = _parse_ts(status.get("updated_at")) or _parse_ts(status.get("started_at"))
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def upgrade_block(status: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    The block imposed by an in-flight upgrade, or None if there isn't one.

    `status` is injectable so callers and tests can avoid a second file read.
    """
    if status is None:
        try:
            from modules.upgrade_manager import read_status
            status = read_status()
        except Exception as e:
            # Never let a bookkeeping failure block a restart the user needs.
            logger.warning("restart guard: could not read upgrade status (%s)", e)
            return None

    state = (status.get("state") or "").strip()
    if state not in BLOCKING_STATES:
        return None

    age = _age_seconds(status)
    if age is not None and age > STALE_AFTER_S:
        logger.warning(
            "restart guard: upgrade status is %s but %.0fs stale — allowing restart",
            state, age,
        )
        return None

    target = status.get("target_version")
    remaining = VERIFY_WINDOW_S - age if age is not None else VERIFY_WINDOW_S
    remaining = max(15, int(remaining))

    if state == "rolling_back":
        message = (
            "An upgrade is being rolled back. Restarting now would interrupt the "
            "rollback and could leave no healthy container running."
        )
    else:
        release = f"v{target}" if target else "a new version"
        message = (
            f"An upgrade to {release} is being health-checked. A restart now is "
            "indistinguishable from a crash, so it would trigger an automatic "
            "rollback of a release that is otherwise fine."
        )

    return {
        "code": "upgrade_verifying",
        "state": state,
        "target_version": target,
        "current_step": status.get("current_step") or "",
        "message": message,
        "retry_after_s": remaining,
    }


def bringup_block(bringup: Optional[str]) -> Optional[Dict[str, Any]]:
    """The block imposed by the app's own boot still being in progress."""
    if bringup != "starting":
        return None
    return {
        "code": "bringup_in_progress",
        "state": "starting",
        "target_version": None,
        "current_step": "",
        "message": (
            "The application is still starting up. Restarting mid-boot can leave "
            "the radio and database in an inconsistent state."
        ),
        "retry_after_s": 30,
    }


def restart_block(bringup: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Why a restart must not happen right now, or None when one is allowed.

    An upgrade beats a bring-up: during a swap the app is booting *because* of
    the upgrade, and the upgrade explains it better.
    """
    return upgrade_block() or bringup_block(bringup)


def restart_status(bringup: Optional[str] = None) -> Dict[str, Any]:
    """Guard state shaped for the API and the UI."""
    block = restart_block(bringup)
    if block is None:
        return {"allowed": True, "reason": None}
    return {"allowed": False, "reason": block}
