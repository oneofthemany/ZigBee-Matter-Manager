"""
Western Australia — FuelWatch.

WA is the odd one of the three, and not because of its API. Under the state's
24-hour rule a station sets tomorrow's price today and may not change it for the
whole of tomorrow, so FuelWatch publishes a price that is *fixed in advance*
rather than one that moves during the day. `priceToday` is what a driver pays
now; `priceTomorrow` is already known and already binding. Only today's is
offered as a price, because a cheapest-first list built from tomorrow's numbers
would send someone to a station that is dearer when they arrive.

The old `/rss/fuelwatchrss` feed this was going to use no longer exists — the
site was rewritten and that path now serves the single-page app shell. The
current JSON API at `/api/sites` replaces it, and needs no key.

It answers for one fuel type per request, so the grades are fetched concurrently
and merged by site. The whole state is about 950 stations, so this is a
[BulkSnapshotProvider] like the European feeds.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from modules.fuel.base import BulkSnapshotProvider
from modules.fuel.providers import au_common

logger = logging.getLogger("modules.fuel.providers.au_wa")

BASE_URL = "https://www.fuelwatch.wa.gov.au/api/sites"

FUEL_TYPES = {
    "ULP": "Unleaded",
    "PUP": "Premium unleaded",
    "98R": "98 RON",
    "DSL": "Diesel",
    "BDL": "Brand diesel",
    "LPG": "LPG",
    "E85": "E85",
}

#: All of them: unlike NSW this is a once-an-hour background refresh rather than
#: something a user waits on, and seven requests for the whole state is cheap.
DEFAULT_GRADES = tuple(FUEL_TYPES)


class WesternAustraliaFuelWatch(BulkSnapshotProvider):
    """Every WA forecourt, one request per grade, merged by site."""

    region = "AU-WA"
    label = "Australia — WA (FuelWatch)"
    grades = FUEL_TYPES
    default_grade = "ULP"
    currency = au_common.CURRENCY
    currency_symbol = au_common.CURRENCY_SYMBOL
    volume_unit = au_common.VOLUME_UNIT
    distance_unit = au_common.DISTANCE_UNIT
    display_scale = au_common.DISPLAY_SCALE
    display_decimals = au_common.DISPLAY_DECIMALS
    attribution = "FuelWatch — Government of Western Australia"

    #: Prices change at most once a day under the 24-hour rule, so an hourly
    #: poll is already far more often than there is anything new to learn.
    refresh_s = 3600.0

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        cfg = config or {}
        self.base_url = str(cfg.get("base_url") or BASE_URL).strip()
        wanted = [str(g).upper() for g in (cfg.get("grades") or DEFAULT_GRADES)
                  if str(g).upper() in FUEL_TYPES]
        self._wanted = tuple(wanted or DEFAULT_GRADES)
        # Closed sites keep their published price. Shown by default for the same
        # reason as Germany: planning a stop means knowing what a place charges,
        # not only what is open at this second.
        self.only_open = bool(cfg.get("only_open", False))

    @property
    def source(self) -> str:
        return "au_wa_fuelwatch"

    async def _fetch_all(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            results = await asyncio.gather(
                *(self._one_grade(sess, grade) for grade in self._wanted),
                return_exceptions=True,
            )

        merged: Dict[str, Dict[str, Any]] = {}
        failures = []
        for grade, result in zip(self._wanted, results):
            if isinstance(result, Exception):
                failures.append(f"{grade}: {result}")
                continue
            self._merge(merged, grade, result)

        stations = self._parse(merged)
        if failures and not stations:
            self._last_error = "FuelWatch request failed — " + "; ".join(failures)
        elif failures:
            logger.warning("FuelWatch: some grades unavailable — %s", "; ".join(failures))
        return stations

    async def _one_grade(self, sess: aiohttp.ClientSession,
                         grade: str) -> List[Dict[str, Any]]:
        async with sess.get(self.base_url, params={"fuelType": grade}) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.json(content_type=None)

    def _merge(self, merged: Dict[str, Dict[str, Any]], grade: str,
               rows: Optional[List[Dict[str, Any]]]) -> None:
        """Fold one grade's site list into the map, keyed on the site id."""
        for row in rows or []:
            site_id = row.get("id")
            if site_id is None:
                continue
            if self.only_open and row.get("isClosedNow"):
                continue
            key = str(site_id)
            entry = merged.setdefault(key, {"_row": row})
            # Later grades carry the same site record; the first one wins, so
            # the address does not flip between requests.
            entry.setdefault("_row", row)

            product = row.get("product") or {}
            if product.get("isOutOfSupply"):
                continue
            value = au_common.price_from_cents(product.get("priceToday"))
            if value is not None:
                entry[grade] = value

    def _parse(self, merged: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Merged sites -> station dicts. Pure, so it can be tested."""
        stations: List[Dict[str, Any]] = []
        for key, entry in merged.items():
            row = entry.get("_row") or {}
            address = row.get("address") or {}
            lat, lon = address.get("latitude"), address.get("longitude")
            prices = {g: v for g, v in entry.items() if g in FUEL_TYPES}
            if lat is None or lon is None or not prices:
                continue

            postcode = address.get("postCode")
            stations.append({
                "site_id": key,
                # brandName is a short code ("CCO"); siteName is what is on the
                # sign, which is what a driver is looking for from the road.
                "brand": (row.get("siteName") or "").strip()
                         or (row.get("brandName") or "").strip() or None,
                "address": (address.get("line1") or "").strip() or None,
                "town": (address.get("location") or "").strip() or None,
                "postcode": str(postcode) if postcode not in (None, "") else None,
                "latitude": float(lat),
                "longitude": float(lon),
                # FuelWatch stamps nothing per site: under the 24-hour rule the
                # price belongs to the day, and history keys on the day it saw
                # the value, which is the same thing here.
                "last_updated": None,
                **prices,
            })

        if not stations:
            self._last_error = "FuelWatch returned no usable stations"
        return stations
