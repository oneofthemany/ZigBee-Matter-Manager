"""
Germany — Tankerkönig, over the Bundeskartellamt's MTS-K data.

Every station above 70,000 litres a month must report a price change within
minutes, and Tankerkönig republishes that. Unlike the other feeds in this
package there is no national download: `list.php` is a radius search, capped at
25 km, and the service asks for no more than one request per minute. So this is
the [RadiusQueryProvider] the base class exists for — the answer is cached per
coordinate bucket and calls are serialised behind the limiter.

A free API key is required. It goes in config/secrets.yaml or the environment,
never in config.yaml, for the same reason the Fuel Finder credentials do not:
that file is tracked in git.

Attribution is a licence condition, not a courtesy — the data is CC BY 4.0.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import aiohttp

from modules.fuel.base import RadiusQueryProvider

logger = logging.getLogger("modules.fuel.providers.de_tankerkoenig")

LIST_URL = "https://creativecommons.tankerkoenig.de/json/list.php"

ENV_API_KEY = "ZMM_TANKERKOENIG_API_KEY"
SECRETS_FILE = os.environ.get("ZMM_SECRETS_FILE", "./config/secrets.yaml")

#: Tankerkönig's field -> the code this project exposes.
GRADE_FIELDS = {"e5": "E5", "e10": "E10", "diesel": "DIESEL"}

FUEL_TYPES = {
    "E5": "Super E5",
    "E10": "Super E10",
    "DIESEL": "Diesel",
}

#: The service's own documented ceiling. Asking for more is rejected upstream
#: rather than silently truncated, so it is clamped here.
MAX_RADIUS_KM = 25.0

#: "Maximal eine Anfrage pro Minute." Kept slightly above 60 s so clock jitter
#: cannot turn a compliant pace into a violation.
MIN_INTERVAL_S = 61.0

#: With a one-per-minute budget, a short cache would spend the whole allowance
#: on one impatient user. Ten minutes still tracks a feed that changes several
#: times a day.
CACHE_TTL_S = 600.0


def resolve_api_key(config: Dict[str, Any]) -> str:
    """
    The API key, from the environment or the gitignored secrets file.

    Same order and same reasoning as the Fuel Finder credentials: the container
    gets an environment variable, a bare install gets a file it can edit, and
    neither is config.yaml. A key left in config.yaml is still used — refusing
    would just make the app look broken — but it is loud about it, because by
    then the key is already staged for the next commit.
    """
    key = os.environ.get(ENV_API_KEY, "").strip()
    if key:
        return key

    try:
        import yaml
        with open(SECRETS_FILE, "r") as fh:
            secrets = (yaml.safe_load(fh) or {}).get("tankerkoenig") or {}
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
            "Tankerkoenig api_key found in config.yaml, which is TRACKED IN "
            "GIT. Move it to %s or the %s environment variable, then request a "
            "new one — assume the one in the file is burnt.",
            SECRETS_FILE, ENV_API_KEY,
        )
    return in_config


class GermanyTankerkoenig(RadiusQueryProvider):
    """Stations near a point, straight from Tankerkönig."""

    region = "DE"
    label = "Germany (Tankerkönig)"
    grades = FUEL_TYPES
    default_grade = "E10"
    currency = "EUR"
    currency_symbol = "€"
    volume_unit = "L"
    distance_unit = "km"
    display_scale = "major"
    display_decimals = 3
    needs_credentials = True
    attribution = ("Tankerkönig / MTS-K, CC BY 4.0 — "
                   "https://creativecommons.tankerkoenig.de")

    max_radius_km = MAX_RADIUS_KM
    min_interval_s = MIN_INTERVAL_S
    cache_ttl_s = CACHE_TTL_S

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.api_key = resolve_api_key(cfg)
        self.base_url = str(cfg.get("base_url") or LIST_URL).strip()
        # Closed stations are still listed, with their last price. Included by
        # default: a driver planning a stop wants to know what the place along
        # the route charges, not only what is open at this second.
        self.only_open = bool(cfg.get("only_open", False))

    @property
    def source(self) -> str:
        return "de_tankerkoenig"

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.api_key)

    async def _fetch_nearby(
        self, lat: float, lon: float, radius_km: float
    ) -> List[Dict[str, Any]]:
        if not self.configured:
            self._last_error = ("Tankerkoenig API key not set — put it in "
                                "config/secrets.yaml under tankerkoenig.api_key")
            return []

        params = {
            "lat": f"{float(lat):.6f}",
            "lng": f"{float(lon):.6f}",
            "rad": f"{min(float(radius_km), MAX_RADIUS_KM):.1f}",
            "sort": "dist",
            "type": "all",
            "apikey": self.api_key,
        }
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(self.base_url, params=params) as resp:
                if resp.status != 200:
                    self._last_error = f"Tankerkoenig returned HTTP {resp.status}"
                    return []
                payload = await resp.json(content_type=None)

        return self._parse(payload)

    def _parse(self, payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """API reply -> station dicts. Pure, so it can be tested."""
        if not (payload or {}).get("ok"):
            # The service reports a bad key as HTTP 200 with ok=false, so the
            # status code alone would read this as success and cache an empty
            # answer for ten minutes.
            self._last_error = (
                (payload or {}).get("message") or "Tankerkoenig reported an error")
            return []

        stations: List[Dict[str, Any]] = []
        for row in payload.get("stations") or []:
            site_id = str(row.get("id") or "").strip()
            lat_v, lon_v = row.get("lat"), row.get("lng")
            if not site_id or lat_v is None or lon_v is None:
                continue
            if self.only_open and row.get("isOpen") is False:
                continue

            prices = {}
            for field, code in GRADE_FIELDS.items():
                value = row.get(field)
                if isinstance(value, (int, float)) and value > 0:
                    prices[code] = float(value)
            if not prices:
                continue

            street = (row.get("street") or "").strip()
            number = str(row.get("houseNumber") or "").strip()
            stations.append({
                "site_id": site_id,
                "brand": (row.get("brand") or "").strip() or (
                    row.get("name") or "").strip() or None,
                "address": " ".join(p for p in (street, number) if p) or None,
                "town": (row.get("place") or "").strip() or None,
                # postCode arrives as an integer, which drops the leading zero
                # every postcode in Saxony and Thuringia starts with.
                "postcode": _postcode(row.get("postCode")),
                "latitude": float(lat_v),
                "longitude": float(lon_v),
                # The API already returns kilometres from the query point, so
                # the base class's haversine would only re-derive it less
                # accurately than the service that indexed the data.
                "dist": round(float(row.get("dist") or 0.0), 2),
                # MTS-K carries no per-station timestamp on this endpoint; the
                # regulation caps staleness at minutes, and history falls back
                # to the day it saw the value.
                "last_updated": None,
                **prices,
            })
        return stations


def _postcode(raw: Any) -> Optional[str]:
    """German postcodes are five digits, and 01067 is not 1067."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return text.zfill(5) if text.isdigit() else text
