"""
United States — EIA weekly retail prices. An area average, not a station list.

There is no free station-level price feed for the United States. Reporting is
not mandated the way it is in the UK, Germany, France, Spain, Italy and three
Australian states, so nothing plays the part those schemes play. What the Energy
Information Administration publishes instead is a *weekly average* for the
nation, for each PADD region, for nine states and for ten cities.

So this provider declares `station_level = False` and returns exactly one
record: the average where you are. The Drive tab renders that as a figure rather
than as a table, because presenting an average as though it were a forecourt
would be a lie about what the number is — nobody can drive to it, and it is
already up to a week old.

If a commercial station-level feed is ever bought, it drops in beside this as an
ordinary provider and nothing else changes.

Three things about the data:

**Dollars per US gallon**, not per litre. That is why `volume_unit` is
`gal_us`; the number is never converted, only labelled.

**Weekly.** Published Monday afternoons for the week before. `last_updated`
carries the week-ending date so the UI can say how old it is.

**Coverage thins as it gets local.** Nine states have their own series; every
other state falls back to its PADD region, and failing that the national number.
Diesel is only published nationally, for the PADDs and for California.

A free API key is required, from eia.gov/opendata/register.php. It goes in
config/secrets.yaml or the environment, never in config.yaml.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import aiohttp

from modules.fuel.base import RadiusQueryProvider

logger = logging.getLogger("modules.fuel.providers.us_eia")

BASE_URL = "https://api.eia.gov/v2/petroleum/pri/gnd/data/"

ENV_API_KEY = "ZMM_EIA_API_KEY"
SECRETS_FILE = os.environ.get("ZMM_SECRETS_FILE", "./config/secrets.yaml")

#: EIA product code -> the code this project exposes. These four are the
#: "all formulations" series, which is the only set published for every area —
#: the reformulated and conventional variants cover barely two thirds of them.
PRODUCT_CODES = {
    "EPMR": "REGULAR",
    "EPMM": "MIDGRADE",
    "EPMP": "PREMIUM",
    "EPD2DXL0": "DIESEL",
}

FUEL_TYPES = {
    "REGULAR": "Regular",
    "MIDGRADE": "Midgrade",
    "PREMIUM": "Premium",
    "DIESEL": "Diesel (ULSD)",
}

#: The national series, and the last resort for anywhere unrecognised.
NATIONAL_AREA = "NUS"
NATIONAL_NAME = "the United States"

#: States EIA publishes their own weekly series for. Everywhere else falls back
#: to a PADD region, which is a much wider area — Rocky Mountain is six states.
STATE_AREAS = {
    "CA": ("SCA", "California"),
    "CO": ("SCO", "Colorado"),
    "FL": ("SFL", "Florida"),
    "MA": ("SMA", "Massachusetts"),
    "MN": ("SMN", "Minnesota"),
    "NY": ("SNY", "New York"),
    "OH": ("SOH", "Ohio"),
    "TX": ("STX", "Texas"),
    "WA": ("SWA", "Washington"),
}

#: Petroleum Administration for Defense Districts — the areas EIA reports by.
#: PADD 1 is split into three sub-districts, and those are what is published.
PADD_NAMES = {
    "R1X": "New England (PADD 1A)",
    "R1Y": "the Central Atlantic (PADD 1B)",
    "R1Z": "the Lower Atlantic (PADD 1C)",
    "R20": "the Midwest (PADD 2)",
    "R30": "the Gulf Coast (PADD 3)",
    "R40": "the Rocky Mountains (PADD 4)",
    "R50": "the West Coast (PADD 5)",
}

STATE_PADDS = {
    # PADD 1A — New England
    "CT": "R1X", "ME": "R1X", "MA": "R1X", "NH": "R1X", "RI": "R1X", "VT": "R1X",
    # PADD 1B — Central Atlantic
    "DE": "R1Y", "DC": "R1Y", "MD": "R1Y", "NJ": "R1Y", "NY": "R1Y", "PA": "R1Y",
    # PADD 1C — Lower Atlantic
    "FL": "R1Z", "GA": "R1Z", "NC": "R1Z", "SC": "R1Z", "VA": "R1Z", "WV": "R1Z",
    # PADD 2 — Midwest
    "IL": "R20", "IN": "R20", "IA": "R20", "KS": "R20", "KY": "R20", "MI": "R20",
    "MN": "R20", "MO": "R20", "NE": "R20", "ND": "R20", "OH": "R20", "OK": "R20",
    "SD": "R20", "TN": "R20", "WI": "R20",
    # PADD 3 — Gulf Coast
    "AL": "R30", "AR": "R30", "LA": "R30", "MS": "R30", "NM": "R30", "TX": "R30",
    # PADD 4 — Rocky Mountain
    "CO": "R40", "ID": "R40", "MT": "R40", "UT": "R40", "WY": "R40",
    # PADD 5 — West Coast
    "AK": "R50", "AZ": "R50", "CA": "R50", "HI": "R50", "NV": "R50",
    "OR": "R50", "WA": "R50",
}

#: Full state names, since Nominatim answers with those rather than codes.
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN",
    "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

#: Weekly data, published Monday afternoon. Six hours is already far more often
#: than there is anything new, and keeps a restart from re-querying.
CACHE_TTL_S = 21600.0

#: One request covers a whole state or region, so the bucket only has to be fine
#: enough not to straddle a state line. A degree is roughly 110 km.
BUCKET_DEG = 0.5


def resolve_api_key(config: Dict[str, Any]) -> str:
    """
    The API key, from the environment or the gitignored secrets file.

    Same order and same reasoning as every other credential here: never
    config.yaml, which is tracked in git.
    """
    key = os.environ.get(ENV_API_KEY, "").strip()
    if key:
        return key

    try:
        import yaml
        with open(SECRETS_FILE, "r") as fh:
            secrets = (yaml.safe_load(fh) or {}).get("eia") or {}
        key = str(secrets.get("api_key") or "").strip()
    except FileNotFoundError:
        pass
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"Could not read {SECRETS_FILE}: {e}")
    if key:
        return key

    in_config = str(config.get("api_key") or "").strip()
    if in_config:
        logger.error(
            "EIA api_key found in config.yaml, which is TRACKED IN GIT. Move it "
            "to %s or the %s environment variable, then request a new one — "
            "assume the one in the file is burnt.",
            SECRETS_FILE, ENV_API_KEY,
        )
    return in_config


def _area_name(area: str) -> str:
    """A human name for an EIA area code, falling back to the code itself."""
    if area == NATIONAL_AREA:
        return NATIONAL_NAME
    if area in PADD_NAMES:
        return PADD_NAMES[area]
    for code, name in STATE_AREAS.values():
        if code == area:
            return name
    return area


def area_for_state(state: str) -> tuple[str, str]:
    """
    The most local EIA area covering a state, and what to call it.

    Its own series if EIA publishes one, else its PADD, else the nation. Written
    as a fallback chain rather than a lookup because the honest answer for
    Wyoming is "the Rocky Mountains", not "no data".
    """
    code = (state or "").strip().upper()
    if len(code) != 2:
        code = STATE_NAMES.get((state or "").strip().lower(), "")
    if not code:
        return NATIONAL_AREA, NATIONAL_NAME
    if code in STATE_AREAS:
        return STATE_AREAS[code]
    padd = STATE_PADDS.get(code)
    if padd:
        return padd, PADD_NAMES[padd]
    return NATIONAL_AREA, NATIONAL_NAME


class UnitedStatesEIA(RadiusQueryProvider):
    """The weekly average price where you are, not the stations near you."""

    region = "US"
    label = "United States (EIA weekly average)"
    grades = FUEL_TYPES
    default_grade = "REGULAR"
    currency = "USD"
    currency_symbol = "$"
    volume_unit = "gal_us"
    distance_unit = "mi"
    display_scale = "major"
    display_decimals = 3

    #: The whole point of this provider. Everything downstream branches on it.
    station_level = False
    needs_credentials = True
    attribution = "U.S. Energy Information Administration, weekly retail prices"

    # A radius means nothing to an area average — the answer is the same
    # anywhere in the state — but the contract passes one, so it is accepted
    # and ignored rather than pretended to be honoured.
    max_radius_km = 100.0
    cache_ttl_s = CACHE_TTL_S
    bucket_deg = BUCKET_DEG
    min_interval_s = 0.0

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.api_key = resolve_api_key(cfg)
        self.base_url = str(cfg.get("base_url") or BASE_URL).strip()
        # An operator who knows their area code can pin it and skip the
        # reverse-geocode entirely.
        self.area_override = str(cfg.get("area") or "").strip().upper()

    @property
    def source(self) -> str:
        return "us_eia"

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.api_key)

    async def _area(self, lat: float, lon: float) -> tuple[str, str]:
        """Which EIA area covers this point, and what to call it."""
        if self.area_override:
            return self.area_override, _area_name(self.area_override)
        try:
            from modules.geocode import get_geocoder
            geocoder = get_geocoder()
            if geocoder is None:
                return NATIONAL_AREA, NATIONAL_NAME
            address = await geocoder.reverse_place(lat, lon, zoom=5)
        except Exception as e:                            # noqa: BLE001
            logger.warning(f"could not resolve a US state for {lat},{lon}: {e}")
            return NATIONAL_AREA, NATIONAL_NAME
        return area_for_state(str(address.get("state") or ""))

    async def _fetch_nearby(
        self, lat: float, lon: float, radius_km: float
    ) -> List[Dict[str, Any]]:
        if not self.configured:
            self._last_error = ("EIA API key not set — put it in "
                                "config/secrets.yaml under eia.api_key")
            return []

        area, area_name = await self._area(lat, lon)
        params = [
            ("api_key", self.api_key),
            ("frequency", "weekly"),
            ("data[0]", "value"),
            ("facets[duoarea][]", area),
            ("sort[0][column]", "period"),
            ("sort[0][direction]", "desc"),
            # Four products, a handful of weeks each: enough that the newest
            # row for every product is in the page even when one lags.
            ("length", "40"),
        ]
        params += [("facets[product][]", code) for code in PRODUCT_CODES]

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(self.base_url, params=params) as resp:
                if resp.status == 403:
                    self._last_error = "EIA rejected the API key (403)"
                    return []
                if resp.status != 200:
                    self._last_error = f"EIA returned HTTP {resp.status}"
                    return []
                payload = await resp.json(content_type=None)

        return self._parse(payload, area, area_name, lat, lon)

    def _parse(self, payload: Optional[Dict[str, Any]], area: str,
               area_name: str, lat: float, lon: float) -> List[Dict[str, Any]]:
        """The API reply -> one area record. Pure, so it can be tested."""
        payload = payload or {}
        if payload.get("error"):
            self._last_error = str(payload["error"])
            return []
        rows = ((payload.get("response") or {}).get("data")
                if "response" in payload else payload.get("data")) or []

        # Rows arrive newest first, so the first sighting of a product is the
        # current price and later ones are history.
        prices: Dict[str, float] = {}
        periods: Dict[str, str] = {}
        for row in rows:
            grade = PRODUCT_CODES.get(str(row.get("product") or "").strip())
            if grade is None or grade in prices:
                continue
            try:
                value = float(row.get("value"))
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            prices[grade] = round(value, 3)
            periods[grade] = str(row.get("period") or "").strip()

        if not prices:
            self._last_error = f"EIA returned no prices for {area}"
            return []

        return [{
            # Prefixed so an area code can never collide with a station id from
            # some other feed in the history table.
            "site_id": f"EIA:{area}",
            # There is no forecourt to name, so this is the area itself. The UI
            # shows it as a heading, not as a brand.
            "brand": area_name,
            "address": None,
            "town": None,
            "postcode": None,
            # The query point: the average has no location of its own, and a
            # null coordinate would break every caller that plots one.
            "latitude": lat,
            "longitude": lon,
            "dist": 0.0,
            # The week the figure covers, which is what "how old is this"
            # means for a weekly series.
            "last_updated": max(periods.values()) if periods else None,
            **prices,
        }]
