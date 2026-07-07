"""Host OS status for the manager: pending package updates, reboot-required.

The manager runs in a container and cannot query the host package manager
directly. A host-side collector (scripts/os_updates.sh, installed by
install_watcher.sh, run by zmm-os-updates.timer every 6h) writes
``${DATA_DIR}/data/os_updates.json``; this module reads it and can request an
immediate re-check by touching the refresh trigger that the collector's
systemd path unit watches. Strictly read-only towards the host beyond that
trigger — applying updates stays a manual host task by design.
"""
import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("manager.host")

DATA_DIR = os.environ.get("ZMM_DATA_DIR") or os.environ.get("DATA_DIR") \
    or "/opt/.zigbee-matter-manager"
OS_UPDATES_FILE = os.path.join(DATA_DIR, "data", "os_updates.json")
REFRESH_TRIGGER = os.path.join(DATA_DIR, "data", "os_updates", "refresh")

# The collector runs every 6h; two missed runs means something is wrong on
# the host side and the data shouldn't be trusted as current.
STALE_AFTER = 13 * 3600


def _read() -> Optional[Dict[str, Any]]:
    try:
        with open(OS_UPDATES_FILE) as f:
            return json.load(f) or None
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("os_updates.json unreadable: %s", e)
        return None


def _age_seconds() -> Optional[int]:
    try:
        return max(0, int(time.time() - os.stat(OS_UPDATES_FILE).st_mtime))
    except OSError:
        return None


def summary() -> Dict[str, Any]:
    """Cheap host section for GET /status. Never raises.

    status: unknown (no data) | unsupported | ok | updates | attention
    (attention = security updates pending, reboot required, or newer kernel
    installed than running)."""
    d = _read()
    if d is None:
        return {"available": False, "status": "unknown"}
    age = _age_seconds()
    out = {
        "available": True,
        "os": d.get("os"),
        "pkg_manager": d.get("pkg_manager"),
        "update_count": d.get("update_count") or 0,
        "security_count": d.get("security_count") or 0,
        "reboot_required": bool(d.get("reboot_required")),
        "kernel_pending": bool(d.get("kernel_pending")),
        "checked_at": d.get("checked_at"),
        "age_seconds": age,
        "stale": age is not None and age > STALE_AFTER,
        "error": d.get("error"),
        "refresh_pending": os.path.isfile(REFRESH_TRIGGER),
    }
    if not d.get("pkg_manager"):
        out["status"] = "unsupported"
    elif out["security_count"] or out["reboot_required"] or out["kernel_pending"]:
        out["status"] = "attention"
    elif out["update_count"]:
        out["status"] = "updates"
    else:
        out["status"] = "ok"
    return out


def detail() -> Dict[str, Any]:
    """Full payload for the dashboard card (includes the package list)."""
    out = summary()
    d = _read() or {}
    out["packages"] = d.get("packages") or []
    out["kernel_running"] = d.get("kernel_running")
    out["kernel_latest"] = d.get("kernel_latest")
    out["uptime_seconds"] = d.get("uptime_seconds")
    return out


def request_refresh() -> Tuple[bool, str]:
    """Ask the host collector for an immediate re-check (path-unit trigger)."""
    try:
        os.makedirs(os.path.dirname(REFRESH_TRIGGER), exist_ok=True)
        with open(REFRESH_TRIGGER, "w") as f:
            f.write(str(int(time.time())))
        return True, ("Check requested — the host collector runs it now; "
                      "results appear within a minute or two")
    except Exception as e:
        logger.warning("request_refresh failed: %s", e)
        return False, str(e)
