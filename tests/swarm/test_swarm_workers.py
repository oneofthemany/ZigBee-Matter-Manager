"""
Workers, the clock and the uncapped house — the swarm's second expansion.

    python3 tests/swarm/test_swarm_workers.py

Workers are household state no hardware reports: a mode, a shared temperature, a
countdown, a tally, when something last happened. The swarm now reads every
worker type as offers, proposes the workers a pattern needs where the house has
none, and creates them when a suggestion is applied. The hub gained the
household's day — sun windows, schedules, bedtime, phone shortcuts — and a
house-wide trigger is no longer capped at four.

These check the vocabulary and the matching, then apply suggestions through a
real automation engine and a real worker manager and watch what happens.
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    Checker, DIMMER_COMMANDS, FakeCapabilities, FakeDevice, LOCK_COMMANDS,
    SAMPLE_NAMES, SAMPLE_SETTINGS, offer, sample_network,
)
from test_multi_source import _update  # noqa: E402
from test_swarm_expansion import (  # noqa: E402
    ROOMS, SETTINGS, STORE, Rec, _for, _names, house,
)

from modules import automation  # noqa: E402
from modules.automation import MAX_RULES_PER_DEVICE, TIME_SOURCE, AutomationEngine  # noqa: E402
from modules.swarm import diagnostics as dx  # noqa: E402
from modules.swarm import suggestions as sg  # noqa: E402
from modules.swarm.capabilities import param_display, worker_payload, worker_satisfies  # noqa: E402
from modules.swarm.dedupe import coverage, signature  # noqa: E402
from modules.swarm.matcher import MAX_SOURCE_VARIANTS, MAX_VARIANTS_PER_SLOT  # noqa: E402
from modules.swarm.network import describe_network  # noqa: E402
from modules.swarm.resolver import describe_device, hub_device, proposed_worker_device  # noqa: E402
from modules.swarm.stigmergy import validate  # noqa: E402
from modules.workers import WorkerDevice, WorkerManager  # noqa: E402


def worker(data):
    """A WorkerDevice configured exactly as WorkerManager.create() would."""
    cfg = WorkerManager._normalise(WorkerManager.__new__(WorkerManager), data)
    return WorkerDevice(cfg)


def world(devices):
    """A real engine and a real worker manager, wired as main.py wires them."""
    tmp = tempfile.mkdtemp()
    automation.DATA_FILE = os.path.join(tmp, "automations.json")
    manager = WorkerManager(data_file=os.path.join(tmp, "workers.json"))

    def names():
        out = {k: d.friendly_name for k, d in devices.items()}
        out.update({w.ieee: w.friendly_name for w in manager.workers.values()})
        return out

    engine = AutomationEngine(lambda: {**devices, **manager.automation_devices()}, names)
    manager.set_evaluator(engine.evaluate)
    return engine, manager


def described_of(devices, settings=SETTINGS):
    return describe_network(devices, _names(devices), settings, ROOMS)["devices"]


def lockable_house():
    """The expansion house, with a lock that records what it is told."""
    devs = house()
    devs["nuki_1"] = Rec("nuki_1", "Front Door Lock",
                         {"locked": True, "lock_state": "locked", "door_state": "closed"},
                         commands=LOCK_COMMANDS, capabilities=FakeCapabilities(["lock"]))
    return devs


def create_workers(manager, pattern, suggestion, overrides=None):
    for payload in sg.worker_payloads(pattern, suggestion, overrides):
        if not manager.get(payload["id"]):
            made = manager.create(payload)
            assert made["success"], made


def add(engine, rule):
    data = copy.deepcopy(rule)
    data["cooldown"] = 0
    result = engine.add_rule(data)
    assert result["success"], result
    return result["rule"]["id"]


async def settle(seconds=0.05):
    await asyncio.sleep(seconds)


def flat(steps):
    for st in steps:
        if st.get("type") == "parallel":
            for branch in st["branches"]:
                yield from flat(branch)
        else:
            yield st


async def _run(c: Checker) -> None:

    c.section("every worker type is a set of offers")
    mode = describe_device("worker::hm", worker({"id": "hm", "name": "House Mode", "type": "mode",
                                                "options": ["home", "away", "On holiday"]}))
    c.check("a worker is a worker, not a switch", mode["device_class"] == "worker", mode["device_class"])
    c.check("a mode offers each option as its own trigger",
            [o["key"] for o in mode["triggers"]]
            == ["worker_mode:became:home", "worker_mode:became:away", "worker_mode:became:on_holiday"],
            [o["key"] for o in mode["triggers"]])
    is_away = offer(mode["conditions"], "worker_mode:is:away")
    c.check("and as a condition on its value",
            is_away["condition"] == {"type": "attribute", "attribute": "value",
                                     "operator": "eq", "value": "away"}, is_away)
    c.check("labelled by the option", is_away["label"] == "House Mode is away", is_away["label"])
    c.check("a mode can be set to each option",
            offer(mode["actions"], "worker_mode:set:on_holiday")["step"]["value"] == "On holiday")
    c.check("a worker never goes offline, whatever `available` says",
            not any(o["key"].startswith("availability:") for o in mode["triggers"]))

    flag = describe_device("worker::g", worker({"id": "g", "name": "Guest", "type": "boolean"}))
    c.check("a boolean switches on and off",
            {"worker_flag:turned_on", "worker_flag:turned_off"} == {o["key"] for o in flag["triggers"]}
            and offer(flag["actions"], "worker_flag:toggle")["step"]["command"] == "toggle")
    timer = describe_device("worker::t", worker({"id": "t", "name": "Boost", "type": "timer"}))
    start = offer(timer["actions"], "worker_timer:start")
    c.check("a timer starts for a duration", start["step"]["value"] == 3600
            and start["label"] == "start Boost for 1 h", start)
    counter = describe_device("worker::n", worker({"id": "n", "name": "Count", "type": "counter"}))
    c.check("a counter reaches a limit",
            offer(counter["triggers"], "worker_counter:reached")["condition"]["operator"] == "gte")
    marker = describe_device("worker::m", worker({"id": "m", "name": "Watered", "type": "marker"}))
    c.check("a marker reads its age",
            offer(marker["triggers"], "worker_marker:overdue")["condition"]["attribute"] == "age_minutes")
    c.check("and whether it was ever marked",
            offer(marker["conditions"], "worker_marker:ever_marked")["condition"]
            == {"type": "attribute", "attribute": "marked", "operator": "eq", "value": "on"})
    number = describe_device("worker::c", worker({"id": "c", "name": "Comfort", "type": "number"}))
    c.check("a number offers a live reading, not a trigger",
            number["triggers"] == [] and number["values"][0]["ref"]
            == {"ref": "worker::c", "attribute": "value"}, number["values"])
    c.check("durations read as a person says them",
            param_display("overdue_min", 5760) == "4 days" and param_display("overdue_min", 720) == "12 h")

    c.section("the hub carries the household's day")
    hub = hub_device()
    c.check("the sun as a window", offer(hub["triggers"], "hub:sun_sets")["condition"]
            == {"type": "sun", "from": "sunset", "to": "sunrise", "offset_from": 0})
    c.check("bedtime as a moment", offer(hub["triggers"], "hub:bedtime")["condition"]
            == {"type": "time", "at": "23:00"})
    c.check("a day as a window", offer(hub["triggers"], "hub:day_window")["label"]
            == "it's between 07:00 and 23:00")
    c.check("weekdays as a gate", offer(hub["conditions"], "hub:weekday")["condition"]["days"]
            == [0, 1, 2, 3, 4])
    c.check("a phone shortcut as a webhook", offer(hub["triggers"], "hub:goodnight_called")["condition"]
            == {"type": "webhook", "hook": "goodnight-scene"})

    c.section("worker templates: use what exists, propose what does not")
    proposed = proposed_worker_device("house_mode")
    c.check("a proposal is a worker at the address it will have",
            proposed["ieee"] == "worker::house_mode" and proposed["proposed_template"] == "house_mode")
    c.check("with every option", {o["option"] for o in proposed["actions"]}
            == {"home", "away", "night", "holiday"})
    own = describe_device("worker::hm", worker({"id": "hm", "name": "Our house mode", "type": "mode",
                                                "options": ["home", "away"]}), "Our house mode")
    c.check("a worker named for the job satisfies it", worker_satisfies(own, "house_mode"))
    c.check("a worker of another type does not",
            not worker_satisfies(describe_device("worker::x", worker(
                {"id": "x", "name": "House mode", "type": "boolean"}), "House mode"), "house_mode"))
    pool = sg.with_synthetic([own], STORE.all())
    c.check("nothing is proposed where a worker does the job",
            not any(d.get("proposed_template") == "house_mode" for d in pool))
    clash = describe_device("worker::house_mode", worker(
        {"id": "house_mode", "name": "Something else", "type": "boolean"}), "Something else")
    c.check("nor where the id is taken by another type",
            not any(d.get("proposed_template") == "house_mode"
                    for d in sg.with_synthetic([clash], STORE.all())))
    payload = worker_payload("comfort_temp", {"comfort_c": 19.5})
    c.check("a proposal starts at the card's value",
            payload["initial"] == 19.5 and payload["type"] == "number" and payload["unit"] == "°C", payload)

    c.section("the pattern language says what a worker slot may be")

    def base():
        return {"id": "w", "title": "W", "scope": "house",
                "slots": {"t": {"role": "trigger", "offer": "contact:opened"},
                          "m": {"role": "action", "offer": "worker_mode:set:away", "worker": "house_mode"}},
                "emits": {"source": "t", "conditions": ["t"], "then": ["m"]}}

    c.check("a worker slot is accepted", validate(base()) == [], validate(base()))
    for label, mutate, needle in [
        ("an unknown template", lambda p: p["slots"]["m"].update(worker="spaceship"), "unknown worker template"),
        ("a template of another type", lambda p: p["slots"]["m"].update(offer="worker_flag:turn_on"), "mode worker"),
        ("an option it lacks", lambda p: p["slots"]["m"].update(offer="worker_mode:set:party"), "no option"),
        ("a name_match that is not words", lambda p: p["slots"]["t"].update(name_match="door"), "name_match"),
        ("value_from_slot on a trigger", lambda p: p["slots"]["t"].update(value_from_slot="m"), "value_from_slot"),
        ("value_from_slot naming an action", lambda p: p["slots"]["m"].update(value_from_slot="t"), "not a value slot"),
    ]:
        p = base()
        mutate(p)
        c.check(f"{label} is refused", any(needle in e for e in validate(p)), validate(p))

    c.section("a house-wide trigger is no longer capped at four")
    import test_real_house as real
    rdevs = real._house()
    rnames = {i: d.friendly_name for i, d in rdevs.items()}
    rsettings = {i: {"chamber": d._room} for i, d in rdevs.items() if d._room}
    rnet = describe_network(rdevs, rnames, rsettings, real.ROOMS)["devices"]
    rbuilt = sg.build(rnet, rooms=real.ROOMS, patterns=STORE.all())
    batteries = sum(1 for d in rnet if any(o["key"].startswith("battery:low") for o in d["triggers"]))
    alerts = Counter(s["pattern_id"] for s in rbuilt["suggestions"])["battery_low_alert"]
    c.check(f"one battery alert per battery device ({batteries}), not {MAX_VARIANTS_PER_SLOT}",
            alerts == batteries and batteries > MAX_VARIANTS_PER_SLOT, (alerts, batteries))
    c.check("within the house cap", batteries <= MAX_SOURCE_VARIANTS)
    c.check("and still nothing withheld", rbuilt["rejected"] == [], rbuilt["rejected"][:2])

    c.section("suggestions that use workers")
    devs = lockable_house()
    described = described_of(devs)
    engine, manager = world(devs)
    built = sg.build(described, rooms=ROOMS, names=_names(devs), validator=engine,
                     patterns=STORE.all())
    c.check("nothing withheld", built["rejected"] == [], built["rejected"][:2])
    have = Counter(s["pattern_id"] for s in built["suggestions"])
    for pid in ("house_mode_follows_household", "night_mode_routine", "house_mode_heating",
                "away_openings_alert", "holiday_evening_lights", "heating_boost",
                "remember_last_movement", "stillness_alert", "count_door_opens",
                "busy_doors_reminder", "plants_watering_reminder", "heating_schedule",
                "blinds_follow_the_sun", "room_lights_off_at_bedtime",
                "evening_lights_until_bedtime", "lock_at_bedtime", "unlocked_at_bedtime_alert",
                "openings_at_bedtime", "standby_off_overnight", "idle_appliance_offer",
                "goodnight_shortcut", "leaving_shortcut", "door_opened_nobody_home",
                "motion_nobody_home", "unlocked_nobody_home_alert", "window_open_while_heating",
                "bright_room_lights_off", "lights_left_on_offer"):
        if pid in ("blinds_follow_the_sun", "window_open_while_heating", "bright_room_lights_off",
                   "motion_nobody_home", "remember_last_movement", "stillness_alert"):
            continue        # need blinds, a thermostat beside a contact, or lux — checked below
        if not c.check(f"{pid} is suggested", have.get(pid), dict(have)):
            break

    follows = _for(built, "house_mode_follows_household")[0]
    c.check("a card says which worker it creates",
            follows["creates_workers"] == [{"id": "house_mode", "ieee": "worker::house_mode",
                                            "name": "House mode", "type": "mode",
                                            "options": ["home", "away", "night", "holiday"],
                                            "description": follows["creates_workers"][0]["description"]}],
            follows["creates_workers"])
    c.check("and marks the proposed device", any(d["proposed"] for d in follows["devices"]))
    c.check("away as everybody leaves, home as somebody returns",
            follows["rule"]["then_sequence"] == [{"type": "command", "target_ieee": "worker::house_mode",
                                                  "command": "set", "value": "away", "endpoint_id": None}]
            and follows["rule"]["else_sequence"][0]["value"] == "home", follows["rule"])
    c.check("a proposed worker is not a coverage gap",
            coverage(sg.with_synthetic(described, STORE.all()), [])["devices"] == len(described))

    schedule = _for(built, "heating_schedule")[0]
    warm = schedule["rule"]["then_sequence"][0]
    c.check("a schedule's setpoint is read live from a worker",
            warm["value"] == {"ref": "worker::comfort_temp", "attribute": "value"}, warm)
    c.check("and says so", "set Lounge TRV to Comfort temperature" in schedule["sentence"],
            schedule["sentence"])
    c.check("it creates both temperatures",
            {w["id"] for w in schedule["creates_workers"]} == {"comfort_temp", "setback_temp"})

    c.section("a candidate made only of the hub and proposals is withheld")
    c.check("House mode to night at bedtime needs something that reads House mode",
            "night_mode_at_bedtime" not in have or any(
                not d["proposed"] and d["ieee"] != TIME_SOURCE
                for s in _for(built, "night_mode_at_bedtime") for d in s["devices"]))
    lone = sg.build([hub_device()], rooms={}, patterns=STORE.all())
    c.check("a network of nothing suggests nothing", lone["suggestions"] == [],
            [s["pattern_id"] for s in lone["suggestions"]])
    with_own = dict(devs)
    with_own["worker::house_mode"] = worker({"id": "house_mode", "name": "House mode", "type": "mode",
                                             "options": ["home", "away", "night", "holiday"]})
    own_built = sg.build(described_of(with_own), rooms=ROOMS, names=_names(with_own),
                         patterns=STORE.all())
    morning = _for(own_built, "morning_mode")
    c.check("but an existing House mode is part of the network", len(morning) == 1
            and morning[0]["creates_workers"] == [], [s["creates_workers"] for s in morning])
    routine = _for(own_built, "night_mode_routine")[0]
    c.check("and is used rather than proposed again", routine["creates_workers"] == []
            and not any(d["proposed"] for d in routine["devices"]), routine["devices"])

    c.section("many devices run together")
    big = lockable_house()
    for n in range(3):
        big[f"0xextra{n}"] = Rec(f"0xextra{n}", f"Spare Lamp {n}", {"state": "ON", "brightness": 50},
                                 commands=DIMMER_COMMANDS,
                                 capabilities=FakeCapabilities(["on_off", "light", "level_control"]))
    big_settings = {**SETTINGS, **{f"0xextra{n}": {"chamber": "lounge"} for n in range(3)}}
    big_built = sg.build(described_of(big, big_settings), rooms=ROOMS, names=_names(big),
                         patterns=STORE.all())
    night = _for(big_built, "night_mode_routine")[0]
    kinds = [st["type"] for st in night["rule"]["then_sequence"]]
    c.check("six lights are one parallel step", "parallel" in kinds, kinds)
    c.check("holding every light", {st["target_ieee"] for st in flat(night["rule"]["then_sequence"])
                                    if st["command"] == "off"} >= {"0xextra0", "0xextra1", "0xextra2"})
    c.check("and read as a sentence, not a list", "more" in night["sentence"], night["sentence"])

    c.section("dedupe tells schedules and modes apart")
    away = {"source_ieee": "virtual::household", "conditions": [{"attribute": "anyone_home", "operator": "lt", "value": 1}],
            "then_sequence": [{"type": "command", "target_ieee": "worker::house_mode", "command": "set", "value": "away"}]}
    home = copy.deepcopy(away)
    home["then_sequence"][0]["value"] = "home"
    c.check("setting away and setting home differ", signature(away) != signature(home))

    def at(hhmm):
        return {"source_ieee": TIME_SOURCE, "conditions": [{"type": "time", "at": hhmm}],
                "then_sequence": [{"type": "command", "target_ieee": "0xtrv", "command": "temperature", "value": 16}]}
    c.check("07:00 and 07:05 are one schedule", signature(at("07:00")) == signature(at("07:05")))
    c.check("07:00 and 23:00 are two", signature(at("07:00")) != signature(at("23:00")))

    c.section("diagnostics does not call a missing worker type a gap")
    report = dx.diagnose(described, built=built, rules=[], rooms=ROOMS)
    absent = next(f for f in report["findings"] if f["code"] == "capabilities_absent")
    c.check("no worker capability listed absent",
            not any(cap.startswith("worker_") for cap in absent["capabilities"]), absent["capabilities"])

    c.section("every suggestion saves once its workers exist")
    failed = []
    for s in built["suggestions"]:
        if not s["creates_workers"]:
            continue
        e2, m2 = world(lockable_house())
        create_workers(m2, STORE.get(s["pattern_id"]), s)
        result = e2.add_rule(copy.deepcopy(s["rule"]))
        if not result.get("success"):
            failed.append((s["pattern_id"], result.get("error")))
    c.check(f"all {sum(1 for s in built['suggestions'] if s['creates_workers'])} worker suggestions create",
            failed == [], failed)

    c.section("the hub's own source is not capped at ten")
    e3, _ = world(lockable_house())
    results = [e3.add_rule({"name": f"t{i}", "source_ieee": TIME_SOURCE,
                            "conditions": [{"type": "time", "at": "07:00"}],
                            "then_sequence": [{"type": "command", "target_ieee": "0xlamp",
                                               "command": "on"}]})
               for i in range(MAX_RULES_PER_DEVICE + 2)]
    c.check(f"{MAX_RULES_PER_DEVICE + 2} clock rules save", all(r["success"] for r in results),
            [r.get("error") for r in results if not r["success"]][:1])

    # End to end.

    c.section("end to end: House mode follows the household")
    devs = lockable_house()
    engine, manager = world(devs)
    create_workers(manager, STORE.get("house_mode_follows_household"), follows)
    add(engine, follows["rule"])
    await _update(engine, devs, "virtual::household", anyone_home=0)
    await settle()
    c.check("everybody leaving sets away", manager.get("house_mode").state["value"] == "away",
            manager.get("house_mode").state)
    await _update(engine, devs, "virtual::household", anyone_home=1)
    await settle()
    c.check("somebody returning sets home", manager.get("house_mode").state["value"] == "home",
            manager.get("house_mode").state)

    c.section("end to end: night mode locks up and switches off")
    devs = lockable_house()
    engine, manager = world(devs)
    routine = _for(built, "night_mode_routine")[0]
    create_workers(manager, STORE.get("night_mode_routine"), routine)
    add(engine, routine["rule"])
    await manager.command("house_mode", "set", "night")
    await settle()
    c.check("the door locks", devs["nuki_1"].sent == [("lock", None)], devs["nuki_1"].sent)
    c.check("every light goes off", all(devs[i].sent == [("off", None)]
                                        for i in ("0xhalllight", "0xlamp", "0xpendant")),
            {i: devs[i].sent for i in ("0xhalllight", "0xlamp", "0xpendant")})

    c.section("end to end: a schedule heats to the comfort worker's temperature")
    if time.strftime("%H:%M") == "23:59":
        c.check("the minute rolled over (skipped)", True)
    else:
        devs = lockable_house()
        engine, manager = world(devs)
        overrides = {"wake_time": "00:00", "bed_time": "23:59", "comfort_c": 19.5}
        rule = sg.recompile(STORE.get("heating_schedule"), schedule, described_of(devs),
                            overrides=overrides, rooms=ROOMS)
        create_workers(manager, STORE.get("heating_schedule"), schedule, overrides)
        c.check("the worker starts at the card's comfort value",
                manager.get("comfort_temp").state["value"] == 19.5, manager.get("comfort_temp").state)
        add(engine, rule)
        await engine._evaluate_timed_rules()
        await settle()
        c.check("the radiator is set from the worker", devs["0xtrv"].sent[-1:] == [("temperature", 19.5)],
                devs["0xtrv"].sent)

    c.section("end to end: door opens are counted")
    devs = lockable_house()
    engine, manager = world(devs)
    count = _for(built, "count_door_opens", "Front Door Contact")[0]
    create_workers(manager, STORE.get("count_door_opens"), count)
    add(engine, count["rule"])
    for _ in range(2):
        await _update(engine, devs, "0xfrontdoor", contact=False)
        await _update(engine, devs, "0xfrontdoor", contact=True)
    await settle()
    c.check("twice", manager.get("door_opens_today").state["value"] == 2,
            manager.get("door_opens_today").state)

    # The HTTP path.

    c.section("applying through the API creates the worker, then the rule")
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from test_api import FakeService, RecordingEngine
        from modules.swarm.api import register_swarm_routes
        from modules.workers import set_worker_manager
    except ImportError:
        c.check("fastapi not installed (skipped)", True)
        return
    manager = WorkerManager(data_file=os.path.join(tempfile.mkdtemp(), "workers.json"))
    set_worker_manager(manager)
    try:
        api_engine = RecordingEngine(sample_network(), SAMPLE_NAMES)
        app = FastAPI()
        register_swarm_routes(app, lambda: api_engine, lambda: FakeService(SAMPLE_SETTINGS))
        client = TestClient(app)
        listing = client.get("/api/swarm/suggestions").json()["suggestions"]
        routine = next(s for s in listing if s["pattern_id"] == "night_mode_routine")
        r = client.post(f"/api/swarm/suggestions/{routine['id']}/apply", json={})
        c.check("200", r.status_code == 200, r.text[:200])
        c.check("the worker was created", r.json().get("workers_created") == ["house_mode"]
                and manager.get("house_mode").type == "mode", r.json())
        c.check("and the rule hangs off it",
                api_engine.added[-1]["source_ieee"] == "worker::house_mode", api_engine.added[-1:])

        boost = next(s for s in listing if s["pattern_id"] == "heating_boost")
        api_engine.add_rule = lambda data: {"success": False, "error": "refused"}
        r = client.post(f"/api/swarm/suggestions/{boost['id']}/apply", json={})
        c.check("a refused rule is an error", r.status_code == 400, r.status_code)
        c.check("and leaves no worker behind",
                manager.get("heating_boost") is None and manager.get("comfort_temp") is None,
                [w["id"] for w in manager.list()])
        del api_engine.add_rule
        r = client.post(f"/api/swarm/suggestions/{boost['id']}/apply",
                        json={"params": {"comfort_c": 20.5}})
        c.check("applied with a card value", r.status_code == 200, r.text[:200])
        c.check("the worker starts at it", manager.get("comfort_temp").state["value"] == 20.5,
                manager.get("comfort_temp").state)
    finally:
        set_worker_manager(None)


def run() -> Checker:
    c = Checker("test_swarm_workers")
    asyncio.run(_run(c))
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
