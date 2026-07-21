"""
Journeys API routes — drive history and speed statistics.

Scope model mirrors presence:
- `presence:read`   trip summaries and aggregate stats (no coordinates)
- `admin`           additionally the raw track points, and deletion

Track coordinates cross the same privacy boundary as the live presence map
(see _attach_position in presence_routes.py), so they are gated the same
way: presence:read tells you someone drove 12 miles at an average of
31 mph; pinning the route to streets is an administrator's capability.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request

from modules.auth_middleware import Principal, require_scope

logger = logging.getLogger("modules.journey_routes")

_TRIP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")


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

    @app.get("/api/journeys")
    async def list_journeys(
            user_id: Optional[str] = Query(None),
            limit: int = Query(50, ge=1, le=500),
            _=Depends(require_scope("presence:read")),
    ):
        trips = await _mgr().list_trips(user_id=_check_user_id(user_id),
                                        limit=limit)
        return {"trips": trips}

    @app.get("/api/journeys/stats")
    async def journey_stats(
            user_id: Optional[str] = Query(None),
            _=Depends(require_scope("presence:read")),
    ):
        stats = await _mgr().user_stats(_check_user_id(user_id))
        return {"user_id": user_id, **stats}

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
