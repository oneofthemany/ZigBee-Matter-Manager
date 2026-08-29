"""
Cheapest fuel near a location.

Two sources, tried in order: the government Fuel Finder API (statutory, prices
published within 30 minutes of a change by law), falling back to the retailer
open-data feeds via uk-fuel-prices-api. Both present the same station shape —
see modules/fuel_finder.py — so everything below this docstring is source
agnostic and the fallback costs one branch rather than a second implementation.

Adds a refresh guard, postcode lookup via postcodes.io, and cheapest-first
"best nearby" queries with a Maps link per station. Every query is snapshotted
into fuel history, because neither source publishes an archive.
See docs/journeys.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("modules.fuel_prices")

#: Fuel codes used across the retailer feeds.
FUEL_TYPES = {
    "E10": "Petrol (E10)",
    "E5": "Premium petrol (E5)",
    "B7": "Diesel (B7)",
    "SDV": "Super diesel (SDV)",
}

DEFAULT_RADIUS_KM = 8.0
MAX_RADIUS_KM = 40.0
POSTCODES_IO = "https://api.postcodes.io/postcodes/"


def maps_url(station: Dict[str, Any]) -> str:
    """
    Google Maps search link for a station.

    Query by name + postcode rather than raw coordinates: Maps then shows
    the actual station listing (opening hours, reviews, "navigate") instead
    of a bare pin, and a postcode is what the user asked to see anyway.
    """
    from urllib.parse import quote_plus
    brand = str(station.get("brand") or "").strip()
    postcode = str(station.get("postcode") or "").strip()
    if postcode:
        q = f"{brand} {postcode}".strip()
    else:
        q = f"{station.get('latitude')},{station.get('longitude')}"
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(q)}"


class FuelPriceService:
    """Holds whichever price source is live and answers nearby-price queries."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._api = None                       # lazy: import failure is survivable
        self._finder = None                    # Fuel Finder, when configured
        # Whichever source last answered. Every query reads through this rather
        # than naming a source, which is what makes the fallback invisible to
        # best_nearby.
        self._active = None
        self._config = config or {}
        self._refresh_lock = asyncio.Lock()
        self._last_refresh: float = 0.0
        self._last_error: Optional[str] = None
        self._source: str = "none"

    # Data refresh
    def _finder_client(self):
        """The Fuel Finder client, or None when it isn't configured."""
        if self._finder is None:
            try:
                from modules.fuel_finder import FuelFinderClient
                self._finder = FuelFinderClient(
                    (self._config.get("fuel") or {}).get("finder") or {}
                )
            except Exception as e:                        # noqa: BLE001
                logger.warning(f"Fuel Finder unavailable: {e}")
                return None
        return self._finder if self._finder.configured else None

    async def _ensure_fresh(self, force: bool = False) -> bool:
        """
        Fetch/refresh price data. Returns True if data is available.

        Fuel Finder is tried first and the retailer feeds are the fallback, so a
        misconfigured or unreachable government API degrades to the source this
        project used before it rather than to an empty Drive tab. Whichever
        answers becomes [_active] until the next refresh.
        """
        async with self._refresh_lock:
            finder = self._finder_client()
            if finder is not None:
                try:
                    if await finder.get_prices(force_refresh=force):
                        self._active = finder
                        self._source = "fuel_finder"
                        self._last_refresh = time.time()
                        self._last_error = None
                        return True
                    logger.warning(
                        "Fuel Finder returned no data (%s) — falling back to "
                        "the retailer feeds", finder.last_error)
                except Exception as e:                    # noqa: BLE001
                    logger.warning(f"Fuel Finder failed, falling back: {e}")

            if self._api is None:
                try:
                    from uk_fuel_prices_api import UKFuelPricesApi
                    self._api = UKFuelPricesApi()
                except ImportError as e:
                    self._last_error = (
                        f"Fuel Finder unavailable and uk-fuel-prices-api not "
                        f"installed: {e}"
                    )
                    logger.error(self._last_error)
                    return False
            try:
                ok = await self._api.get_prices(force_refresh=force)
                if ok:
                    self._active = self._api
                    self._source = "retailer_feeds"
                    self._last_refresh = time.time()
                    self._last_error = None
                else:
                    self._last_error = "No fuel data returned from any retailer feed"
                return ok
            except Exception as e:                        # noqa: BLE001
                # A source being down must degrade to "stale data" or
                # "unavailable", never to a 500 that breaks the Drive tab.
                self._last_error = f"Fuel price refresh failed: {e}"
                logger.warning(self._last_error)
                return not self._stations_empty()

    def _stations_empty(self) -> bool:
        try:
            return self._active is None or len(self._active.stations) == 0
        except Exception:                                 # noqa: BLE001
            return True

    # Queries
    async def best_nearby(
            self,
            lat: float,
            lon: float,
            fuel: str = "E10",
            radius_km: float = DEFAULT_RADIUS_KM,
            limit: int = 10,
    ) -> Dict[str, Any]:
        """Stations selling `fuel` within radius, cheapest first."""
        fuel = fuel.upper()
        if fuel not in FUEL_TYPES:
            return {"success": False,
                    "error": f"Unknown fuel '{fuel}'. One of: {', '.join(FUEL_TYPES)}"}
        radius_km = min(max(radius_km, 0.5), MAX_RADIUS_KM)

        if not await self._ensure_fresh():
            return {"success": False,
                    "error": self._last_error or "Fuel data unavailable"}

        try:
            raw = self._active.stationsWithinRadius(lat, lon, radius_km)
        except Exception as e:                            # noqa: BLE001
            return {"success": False, "error": f"Station lookup failed: {e}"}

        stations: List[Dict[str, Any]] = []
        for s in raw:
            price = s.get(fuel)
            if price is None or not isinstance(price, (int, float)) or price <= 0:
                continue
            stations.append({
                "site_id": s.get("site_id"),
                "brand": s.get("brand"),
                "address": s.get("address"),
                "postcode": s.get("postcode"),
                "distance_km": s.get("dist"),
                "latitude": s.get("latitude"),
                "longitude": s.get("longitude"),
                "price": round(float(price), 3),
                # All four so the UI can show alternatives without re-querying.
                "prices": {ft: s.get(ft) for ft in FUEL_TYPES if s.get(ft) is not None},
                "last_updated": s.get("last_updated"),
                "maps_url": maps_url(s),
            })

        # Cheapest first; distance breaks ties so two same-price stations
        # rank nearest-first.
        stations.sort(key=lambda x: (x["price"], x["distance_km"] or 0))

        # Snapshot into history BEFORE the top-N slice: every station in the
        # radius contributes to trends, not just the ten displayed. The feeds
        # keep no archive, so query time is the only chance to record today's
        # number. Best-effort — history must never break the live answer.
        try:
            from modules.fuel_history import get_fuel_history
            await get_fuel_history().record_stations(stations)
        except Exception as e:                            # noqa: BLE001
            logger.warning(f"fuel history snapshot failed: {e}")

        return {
            "success": True,
            "fuel": fuel,
            "fuel_label": FUEL_TYPES[fuel],
            "radius_km": radius_km,
            "count": len(stations),
            "stations": stations[:limit],
            "data_age_s": (time.time() - self._last_refresh) if self._last_refresh else None,
        }

    async def resolve_postcode(self, postcode: str) -> Optional[Dict[str, float]]:
        """Postcode → lat/lon via postcodes.io. None if it doesn't resolve."""
        pc = postcode.strip().replace(" ", "").upper()
        if not (2 <= len(pc) <= 8) or not pc.isalnum():
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(POSTCODES_IO + pc) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            r = (data or {}).get("result") or {}
            lat, lon = r.get("latitude"), r.get("longitude")
            if lat is None or lon is None:
                return None
            return {"lat": float(lat), "lon": float(lon)}
        except Exception as e:                            # noqa: BLE001
            logger.warning(f"postcode lookup failed: {e}")
            return None

    def status(self) -> Dict[str, Any]:
        count = 0
        try:
            if self._active is not None:
                count = int(len(self._active.stations))
        except Exception:                                 # noqa: BLE001
            pass
        return {
            "stations_loaded": count,
            # Which feed actually answered. Worth surfacing: the two can
            # disagree, and "why is this price different from the app" starts
            # with knowing which source produced it.
            "source": self._source,
            "last_refresh": self._last_refresh or None,
            "last_error": self._last_error,
            "fuel_types": FUEL_TYPES,
        }

    async def refresh(self) -> Dict[str, Any]:
        ok = await self._ensure_fresh(force=True)
        return {"success": ok, **self.status()}


_service: Optional[FuelPriceService] = None


def get_fuel_service(config: Optional[Dict[str, Any]] = None) -> FuelPriceService:
    """Created on first use — no startup cost, no startup wiring to forget."""
    global _service
    if _service is None:
        if config is None:
            # Imported here rather than at module scope: main imports this
            # module, so a top-level import of main would be circular.
            try:
                from main import CONFIG
                config = CONFIG
            except Exception:                             # noqa: BLE001
                config = {}
        _service = FuelPriceService(config)
    return _service


def reset_fuel_service() -> None:
    """
    Drop the cached service so the next query rebuilds it.

    Credentials are read when the client is constructed, so without this a save
    from the settings page appears to do nothing until the hub is restarted —
    and the operator's reasonable conclusion is that the save failed.
    """
    global _service
    _service = None
