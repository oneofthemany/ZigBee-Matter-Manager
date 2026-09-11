"""
Change-trigger and offline-condition tests.

    python3 tests/swarm/test_change_and_offline.py

Trigger conditions could only test what a value *is*. Change operators test
that it just changed (to / from something), or moved by an amount within a
window. And a device that stops reporting sent nothing a rule could react to;
the offline condition reads its silence on the clock. These drive the real
engine with fake devices.
"""

from __future__ import annotations

import asyncio
import datetime
import sys
import time
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker  # noqa: E402
from test_multi_source import Dev, _attr, _engine, _house, _rule, _update  # noqa: E402

from modules.automation import AutomationEngine, DEFAULT_TREND_WINDOW  # noqa: E402


def _house_plus():
    devices = _house()
    devices["0xtherm"] = Dev("Thermostat", running_state="idle", temperature=20.0)
    return devices


def _trend(op, amount, within=None, ieee=None):
    c = _attr("temperature", op, amount, ieee)
    if within is not None:
        c["within"] = within
    return c


async def _settle():
    await asyncio.sleep(0.01)


async def _run(c: Checker) -> None:
    c.section("validation")
    e = _engine(_house_plus())
    add = lambda conds, src="0xtherm": e.add_rule(_rule(src, conds))  # noqa: E731
    res = add([_attr("running_state", "changed_to", None)])
    c.check("changes to needs a value", not res["success"] and "needs a value" in res["error"], res)
    res = add([_trend("rose_by", 0)])
    c.check("rises by needs a positive amount", not res["success"], res)
    res = add([_trend("rose_by", 2, within=10 ** 6)])
    c.check("the window has a ceiling", not res["success"] and "within" in res["error"], res)
    res = add([_trend("rose_by", 2)])
    c.check("the window defaults to an hour",
            res["success"] and res["rule"]["conditions"][0]["within"] == DEFAULT_TREND_WINDOW, res)
    res = add([{**_attr("running_state", "changed", None), "sustain": 30}])
    c.check("a change carries no sustain",
            res["success"] and "sustain" not in res["rule"]["conditions"][0], res)
    res = add([{"type": "attribute", "attribute": "temperature", "operator": "changed"}])
    c.check("changes needs no value at all", res["success"], res)
    data = _rule("0xtherm", [_attr("temperature", "gt", 25)])
    data["prerequisites"] = [{"type": "device", "ieee": "0xtherm", "attribute": "temperature",
                              "operator": "changed", "value": None}]
    res = e.add_rule(data)
    c.check("a prerequisite can't test a change",
            not res["success"] and "trigger condition" in res["error"], res)
    data = _rule("0xtherm", [_attr("temperature", "gt", 25)])
    data["then_sequence"] = [{"type": "condition", "ieee": "0xtherm", "attribute": "temperature",
                              "operator": "rose_by", "value": 1},
                             {"type": "command", "target_ieee": "0xlight", "command": "on"}]
    res = e.add_rule(data)
    c.check("nor can a gate", not res["success"] and "trigger condition" in res["error"], res)
    res = add([{"type": "offline", "minutes": -5}])
    c.check("offline minutes must be positive", not res["success"], res)
    res = add([{"type": "offline"}])
    c.check("offline without minutes means the hub's verdict",
            res["success"] and "minutes" not in res["rule"]["conditions"][0], res)

    c.section("changes: any new value, once per change")
    devices = _house_plus()
    e = _engine(devices)
    e.add_rule(_rule("0xlux", [_attr("illuminance_lux", "changed", None)], else_="off"))
    light = devices["0xlight"]
    await _update(e, devices, "0xlux", illuminance_lux=100)
    c.check("a report of the same value is not a change", light.sent == [], light.sent)
    await _update(e, devices, "0xlux", illuminance_lux=50)
    c.check("the first change after the rule was added fires", light.sent == ["on"], light.sent)
    await _update(e, devices, "0xlux", illuminance_lux=50)
    c.check("reporting it again does not", light.sent == ["on"], light.sent)
    await _update(e, devices, "0xlux", illuminance_lux=60)
    c.check("the next change fires again, and ELSE never runs",
            light.sent == ["on", "on"], light.sent)

    c.section("from one value to another: changes from AND changes to")
    devices = _house_plus()
    e = _engine(devices)
    e.add_rule(_rule("0xtherm", [_attr("running_state", "changed_from", "idle"),
                                 _attr("running_state", "changed_to", "heating")]))
    light = devices["0xlight"]
    for state in ("cooling", "heating"):
        await _update(e, devices, "0xtherm", running_state=state)
    c.check("idle → cooling → heating is not idle → heating", light.sent == [], light.sent)
    await _update(e, devices, "0xtherm", running_state="idle")
    await _update(e, devices, "0xtherm", running_state="heating")
    c.check("idle → heating fires", light.sent == ["on"], light.sent)

    c.section("a change on another device counts only on that device's update")
    devices = _house_plus()
    e = _engine(devices)
    e.add_rule(_rule("0xpir", [_attr("occupancy", "eq", True),
                               _attr("contact", "changed_to", False, "0xfront")]))
    light = devices["0xlight"]
    await _update(e, devices, "0xfront", contact=False)
    await _update(e, devices, "0xpir", occupancy=True)
    c.check("motion after the door opened is not the door opening", light.sent == [], light.sent)
    await _update(e, devices, "0xfront", contact=True)
    await _update(e, devices, "0xfront", contact=False)
    c.check("the door opening during motion fires", light.sent == ["on"], light.sent)

    c.section("rises by: within a window, true while it holds")
    devices = _house_plus()
    e = _engine(devices)
    rid = e.add_rule(_rule("0xtherm", [_trend("rose_by", 2, within=2)], else_="off"))["rule"]["id"]
    key = ("0xtherm", "temperature")
    c.check("the attribute's readings are kept", key in e._trend_windows, e._trend_windows)
    light = devices["0xlight"]
    for t in (20.5, 21.5):
        await _update(e, devices, "0xtherm", temperature=t)
    c.check("a rise of 1.5 is not 2", light.sent == [], light.sent)
    await _update(e, devices, "0xtherm", temperature=22.1)
    c.check("a rise of 2.1 from where it stood fires", light.sent == ["on"], light.sent)
    await _update(e, devices, "0xtherm", temperature=22.3)
    c.check("still risen is no new transition", light.sent == ["on"], light.sent)
    await asyncio.sleep(2.2)
    await _update(e, devices, "0xtherm", temperature=22.4)
    c.check("once the window slides past the rise, ELSE runs",
            light.sent == ["on", "off"], light.sent)
    c.check("old readings are pruned", len(e._history.get(key, ())) <= 2, e._history.get(key))
    e.delete_rule(rid)
    c.check("and dropped with the rule", key not in e._history and key not in e._trend_windows)

    c.section("falls by")
    devices = _house_plus()
    e = _engine(devices)
    e.add_rule(_rule("0xtherm", [_trend("fell_by", 1, within=600)]))
    light = devices["0xlight"]
    await _update(e, devices, "0xtherm", temperature=19.5)
    c.check("a fall of 0.5 is not 1", light.sent == [], light.sent)
    await _update(e, devices, "0xtherm", temperature=18.9)
    c.check("a fall of 1.1 fires", light.sent == ["on"], light.sent)
    await _update(e, devices, "0xtherm", running_state="heating")
    c.check("an unwatched attribute keeps no history",
            ("0xtherm", "running_state") not in e._history, list(e._history))

    c.section("a zone or change rule still fires on its clock condition")
    devices = _house()
    e = _engine(devices)
    at = datetime.datetime.now().strftime("%H:%M")
    e.add_rule(_rule("user::sean", [{"type": "zone", "event": "enter", "place": "home"},
                                    {"type": "time", "at": at}], logic="or"))
    await e._evaluate_timed_rules()
    await _settle()
    if datetime.datetime.now().strftime("%H:%M") == at:
        c.check("'arrives home OR it's HH:MM' fires at HH:MM",
                devices["0xlight"].sent == ["on"], devices["0xlight"].sent)
    else:
        c.check("minute rolled over mid-test (skipped)", True)

    c.section("offline: not heard from for N minutes")
    devices = _house()
    e = _engine(devices)
    pir = devices["0xpir"]
    pir.last_seen = int((time.time() - 600) * 1000)      # Zigbee keeps ms
    rid = e.add_rule(_rule("0xpir", [{"type": "offline", "minutes": 5}], else_="off"))["rule"]["id"]
    light = devices["0xlight"]
    c.check("an offline condition watches last_seen",
            "last_seen" in AutomationEngine._watched_attributes([{"type": "offline"}]))
    e._evaluate_offline_rules()
    await _settle()
    c.check("ten minutes silent, five allowed: fires", light.sent == ["on"], light.sent)
    traced = len(e.get_trace_log(rid))
    e._evaluate_offline_rules()
    await _settle()
    c.check("the next pass, with nothing moved, neither fires nor traces",
            light.sent == ["on"] and len(e.get_trace_log(rid)) == traced,
            (light.sent, e.get_trace_log(rid)[traced:]))
    pir.last_seen = int(time.time() * 1000)
    await _update(e, devices, "0xpir", occupancy=True, last_seen=pir.last_seen)
    c.check("the device reporting again runs ELSE", light.sent == ["on", "off"], light.sent)

    devices = _house()
    e = _engine(devices)
    devices["0xpir"].last_seen = int((time.time() - 120) * 1000)
    e.add_rule(_rule("0xpir", [{"type": "offline", "minutes": 5}]))
    e._evaluate_offline_rules()
    await _settle()
    c.check("two minutes silent is not five", devices["0xlight"].sent == [], devices["0xlight"].sent)

    c.section("offline, negated: has reported within N minutes")
    devices = _house()
    e = _engine(devices)
    pir = devices["0xpir"]
    pir.last_seen = int((time.time() - 600) * 1000)
    stored = e.add_rule(_rule("0xpir", [{"type": "offline", "minutes": 5, "negate": "yes"}]))
    c.check("negate is stored as a plain true", stored["rule"]["conditions"][0].get("negate") is True,
            stored["rule"]["conditions"])
    plain = e.add_rule(_rule("0xpir", [{"type": "offline", "minutes": 5, "negate": False}],
                             then="toggle"))
    c.check("and dropped when false", "negate" not in plain["rule"]["conditions"][0],
            plain["rule"]["conditions"])
    e.delete_rule(plain["rule"]["id"])
    e._evaluate_offline_rules()
    await _settle()
    c.check("ten minutes silent is not 'has reported'", devices["0xlight"].sent == [],
            devices["0xlight"].sent)
    pir.last_seen = int(time.time() * 1000)
    await _update(e, devices, "0xpir", occupancy=True, last_seen=pir.last_seen)
    c.check("reporting again passes", devices["0xlight"].sent == ["on"], devices["0xlight"].sent)
    blind = e.add_rule(_rule("0xlux", [{"type": "offline", "negate": True}], then="stop"))["rule"]["id"]
    e._evaluate_offline_rules()
    await _settle()
    verdicts = [cr for t in e.get_trace_log(blind) for cr in t.get("conditions") or []]
    c.check("a device whose availability is unknown is not 'online' either",
            verdicts and all(cr.get("result") == "FAIL" for cr in verdicts)
            and any("no availability" in cr.get("reason", "") for cr in verdicts), verdicts)

    c.section("offline: the hub's own verdict")
    devices = _house()
    e = _engine(devices)
    devices["0xpir"]._available = False
    devices["0xfront"].is_available = lambda: True
    e.add_rule(_rule("0xpir", [{"type": "offline"}]))
    e.add_rule(_rule("0xfront", [{"type": "offline"}], then="toggle"))
    blind = e.add_rule(_rule("0xlux", [{"type": "offline"}], then="stop"))["rule"]["id"]
    minutes_blind = e.add_rule(_rule("0xback", [{"type": "offline", "minutes": 1}],
                                     then="close"))["rule"]["id"]
    e._evaluate_offline_rules()
    await _settle()
    c.check("only the device the hub marks unavailable fires",
            devices["0xlight"].sent == ["on"], devices["0xlight"].sent)
    reasons = [cr.get("reason", "") for t in e.get_trace_log(blind)
               for cr in t.get("conditions") or []]
    c.check("a device with no availability says so in the trace",
            any("no availability" in r for r in reasons), reasons)
    reasons = [cr.get("reason", "") for t in e.get_trace_log(minutes_blind)
               for cr in t.get("conditions") or []]
    c.check("and one with no last-seen time says that",
            any("last-seen" in r for r in reasons), reasons)

    c.section("the API carries the new fields")
    try:
        from modules.automation_api import AutomationCreateRequest, _conds_to_dicts
    except ImportError:
        c.check("automation_api importable (skipped)", True)
    else:
        req = AutomationCreateRequest(source_ieee="0xpir", conditions=[
            {"type": "offline", "minutes": 30, "ieee": "0xfront", "negate": True},
            {"type": "attribute", "attribute": "temperature", "operator": "rose_by",
             "value": 2, "within": 1800}])
        d = _conds_to_dicts(req.conditions)
        c.check("an offline condition keeps its minutes, device and negation",
                d[0]["type"] == "offline" and d[0]["minutes"] == 30 and d[0]["ieee"] == "0xfront"
                and d[0].get("negate") is True, d)
        c.check("a trend keeps its window", d[1]["within"] == 1800 and d[1]["operator"] == "rose_by", d)


def run() -> Checker:
    c = Checker("test_change_and_offline")
    asyncio.run(_run(c))
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
