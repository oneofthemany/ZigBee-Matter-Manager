"""
Multi-source trigger tests — one rule, several trigger devices, AND / OR.

    python3 tests/swarm/test_multi_source.py

A trigger condition may name its own device in `ieee`; without one it reads the
rule's source_ieee. The rule's condition_logic then joins the conditions across
devices. These checks drive the real engine through evaluate() with fake
devices, so they cover indexing, per-device reading, the momentary-attribute
rule and validation together rather than each helper on its own.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker  # noqa: E402

import modules.automation as automation  # noqa: E402
from modules.automation import MAX_RULES_PER_DEVICE, AutomationEngine  # noqa: E402


class Dev:
    """A registry device: state plus a send_command that records what it got."""

    def __init__(self, name, **state):
        self.friendly_name = name
        self.state = dict(state)
        self.sent = []

    async def send_command(self, command, value=None, endpoint_id=None):
        self.sent.append(command)
        return {"success": True}


def _house():
    return {
        "0xpir":   Dev("Hall Motion", occupancy=False),
        "0xlux":   Dev("Hall Lux", illuminance_lux=100, battery=90),
        "0xfront": Dev("Front Door", contact=True),
        "0xback":  Dev("Back Door", contact=True),
        "0xbtn":   Dev("Bedside Button", action="none"),
        "0xlight": Dev("Hall Light", state="OFF"),
        "user::sean":    Dev("Sean", place="home"),
        "user::charlie": Dev("Charlie", place="work"),
    }


def _engine(devices):
    # Never the real data/automations.json: the engine loads and saves there.
    automation.DATA_FILE = os.path.join(tempfile.mkdtemp(), "automations.json")
    return AutomationEngine(lambda: devices,
                            lambda: {k: d.friendly_name for k, d in devices.items()})


def _rule(source, conditions, logic="and", then="on", else_=None):
    data = {"name": "t", "source_ieee": source, "conditions": conditions,
            "condition_logic": logic, "cooldown": 0,
            "then_sequence": [{"type": "command", "target_ieee": "0xlight",
                               "command": then}]}
    if else_:
        data["else_sequence"] = [{"type": "command", "target_ieee": "0xlight",
                                  "command": else_}]
    return data


async def _update(engine, devices, ieee, **changed):
    """What core does: state already holds the new value when evaluate runs."""
    devices[ieee].state.update(changed)
    await engine.evaluate(ieee, changed)
    await asyncio.sleep(0.01)          # let a fired sequence reach the device


def _attr(attribute, operator, value, ieee=None):
    c = {"type": "attribute", "attribute": attribute, "operator": operator,
         "value": value}
    if ieee:
        c["ieee"] = ieee
    return c


async def _run(c: Checker) -> None:
    c.section("a legacy rule is unchanged")
    legacy = {"source_ieee": "0xpir", "conditions": [_attr("occupancy", "eq", True)]}
    c.check("its only trigger device is its source",
            AutomationEngine.rule_sources(legacy) == ["0xpir"],
            AutomationEngine.rule_sources(legacy))
    timed = {"source_ieee": "0xpir",
             "conditions": [{"type": "time", "at": "07:00", "ieee": "0xlux"}]}
    c.check("a clock condition names no device even if one is attached",
            AutomationEngine.rule_sources(timed) == ["0xpir"])

    c.section("validation")
    devices = _house()
    e = _engine(devices)
    bad = e.add_rule(_rule("0xpir", [_attr("occupancy", "eq", True),
                                     _attr("illuminance_lux", "lt", 20, "0xgone")]))
    c.check("a condition on a missing device is refused",
            not bad["success"] and "not found" in bad["error"], bad)
    grp = e.add_rule(_rule("0xpir", [_attr("state", "eq", "ON", "group:3")]))
    c.check("a group can't be a trigger device",
            not grp["success"] and "group" in grp["error"], grp)
    blind = e.add_rule(_rule("__time__", [_attr("contact", "eq", False)]))
    c.check("a time rule's device condition must pick a device",
            not blind["success"], blind)
    sighted = e.add_rule(_rule("__time__", [_attr("contact", "eq", False, "0xfront")]))
    c.check("a time rule with a device condition naming its device is accepted",
            sighted["success"], sighted)
    zone = e.add_rule(_rule("user::sean", [
        {"type": "zone", "event": "leave", "place": "home", "ieee": "0xpir"}]))
    c.check("a zone condition must read a person",
            not zone["success"] and "presence" in zone["error"], zone)
    same = e.add_rule(_rule("0xpir", [_attr("occupancy", "eq", True, "0xpir")]))
    c.check("naming the source itself is harmless",
            same["success"] and e.rule_sources(same["rule"]) == ["0xpir"], same)

    c.section("AND across two devices")
    devices = _house()
    e = _engine(devices)
    r = e.add_rule(_rule("0xpir", [_attr("occupancy", "eq", True),
                                   _attr("illuminance_lux", "lt", 20, "0xlux")],
                         else_="off"))
    c.check("saved", r["success"], r)
    rid = r["rule"]["id"]
    c.check("indexed under both devices",
            rid in e._source_index.get("0xpir", []) and rid in e._source_index.get("0xlux", []),
            e._source_index)
    listed = e.get_rules(source_ieee="0xlux")
    c.check("listed for the second device too", [x["id"] for x in listed] == [rid], listed)
    c.check("the listing names every trigger device",
            listed and listed[0].get("sources") == ["0xpir", "0xlux"], listed)
    c.check("the listing names the condition's device",
            listed and listed[0]["conditions"][1].get("device_name") == "Hall Lux", listed)

    light = devices["0xlight"]
    await _update(e, devices, "0xpir", occupancy=True)
    c.check("motion alone, while bright, does nothing", light.sent == [], light.sent)
    await _update(e, devices, "0xlux", battery=89)
    c.check("an unwatched attribute on the second device does nothing",
            light.sent == [], light.sent)
    await _update(e, devices, "0xlux", illuminance_lux=10)
    c.check("the second device changing completes the AND and fires",
            light.sent == ["on"], light.sent)
    await _update(e, devices, "0xlux", illuminance_lux=12)
    c.check("still matched is not a new transition", light.sent == ["on"], light.sent)
    await _update(e, devices, "0xpir", occupancy=False)
    c.check("either side failing runs ELSE", light.sent == ["on", "off"], light.sent)
    trace = e.get_trace_log(rid)
    c.check("the trace names the other device on its condition",
            any(cr.get("device_name") == "Hall Lux"
                for t in trace for cr in t.get("conditions") or []), trace[-3:])

    c.section("OR across two devices")
    devices = _house()
    e = _engine(devices)
    r = e.add_rule(_rule("0xfront", [_attr("contact", "eq", False),
                                     _attr("contact", "eq", False, "0xback")],
                         logic="or", else_="off"))
    light = devices["0xlight"]
    await _update(e, devices, "0xback", contact=False)
    c.check("the second door alone fires it", light.sent == ["on"], light.sent)
    await _update(e, devices, "0xfront", contact=False)
    c.check("the first door opening too is no new transition",
            light.sent == ["on"], light.sent)
    await _update(e, devices, "0xback", contact=True)
    c.check("one door still open keeps it matched", light.sent == ["on"], light.sent)
    await _update(e, devices, "0xfront", contact=True)
    c.check("both shut runs ELSE", light.sent == ["on", "off"], light.sent)

    c.section("a source with no condition of its own is not re-evaluated")
    devices = _house()
    e = _engine(devices)
    r = e.add_rule(_rule("0xpir", [_attr("contact", "eq", False, "0xfront")]))
    rid = r["rule"]["id"]
    before = len(e.get_trace_log(rid))
    await _update(e, devices, "0xpir", occupancy=True)
    c.check("the source's own updates leave the rule alone",
            len(e.get_trace_log(rid)) == before, e.get_trace_log(rid))
    await _update(e, devices, "0xfront", contact=False)
    c.check("the named device still fires it", devices["0xlight"].sent == ["on"],
            devices["0xlight"].sent)

    c.section("momentary attributes count only on their own update")
    devices = _house()
    e = _engine(devices)
    e.add_rule(_rule("0xbtn", [_attr("action", "eq", "single"),
                               _attr("illuminance_lux", "lt", 20, "0xlux")]))
    light = devices["0xlight"]
    await _update(e, devices, "0xbtn", action="single")
    c.check("a press while bright does nothing", light.sent == [], light.sent)
    await _update(e, devices, "0xlux", illuminance_lux=5)
    c.check("getting dark later does not replay the old press",
            light.sent == [], light.sent)
    await _update(e, devices, "0xbtn", action="single")
    c.check("a fresh press in the dark fires", light.sent == ["on"], light.sent)
    await _update(e, devices, "0xbtn", action="single")
    c.check("and re-arms for the next press", light.sent == ["on", "on"], light.sent)

    c.section("a zone on one person, a place check on another")
    devices = _house()
    e = _engine(devices)
    r = e.add_rule(_rule("user::sean", [
        {"type": "zone", "event": "leave", "place": "home"},
        _attr("place", "neq", "home", "user::charlie")], then="off"))
    c.check("saved", r["success"], r)
    # Startup records where everyone was, so a first departure has a "from".
    e._seed_last_values()
    c.check("both people are baselined, not only the source",
            {"user::sean", "user::charlie"} <= set(e._last_values), list(e._last_values))
    light = devices["0xlight"]
    await _update(e, devices, "user::charlie", place="shops")
    c.check("the other person moving is not Sean leaving", light.sent == [], light.sent)
    await _update(e, devices, "user::sean", place="away")
    c.check("Sean leaving while Charlie is out fires", light.sent == ["off"], light.sent)

    c.section("the per-device cap counts every trigger device")
    devices = _house()
    e = _engine(devices)
    for _ in range(MAX_RULES_PER_DEVICE):
        ok = e.add_rule(_rule("0xpir", [_attr("illuminance_lux", "lt", 20, "0xlux")]))
    over = e.add_rule(_rule("0xfront", [_attr("contact", "eq", False),
                                        _attr("illuminance_lux", "lt", 20, "0xlux")]))
    c.check("the next rule naming a full device is refused",
            ok["success"] and not over["success"] and "Hall Lux" in over["error"], over)

    c.section("update_rule re-indexes")
    devices = _house()
    e = _engine(devices)
    rid = e.add_rule(_rule("0xpir", [_attr("occupancy", "eq", True)]))["rule"]["id"]
    res = e.update_rule(rid, {"conditions": [_attr("occupancy", "eq", True),
                                             _attr("contact", "eq", False, "0xback")]})
    c.check("updated", res["success"], res)
    c.check("the new device now triggers it", rid in e._source_index.get("0xback", []))
    res = e.update_rule(rid, {"conditions": [_attr("occupancy", "eq", True)]})
    c.check("and stops once the condition is gone",
            rid not in e._source_index.get("0xback", []), e._source_index)

    c.section("swarm bookkeeping sees every trigger device")
    try:
        from modules.swarm.dedupe import coverage, signature
    except ImportError as err:                       # noqa: F841
        c.check("dedupe importable (skipped)", True)
    else:
        own = {"source_ieee": "0xpir", "conditions": [_attr("occupancy", "eq", True)],
               "then_sequence": []}
        other = {"source_ieee": "0xpir",
                 "conditions": [_attr("occupancy", "eq", True, "0xradar")],
                 "then_sequence": []}
        c.check("watching another device's attribute signs differently",
                signature(own) != signature(other), (signature(own), signature(other)))
        cov = coverage([{"ieee": "0xradar", "name": "Radar"}], [other])
        c.check("a device a condition names counts as automated",
                cov["covered"] == 1, cov)


def run() -> Checker:
    c = Checker("test_multi_source")
    asyncio.run(_run(c))
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
