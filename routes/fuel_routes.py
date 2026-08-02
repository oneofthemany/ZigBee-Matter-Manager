"""
Fuel price API — cheapest fuel near a location, for the Drive tab.

Centre resolves from an explicit postcode, then explicit coordinates, then the
household home. Prices are public open data but the centre is not, so nothing
about who asked is stored. See docs/journeys.md.
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

    # History — snapshots recorded at query time (modules/fuel_history.py)
    @app.get("/api/fuel/history")
    async def fuel_history(
            fuel: str = Query("E10"),
            days: int = Query(30, ge=1, le=365),
            site_id: Optional[str] = Query(None, max_length=64),
            _=Depends(require_authenticated),
    ):
        fuel = fuel.upper()
        if fuel not in FUEL_TYPES:
            raise HTTPException(400, f"Unknown fuel '{fuel}'")
        from modules.fuel_history import get_fuel_history
        return await get_fuel_history().daily_trend(fuel, days, site_id)

    @app.get("/api/fuel/history/station/{site_id}")
    async def fuel_station_history(
            site_id: str,
            days: int = Query(90, ge=1, le=365),
            _=Depends(require_authenticated),
    ):
        if not site_id or len(site_id) > 64:
            raise HTTPException(400, "Bad site_id")
        from modules.fuel_history import get_fuel_history
        return {"site_id": site_id,
                "history": await get_fuel_history().station_history(site_id, days)}

    @app.get("/api/fuel/history/status")
    async def fuel_history_status(_=Depends(require_authenticated)):
        from modules.fuel_history import get_fuel_history
        return await get_fuel_history().status()

    logger.info("Fuel price routes registered")
