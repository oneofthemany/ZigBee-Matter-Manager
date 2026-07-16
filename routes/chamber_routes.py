"""
Chamber API routes — the Frames-side room registry and device assignment.

Endpoints:
    GET    /api/chambers                 — registry (config + adopted rooms)
    POST   /api/chambers                 — create/update a chamber
    DELETE /api/chambers/{chamber_id}    — remove a Frames-owned chamber
    GET    /api/chambers/assignments     — {ieee: chamber_id}
    POST   /api/chambers/assign          — assign one device
    POST   /api/chambers/assign/bulk     — assign many devices at once

Storage:
    Chamber definitions: ``chambers:`` in ``config/config.yaml``.
    Device assignment:   ``device_settings[ieee]["chamber"]``
                         (``data/device_settings.json``).

Why device_settings rather than a new file — it merges rather than clobbers on
write (core/service.py: ``existing.update(...)`` in ``configure_device``), it is
deleted with the device on removal, it is already in the backup set
(routes/backup_routes.py), and it already ships to the frontend as ``settings``
on every device from ``get_device_list()``. So assignment needs no read API and
no new backup wiring.

Phase 1 is Zigbee-only: assignment validates against ``zigbee_service.devices``.
Matter/AC/media devices are not assignable yet by design.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import yaml
from fastapi import FastAPI, Request

from modules.chambers import (
    build_registry,
    delete_chamber,
    levels,
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


def register_chamber_routes(app: FastAPI, get_zigbee_service):
    """Register chamber registry + device assignment routes."""

    def _assign(svc, ieee: str, chamber_id: Optional[str]) -> None:
        """
        Write (or clear) one device's chamber.

        Merges into the existing settings dict so unrelated keys — polling
        interval, reporting config — survive untouched.
        """
        existing = dict(svc.device_settings.get(ieee) or {})
        if chamber_id is None:
            existing.pop("chamber", None)
        else:
            existing["chamber"] = chamber_id
        if existing:
            svc.device_settings[ieee] = existing
        else:
            svc.device_settings.pop(ieee, None)

    # ─────────────────────────── registry ───────────────────────────

    @app.get("/api/chambers")
    async def get_chambers():
        """
        The chamber registry, plus known levels.

        Entries adopted from heating / the floor plan are returned with
        ``editable: false`` — they exist because another subsystem defines them.
        """
        try:
            cfg = _load_config()
            return {
                "success": True,
                "chambers": build_registry(cfg),
                "levels": levels(cfg),
            }
        except Exception as e:
            logger.error(f"Failed to build chamber registry: {e}")
            return {"success": False, "error": str(e), "chambers": [], "levels": []}

    @app.post("/api/chambers")
    async def post_chamber(req: Request):
        """Create or update a chamber. Body: ``{id?, name, level?, icon?}``."""
        try:
            raw = await req.json()
        except Exception as e:
            return {"success": False, "error": f"invalid JSON: {e}"}

        try:
            cfg = _load_config()
            chamber, err = upsert_chamber(cfg, raw)
            if err:
                return {"success": False, "error": err}
            _save_config(cfg)
            return {"success": True, "chamber": chamber, "chambers": build_registry(cfg)}
        except Exception as e:
            logger.error(f"Failed to save chamber: {e}")
            return {"success": False, "error": str(e)}

    @app.delete("/api/chambers/{chamber_id}")
    async def remove_chamber(chamber_id: str):
        """
        Delete a Frames-owned chamber.

        Devices assigned to it are unassigned in the same pass — leaving them
        pointing at a chamber that no longer exists would strand them in a frame
        nobody can see.
        """
        try:
            cfg = _load_config()
            ok, err = delete_chamber(cfg, chamber_id)
            if not ok:
                # delete_chamber may still have mutated cfg (config entry cleared
                # for an adopted room); persist that, but report the truth.
                if err and "only its Frames-specific settings were cleared" in err:
                    _save_config(cfg)
                    return {"success": False, "error": err, "chambers": build_registry(cfg)}
                return {"success": False, "error": err}

            _save_config(cfg)

            unassigned = 0
            svc = get_zigbee_service()
            if svc is not None:
                cid = valid_id(chamber_id)
                stale = [
                    ieee for ieee, s in (svc.device_settings or {}).items()
                    if isinstance(s, dict) and s.get("chamber") == cid
                ]
                for ieee in stale:
                    _assign(svc, ieee, None)
                if stale:
                    svc._save_json(SETTINGS_PATH, svc.device_settings)
                    unassigned = len(stale)

            return {
                "success": True,
                "unassigned": unassigned,
                "chambers": build_registry(cfg),
            }
        except Exception as e:
            logger.error(f"Failed to delete chamber {chamber_id}: {e}")
            return {"success": False, "error": str(e)}

    # ────────────────────────── assignment ──────────────────────────

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

        raw_chamber = body.get("chamber")
        chamber_id: Optional[str] = None
        if raw_chamber not in (None, ""):
            chamber_id = valid_id(raw_chamber)
            if not chamber_id:
                return {"success": False, "error": f"invalid chamber id: {raw_chamber!r}"}
            if chamber_id not in registry_ids(_load_config()):
                # Reject unknown ids outright — silently accepting one would let a
                # typo create a phantom chamber that no frame ever renders.
                return {"success": False, "error": f"no chamber '{chamber_id}'"}

        try:
            svc = get_zigbee_service()
            if svc is None:
                return {"success": False, "error": "zigbee service unavailable"}
            if ieee not in svc.devices:
                return {"success": False, "error": f"unknown device: {ieee}"}

            _assign(svc, ieee, chamber_id)
            svc._save_json(SETTINGS_PATH, svc.device_settings)
            return {"success": True, "ieee": ieee, "chamber": chamber_id}
        except Exception as e:
            logger.error(f"Failed to assign {ieee} to chamber: {e}")
            return {"success": False, "error": str(e)}

    @app.post("/api/chambers/assign/bulk")
    async def assign_devices_bulk(req: Request):
        """
        Assign many devices at once. Body: ``{ieees: [...], chamber}``.

        Partial success is normal and reported per-device: unknown ieees are
        skipped and named in ``skipped`` rather than failing the whole batch.
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

        raw_chamber = body.get("chamber")
        chamber_id: Optional[str] = None
        if raw_chamber not in (None, ""):
            chamber_id = valid_id(raw_chamber)
            if not chamber_id:
                return {"success": False, "error": f"invalid chamber id: {raw_chamber!r}"}
            if chamber_id not in registry_ids(_load_config()):
                return {"success": False, "error": f"no chamber '{chamber_id}'"}

        try:
            svc = get_zigbee_service()
            if svc is None:
                return {"success": False, "error": "zigbee service unavailable"}

            assigned: List[str] = []
            skipped: List[str] = []
            for raw_ieee in ieees:
                ieee = str(raw_ieee or "").strip().lower()
                if not ieee or ieee not in svc.devices:
                    skipped.append(str(raw_ieee))
                    continue
                _assign(svc, ieee, chamber_id)
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
