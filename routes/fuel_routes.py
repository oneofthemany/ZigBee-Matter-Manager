"""
Fuel price API routes — cheapest fuel near a location, for the Drive tab.

Centre resolution, in order of preference:
    1. explicit ?postcode=      (resolved via postcodes.io)
    2. explicit ?lat= & ?lon=
    3. the requesting household's home (first presence user with one set)

Prices themselves are public open data; the location the query centres on is
not, which is why the fallback is the home location every household member
already knows, and why any authenticated user may query but nothing about
who asked is stored.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query

from modules.auth_middleware import require_authenticated, require_scope
from modules.fuel_prices import FUEL_TYPES, get_fuel_service

logger = logging.getLogger("modules.fuel_routes")


def register_fuel_routes(app: FastAPI):

    def _home_fallback() -> Optional[dict]:
        """First configured home among presence users, if any."""
        try:
            from modules.presence_users import get_presence_manager
            pmgr = get_presence_manager()
            if not pmgr:
                return None
            for dev in pmgr.devices.values():
                cfg = dev.cfg
                if cfg.enabled and cfg.home_lat is not None and cfg.home_lon is not None:
                    return {"lat": cfg.home_lat, "lon": cfg.home_lon}
        except Exception as e:                            # noqa: BLE001
            logger.debug(f"home fallback failed: {e}")
        return None

    @app.get("/api/fuel/types")
    async def fuel_types(_=Depends(require_authenticated)):
        return {"fuel_types": FUEL_TYPES}

    @app.get("/api/fuel/status")
    async def fuel_status(_=Depends(require_authenticated)):
        return get_fuel_service().status()

    @app.get("/api/fuel/nearby")
    async def fuel_nearby(
            fuel: str = Query("E10"),
            postcode: Optional[str] = Query(None, max_length=10),
            lat: Optional[float] = Query(None, ge=-90.0, le=90.0),
            lon: Optional[float] = Query(None, ge=-180.0, le=180.0),
            radius_km: float = Query(8.0, gt=0.0, le=40.0),
            limit: int = Query(10, ge=1, le=50),
            _=Depends(require_authenticated),
    ):
        svc = get_fuel_service()

        centre = None
        centre_source = None
        if postcode:
            centre = await svc.resolve_postcode(postcode)
            if not centre:
                raise HTTPException(400, f"Postcode '{postcode}' did not resolve")
            centre_source = "postcode"
        elif lat is not None and lon is not None:
            centre = {"lat": lat, "lon": lon}
            centre_source = "coords"
        else:
            centre = _home_fallback()
            centre_source = "home"
            if not centre:
                raise HTTPException(
                    400,
                    "No location: pass ?postcode= or ?lat=&lon=, or set a home "
                    "location on a presence user."
                )

        result = await svc.best_nearby(
            lat=centre["lat"], lon=centre["lon"],
            fuel=fuel, radius_km=radius_km, limit=limit,
        )
        if not result.get("success"):
            raise HTTPException(503, result.get("error", "Fuel data unavailable"))
        # The centre goes back rounded: enough for the UI to say "around
        # NW1" and draw distances, without echoing a precise home fix.
        result["centre"] = {"lat": round(centre["lat"], 3),
                            "lon": round(centre["lon"], 3),
                            "source": centre_source}
        return result

    @app.post("/api/fuel/refresh")
    async def fuel_refresh(_=Depends(require_scope("admin"))):
        return await get_fuel_service().refresh()

    logger.info("Fuel price routes registered")
