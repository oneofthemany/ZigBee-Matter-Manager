"""
Fuel Prices — cheapest fuel near a location, from the UK retailer open-data
feeds (https://www.gov.uk/guidance/access-fuel-price-data) via the
uk-fuel-prices-api package.

The package fetches ~15 retailer JSON feeds (Asda, Tesco, BP, Shell, …) and
holds them in memory with an hour's cache; most retailers only refresh daily,
so that cadence loses nothing. This module wraps it with:

    - a refresh guard (one refresh at a time; callers share the result)
    - postcode → coordinates via postcodes.io (free, no key, no logging of
      who asked)
    - "best nearby" queries: stations within a radius that sell the wanted
      fuel, sorted cheapest-first, each with a Google Maps link built from
      its postcode so a phone can navigate to the winner in one tap.

Prices are re-fetched on demand, but each query's results are also
snapshotted into fuel price history (modules/fuel_history.py, its own
DuckDB) — the retailer feeds publish only "today's number" with no archive,
so anything not recorded at query time is gone by tomorrow.
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
    """Owns the uk-fuel-prices-api client and answers nearby-price queries."""

    def __init__(self) -> None:
        self._api = None                       # lazy: import failure is survivable
        self._refresh_lock = asyncio.Lock()
        self._last_refresh: float = 0.0
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------
    async def _ensure_fresh(self, force: bool = False) -> bool:
        """Fetch/refresh price data. Returns True if data is available."""
        async with self._refresh_lock:
            if self._api is None:
                try:
                    from uk_fuel_prices_api import UKFuelPricesApi
                    self._api = UKFuelPricesApi()
                except ImportError as e:
                    self._last_error = f"uk-fuel-prices-api not installed: {e}"
                    logger.error(self._last_error)
                    return False
            try:
                ok = await self._api.get_prices(force_refresh=force)
                if ok:
                    self._last_refresh = time.time()
                    self._last_error = None
                else:
                    self._last_error = "No fuel data returned from any retailer feed"
                return ok
            except Exception as e:                        # noqa: BLE001
                # A retailer feed being down must degrade to "stale data" or
                # "unavailable", never to a 500 that breaks the Drive tab.
                self._last_error = f"Fuel price refresh failed: {e}"
                logger.warning(self._last_error)
                return not self._stations_empty()

    def _stations_empty(self) -> bool:
        try:
            return self._api is None or len(self._api.stations) == 0
        except Exception:                                 # noqa: BLE001
            return True

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
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
            raw = self._api.stationsWithinRadius(lat, lon, radius_km)
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
            if self._api is not None:
                count = int(len(self._api.stations))
        except Exception:                                 # noqa: BLE001
            pass
        return {
            "stations_loaded": count,
            "last_refresh": self._last_refresh or None,
            "last_error": self._last_error,
            "fuel_types": FUEL_TYPES,
        }

    async def refresh(self) -> Dict[str, Any]:
        ok = await self._ensure_fresh(force=True)
        return {"success": ok, **self.status()}


# ---------------------------------------------------------------------------
# Singleton helper
# ---------------------------------------------------------------------------

_service: Optional[FuelPriceService] = None


def get_fuel_service() -> FuelPriceService:
    """Created on first use — no startup cost, no startup wiring to forget."""
    global _service
    if _service is None:
        _service = FuelPriceService()
    return _service
