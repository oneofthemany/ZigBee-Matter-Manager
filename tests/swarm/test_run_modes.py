"""
Run-mode tests — what a rule does when it fires again while still running.

    python3 tests/swarm/test_run_modes.py

Every rule used to restart: a new transition cancelled whatever was running.
Run modes add queued, single and parallel, and the rewrite also fixes a race in
restart itself — a cancelled run's cleanup untracked the run that replaced it,
so the next restart could not cancel that one. These drive the real engine with
fake devices and short real delays.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker  # noqa: E402
from test_multi_source import _attr, _engine, _house, _update  # noqa: E402

import modules.automation as automation  # noqa: E402


def _cmd(command):
    return {"type": "command", "target_ieee": "0xlight", "command": command}


def _delay(seconds):
    return {"type": "delay", "seconds": seconds}


def _door_rule(then, else_, mode=None):
    data = {"name": "door", "source_ieee": "0xfront", "cooldown": 0,
            "conditions": [_attr("contact", "eq", False)],
            "then_sequence": then, "else_sequence": else_}
    if mode:
        data["run_mode"] = mode
    return data


async def _open(e, devices):
    await _update(e, devices, "0xfront", contact=False)


async def _close(e, devices):
    await _update(e, devices, "0xfront", contact=True)


def _results(e, rid):
    return [t["result"] for t in e.get_trace_log(rid)]


async def _mode_case(mode):
    """Open (THEN: on, 0.4 s, off), then close 0.1 s in (ELSE: toggle)."""
    devices = _house()
    e = _engine(devices)
    r = e.add_rule(_door_rule([_cmd("on"), _delay(0.4), _cmd("off")],
                              [_cmd("toggle")], mode))
    rid = r["rule"]["id"]
    await _open(e, devices)
    await asyncio.sleep(0.1)
    await _close(e, devices)
    early = list(devices["0xlight"].sent)
    await asyncio.sleep(0.6)
    return e, rid, r, early, devices["0xlight"].sent


async def _run(c: Checker) -> None:
    c.section("validation")
    e = _engine(_house())
    bad = e.add_rule(_door_rule([_cmd("on")], [], "loop"))
    c.check("an unknown run mode is refused", not bad["success"] and "run_mode" in bad["error"], bad)
    ok = e.add_rule(_door_rule([_cmd("on")], []))
    c.check("a rule without one is saved as restart",
            ok["success"] and ok["rule"]["run_mode"] == "restart", ok)
    res = e.update_rule(ok["rule"]["id"], {"run_mode": "queued"})
    c.check("update can change it", res["success"] and res["rule"]["run_mode"] == "queued", res)
    res = e.update_rule(ok["rule"]["id"], {"run_mode": "sometimes"})
    c.check("update refuses an unknown one", not res["success"], res)
    c.check("a saved rule with no key reads as restart",
            e._run_mode({"id": "old"}) == "restart")

    c.section("restart (the default): the new run cancels the old")
    e, rid, r, early, sent = await _mode_case(None)
    c.check("closing cancels THEN and runs ELSE at once", early == ["on", "toggle"], early)
    c.check("the cancelled THEN never sends its off", sent == ["on", "toggle"], sent)

    c.section("queued: the new run waits for the old to finish")
    e, rid, r, early, sent = await _mode_case("queued")
    c.check("ELSE does not run while THEN is still going", early == ["on"], early)
    c.check("THEN finishes, then ELSE runs", sent == ["on", "off", "toggle"], sent)
    c.check("the trace shows it queued and dequeued",
            "QUEUED" in _results(e, rid) and "DEQUEUED" in _results(e, rid), _results(e, rid))
    c.check("and nothing is left tracked", rid not in e._rule_runs, e._rule_runs)

    c.section("single: the new run is dropped while the old one runs")
    e, rid, r, early, sent = await _mode_case("single")
    c.check("THEN runs to the end and ELSE never runs", sent == ["on", "off"], sent)
    c.check("the drop is traced", "RUN_SKIPPED" in _results(e, rid), _results(e, rid))
    c.check("the rule's state still follows its conditions",
            e._rule_states.get(rid) == "unmatched", e._rule_states)

    c.section("parallel: both run at once")
    e, rid, r, early, sent = await _mode_case("parallel")
    c.check("ELSE starts while THEN is still going", early == ["on", "toggle"], early)
    c.check("and THEN still finishes", sent == ["on", "toggle", "off"], sent)

    c.section("restart cancels a run that itself replaced a cancelled one")
    # THEN A is replaced by ELSE B, which is replaced by THEN C. A's cleanup
    # used to untrack B, so C could not cancel it and B's "stop" still arrived.
    devices = _house()
    e = _engine(devices)
    rid = e.add_rule(_door_rule([_cmd("on"), _delay(0.5), _cmd("off")],
                                [_cmd("toggle"), _delay(0.5), _cmd("stop")]))["rule"]["id"]
    await _open(e, devices)
    await asyncio.sleep(0.1)
    await _close(e, devices)
    await asyncio.sleep(0.1)
    await _open(e, devices)
    await asyncio.sleep(0.8)
    sent = devices["0xlight"].sent
    c.check("the replaced ELSE never sends its stop", "stop" not in sent, sent)
    c.check("the last THEN runs to the end", sent == ["on", "toggle", "on", "off"], sent)

    c.section("queued and parallel are capped")
    saved_cap = automation.MAX_RULE_RUNS
    automation.MAX_RULE_RUNS = 2
    try:
        devices = _house()
        e = _engine(devices)
        rid = e.add_rule(_door_rule([_cmd("on"), _delay(0.3)],
                                    [_cmd("toggle"), _delay(0.3)], "queued"))["rule"]["id"]
        await _open(e, devices)
        await _close(e, devices)
        listed = e.get_rules(source_ieee="0xfront")[0]
        c.check("the listing reports both live runs",
                listed["_running"] and listed["_runs"] == 2, listed)
        c.check("the stats count them", e.get_stats()["running_sequences"] == 2, e.get_stats())
        await _open(e, devices)
        await _close(e, devices)
        c.check("a third run is refused, not queued", len(e._live_runs(rid)) == 2,
                e._live_runs(rid))
        c.check("and traced as QUEUE_FULL", "QUEUE_FULL" in _results(e, rid), _results(e, rid))
        await asyncio.sleep(0.9)
        c.check("the two that were accepted both ran", devices["0xlight"].sent == ["on", "toggle"],
                devices["0xlight"].sent)
    finally:
        automation.MAX_RULE_RUNS = saved_cap

    c.section("disabling a rule cancels its running and queued runs")
    devices = _house()
    e = _engine(devices)
    rid = e.add_rule(_door_rule([_cmd("on"), _delay(0.4), _cmd("off")],
                                [_cmd("toggle")], "queued"))["rule"]["id"]
    await _open(e, devices)
    await _close(e, devices)
    tasks = e._live_runs(rid)
    e.update_rule(rid, {"enabled": False})
    await asyncio.sleep(0.01)
    c.check("both were live", len(tasks) == 2, tasks)
    # _run_sequence catches its own cancellation to trace it, so the running
    # task ends "finished" rather than "cancelled"; the queued one never started.
    c.check("both are stopped", all(t.done() for t in tasks) and tasks[1].cancelled(), tasks)
    await asyncio.sleep(0.6)
    c.check("neither the rest of THEN nor the queued ELSE ran",
            devices["0xlight"].sent == ["on"], devices["0xlight"].sent)

    c.section("the API carries it")
    try:
        from modules.automation_api import AutomationCreateRequest, AutomationUpdateRequest
    except ImportError:
        c.check("automation_api importable (skipped)", True)
    else:
        c.check("create defaults to restart",
                AutomationCreateRequest(source_ieee="0xfront").run_mode == "restart")
        c.check("create accepts a mode",
                AutomationCreateRequest(source_ieee="0xfront", run_mode="queued")
                .model_dump()["run_mode"] == "queued")
        c.check("update leaves it alone unless sent",
                AutomationUpdateRequest(name="x").run_mode is None)


def run() -> Checker:
    c = Checker("test_run_modes")
    asyncio.run(_run(c))
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
