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
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from modules.config_yaml import remove_keys, update_block

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
    "latitude": ("The home's position, decimal degrees — the one place it is kept.\n"
                 "Weather, sun and daylight, the floor-plan map, presence and\n"
                 "journeys all read it; set it in Settings or by lining up the\n"
                 "floor plan's map."),
    "longitude": "Decimal degrees.",
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
    Once `init` has run, the live home wins: it is what `set_home` last wrote.
    """
    if _home is not None:
        return _home
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


# The home's position, live — the one answer every feature reads. docs/location.md.

_home_lock = threading.Lock()
_home: Optional[Tuple[float, float]] = None
_listeners: List[Callable[[Optional[Tuple[float, float]]], None]] = []


def _valid(lat: Any, lon: Any) -> Optional[Tuple[float, float]]:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def home() -> Optional[Tuple[float, float]]:
    """Where the home is, (lat, lon), or None when it hasn't been set."""
    return _home


def init(config: Dict[str, Any], presence_homes: Optional[List[Tuple[Any, Any]]] = None,
         path: Path = CONFIG_PATH) -> Optional[Tuple[float, float]]:
    """
    Load the home at boot, and make `location:` its only copy.

    It used to be kept three times: `weather.latitude/longitude`, `location:`
    (which read the weather's as a fallback), and per presence user. Each drifted
    from the others. On the first boot after that, the first of them that is set
    is moved into `location:` (the weather's first, as it drove the most), and
    the weather block's copy is removed, so the file can't disagree with itself
    again. Presence users' own copies are ignored from now on.
    """
    global _home
    config = config or {}
    block = _block(config)
    found = _valid(block.get("latitude"), block.get("longitude"))
    weather = config.get("weather") or {}
    if found is None:
        found = _valid(weather.get("latitude"), weather.get("longitude"))
        if found is None:
            found = next((h for h in (_valid(*p) for p in presence_homes or []) if h), None)
        if found is not None:
            try:
                persist({"latitude": found[0], "longitude": found[1]}, path)
                config.setdefault("location", {}).update({"latitude": found[0], "longitude": found[1]})
                logger.info(f"Home location {found} moved into location: — the one copy from now on")
            except (OSError, ValueError) as e:
                logger.error(f"Could not move the home location into location: ({e}); using it from memory")
    if found is not None and ("latitude" in weather or "longitude" in weather):
        try:
            gone = remove_keys(path, "weather", ["latitude", "longitude"])
            for k in gone:
                weather.pop(k, None)
            if gone:
                logger.info("Removed weather.latitude/longitude — location: is the home's only copy")
        except OSError as e:
            logger.warning(f"Could not remove the weather block's old coordinates: {e}")
    with _home_lock:
        _home = found
    return found


def set_home(lat: Any, lon: Any, config: Optional[Dict[str, Any]] = None,
             path: Path = CONFIG_PATH) -> Tuple[float, float]:
    """
    Move the home. Written to `location:` (comments intact), the live config
    patched if given, and every listener told — the weather refetches for the
    new place and sun times recompute — so no restart is needed.
    Raises ValueError for coordinates that aren't a place on earth.
    """
    global _home
    found = _valid(lat, lon)
    if found is None:
        raise ValueError(f"not a position on earth: {lat!r}, {lon!r}")
    found = (round(found[0], 7), round(found[1], 7))
    persist({"latitude": found[0], "longitude": found[1]}, path)
    if config is not None:
        config.setdefault("location", {}).update({"latitude": found[0], "longitude": found[1]})
    with _home_lock:
        changed = _home != found
        _home = found
    if changed:
        for fn in list(_listeners):
            try:
                fn(found)
            except Exception as e:                        # noqa: BLE001
                logger.warning(f"home-location listener failed: {e}")
    return found


def on_change(fn: Callable[[Optional[Tuple[float, float]]], None]) -> None:
    """Call `fn(new_home)` whenever the home moves."""
    _listeners.append(fn)


def reset_home(value: Optional[Tuple[float, float]] = None) -> None:
    """Tests: set the in-memory home and forget listeners."""
    global _home
    with _home_lock:
        _home = value
    _listeners.clear()
