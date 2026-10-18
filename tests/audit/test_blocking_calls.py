"""blocking_calls: what the loop can reach, and what a hand-off hides."""

from __future__ import annotations

from harness import Checker, tree

import blocking_calls

SRC = {
    "modules/__init__.py": "",
    "modules/telemetry_db.py": """
        def _get_db():
            pass
        def write_row(r):
            _get_db()
        """,
    "modules/worker.py": """
        import asyncio, time, subprocess
        from modules import telemetry_db

        def helper():
            telemetry_db.write_row(1)

        def guarded():
            if _on_event_loop():
                return None
            helper()

        def _on_event_loop():
            return True

        async def direct():
            helper()

        async def handed_off():
            await asyncio.to_thread(helper)
            await asyncio.get_running_loop().run_in_executor(None, helper)
            await asyncio.to_thread(lambda: helper())

        async def asks_first():
            guarded()

        async def sleeps():
            time.sleep(1)

        class Listener:
            def attribute_updated(self, attrid, value):
                self._store(value)
            def _store(self, value):
                subprocess.run(["true"])

        def tick():
            helper()

        def arm(loop):
            loop.call_later(5, tick)
        """,
}


def findings(root):
    funcs = blocking_calls.scan(root)
    return blocking_calls.findings(funcs, blocking_calls.blocking_chains(funcs))


def run() -> Checker:
    c = Checker("blocking_calls")
    with tree(SRC) as root:
        res = findings(root)
        by_fn = {}
        for r in res:
            by_fn.setdefault(r["function"].split(".")[-1], []).append(r)

        c.section("async callers")
        c.check("an async def calling a sync telemetry writer is reported",
                "direct" in by_fn, res)
        c.check("the witness chain runs through the helper to the DB seed",
                by_fn.get("direct") and by_fn["direct"][0]["chain"][0] == "modules.worker.helper"
                and "telemetry_db._get_db" in by_fn["direct"][0]["chain"][-1], by_fn.get("direct"))
        c.check("to_thread, run_in_executor and lambda hand-offs are not reported",
                "handed_off" not in by_fn, by_fn.get("handed_off"))
        c.check("a function that checks _on_event_loop() first is not propagated",
                "asks_first" not in by_fn, by_fn.get("asks_first"))
        c.check("time.sleep in an async def is reported", "sleeps" in by_fn)

        c.section("sync code the loop runs")
        c.check("a zigpy listener reaching subprocess is reported as loop context",
                any(r["kind"] == "loop" for r in by_fn.get("attribute_updated", [])),
                by_fn.get("attribute_updated"))
        c.check("a call_later target reaching the DB is reported as loop context",
                any(r["kind"] == "loop" and r["context"] == "scheduled on the loop"
                    for r in by_fn.get("tick", [])), by_fn.get("tick"))
    return c
