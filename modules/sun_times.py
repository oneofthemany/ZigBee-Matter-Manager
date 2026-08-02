"""
Shared sunrise/sunset resolver — today's (or any date's) times in LOCAL clock
time, cached per date.

Location comes from the live WeatherService when registered, so sun conditions
track the same coordinates as external temperature, falling back to config.yaml.
The maths is local via modules.sun_position, with no network. Used by both the
automation engine and the NL parser.
"""

import datetime
import logging
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("modules.sun_times")

_cache: Dict[str, Dict[str, Any]] = {}

# Optional callable -> (lat, lon). Registered at startup so sun times share the
# exact location the weather API uses, instead of independently re-reading
# config.yaml (which can be a stale/template file or missing in a container CWD).
_location_provider: Optional[Callable[[], Optional[Tuple[Any, Any]]]] = None


def set_location_provider(provider: Optional[Callable[[], Optional[Tuple[Any, Any]]]]) -> None:
    """Register the authoritative location source (e.g. the WeatherService).

    The provider returns ``(lat, lon)`` (or ``None`` / ``(None, None)`` when it
    has no location yet). It takes priority over the config.yaml fallback. We
    drop the date cache so a freshly-supplied location takes effect immediately.
    """
    global _location_provider
    _location_provider = provider
    _cache.clear()


def _latlon_from_config() -> Tuple[Optional[float], Optional[float]]:
    try:
        import yaml
        with open("./config/config.yaml") as f:
            cfg = yaml.safe_load(f) or {}
        w = cfg.get("weather", {}) or {}
        lat, lon = w.get("latitude"), w.get("longitude")
        if lat is None or lon is None:
            return None, None
        return float(lat), float(lon)
    except Exception:
        return None, None


def _latlon() -> Tuple[Optional[float], Optional[float]]:
    # 1. Live weather service (authoritative — already-loaded, in-memory config).
    if _location_provider is not None:
        try:
            res = _location_provider()
            if res:
                lat, lon = res
                if lat is not None and lon is not None:
                    return float(lat), float(lon)
        except Exception as e:
            logger.debug(f"location provider failed, falling back to config: {e}")
    # 2. Fallback: read config.yaml directly (standalone use / tests / pre-wire).
    return _latlon_from_config()


def _utc_iso_to_local_time(iso: Optional[str]) -> Optional[datetime.time]:
    if not iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone().time()
    except Exception:
        return None


def sun_times(d: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Return {available, sunrise, sunset (datetime.time|None),
    sunrise_hhmm, sunset_hhmm (str|None), polar}."""
    d = d or datetime.date.today()
    key = d.isoformat()
    cached = _cache.get(key)
    if cached is not None:
        return cached

    res = {"available": False, "sunrise": None, "sunset": None,
           "sunrise_hhmm": None, "sunset_hhmm": None, "polar": None}
    lat, lon = _latlon()
    if lat is not None and lon is not None:
        try:
            from modules.sun_position import sunrise_sunset
            rs = sunrise_sunset(lat, lon, d)
            sr = _utc_iso_to_local_time(rs.get("sunrise"))
            ss = _utc_iso_to_local_time(rs.get("sunset"))
            res = {
                "available": True,
                "sunrise": sr, "sunset": ss,
                "sunrise_hhmm": sr.strftime("%H:%M") if sr else None,
                "sunset_hhmm": ss.strftime("%H:%M") if ss else None,
                "polar": rs.get("polar"),
            }
        except Exception as e:
            logger.debug(f"sun computation failed: {e}")

    # Only cache successful resolutions. An "unavailable" result usually means
    # the location wasn't wired up yet at first call — don't pin that for the
    # rest of the day; re-check until the weather service supplies coordinates.
    if res["available"]:
        if len(_cache) > 8:   # keep only a handful of recent dates
            _cache.clear()
        _cache[key] = res
    return res
