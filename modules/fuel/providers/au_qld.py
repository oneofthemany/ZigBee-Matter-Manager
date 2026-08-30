"""
Queensland — the Fuel Price Reporting scheme, over the FPDAPI Direct API (OUT).

Retailers must report a price change within 30 minutes. The API splits what a
station *is* from what it *charges*: `GetFullSiteDetails` is the register, meant
to be pulled once a day, and `GetSitesPrices` is the prices, which the spec asks
not to be polled more than once a minute. This provider honours both — the
register is cached for a day, the prices refresh on the normal timer — so it is
a [BulkSnapshotProvider] whose "fetch everything" is two calls with very
different lifetimes.

A subscriber token is required, from fuelpricesqld.com.au. It goes in
config/secrets.yaml or the environment, never in config.yaml.

Two things from the published spec (API (OUT) v1.6) worth stating here, because
both are silently wrong if assumed:

**Prices are in tenths of a cent.** `1679` is 167.9c, or A$1.679. Read as cents
it is ten times too dear; read as dollars, a thousand times.

**9999 means "unavailable", not "very expensive".** It is a sentinel in the
price field, and left in it would be the dearest station in Queensland forever.

The register's JSON is minified — `S`, `N`, `A`, `B`, `P`, `Lat`, `Lng` — which
is why the field names below look like nothing else in this package.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

import aiohttp

from modules.fuel.base import BulkSnapshotProvider
from modules.fuel.providers import au_common

logger = logging.getLogger("modules.fuel.providers.au_qld")

BASE_URL = "https://fppdirectapi-prod.fuelpricesqld.com.au"
SITES_PATH = "/Subscriber/GetFullSiteDetails"
PRICES_PATH = "/Price/GetSitesPrices"
BRANDS_PATH = "/Subscriber/GetCountryBrands"
FUELS_PATH = "/Subscriber/GetCountryFuelTypes"

ENV_TOKEN = "ZMM_FUELPRICES_QLD_TOKEN"
SECRETS_FILE = os.environ.get("ZMM_SECRETS_FILE", "./config/secrets.yaml")

#: Fixed by the spec: 21 is Australia, geographic level 3 is states, and
#: within that, 1 is Queensland.
COUNTRY_ID = 21
GEO_REGION_LEVEL = 3
GEO_REGION_ID = 1

#: Tenths of a cent per litre -> dollars per litre.
TENTHS_OF_A_CENT = 1000.0

#: "If 9999 is returned in the Price field, this indicates that the product is
#: currently unavailable at the site."
UNAVAILABLE_PRICE = 9999

#: The register changes when a station opens, closes or is rebranded. The spec
#: asks for one call a day; this is that, with room for a restart.
SITE_CACHE_S = 86400.0

#: FuelId -> the code this project exposes. Queensland numbers its fuels rather
#: than naming them, and the numbering is stable across the scheme.
GRADE_IDS = {
    2: "U91",
    3: "PULP95",
    5: "PULP98",
    8: "DL",
    12: "PDL",
    14: "E10",
    16: "LPG",
    19: "B20",
    21: "E85",
}

FUEL_TYPES = {
    "U91": "Unleaded 91",
    "PULP95": "Premium 95",
    "PULP98": "Premium 98",
    "DL": "Diesel",
    "PDL": "Premium diesel",
    "E10": "Unleaded E10",
    "LPG": "LPG",
    "B20": "Biodiesel B20",
    "E85": "Ethanol E85",
}


def resolve_token(config: Dict[str, Any]) -> str:
    """
    The subscriber token, from the environment or the gitignored secrets file.

    Same order and same reasoning as every other credential here: the container
    gets an environment variable, a bare install gets a file it can edit, and
    neither is config.yaml, which is tracked in git.
    """
    token = os.environ.get(ENV_TOKEN, "").strip()
    if token:
        return token

    try:
        import yaml
        with open(SECRETS_FILE, "r") as fh:
            secrets = (yaml.safe_load(fh) or {}).get("fuelprices_qld") or {}
        token = str(secrets.get("token") or "").strip()
    except FileNotFoundError:
        pass
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"Could not read {SECRETS_FILE}: {e}")
    if token:
        return token

    in_config = str(config.get("token") or "").strip()
    if in_config:
        logger.error(
            "Fuel Prices QLD token found in config.yaml, which is TRACKED IN "
            "GIT. Move it to %s or the %s environment variable, then request a "
            "new one — assume the one in the file is burnt.",
            SECRETS_FILE, ENV_TOKEN,
        )
    return in_config


def _listing(payload: Any) -> List[Dict[str, Any]]:
    """
    The list inside one of this API's replies.

    The spec documents each response by showing a single element, not the
    envelope around it, and the envelope key differs per method. Rather than
    hard-code a guess for each, take the first list-valued key — and accept a
    bare list too, in case a method returns one.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


class QueenslandFuelPrices(BulkSnapshotProvider):
    """Every Queensland forecourt: the register, priced."""

    region = "AU-QLD"
    label = "Australia — QLD (Fuel Price Reporting)"
    grades = FUEL_TYPES
    default_grade = "U91"
    currency = au_common.CURRENCY
    currency_symbol = au_common.CURRENCY_SYMBOL
    volume_unit = au_common.VOLUME_UNIT
    distance_unit = au_common.DISTANCE_UNIT
    display_scale = au_common.DISPLAY_SCALE
    display_decimals = au_common.DISPLAY_DECIMALS
    needs_credentials = True
    attribution = ("Fuel price data © State of Queensland "
                   "(Department of Energy and Public Works)")

    #: The spec asks for no more than one price call a minute; half an hour
    #: matches the 30 minutes retailers have to report a change in.
    refresh_s = 1800.0

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.token = resolve_token(cfg)
        self.base_url = str(cfg.get("base_url") or BASE_URL).strip().rstrip("/")
        self._sites: Dict[str, Dict[str, Any]] = {}
        self._brands: Dict[int, str] = {}
        self._sites_fetched: float = 0.0

    @property
    def source(self) -> str:
        return "au_qld_fpdapi"

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.token)

    def _headers(self) -> Dict[str, str]:
        # The scheme's own scheme name, not Bearer — an ordinary Bearer header
        # is rejected with a 401 that says nothing about why.
        return {"Authorization": f"FPDAPI SubscriberToken={self.token}",
                "Content-Type": "application/json"}

    async def _get(self, sess: aiohttp.ClientSession, path: str,
                   params: Dict[str, Any]) -> Any:
        async with sess.get(f"{self.base_url}{path}", params=params) as resp:
            if resp.status == 401:
                raise RuntimeError("subscriber token rejected (401)")
            if resp.status != 200:
                raise RuntimeError(f"{path} returned HTTP {resp.status}")
            return await resp.json(content_type=None)

    async def _fetch_all(self) -> List[Dict[str, Any]]:
        if not self.configured:
            self._last_error = ("Fuel Prices QLD token not set — put it in "
                                "config/secrets.yaml under fuelprices_qld.token")
            return []

        region_params = {"countryId": COUNTRY_ID,
                         "geoRegionLevel": GEO_REGION_LEVEL,
                         "geoRegionId": GEO_REGION_ID}
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout,
                                         headers=self._headers()) as sess:
            # The register is meant to be pulled once a day, so it is only
            # re-fetched when the cached copy has aged out; prices come every
            # refresh.
            if self._stale_register():
                sites, brands = await asyncio.gather(
                    self._get(sess, SITES_PATH, region_params),
                    self._get(sess, BRANDS_PATH, {"countryId": COUNTRY_ID}),
                )
                self._brands = self._parse_brands(brands)
                self._sites = self._parse_sites(sites)
                if self._sites:
                    self._sites_fetched = time.time()

            prices = await self._get(sess, PRICES_PATH, region_params)

        if not self._sites:
            self._last_error = "Fuel Prices QLD returned no site register"
            return []
        return self._parse(prices)

    def _stale_register(self) -> bool:
        return (not self._sites
                or (time.time() - self._sites_fetched) > SITE_CACHE_S)

    @staticmethod
    def _parse_brands(payload: Any) -> Dict[int, str]:
        out: Dict[int, str] = {}
        for row in _listing(payload):
            try:
                out[int(row["BrandId"])] = str(row.get("Name") or "").strip()
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _parse_sites(self, payload: Any) -> Dict[str, Dict[str, Any]]:
        """The minified register -> site records keyed by site id."""
        out: Dict[str, Dict[str, Any]] = {}
        for row in _listing(payload):
            site_id = row.get("S")
            lat, lon = row.get("Lat"), row.get("Lng")
            if site_id is None or lat is None or lon is None:
                continue
            try:
                lat, lon = float(lat), float(lon)
            except (TypeError, ValueError):
                continue
            if lat == 0 and lon == 0:
                continue
            postcode = row.get("P")
            try:
                brand = self._brands.get(int(row.get("B")))
            except (TypeError, ValueError):
                brand = None
            out[str(site_id)] = {
                # N is the site's own name ("Caltex Surat"), which is more
                # useful on a forecourt than the brand alone.
                "brand": (row.get("N") or "").strip() or brand or None,
                "address": (row.get("A") or "").strip() or None,
                "town": None,
                "postcode": str(postcode).strip() or None if postcode else None,
                "latitude": lat,
                "longitude": lon,
            }
        return out

    def _parse(self, payload: Any) -> List[Dict[str, Any]]:
        """Price rows joined onto the register. Pure, so it can be tested."""
        priced: Dict[str, Dict[str, Any]] = {}
        for row in _listing(payload):
            site_id = row.get("SiteId")
            site = self._sites.get(str(site_id))
            if site is None:
                continue
            grade = GRADE_IDS.get(row.get("FuelId"))
            if grade is None:
                continue
            raw = row.get("Price")
            if raw is None or float(raw) >= UNAVAILABLE_PRICE:
                continue
            value = au_common.price_from_cents(raw, TENTHS_OF_A_CENT)
            if value is None:
                continue

            entry = priced.setdefault(str(site_id), {
                "site_id": str(site_id), **site, "last_updated": None})
            entry[grade] = value
            stamp = str(row.get("TransactionDateUtc") or "").strip()
            if stamp:
                entry["last_updated"] = max(entry.get("last_updated") or "", stamp)

        stations = list(priced.values())
        if not stations:
            self._last_error = "Fuel Prices QLD returned no usable prices"
        return stations
