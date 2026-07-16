"""
Frames API routes — auto-generated dashboards from chamber + device type.

Endpoints:
    GET /api/frames/auto        — grouped cells for a frame
    GET /api/frames/cells       — flat cell list (no grouping), for the picker

Query params (both):
    split=chamber|type          — group by room, or by device type
    chambers=a,b                — restrict to these chambers
    kinds=light,switch          — restrict to these cell kinds

Phase 3 is Zigbee-only: AC units, media players and heating are not cells yet.
Frame persistence (data/frames.json) lands with the custom frame builder.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import yaml
from fastapi import FastAPI

from modules.chambers import build_registry
from modules.frames import (
    CELL_LABELS,
    CELL_ORDER,
    VALID_SPLITS,
    build_auto_frame,
    is_zigbee,
    resolve_cell,
)

logger = logging.getLogger("routes.frames")

CONFIG_PATH = "./config/config.yaml"


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


def register_frame_routes(app: FastAPI, get_zigbee_service):
    """Register Frames layout routes."""

    def _devices() -> List[Dict[str, Any]]:
        svc = get_zigbee_service()
        if svc is None:
            return []
        return svc.get_device_list() or []

    @app.get("/api/frames/auto")
    async def get_auto_frame(
        split: str = "chamber",
        chambers: Optional[str] = None,
        kinds: Optional[str] = None,
    ):
        """
        A frame, laid out automatically.

        An unknown ``split`` falls back to chamber rather than erroring — a bad
        query param shouldn't leave the user staring at a blank dashboard.
        """
        try:
            registry = build_registry(_load_config())
            frame = build_auto_frame(
                _devices(),
                split=split,
                chambers=registry,
                include_chambers=_csv(chambers),
                include_kinds=_csv(kinds),
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
