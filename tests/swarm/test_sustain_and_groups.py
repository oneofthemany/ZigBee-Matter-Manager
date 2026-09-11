"""
Sustain re-check and condition-group tests.

    python3 tests/swarm/test_sustain_and_groups.py

A sustain ("for N seconds") used to resolve only on its device's next update,
so a sensor that reported once and then went quiet never fired. Trigger
conditions were also one flat AND/OR list, with no way to say "(A or B) and C".
These drive the real engine through evaluate() with fake devices and real,
short timers — a sustain test that mocked the clock would not catch a re-check
that was never scheduled.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker  # noqa: E402
from test_multi_source import _attr, _engine, _house, _rule, _update  # noqa: E402

from modules.automation import MAX_CONDITIONS_PER_GROUP  # noqa: E402


def _group(logic, *conds):
    return {"type": "group", "condition_logic": logic, "conditions": list(conds)}


def _sustained(attribute, operator, value, seconds, ieee=None):
    return {**_attr(attribute, operator, value, ieee), "sustain": seconds}


async def _run(c: Checker) -> None:
    c.section("a sustain fires on its own once its time is up")
    devices = _house()
    e = _engine(devices)
    r = e.add_rule(_rule("0xfront", [_sustained("contact", "eq", False, 1)], else_="off"))
    c.check("saved", r["success"], r)
    rid = r["rule"]["id"]
    light = devices["0xlight"]
    await _update(e, devices, "0xfront", contact=False)
    c.check("opening the door does not fire at once", light.sent == [], light.sent)
    c.check("a re-check is scheduled", rid in e._sustain_timers, e._sustain_timers)
    await asyncio.sleep(1.5)
    c.check("with no further update it fires once the second is up",
            light.sent == ["on"], light.sent)
    c.check("and leaves no timer behind", rid not in e._sustain_timers, e._sustain_timers)
    await asyncio.sleep(1.2)
    c.check("it does not fire twice", light.sent == ["on"], light.sent)
    await _update(e, devices, "0xfront", contact=True)
    c.check("closing runs ELSE", light.sent == ["on", "off"], light.sent)

    c.section("a sustain interrupted before its time does not fire")
    devices = _house()
    e = _engine(devices)
    rid = e.add_rule(_rule("0xfront", [_sustained("contact", "eq", False, 1)]))["rule"]["id"]
    light = devices["0xlight"]
    await _update(e, devices, "0xfront", contact=False)
    await asyncio.sleep(0.4)
    await _update(e, devices, "0xfront", contact=True)
    c.check("closing early cancels the re-check", rid not in e._sustain_timers,
            e._sustain_timers)
    await asyncio.sleep(1.2)
    c.check("nothing fires", light.sent == [], light.sent)

    c.section("a sustain is timed from when its own condition became true")
    devices = _house()
    e = _engine(devices)
    rid = e.add_rule(_rule("0xfront", [
        _attr("illuminance_lux", "lt", 20, "0xlux"),          # listed first
        _sustained("contact", "eq", False, 2)]))["rule"]["id"]
    light = devices["0xlight"]
    await _update(e, devices, "0xfront", contact=False)      # t0, still bright
    c.check("no re-check while another condition fails outright",
            rid not in e._sustain_timers, e._sustain_timers)
    await asyncio.sleep(1.0)
    await _update(e, devices, "0xlux", illuminance_lux=5)    # t0 + 1
    c.check("getting dark does not fire before the door's two seconds",
            light.sent == [], light.sent)
    await asyncio.sleep(1.6)                                 # t0 + 2.6
    c.check("it fires two seconds after the door opened, not after it got dark",
            light.sent == ["on"], light.sent)

    c.section("deleting or disabling a rule drops its re-check")
    devices = _house()
    e = _engine(devices)
    rid = e.add_rule(_rule("0xfront", [_sustained("contact", "eq", False, 5)]))["rule"]["id"]
    await _update(e, devices, "0xfront", contact=False)
    task = e._sustain_timers.get(rid)
    e.update_rule(rid, {"enabled": False})
    await asyncio.sleep(0)
    c.check("disabling cancels it", rid not in e._sustain_timers and task is not None
            and task.cancelled(), (e._sustain_timers, task))
    e.update_rule(rid, {"enabled": True})
    await _update(e, devices, "0xfront", contact=True)
    await _update(e, devices, "0xfront", contact=False)
    c.check("re-enabled, a new sustain schedules again", rid in e._sustain_timers)
    e.delete_rule(rid)
    c.check("deleting drops it and its clocks",
            rid not in e._sustain_timers
            and not any(k.startswith(f"{rid}_") for k in e._sustain_tracker),
            (e._sustain_timers, e._sustain_tracker))

    c.section("groups: validation")
    e = _engine(_house())
    occ = _attr("occupancy", "eq", True)
    res = e.add_rule(_rule("0xpir", [_group("or", _group("and", occ), occ)]))
    c.check("a group can't hold a group",
            not res["success"] and "another group" in res["error"], res)
    res = e.add_rule(_rule("0xpir", [_group("or")]))
    c.check("an empty group is refused", not res["success"], res)
    res = e.add_rule(_rule("0xpir", [{"type": "group", "condition_logic": "xor",
                                      "conditions": [occ]}]))
    c.check("a group's logic must be and/or", not res["success"], res)
    res = e.add_rule(_rule("0xpir", [_group("or", *[dict(occ)] * (MAX_CONDITIONS_PER_GROUP + 1))]))
    c.check("a group has a size limit",
            not res["success"] and "in a group" in res["error"], res)
    res = e.add_rule(_rule("0xpir", [_group("or", _attr("contact", "eq", False, "0xgone"))]))
    c.check("a missing device inside a group is still caught",
            not res["success"] and "not found" in res["error"], res)

    c.section("groups: (front door or back door open) and dark")
    devices = _house()
    e = _engine(devices)
    r = e.add_rule(_rule("0xlux", [
        _group("or", _attr("contact", "eq", False, "0xfront"),
                     _attr("contact", "eq", False, "0xback")),
        _attr("illuminance_lux", "lt", 20)], else_="off"))
    c.check("saved", r["success"], r)
    rid = r["rule"]["id"]
    c.check("every device inside the group is a trigger device",
            e.rule_sources(r["rule"]) == ["0xlux", "0xfront", "0xback"],
            e.rule_sources(r["rule"]))
    c.check("and indexed", all(rid in e._source_index.get(s, [])
                               for s in ("0xlux", "0xfront", "0xback")), e._source_index)
    light = devices["0xlight"]
    await _update(e, devices, "0xback", contact=False)
    c.check("a door opening while bright does nothing", light.sent == [], light.sent)
    await _update(e, devices, "0xlux", illuminance_lux=5)
    c.check("getting dark with a door open fires", light.sent == ["on"], light.sent)
    await _update(e, devices, "0xfront", contact=False)
    await _update(e, devices, "0xback", contact=True)
    c.check("one door still open keeps it matched", light.sent == ["on"], light.sent)
    await _update(e, devices, "0xfront", contact=True)
    c.check("both doors shut runs ELSE", light.sent == ["on", "off"], light.sent)
    trace = e.get_trace_log(rid)
    c.check("the trace records the group with its members",
            any(cr.get("type") == "group" and len(cr.get("conditions") or []) == 2
                for t in trace for cr in t.get("conditions") or []), trace[-2:])
    listed = e.get_rules(source_ieee="0xback")
    c.check("listed for a device inside the group, with its name filled in",
            listed and listed[0]["conditions"][0]["conditions"][1].get("device_name") == "Back Door",
            listed)

    c.section("groups: (motion and dark) or the back door opens")
    devices = _house()
    e = _engine(devices)
    r = e.add_rule(_rule("0xpir", [
        _group("and", _attr("occupancy", "eq", True),
                      _attr("illuminance_lux", "lt", 20, "0xlux")),
        _attr("contact", "eq", False, "0xback")], logic="or"))
    light = devices["0xlight"]
    await _update(e, devices, "0xpir", occupancy=True)
    c.check("motion while bright does nothing", light.sent == [], light.sent)
    await _update(e, devices, "0xback", contact=False)
    c.check("the other side of the OR fires alone", light.sent == ["on"], light.sent)
    await _update(e, devices, "0xback", contact=True)
    await _update(e, devices, "0xlux", illuminance_lux=5)
    c.check("the whole group coming true fires again", light.sent == ["on", "on"], light.sent)

    c.section("a sustain inside a group")
    devices = _house()
    e = _engine(devices)
    devices["0xlux"].state["illuminance_lux"] = 5
    rid = e.add_rule(_rule("0xpir", [_group(
        "and", _sustained("contact", "eq", False, 1, "0xfront"),
               _attr("illuminance_lux", "lt", 20, "0xlux"))]))["rule"]["id"]
    light = devices["0xlight"]
    await _update(e, devices, "0xfront", contact=False)
    c.check("its clock is keyed inside the group", f"{rid}_0.0" in e._sustain_tracker,
            e._sustain_tracker)
    c.check("and a re-check is scheduled", rid in e._sustain_timers)
    await asyncio.sleep(1.5)
    c.check("it fires when the time is up", light.sent == ["on"], light.sent)

    c.section("groups: clock times, zones and bookkeeping look inside")
    e = _engine(_house())
    timed = {"id": "t", "source_ieee": "0xpir", "conditions": [
        _group("or", {"type": "time", "at": "07:00"}, _attr("occupancy", "eq", True))]}
    c.check("an alarm inside a group is a clock boundary",
            "07:00" in e._rule_temporal_boundaries(timed), e._rule_temporal_boundaries(timed))
    c.check("a zone inside a group makes it a crossing rule",
            e._has_zone([_group("or", {"type": "zone", "event": "leave", "place": "home"})]))
    try:
        from modules.swarm.dedupe import coverage, signature
    except ImportError:
        c.check("dedupe importable (skipped)", True)
    else:
        grouped = {"source_ieee": "0xpir", "then_sequence": [], "conditions": [
            _group("or", _attr("occupancy", "eq", True), _attr("contact", "eq", False, "0xback"))]}
        c.check("a signature sees attributes inside a group",
                "0xback:contact:eq" in signature(grouped)[1], signature(grouped))
        cov = coverage([{"ieee": "0xback", "name": "Back Door"}], [grouped])
        c.check("a device inside a group counts as automated", cov["covered"] == 1, cov)
    try:
        from modules.automation_api import AutomationCreateRequest, _conds_to_dicts
    except ImportError:
        c.check("automation_api importable (skipped)", True)
    else:
        req = AutomationCreateRequest(source_ieee="0xpir", conditions=[
            {"type": "group", "condition_logic": "or", "conditions": [
                {"type": "attribute", "ieee": "0xfront", "attribute": "contact",
                 "operator": "eq", "value": False},
                {"type": "attribute", "attribute": "occupancy", "operator": "eq",
                 "value": True, "sustain": 30}]}])
        d = _conds_to_dicts(req.conditions)
        c.check("the API keeps a group's shape, logic and members",
                d[0]["type"] == "group" and d[0]["condition_logic"] == "or"
                and d[0]["conditions"][0]["ieee"] == "0xfront"
                and d[0]["conditions"][1]["sustain"] == 30, d)


def run() -> Checker:
    c = Checker("test_sustain_and_groups")
    asyncio.run(_run(c))
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
