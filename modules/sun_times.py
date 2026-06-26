"""
Shared Sunrise/Sunset Resolver
==============================
Resolves today's (or any date's) sunrise/sunset to LOCAL clock time, cached per
date. Location comes from config.yaml weather.{latitude,longitude}; the maths is
done locally via modules.sun_position (no network).

Used by both the automation engine (dynamic "sun" conditions that re-resolve
every evaluation, so rules track the seasons) and the NL parser.
"""

import datetime
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("modules.sun_times")

_cache: Dict[str, Dict[str, Any]] = {}


def _latlon() -> Tuple[Optional[float], Optional[float]]:
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

    if len(_cache) > 8:   # keep only a handful of recent dates
        _cache.clear()
    _cache[key] = res
    return res
