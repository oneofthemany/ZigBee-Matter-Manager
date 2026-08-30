"""
Cheapest fuel near a location.

Holds the provider for the configured region and answers cheapest-first "best
nearby" queries against it. Which region that is, and whether its source is a
national download or a per-location query, is settled in modules/fuel/registry.py
and modules/fuel/base.py — everything below this docstring is region agnostic.

Adds postcode lookup, a Maps link per station, and a history snapshot of every
query, because no source publishes an archive: query time is the only chance to
record today's number. See docs/journeys.md and docs/plans/fuel_prices_region.md.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from modules import location
from modules.fuel.base import FuelProvider
from modules.fuel.registry import build_provider, resolve_region
# Re-exported: routes and the car screen have imported this name from here
# since before regions existed.
from modules.fuel.providers.uk_fuel_finder import FUEL_TYPES

logger = logging.getLogger("modules.fuel.service")

DEFAULT_RADIUS_KM = 8.0
MAX_RADIUS_KM = 40.0

__all__ = [
    "FUEL_TYPES", "DEFAULT_RADIUS_KM", "MAX_RADIUS_KM",
    "FuelPriceService", "get_fuel_service", "reset_fuel_service", "maps_url",
]


def maps_url(station: Dict[str, Any]) -> str:
    """
    Google Maps search link for a station.

    Query by name and place rather than raw coordinates: Maps then shows the
    actual station listing (opening hours, reviews, "navigate") instead of a
    bare pin, and a postcode is what the user asked to see anyway.

    Not every feed has a postcode — Italy's register carries none at all — so
    the address and town stand in when it is missing. Coordinates are the last
    resort rather than the alternative, because a pin drops the listing.
    """
    from urllib.parse import quote_plus
    brand = str(station.get("brand") or "").strip()
    postcode = str(station.get("postcode") or "").strip()
    if postcode:
        q = f"{brand} {postcode}".strip()
    else:
        parts = [brand,
                 str(station.get("address") or "").strip(),
                 str(station.get("town") or "").strip()]
        q = " ".join(p for p in parts if p)
    if not q:
        q = f"{station.get('latitude')},{station.get('longitude')}"
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(q)}"


class FuelPriceService:
    """Holds the active region's provider and answers nearby-price queries."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._config = config or {}
        self._provider: Optional[FuelProvider] = None
        self._region: str = ""
        self._build_error: Optional[str] = None

    # Provider

    @property
    def provider(self) -> Optional[FuelProvider]:
        """
        The configured region's provider, built on first use.

        Built lazily rather than at startup for the same reason the service
        itself is: constructing it reads credentials, and a hub with none should
        still boot.
        """
        if self._provider is None:
            self._region = resolve_region(
                location.country(self._config), location.subdivision(self._config)
            )
            try:
                self._provider = build_provider(self._region, self._config)
                self._build_error = None
            except Exception as e:                        # noqa: BLE001
                self._build_error = f"Could not build fuel provider for {self._region}: {e}"
                logger.error(self._build_error)
        return self._provider

    @property
    def region(self) -> str:
        if not self._region:
            _ = self.provider
        return self._region

    def _grades(self) -> Dict[str, str]:
        p = self.provider
        return dict(p.grades) if p and p.grades else dict(FUEL_TYPES)

    @property
    def last_error(self) -> Optional[str]:
        if self._build_error:
            return self._build_error
        p = self.provider
        return p.last_error if p else None

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
        grades = self._grades()
        if fuel not in grades:
            return {"success": False,
                    "error": f"Unknown fuel '{fuel}'. One of: {', '.join(grades)}"}

        provider = self.provider
        if provider is None:
            return {"success": False,
                    "error": self._build_error or "Fuel data unavailable"}
        radius_km = provider.clamp_radius(radius_km)

        try:
            raw = await provider.nearby(lat, lon, radius_km)
        except Exception as e:                            # noqa: BLE001
            # A source being down must degrade to "unavailable", never to a 500
            # that breaks the Drive tab.
            return {"success": False, "error": f"Station lookup failed: {e}"}

        if not raw and provider.last_error:
            return {"success": False, "error": provider.last_error}

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
                # All grades so the UI can show alternatives without re-querying.
                "prices": {ft: s.get(ft) for ft in grades if s.get(ft) is not None},
                "last_updated": s.get("last_updated"),
                # An area average has nowhere to navigate to, so it gets no
                # link rather than one that searches for the name of a state.
                "maps_url": maps_url(s) if provider.station_level else None,
            })

        # Cheapest first; distance breaks ties so two same-price stations
        # rank nearest-first.
        stations.sort(key=lambda x: (x["price"], x["distance_km"] or 0))

        # Snapshot into history BEFORE the top-N slice: every station in the
        # radius contributes to trends, not just the ten displayed. The feeds
        # keep no archive, so query time is the only chance to record today's
        # number. Best-effort — history must never break the live answer.
        try:
            from modules.fuel.history import get_fuel_history
            await get_fuel_history().record_stations(
                stations, region=self.region, currency=provider.currency)
        except Exception as e:                            # noqa: BLE001
            logger.warning(f"fuel history snapshot failed: {e}")

        last_refresh = provider.status().get("last_refresh")
        return {
            "success": True,
            "fuel": fuel,
            "fuel_label": grades[fuel],
            "radius_km": radius_km,
            "count": len(stations),
            "stations": stations[:limit],
            "data_age_s": (time.time() - last_refresh) if last_refresh else None,
            # Everything the UI needs to render a price without knowing which
            # country it came from. Prices above are always in the major
            # currency unit; `display_scale` says whether to show them that way
            # or as the region's minor unit, the way the UK quotes pence.
            "region": self.region,
            "units": provider.units(),
            "station_level": provider.station_level,
            "attribution": provider.attribution,
        }

    async def resolve_place(self, query: str) -> Optional[Dict[str, float]]:
        """
        A typed postcode or place name → lat/lon. None if it doesn't resolve.

        Uses the shared geocoder rather than a postcode service, because the old
        one was postcodes.io: UK only, and gated behind a "2 to 8 alphanumerics"
        check that rejects perfectly valid French and German postcodes. The
        geocoder searches installed postal data first and falls back to
        OpenStreetMap, and is told the active region's country so that "M1"
        resolves in the right one.

        `allow_online` is forced on: this lookup has always left the hub — it
        went to postcodes.io before — so honouring an opt-in written for the map
        picker would withhold a lookup the user is explicitly asking for.
        """
        q = " ".join(str(query or "").split())
        if not q or len(q) > 200:
            return None
        try:
            from modules.geocode import get_geocoder
            geocoder = get_geocoder()
            if geocoder is None:
                logger.warning("place lookup unavailable: no geocoder")
                return None
            cc = (self.region or "").split("-")[0]
            found = await geocoder.search(q, limit=1, country=cc, allow_online=True)
        except Exception as e:                            # noqa: BLE001
            logger.warning(f"place lookup failed: {e}")
            return None

        results = (found or {}).get("results") or []
        if not results:
            return None
        try:
            return {"lat": float(results[0]["lat"]), "lon": float(results[0]["lon"])}
        except (KeyError, TypeError, ValueError):
            return None

    def status(self) -> Dict[str, Any]:
        provider = self.provider
        if provider is None:
            return {
                "stations_loaded": 0,
                "source": "none",
                "last_refresh": None,
                "last_error": self._build_error,
                "fuel_types": dict(FUEL_TYPES),
                "region": self._region or "",
                "region_label": "",
                "units": {"currency": "GBP", "symbol": "£", "volume": "L",
                          "distance": "km", "display_scale": "minor",
                          "decimals": 3},
                "station_level": True,
                "attribution": "",
                "default_grade": "E10",
            }
        st = provider.status()
        return {
            "stations_loaded": st["stations_loaded"],
            # Which feed actually answered. Worth surfacing: sources can
            # disagree, and "why is this price different from the app" starts
            # with knowing which one produced it.
            "source": st["source"],
            "last_refresh": st["last_refresh"],
            "last_error": st["last_error"],
            "fuel_types": st["fuel_types"],
            "region": st["region"],
            "region_label": st["label"],
            "units": st["units"],
            "station_level": st["station_level"],
            "attribution": st["attribution"],
            "default_grade": provider.default_grade,
        }

    async def refresh(self) -> Dict[str, Any]:
        provider = self.provider
        ok = bool(provider) and await provider.refresh()
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

    Credentials and the region are read when the provider is constructed, so
    without this a save from the settings page appears to do nothing until the
    hub is restarted — and the operator's reasonable conclusion is that the save
    failed.
    """
    global _service
    _service = None
