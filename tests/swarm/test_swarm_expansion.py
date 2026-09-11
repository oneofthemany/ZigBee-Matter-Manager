"""
Swarm expansion tests — the swarm using the whole automation engine.

    python3 tests/swarm/test_swarm_expansion.py

The pattern language can now collect every matching device into one rule, make
another device's reading a live trigger, hold a trigger, choose a run mode, and
use repeat, wait-for, snapshot/restore and live values in its steps; the
vocabulary gained trends, silence and a hub device for startup and calendar
conditions. These build every shipped pattern against a larger fake house, put
each suggestion through the real engine's validation and save path, and run a
handful end to end.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    Checker, DIMMER_COMMANDS, FakeCapabilities, FakeDevice, ONOFF_COMMANDS,
    SAMPLE_ROOMS, SAMPLE_SETTINGS, THERMOSTAT_COMMANDS, offer, sample_network,
    stub_duckdb,
)
from test_multi_source import _engine, _update  # noqa: E402

stub_duckdb()
import modules.messages_store as ms  # noqa: E402
from modules.automation import TIME_SOURCE  # noqa: E402
from modules.swarm import diagnostics as dx  # noqa: E402
from modules.swarm import suggestions as sg  # noqa: E402
from modules.swarm.capabilities import coerce_param, param_display  # noqa: E402
from modules.swarm.compiler import describe_candidate  # noqa: E402
from modules.swarm.dedupe import signature  # noqa: E402
from modules.swarm.matcher import MAX_COLLECT, match_pattern  # noqa: E402
from modules.swarm.network import describe_network  # noqa: E402
from modules.swarm.resolver import hub_device  # noqa: E402
from modules.swarm.stigmergy import StigmergyStore, validate  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
STORE = StigmergyStore(bundled_dir=str(REPO / "modules" / "swarm" / "patterns"),
                       user_dir="/nonexistent")

NEW_OR_UPGRADED = [
    "room_windows_heating_down", "last_out_openings_check", "alarm_flash_lights",
    "mirror_lights", "shower_extractor", "window_opened_cooling", "rapid_warming_alert",
    "power_surge_alert", "appliance_left_running", "leak_remind_until_dry",
    "soil_dry_message", "relock_when_nobody_home", "hub_restarted_notice",
    "restart_lights_off_when_empty", "restart_relock", "seasonal_lights_at_dusk",
    "night_path_light", "door_left_open", "device_offline_alert",
    "everyone_out_lights_off",
]

ROOMS = {**SAMPLE_ROOMS, "bathroom": "Bathroom", "kitchen": "Kitchen"}


class Rec(FakeDevice):
    """A fake device that records what it is told."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = []

    async def send_command(self, command, value=None, endpoint_id=None):
        self.sent.append((command, value))
        return {"success": True}


class FakeStore:
    def __init__(self):
        self.sent = []

    async def send(self, from_user, to_user, body, source="user"):
        self.sent.append(body)
        return {"success": True, "message": {"id": "m1"}}


def _ms():
    return int(time.time() * 1000)


def house():
    devs = sample_network()
    devs["0xtrv"] = Rec("0xtrv", "Lounge TRV",
                        {"local_temperature": 17.5, "pi_heating_demand": 40,
                         "occupied_heating_setpoint": 21.0, "battery": 70},
                        commands=THERMOSTAT_COMMANDS,
                        capabilities=FakeCapabilities(["thermostat", "hvac", "battery"]))
    devs["0xhalllight"] = Rec("0xhalllight", "Light - Hallway",
                              {"state": "OFF", "brightness": 180}, commands=DIMMER_COMMANDS,
                              capabilities=FakeCapabilities(["on_off", "light", "level_control"]))
    devs["0xbath"] = FakeDevice("0xbath", "Bathroom Sensor",
                                {"humidity": 58, "temperature": 21.0, "battery": 90,
                                 "last_seen": _ms()},
                                capabilities=FakeCapabilities(["humidity_sensor",
                                                               "temperature_sensor", "battery"]))
    devs["0xfan"] = Rec("0xfan", "Extractor Fan", {"state": "OFF"}, commands=ONOFF_COMMANDS,
                        capabilities=FakeCapabilities(["on_off", "switch"]))
    devs["0xwin1"] = FakeDevice("0xwin1", "Lounge Window Left", {"contact": True},
                                capabilities=FakeCapabilities(["contact_sensor"]))
    devs["0xwin2"] = FakeDevice("0xwin2", "Lounge Window Right", {"contact": True},
                                capabilities=FakeCapabilities(["contact_sensor"]))
    devs["0xlamp"] = Rec("0xlamp", "Lamp - Lounge", {"state": "ON", "brightness": 120},
                         commands=DIMMER_COMMANDS,
                         capabilities=FakeCapabilities(["on_off", "light", "level_control"]))
    devs["0xpendant"] = Rec("0xpendant", "Pendant - Lounge", {"state": "OFF", "brightness": 200},
                            commands=DIMMER_COMMANDS,
                            capabilities=FakeCapabilities(["on_off", "light", "level_control"]))
    devs["0xleak"] = FakeDevice("0xleak", "Leak - Kitchen",
                                {"water_leak": False, "battery": 99, "last_seen": _ms()},
                                capabilities=FakeCapabilities(["ias_zone", "battery"]))
    devs["0xsoil"] = FakeDevice("0xsoil", "Plant - Lounge", {"soil_moisture": 45})
    devs["0xtree"] = Rec("0xtree", "Tree Plug", {"state": "OFF", "power": 0.0},
                         commands=ONOFF_COMMANDS,
                         capabilities=FakeCapabilities(["on_off", "power_monitoring"]))
    devs["virtual::weather"] = FakeDevice("virtual::weather", "Weather",
                                          {"temperature": 8.0, "is_daylight": 1},
                                          capabilities=FakeCapabilities(["weather"]))
    devs["virtual::household"] = FakeDevice("virtual::household", "Household",
                                            {"anyone_home": 1, "everyone_home": 0},
                                            capabilities=FakeCapabilities(["household"]))
    return devs


SETTINGS = {**SAMPLE_SETTINGS,
            "0xbath": {"chamber": "bathroom"}, "0xfan": {"chamber": "bathroom"},
            "0xwin1": {"chamber": "lounge"}, "0xwin2": {"chamber": "lounge"},
            "0xlamp": {"chamber": "lounge"}, "0xpendant": {"chamber": "lounge"},
            "0xsoil": {"chamber": "lounge"}, "0xtree": {"chamber": "lounge"},
            "0xleak": {"chamber": "kitchen"}}


def _names(devs):
    return {k: d.friendly_name for k, d in devs.items()}


def _described(devs):
    return describe_network(devs, _names(devs), SETTINGS, ROOMS)["devices"]


def _for(built, pattern_id, needle=None):
    return [s for s in built["suggestions"] if s["pattern_id"] == pattern_id
            and (needle is None or needle in s["sentence"])]


def _add(engine, rule):
    """Save a suggestion's rule with no cooldown, so a test can drive it twice."""
    data = copy.deepcopy(rule)
    data["cooldown"] = 0
    result = engine.add_rule(data)
    assert result["success"], result
    return result["rule"]["id"]


async def _settle(seconds=0.05):
    await asyncio.sleep(seconds)


async def _run(c: Checker) -> None:
    devs = house()
    described = _described(devs)
    by_ieee = {d["ieee"]: d for d in described}

    c.section("vocabulary: trends, silence, a dry leak sensor and the hub")
    bath = by_ieee["0xbath"]
    spike = offer(bath["triggers"], "humidity:spiking")
    c.check("a humidity sensor offers a jump", spike is not None, [o["key"] for o in bath["triggers"]])
    c.check("as a rise within a window, in seconds",
            spike and spike["condition"]["operator"] == "rose_by"
            and spike["condition"]["within"] == 900 and spike["condition"]["value"] == 10, spike)
    c.check("and says so", spike and "within 15 min" in spike["label"], spike and spike["label"])
    c.check("temperature can fall fast too",
            offer(bath["triggers"], "temperature:falling_fast") is not None)
    gone = offer(bath["triggers"], "availability:went_offline")
    c.check("a device reporting a last-seen time can go offline",
            gone and gone["condition"] == {"type": "offline", "minutes": 120}, gone)
    c.check("one reporting nothing of the kind cannot",
            not any(o["key"].startswith("availability:") for o in by_ieee["0xradar"]["triggers"]))
    c.check("a leak sensor can be asked whether it is dry",
            offer(by_ieee["0xleak"]["conditions"], "water_leak:is_dry") is not None)
    hub = hub_device()
    c.check("the hub is the engine's clock source", hub["ieee"] == TIME_SOURCE, hub["ieee"])
    c.check("it can start", offer(hub["triggers"], "hub:started")["condition"] == {"type": "startup"})
    season = offer(hub["conditions"], "hub:in_season")
    c.check("it knows the season", season["condition"] == {"type": "date", "from": "12-01",
                                                           "to": "01-06"}, season)
    c.check("in words", season["label"] == "it's between 1 Dec and 6 Jan", season["label"])
    c.check("and quiet hours",
            offer(hub["conditions"], "hub:quiet_hours")["condition"]["time_from"] == "22:30")
    c.check("the hub is not in the network view", TIME_SOURCE not in by_ieee)

    c.section("parameters: display and coercion")
    c.check("a window reads in minutes", param_display("trend_window_min", 15) == "15 min")
    c.check("a season boundary reads as a day", param_display("season_from", "12-01") == "1 Dec")
    c.check("a number arriving as text is taken", coerce_param("remind_max", "6") == 6)
    c.check("and clamped", coerce_param("remind_max", 1000) == 100)
    c.check("a real month-day is kept", coerce_param("season_from", "11-15") == "11-15")
    c.check("an impossible one is refused", coerce_param("season_from", "13-45") is None)
    c.check("29 February is a day", coerce_param("season_to", "02-29") == "02-29")
    c.check("a bad time is refused", coerce_param("quiet_from", "25:00") is None)
    c.check("a colour by name", coerce_param("alert_colour", "amber") == [40, 100])

    c.section("the shipped patterns load, and the new ones are there")
    c.check("no load errors", STORE.errors == [], STORE.errors)
    for pid in NEW_OR_UPGRADED:
        if not c.check(f"{pid} loaded", STORE.get(pid) is not None):
            break
    c.check("the one-window heating pattern is retired",
            STORE.get("window_open_pause_heating") is not None
            and all(p["id"] != "window_open_pause_heating" for p in STORE.all()))

    c.section("every pattern builds and passes the engine's own validation")
    validator = _engine(devs)
    built = sg.build(described, rules=[], rooms=ROOMS, names=_names(devs),
                     validator=validator, patterns=STORE.all())
    c.check("nothing withheld", built["rejected"] == [], built["rejected"])
    have = defaultdict(int)
    for s in built["suggestions"]:
        have[s["pattern_id"]] += 1
    for pid in NEW_OR_UPGRADED:
        if not c.check(f"{pid} is suggested", have.get(pid), dict(have)):
            break
    c.check("ids are stable across builds",
            {s["id"] for s in built["suggestions"]}
            == {s["id"] for s in sg.build(described, rooms=ROOMS, names=_names(devs),
                                          validator=validator,
                                          patterns=STORE.all())["suggestions"]})

    c.section("every suggestion saves through the engine")
    failed = []
    for s in built["suggestions"]:
        engine = _engine(house())
        result = engine.add_rule(copy.deepcopy(s["rule"]))
        if not result.get("success"):
            failed.append((s["pattern_id"], result.get("error")))
    c.check(f"all {len(built['suggestions'])} create", failed == [], failed)

    c.section("collect: every window in the room as one group")
    win = _for(built, "room_windows_heating_down")[0]
    group = win["rule"]["conditions"][0]
    c.check("one OR group of both windows",
            group.get("type") == "group" and group["condition_logic"] == "or"
            and len(group["conditions"]) == 2, win["rule"]["conditions"])
    c.check("the second window names its device",
            any(leaf.get("ieee") in ("0xwin1", "0xwin2") for leaf in group["conditions"]), group)
    c.check("down while open, back once shut",
            win["rule"]["then_sequence"][0]["value"] == 7.0
            and win["rule"]["else_sequence"][0]["value"] == 21.0, win["rule"])
    c.check("the card lists both windows",
            {"0xwin1", "0xwin2"} <= {d["ieee"] for d in win["devices"]}, win["devices"])
    lights_off = _for(built, "everyone_out_lights_off")
    c.check("every light off is one suggestion, not one per light", len(lights_off) == 1,
            [s["sentence"] for s in lights_off])
    c.check("driving every light",
            {"0xhalllight", "0xlamp", "0xpendant"}
            <= {st["target_ieee"] for st in lights_off[0]["rule"]["then_sequence"]},
            lights_off[0]["rule"]["then_sequence"])

    c.section("an alarm flashes every light, then restores them")
    flash = _for(built, "alarm_flash_lights")[0]["rule"]
    lights = {"0xhalllight", "0xlamp", "0xpendant"}
    c.check("snapshot of every light first", flash["then_sequence"][0]["type"] == "snapshot"
            and set(flash["then_sequence"][0]["targets"]) == lights, flash["then_sequence"][0])
    repeat = flash["then_sequence"][1]
    c.check("then a repeat of toggles and pauses", repeat["type"] == "repeat"
            and repeat["count"] == 3 and len(repeat["steps"]) == 8, repeat)
    c.check("then put back", flash["then_sequence"][2] == {"type": "restore",
                                                            "name": "before_alarm"})
    c.check("never twice at once", flash["run_mode"] == "single")

    c.section("a door reminder holds, repeats until shut, and names the door live")
    doors = _for(built, "door_left_open", "Front Door Contact")
    c.check("the front door has one", len(doors) == 1, [s["sentence"] for s in _for(built, "door_left_open")])
    door = doors[0]["rule"]
    c.check("held for the open time", door["conditions"][0].get("sustain") == 300, door["conditions"])
    rep = door["then_sequence"][0]
    c.check("repeats until that door is shut",
            rep["mode"] == "until" and rep["inline_conditions"][0]
            == {"ieee": "0xfrontdoor", "attribute": "contact", "operator": "eq", "value": True}, rep)
    c.check("waiting between reminders for the door, not a fixed delay",
            rep["steps"][1]["type"] == "wait_for" and rep["steps"][1]["timeout"] == 600
            and rep["max_iterations"] == 6, rep["steps"])
    c.check("the sentence says the hold", "for 5 min" in doors[0]["sentence"], doors[0]["sentence"])

    c.section("the hub: startup, seasons and quiet hours")
    boot = _for(built, "hub_restarted_notice")[0]["rule"]
    c.check("a restart rule hangs off the clock source",
            boot["source_ieee"] == TIME_SOURCE and boot["conditions"] == [{"type": "startup"}], boot)
    after_cut = _for(built, "restart_lights_off_when_empty")[0]["rule"]
    c.check("after a cut, only if nobody is home",
            after_cut["prerequisites"] == [{"ieee": "virtual::household", "attribute": "anyone_home",
                                            "operator": "lt", "value": 1}], after_cut["prerequisites"])
    xmas = _for(built, "seasonal_lights_at_dusk")[0]
    c.check("seasonal lights test the date as a condition",
            {"type": "date", "from": "12-01", "to": "01-06"} in xmas["rule"]["conditions"],
            xmas["rule"]["conditions"])
    c.check("and read as dates", "1 Dec" in xmas["sentence"] and "6 Jan" in xmas["sentence"],
            xmas["sentence"])
    night = _for(built, "night_path_light")[0]["rule"]
    c.check("the path light waits for quiet hours",
            any(cn.get("type") == "time_window" and cn["time_from"] == "22:30"
                for cn in night["conditions"]), night["conditions"])
    c.check("and comes up low", night["then_sequence"][0]["value"] == 15, night["then_sequence"])

    c.section("trends, holds and live values")
    shower = _for(built, "shower_extractor")[0]["rule"]
    c.check("a shower is a humidity jump",
            shower["conditions"][0]["operator"] == "rose_by" and shower["conditions"][0]["within"] == 900,
            shower["conditions"])
    c.check("the fan waits for the room to dry",
            shower["then_sequence"][1] == {"type": "wait_for", "timeout": 3600, "ieee": "0xbath",
                                           "attribute": "humidity", "operator": "lt", "value": 60},
            shower["then_sequence"])
    running = _for(built, "appliance_left_running")[0]["rule"]
    c.check("running for hours is a three-hour hold", running["conditions"][0]["sustain"] == 10800,
            running["conditions"])
    c.check("the message carries the live reading",
            "{matter_plug.power}" in running["then_sequence"][0]["message"],
            running["then_sequence"][0]["message"])
    battery = _for(built, "battery_low_alert", "Front Door Contact")[0]["rule"]
    c.check("a battery message carries its reading",
            "{0xfrontdoor.battery}" in battery["then_sequence"][0]["message"],
            battery["then_sequence"][0]["message"])
    relock = _for(built, "relock_when_nobody_home")[0]["rule"]
    c.check("a lock is relocked after being unlocked a while, with nobody home",
            relock["conditions"][0].get("sustain") == 600
            and relock["prerequisites"][0]["ieee"] == "virtual::household"
            and relock["then_sequence"][0]["command"] == "lock", relock)
    offline = _for(built, "device_offline_alert", "Bathroom")[0]["rule"]
    c.check("going offline is silence for two hours, and coming back is ELSE",
            offline["conditions"] == [{"type": "offline", "minutes": 120}]
            and offline["else_sequence"][0]["type"] == "request", offline)
    last_out = _for(built, "last_out_openings_check")[0]["rule"]
    openings = [cn for cn in last_out["conditions"] if cn.get("type") == "group"]
    c.check("leaving checks every opening, each watched live",
            openings and len(openings[0]["conditions"]) == 3
            and all(leaf.get("ieee") for leaf in openings[0]["conditions"]), last_out["conditions"])
    mirror = _for(built, "mirror_lights")[0]["rule"]
    c.check("a light never follows itself",
            mirror["then_sequence"][0]["target_ieee"] != mirror["source_ieee"], mirror)

    c.section("a card's parameters reach the rule, and nonsense does not")
    pattern = STORE.get("seasonal_lights_at_dusk")
    tuned = sg.recompile(pattern, xmas, described,
                         overrides={"season_from": "11-15", "season_to": "bogus"}, rooms=ROOMS)
    c.check("a new season start is used, a bad end ignored",
            {"type": "date", "from": "11-15", "to": "01-06"} in tuned["conditions"], tuned["conditions"])

    c.section("dedupe tells these automations apart")
    finished = _for(built, "appliance_finished", "Lounge Plug")[0]["rule"]
    c.check("finishing and running for hours are different", signature(finished) != signature(running))
    single = copy.deepcopy(door)
    single["then_sequence"] = [rep["steps"][0]]
    c.check("a reminder is not a single message", signature(single) != signature(door))
    c.check("a restart rule signs as one", "startup" in signature(boot)[1], signature(boot))

    c.section("collect is capped, and says what it left out")
    many = house()
    for n in range(3, 8):
        many[f"0xwin{n}"] = FakeDevice(f"0xwin{n}", f"Lounge Window {n}", {"contact": True},
                                       capabilities=FakeCapabilities(["contact_sensor"]))
    settings = {**SETTINGS, **{f"0xwin{n}": {"chamber": "lounge"} for n in range(3, 8)}}
    pool = sg.with_hub(describe_network(many, _names(many), settings, ROOMS)["devices"])
    result = match_pattern(STORE.get("room_windows_heating_down"), pool, ROOMS)
    lounge = [cd for cd in result["candidates"] if cd["room"] == "lounge"][0]
    trace = [t for t in result["trace"] if t["room"] == "lounge"][0]
    c.check(f"at most {MAX_COLLECT} windows", len(lounge["fills"]["open"]["members"]) == MAX_COLLECT)
    c.check("the rest are counted", trace["slots"]["open"]["left_out"] == 2, trace["slots"]["open"])

    c.section("the pattern language refuses what would not compile")

    def minimal():
        return {"id": "t", "title": "T", "scope": "room",
                "slots": {"a": {"role": "trigger", "offer": "presence:detected"},
                          "b": {"role": "action", "offer": "on_off:turn_on"}},
                "emits": {"source": "a", "conditions": ["a"], "then": ["b"]}}

    def fails(mutate, needle):
        p = minimal()
        mutate(p)
        errors = validate(p)
        return any(needle in e for e in errors), errors

    for label, mutate, needle in [
        ("collect_logic", lambda p: p["slots"]["a"].update(collect=True, collect_logic="xor"), "collect_logic"),
        ("exclude_slot", lambda p: p["slots"]["b"].update(exclude_slot="zz"), "exclude_slot"),
        ("sustain", lambda p: p["slots"]["a"].update(sustain={"param": "nope"}), "sustain"),
        ("reactive on an action", lambda p: p["slots"]["b"].update(reactive=True), "reactive"),
        ("run_mode", lambda p: p["emits"].update(run_mode="forever"), "run_mode"),
        ("literal parameter", lambda p: p["emits"].update(
            then=["b", {"type": "delay", "seconds": {"param": "nope"}}]), "unknown parameter"),
        ("$cond slot", lambda p: p["emits"].update(
            then=["b", {"type": "wait_for", "$cond": "ghost", "timeout": 5}]), "$ghost"),
    ]:
        ok, errors = fails(mutate, needle)
        c.check(f"a bad {label} is caught", ok, errors)
    good = minimal()
    good["slots"]["a"].update(collect=True, sustain=30)
    good["emits"]["run_mode"] = "queued"
    c.check("the new keys used properly are accepted", validate(good) == [], validate(good))

    c.section("diagnostics does not call the hub missing")
    report = dx.diagnose(described, built=built, rules=[], rooms=ROOMS)
    absent = [f for f in report.get("findings", []) if f.get("code") == "capabilities_absent"]
    c.check("the hub is not an absent capability",
            absent and "hub" not in absent[0].get("capabilities", []), absent)
    c.check("explain can see the hub",
            dx.explain("hub_restarted_notice", described, ROOMS)["outcome"] == "matched")

    # End to end, through the engine.

    c.section("end to end: windows drop the heating and bring it back")
    devs = house()
    engine = _engine(devs)
    _add(engine, win["rule"])
    await _update(engine, devs, "0xwin2", contact=False)
    await _settle()
    c.check("opening either window drops the radiator", devs["0xtrv"].sent == [("temperature", 7.0)],
            devs["0xtrv"].sent)
    await _update(engine, devs, "0xwin2", contact=True)
    await _settle()
    c.check("shutting it brings it back", devs["0xtrv"].sent[-1] == ("temperature", 21.0),
            devs["0xtrv"].sent)

    c.section("end to end: the hub restarting sends a message")
    devs = house()
    engine = _engine(devs)
    store = FakeStore()
    ms.set_message_store(store)
    _add(engine, boot)
    engine._evaluate_startup_rules()
    await _settle()
    c.check("with the time in it", store.sent and store.sent[0].startswith("The hub restarted at "),
            store.sent)

    c.section("end to end: an alarm flashes and restores")
    devs = house()
    engine = _engine(devs)
    rule = sg.recompile(STORE.get("alarm_flash_lights"), _for(built, "alarm_flash_lights")[0],
                        described, overrides={"flash_count": 1}, rooms=ROOMS)
    _add(engine, rule)
    await _update(engine, devs, "0xleak", water_leak=True)
    await _settle(2.6)
    c.check("a light that was off flashes and ends off",
            devs["0xhalllight"].sent == [("toggle", None), ("toggle", None), ("off", None)],
            devs["0xhalllight"].sent)
    c.check("a light that was on flashes and ends on at its brightness",
            devs["0xlamp"].sent == [("toggle", None), ("toggle", None), ("on", None), ("brightness", 47)],
            devs["0xlamp"].sent)

    c.section("end to end: a shower runs the fan until the room dries")
    devs = house()
    engine = _engine(devs)
    _add(engine, shower)
    await _update(engine, devs, "0xbath", humidity=70)
    await _settle()
    c.check("the jump starts the fan", devs["0xfan"].sent == [("on", None)], devs["0xfan"].sent)
    devs["0xbath"].state["humidity"] = 55
    await _settle(2.5)
    c.check("drying out stops it", devs["0xfan"].sent == [("on", None), ("off", None)],
            devs["0xfan"].sent)

    c.section("end to end: a door reminder fires after the hold and stops when shut")
    devs = house()
    engine = _engine(devs)
    store = FakeStore()
    ms.set_message_store(store)
    rid = _add(engine, sg.recompile(STORE.get("door_left_open"), doors[0], described,
                                    overrides={"open_hold_s": 1}, rooms=ROOMS))
    await _update(engine, devs, "0xfrontdoor", contact=False)
    await _settle(1.7)
    c.check("one reminder, naming the door", len(store.sent) == 1
            and store.sent[0].startswith("Front Door Contact is still open"), store.sent)
    await _update(engine, devs, "0xfrontdoor", contact=True)
    await _settle(2.5)
    results = [t["result"] for t in engine.get_trace_log(rid)]
    c.check("shutting it ends the reminders", "REPEAT_DONE" in results and len(store.sent) == 1,
            (results[-6:], store.sent))


def run() -> Checker:
    c = Checker("test_swarm_expansion")
    asyncio.run(_run(c))
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
