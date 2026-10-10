"""Scheduled-backup health for the manager: read-only, from the files the
app's scheduler writes (modules/backup.py). See docs/backups.md.

Standalone by design: the manager never imports from modules/."""
import json
import os
import time
from typing import Any, Dict

_DATA_DIR = os.environ.get("ZMM_DATA_DIR") or os.environ.get("DATA_DIR") \
    or "/opt/.zigbee-matter-manager"
SCHEDULE = os.path.join(_DATA_DIR, "data", "backup_schedule.json")
STATUS = os.path.join(_DATA_DIR, "data", "backup_status.json")
STALE_AFTER_S = 2 * 86400     # kept in step with modules/backup.STALE_AFTER_S


def _read(path: str) -> Dict[str, Any]:
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def summary() -> Dict[str, Any]:
    """status: off | never | ok | failed | stale. Never raises."""
    sched, st = _read(SCHEDULE), _read(STATUS)
    enabled = bool(sched.get("enabled"))
    last_ok = st.get("last_success")
    if not enabled:
        status = "off"
    elif st.get("last_error"):
        status = "failed"
    elif not last_ok:
        status = "never"
    elif time.time() - float(last_ok) > STALE_AFTER_S:
        status = "stale"
    else:
        status = "ok"
    return {"status": status, "enabled": enabled, "time": sched.get("time"),
            "last_success": last_ok, "last_attempt": st.get("last_attempt"),
            "last_error": st.get("last_error"), "last_file": st.get("last_file"),
            "last_size": st.get("last_size"), "target": st.get("target"),
            "encrypted": st.get("encrypted")}
