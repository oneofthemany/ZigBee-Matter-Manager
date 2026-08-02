"""
Journeys API — drive history and speed statistics.

Scope model mirrors presence: presence:read gives summaries, events and
aggregates with no coordinates, while raw track points and deletion are admin.
Events stay on the presence:read side deliberately — they carry time, kind and
magnitude but no position. See docs/journeys.md.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request

from modules.auth_middleware import Principal, require_scope

logger = logging.getLogger("modules.journey_routes")

_TRIP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
_DRIVER_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
#: Hex only. The colour is interpolated straight into the leaderboard's inline
#: styles, so anything else is a stylesheet the caller gets to write.
_COLOUR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def register_journey_routes(app: FastAPI, journey_manager_getter: Callable):

    def _mgr():
        mgr = journey_manager_getter()
        if not mgr:
            raise HTTPException(503, "Journey manager not initialised")
        return mgr

    def _is_admin(request: Request) -> bool:
        from modules.auth import scope_matches
        principal: Optional[Principal] = getattr(request.state, "principal", None)
        return bool(principal) and scope_matches("admin", principal.scopes)

    def _check_user_id(user_id: Optional[str]) -> Optional[str]:
        if user_id is not None and not _USER_ID_RE.match(user_id):
            raise HTTPException(400, "Bad user_id")
        return user_id

    def _check_driver_id(driver_id: Optional[str]) -> Optional[str]:
        if driver_id is not None and not _DRIVER_ID_RE.match(driver_id):
            raise HTTPException(400, "Bad driver_id")
        return driver_id

    @app.get("/api/journeys")
    async def list_journeys(
            user_id: Optional[str] = Query(None),
            driver_id: Optional[str] = Query(None),
            limit: int = Query(50, ge=1, le=500),
            _=Depends(require_scope("presence:read")),
    ):
        trips = await _mgr().list_trips(user_id=_check_user_id(user_id),
                                        driver_id=_check_driver_id(driver_id),
                                        limit=limit)
        return {"trips": trips}

    @app.get("/api/journeys/stats")
    async def journey_stats(
            user_id: Optional[str] = Query(None),
            driver_id: Optional[str] = Query(None),
            _=Depends(require_scope("presence:read")),
    ):
        stats = await _mgr().user_stats(_check_user_id(user_id),
                                        _check_driver_id(driver_id))
        return {"user_id": user_id, "driver_id": driver_id, **stats}

    # Registered before /api/journeys/{trip_id}: FastAPI matches routes in
    # registration order, and the parameterised route would otherwise swallow
    # "drivers" and "leaderboard" as trip ids.
    @app.get("/api/journeys/drivers")
    async def list_drivers(_=Depends(require_scope("presence:read"))):
        return {"drivers": await _mgr().list_drivers()}

    @app.get("/api/journeys/leaderboard")
    async def leaderboard(_=Depends(require_scope("presence:read"))):
        return await _mgr().leaderboard()

    @app.post("/api/journeys/drivers")
    async def save_driver(
            body: Dict[str, Any] = Body(...),
            _=Depends(require_scope("admin")),
    ):
        driver_id = _check_driver_id(str(body.get("driver_id") or "").strip() or None)
        if not driver_id:
            raise HTTPException(400, "driver_id required")
        name = str(body.get("name") or "").strip()
        if not name or len(name) > 64:
            raise HTTPException(400, "name required (1-64 chars)")
        colour = body.get("colour") or None
        if colour is not None and not _COLOUR_RE.match(str(colour)):
            raise HTTPException(400, "colour must be #rrggbb")
        result = await _mgr().save_driver(
            driver_id=driver_id,
            name=name,
            user_id=_check_user_id(body.get("user_id") or None),
            colour=colour,
            active=bool(body.get("active", True)),
        )
        return {"success": True, **result}

    @app.delete("/api/journeys/drivers/{driver_id}")
    async def delete_driver(
            driver_id: str,
            _=Depends(require_scope("admin")),
    ):
        _check_driver_id(driver_id)
        if not await _mgr().delete_driver(driver_id):
            raise HTTPException(404, "Driver not found")
        return {"success": True}

    @app.put("/api/journeys/{trip_id}/driver")
    async def set_trip_driver(
            trip_id: str,
            body: Dict[str, Any] = Body(...),
            _=Depends(require_scope("admin")),
    ):
        if not _TRIP_ID_RE.match(trip_id):
            raise HTTPException(400, "Bad trip_id")
        # An explicit null unattributes the trip, which is a different thing
        # from never having said: it clears a wrong guess without inventing a
        # replacement.
        driver_id = _check_driver_id(body.get("driver_id") or None)
        try:
            found = await _mgr().set_trip_driver(trip_id, driver_id)
        except KeyError:
            raise HTTPException(404, "Driver not found")
        if not found:
            raise HTTPException(404, "Trip not found")
        return {"success": True, "trip_id": trip_id, "driver_id": driver_id}

    @app.get("/api/journeys/{trip_id}")
    async def get_journey(
            trip_id: str,
            request: Request,
            _=Depends(require_scope("presence:read")),
    ):
        if not _TRIP_ID_RE.match(trip_id):
            raise HTTPException(400, "Bad trip_id")
        trip = await _mgr().get_trip(trip_id, include_track=_is_admin(request))
        if not trip:
            raise HTTPException(404, "Trip not found")
        return trip

    @app.delete("/api/journeys/{trip_id}")
    async def delete_journey(
            trip_id: str,
            _=Depends(require_scope("admin")),
    ):
        if not _TRIP_ID_RE.match(trip_id):
            raise HTTPException(400, "Bad trip_id")
        if not await _mgr().delete_trip(trip_id):
            raise HTTPException(404, "Trip not found")
        return {"success": True}

    logger.info("Journey routes registered (auth-protected)")
