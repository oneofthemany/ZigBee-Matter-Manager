"""
Fuel price API — cheapest fuel near a location, for the Drive tab.

Centre resolves from an explicit place (a postcode or a place name), then
explicit coordinates, then the household home, then the hub's configured
location. Prices are public open data but the centre is not, so nothing about
who asked is stored.

Which country's feed answers is settled by `location:` in config.yaml — see
modules/fuel/registry.py and docs/plans/fuel_prices_region.md.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query

from modules.auth_middleware import require_authenticated, require_scope
from modules.fuel.service import get_fuel_service

logger = logging.getLogger("modules.fuel_routes")


def register_fuel_routes(app: FastAPI):

    def _home_fallback() -> Optional[dict]:
        """
        First configured home among presence users, else the hub's own location.

        Presence comes first because it is the more specific answer: it is where
        a person lives, whereas `location:` is where the hub is, and the two are
        the same house only most of the time.
        """
        try:
            from modules.presence_users import get_presence_manager
            pmgr = get_presence_manager()
            if pmgr:
                for dev in pmgr.devices.values():
                    cfg = dev.cfg
                    if cfg.enabled and cfg.home_lat is not None and cfg.home_lon is not None:
                        return {"lat": cfg.home_lat, "lon": cfg.home_lon}
        except Exception as e:                            # noqa: BLE001
            logger.debug(f"home fallback failed: {e}")

        try:
            from modules import location
            coords = location.home_coords(_config())
            if coords:
                return {"lat": coords[0], "lon": coords[1]}
        except Exception as e:                            # noqa: BLE001
            logger.debug(f"location fallback failed: {e}")
        return None

    @app.get("/api/fuel/types")
    async def fuel_types(_=Depends(require_authenticated)):
        svc = get_fuel_service()
        return {"fuel_types": svc.status()["fuel_types"], "region": svc.region}

    @app.get("/api/fuel/status")
    async def fuel_status(_=Depends(require_authenticated)):
        return get_fuel_service().status()

    @app.get("/api/fuel/nearby")
    async def fuel_nearby(
            fuel: str = Query("E10"),
            q: Optional[str] = Query(None, max_length=120),
            # The old name for `q`, kept because the Android client sends it.
            # Longer than the UK's 10 now: a French or Italian place name is a
            # legitimate query in a region with no postcode-shaped search.
            postcode: Optional[str] = Query(None, max_length=120),
            lat: Optional[float] = Query(None, ge=-90.0, le=90.0),
            lon: Optional[float] = Query(None, ge=-180.0, le=180.0),
            radius_km: float = Query(8.0, gt=0.0, le=40.0),
            limit: int = Query(10, ge=1, le=50),
            _=Depends(require_authenticated),
    ):
        svc = get_fuel_service()
        place = q or postcode

        centre = None
        centre_source = None
        if place:
            centre = await svc.resolve_place(place)
            if not centre:
                raise HTTPException(400, f"'{place}' did not resolve to a location")
            centre_source = "place"
        elif lat is not None and lon is not None:
            centre = {"lat": lat, "lon": lon}
            centre_source = "coords"
        else:
            centre = _home_fallback()
            centre_source = "home"
            if not centre:
                raise HTTPException(
                    400,
                    "No location: pass ?q= or ?lat=&lon=, set a home location "
                    "on a presence user, or fill in location: in config.yaml."
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

    # History — snapshots recorded at query time (modules/fuel/history.py)
    @app.get("/api/fuel/history")
    async def fuel_history(
            fuel: str = Query("E10"),
            days: int = Query(30, ge=1, le=365),
            site_id: Optional[str] = Query(None, max_length=64),
            _=Depends(require_authenticated),
    ):
        svc = get_fuel_service()
        fuel = fuel.upper()
        grades = svc.status()["fuel_types"]
        if fuel not in grades:
            raise HTTPException(400, f"Unknown fuel '{fuel}'. One of: {', '.join(grades)}")
        from modules.fuel.history import get_fuel_history
        trend = await get_fuel_history().daily_trend(
            fuel, days, site_id, region=svc.region)
        # The chart formats its own axis, so it needs the same units block the
        # live prices carry — the stored numbers are in the same major unit.
        trend["units"] = svc.status()["units"]
        return trend

    @app.get("/api/fuel/history/station/{site_id}")
    async def fuel_station_history(
            site_id: str,
            days: int = Query(90, ge=1, le=365),
            _=Depends(require_authenticated),
    ):
        if not site_id or len(site_id) > 64:
            raise HTTPException(400, "Bad site_id")
        from modules.fuel.history import get_fuel_history
        svc = get_fuel_service()
        return {"site_id": site_id,
                "region": svc.region,
                "units": svc.status()["units"],
                "history": await get_fuel_history().station_history(
                    site_id, days, region=svc.region)}

    @app.get("/api/fuel/history/status")
    async def fuel_history_status(_=Depends(require_authenticated)):
        from modules.fuel.history import get_fuel_history
        return await get_fuel_history().status(region=get_fuel_service().region)

    # ---- Fuel Finder credentials -------------------------------------------
    #
    # Admin only, and the secret is write-only: it is accepted here and never
    # returned. Each operator supplies their own credentials from the GOV.UK
    # developer portal — nothing ships with this project, and nothing about
    # them reaches the phone, which only ever talks to its own hub.

    def _config() -> dict:
        from main import CONFIG
        return CONFIG or {}

    @app.get("/api/fuel/finder/config")
    async def fuel_finder_config(_=Depends(require_scope("admin"))):
        from modules.fuel.providers.uk_fuel_finder import credentials_status
        cfg = _config()
        finder = (cfg.get("fuel") or {}).get("finder") or {}
        return {
            **credentials_status(finder),
            "enabled": bool(finder.get("enabled", False)),
            "base_url": finder.get("base_url") or "",
            "token_url": finder.get("token_url") or "",
        }

    @app.post("/api/fuel/finder/config")
    async def save_fuel_finder_config(
            body: dict = Body(...),
            _=Depends(require_scope("admin")),
    ):
        from modules.fuel.providers.uk_fuel_finder import credentials_status, save_credentials
        from modules.fuel.service import reset_fuel_service

        cfg = _config()
        finder = (cfg.get("fuel") or {}).get("finder") or {}

        client_id = str(body.get("client_id") or "").strip()
        client_secret = str(body.get("client_secret") or "").strip()
        if client_id or client_secret:
            if credentials_status(finder).get("source") == "environment":
                raise HTTPException(
                    409,
                    "Credentials come from the environment "
                    "(ZMM_FUEL_FINDER_CLIENT_ID/SECRET); a saved file would be "
                    "ignored. Unset those to edit them here.",
                )
            try:
                save_credentials(client_id, client_secret)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            except OSError as e:
                raise HTTPException(500, f"Could not write the secrets file: {e}") from e

        # The endpoints are not secret, so they stay in config.yaml where they
        # are visible and reviewable alongside the rest of the deployment.
        urls = {k: str(body.get(k) or "").strip()
                for k in ("base_url", "token_url") if body.get(k) is not None}
        if urls or "enabled" in body:
            import yaml
            path = "./config/config.yaml"
            with open(path, "r") as fh:
                disk = yaml.safe_load(fh) or {}
            section = disk.setdefault("fuel", {}).setdefault("finder", {})
            section.update(urls)
            if "enabled" in body:
                section["enabled"] = bool(body["enabled"])
            with open(path, "w") as fh:
                yaml.dump(disk, fh, default_flow_style=False, sort_keys=False)
            cfg.setdefault("fuel", {}).setdefault("finder", {}).update(section)

        reset_fuel_service()
        finder = (cfg.get("fuel") or {}).get("finder") or {}
        return {"success": True, **credentials_status(finder)}

    @app.get("/api/fuel/regions")
    async def fuel_regions(_=Depends(require_authenticated)):
        """
        Every region the hub can serve, plus the one it is set to.

        `detected` is a suggestion reverse-geocoded from the hub's coordinates,
        offered so the Settings page can pre-select something sensible. It is
        never applied here — picking a country changes which currency prices
        are quoted in, so it takes a deliberate save.
        """
        from modules import location
        from modules.fuel.registry import known_regions

        cfg = _config()
        svc = get_fuel_service()
        return {
            "regions": known_regions(),
            "active": svc.region,
            "configured": {"country": location.country(cfg),
                           "subdivision": location.subdivision(cfg)},
            "detected": await location.detect_country(cfg),
        }

    @app.post("/api/fuel/region")
    async def save_fuel_region(
            body: dict = Body(...),
            _=Depends(require_scope("admin")),
    ):
        """
        Set the hub's country (and state, where that matters).

        Written with the comment-preserving writer: config.yaml is roughly a
        third comments, and a settings save that silently deleted them would be
        noticed only long afterwards, by someone reading the file.
        """
        from modules import location
        from modules.fuel.service import reset_fuel_service

        values = {k: body[k] for k in ("country", "subdivision") if k in body}
        if not values:
            raise HTTPException(400, "Nothing to set: pass country and/or subdivision")
        try:
            written = location.persist(values)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except OSError as e:
            raise HTTPException(500, f"Could not write config.yaml: {e}") from e

        # Patch the live config too: the file is only re-read on boot, and a
        # save that needs a restart to take effect reads as a save that failed.
        _config().setdefault("location", {}).update(written)
        reset_fuel_service()

        svc = get_fuel_service()
        return {"success": True, "active": svc.region, **svc.status()}

    @app.post("/api/fuel/finder/test")
    async def test_fuel_finder(_=Depends(require_scope("admin"))):
        """Prove the credentials by exchanging them for a token."""
        from modules.fuel.providers.uk_fuel_finder import FuelFinderClient
        finder = (_config().get("fuel") or {}).get("finder") or {}
        return await FuelFinderClient(finder).verify()

    logger.info("Fuel price routes registered")
