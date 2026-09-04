"""
Frames API — auto-generated dashboards from chamber + device type.

A frame is only filters over the live hive, never stored device state, so it
cannot go stale; saved frames live in data/frames.json. Zigbee-only for now.
Endpoints and query params: docs/frames.md.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import yaml
from fastapi import FastAPI, Request

from modules.chambers import build_registry, levels as chamber_levels
from modules.frames import (
    CELL_LABELS,
    CELL_ORDER,
    HIDDEN_KEY,
    VALID_SPLITS,
    build_auto_frame,
    clean_frames,
    delete_frame,
    is_zigbee,
    render_saved_frame,
    resolve_cell,
    upsert_frame,
)

logger = logging.getLogger("routes.frames")

CONFIG_PATH = "./config/config.yaml"
FRAMES_PATH = "./data/frames.json"
SETTINGS_PATH = "./data/device_settings.json"

#: Cap on one visibility change. Same reasoning as the chamber bulk assign:
#: large enough for "hide every member of this group" on a real hive, small
#: enough that a malformed client cannot walk the whole device table.
MAX_BULK = 500


def _load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {}


def _csv(value: Optional[str]) -> List[str]:
    """Parse a comma-separated query param into a clean list."""
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _load_frames() -> List[dict]:
    """
    Saved frames, normalised.

    A corrupt or hand-mangled frames file yields an empty list rather than a
    500 — losing your saved layouts is bad, but a dashboard that won't load at
    all is worse, and the auto frames still work.
    """
    if not os.path.exists(FRAMES_PATH):
        return []
    try:
        with open(FRAMES_PATH, "r") as f:
            return clean_frames(json.load(f))
    except Exception as e:
        logger.error(f"Could not read {FRAMES_PATH}: {e}")
        return []


def _save_frames(frames: List[dict]) -> None:
    os.makedirs(os.path.dirname(FRAMES_PATH), exist_ok=True)
    tmp = FRAMES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(frames, f, indent=2)
    os.replace(tmp, FRAMES_PATH)


def register_frame_routes(app: FastAPI, get_zigbee_service):
    """Register Frames layout routes."""

    def _devices() -> List[Dict[str, Any]]:
        svc = get_zigbee_service()
        if svc is None:
            return []
        return svc.get_device_list() or []

    def _device_groups() -> List[Dict[str, Any]]:
        """Zigbee groups, so a chamber-assigned one gets its own cell — [] if groups aren't wired up yet."""
        svc = get_zigbee_service()
        if svc is None or not hasattr(svc, "group_manager"):
            return []
        return svc.group_manager.get_all_groups() or []

    @app.get("/api/frames/auto")
    async def get_auto_frame(
        split: str = "chamber",
        chambers: Optional[str] = None,
        kinds: Optional[str] = None,
        hidden: bool = False,
    ):
        """
        A frame, laid out automatically.

        An unknown ``split`` falls back to chamber rather than erroring — a bad
        query param shouldn't leave the user staring at a blank dashboard.

        ``hidden=true`` includes hidden devices, for the visibility editor.
        """
        try:
            cfg = _load_config()
            frame = build_auto_frame(
                _devices(),
                split=split,
                chambers=build_registry(cfg),
                include_chambers=_csv(chambers),
                include_kinds=_csv(kinds),
                levels=chamber_levels(cfg),
                device_groups=_device_groups(),
                include_hidden=hidden,
            )
            return {"success": True, **frame}
        except Exception as e:
            logger.error(f"Failed to build auto frame: {e}")
            return {"success": False, "error": str(e), "groups": [], "total": 0}

    @app.get("/api/frames/cells")
    async def get_cells():
        """
        Every device as a flat cell, ungrouped.

        Feeds the frame builder's picker, which needs the whole hive regardless
        of how any one frame is split.
        """
        try:
            cells = [resolve_cell(d) for d in _devices() if is_zigbee(d)]
            cells.sort(key=lambda c: c["name"].lower())
            return {"success": True, "cells": cells, "total": len(cells)}
        except Exception as e:
            logger.error(f"Failed to build cell list: {e}")
            return {"success": False, "error": str(e), "cells": [], "total": 0}

    @app.get("/api/frames/kinds")
    async def get_kinds():
        """Cell kinds and their labels, so the frontend doesn't hardcode them."""
        return {
            "success": True,
            "splits": list(VALID_SPLITS),
            "kinds": [{"kind": k, "label": CELL_LABELS.get(k, k)} for k in CELL_ORDER],
        }

    # visibility

    def _set_hidden(svc, ieee: str, hidden: bool) -> None:
        """
        Write (or clear) one device's Frames visibility.

        Merges into the existing settings dict so unrelated keys — chamber,
        polling interval, reporting config — survive untouched, and drops the
        key entirely when shown again rather than storing a false.
        """
        existing = dict(svc.device_settings.get(ieee) or {})
        if hidden:
            existing[HIDDEN_KEY] = True
        else:
            existing.pop(HIDDEN_KEY, None)
        if existing:
            svc.device_settings[ieee] = existing
        else:
            svc.device_settings.pop(ieee, None)

    @app.get("/api/frames/hidden")
    async def get_hidden():
        """Every device hidden from Frames, as a list of ieees."""
        try:
            svc = get_zigbee_service()
            if svc is None:
                return {"success": True, "hidden": []}
            return {
                "success": True,
                "hidden": [
                    ieee for ieee, settings in (svc.device_settings or {}).items()
                    if isinstance(settings, dict) and settings.get(HIDDEN_KEY)
                ],
            }
        except Exception as e:
            logger.error(f"Failed to read hidden devices: {e}")
            return {"success": False, "error": str(e), "hidden": []}

    @app.post("/api/frames/hidden")
    async def set_hidden(req: Request):
        """
        Hide or show devices. Body: ``{ieee | ieees: [...], hidden: bool}``.

        One route for one and many because hiding every member of a group is
        the reason this exists — a per-device round trip for that would be a
        worse API for the only case that motivated it.

        Partial success is normal and reported: unknown ieees are skipped and
        named rather than failing the whole batch, which matters when a group
        still lists a device that has since left the hive.
        """
        try:
            body = await req.json()
        except Exception as e:
            return {"success": False, "error": f"invalid JSON: {e}"}

        raw_ieees = body.get("ieees")
        if raw_ieees is None:
            single = body.get("ieee")
            raw_ieees = [single] if single else []
        if not isinstance(raw_ieees, list) or not raw_ieees:
            return {"success": False, "error": "ieee or ieees required"}
        if len(raw_ieees) > MAX_BULK:
            return {"success": False, "error": f"too many devices (max {MAX_BULK})"}

        hidden = bool(body.get("hidden"))

        try:
            svc = get_zigbee_service()
            if svc is None:
                return {"success": False, "error": "zigbee service unavailable"}

            changed: List[str] = []
            skipped: List[str] = []
            for raw_ieee in raw_ieees:
                ieee = str(raw_ieee or "").strip().lower()
                if not ieee or ieee not in svc.devices:
                    skipped.append(str(raw_ieee))
                    continue
                _set_hidden(svc, ieee, hidden)
                changed.append(ieee)

            if changed:
                svc._save_json(SETTINGS_PATH, svc.device_settings)

            return {"success": True, "hidden": hidden, "changed": changed, "skipped": skipped}
        except Exception as e:
            logger.error(f"Failed to set Frames visibility: {e}")
            return {"success": False, "error": str(e)}

    # saved frames

    @app.get("/api/frames")
    async def list_frames():
        """Saved frames (definitions, not rendered layouts)."""
        try:
            return {"success": True, "frames": _load_frames()}
        except Exception as e:
            logger.error(f"Failed to list frames: {e}")
            return {"success": False, "error": str(e), "frames": []}

    @app.post("/api/frames")
    async def post_frame(req: Request):
        """Create or update a saved frame. Body: ``{id?, name, split, chambers[], kinds[], devices[], order[]}``."""
        try:
            raw = await req.json()
        except Exception as e:
            return {"success": False, "error": f"invalid JSON: {e}"}

        try:
            frames = _load_frames()
            frame, err = upsert_frame(frames, raw)
            if err:
                return {"success": False, "error": err}
            _save_frames(frames)
            return {"success": True, "frame": frame, "frames": frames}
        except Exception as e:
            logger.error(f"Failed to save frame: {e}")
            return {"success": False, "error": str(e)}

    # NOTE: the two routes below take a path param and MUST stay declared after
    # /auto, /cells, /kinds and /hidden — FastAPI matches in declaration order,
    # so moving them up would swallow those literals as frame ids.

    @app.delete("/api/frames/{frame_id}")
    async def remove_frame(frame_id: str):
        """Delete a saved frame."""
        try:
            frames = _load_frames()
            ok, err = delete_frame(frames, frame_id)
            if not ok:
                return {"success": False, "error": err}
            _save_frames(frames)
            return {"success": True, "frames": frames}
        except Exception as e:
            logger.error(f"Failed to delete frame {frame_id}: {e}")
            return {"success": False, "error": str(e)}

    @app.get("/api/frames/{frame_id}")
    async def get_frame(frame_id: str, hidden: bool = False):
        """Render a saved frame against the live hive."""
        try:
            frame = next((f for f in _load_frames() if f["id"] == (frame_id or "").strip().lower()), None)
            if not frame:
                return {"success": False, "error": f"no frame '{frame_id}'", "groups": [], "total": 0}
            cfg = _load_config()
            rendered = render_saved_frame(
                frame, _devices(), build_registry(cfg), chamber_levels(cfg),
                device_groups=_device_groups(), include_hidden=hidden,
            )
            return {"success": True, **rendered, "frame": frame}
        except Exception as e:
            logger.error(f"Failed to render frame {frame_id}: {e}")
            return {"success": False, "error": str(e), "groups": [], "total": 0}
