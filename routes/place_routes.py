"""
Named places API.

Read is available to anything holding `presence:read` or any `presence:write`
scope — the phone needs the list to register its geofences, its token carries
only `presence:write:<user>`, and a place is household configuration rather
than anyone's personal location. Writes are admin-only: a place defines where
automations fire, so moving one silently changes behaviour for everybody.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from modules.auth_middleware import Principal, require_scope
from modules.places import (
    DEFAULT_PLACE_RADIUS_M, MAX_PLACES, get_place_manager,
)

logger = logging.getLogger("routes.places")


class PlaceUpsert(BaseModel):
    id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=64)
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    radius_m: float = Field(DEFAULT_PLACE_RADIUS_M, gt=0, le=50_000)
    enabled: bool = True
    icon: str = "map-marker-alt"


def require_places_read(request: Request) -> Principal:
    """presence:read, or a phone's presence:write:<user> (it arms geofences from the list)."""
    from modules.auth import scope_matches
    p: Optional[Principal] = getattr(request.state, "principal", None)
    if p is None:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    if scope_matches("presence:read", p.scopes) or any(
            s == "presence:write" or s.startswith("presence:write:") for s in p.scopes):
        return p
    raise HTTPException(403, "Token lacks scope: presence:read or presence:write")


def register_place_routes(app: FastAPI) -> None:

    def _mgr():
        m = get_place_manager()
        if not m:
            raise HTTPException(503, "Place manager not initialised")
        return m

    @app.get("/api/places")
    async def list_places(_=Depends(require_places_read)):
        return {"places": _mgr().list(), "max": MAX_PLACES}

    @app.post("/api/places")
    async def upsert_place(
            payload: PlaceUpsert,
            _=Depends(require_scope("admin")),
    ):
        data = payload.dict()
        if not data.get("id"):
            data.pop("id", None)          # let the manager slugify the name
        result = _mgr().upsert(data)
        if not result.get("success"):
            raise HTTPException(400, result.get("error"))
        logger.info("[places] upserted %s", result["place"]["id"])
        return result

    @app.delete("/api/places/{place_id}")
    async def delete_place(
            place_id: str,
            _=Depends(require_scope("admin")),
    ):
        result = _mgr().delete(place_id)
        if not result.get("success"):
            raise HTTPException(404, result.get("error"))
        logger.info("[places] deleted %s", place_id)
        # Automations referencing this place id keep their condition but will
        # never match again. Deliberately not cascaded: silently editing
        # someone's rules is worse than leaving one that no longer fires.
        return result

    logger.info("Place routes registered")
