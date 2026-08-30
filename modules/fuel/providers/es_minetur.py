"""
Spain — the Ministry's fuel price register.

`sedeaplicaciones.minetur.gob.es` publishes every filling station in the country
as one JSON document, no key and no rate limit, which makes it a straight
[BulkSnapshotProvider] like the UK feeds.

Two things about the format are worth knowing before reading the parser: numbers
use a comma as the decimal separator, because the document is written for a
Spanish locale, and an absent price is an empty string rather than null. Both
are handled in `_num`, which is the only place either assumption lives.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import aiohttp

from modules.fuel.base import BulkSnapshotProvider

logger = logging.getLogger("modules.fuel.providers.es_minetur")

BASE_URL = ("https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes"
            "/PreciosCarburantes/EstacionesTerrestres/")

#: The ministry's own field name -> the code this project exposes. Only road
#: fuels a driver can select: the document also carries Gasoleo B (agricultural,
#: dyed, illegal in a road vehicle) and a dozen experimental fuels, which are
#: deliberately not offered.
GRADE_FIELDS = {
    "Precio Gasolina 95 E5": "G95E5",
    "Precio Gasolina 95 E10": "G95E10",
    "Precio Gasolina 98 E5": "G98E5",
    "Precio Gasoleo A": "GOA",
    "Precio Gasoleo Premium": "GOP",
    "Precio Gases licuados del petróleo": "GLP",
}

FUEL_TYPES = {
    "G95E5": "Gasolina 95 E5",
    "G95E10": "Gasolina 95 E10",
    "G98E5": "Gasolina 98 E5",
    "GOA": "Gasóleo A",
    "GOP": "Gasóleo Premium",
    "GLP": "GLP (autogas)",
}


def _num(raw: Any) -> Optional[float]:
    """
    A Spanish decimal to a float. None for absent, blank or unparseable.

    The comma is the decimal separator throughout this document — '1,599' is one
    euro fifty-nine, not one thousand five hundred and ninety-nine. Read as an
    English decimal it would be silently three orders of magnitude wrong, which
    is exactly the kind of error that reaches a user as a plausible price.
    """
    if raw is None:
        return None
    text = str(raw).strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class SpainMinetur(BulkSnapshotProvider):
    """Every Spanish forecourt, refreshed on a timer."""

    region = "ES"
    label = "Spain (Ministerio)"
    grades = FUEL_TYPES
    default_grade = "G95E5"
    currency = "EUR"
    currency_symbol = "€"
    volume_unit = "L"
    distance_unit = "km"
    display_scale = "major"
    display_decimals = 3
    attribution = "Precios de carburantes © Ministerio para la Transición Ecológica"

    #: The register is refreshed about every half hour, so asking more often
    #: spends bandwidth on an unchanged 12 MB document.
    refresh_s = 1800.0

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self.base_url = str((config or {}).get("base_url") or BASE_URL).strip()

    @property
    def source(self) -> str:
        return "es_minetur"

    async def _fetch_all(self) -> List[Dict[str, Any]]:
        # Generous: the document is around 12 MB and a hub on domestic broadband
        # doing anything else at the time should still finish rather than fail
        # late and fall back to nothing.
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(self.base_url) as resp:
                if resp.status != 200:
                    self._last_error = f"Ministerio returned HTTP {resp.status}"
                    return []
                # The service answers with text/html; content_type=None stops
                # aiohttp refusing to parse a body that is plainly JSON.
                payload = await resp.json(content_type=None)

        stations = self._parse(payload)
        if not stations:
            self._last_error = "Ministerio returned no usable stations"
        return stations

    def _parse(self, payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Register document -> station dicts. Pure, so it can be tested."""
        rows = (payload or {}).get("ListaEESSPrecio") or []
        # One timestamp for the whole document — the register is published as a
        # snapshot, not per station.
        stamp = (payload or {}).get("Fecha") or None

        stations: List[Dict[str, Any]] = []
        for row in rows:
            lat = _num(row.get("Latitud"))
            lon = _num(row.get("Longitud (WGS84)"))
            site_id = str(row.get("IDEESS") or "").strip()
            if lat is None or lon is None or not site_id:
                continue

            prices = {}
            for field, code in GRADE_FIELDS.items():
                value = _num(row.get(field))
                if value is not None and value > 0:
                    prices[code] = value
            if not prices:
                continue

            stations.append({
                "site_id": site_id,
                # 'Rótulo' is the sign over the forecourt. Independents carry a
                # number rather than a name, which is still what is written up
                # there and still how a driver recognises the place.
                "brand": (row.get("Rótulo") or "").strip() or None,
                "address": (row.get("Dirección") or "").strip() or None,
                "town": (row.get("Municipio") or "").strip() or None,
                "postcode": (row.get("C.P.") or "").strip() or None,
                "latitude": lat,
                "longitude": lon,
                "last_updated": stamp,
                **prices,
            })
        return stations
