"""
The one floor plan — heating, chambers and the Topology view all read this.

Stored at data/floor_plan.json. The first load on a hub that still keeps the
plan at ``heating.floor_plan`` in config.yaml copies it here; from then on the
old key is never read, and it is dropped the next time the plan routes rewrite
config.yaml anyway. See docs/floor-plan.md.

Held in memory after the first load, so a read costs no I/O and can be made
from the event loop. Callers get a copy; only ``save_plan`` changes the plan.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("modules.floor_plan_store")

PLAN_PATH = "./data/floor_plan.json"
CONFIG_PATH = "./config/config.yaml"

_UNLOADED = object()
_lock = threading.Lock()
_plan: Any = _UNLOADED


def _read_legacy() -> Optional[Dict[str, Any]]:
    if not os.path.exists(CONFIG_PATH):
        return None
    try:
        import yaml
        with open(CONFIG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:                                      # noqa: BLE001
        logger.warning(f"Could not read {CONFIG_PATH} for a legacy plan: {e}")
        return None
    plan = (cfg.get("heating") or {}).get("floor_plan")
    return plan if isinstance(plan, dict) else None


def _write(plan: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(PLAN_PATH) or ".", exist_ok=True)
    tmp = PLAN_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(plan, f, indent=2)
    os.replace(tmp, PLAN_PATH)


def _load_locked() -> Optional[Dict[str, Any]]:
    global _plan
    if _plan is not _UNLOADED:
        return _plan
    if os.path.exists(PLAN_PATH):
        try:
            with open(PLAN_PATH, "r") as f:
                loaded = json.load(f)
            _plan = loaded if isinstance(loaded, dict) else None
        except Exception as e:                                  # noqa: BLE001
            # Unreadable is not the same as absent: don't migrate over it.
            logger.error(f"Could not read {PLAN_PATH}: {e}")
            _plan = None
        return _plan
    legacy = _read_legacy()
    if legacy is not None:
        try:
            _write(legacy)
            logger.info(f"Moved the floor plan from heating.floor_plan to {PLAN_PATH}")
        except OSError as e:
            # Still served from memory; the next save writes the file.
            logger.error(f"Could not write {PLAN_PATH}: {e}")
    _plan = legacy
    return _plan


def load_plan() -> Optional[Dict[str, Any]]:
    """The saved plan, or None when none has been drawn."""
    with _lock:
        return copy.deepcopy(_load_locked())


def save_plan(plan: Dict[str, Any]) -> None:
    """Persist an already-cleaned plan (``floor_plan.clean_floor_plan``)."""
    global _plan
    with _lock:
        _write(plan)
        _plan = copy.deepcopy(plan)


def delete_plan() -> None:
    global _plan
    with _lock:
        if os.path.exists(PLAN_PATH):
            os.remove(PLAN_PATH)
        _plan = None


def drop_legacy_key(cfg: Dict[str, Any]) -> bool:
    """Remove ``heating.floor_plan`` from a config dict about to be written."""
    heating = cfg.get("heating")
    if isinstance(heating, dict) and "floor_plan" in heating:
        del heating["floor_plan"]
        return True
    return False


def reset(plan_path: Optional[str] = None, config_path: Optional[str] = None) -> None:
    """Forget the cached plan, and optionally point at other files (tests)."""
    global _plan, PLAN_PATH, CONFIG_PATH
    with _lock:
        _plan = _UNLOADED
        if plan_path:
            PLAN_PATH = plan_path
        if config_path:
            CONFIG_PATH = config_path
