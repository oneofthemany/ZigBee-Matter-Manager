"""
Map tile routes — a caching proxy in front of the upstream tile server.

Authenticated deliberately. An open tile proxy is something other people will
find and use, and the traffic would be attributed to this hub's address by the
upstream server — which is exactly how a self-hosted tool gets blocked.

Place search rides along here for the same reason and with the same care: it is
the other half of picking a point on a map, and an open geocoding proxy is the
same liability as an open tile proxy.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Response

from modules.auth_middleware import require_authenticated, require_scope
from modules.geocode import (COMMON_COUNTRIES, MAX_RESULTS, SOURCES,
                             default_source, get_geocoder)
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

    # ------------------------------------------------------------------
    # Place search
    # ------------------------------------------------------------------
    def _geo():
        g = get_geocoder()
        if not g:
            raise HTTPException(503, "Geocoder not initialised")
        return g

    @app.get("/api/geocode")
    async def geocode(
            q: str = Query(..., min_length=1, max_length=200),
            limit: int = Query(5, ge=1, le=MAX_RESULTS),
            country: Optional[str] = Query(None, min_length=2, max_length=2),
            _=Depends(require_authenticated),
    ):
        g = _geo()
        result = await g.search(q, limit=limit, country=country)
        # Told apart from "no such place" by the caller, which otherwise has no
        # way to suggest installing the data that would have answered.
        result["datasets_installed"] = len(await g.datasets())
        return result

    @app.get("/api/geocode/datasets")
    async def geocode_datasets(_=Depends(require_authenticated)):
        g = _geo()
        return {
            "installed": await g.datasets(),
            "available": [{"country": c, "name": n,
                           "default_source": default_source(c)}
                          for c, n in sorted(COMMON_COUNTRIES.items(),
                                             key=lambda kv: kv[1])],
            "sources": [{"id": k, "label": v["label"],
                         "countries": list(v["countries"]) if v["countries"] else None,
                         "precision": v["precision"],
                         "has_places": v["has_places"],
                         "note": v["note"],
                         "attribution": v["attribution"]}
                        for k, v in SOURCES.items()],
            "online_fallback": g.online_fallback,
        }

    @app.post("/api/geocode/datasets/{country}")
    async def geocode_install(
            country: str,
            source: Optional[str] = Query(None),
            _=Depends(require_scope("admin")),
    ):
        try:
            info = await _geo().install(country, source)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:                            # noqa: BLE001
            # The download is the failure worth reporting precisely: an
            # unreachable upstream and an unpublished country look identical
            # from the UI otherwise.
            logger.warning("[geocode] install of %s failed: %s", country, e)
            raise HTTPException(502, f"Could not fetch postal data: {e}")
        return {"success": True, **info}

    @app.delete("/api/geocode/datasets/{country}")
    async def geocode_remove(
            country: str,
            source: Optional[str] = Query(None),
            _=Depends(require_scope("admin")),
    ):
        if not await _geo().remove(country, source):
            raise HTTPException(404, "No data installed for that country")
        return {"success": True}

    @app.put("/api/geocode/settings")
    async def geocode_settings(
            body: Dict[str, Any] = Body(...),
            _=Depends(require_scope("admin")),
    ):
        g = _geo()
        persisted = True
        if "online_fallback" in body:
            persisted = g.set_online_fallback(bool(body["online_fallback"]))
        # `persisted` false means the setting is live but will not survive a
        # restart — worth telling the user rather than reporting plain success.
        return {"success": True, "online_fallback": g.online_fallback,
                "persisted": persisted}

    logger.info("Map tile and place-search routes registered")
