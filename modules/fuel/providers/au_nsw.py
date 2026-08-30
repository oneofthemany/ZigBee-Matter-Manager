"""
New South Wales (and the ACT) — FuelCheck.

Stations must report a price change within 30 minutes of making it, and
FuelCheck republishes that. The endpoint used here is `FuelCheckApp/v1`, the one
the official mobile app calls: it is a nearby search, so this is a
[RadiusQueryProvider], and it needs no credentials — only a `requesttimestamp`
header.

That last point deserves saying plainly. NSW also runs a documented, supported
`FuelCheck/v2` API behind OAuth client credentials on the OneGov gateway, and if
this endpoint is ever closed that is where to go: the request and response
shapes are close, and only `_fetch_nearby` would change. The app endpoint is
used because it works today without asking an operator to register for a key, in
exchange for depending on something NSW has not promised to keep.

The API answers for one fuel type per request. The provider contract has no
grade — `best_nearby` filters after the fact and the UI shows alternatives
without re-querying — so the grades are fetched concurrently and merged into one
station each. Ten minutes of caching per neighbourhood keeps that to one burst.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from modules.fuel.base import RadiusQueryProvider
from modules.fuel.providers import au_common

logger = logging.getLogger("modules.fuel.providers.au_nsw")

BASE_URL = "https://api.onegov.nsw.gov.au/FuelCheckApp/v1/fuel"
NEARBY_PATH = "/prices/nearby"

FUEL_TYPES = {
    "E10": "Unleaded E10",
    "U91": "Unleaded 91",
    "P95": "Premium 95",
    "P98": "Premium 98",
    "DL": "Diesel",
    "PDL": "Premium diesel",
    "LPG": "LPG",
    "B20": "Biodiesel B20",
    "E85": "Ethanol E85",
}

#: Fetched on every lookup. Deliberately not all nine: each is its own HTTP
#: request, and B20 and E85 are sold at a handful of sites statewide, so asking
#: for them on every search costs two round trips to almost always learn
#: nothing. They stay selectable — a user who picks one gets it, because
#: `grades_wanted` puts the chosen grade in the set.
DEFAULT_GRADES = ("E10", "U91", "P95", "P98", "DL", "PDL")

#: FuelCheck lists EV charging under the same endpoint at a price of 0.0, which
#: would sort to the top of a cheapest-first list and read as free petrol.
EXCLUDED_GRADES = ("EV",)

#: The API wants this exact format, and rejects ISO-8601.
_TIMESTAMP_FMT = "%d/%m/%Y %H:%M:%S"

MAX_RADIUS_KM = 50.0
CACHE_TTL_S = 600.0


class NewSouthWalesFuelCheck(RadiusQueryProvider):
    """Stations near a point, from the FuelCheck app endpoint."""

    region = "AU-NSW"
    label = "Australia — NSW & ACT (FuelCheck)"
    grades = FUEL_TYPES
    default_grade = "U91"
    currency = au_common.CURRENCY
    currency_symbol = au_common.CURRENCY_SYMBOL
    volume_unit = au_common.VOLUME_UNIT
    distance_unit = au_common.DISTANCE_UNIT
    display_scale = au_common.DISPLAY_SCALE
    display_decimals = au_common.DISPLAY_DECIMALS
    attribution = "FuelCheck — NSW Government, CC BY 4.0"

    max_radius_km = MAX_RADIUS_KM
    cache_ttl_s = CACHE_TTL_S
    # No published rate limit on this endpoint, and the grades are fetched
    # concurrently, so serialising them would only make a lookup slower.
    min_interval_s = 0.0

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        cfg = config or {}
        self.base_url = str(cfg.get("base_url") or BASE_URL).strip().rstrip("/")
        wanted = cfg.get("grades") or DEFAULT_GRADES
        self._wanted = tuple(self.grades_wanted(wanted))

    @property
    def source(self) -> str:
        return "au_nsw_fuelcheck"

    def grades_wanted(self, wanted) -> List[str]:
        """Configured grades, filtered to ones this scheme actually publishes."""
        out = [str(g).upper() for g in wanted
               if str(g).upper() in FUEL_TYPES and str(g).upper() not in EXCLUDED_GRADES]
        return out or list(DEFAULT_GRADES)

    async def _fetch_nearby(
        self, lat: float, lon: float, radius_km: float
    ) -> List[Dict[str, Any]]:
        headers = {
            "Content-Type": "application/json",
            "requesttimestamp": dt.datetime.now().strftime(_TIMESTAMP_FMT),
        }
        timeout = aiohttp.ClientTimeout(total=30)
        url = f"{self.base_url}{NEARBY_PATH}"

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
            results = await asyncio.gather(
                *(self._one_grade(sess, url, grade, lat, lon, radius_km)
                  for grade in self._wanted),
                return_exceptions=True,
            )

        merged: Dict[str, Dict[str, Any]] = {}
        failures = []
        for grade, result in zip(self._wanted, results):
            if isinstance(result, Exception):
                # One grade failing must not lose the other five: a search for
                # the cheapest unleaded should still answer when the diesel
                # request times out.
                failures.append(f"{grade}: {result}")
                continue
            self._merge(merged, result)

        # A station whose every price was unusable is not worth returning: it
        # would show in the count and then vanish from the table. Pruned once,
        # after every grade has had its chance to price it.
        priced = [s for s in merged.values()
                  if any(g in s for g in FUEL_TYPES)]

        if failures and not priced:
            self._last_error = "FuelCheck request failed — " + "; ".join(failures)
        elif failures:
            logger.warning("FuelCheck: some grades unavailable — %s", "; ".join(failures))
        return priced

    async def _one_grade(self, sess, url, grade, lat, lon, radius_km) -> Dict[str, Any]:
        body = {
            "fueltype": grade,
            "latitude": float(lat),
            "longitude": float(lon),
            # The API takes whole kilometres and rejects a float.
            "radius": int(round(min(float(radius_km), MAX_RADIUS_KM))),
            "brand": [],
        }
        async with sess.post(url, json=body) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.json(content_type=None)

    def _merge(self, merged: Dict[str, Dict[str, Any]],
               payload: Optional[Dict[str, Any]]) -> None:
        """
        Fold one grade's reply into the station map.

        The two halves of the reply are joined on the station's `code`, which is
        what `prices[].stationcode` refers to — not `stationid`, which is a
        different identifier in the same document.
        """
        payload = payload or {}
        for station in payload.get("stations") or []:
            code = station.get("code")
            if code is None:
                continue
            key = str(code)
            entry = merged.get(key)
            if entry is None:
                location = station.get("location") or {}
                lat_v, lon_v = location.get("latitude"), location.get("longitude")
                if lat_v is None or lon_v is None:
                    continue
                address = (station.get("address") or "").strip() or None
                entry = merged[key] = {
                    "site_id": key,
                    "brand": (station.get("brand") or "").strip() or None,
                    "address": address,
                    "town": None,
                    # NSW publishes no postcode field, only the address line.
                    "postcode": au_common.postcode_from_address(address),
                    "latitude": float(lat_v),
                    "longitude": float(lon_v),
                    # The API already measured the distance from the query
                    # point, so recomputing it would only be less accurate.
                    "dist": round(float(location.get("distance") or 0.0), 2),
                    "last_updated": None,
                }

        for price in payload.get("prices") or []:
            key = str(price.get("stationcode"))
            grade = str(price.get("fueltype") or "").upper()
            entry = merged.get(key)
            if entry is None or grade not in FUEL_TYPES or grade in EXCLUDED_GRADES:
                continue
            value = au_common.price_from_cents(price.get("price"))
            if value is None:
                continue
            entry[grade] = value
            stamp = str(price.get("lastupdated") or "").strip()
            if stamp:
                entry["last_updated"] = max(entry.get("last_updated") or "", stamp)

