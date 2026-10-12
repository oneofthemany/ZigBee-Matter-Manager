"""
Chamber API — the room registry and device assignment.

Definitions live under `chambers:` in config.yaml; assignment rides on
device_settings[ieee]["chamber"], which already merges on write, is deleted with
the device, is in the backup set, and already ships to the frontend.
Zigbee-only for now. See docs/chambers.md.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

import yaml
from fastapi import FastAPI, Request

from modules import floor_plan_store
from modules.chambers import (
    build_registry,
    delete_chamber,
    duplicate_candidates,
    heating_device_rooms,
    levels,
    merge_chambers,
    reconcile,
    registry_ids,
    upsert_chamber,
    valid_id,
)

logger = logging.getLogger("routes.chambers")

CONFIG_PATH = "./config/config.yaml"
SETTINGS_PATH = "./data/device_settings.json"

#: Cap on one bulk assign. Large enough for "select all" on a real hive,
#: small enough that a malformed client cannot walk the whole device table.
MAX_BULK = 500

# Set by register_chamber_routes; the heating and floor-plan saves reach the
# device table through apply_room_edit without being handed the service.
_get_service: Optional[Callable[[], Any]] = None


def _load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {}


def _save_config(cfg: Dict[str, Any]) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp, CONFIG_PATH)


def _service():
    try:
        return _get_service() if _get_service else None
    except Exception:                                           # noqa: BLE001
        return None


def _set_device_chamber(svc, ieee: str, chamber_id: Optional[str]) -> None:
    """Write or clear one device's chamber, leaving its other settings alone."""
    existing = dict(svc.device_settings.get(ieee) or {})
    if chamber_id is None:
        existing.pop("chamber", None)
    else:
        existing["chamber"] = chamber_id
    if existing:
        svc.device_settings[ieee] = existing
    else:
        svc.device_settings.pop(ieee, None)


def _repoint(moves: Dict[str, Optional[str]], cfg: Dict[str, Any]) -> Dict[str, int]:
    """
    Carry chamber id changes (``{old: new or None}``) into everything keyed on
    one — devices, groups, saved frames — then put each heating-bound device in
    the chamber heating binds it to.
    """
    counts = {"devices": 0, "groups": 0, "frames": 0}
    svc = _service()
    if svc is not None:
        settings = svc.device_settings or {}
        wanted = {ieee: moves[s["chamber"]] for ieee, s in settings.items()
                  if isinstance(s, dict) and s.get("chamber") in moves}
        devices = getattr(svc, "devices", None) or {}
        for ieee, rid in heating_device_rooms(cfg).items():
            if ieee in devices and (settings.get(ieee) or {}).get("chamber") != rid:
                wanted[ieee] = rid
        for ieee, rid in wanted.items():
            _set_device_chamber(svc, ieee, rid)
        if wanted:
            svc._save_json(SETTINGS_PATH, svc.device_settings)
            counts["devices"] = len(wanted)

        gm = getattr(svc, "group_manager", None)
        if gm is not None and moves:
            stale = [g for g in gm.groups.values() if g.get("chamber") in moves]
            for g in stale:
                g["chamber"] = moves[g["chamber"]]
            if stale:
                gm.save_groups()
                counts["groups"] = len(stale)

    if moves:
        from routes.frame_routes import _load_frames, _save_frames
        frames = _load_frames()
        for frame in frames:
            old = frame.get("chambers") or []
            new = list(dict.fromkeys(moves.get(c, c) for c in old if moves.get(c, c)))
            if new != old:
                frame["chambers"] = new
                counts["frames"] += 1
        if counts["frames"]:
            _save_frames(frames)
    return counts


def apply_room_edit(cfg: Dict[str, Any], plan: Optional[Dict[str, Any]],
                    edited: Optional[str] = None,
                    prev_names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Run after heating or the floor plan changes its rooms, before the caller
    writes ``cfg`` and ``plan``: registers the rooms as chambers
    (`chambers.reconcile`) and re-points what the change moved.
    """
    result = reconcile(cfg, plan, edited=edited, prev_names=prev_names)
    try:
        _repoint(dict(result["merged"]), cfg)
    except Exception as e:                                      # noqa: BLE001
        logger.error(f"Could not carry chamber changes to devices: {e}")
    return result


def register_chamber_routes(app: FastAPI, get_zigbee_service, get_heating_controller=None):
    """Register chamber registry + device assignment routes."""
    global _get_service
    _get_service = get_zigbee_service

    def _payload(cfg: Dict[str, Any], plan: Optional[dict]) -> Dict[str, Any]:
        registry = build_registry(cfg, plan or {})
        return {"chambers": registry, "levels": levels(plan or {}),
                "duplicates": duplicate_candidates(registry)}

    async def _persist(cfg: Dict[str, Any], plan: Optional[dict], result: Dict[str, Any]) -> None:
        """Write what a registry change touched, and tell the running controller."""
        if result.get("plan_changed") and plan:
            floor_plan_store.save_plan(plan)
        _save_config(cfg)
        if not result.get("heating_changed") or not get_heating_controller:
            return
        ctrl = get_heating_controller()
        if ctrl is not None and hasattr(ctrl, "apply_config"):
            try:
                await ctrl.apply_config(cfg.get("heating") or {}, reason="chamber-rename")
            except Exception as e:                              # noqa: BLE001
                logger.warning(f"heating did not take the chamber rename live: {e}")

    # registry

    @app.get("/api/chambers")
    async def get_chambers():
        """The registry, known levels, and pairs that look like one room."""
        try:
            return {"success": True,
                    **_payload(_load_config(), floor_plan_store.load_plan())}
        except Exception as e:
            logger.error(f"Failed to build chamber registry: {e}")
            return {"success": False, "error": str(e),
                    "chambers": [], "levels": [], "duplicates": []}

    @app.post("/api/chambers")
    async def post_chamber(req: Request):
        """
        Create or update a chamber. Body: ``{id?, name, level?, icon?}``.

        With an ``id`` it renames, and heating and the floor plan follow.
        """
        try:
            raw = await req.json()
        except Exception as e:
            return {"success": False, "error": f"invalid JSON: {e}"}

        try:
            cfg = _load_config()
            plan = floor_plan_store.load_plan()
            chamber, err = upsert_chamber(cfg, raw)
            if err:
                return {"success": False, "error": err}
            await _persist(cfg, plan, apply_room_edit(cfg, plan))
            return {"success": True, "chamber": chamber, **_payload(cfg, plan)}
        except Exception as e:
            logger.error(f"Failed to save chamber: {e}")
            return {"success": False, "error": str(e)}

    @app.post("/api/chambers/merge")
    async def post_merge(req: Request):
        """
        Fold one chamber into another. Body: ``{source, into}``.

        ``into``'s name survives; devices, groups and frames follow.
        """
        try:
            body = await req.json()
        except Exception as e:
            return {"success": False, "error": f"invalid JSON: {e}"}

        try:
            cfg = _load_config()
            plan = floor_plan_store.load_plan()
            result, err = merge_chambers(cfg, plan, body.get("source"), body.get("into"))
            if err:
                return {"success": False, "error": err}
            await _persist(cfg, plan, result)
            moved = _repoint({**result["merged"], result["gone"]: result["survivor"]}, cfg)
            return {"success": True, "chamber": result["survivor"], "removed": result["gone"],
                    "moved": moved, **_payload(cfg, plan)}
        except Exception as e:
            logger.error(f"Failed to merge chambers: {e}")
            return {"success": False, "error": str(e)}

    @app.delete("/api/chambers/{chamber_id}")
    async def remove_chamber(chamber_id: str):
        """
        Delete a chamber neither heating nor the floor plan uses.

        Its devices and groups are unassigned in the same pass — left pointing
        at a chamber that no longer exists they would sit in a frame nobody
        can see.
        """
        try:
            cfg = _load_config()
            plan = floor_plan_store.load_plan()
            ok, err = delete_chamber(cfg, chamber_id, plan or {})
            if not ok:
                return {"success": False, "error": err}
            _save_config(cfg)
            cleared = _repoint({valid_id(chamber_id): None}, cfg)
            return {
                "success": True,
                "unassigned": cleared["devices"],
                "groups_cleared": cleared["groups"],
                **_payload(cfg, plan),
            }
        except Exception as e:
            logger.error(f"Failed to delete chamber {chamber_id}: {e}")
            return {"success": False, "error": str(e)}

    # assignment

    def _bound_error(cfg: Dict[str, Any], bound: str) -> str:
        name = next((c["name"] for c in build_registry(cfg) if c["id"] == bound), bound)
        return (f"heating uses this device in '{name}' — move it in heating, "
                f"and its chamber follows")


    @app.get("/api/chambers/assignments")
    async def get_assignments():
        """Every device that has a chamber, as ``{ieee: chamber_id}``."""
        try:
            svc = get_zigbee_service()
            if svc is None:
                return {"success": True, "assignments": {}}
            return {
                "success": True,
                "assignments": {
                    ieee: s["chamber"]
                    for ieee, s in (svc.device_settings or {}).items()
                    if isinstance(s, dict) and s.get("chamber")
                },
            }
        except Exception as e:
            logger.error(f"Failed to read chamber assignments: {e}")
            return {"success": False, "error": str(e), "assignments": {}}

    @app.post("/api/chambers/assign")
    async def assign_device(req: Request):
        """
        Assign one device to a chamber. Body: ``{ieee, chamber}``.

        ``chamber: null`` clears the assignment.
        """
        try:
            body = await req.json()
        except Exception as e:
            return {"success": False, "error": f"invalid JSON: {e}"}

        ieee = str(body.get("ieee") or "").strip().lower()
        if not ieee:
            return {"success": False, "error": "ieee required"}

        cfg = _load_config()
        raw_chamber = body.get("chamber")
        chamber_id: Optional[str] = None
        if raw_chamber not in (None, ""):
            chamber_id = valid_id(raw_chamber)
            if not chamber_id:
                return {"success": False, "error": f"invalid chamber id: {raw_chamber!r}"}
            if chamber_id not in registry_ids(cfg):
                # Reject unknown ids outright — silently accepting one would let a
                # typo create a phantom chamber that no frame ever renders.
                return {"success": False, "error": f"no chamber '{chamber_id}'"}

        bound = heating_device_rooms(cfg).get(ieee)
        if bound and bound != chamber_id:
            return {"success": False, "error": _bound_error(cfg, bound)}

        try:
            svc = get_zigbee_service()
            if svc is None:
                return {"success": False, "error": "zigbee service unavailable"}
            if ieee not in svc.devices:
                return {"success": False, "error": f"unknown device: {ieee}"}

            _set_device_chamber(svc, ieee, chamber_id)
            svc._save_json(SETTINGS_PATH, svc.device_settings)
            return {"success": True, "ieee": ieee, "chamber": chamber_id}
        except Exception as e:
            logger.error(f"Failed to assign {ieee} to chamber: {e}")
            return {"success": False, "error": str(e)}

    @app.post("/api/chambers/assign/bulk")
    async def assign_devices_bulk(req: Request):
        """
        Assign many devices at once. Body: ``{ieees: [...], chamber}``.

        Partial success is normal and reported per-device: unknown ieees, and
        devices heating binds to another chamber, are named in ``skipped``
        rather than failing the whole batch.
        """
        try:
            body = await req.json()
        except Exception as e:
            return {"success": False, "error": f"invalid JSON: {e}"}

        ieees = body.get("ieees")
        if not isinstance(ieees, list) or not ieees:
            return {"success": False, "error": "ieees must be a non-empty list"}
        if len(ieees) > MAX_BULK:
            return {"success": False, "error": f"too many devices (max {MAX_BULK})"}

        cfg = _load_config()
        raw_chamber = body.get("chamber")
        chamber_id: Optional[str] = None
        if raw_chamber not in (None, ""):
            chamber_id = valid_id(raw_chamber)
            if not chamber_id:
                return {"success": False, "error": f"invalid chamber id: {raw_chamber!r}"}
            if chamber_id not in registry_ids(cfg):
                return {"success": False, "error": f"no chamber '{chamber_id}'"}
        bound = heating_device_rooms(cfg)

        try:
            svc = get_zigbee_service()
            if svc is None:
                return {"success": False, "error": "zigbee service unavailable"}

            assigned: List[str] = []
            skipped: List[str] = []
            for raw_ieee in ieees:
                ieee = str(raw_ieee or "").strip().lower()
                # Heating's own room for a device wins; it is moved there.
                if (not ieee or ieee not in svc.devices
                        or bound.get(ieee, chamber_id) != chamber_id):
                    skipped.append(str(raw_ieee))
                    continue
                _set_device_chamber(svc, ieee, chamber_id)
                assigned.append(ieee)

            if assigned:
                svc._save_json(SETTINGS_PATH, svc.device_settings)

            return {
                "success": True,
                "chamber": chamber_id,
                "assigned": assigned,
                "skipped": skipped,
            }
        except Exception as e:
            logger.error(f"Failed bulk chamber assign: {e}")
            return {"success": False, "error": str(e)}
