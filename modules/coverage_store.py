"""
Saved signal heatmaps: each fresh estimate of the whole mesh, kept so the
editor can show the last one at once while it works out a new one, and so two
can be compared. docs/signal-coverage.md § Snapshots.

One JSON file per snapshot under data/coverage/, named by its id (the UTC time
it was taken, which sorts). The newest ``KEEP`` are kept. A snapshot that
matches the newest one in everything but the time only moves that one's
``checked_at``, so the history holds changes, not repeats.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("modules.coverage_store")

DIR = "./data/coverage"
KEEP = 50
_ID = re.compile(r"^\d{8}T\d{6}Z(-\d+)?$")
_lock = threading.Lock()


def plan_hash(plan: Optional[dict]) -> str:
    return hashlib.sha1(json.dumps(plan or {}, sort_keys=True).encode()).hexdigest()[:16]


def _fingerprint(snap: Dict[str, Any]) -> str:
    """What makes two snapshots different: the plan, the learned model, the
    fields, and each device's signal to the dB."""
    key = {
        "plan": snap.get("plan_hash"),
        "model": snap.get("model"),
        "levels": [(l["level_id"], l["field"]["data"]) for l in snap.get("levels") or []],
        "devices": sorted((d["ieee"], round(d["dbm"]), d["weak"]) for d in snap.get("device_signal") or []),
    }
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()


def _path(snap_id: str) -> str:
    return os.path.join(DIR, f"{snap_id}.json")


def _ids() -> List[str]:
    try:
        names = os.listdir(DIR)
    except FileNotFoundError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json") and _ID.match(n[:-5]))


def _read(snap_id: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_path(snap_id)) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:                                      # noqa: BLE001
        logger.warning(f"Unreadable coverage snapshot {snap_id}: {e}")
        return None


def _write(snap: Dict[str, Any]) -> None:
    os.makedirs(DIR, exist_ok=True)
    tmp = _path(snap["id"]) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f, separators=(",", ":"))
    os.replace(tmp, _path(snap["id"]))


def save(result: Dict[str, Any], plan: Optional[dict], now: Optional[float] = None) -> Dict[str, Any]:
    """Keep ``result`` (radio_model.analyse of the whole mesh) as a snapshot.

    Returns the snapshot's ``{id, taken_at, checked_at, new}``: ``new`` is
    False when it matched the newest and only that one's check time moved.
    """
    now = time.time() if now is None else now
    snap = {k: result[k] for k in ("model", "calibration", "weak", "suggestions", "thresholds",
                                    "device_signal", "devices", "coordinator", "levels", "summary")
            if k in result}
    snap["plan_hash"] = plan_hash(plan)
    snap["fingerprint"] = _fingerprint(snap)
    with _lock:
        ids = _ids()
        latest = _read(ids[-1]) if ids else None
        if latest and latest.get("fingerprint") == snap["fingerprint"]:
            latest["checked_at"] = now
            _write(latest)
            return {"id": latest["id"], "taken_at": latest["taken_at"], "checked_at": now, "new": False}
        snap_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        n = 1
        while snap_id in ids or os.path.exists(_path(snap_id)):
            snap_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}-{n}"
            n += 1
        snap.update({"id": snap_id, "taken_at": now, "checked_at": now})
        _write(snap)
        for old in (ids + [snap_id])[:-KEEP]:
            try:
                os.remove(_path(old))
            except OSError as e:
                logger.warning(f"Could not prune coverage snapshot {old}: {e}")
        return {"id": snap_id, "taken_at": now, "checked_at": now, "new": True}


def latest() -> Optional[Dict[str, Any]]:
    with _lock:
        for snap_id in reversed(_ids()):
            snap = _read(snap_id)
            if snap:
                return snap
    return None


def get(snap_id: str) -> Optional[Dict[str, Any]]:
    if not _ID.match(snap_id or ""):
        return None
    with _lock:
        return _read(snap_id)


def history() -> List[Dict[str, Any]]:
    """Newest first: each snapshot's id, times, plan and headline figures."""
    out = []
    with _lock:
        for snap_id in reversed(_ids()):
            snap = _read(snap_id)
            if not snap:
                continue
            out.append({"id": snap["id"], "taken_at": snap["taken_at"],
                        "checked_at": snap.get("checked_at", snap["taken_at"]),
                        "plan_hash": snap.get("plan_hash"), "summary": snap.get("summary"),
                        "weak": len(snap.get("weak") or []),
                        "samples": (snap.get("model") or {}).get("samples")})
    return out


def reset(directory: Optional[str] = None) -> None:
    """Point at another directory (tests)."""
    global DIR
    if directory:
        DIR = directory
