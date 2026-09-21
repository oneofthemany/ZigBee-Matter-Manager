"""
Daylight lighting tests — lights at dusk with no lux sensor.

    python3 tests/swarm/test_daylight_lights.py

The weather virtual device estimates outdoor light from the sun and the cloud
(modules/daylight.py), and the dusk-lights pattern turns every light on as it
fades, minus any the user unticks. These drive the real provider, the real
pattern through the matcher and compiler, and the rule through the engine.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker  # noqa: E402
from test_multi_source import _engine, _update  # noqa: E402
from test_swarm_expansion import (  # noqa: E402
    ROOMS, STORE, _add, _described, _for, _names, _settle, house,
)

from modules.swarm import suggestions as sg  # noqa: E402
from modules.swarm.compiler import CompileError  # noqa: E402
from modules.floor_plan import clean_floor_plan  # noqa: E402
from modules.swarm.virtual import (  # noqa: E402
    ROOM_DAYLIGHT_PREFIX, WEATHER_IEEE, VirtualDeviceProvider,
)

LONDON = (51.5074, -0.1278)
LIGHTS = {"0xhalllight", "0xlamp", "0xpendant"}


class FakeWeather:
    def __init__(self, **cur):
        self._cur = cur

    def get_current(self):
        return dict(self._cur)


def _at(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def _provider(clock, weather=None, location=LONDON):
    return VirtualDeviceProvider(
        weather_getter=(lambda: weather) if weather else None,
        location_getter=(lambda: location) if location else None,
        clock=lambda: clock[0])


def _targets(rule):
    out = set()
    for step in rule["then_sequence"]:
        if step.get("target_ieee"):
            out.add(step["target_ieee"])
        for branch in step.get("branches") or []:
            out.update(s.get("target_ieee") for s in branch if s.get("target_ieee"))
    return out


async def _run(c: Checker) -> None:
    c.section("the weather device estimates light from the sun alone")
    clock = [_at(2026, 12, 21, 12, 0)]
    p = _provider(clock)
    await p.refresh()
    w = p.devices[WEATHER_IEEE].state
    c.check("a winter noon under a clear sky is bright",
            w.get("daylight_level") == "bright" and w.get("is_daylight") == 1, w)
    c.check("and says it is a clear-sky guess", w.get("daylight_source") == "clear_sky", w)
    c.check("with no weather service there is no temperature", "temperature" not in w, w)
    clock[0] = _at(2026, 12, 21, 18, 0)
    await p.refresh()
    w = p.devices[WEATHER_IEEE].state
    c.check("two hours after sunset it is dark",
            w.get("daylight_level") == "dark" and w.get("is_daylight") == 0
            and w.get("is_gloomy") == 1, w)

    c.section("the weather is not mistaken for a lux sensor")
    from modules.swarm.resolver import describe_device
    d = describe_device(WEATHER_IEEE, p.devices[WEATHER_IEEE], "Weather")
    c.check("it resolves as weather only", d["capabilities"] == ["weather"], d["capabilities"])
    c.check("so no room-level lux rule can be built on it",
            not any(t["key"].startswith("illuminance:") for t in d["triggers"]))

    c.section("a dull sky brings the dark forward")
    clock = [_at(2026, 12, 21, 15, 39)]             # ~15 min before sunset
    clear = _provider(clock, FakeWeather(cloud_cover=0, fetched_at=clock[0]))
    grey = _provider(clock, FakeWeather(cloud_cover=100, fetched_at=clock[0]))
    await clear.refresh()
    await grey.refresh()
    cw, gw = clear.devices[WEATHER_IEEE].state, grey.devices[WEATHER_IEEE].state
    c.check("still daylight under a clear sky", cw.get("is_daylight") == 1, cw)
    c.check("already dusk under full cloud", gw.get("is_daylight") == 0, gw)
    c.check("the grey one reads from cloud cover", gw.get("daylight_source") == "cloud", gw)

    c.section("a measured reading drives it while it is fresh")
    fetched = _at(2026, 12, 21, 12, 0)
    clock = [fetched + 1800]
    p = _provider(clock, FakeWeather(shortwave_radiation=30.0, cloud_cover=100,
                                     fetched_at=fetched))
    await p.refresh()
    w = p.devices[WEATHER_IEEE].state
    c.check("a leaden noon is dull, not bright",
            w.get("daylight_level") == "dull" and w.get("is_gloomy") == 1
            and w.get("is_daylight") == 1, w)
    c.check("from the measurement", w.get("daylight_source") == "measured", w)

    c.section("the band is held against the last one published")
    clock = [_at(2026, 12, 21, 15, 54)]
    p = _provider(clock)
    p.devices[WEATHER_IEEE].state["daylight_level"] = "dusk"
    await p.refresh()
    lux = p.devices[WEATHER_IEEE].state["outdoor_lux"]
    c.check("a reading just above the dusk line stays dusk",
            400 <= lux < 600 and p.devices[WEATHER_IEEE].state["daylight_level"] == "dusk", lux)

    c.section("the pattern: every light, one card")
    devs = house()
    described = _described(devs)
    validator = _engine(devs)
    built = sg.build(described, rules=[], rooms=ROOMS, names=_names(devs),
                     validator=validator, patterns=STORE.all())
    dusk = _for(built, "dusk_lights_on")
    c.check("one suggestion for the whole house", len(dusk) == 1, len(dusk))
    s = dusk[0]
    c.check("it holds every light", _targets(s["rule"]) == LIGHTS, _targets(s["rule"]))
    c.check("the lights are the choosable part", s["choosable"] == ["lights"], s["choosable"])
    c.check("it remembers the lights before switching them on",
            s["rule"]["then_sequence"][0]["type"] == "snapshot"
            and set(s["rule"]["then_sequence"][0]["targets"]) == LIGHTS,
            s["rule"]["then_sequence"][0])
    c.check("and puts them back at daybreak",
            s["rule"]["else_sequence"] == [{"type": "restore", "name": "before_dusk"}],
            s["rule"]["else_sequence"])
    c.check("a passing dip is ridden out", s["rule"]["conditions"][0].get("sustain") == 120,
            s["rule"]["conditions"][0])

    c.section("unticking a light leaves it out")
    pattern = STORE.get("dusk_lights_on")
    rule = sg.recompile(pattern, s, described, rooms=ROOMS, exclude=["0xPendant"])
    c.check("the pendant is not switched", _targets(rule) == LIGHTS - {"0xpendant"},
            _targets(rule))
    c.check("nor remembered", "0xpendant" not in rule["then_sequence"][0]["targets"],
            rule["then_sequence"][0])
    try:
        sg.recompile(pattern, s, described, rooms=ROOMS, exclude=sorted(LIGHTS))
        c.check("unticking every light is refused", False)
    except CompileError as e:
        c.check("unticking every light is refused", "at least one" in str(e), str(e))
    c.check("exclude cannot touch the trigger",
            sg.recompile(pattern, s, described, rooms=ROOMS,
                         exclude=["virtual::weather"])["source_ieee"] == "virtual::weather")

    c.section("a rule built with fewer lights counts as built")
    engine = _engine(devs)
    _add(engine, rule)
    rebuilt = sg.build(described, rules=engine.get_rules(), rooms=ROOMS, names=_names(devs),
                       validator=engine, patterns=STORE.all())
    again = _for(rebuilt, "dusk_lights_on")
    c.check("the card is marked built", again and again[0]["status"] == "active",
            again and again[0]["status"])
    other = [x for x in rebuilt["suggestions"]
             if x["pattern_id"] != "dusk_lights_on" and x["status"] == "active"]
    c.check("and nothing else is", not other, [x["pattern_id"] for x in other])

    c.section("end to end: dusk on, daybreak back as they were")
    devs = house()
    engine = _engine(devs)
    rule = sg.recompile(pattern, s, described, rooms=ROOMS, exclude=["0xpendant"])
    for cond in rule["conditions"]:
        cond.pop("sustain", None)
    _add(engine, rule)
    await _update(engine, devs, WEATHER_IEEE, is_daylight=0)
    await _settle()
    c.check("the hall light comes on", ("on", None) in devs["0xhalllight"].sent,
            devs["0xhalllight"].sent)
    c.check("the unticked pendant is left alone", devs["0xpendant"].sent == [],
            devs["0xpendant"].sent)
    devs["0xhalllight"].state["state"] = "ON"
    devs["0xhalllight"].sent.clear()
    devs["0xlamp"].sent.clear()
    await _update(engine, devs, WEATHER_IEEE, is_daylight=1)
    await _settle()
    c.check("at daybreak the hall light goes back off",
            devs["0xhalllight"].sent == [("off", None)], devs["0xhalllight"].sent)
    c.check("the lamp, already on at dusk, stays on",
            ("off", None) not in devs["0xlamp"].sent, devs["0xlamp"].sent)


def _rooms_plan():
    """Lounge and hallway, each with an outside window; a windowless larder."""
    def box(rid, name, x0, win=True):
        return rid, name, x0, win
    walls, rooms, openings = [], [], []
    for rid, name, x0, win in (box("lounge", "Lounge", 0), box("hallway", "Hallway", 5),
                               box("larder", "Larder", 10, win=False)):
        rooms.append({"id": rid, "name": name,
                      "polygon": [[x0, 0], [x0 + 5, 0], [x0 + 5, 4], [x0, 4]]})
        walls.append({"id": f"{rid}_s", "x1": x0, "y1": 0, "x2": x0 + 5, "y2": 0, "type": "external"})
        walls.append({"id": f"{rid}_n", "x1": x0 + 5, "y1": 4, "x2": x0, "y2": 4, "type": "external"})
        if win:
            openings.append({"id": f"{rid}_w", "wall_id": f"{rid}_s", "kind": "window",
                             "offset_m": 1, "width_m": 1.4, "height_m": 1.2})
    walls += [{"id": "w0", "x1": 0, "y1": 0, "x2": 0, "y2": 4, "type": "external"},
              {"id": "w5", "x1": 5, "y1": 0, "x2": 5, "y2": 4},
              {"id": "w10", "x1": 10, "y1": 0, "x2": 10, "y2": 4},
              {"id": "w15", "x1": 15, "y1": 0, "x2": 15, "y2": 4, "type": "external"}]
    return clean_floor_plan({"levels": [{"id": "ground", "name": "Ground", "index": 0,
                                         "rooms": rooms, "walls": walls, "openings": openings}]})


async def _rooms(c: Checker) -> None:
    c.section("each windowed room gets a daylight device")
    plan = _rooms_plan()
    clock = [_at(2026, 12, 21, 12, 0)]
    p = VirtualDeviceProvider(location_getter=lambda: LONDON, plan_getter=lambda: plan,
                              clock=lambda: clock[0])
    changes = await p.refresh()
    room_devs = sorted(i for i in p.devices if i.startswith(ROOM_DAYLIGHT_PREFIX))
    c.check("lounge and hallway, not the windowless larder",
            room_devs == [ROOM_DAYLIGHT_PREFIX + "hallway", ROOM_DAYLIGHT_PREFIX + "lounge"], room_devs)
    lounge = p.devices[ROOM_DAYLIGHT_PREFIX + "lounge"]
    c.check("it reports lux as a light sensor would", lounge.state.get("illuminance_lux", 0) > 100
            and lounge.state.get("direct_sun") == 1, lounge.state)
    c.check("and the engine hears about it", ROOM_DAYLIGHT_PREFIX + "lounge" in changes)
    c.check("it knows its room", lounge.chamber == "lounge" and lounge.friendly_name == "Lounge daylight")
    from modules.swarm.resolver import describe_device
    d = describe_device(lounge.ieee, lounge, lounge.friendly_name)
    c.check("it resolves as an estimated lux sensor",
            "illuminance" in d["capabilities"] and d["estimated"] and d["scope"] == "room", d["capabilities"])
    c.check("that can never be offered as going offline",
            not any(t["key"].startswith("availability:") for t in d["triggers"]), [t["key"] for t in d["triggers"]])
    clock[0] = _at(2026, 12, 21, 18, 0)
    await p.refresh()
    c.check("after dark it reads dark", lounge.state.get("illuminance_lux") == 0
            and lounge.state.get("direct_sun") == 0, lounge.state)
    plan["levels"][0]["openings"] = [o for o in plan["levels"][0]["openings"] if o["id"] != "hallway_w"]
    await p.refresh()
    c.check("bricking up the hallway window removes its device",
            ROOM_DAYLIGHT_PREFIX + "hallway" not in p.devices)

    c.section("the room pattern: every light in the room, sensor or not")
    plan = _rooms_plan()
    p = VirtualDeviceProvider(location_getter=lambda: LONDON, plan_getter=lambda: plan,
                              clock=lambda: _at(2026, 12, 21, 12, 0))
    await p.refresh()
    devs = house()
    devs.update({i: d for i, d in p.devices.items() if i.startswith(ROOM_DAYLIGHT_PREFIX)})
    described = _described(devs)
    built = sg.build(described, rules=[], rooms=ROOMS, names=_names(devs),
                     validator=_engine(devs), patterns=STORE.all())
    rooms = {s["room"]: s for s in _for(built, "dark_light_on")}
    c.check("one card per room with lights", set(rooms) >= {"lounge", "hallway"}, sorted(rooms))
    lounge_rule = rooms["lounge"]["rule"]
    c.check("the lounge, with no light sensor, runs on its estimate",
            lounge_rule["source_ieee"] == ROOM_DAYLIGHT_PREFIX + "lounge", lounge_rule["source_ieee"])
    c.check("the hallway prefers its real sensor over the estimate",
            rooms["hallway"]["rule"]["source_ieee"] == "0xradar", rooms["hallway"]["rule"]["source_ieee"])
    c.check("every lounge light, as a checklist",
            _targets(lounge_rule) == {"0xlamp", "0xpendant"} and rooms["lounge"]["choosable"] == ["lights"],
            _targets(lounge_rule))
    c.check("remembered at dusk, restored when light",
            lounge_rule["then_sequence"][0]["type"] == "snapshot"
            and lounge_rule["else_sequence"] == [{"type": "restore", "name": "before_dark"}])

    c.section("end to end: the lounge darkens with no sensor in it")
    engine = _engine(devs)
    rule = sg.recompile(STORE.get("dark_light_on"), rooms["lounge"], described, rooms=ROOMS,
                        exclude=["0xlamp"])
    for cond in rule["conditions"]:
        cond.pop("sustain", None)
    _add(engine, rule)
    await _update(engine, devs, ROOM_DAYLIGHT_PREFIX + "lounge", illuminance_lux=3)
    await _settle()
    c.check("the pendant comes on", ("on", None) in devs["0xpendant"].sent, devs["0xpendant"].sent)
    c.check("the unticked lamp is left alone", devs["0xlamp"].sent == [], devs["0xlamp"].sent)


def run() -> Checker:
    c = Checker("test_daylight_lights")
    asyncio.run(_run(c))
    asyncio.run(_rooms(c))
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
