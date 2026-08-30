"""
Where this hub is, as one answer rather than several.

The house already had coordinates, but only inside `weather:`, which is the
wrong owner: fuel prices need a country, and a second latitude in a second block
is how two settings start disagreeing. So `location:` is the hub's own place,
and `weather.latitude` / `weather.longitude` remain the fallback so an existing
install keeps working without being edited.

The country is what selects a fuel provider — see modules/fuel/registry.py.
It can be detected by reverse-geocoding the coordinates, but only ever as a
suggestion the user confirms: a hub near a border would otherwise silently
change country, and a wrong guess means wrong prices in a wrong currency.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from modules.config_yaml import update_block

logger = logging.getLogger("modules.location")

CONFIG_PATH = Path("./config/config.yaml")

_CC_RE = re.compile(r"^[A-Za-z]{2}$")
#: Subdivisions are ISO-3166-2 suffixes: "NSW", "QLD", "WA", "CA".
_SUB_RE = re.compile(r"^[A-Za-z0-9]{1,3}$")

_BLOCK_COMMENT = "Where this hub is. Used to pick region-specific data sources."
_KEY_COMMENTS = {
    "country": ("ISO-3166 alpha-2, e.g. GB, DE, AU. Blank asks the Settings\n"
                "page to suggest one from the coordinates below."),
    "subdivision": ("State or province, for countries whose data is published\n"
                    "per state rather than nationally — AU is the one that\n"
                    "matters: NSW, QLD and WA each run their own fuel feed."),
    "latitude": "Decimal degrees. Falls back to weather.latitude when blank.",
    "longitude": "Decimal degrees. Falls back to weather.longitude when blank.",
}


def _block(config: Dict[str, Any]) -> Dict[str, Any]:
    return (config or {}).get("location") or {}


def country(config: Dict[str, Any]) -> str:
    """The configured country, uppercased. "" when unset or malformed."""
    cc = str(_block(config).get("country") or "").strip()
    return cc.upper() if _CC_RE.match(cc) else ""


def subdivision(config: Dict[str, Any]) -> str:
    """The configured state/province, uppercased. "" when unset."""
    sub = str(_block(config).get("subdivision") or "").strip()
    return sub.upper() if _SUB_RE.match(sub) else ""


def home_coords(config: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """
    The hub's coordinates, from `location:` or failing that `weather:`.

    None when neither is set — which is normal on a fresh install, and callers
    have to cope rather than assume a default that would silently be wrong.
    """
    for block in (_block(config), (config or {}).get("weather") or {}):
        lat, lon = block.get("latitude"), block.get("longitude")
        if lat is None or lon is None or lat == "" or lon == "":
            continue
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    return None


async def detect_country(config: Dict[str, Any]) -> Optional[str]:
    """
    Reverse-geocode the hub's coordinates to a country code.

    A suggestion, never applied on its own. Returns None when there are no
    coordinates, no geocoder, or no answer — all of which mean "ask the user"
    rather than "assume".
    """
    coords = home_coords(config)
    if coords is None:
        return None
    try:
        from modules.geocode import get_geocoder
        geocoder = get_geocoder()
        if geocoder is None:
            return None
        return await geocoder.reverse_country(*coords)
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"country detection failed: {e}")
        return None


def persist(values: Dict[str, Any], path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """
    Write the given `location:` keys back to config.yaml, comments intact.

    Only the keys passed are touched, and each is validated here rather than at
    the route: a bad country code written to the file would come back on every
    boot, so it is rejected once, at the point of writing.
    """
    clean: Dict[str, Any] = {}

    if "country" in values:
        cc = str(values.get("country") or "").strip().upper()
        if cc and not _CC_RE.match(cc):
            raise ValueError(f"country must be an ISO-3166 alpha-2 code, got {cc!r}")
        clean["country"] = cc

    if "subdivision" in values:
        sub = str(values.get("subdivision") or "").strip().upper()
        if sub and not _SUB_RE.match(sub):
            raise ValueError(f"subdivision must be 1-3 alphanumerics, got {sub!r}")
        clean["subdivision"] = sub

    for key, lo, hi in (("latitude", -90.0, 90.0), ("longitude", -180.0, 180.0)):
        if key not in values:
            continue
        raw = values.get(key)
        if raw is None or raw == "":
            clean[key] = ""
            continue
        try:
            num = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number, got {raw!r}")
        if not lo <= num <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}, got {num}")
        clean[key] = num

    if clean:
        update_block(path, "location", clean,
                     block_comment=_BLOCK_COMMENT, comments=_KEY_COMMENTS)
    return clean
