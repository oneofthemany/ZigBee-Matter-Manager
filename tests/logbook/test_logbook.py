"""
The logbook end to end (modules/logbook.py) through the real automation
engine: a sensor fires a rule that switches a light, and each log line is
traced back to what caused it and forward to what it did. Real DuckDB; skipped
where it isn't installed.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from harness import Checker


class Clock:
    def __init__(self):
        import time
        self.offset = 0.0
        self._time = time.time

    def __call__(self):
        return self._time() + self.offset


def run() -> Checker:
    c = Checker("logbook")
    try:
        import duckdb  # noqa: F401
    except ImportError:
        print("\n  skipped (duckdb not installed)")
        return c

    from modules import automation, logbook as L
    from modules.automation import AutomationEngine, current_chain

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            automation.DATA_FILE = os.path.join(tmp, "automations.json")
            clock = Clock()
            lines = []

            class Dev:
                def __init__(self, ieee, name, **state):
                    self.ieee, self.friendly_name, self.state = ieee, name, dict(state)

                def is_available(self):
                    return True

                async def send_command(self, command, value=None, endpoint_id=None):
                    # What core does when the device reports back: a log line, then evaluate.
                    new = {"state": "ON" if command == "on" else "OFF"}
                    self.state.update(new)
                    lines.append(lb.record_log({"level": "INFO", "category": "attribute_update",
                                                "ieee": self.ieee, "device_name": self.friendly_name,
                                                "attribute": "state", "value": new["state"],
                                                "message": f"[{self.ieee}] ({self.friendly_name}) state={new['state']}"}))
                    asyncio.get_running_loop().create_task(engine.evaluate(self.ieee, new))
                    return {"success": True}

            devices = {"0xpir": Dev("0xpir", "Hall PIR", occupancy=False),
                       "0xlux": Dev("0xlux", "Hall Lux", illuminance=500),
                       "0xlight": Dev("0xlight", "Hall <b>Light</b>", state="OFF"),
                       "worker::mode": Dev("worker::mode", "House Mode", value="home")}
            engine = AutomationEngine(lambda: devices, lambda: {k: d.friendly_name for k, d in devices.items()})
            lb = L.Logbook(db_path=Path(tmp) / "logbook.duckdb", current_chain=current_chain, clock=clock,
                           get_names=lambda: {k: d.friendly_name for k, d in devices.items()})
            await lb.start()
            engine.add_state_listener(lb.observe)
            engine.add_trace_listener(lb.on_trace)

            def cmd(c_):
                return [{"type": "command", "target_ieee": "0xlight", "command": c_}]
            fires = engine.add_rule({"name": "Night path", "source_ieee": "0xpir", "cooldown": 0,
                                     "conditions": [{"type": "attribute", "attribute": "occupancy",
                                                     "operator": "eq", "value": True}],
                                     "then_sequence": cmd("on")})
            engine.add_rule({"name": "Only when dark", "source_ieee": "0xpir", "cooldown": 0,
                             "conditions": [{"type": "attribute", "attribute": "occupancy", "operator": "eq", "value": True},
                                            {"type": "attribute", "attribute": "illuminance", "operator": "lt",
                                             "value": 20, "ieee": "0xlux"}],
                             "then_sequence": cmd("on")})
            c.check("the test rules were accepted", fires.get("success"), fires)

            async def zigbee_update(ieee, **changed):
                devices[ieee].state.update(changed)
                k, v = next(iter(changed.items()))
                line = lb.record_log({"level": "INFO", "category": "attribute_update", "ieee": ieee,
                                      "device_name": devices[ieee].friendly_name, "attribute": k, "value": v,
                                      "message": f"[{ieee}] ({devices[ieee].friendly_name}) {k}={v}"})
                await engine.evaluate(ieee, changed)
                await asyncio.sleep(0.05)
                return line

            try:
                c.section("a line gets an id and is kept")
                motion = await zigbee_update("0xpir", occupancy=True)
                c.check("every log line gets an event id and a time", motion["event_id"] and motion["ts"])
                c.check("recording the same line twice doesn't duplicate it",
                        lb.record_log(motion)["event_id"] == motion["event_id"])
                rows = await lb.events(limit=10)
                c.check("history comes back newest first",
                        [r["ts"] for r in rows] == sorted((r["ts"] for r in rows), reverse=True)
                        and any(r["id"] == motion["event_id"] for r in rows), rows)
                c.check("rule firings and command results are in the same stream",
                        any(r["category"] == "automation" and "Night path" in r["message"] for r in rows),
                        [r["message"] for r in rows])

                c.section("tracing the line that triggered a rule")
                t = await lb.trace(motion["event_id"])
                rules = {r["rule_name"]: r for r in t["chain"]["rules"]}
                c.check("the line is linked to the evaluation it started",
                        t["chain"] and t["chain"]["trigger_ieee"] == "0xpir"
                        and t["chain"]["changed"] == {"occupancy": True}, t["chain"])
                c.check("the rule that fired is shown as fired, with its steps",
                        rules["Night path"]["fired"]
                        and any(e["result"] == "SUCCESS" for e in rules["Night path"]["entries"]), rules["Night path"])
                c.check("the rule that didn't fire says why",
                        not rules["Only when dark"]["fired"] and rules["Only when dark"]["outcome"] == "NO_MATCH",
                        rules["Only when dark"])
                why = next(e for e in rules["Only when dark"]["entries"] if e["result"] == "NO_MATCH")
                c.check("…down to which condition failed",
                        why["detail"] and len(why["detail"]["conditions"]) == 2
                        and [x["result"] for x in why["detail"]["conditions"]] == ["PASS", "FAIL"]
                        and why["detail"]["conditions"][1]["actual_raw"] == "500",
                        why["detail"])
                c.check("what the rule changed is listed as its effect",
                        [e["ieee"] for e in t["chain"]["effects"]] == ["0xlight"], t["chain"]["effects"])

                c.section("tracing the line the rule caused")
                light_line = next(x for x in lines if x["ieee"] == "0xlight")
                t2 = await lb.trace(light_line["event_id"])
                c.check("the light's line is attributed to the rule, and to what fired it",
                        t2["cause"]["kind"] == "rule" and t2["cause"]["rules"] == ["Night path"]
                        and t2["cause"]["chain"]["trigger_name"] == "Hall PIR", t2["cause"])
                c.check("a name with markup is stored as text, for the page to escape",
                        "<b>" in t2["event"]["device_name"])

                c.section("a person's command")
                await asyncio.sleep(0.01)
                lb.note_user_command("0xlight", "alex")
                await devices["0xlight"].send_command("off")
                await asyncio.sleep(0.05)
                t3 = await lb.trace(lines[-1]["event_id"])
                c.check("a change someone made from the UI names them",
                        t3["cause"] == {"kind": "user", "user": "alex"}, t3["cause"])
                clock.offset += L.CAUSE_TTL_S + 1
                await devices["0xlight"].send_command("on")
                await asyncio.sleep(0.05)
                t4 = await lb.trace(lines[-1]["event_id"])
                c.check("a later change isn't blamed on an old command", t4["cause"] is None, t4["cause"])

                c.section("other sources")
                devices["worker::mode"].state["value"] = "away"
                await engine.evaluate("worker::mode", {"value": "away"})
                await asyncio.sleep(0.05)
                rows = await lb.events(q="house mode")
                c.check("a worker's change gets its own log line, already tied to its chain",
                        rows and rows[0]["attribute"] == "value" and rows[0]["chain_id"], rows[:1])
                rid = fires["rule"]["id"] if isinstance(fires.get("rule"), dict) else fires.get("id")
                engine.run_now(rid)
                await asyncio.sleep(0.05)
                rows = await lb.events(limit=20)
                manual = next(r for r in rows if r["category"] == "automation" and "ON" not in r["message"]
                              and r["chain_id"] not in (t["chain"]["id"],))
                tm = await lb.trace(manual["id"])
                c.check("a manual run has a chain too, with nothing claiming to have triggered it",
                        tm["chain"] and tm["chain"]["trigger_ieee"] is None, tm["chain"])

                c.section("paging, search and retention")
                first = await lb.events(limit=2)
                older = await lb.events(limit=2, before=first[-1]["ts"])
                c.check("`before` pages back without repeating",
                        older and not {r["id"] for r in first} & {r["id"] for r in older})
                c.check("search matches message text, case-insensitively",
                        all("hall pir" in r["message"].lower() for r in await lb.events(q="HALL PIR")))
                c.check("a quote in the search is just text", await lb.events(q="'; DROP TABLE events; --") == [])
                await lb.flush()
                await lb._run(lb._prune, clock() + 1)
                c.check("lines older than the retention are pruned", await lb.events() == [])
                c.check("and their traces answer 'gone'", await lb.trace(motion["event_id"]) is None)
            finally:
                await lb.stop()

    asyncio.run(scenario())
    return c
