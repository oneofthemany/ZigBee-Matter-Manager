"""
Map tile routes — a caching proxy in front of the upstream tile server.

Authenticated deliberately. An open tile proxy is something other people will
find and use, and the traffic would be attributed to this hub's address by the
upstream server — which is exactly how a self-hosted tool gets blocked.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException, Response

from modules.auth_middleware import require_authenticated, require_scope
from modules.map_tiles import MAX_ZOOM, get_tile_cache

logger = logging.getLogger("routes.map")


def register_map_routes(app: FastAPI) -> None:

    @app.get("/api/map/tiles/{z}/{x}/{y}.png")
    async def tile(z: int, x: int, y: int, _=Depends(require_authenticated)):
        # Typed as int by FastAPI, so a non-numeric segment 422s before
        # reaching us — no string ever reaches the filesystem path or the
        # upstream URL.
        cache = get_tile_cache()
        if not cache.valid(z, x, y):
            raise HTTPException(400, f"Tile out of range (zoom 0-{MAX_ZOOM})")

        data = await cache.get(z, x, y)
        if data is None:
            # 404 rather than 502: Leaflet renders a blank square and carries
            # on, where a 5xx makes it retry and amplify a failing upstream.
            raise HTTPException(404, "Tile unavailable")

        return Response(
            content=data,
            media_type="image/png",
            headers={
                # The browser cache is the first line; the disk cache behind it
                # only sees what the browser misses.
                "Cache-Control": "public, max-age=604800",
            },
        )

    @app.get("/api/map/cache")
    async def cache_stats(_=Depends(require_scope("admin"))):
        return get_tile_cache().stats()

    @app.delete("/api/map/cache")
    async def cache_clear(_=Depends(require_scope("admin"))):
        removed = get_tile_cache().clear()
        logger.info("[tiles] cache cleared, %d tiles removed", removed)
        return {"success": True, "removed": removed}

    logger.info("Map tile routes registered (caching proxy)")
