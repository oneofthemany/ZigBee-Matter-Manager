"""
Run-now, webhook and startup triggers, snapshot/restore, date conditions, rule
state across restarts, and single-rule import.

    python3 tests/swarm/test_final_gaps.py

Driven through the real engine with fake devices, a fake message store and a
fake group manager; the import, webhook and run routes through Starlette's
TestClient where fastapi is installed.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker, stub_duckdb  # noqa: E402
from test_multi_source import Dev, _attr, _engine, _house, _update  # noqa: E402

stub_duckdb()                    # messages_store imports duckdb at module load
import modules.messages_store as ms  # noqa: E402
from modules.automation import AutomationEngine  # noqa: E402


class FakeStore:
    def __init__(self):
        self.sent = []

    async def send(self, from_user, to_user, body, source="user"):
        self.sent.append(body)
        return {"success": True, "message": {"id": "m1"}}


class ValDev(Dev):
    """A device that records the value of each command, not just its name."""

    async def send_command(self, command, value=None, endpoint_id=None):
        self.sent.append((command, value))
        return {"success": True}


class FakeGroups:
    def __init__(self, groups):
        self.groups = groups


def _cmd(command, target="0xlight"):
    return {"type": "command", "target_ieee": target, "command": command}


def _rule(conditions, then, source="0xpir", **extra):
    data = {"name": "t", "source_ieee": source, "cooldown": 0,
            "conditions": conditions, "then_sequence": then}
    data.update(extra)
    return data


def _names(devices):
    return lambda: {k: d.friendly_name for k, d in devices.items()}


def _results(e, rid):
    return [t["result"] for t in e.get_trace_log(rid)]


async def _settle():
    await asyncio.sleep(0.03)


async def _run(c: Checker) -> None:
    motion = [_attr("occupancy", "eq", True)]

    c.section("run now")
    devices = _house()
    e = _engine(devices)
    light = devices["0xlight"]
    rid = e.add_rule(_rule(motion, [_cmd("on")], else_sequence=[_cmd("off")]))["rule"]["id"]
    res = e.run_now(rid)
    await _settle()
    c.check("runs THEN on demand", res["success"] and light.sent == ["on"], (res, light.sent))
    c.check("without touching the rule's state", rid not in e._rule_states, e._rule_states)
    e.run_now(rid, "else")
    await _settle()
    c.check("and ELSE when asked", light.sent == ["on", "off"], light.sent)
    c.check("traced as a manual run", "MANUAL_RUN" in _results(e, rid), _results(e, rid))
    c.check("an unknown rule is refused", not e.run_now("auto_nope")["success"])
    bare = e.add_rule(_rule(motion, [_cmd("toggle")]))["rule"]["id"]
    res = e.run_now(bare, "else")
    c.check("asking for steps a rule doesn't have is refused",
            not res["success"] and "no ELSE" in res["error"], res)
    e.update_rule(rid, {"enabled": False})
    e.run_now(rid)
    await _settle()
    c.check("a disabled rule can still be tested", light.sent == ["on", "off", "on"], light.sent)

    c.section("webhook trigger")
    devices = _house()
    e = _engine(devices)
    store = FakeStore()
    ms.set_message_store(store)
    r = e.add_rule(_rule([{"type": "webhook"}],
                         [{"type": "request", "to_user": "sean", "message": "Hello {webhook.who}"}],
                         source="__time__"))
    c.check("a webhook rule needs no device", r["success"], r)
    hook = r["rule"]["conditions"][0]["hook"]
    c.check("and is given an id if it has none",
            len(hook) == 32 and all(ch in "0123456789abcdef" for ch in hook), hook)
    res = e.fire_webhook(hook, {"who": "Sean"})
    await _settle()
    c.check("calling it fires the rule, with the body in the message",
            res["success"] and store.sent == ["Hello Sean"], (res, store.sent))
    e.fire_webhook(hook, {"who": "Charlie"})
    await _settle()
    c.check("and it re-arms for the next call", store.sent == ["Hello Sean", "Hello Charlie"],
            store.sent)
    c.check("a webhook nobody listens on is refused", not e.fire_webhook("0" * 32)["success"])
    kept = e.add_rule(_rule([{"type": "webhook", "hook": "garage-door-01"}], [_cmd("on")],
                            source="__time__"))
    c.check("a chosen id is kept",
            kept["success"] and kept["rule"]["conditions"][0]["hook"] == "garage-door-01", kept)
    bad = e.add_rule(_rule([{"type": "webhook", "hook": "a b"}], [_cmd("on")], source="__time__"))
    c.check("an id with spaces is refused", not bad["success"], bad)
    rid = e.add_rule(_rule([{"type": "webhook", "hook": "motion-hook-1"}], [_cmd("toggle")]))["rule"]["id"]
    await _update(e, devices, "0xpir", occupancy=True)
    await _settle()
    c.check("the source device updating is not a webhook call",
            "toggle" not in devices["0xlight"].sent and e.get_trace_log(rid) == [],
            e.get_trace_log(rid))
    e.update_rule(rid, {"enabled": False})
    c.check("a disabled rule doesn't answer its webhook",
            not e.fire_webhook("motion-hook-1")["success"])

    c.section("startup trigger")
    devices = _house()
    e = _engine(devices)
    e.add_rule(_rule([{"type": "startup"}], [_cmd("on")], source="__time__"))
    await _update(e, devices, "0xpir", occupancy=True)
    await _settle()
    c.check("no device update fires it", devices["0xlight"].sent == [], devices["0xlight"].sent)
    e._evaluate_startup_rules()
    await _settle()
    c.check("the hub starting does", devices["0xlight"].sent == ["on"], devices["0xlight"].sent)

    c.section("snapshot and restore")
    devices = _house()
    devices["0xlight"] = ValDev("Hall Light", state="ON", brightness=127, color_temp=370)
    devices["0xlamp"] = ValDev("Lamp", state="OFF", brightness=200)
    e = _engine(devices)
    rid = e.add_rule(_rule(motion, [
        {"type": "snapshot", "targets": ["0xlight", "0xlamp"], "name": "before"},
        _cmd("off"),
        {"type": "restore", "name": "before"}]))["rule"]["id"]
    await _update(e, devices, "0xpir", occupancy=True)
    await _settle()
    c.check("a light that was on comes back on, at its brightness and colour",
            devices["0xlight"].sent == [("off", None), ("on", None), ("brightness", 50),
                                        ("color_temp", 2703)], devices["0xlight"].sent)
    c.check("a light that was off goes back off", devices["0xlamp"].sent == [("off", None)],
            devices["0xlamp"].sent)
    c.check("both are traced", {"SNAPSHOT", "RESTORE"} <= set(_results(e, rid)), _results(e, rid))
    skip = e.add_rule(_rule([_attr("contact", "eq", False)], [{"type": "restore", "name": "never"}],
                            source="0xfront"))["rule"]["id"]
    await _update(e, devices, "0xfront", contact=False)
    await _settle()
    c.check("restoring something never remembered does nothing, and says so",
            "RESTORE_SKIP" in _results(e, skip) and devices["0xlamp"].sent == [("off", None)],
            _results(e, skip))
    e._get_group_manager = lambda: FakeGroups({3: {"name": "Downstairs", "members": ["0xlamp"]}})
    e._step_snapshot("rX", {"type": "snapshot", "targets": ["group:3"], "name": "g"}, "[T]")
    c.check("a group is remembered as the devices in it", list(e._snapshots[("rX", "g")]) == ["0xlamp"],
            e._snapshots.get(("rX", "g")))
    c.check("a kelvin colour is passed through as it is",
            AutomationEngine._restore_commands({"on": True, "color_temp": 2700})
            == [("on", None), ("color_temp", 2700)])
    c.check("a cover goes back to its position",
            AutomationEngine._restore_commands({"position": 40}) == [("position", 40)])
    c.check("a snapshot needs devices",
            e._validate_sequence([{"type": "snapshot", "targets": []}], "THEN") is not None)

    c.section("date conditions")
    m = AutomationEngine._date_matches
    xmas = {"from": "12-01", "to": "01-06"}
    c.check("a yearly range wrapping new year includes Christmas",
            m(xmas, datetime.date(2026, 12, 25)))
    c.check("and early January", m(xmas, datetime.date(2027, 1, 3)))
    c.check("but not February", not m(xmas, datetime.date(2027, 2, 1)))
    dated = {"from": "2026-12-20", "to": "2026-12-31"}
    c.check("particular dates hold only that year",
            m(dated, datetime.date(2026, 12, 25)) and not m(dated, datetime.date(2027, 12, 25)))
    c.check("NOT inverts it", not m({**xmas, "negate": True}, datetime.date(2026, 12, 25)))
    e = _engine(_house())
    v = e._validate_conditions
    c.check("mixed forms are refused",
            v([{"type": "date", "from": "12-01", "to": "2026-01-06"}]) is not None)
    c.check("dates the wrong way round are refused",
            v([{"type": "date", "from": "2026-12-31", "to": "2026-12-01"}]) is not None)
    c.check("month 13 is refused", v([{"type": "date", "from": "13-01", "to": "12-01"}]) is not None)
    c.check("29 February is a day", v([{"type": "date", "from": "02-29", "to": "03-01"}]) is None)
    today = datetime.date.today().strftime("%m-%d")
    devices = _house()
    e = _engine(devices)
    e.add_rule(_rule(motion + [{"type": "date", "from": today, "to": today}], [_cmd("on")]))
    blocked = e.add_rule(_rule(motion, [_cmd("toggle")], prerequisites=[
        {"type": "date", "from": today, "to": today, "negate": True}]))
    c.check("a date prerequisite is accepted", blocked["success"], blocked)
    await _update(e, devices, "0xpir", occupancy=True)
    await _settle()
    c.check("a date condition covering today lets the rule fire", "on" in devices["0xlight"].sent,
            devices["0xlight"].sent)
    c.check("a date prerequisite excluding today stops it", "toggle" not in devices["0xlight"].sent,
            devices["0xlight"].sent)
    c.check("a date range is re-checked at midnight",
            "00:00" in e._rule_temporal_boundaries({"conditions": [{"type": "date", "from": today, "to": today}]}))

    c.section("rule state survives a restart")
    devices = _house()
    e1 = _engine(devices)
    rid = e1.add_rule(_rule(motion, [_cmd("on")]))["rule"]["id"]
    await _update(e1, devices, "0xpir", occupancy=True)
    await _settle()
    e1._save_rule_states()
    path = e1._state_file()
    with open(path) as f:
        saved = json.load(f)
    c.check("the matched state is written beside the rules",
            saved["states"].get(rid) == "matched", saved)
    e2 = AutomationEngine(lambda: devices, _names(devices))
    c.check("a new engine picks it up", e2._rule_states.get(rid) == "matched", e2._rule_states)
    await _update(e2, devices, "0xpir", occupancy=True)
    await _settle()
    c.check("so a rule already true does not run THEN again", devices["0xlight"].sent == ["on"],
            devices["0xlight"].sent)
    await _update(e2, devices, "0xpir", occupancy=False)
    await _update(e2, devices, "0xpir", occupancy=True)
    await _settle()
    c.check("while a real new transition still fires", devices["0xlight"].sent == ["on", "on"],
            devices["0xlight"].sent)
    with open(path, "w") as f:
        json.dump({"states": {"auto_gone": "matched", rid: "unmatched"}}, f)
    e3 = AutomationEngine(lambda: devices, _names(devices))
    c.check("a state for a rule that no longer exists is ignored",
            "auto_gone" not in e3._rule_states and e3._rule_states.get(rid) == "unmatched",
            e3._rule_states)

    c.section("import")
    devices = _house()
    e = _engine(devices)
    rid = e.add_rule(_rule(motion, [_cmd("on")], run_mode="queued", name="Hall"))["rule"]["id"]
    exported = e.get_rules()[0]                  # the listing's shape, names and all
    res = e.import_rules(exported)
    c.check("a downloaded rule imports", res["imported"] == 1, res)
    new = e.get_rule(res["results"][0]["rule_id"])
    c.check("as a new rule with its settings",
            new["id"] != rid and new["run_mode"] == "queued" and new["name"] == "Hall", new)
    c.check("without the listing's fields or display names",
            not any(k.startswith("_") for k in new) and "source_name" not in new
            and "sources" not in new and "target_name" not in json.dumps(new), new)
    res = e.import_rules({"rules": [
        e.get_rule(rid),
        {"source_ieee": "0xgone", "conditions": motion, "then_sequence": [_cmd("on")]},
        "junk"]})
    c.check("a batch imports what it can and reports the rest",
            res["imported"] == 1 and len(res["results"]) == 3
            and "not found" in (res["results"][1].get("error") or "")
            and not res["results"][2]["success"], res)
    c.check("nothing to import is refused", not e.import_rules([])["success"])
    c.check("nor is a string", not e.import_rules("x")["success"])

    c.section("the API carries the new types and routes")
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from modules.automation_api import (AutomationCreateRequest, _conds_to_dicts,
                                            _prereqs_to_dicts, register_automation_routes)
    except ImportError:
        c.check("fastapi importable (skipped)", True)
    else:
        req = AutomationCreateRequest(
            source_ieee="__time__",
            conditions=[{"type": "webhook", "hook": "garage-door-01"}, {"type": "startup"},
                        {"type": "date", "from": "12-01", "to": "01-06", "negate": True}],
            prerequisites=[{"type": "date", "from": "12-01", "to": "01-06"}])
        d = _conds_to_dicts(req.conditions)
        c.check("webhook, startup and date conditions keep their fields",
                d[0] == {"type": "webhook", "hook": "garage-door-01"} and d[1] == {"type": "startup"}
                and d[2]["from"] == "12-01" and d[2]["negate"] is True, d)
        p = _prereqs_to_dicts(req.prerequisites)
        c.check("a date prerequisite keeps its range", p[0]["type"] == "date" and p[0]["to"] == "01-06", p)
        app = FastAPI()
        register_automation_routes(app, lambda: e)
        client = TestClient(app)
        r = client.post("/api/automations/import", json=e.get_rule(rid))
        c.check("POST /import adds a rule", r.status_code == 200 and r.json()["imported"] == 1, r.text)
        r = client.post("/api/automations/import", json=[])
        c.check("and refuses an empty file", r.status_code == 400, r.text)
        r = client.post("/api/automations/webhook/" + "0" * 32, json={})
        c.check("a webhook nobody listens on is a 404", r.status_code == 404, r.text)
        r = client.post("/api/automations/auto_nope/run")
        c.check("running an unknown rule is a 404", r.status_code == 404, r.text)


def run() -> Checker:
    c = Checker("test_final_gaps")
    asyncio.run(_run(c))
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
