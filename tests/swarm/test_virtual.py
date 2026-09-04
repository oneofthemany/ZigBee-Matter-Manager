"""
Virtual device tests — the house-scope inputs that are not devices.

    python3 tests/swarm/test_virtual.py

The computed flags are what matter here. The rule engine compares an attribute
to a literal, never to another live reading, so "cooler outside than in" and
"due home within the warm-up time" have to be decided in this module and
published as plain values. These checks pin that arithmetic down.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker  # noqa: E402

from modules.swarm.resolver import describe_device  # noqa: E402
from modules.swarm.virtual import (  # noqa: E402
    HOUSE_IEEE, TARIFF_IEEE, WEATHER_IEEE, VirtualDeviceProvider,
    _eta_minutes, VirtualDevice,
)


class FakeWeather:
    def __init__(self, **cur):
        self._cur = cur

    def get_current(self):
        return dict(self._cur)


class FakeAdvisor:
    def __init__(self, indoor=None, outdoor=None, preheat=None, raise_on=None):
        self._indoor, self._outdoor, self._preheat = indoor, outdoor, preheat
        self._raise_on = raise_on

    def _get_avg_indoor_temp(self):
        if self._raise_on == "indoor":
            raise RuntimeError("sensor bus down")
        return self._indoor

    def get_preheat_recommendation(self):
        if self._raise_on == "preheat":
            raise RuntimeError("model unavailable")
        if self._preheat is None:
            return {"error": "Insufficient sensor data"}
        return {"preheat_minutes": self._preheat, "current_outdoor": self._outdoor}


class FakeTariff:
    def __init__(self, rate=None, off_peak=None):
        self._rate, self._off_peak = rate, off_peak

    def get_status(self):
        return {"current_rate_p": self._rate, "is_off_peak": self._off_peak}


class FakePresence:
    def __init__(self, people):
        # people: [(presence, distance_m)]
        self.devices = {
            f"user::{i}": type("D", (), {"state": {"presence": p, "distance_m": d}})()
            for i, (p, d) in enumerate(people)
        }


def _provider(**kw):
    return VirtualDeviceProvider(
        weather_getter=(lambda: kw.get("weather")) if "weather" in kw else None,
        advisor_getter=(lambda: kw.get("advisor")) if "advisor" in kw else None,
        tariff_getter=(lambda: kw.get("tariff")) if "tariff" in kw else None,
        presence_getter=(lambda: kw.get("presence")) if "presence" in kw else None,
    )


def run() -> Checker:
    c = Checker("test_virtual")

    c.section("weather is read from the service's cached current conditions")
    p = _provider(weather=FakeWeather(temperature_2m=4.5, relative_humidity_2m=80,
                                      wind_speed_10m=12.0, shortwave_radiation=0.0))
    asyncio.run(p.refresh())
    w = p.devices[WEATHER_IEEE].state
    c.check("temperature published", w["temperature"] == 4.5, w)
    c.check("solar published", w["solar_wm2"] == 0.0, w)
    c.check("darkness derived from irradiance", w["is_daylight"] == 0, w)

    p = _provider(weather=FakeWeather(temperature_2m=18.0, shortwave_radiation=350.0))
    asyncio.run(p.refresh())
    c.check("daylight derived from irradiance",
            p.devices[WEATHER_IEEE].state["is_daylight"] == 1)

    c.section("a missing reading is dropped, never published as zero")
    p = _provider(weather=FakeWeather(temperature_2m=None, shortwave_radiation=None))
    asyncio.run(p.refresh())
    w = p.devices[WEATHER_IEEE].state
    c.check("no temperature key", "temperature" not in w, w)
    c.check("no is_daylight key", "is_daylight" not in w, w)
    c.check("the device still exists", p.devices[WEATHER_IEEE].is_available())

    c.section("cooler-outside is computed, not compared by the engine")
    p = _provider(weather=FakeWeather(temperature_2m=17.0),
                  advisor=FakeAdvisor(indoor=26.0, preheat=30))
    asyncio.run(p.refresh())
    h = p.devices[HOUSE_IEEE].state
    c.check("flag set when it is cooler out", h["outdoor_cooler_than_indoor"] == 1, h)
    c.check("both readings published", h["indoor_avg_temp"] == 26.0
            and h["outdoor_temp"] == 17.0, h)

    p = _provider(weather=FakeWeather(temperature_2m=28.0),
                  advisor=FakeAdvisor(indoor=22.0, preheat=30))
    asyncio.run(p.refresh())
    c.check("clear when it is warmer out",
            p.devices[HOUSE_IEEE].state["outdoor_cooler_than_indoor"] == 0)

    # A degree either way is noise, not an opportunity to open a window.
    p = _provider(weather=FakeWeather(temperature_2m=21.5),
                  advisor=FakeAdvisor(indoor=22.0, preheat=30))
    asyncio.run(p.refresh())
    c.check("a near-identical reading does not count as cooler",
            p.devices[HOUSE_IEEE].state["outdoor_cooler_than_indoor"] == 0)

    c.section("travel time")
    c.check("nothing from no distance", _eta_minutes(None) is None)
    c.check("nothing from a negative distance", _eta_minutes(-5) is None)
    c.check("home is zero minutes", _eta_minutes(0) == 0)
    c.check("about 15 minutes from 10 km",
            10 < _eta_minutes(10_000) < 20, _eta_minutes(10_000))
    c.check("a continent away is capped", _eta_minutes(10_000_000) == 240)

    c.section("preheat-for-arrival, the whole pre-arrival example in one flag")
    # 20 km out is ~30 minutes; a 45-minute warm-up means start now.
    p = _provider(weather=FakeWeather(temperature_2m=2.0),
                  advisor=FakeAdvisor(indoor=15.0, preheat=45),
                  presence=FakePresence([("away", 20_000)]))
    asyncio.run(p.refresh())
    c.check("set when the journey is shorter than the warm-up",
            p.devices[HOUSE_IEEE].state["preheat_now_for_arrival"] == 1,
            p.devices[HOUSE_IEEE].state)

    # Same journey, but the house warms in 5 minutes: no need yet.
    p = _provider(weather=FakeWeather(temperature_2m=2.0),
                  advisor=FakeAdvisor(indoor=20.0, preheat=5),
                  presence=FakePresence([("away", 20_000)]))
    asyncio.run(p.refresh())
    c.check("clear when there is still time",
            p.devices[HOUSE_IEEE].state["preheat_now_for_arrival"] == 0)

    p = _provider(weather=FakeWeather(temperature_2m=2.0),
                  advisor=FakeAdvisor(indoor=15.0, preheat=45),
                  presence=FakePresence([("home", 0)]))
    asyncio.run(p.refresh())
    c.check("somebody already home is not a pending arrival",
            p.devices[HOUSE_IEEE].state["preheat_now_for_arrival"] == 0)

    p = _provider(weather=FakeWeather(temperature_2m=2.0),
                  advisor=FakeAdvisor(indoor=15.0, preheat=45),
                  presence=FakePresence([("away", 60_000), ("away", 8_000)]))
    asyncio.run(p.refresh())
    c.check("the soonest arrival decides it",
            p.devices[HOUSE_IEEE].state["preheat_now_for_arrival"] == 1)

    c.section("tariff")
    p = _provider(tariff=FakeTariff(rate=7.5, off_peak=True))
    asyncio.run(p.refresh())
    t = p.devices[TARIFF_IEEE].state
    c.check("rate published", t["unit_rate"] == 7.5, t)
    c.check("off-peak is an int, matching the engine's operators",
            t["is_off_peak"] == 1, t)
    p = _provider(tariff=FakeTariff(rate=32.0, off_peak=False))
    asyncio.run(p.refresh())
    c.check("peak is zero, not absent",
            p.devices[TARIFF_IEEE].state["is_off_peak"] == 0)

    c.section("a service that is absent or throwing does not break the rest")
    p = _provider()
    asyncio.run(p.refresh())
    c.check("no services, no crash", all(
        set(d.state) == {"available"} for d in p.devices.values()))

    p = _provider(weather=FakeWeather(temperature_2m=9.0),
                  advisor=FakeAdvisor(indoor=20.0, preheat=10, raise_on="preheat"))
    asyncio.run(p.refresh())
    c.check("weather survives a throwing advisor",
            p.devices[WEATHER_IEEE].state["temperature"] == 9.0)
    c.check("the indoor reading still lands",
            p.devices[HOUSE_IEEE].state["indoor_avg_temp"] == 20.0)
    c.check("the flag needing the failed call is withheld",
            "preheat_now_for_arrival" not in p.devices[HOUSE_IEEE].state,
            p.devices[HOUSE_IEEE].state)

    c.section("only changes are reported, so rules do not re-fire on every tick")
    p = _provider(weather=FakeWeather(temperature_2m=11.0))
    first = asyncio.run(p.refresh())
    second = asyncio.run(p.refresh())
    c.check("the first pass reports the values", WEATHER_IEEE in first)
    c.check("an unchanged pass reports nothing", second == {}, second)

    c.section("the engine is told what changed")
    seen = []

    async def _evaluator(ieee, changed):
        seen.append((ieee, changed))

    p = _provider(weather=FakeWeather(temperature_2m=3.0))
    p._evaluator = _evaluator
    asyncio.run(p.refresh())
    c.check("the evaluator was called", len(seen) == 1, seen)
    c.check("with the device and its changes",
            seen[0][0] == WEATHER_IEEE and seen[0][1]["temperature"] == 3.0, seen)

    c.section("a throwing evaluator does not stop the refresh")
    async def _bad(ieee, changed):
        raise RuntimeError("engine busy")

    p = _provider(weather=FakeWeather(temperature_2m=6.0))
    p._evaluator = _bad
    asyncio.run(p.refresh())
    c.check("state still updated", p.devices[WEATHER_IEEE].state["temperature"] == 6.0)

    c.section("virtual devices resolve as swarm offers like anything else")
    p = _provider(weather=FakeWeather(temperature_2m=2.0),
                  advisor=FakeAdvisor(indoor=15.0, preheat=45),
                  presence=FakePresence([("away", 20_000)]))
    asyncio.run(p.refresh())
    house = describe_device(HOUSE_IEEE, p.devices[HOUSE_IEEE], "House")
    keys = {t["key"] for t in house["triggers"]}
    c.check("the preheat trigger is offered", "house:preheat_due" in keys, keys)
    c.check("house scope", house["scope"] == "house", house["scope"])
    c.check("classified as the house", house["device_class"] == "house")
    c.check("nothing can be commanded", house["actions"] == [])
    preheat = next(t for t in house["triggers"] if t["key"] == "house:preheat_due")
    c.check("it compiles against the computed flag",
            preheat["condition"] == {"type": "attribute",
                                     "attribute": "preheat_now_for_arrival",
                                     "operator": "eq", "value": 1}, preheat)

    weather = describe_device(WEATHER_IEEE, p.devices[WEATHER_IEEE], "Weather")
    c.check("weather offers a cold trigger",
            any(t["key"] == "weather:got_cold_out" for t in weather["triggers"]),
            [t["key"] for t in weather["triggers"]])

    c.section("an empty virtual device offers nothing rather than nonsense")
    bare = describe_device(TARIFF_IEEE,
                           VirtualDevice(TARIFF_IEEE, "Tariff", "x", ["tariff"]),
                           "Tariff")
    c.check("no offers without readings",
            not bare["triggers"] and not bare["conditions"],
            bare["triggers"] + bare["conditions"])

    return c


if __name__ == "__main__":
    checker = run()
    print(f"\n{checker.passed} passed, {len(checker.failures)} failed")
    sys.exit(1 if checker.failures else 0)
