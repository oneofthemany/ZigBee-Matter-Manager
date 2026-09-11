"""
Live-value and repeat-step tests.

    python3 tests/swarm/test_templates_and_repeat.py

Message, offer and announcement text can carry {placeholders} the engine fills
when the step runs, and a repeat step runs its steps a number of times, while a
condition holds, or until one does. The repeat work also fixed cancellation,
which never reached steps nested inside If/Else, Together or Repeat. These
drive the real engine with fake devices, a fake message store and a fake media
service.
"""

from __future__ import annotations

import asyncio
import datetime
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker, stub_duckdb  # noqa: E402
from test_multi_source import _attr, _engine, _house, _update  # noqa: E402

stub_duckdb()                    # messages_store imports duckdb at module load
import modules.automation as automation  # noqa: E402
import modules.messages_store as ms  # noqa: E402


class FakeStore:
    def __init__(self):
        self.sent = []

    async def send(self, from_user, to_user, body, source="user"):
        self.sent.append(body)
        return {"success": True, "message": {"id": "m1"}}


class FakeMedia:
    enabled = True

    def __init__(self):
        self.said = []

    def zone_id(self, player_id):
        return None

    async def announce(self, player_id, text, volume=None):
        self.said.append(text)
        return {"success": True}


def _cmd(command):
    return {"type": "command", "target_ieee": "0xlight", "command": command}


def _delay(seconds):
    return {"type": "delay", "seconds": seconds}


def _ic(ieee, attribute, operator, value):
    return {"ieee": ieee, "attribute": attribute, "operator": operator, "value": value}


def _rule(then, source="0xpir", conditions=None, logic="and"):
    return {"name": "t", "source_ieee": source, "cooldown": 0, "condition_logic": logic,
            "conditions": conditions or [_attr("occupancy", "eq", True)],
            "then_sequence": then}


def _doors():
    return [_attr("contact", "eq", False), _attr("contact", "eq", False, "0xback")]


def _results(e, rid):
    return [(t["result"], t.get("level")) for t in e.get_trace_log(rid)]


async def _motion(e, devices):
    await _update(e, devices, "0xpir", occupancy=True)


async def _run(c: Checker) -> None:
    c.section("placeholders fill with live values")
    devices = _house()
    devices["0xlux"].state["illuminance_lux"] = 21.456
    e = _engine(devices)
    rid = e.add_rule(_rule([_cmd("on")]))["rule"]["id"]
    ctx = automation._trigger_ieee.set("0xlux")
    try:
        text = e._render_text("{trigger} reads {trigger.illuminance_lux} lux at {time}", rid)
    finally:
        automation._trigger_ieee.reset(ctx)
    c.check("the trigger's name and a rounded value",
            text.startswith("Hall Lux reads 21.46 lux at "), text)
    c.check("the time", text.endswith(datetime.datetime.now().strftime("%H:%M")), text)
    c.check("any device by id, a boolean as yes/no",
            e._render_text("motion: {0xpir.occupancy}", rid) == "motion: no",
            e._render_text("motion: {0xpir.occupancy}", rid))
    c.check("device ids with colons work", e._render_text("{user::sean.place}", rid) == "home",
            e._render_text("{user::sean.place}", rid))
    c.check("with no update behind it, the trigger is the rule's device",
            e._render_text("{trigger}", rid) == "Hall Motion", e._render_text("{trigger}", rid))
    c.check("a value the trigger doesn't have reads ?",
            e._render_text("{trigger.nope}", rid) == "?")
    kept = "{0xgone.x} and {not a token} and {}"
    c.check("braces that name nothing are left as written", e._render_text(kept, rid) == kept,
            e._render_text(kept, rid))
    c.check("text without braces is untouched", e._render_text("plain", rid) == "plain")

    c.section("a message names the device that fired it")
    devices = _house()
    e = _engine(devices)
    store = FakeStore()
    ms.set_message_store(store)
    e.add_rule(_rule([{"type": "request", "to_user": "sean",
                       "message": "{trigger} opened at {time}"}],
                     source="0xfront", logic="or", conditions=_doors()))
    await _update(e, devices, "0xback", contact=False)
    await asyncio.sleep(0.05)
    c.check("the back door fired a front-door rule, and the message says so",
            store.sent and store.sent[0].startswith("Back Door opened at"), store.sent)

    c.section("an offer's question and its yes-steps both know what fired it")
    devices = _house()
    e = _engine(devices)
    store = FakeStore()
    ms.set_message_store(store)
    e.add_rule(_rule([{"type": "offer", "to_user": "sean", "message": "{trigger} opened — lights?",
                       "accept_steps": [{"type": "request", "to_user": "sean",
                                         "message": "ok, {trigger}"}]}],
                     source="0xfront", logic="or", conditions=_doors()))
    await _update(e, devices, "0xback", contact=False)
    await asyncio.sleep(0.05)
    c.check("the question names it", store.sent and store.sent[0].startswith("Back Door opened"),
            store.sent)
    offers = e.get_offers()
    await e.accept_offer(offers[0]["token"])
    await asyncio.sleep(0.05)
    c.check("so does the step run on yes, later, from another task",
            store.sent[-1] == "ok, Back Door", store.sent)

    c.section("announcements fill placeholders too")
    devices = _house()
    e = _engine(devices)
    media = FakeMedia()
    e.set_media_service_getter(lambda: media)
    e.add_rule(_rule([{"type": "media", "player_id": "p1", "media_action": "announce",
                       "text": "Light level is {0xlux.illuminance_lux}"}]))
    await _motion(e, devices)
    await asyncio.sleep(0.05)
    c.check("the speaker hears the value, not the braces",
            media.said == ["Light level is 100"], media.said)

    c.section("repeat: validation")
    e = _engine(_house())
    v = lambda step: e._validate_sequence([step], "THEN")  # noqa: E731
    body = [_cmd("toggle")]
    c.check("a count of 0 is refused", v({"type": "repeat", "count": 0, "steps": body}) is not None)
    c.check("no steps is refused", v({"type": "repeat", "count": 2, "steps": []}) is not None)
    c.check("an unknown mode is refused",
            v({"type": "repeat", "mode": "forever", "steps": body}) is not None)
    c.check("while without a condition is refused",
            v({"type": "repeat", "mode": "while", "steps": body}) is not None)
    cond = [_ic("0xfront", "contact", "eq", True)]
    c.check("an absurd cap is refused",
            v({"type": "repeat", "mode": "until", "inline_conditions": cond,
               "max_iterations": 10 ** 6, "steps": body}) is not None)
    c.check("a change operator can't be a repeat condition",
            v({"type": "repeat", "mode": "while", "steps": body,
               "inline_conditions": [_ic("0xfront", "contact", "changed", None)]}) is not None)
    c.check("the repeated steps are validated too",
            v({"type": "repeat", "count": 2, "steps": [{"type": "command"}]}) is not None)
    c.check("a good count passes", v({"type": "repeat", "count": 3, "steps": body}) is None,
            v({"type": "repeat", "count": 3, "steps": body}))
    c.check("a good until passes",
            v({"type": "repeat", "mode": "until", "inline_conditions": cond, "steps": body}) is None)

    c.section("repeat N times")
    devices = _house()
    e = _engine(devices)
    rid = e.add_rule(_rule([{"type": "repeat", "mode": "count", "count": 3,
                             "steps": [_cmd("toggle")]}]))["rule"]["id"]
    await _motion(e, devices)
    await asyncio.sleep(0.05)
    c.check("runs three times", devices["0xlight"].sent == ["toggle"] * 3, devices["0xlight"].sent)
    c.check("and says it is done", ("REPEAT_DONE", "INFO") in _results(e, rid), _results(e, rid))
    listed = e.get_rules()[0]
    c.check("the listing names targets inside the repeat",
            listed["then_sequence"][0]["steps"][0].get("target_name") == "Hall Light", listed)

    c.section("repeat while: stops when the condition stops holding")
    devices = _house()
    e = _engine(devices)
    devices["0xfront"].state["contact"] = False              # door open
    e.add_rule(_rule([{"type": "repeat", "mode": "while", "max_iterations": 50,
                       "inline_conditions": [_ic("0xfront", "contact", "eq", False)],
                       "steps": [_cmd("on"), _delay(0.15)]}]))
    await _motion(e, devices)
    await asyncio.sleep(0.4)
    devices["0xfront"].state["contact"] = True                # shut
    await asyncio.sleep(0.3)
    n = len(devices["0xlight"].sent)
    c.check("it ran while the door was open", 2 <= n <= 4, devices["0xlight"].sent)
    await asyncio.sleep(0.3)
    c.check("and not again once it shut", len(devices["0xlight"].sent) == n, devices["0xlight"].sent)

    devices = _house()
    e = _engine(devices)
    e.add_rule(_rule([{"type": "repeat", "mode": "while",
                       "inline_conditions": [_ic("0xfront", "contact", "eq", False)],
                       "steps": [_cmd("on")]}]))
    await _motion(e, devices)
    await asyncio.sleep(0.05)
    c.check("false from the start runs no passes", devices["0xlight"].sent == [],
            devices["0xlight"].sent)

    c.section("repeat until")
    devices = _house()
    e = _engine(devices)
    devices["0xfront"].state["contact"] = False
    rid = e.add_rule(_rule([{"type": "repeat", "mode": "until", "max_iterations": 5,
                             "inline_conditions": [_ic("0xfront", "contact", "eq", True)],
                             "steps": [_cmd("toggle")]}]))["rule"]["id"]
    await _motion(e, devices)
    await asyncio.sleep(0.05)
    c.check("a condition that never comes true stops at the cap",
            devices["0xlight"].sent == ["toggle"] * 5, devices["0xlight"].sent)
    c.check("with a warning", ("REPEAT_DONE", "WARNING") in _results(e, rid), _results(e, rid))

    devices = _house()
    e = _engine(devices)
    e.add_rule(_rule([{"type": "repeat", "mode": "until",
                       "inline_conditions": [_ic("0xfront", "contact", "eq", True)],
                       "steps": [_cmd("toggle")]}]))
    await _motion(e, devices)
    await asyncio.sleep(0.05)
    c.check("already true runs exactly once", devices["0xlight"].sent == ["toggle"],
            devices["0xlight"].sent)

    c.section("a gate inside a repeat ends that pass only")
    devices = _house()
    e = _engine(devices)
    e.add_rule(_rule([{"type": "repeat", "count": 3, "steps": [
        _cmd("toggle"),
        {"type": "condition", "ieee": "0xfront", "attribute": "contact", "operator": "eq",
         "value": False},
        _cmd("on")]}]))
    await _motion(e, devices)
    await asyncio.sleep(0.05)
    c.check("every pass starts, none gets past the gate",
            devices["0xlight"].sent == ["toggle"] * 3, devices["0xlight"].sent)

    c.section("stopping a rule stops steps nested inside it")
    devices = _house()
    e = _engine(devices)
    rid = e.add_rule(_rule([{"type": "repeat", "count": 50,
                             "steps": [_cmd("on"), _delay(0.1)]}]))["rule"]["id"]
    await _motion(e, devices)
    await asyncio.sleep(0.25)
    e.update_rule(rid, {"enabled": False})
    n = len(devices["0xlight"].sent)
    await asyncio.sleep(0.4)
    c.check("disabling stops a repeat mid-run",
            len(devices["0xlight"].sent) == n and n < 50, devices["0xlight"].sent)

    devices = _house()
    e = _engine(devices)
    rid = e.add_rule(_rule([
        {"type": "if_then_else", "condition_logic": "and",
         "inline_conditions": [_ic("0xpir", "occupancy", "eq", True)],
         "then_steps": [_delay(0.3), _cmd("off")], "else_steps": []},
        _cmd("stop")]))["rule"]["id"]
    await _motion(e, devices)
    await asyncio.sleep(0.1)
    e.update_rule(rid, {"enabled": False})
    await asyncio.sleep(0.4)
    c.check("neither the rest of an If/Else nor the step after it runs",
            devices["0xlight"].sent == [], devices["0xlight"].sent)


def run() -> Checker:
    c = Checker("test_templates_and_repeat")
    asyncio.run(_run(c))
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
