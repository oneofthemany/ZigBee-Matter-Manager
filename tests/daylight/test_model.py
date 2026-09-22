"""
Daylight model tests.

    python3 tests/daylight/test_model.py

The claims that let a house do without a lux sensor: the estimate follows dusk
minute by minute, cloud brings the dark forward, a weather reading sets how
clear the sky is only while it is fresh, and a light level hovering at a
threshold does not flicker between bands.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker  # noqa: E402

from modules import daylight as dl  # noqa: E402
from modules.sun_position import sun_position, sunrise_sunset  # noqa: E402

LONDON = (51.5074, -0.1278)


def _elev(t: datetime) -> float:
    return sun_position(*LONDON, t)["elevation_deg"]


def _evening(day: datetime, cloud=None):
    """(time, lux, level) each minute from 2 h before sunset to 1 h after."""
    sunset = datetime.fromisoformat(sunrise_sunset(*LONDON, day)["sunset"])
    out, level = [], None
    t = sunset - timedelta(hours=2)
    while t <= sunset + timedelta(hours=1):
        lux, _ = dl.outdoor_lux(_elev(t), cloud_fraction=cloud)
        level = dl.light_level(lux, level)
        out.append((t, lux, level))
        t += timedelta(minutes=1)
    return sunset, out


def _first_dark(series):
    return next(t for t, _, level in series if level not in dl.DAYLIGHT_LEVELS)


def run() -> Checker:
    c = Checker("test_model")

    c.section("the clear-sky curve")
    c.check("nothing after astronomical dusk", dl.clear_sky_lux(-20) == 0.0)
    c.check("a sunset on a clear day is a few hundred lux",
            200 < dl.clear_sky_lux(-0.8) < 800, dl.clear_sky_lux(-0.8))
    c.check("civil dusk is a few lux", 1 < dl.clear_sky_lux(-6) < 10)
    c.check("the midsummer sun is ~100 klux", dl.clear_sky_lux(62) > 90000)
    elevs = [e / 4 for e in range(-71, 360)]
    c.check("rises with the sun, from astronomical dusk up",
            all(dl.clear_sky_lux(a) < dl.clear_sky_lux(b) for a, b in zip(elevs, elevs[1:])))

    c.section("it follows dusk minute by minute")
    for label, day in (("December", datetime(2026, 12, 21, tzinfo=timezone.utc)),
                       ("June", datetime(2026, 6, 21, tzinfo=timezone.utc))):
        sunset, series = _evening(day)
        luxes = [lux for _, lux, _ in series]
        c.check(f"{label}: falls every minute", all(a > b for a, b in zip(luxes, luxes[1:])))
        gap = (_first_dark(series) - sunset).total_seconds() / 60
        c.check(f"{label}: a clear sky goes dark within 10 min of sunset",
                abs(gap) <= 10, f"{gap:.1f} min")

    c.section("cloud brings the dark forward")
    day = datetime(2026, 12, 21, tzinfo=timezone.utc)
    _, clear = _evening(day)
    _, overcast = _evening(day, cloud=1.0)
    lead = (_first_dark(clear) - _first_dark(overcast)).total_seconds() / 60
    c.check("overcast goes dark at least 5 min earlier", lead >= 5, f"{lead:.1f} min")
    _, light_cloud = _evening(day, cloud=0.2)
    light_lead = (_first_dark(clear) - _first_dark(light_cloud)).total_seconds() / 60
    c.check("light cloud barely moves it", light_lead <= 2, f"{light_lead:.1f} min")

    c.section("a weather reading sets how clear the sky is, while fresh")
    noon = datetime(2026, 12, 21, 12, 0, tzinfo=timezone.utc)
    later = noon + timedelta(minutes=60)
    ghi = 40.0                                  # a leaden winter noon
    lux, source = dl.outdoor_lux(_elev(later), ghi_wm2=ghi, ghi_elevation_deg=_elev(noon),
                                 ghi_age_s=3600, cloud_fraction=1.0, cloud_age_s=3600)
    kt = ghi * dl.luminous_efficacy(1.0) / dl.clear_sky_lux(_elev(noon))
    c.check("the measured clearness is used", source == dl.SOURCE_MEASURED, source)
    c.check("and carried forward to where the sun is now",
            abs(lux - dl.clear_sky_lux(_elev(later)) * kt) < 1, (lux, kt))
    c.check("a dull day reads dull at noon", dl.light_level(lux) == "dull", lux)
    _, stale = dl.outdoor_lux(_elev(later), ghi_wm2=ghi, ghi_elevation_deg=_elev(noon),
                              ghi_age_s=dl.MEASURED_MAX_AGE_S + 60,
                              cloud_fraction=1.0, cloud_age_s=dl.MEASURED_MAX_AGE_S + 60)
    c.check("a stale reading falls back to cloud cover", stale == dl.SOURCE_CLOUD, stale)
    _, low = dl.outdoor_lux(0.0, ghi_wm2=5.0, ghi_elevation_deg=1.0, ghi_age_s=60,
                            cloud_fraction=0.5, cloud_age_s=60)
    c.check("a reading with the sun on the horizon is not trusted", low == dl.SOURCE_CLOUD, low)
    _, none = dl.outdoor_lux(10.0)
    c.check("no weather at all is the clear-sky model", none == dl.SOURCE_CLEAR_SKY, none)
    lux, source = dl.outdoor_lux(None, ghi_wm2=100.0, cloud_fraction=0.0)
    c.check("no location: the irradiance alone answers",
            source == dl.SOURCE_MEASURED and lux == 100.0 * dl.luminous_efficacy(0.0), (lux, source))
    c.check("no location and no irradiance: nothing is known",
            dl.outdoor_lux(None) == (None, None))

    c.section("a level hovering at a threshold does not flicker")
    series = [400 * (1 + 0.1 * (-1) ** i) for i in range(40)]      # 360, 440, 360, ...
    levels, level = [], "dull"
    for lux in series:
        level = dl.light_level(lux, level)
        levels.append(level)
    changes = sum(1 for a, b in zip(levels, levels[1:]) if a != b)
    c.check("one change, not one per sample", changes == 1, levels[:6])
    c.check("it leaves dusk only once clearly brighter",
            dl.light_level(590, "dusk") == "dusk" and dl.light_level(610, "dusk") == "dull")
    c.check("going darker is immediate", dl.light_level(9, "bright") == "dark")
    c.check("a jump from night to day passes every band", dl.light_level(50000, "dark") == "bright")
    c.check("without history it is the plain band", dl.light_level(399) == "dusk")

    c.section("each room gets the light its windows let in")
    def room(bearing, area=1.68, glazing="double"):
        return {"surface_m2": 83.2, "windows": [{"area_m2": area, "glazing": glazing,
                                                 "bearing_deg": bearing}]}

    def lux_in(r, t, cloud=0.0):
        pos = sun_position(*LONDON, t)
        out, _ = dl.outdoor_lux(pos["elevation_deg"], cloud_fraction=cloud)
        return dl.room_lux(r, out, pos["azimuth_deg"], pos["elevation_deg"], cloud)

    noon = datetime(2026, 12, 21, 12, 0, tzinfo=timezone.utc)
    south, south_sun = lux_in(room(180), noon)
    north, north_sun = lux_in(room(0), noon)
    c.check("a south room is sunlit at a winter noon", south_sun and not north_sun)
    c.check("and far brighter than a north one", south > 10 * north, (south, north))
    c.check("a north room still has sky light", 10 < north < 200, north)
    morning = datetime(2026, 6, 21, 7, 0, tzinfo=timezone.utc)      # 08:00 BST
    evening = datetime(2026, 6, 21, 16, 0, tzinfo=timezone.utc)     # 17:00 BST
    c.check("an east room is brighter than a west one in the morning",
            lux_in(room(90), morning)[0] > lux_in(room(270), morning)[0])
    c.check("and darker in the evening",
            lux_in(room(90), evening)[0] < lux_in(room(270), evening)[0])
    grey, grey_sun = lux_in(room(180), noon, cloud=1.0)
    c.check("under full cloud no sun comes in", not grey_sun and grey < south / 5, (grey, south))
    c.check("more glass, more light", lux_in(room(0, area=3.36), noon)[0] > 1.9 * north)
    c.check("triple glazing lets in less than single",
            lux_in(room(0, glazing="triple"), noon)[0] < lux_in(room(0, glazing="single"), noon)[0])
    c.check("night is dark indoors", lux_in(room(180), datetime(2026, 12, 21, 22, tzinfo=timezone.utc))[0] < 0.01)

    c.section("the sky at any time today")
    t = noon.timestamp()
    cur = {"shortwave_radiation": 30.0, "cloud_cover": 100, "fetched_at": t - 600}
    c.check("near now a fresh reading is used", dl.sky(t, LONDON, cur)["source"] == dl.SOURCE_MEASURED)
    later = dl.sky(t + 5 * 3600, LONDON, cur, hourly_cloud=0.0)
    c.check("hours away the forecast's cloud is used", later["source"] == dl.SOURCE_CLOUD
            and later["cloud"] == 0.0, later)
    c.check("with no location only a measurement can answer",
            dl.sky(t, None, cur)["elevation"] is None and dl.sky(t, None, {}) is None)

    c.section("the sky's parts, for spreading light across a room")
    clear = dl.sky_parts(dl.sky(t, LONDON, {}, hourly_cloud=0.0))
    c.check("a clear noon is mostly beam", clear["beam_n"] > clear["diffuse"], clear)
    grey = dl.sky_parts(dl.sky(t, LONDON, {}, hourly_cloud=1.0))
    c.check("overcast is all sky", grey["beam_n"] < grey["diffuse"] / 10, grey)
    total = dl.sky(t, LONDON, {}, hourly_cloud=0.0)["lux"]
    c.check("beam and sky add back up to the horizontal total",
            abs(clear["diffuse"] + clear["beam_n"] * math.sin(math.radians(clear["elevation"]))
                - total) < total * 0.002, clear)
    night = datetime(2026, 12, 21, 22, tzinfo=timezone.utc).timestamp()
    c.check("nothing at night", dl.sky_parts(dl.sky(night, LONDON, {})) is None)

    c.section("rounding keeps the reading quiet")
    c.check("two significant figures", dl.round_lux(12345) == 12000 and dl.round_lux(456) == 460)
    c.check("small values stay whole", dl.round_lux(37.4) == 37 and dl.round_lux(0.4) == 0)
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
