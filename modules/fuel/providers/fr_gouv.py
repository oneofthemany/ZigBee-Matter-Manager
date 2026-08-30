"""
France — the *flux instantané*, the live national price feed.

data.economie.gouv.fr republishes what stations report, within about ten minutes.
The whole country exports as one JSON array, so this is a [BulkSnapshotProvider].

Two format traps, both handled in one place each:

Coordinates arrive as integer strings scaled by 100000 — `'5004475'` is 50.04475
and `'-269000'` is -2.69. There is a `geom` column with proper decimals, but it
is written as a Python dict repr (single quotes), not JSON, so it is not safely
parseable; the scaled integers are.

An absent price is the four-character string `'None'`, not null and not empty.
Read naively that is truthy, and `float('None')` raises rather than returning
nothing, so `_num` treats it as absent explicitly.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import aiohttp

from modules.fuel.base import BulkSnapshotProvider

logger = logging.getLogger("modules.fuel.providers.fr_gouv")

DATASET = "prix-des-carburants-en-france-flux-instantane-v2"
BASE_URL = (f"https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
            f"{DATASET}/exports/json")

#: Feed column prefix -> the code this project exposes.
GRADE_FIELDS = {
    "gazole": "GAZOLE",
    "sp95": "SP95",
    "sp98": "SP98",
    "e10": "E10",
    "e85": "E85",
    "gplc": "GPLC",
}

FUEL_TYPES = {
    "GAZOLE": "Gazole (B7)",
    "SP95": "SP95",
    "SP98": "SP98",
    "E10": "SP95-E10",
    "E85": "Superéthanol E85",
    "GPLC": "GPL (GPLc)",
}

#: Latitude and longitude are published as integers scaled by this factor.
COORD_SCALE = 100000.0

#: Strings the feed uses for "no value". 'None' is the literal four characters,
#: not a JSON null.
_ABSENT = {"", "none", "null"}


def _num(raw: Any) -> Optional[float]:
    """A feed number to a float, or None when the feed means 'no value'."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text.lower() in _ABSENT:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class FranceGouv(BulkSnapshotProvider):
    """Every French forecourt reporting a price, refreshed on a timer."""

    region = "FR"
    label = "France (flux instantané)"
    grades = FUEL_TYPES
    default_grade = "GAZOLE"
    currency = "EUR"
    currency_symbol = "€"
    volume_unit = "L"
    distance_unit = "km"
    display_scale = "major"
    display_decimals = 3
    attribution = "Prix des carburants — data.economie.gouv.fr, Licence Ouverte"

    #: The feed itself refreshes about every ten minutes; fifteen keeps the
    #: hub comfortably inside that without polling for nothing.
    refresh_s = 900.0

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self.base_url = str((config or {}).get("base_url") or BASE_URL).strip()

    @property
    def source(self) -> str:
        return "fr_gouv"

    async def _fetch_all(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            # limit=-1 is this API's "everything"; without it the export is
            # capped and the south of the country quietly goes missing.
            async with sess.get(self.base_url, params={"limit": "-1"}) as resp:
                if resp.status != 200:
                    self._last_error = f"data.economie.gouv.fr returned HTTP {resp.status}"
                    return []
                payload = await resp.json(content_type=None)

        stations = self._parse(payload)
        if not stations:
            self._last_error = "data.economie.gouv.fr returned no usable stations"
        return stations

    def _parse(self, payload: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """Export rows -> station dicts. Pure, so it can be tested."""
        stations: List[Dict[str, Any]] = []
        for row in payload or []:
            lat = _num(row.get("latitude"))
            lon = _num(row.get("longitude"))
            site_id = str(row.get("id") or "").strip()
            if lat is None or lon is None or not site_id:
                continue
            lat, lon = lat / COORD_SCALE, lon / COORD_SCALE
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue

            prices, stamps = {}, []
            for field, code in GRADE_FIELDS.items():
                value = _num(row.get(f"{field}_prix"))
                if value is not None and value > 0:
                    prices[code] = value
                    updated = str(row.get(f"{field}_maj") or "").strip()
                    if updated and updated.lower() not in _ABSENT:
                        stamps.append(updated)
            if not prices:
                continue

            town = (row.get("ville") or "").strip() or None
            stations.append({
                "site_id": site_id,
                # This dataset carries no brand: there is no enseigne column at
                # all. The town is the most useful identity available, and it is
                # what makes the Maps link land on the right forecourt rather
                # than on a bare pin.
                "brand": town,
                "address": (row.get("adresse") or "").strip() or None,
                "town": town,
                "postcode": (row.get("cp") or "").strip() or None,
                "latitude": lat,
                "longitude": lon,
                # Prices are stamped per fuel; the newest is what "how old is
                # this station's data" means.
                "last_updated": max(stamps) if stamps else None,
                **prices,
            })
        return stations
