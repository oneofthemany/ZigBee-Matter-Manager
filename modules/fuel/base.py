"""
What every fuel price source has to provide, and the two shapes they come in.

`FuelPriceService` never named a source — it held whichever client answered and
called `get_prices` / `stations` / `stationsWithinRadius` on it. That worked
while both sources were UK feeds that publish the whole country at once. It
stops working the moment a region's API is a *query* rather than a download:
Tankerkoenig caps a search at 25 km and one request per minute, and there is no
national snapshot to hold.

So the abstract method here is `nearby()`, not `get_prices()`. "Fetch the whole
country and filter locally" is one implementation of it — [BulkSnapshotProvider],
which is the old behaviour lifted out of the Fuel Finder client unchanged — and
"ask the API per location" is the other, [RadiusQueryProvider]. A region adapter
subclasses whichever fits and implements one method.

Prices leave a provider in the region's *major* currency unit per litre (or per
US gallon), because that is what the whole app downstream already assumes about
pounds. How they are displayed — pence, euro, dollars — is `display_scale`, and
is the UI's problem, not the fetcher's. See docs/plans/fuel_prices_region.md.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("modules.fuel.base")

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Same maths uk-fuel-prices-api did server-side."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(a))


class FuelProvider(abc.ABC):
    """
    One region's fuel price source.

    The class attributes are the region's dialect — its grade codes, its money,
    its volume unit. They are declared rather than detected because detection is
    what made the UK code UK-only: a median-based "is this pence?" guess reads
    Australian cents per litre (~180) as pence and US dollars per gallon (~3.40)
    as already-converted. A provider knows what its own API returns.
    """

    #: Registry key. "GB", "DE", or "AU-NSW" where a country reports per state.
    region: str = ""
    label: str = ""

    #: The region's own grade codes -> display labels. The keys are what appear
    #: in a station dict and in the `fuel` query parameter.
    grades: Dict[str, str] = {}
    #: Which grade the UI selects when the user has expressed no preference.
    default_grade: str = ""

    currency: str = "GBP"
    currency_symbol: str = "£"
    #: "L", or "gal_us" where a country prices by the gallon.
    volume_unit: str = "L"
    #: How distances are shown. Always kilometres on the wire — the radius
    #: parameter and every station's `dist` are km regardless — so this is a
    #: display choice, and it belongs with the region rather than the UI.
    distance_unit: str = "km"
    #: "major" renders 1.719 as €1.719; "minor" renders 1.399 as 139.9p. Only
    #: presentation — the number itself is always major units.
    display_scale: str = "major"
    display_decimals: int = 3

    #: False when the source publishes area averages rather than individual
    #: forecourts (the US EIA feed). The UI shows an average instead of a table;
    #: it must not present an average as though it were a station.
    station_level: bool = True

    needs_credentials: bool = False
    #: A licence condition for several sources, so it is carried, not optional.
    attribution: str = ""

    default_radius_km: float = 8.0
    max_radius_km: float = 40.0

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._config = config or {}
        self._last_error: Optional[str] = None
        self._last_refresh: float = 0.0

    # Identity

    @property
    def configured(self) -> bool:
        """Whether this provider can be used. Sources needing no key are ready."""
        return True

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def source(self) -> str:
        """
        Which concrete feed answered. Worth surfacing: sources can disagree, and
        "why is this price different from the app" starts with knowing which one
        produced it.
        """
        return type(self).__name__

    @property
    def stations(self) -> List[Dict[str, Any]]:
        """Whatever snapshot is cached. Empty for providers that hold none."""
        return []

    def clamp_radius(self, radius_km: float) -> float:
        return min(max(float(radius_km), 0.5), self.max_radius_km)

    # Queries

    @abc.abstractmethod
    async def nearby(
        self, lat: float, lon: float, radius_km: float
    ) -> List[Dict[str, Any]]:
        """
        Stations within the radius, each a dict of site_id / brand / address /
        postcode / latitude / longitude / last_updated / dist plus one key per
        grade holding a price in major currency units.

        Must not raise for an upstream failure. Returning what was last known —
        or nothing — is always better than a 500 that breaks the Drive tab.
        """

    async def refresh(self) -> bool:
        """Force a re-fetch. True when usable data is in hand afterwards."""
        return True

    # Presentation

    def units(self) -> Dict[str, Any]:
        """The block the API hands the UI so no formatting is hardcoded there."""
        return {
            "currency": self.currency,
            "symbol": self.currency_symbol,
            "volume": self.volume_unit,
            "distance": self.distance_unit,
            "display_scale": self.display_scale,
            "decimals": self.display_decimals,
        }

    def status(self) -> Dict[str, Any]:
        return {
            "region": self.region,
            "label": self.label,
            "source": self.source,
            "configured": self.configured,
            "station_level": self.station_level,
            "stations_loaded": len(self.stations),
            "last_refresh": self._last_refresh or None,
            "last_error": self._last_error,
            "attribution": self.attribution,
            "units": self.units(),
            "fuel_types": dict(self.grades),
        }


class BulkSnapshotProvider(FuelProvider):
    """
    A source that publishes its whole country at once.

    One national fetch per refresh window serves every query inside it, and the
    radius is applied locally. Subclasses implement `_fetch_all`; everything
    else — the TTL, the lock, the "stale beats empty" rule — is here because
    getting that wrong is what turns a feed blip into an empty car screen.
    """

    #: Seconds between national fetches. Floor of 60 regardless of config: a
    #: source that caps its own staleness at 30 minutes learns nothing from
    #: being asked every second, it just spends rate limit.
    refresh_s: float = 1800.0

    #: A refresh returning less than this fraction of the previous snapshot is
    #: rejected and the old data kept.
    #:
    #: Observed, not theoretical: the French export answered one request with
    #: 930 stations instead of 9,677 and an HTTP 200. "Stale beats empty" does
    #: not catch that — a truncated body is not an empty one — so a national
    #: feed that loses half its stations between two polls is treated as a bad
    #: response rather than as news. Set to 0 to disable.
    min_retained_fraction: float = 0.5

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self._stations: List[Dict[str, Any]] = []
        self._lock = asyncio.Lock()

    @property
    def stations(self) -> List[Dict[str, Any]]:
        return self._stations

    @abc.abstractmethod
    async def _fetch_all(self) -> List[Dict[str, Any]]:
        """The whole country, in station shape. [] means the fetch failed."""

    async def _ensure_fresh(self, force: bool = False) -> bool:
        async with self._lock:
            fresh_enough = (time.time() - self._last_refresh) < self.refresh_s
            if self._stations and fresh_enough and not force:
                return True
            try:
                fetched = await self._fetch_all()
            except Exception as e:                        # noqa: BLE001
                self._last_error = f"{self.label or self.region} refresh failed: {e}"
                logger.warning(self._last_error)
                return bool(self._stations)

            if not fetched:
                if not self._last_error:
                    self._last_error = (
                        f"{self.label or self.region} returned no usable stations"
                    )
                logger.warning(self._last_error)
                # Stale data still answers the question. Only an empty cache is
                # a failure.
                return bool(self._stations)

            floor = len(self._stations) * self.min_retained_fraction
            if self._stations and len(fetched) < floor:
                self._last_error = (
                    f"{self.label or self.region} returned {len(fetched)} stations, "
                    f"down from {len(self._stations)} — keeping the previous "
                    f"snapshot"
                )
                logger.warning(self._last_error)
                # Deliberately not stored: half a country is worse than a
                # slightly old whole one, because the missing half looks like
                # "no station near you" rather than like an error.
                return True

            self._stations = fetched
            self._last_refresh = time.time()
            self._last_error = None
            logger.info("%s: %d stations loaded", self.label or self.region, len(fetched))
            return True

    async def refresh(self) -> bool:
        return await self._ensure_fresh(force=True)

    async def nearby(
        self, lat: float, lon: float, radius_km: float
    ) -> List[Dict[str, Any]]:
        if not await self._ensure_fresh():
            return []
        radius_km = self.clamp_radius(radius_km)
        out = []
        for s in self._stations:
            try:
                d = haversine_km(lat, lon, s["latitude"], s["longitude"])
            except (KeyError, TypeError):
                continue
            if d <= radius_km:
                out.append({**s, "dist": round(d, 2)})
        return out


class RadiusQueryProvider(FuelProvider):
    """
    A source that must be asked per location.

    There is no national snapshot to cache, so the cache is keyed on a coarse
    coordinate bucket instead: two lookups from the same neighbourhood inside
    the TTL are one upstream call. `min_interval_s` serialises calls that do
    miss, because these APIs publish hard rate limits (Tankerkoenig: one request
    per minute) and exceeding one turns into a 429 on a query the driver is
    waiting for.
    """

    #: Cache lifetime for one bucket.
    cache_ttl_s: float = 300.0
    #: Bucket size in degrees. ~0.05 deg is roughly 5 km of latitude — small
    #: enough that a cached answer is still about where you are, large enough
    #: that nudging the map does not re-query.
    bucket_deg: float = 0.05
    #: Minimum spacing between upstream calls. 0 disables the limiter.
    min_interval_s: float = 0.0
    max_radius_km: float = 25.0

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self._cache: Dict[Tuple[int, int, int], Tuple[float, List[Dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()
        self._last_call: float = 0.0

    @abc.abstractmethod
    async def _fetch_nearby(
        self, lat: float, lon: float, radius_km: float
    ) -> List[Dict[str, Any]]:
        """One upstream query, in station shape."""

    def _bucket(self, lat: float, lon: float, radius_km: float):
        return (
            int(lat / self.bucket_deg),
            int(lon / self.bucket_deg),
            int(radius_km),
        )

    async def _throttle(self) -> None:
        if self.min_interval_s <= 0:
            return
        wait = self._last_call + self.min_interval_s - time.time()
        if wait > 0:
            await asyncio.sleep(wait)

    async def nearby(
        self, lat: float, lon: float, radius_km: float
    ) -> List[Dict[str, Any]]:
        radius_km = self.clamp_radius(radius_km)
        key = self._bucket(lat, lon, radius_km)

        # Held across the upstream call, not just the cache read: two concurrent
        # misses in the same bucket must make one request, and the limiter below
        # only means anything if callers queue on it.
        async with self._lock:
            hit = self._cache.get(key)
            if hit and (time.time() - hit[0]) < self.cache_ttl_s:
                return hit[1]

            try:
                await self._throttle()
                found = await self._fetch_nearby(lat, lon, radius_km)
                self._last_call = time.time()
            except Exception as e:                        # noqa: BLE001
                self._last_error = f"{self.label or self.region} query failed: {e}"
                logger.warning(self._last_error)
                self._last_call = time.time()
                return hit[1] if hit else []

            self._cache[key] = (time.time(), found)
            self._last_refresh = time.time()
            self._last_error = None
            return found

    async def refresh(self) -> bool:
        """Drop the bucket cache. There is no snapshot to re-fetch eagerly."""
        async with self._lock:
            self._cache.clear()
        return True


class FallbackProvider(FuelProvider):
    """
    A chain of providers for one region, tried in order.

    This is the UK arrangement made explicit: the statutory Fuel Finder feed
    first, the retailer open-data feeds behind it, so a misconfigured or
    unreachable government API degrades to the source this project used before
    it rather than to an empty Drive tab. It reports whichever child actually
    answered, because a price that disagrees with the pump is the first thing
    anyone asks about and the answer starts with which feed produced it.
    """

    def __init__(self, region: str, label: str, children: List[FuelProvider],
                 config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        if not children:
            raise ValueError(f"FallbackProvider for {region} needs at least one child")
        self.region = region
        self.label = label
        self._children = children
        self._active: Optional[FuelProvider] = None
        # The chain speaks the dialect of its first child; a fallback that
        # disagreed about grades or currency would be a different region, not a
        # fallback.
        head = children[0]
        self.grades = head.grades
        self.default_grade = head.default_grade
        self.currency = head.currency
        self.currency_symbol = head.currency_symbol
        self.volume_unit = head.volume_unit
        self.distance_unit = head.distance_unit
        self.display_scale = head.display_scale
        self.display_decimals = head.display_decimals
        self.station_level = head.station_level
        self.attribution = head.attribution
        self.default_radius_km = head.default_radius_km
        self.max_radius_km = max(c.max_radius_km for c in children)

    @property
    def children(self) -> List[FuelProvider]:
        return list(self._children)

    @property
    def configured(self) -> bool:
        return any(c.configured for c in self._children)

    @property
    def source(self) -> str:
        return self._active.source if self._active else "none"

    @property
    def stations(self) -> List[Dict[str, Any]]:
        return self._active.stations if self._active else []

    async def nearby(
        self, lat: float, lon: float, radius_km: float
    ) -> List[Dict[str, Any]]:
        errors = []
        for child in self._children:
            if not child.configured:
                continue
            found = await child.nearby(lat, lon, self.clamp_radius(radius_km))
            if found:
                self._active = child
                self._last_refresh = time.time()
                self._last_error = None
                return found
            if child.last_error:
                errors.append(f"{child.source}: {child.last_error}")
                logger.warning(
                    "%s returned nothing (%s) — trying the next source",
                    child.source, child.last_error,
                )

        # An empty radius is not a failure: rural coordinates legitimately have
        # no forecourt within 8 km. Only report an error if one was raised.
        self._last_error = "; ".join(errors) or None
        return []

    async def refresh(self) -> bool:
        ok = False
        for child in self._children:
            if child.configured:
                ok = await child.refresh() or ok
        return ok

    def status(self) -> Dict[str, Any]:
        st = super().status()
        st["chain"] = [
            {"source": c.source, "configured": c.configured, "last_error": c.last_error}
            for c in self._children
        ]
        return st
