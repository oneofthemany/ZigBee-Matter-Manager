"""Host OS status + updates for the manager.

The manager runs in a container and cannot touch the host package manager
directly. Host-side helpers (installed by install_watcher.sh) do the work:

  - scripts/os_updates.sh — read-only collector (6h timer + on-demand path
    unit) writing ``${DATA_DIR}/data/os_updates.json``.
  - scripts/os_apply.sh — apply worker (on-demand path unit) that applies
    package updates or an OS release upgrade, writing progress to
    ``${DATA_DIR}/data/os_updates/apply_status.json``.

This module reads those files and requests work by writing the trigger files
the hosts' systemd path units watch: ``refresh`` (re-check), ``apply``
(install pending updates) and ``release_upgrade`` (distro upgrade — the host
REBOOTS at the end; the dashboard warns before triggering it).
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
APPLY_TRIGGER = os.path.join(DATA_DIR, "data", "os_updates", "apply")
RELEASE_TRIGGER = os.path.join(DATA_DIR, "data", "os_updates", "release_upgrade")
APPLY_STATUS_FILE = os.path.join(DATA_DIR, "data", "os_updates", "apply_status.json")

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


def apply_status() -> Optional[Dict[str, Any]]:
    """Last/current os_apply.sh run, written by the host-side worker."""
    try:
        with open(APPLY_STATUS_FILE) as f:
            st = json.load(f) or None
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("apply_status.json unreadable: %s", e)
        return None
    if st is not None:
        st["apply_pending"] = os.path.isfile(APPLY_TRIGGER)
        st["release_pending"] = os.path.isfile(RELEASE_TRIGGER)
    return st


def detail() -> Dict[str, Any]:
    """Full payload for the dashboard card (includes the package list)."""
    out = summary()
    d = _read() or {}
    out["packages"] = d.get("packages") or []
    out["kernel_running"] = d.get("kernel_running")
    out["kernel_latest"] = d.get("kernel_latest")
    out["uptime_seconds"] = d.get("uptime_seconds")
    out["os_release_current"] = d.get("os_release_current")
    out["os_release_available"] = d.get("os_release_available")
    out["os_release_automated"] = bool(d.get("os_release_automated"))
    out["apply"] = apply_status()
    out["apply_pending"] = os.path.isfile(APPLY_TRIGGER) or os.path.isfile(RELEASE_TRIGGER)
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


def _apply_running() -> bool:
    st = apply_status() or {}
    return st.get("state") in ("running", "rebooting") \
        or os.path.isfile(APPLY_TRIGGER) or os.path.isfile(RELEASE_TRIGGER)


def request_apply() -> Tuple[bool, str]:
    """Ask the host worker to install the pending package updates."""
    if _apply_running():
        return False, "an OS update/upgrade is already in progress"
    try:
        os.makedirs(os.path.dirname(APPLY_TRIGGER), exist_ok=True)
        with open(APPLY_TRIGGER, "w") as f:
            f.write(str(int(time.time())))
        return True, ("Update started — progress lands in os_apply.log and the "
                      "card refreshes when it finishes")
    except Exception as e:
        logger.warning("request_apply failed: %s", e)
        return False, str(e)


def request_release_upgrade(target: str) -> Tuple[bool, str]:
    """Ask the host worker to upgrade the OS release. The host reboots."""
    target = (target or "").strip()
    # Version-shaped only — this string ends up in a root shell's --releasever.
    if not target or not all(c.isdigit() or c == "." for c in target):
        return False, f"invalid target release: {target!r}"
    d = _read() or {}
    if str(d.get("os_release_available") or "") != target:
        return False, (f"release {target} is not the one the collector "
                       f"detected ({d.get('os_release_available')!r}) — re-check first")
    if not d.get("os_release_automated"):
        return False, ("this distro has no sanctioned non-interactive release "
                       "upgrade — do it manually on the host")
    if _apply_running():
        return False, "an OS update/upgrade is already in progress"
    try:
        os.makedirs(os.path.dirname(RELEASE_TRIGGER), exist_ok=True)
        with open(RELEASE_TRIGGER, "w") as f:
            f.write(target)
        return True, (f"Release upgrade to {target} started — the host downloads "
                      "the new release, then REBOOTS to install it")
    except Exception as e:
        logger.warning("request_release_upgrade failed: %s", e)
        return False, str(e)
