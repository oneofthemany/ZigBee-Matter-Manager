"""
Italy — MIMIT's Osservaprezzi Carburanti open data.

Published as two pipe-delimited CSVs that have to be joined on `idImpianto`:
one is the register of active stations (location, brand), the other the prices.
Both begin with a banner line — `Estrazione del YYYY-MM-DD` — before the real
header, which is why the parser skips a line before reading columns.

Two decisions this file makes that the data forces:

**Self-service wins.** Italy prices most fuels twice, `isSelf=1` and `isSelf=0`,
and about half of all station/fuel pairs carry both. Self-service is the cheaper
of the two and the one comparison sites quote, so it is preferred and the served
price is the fallback. Showing the served price beside a competitor's
self-service price would make a station look dearer than it is.

**Only the four standard grades are selectable.** The feed carries eighty-odd
`descCarburante` values, most of them one chain's branded premium diesel. Benzina,
Gasolio, GPL and Metano are what every station names the same way; the rest are
ignored rather than turned into grades nobody can compare across brands.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from modules.fuel.base import BulkSnapshotProvider

logger = logging.getLogger("modules.fuel.providers.it_mimit")

ANAGRAFICA_URL = "https://www.mimit.gov.it/images/exportCSV/anagrafica_impianti_attivi.csv"
PREZZI_URL = "https://www.mimit.gov.it/images/exportCSV/prezzo_alle_8.csv"

DELIMITER = "|"
#: Both files open with "Estrazione del <date>" before the header row.
BANNER_LINES = 1

#: descCarburante -> the code this project exposes. Matched case-insensitively
#: on the exact name; branded variants ("Blue Diesel", "HiQ Perform+") are left
#: out on purpose — see the module docstring.
GRADE_NAMES = {
    "benzina": "BENZINA",
    "gasolio": "GASOLIO",
    "gpl": "GPL",
    "metano": "METANO",
}

FUEL_TYPES = {
    "BENZINA": "Benzina",
    "GASOLIO": "Gasolio",
    "GPL": "GPL",
    "METANO": "Metano",
}


def _num(raw: Any) -> Optional[float]:
    """A price to a float. None for absent, blank or unparseable."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _rows(text: str) -> List[Dict[str, str]]:
    """Parse one of the two CSVs, skipping the banner line above the header."""
    lines = text.splitlines()
    if len(lines) <= BANNER_LINES:
        return []
    reader = csv.DictReader(lines[BANNER_LINES:], delimiter=DELIMITER)
    return [r for r in reader if r]


class ItalyMimit(BulkSnapshotProvider):
    """Every Italian forecourt, joined from the register and the price file."""

    region = "IT"
    label = "Italy (Osservaprezzi)"
    grades = FUEL_TYPES
    default_grade = "BENZINA"
    currency = "EUR"
    currency_symbol = "€"
    volume_unit = "L"
    distance_unit = "km"
    display_scale = "major"
    display_decimals = 3
    attribution = "Osservaprezzi Carburanti — MIMIT, Italian Open Data Licence 2.0"

    #: The price file is published once a day ("prezzo_alle_8" — the 8 a.m.
    #: extract), so an hourly poll is already more often than it changes.
    refresh_s = 3600.0

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        cfg = config or {}
        self.anagrafica_url = str(cfg.get("anagrafica_url") or ANAGRAFICA_URL).strip()
        self.prezzi_url = str(cfg.get("prezzi_url") or PREZZI_URL).strip()

    @property
    def source(self) -> str:
        return "it_mimit"

    async def _get(self, sess: aiohttp.ClientSession, url: str) -> str:
        async with sess.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"{url} returned HTTP {resp.status}")
            # The files are latin-1 in places despite declaring otherwise;
            # replacing an undecodable byte loses one character of a station
            # name, whereas raising loses every price in the country.
            raw = await resp.read()
            return raw.decode("utf-8", errors="replace")

    async def _fetch_all(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            registry_text = await self._get(sess, self.anagrafica_url)
            prices_text = await self._get(sess, self.prezzi_url)

        stations = self._parse(registry_text, prices_text)
        if not stations:
            self._last_error = "Osservaprezzi returned no usable stations"
        return stations

    def _parse(self, registry_text: str, prices_text: str) -> List[Dict[str, Any]]:
        """The two CSVs -> station dicts. Pure, so it can be tested."""
        prices = self._parse_prices(_rows(prices_text))
        return self._parse_registry(_rows(registry_text), prices)

    @staticmethod
    def _parse_prices(rows: List[Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
        """
        idImpianto -> {grade: price, "_updated": newest timestamp}.

        Self-service beats served for the same grade; anything else keeps the
        first value seen, so a duplicate row cannot quietly overwrite a price
        with an identical one.
        """
        out: Dict[str, Dict[str, Any]] = {}
        best_is_self: Dict[Tuple[str, str], bool] = {}

        for row in rows:
            site_id = (row.get("idImpianto") or "").strip()
            grade = GRADE_NAMES.get((row.get("descCarburante") or "").strip().lower())
            price = _num(row.get("prezzo"))
            if not site_id or not grade or price is None or price <= 0:
                continue
            is_self = (row.get("isSelf") or "").strip() == "1"

            key = (site_id, grade)
            if key in best_is_self and not (is_self and not best_is_self[key]):
                continue
            best_is_self[key] = is_self

            entry = out.setdefault(site_id, {})
            entry[grade] = price
            stamp = (row.get("dtComu") or "").strip()
            if stamp:
                # Kept as published (dd/mm/yyyy HH:MM:SS). Compared as text only
                # to pick one, never parsed into a date — the history module
                # falls back to the day it saw the value, which is right for a
                # feed extracted once a day.
                entry["_updated"] = max(entry.get("_updated") or "", stamp)
        return out

    @staticmethod
    def _parse_registry(rows: List[Dict[str, str]],
                        prices: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        stations: List[Dict[str, Any]] = []
        for row in rows:
            site_id = (row.get("idImpianto") or "").strip()
            found = prices.get(site_id)
            if not site_id or not found:
                continue
            lat = _num(row.get("Latitudine"))
            lon = _num(row.get("Longitudine"))
            if lat is None or lon is None or (lat == 0 and lon == 0):
                continue

            grades = {k: v for k, v in found.items() if not k.startswith("_")}
            if not grades:
                continue

            town = (row.get("Comune") or "").strip() or None
            stations.append({
                "site_id": site_id,
                # 'Bandiera' is the flag over the forecourt — Agip Eni, Q8, IP.
                # 'Gestore' is the company that runs it, which a driver does not
                # recognise from the road.
                "brand": (row.get("Bandiera") or "").strip() or None,
                "address": (row.get("Indirizzo") or "").strip() or None,
                "town": town,
                # The register carries no CAP. Left absent rather than filled
                # with the comune, which is not a postcode; the Maps link falls
                # back to the address and town instead.
                "postcode": None,
                "latitude": lat,
                "longitude": lon,
                "last_updated": found.get("_updated"),
                **grades,
            })
        return stations
