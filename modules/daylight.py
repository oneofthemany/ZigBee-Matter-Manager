"""
Outdoor daylight, estimated from the sun's position and the weather — the light
level a lux sensor would report, for a house that has none.

Pure functions, no I/O. The weather says how clear the sky is; the sun, computed
locally every call, says when. Model and constants: docs/daylight.md.
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

from .solar_gain import _beam_attenuation as cloud_attenuation
from .solar_gain import _cos_incidence, _diffuse_fraction

# Clear-sky horizontal illuminance [lux] against solar elevation [deg], sun plus
# sky, through twilight. Interpolated in log space. docs/daylight.md §2.
CLEAR_SKY_LUX = (
    (-18.0, 0.001), (-12.0, 0.01), (-9.0, 0.4), (-6.0, 3.4), (-4.0, 25.0),
    (-2.0, 150.0), (0.0, 600.0), (2.0, 1800.0), (5.0, 5000.0), (10.0, 11000.0),
    (20.0, 28000.0), (30.0, 48000.0), (45.0, 75000.0), (60.0, 95000.0),
    (90.0, 115000.0),
)

# Below this the measured-to-clear-sky ratio is dominated by the model's own
# error near the horizon, so cloud cover is used instead.
MEASURED_MIN_ELEVATION_DEG = 5.0
# A clearness reading older than this no longer describes the sky overhead.
MEASURED_MAX_AGE_S = 90 * 60
# Cloud cover this stale is still a better guess than a clear sky.
CLOUD_MAX_AGE_S = 3 * 3600

# Enter a level on falling below its threshold [lux]; leave it upward only at
# threshold × LEVEL_HYSTERESIS, so a passing cloud does not flicker the lights.
LEVELS = (("dark", 10.0), ("dusk", 400.0), ("dull", 5000.0))
LEVEL_ORDER = ("dark", "dusk", "dull", "bright")
LEVEL_HYSTERESIS = 1.5
DAYLIGHT_LEVELS = ("dull", "bright")

SOURCE_MEASURED = "measured"
SOURCE_CLOUD = "cloud"
SOURCE_CLEAR_SKY = "clear_sky"


def luminous_efficacy(cloud_fraction: Optional[float]) -> float:
    """Lumens per watt of daylight: ~105 under a clear sky, ~125 overcast."""
    cf = 0.0 if cloud_fraction is None else max(0.0, min(1.0, cloud_fraction))
    return 105.0 + 20.0 * cf


def clear_sky_lux(elevation_deg: float) -> float:
    """Clear-sky horizontal illuminance at this solar elevation."""
    table = CLEAR_SKY_LUX
    if elevation_deg <= table[0][0]:
        return 0.0
    if elevation_deg >= table[-1][0]:
        return table[-1][1]
    for (e0, l0), (e1, l1) in zip(table, table[1:]):
        if e0 <= elevation_deg <= e1:
            t = (elevation_deg - e0) / (e1 - e0)
            return math.exp(math.log(l0) + t * (math.log(l1) - math.log(l0)))
    return 0.0


def clearness(ghi_wm2: float, elevation_deg: float,
              cloud_fraction: Optional[float] = None) -> Optional[float]:
    """Measured light over clear-sky light, or None where the ratio means nothing."""
    if elevation_deg < MEASURED_MIN_ELEVATION_DEG or ghi_wm2 is None or ghi_wm2 < 0:
        return None
    cs = clear_sky_lux(elevation_deg)
    if cs <= 0:
        return None
    kt = ghi_wm2 * luminous_efficacy(cloud_fraction) / cs
    return max(0.03, min(1.3, kt))


def outdoor_lux(elevation_deg: Optional[float], *,
                ghi_wm2: Optional[float] = None,
                ghi_elevation_deg: Optional[float] = None,
                ghi_age_s: Optional[float] = None,
                cloud_fraction: Optional[float] = None,
                cloud_age_s: Optional[float] = None,
                ) -> Tuple[Optional[float], Optional[str]]:
    """
    Estimated outdoor illuminance and what it rests on.

    ``elevation_deg`` is the sun now; None means no location, and only a
    measured irradiance can answer. The ``ghi_*`` fields describe the last
    weather reading and the sun when it was taken. Returns (lux, source) or
    (None, None) when nothing is known.
    """
    if elevation_deg is None:
        if ghi_wm2 is None:
            return None, None
        return ghi_wm2 * luminous_efficacy(cloud_fraction), SOURCE_MEASURED

    sky = clear_sky_lux(elevation_deg)
    if (ghi_wm2 is not None and ghi_elevation_deg is not None
            and ghi_age_s is not None and 0 <= ghi_age_s <= MEASURED_MAX_AGE_S):
        kt = clearness(ghi_wm2, ghi_elevation_deg, cloud_fraction)
        if kt is not None:
            return sky * kt, SOURCE_MEASURED
    if cloud_fraction is not None and (cloud_age_s is None or cloud_age_s <= CLOUD_MAX_AGE_S):
        return sky * cloud_attenuation(cloud_fraction), SOURCE_CLOUD
    return sky, SOURCE_CLEAR_SKY


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def sky(ts: float, coords: Optional[Tuple[float, float]], current: Optional[dict],
        hourly_cloud: Optional[float] = None) -> Optional[dict]:
    """Outdoor light and the sun at ``ts`` (unix seconds).

    ``current`` is the weather service's cached current conditions (Open-Meteo
    keys plus ``fetched_at``); ``hourly_cloud`` the forecast cloud fraction for
    ``ts``'s hour, used where the current reading does not reach. Returns
    ``{lux, source, azimuth, elevation, cloud}``; the sun fields are None with
    no location. None when nothing is known.
    """
    from .sun_position import sun_position
    current = current or {}
    ghi = _num(current.get("shortwave_radiation"))
    cloud = _num(current.get("cloud_cover"))
    cloud = cloud / 100.0 if cloud is not None else None
    fetched = _num(current.get("fetched_at"))
    age = ts - fetched if fetched is not None else None
    if hourly_cloud is not None and (cloud is None or age is None or abs(age) > MEASURED_MAX_AGE_S):
        cloud, cloud_age = hourly_cloud, 0.0
    else:
        cloud_age = abs(age) if age is not None else None

    elevation = azimuth = ghi_elevation = None
    if coords:
        pos = sun_position(coords[0], coords[1], ts)
        elevation, azimuth = pos["elevation_deg"], pos["azimuth_deg"]
        if fetched is not None:
            ghi_elevation = sun_position(coords[0], coords[1], fetched)["elevation_deg"]
    lux, source = outdoor_lux(elevation, ghi_wm2=ghi, ghi_elevation_deg=ghi_elevation,
                              ghi_age_s=age, cloud_fraction=cloud, cloud_age_s=cloud_age)
    if lux is None:
        return None
    return {"lux": lux, "source": source, "azimuth": azimuth,
            "elevation": elevation, "cloud": cloud}


def light_level(lux: float, previous: Optional[str] = None) -> str:
    """The named band for ``lux``, held against ``previous`` with hysteresis."""
    raw = next((name for name, thr in LEVELS if lux < thr), "bright")
    if previous not in LEVEL_ORDER or \
            LEVEL_ORDER.index(raw) <= LEVEL_ORDER.index(previous):
        return raw
    thresholds = dict(LEVELS)
    level = previous
    while level != raw and lux >= thresholds[level] * LEVEL_HYSTERESIS:
        level = LEVEL_ORDER[LEVEL_ORDER.index(level) + 1]
    return level


def round_lux(lux: float) -> int:
    """Two significant figures: enough for a threshold, few enough to stay quiet."""
    if lux < 1:
        return 0
    digits = int(math.floor(math.log10(lux))) - 1
    return int(round(lux, -digits)) if digits > 0 else int(round(lux))


# per room — docs/daylight.md §7

#: Visible light transmittance by glazing, clean clear glass.
GLAZING_TRANSMITTANCE = {"single": 0.85, "double": 0.75, "triple": 0.65}
#: Sky angle seen from the window, degrees: 90 is an open horizon; 70 allows
#: for the neighbours, fences and trees most windows look at.
SKY_ANGLE_DEG = 70.0
#: Mean reflectance of the room's surfaces: pale walls, mid-tone floor.
SURFACE_REFLECTANCE = 0.5
#: Below this the sun is too low for its beam to be told apart from the sky.
BEAM_MIN_ELEVATION_DEG = 2.0


def split_outdoor(lux: float, elevation_deg: float,
                  cloud_fraction: Optional[float]) -> Tuple[float, float]:
    """(diffuse horizontal, beam normal) lux from the total horizontal estimate."""
    diffuse = lux * _diffuse_fraction(cloud_fraction or 0.0)
    if elevation_deg < BEAM_MIN_ELEVATION_DEG:
        return lux, 0.0
    beam_h = lux - diffuse
    return diffuse, beam_h / math.sin(math.radians(elevation_deg))


def room_lux(room: dict, outdoor: float, sun_azimuth_deg: float,
             sun_elevation_deg: float, cloud_fraction: Optional[float]
             ) -> Tuple[float, bool]:
    """Average daylight on a room's surfaces, and whether sun is coming in.

    ``room`` is one entry of ``floor_plan.daylight_geometry``. Sky light is the
    BRE average daylight factor; sunlight is the beam through each window,
    spread over the room the same way. Both divide by A·(1−R²).
    """
    diffuse, beam_n = split_outdoor(outdoor, sun_elevation_deg, cloud_fraction)
    spread = room["surface_m2"] * (1.0 - SURFACE_REFLECTANCE ** 2)
    if spread <= 0:
        return 0.0, False
    sky = beam = 0.0
    for w in room["windows"]:
        t = GLAZING_TRANSMITTANCE.get(w.get("glazing"), GLAZING_TRANSMITTANCE["double"])
        sky += t * w["area_m2"] * SKY_ANGLE_DEG
        if beam_n > 0:
            beam += beam_n * t * w["area_m2"] * _cos_incidence(
                sun_azimuth_deg, sun_elevation_deg, w["bearing_deg"])
    return diffuse * sky / 100.0 / spread + beam / spread, beam > 0
