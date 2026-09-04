"""
Offer tests — a message that can act.

    python3 tests/swarm/test_offers.py

An offer asks somebody and runs a stored sequence only if they say yes. The
sequence lives in the engine, never in the message, so what runs is what the
rule said rather than whatever came back over the wire. These checks cover that
boundary, the expiry, and the double-tap.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker, stub_duckdb  # noqa: E402

stub_duckdb()                    # messages_store imports duckdb at module load
import modules.messages_store as ms  # noqa: E402
from modules.automation import (  # noqa: E402
    DEFAULT_OFFER_EXPIRY, MAX_OFFER_EXPIRY, MAX_PENDING_OFFERS, AutomationEngine,
)


class FakeStore:
    """Stands in for the message store; records what was sent."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send(self, from_user, to_user, body, source="user"):
        if self.fail:
            return {"success": False, "error": "store offline"}
        self.sent.append({"from_user": from_user, "to_user": to_user,
                          "body": body, "source": source})
        return {"success": True, "message": {"id": "m1"}}


def _engine(rules=None):
    """A bare engine: no radio, no disk, just the offer machinery."""
    e = AutomationEngine.__new__(AutomationEngine)
    e.rules = rules or [{"id": "r1", "name": "Cooling opportunity"}]
    e._offers = {}
    e._trace_log = []
    e._trace_by_rule = {}
    e._max_trace_entries = 200
    e._max_trace_per_rule = 100
    e._running_sequences = {}
    e._event_emitter = None
    e._stats = {"executions": 0, "execution_successes": 0,
                "execution_failures": 0, "errors": 0}
    return e


COOL = {"type": "command", "target_ieee": "0xplug", "command": "on"}


def _offer_step(**kw):
    step = {"type": "offer", "to_user": "sean",
            "message": "Cool the house down?", "accept_steps": [COOL]}
    step.update(kw)
    return step


def run() -> Checker:
    c = Checker("test_offers")

    c.section("validation refuses an offer that cannot work")
    e = _engine()
    v = lambda step: e._validate_sequence([step], "THEN")  # noqa: E731
    c.check("needs a recipient",
            "to_user" in (v(_offer_step(to_user="")) or ""), v(_offer_step(to_user="")))
    c.check("needs text",
            "text" in (v(_offer_step(message="  ")) or ""), v(_offer_step(message="")))
    c.check("an offer with nothing to run is refused",
            "accept_steps" in (v(_offer_step(accept_steps=[])) or ""),
            v(_offer_step(accept_steps=[])))
    c.check("the accept sequence is validated too",
            v(_offer_step(accept_steps=[{"type": "command"}])) is not None,
            v(_offer_step(accept_steps=[{"type": "command"}])))
    c.check("a bad expiry is refused",
            v(_offer_step(expires_in=0)) is not None)
    c.check("an absurd expiry is refused",
            v(_offer_step(expires_in=MAX_OFFER_EXPIRY + 1)) is not None)
    c.check("a good offer passes", v(_offer_step()) is None, v(_offer_step()))

    c.section("asking")
    e = _engine()
    store = FakeStore()
    ms.set_message_store(store)
    asyncio.run(e._step_offer("r1", _offer_step(), "[T 1/1]"))
    c.check("a message went out", len(store.sent) == 1, store.sent)
    c.check("tagged so a client can render an accept control",
            store.sent[0]["source"] == "automation_offer", store.sent[0])
    offers = e.get_offers()
    c.check("one offer is pending", len(offers) == 1, offers)
    c.check("it names the rule", offers[0]["rule_name"] == "Cooling opportunity")
    c.check("the action is not exposed to the client",
            "accept_steps" not in offers[0], offers[0])
    c.check("filtering by recipient works", len(e.get_offers("sean")) == 1)
    c.check("and excludes other people", e.get_offers("charlie") == [])

    c.section("nothing is left pending if nobody was told")
    e = _engine()
    ms.set_message_store(FakeStore(fail=True))
    asyncio.run(e._step_offer("r1", _offer_step(), "[T 1/1]"))
    c.check("no orphan offer", e.get_offers() == [], e.get_offers())

    c.section("accepting runs what the rule stored")
    e = _engine()
    ms.set_message_store(FakeStore())
    ran = []

    async def _fake_seq(rule_id, rule_name, steps, path, depth=0):
        ran.append((rule_id, path, steps))

    e._run_sequence = _fake_seq
    asyncio.run(e._step_offer("r1", _offer_step(), "[T 1/1]"))
    token = e.get_offers()[0]["token"]
    result = asyncio.run(e.accept_offer(token))
    asyncio.run(asyncio.sleep(0))          # let the task start
    c.check("accepted", result["success"] is True, result)
    c.check("the sequence ran", len(ran) == 1, ran)
    c.check("with the rule's own steps", ran[0][2] == [COOL], ran[0])
    c.check("tagged as the accept path", ran[0][1] == "ACCEPT", ran[0])
    c.check("the offer is gone", e.get_offers() == [])

    c.section("a double tap cannot run the action twice")
    again = asyncio.run(e.accept_offer(token))
    c.check("refused", again["success"] is False, again)
    c.check("with a reason", "expired" in again["error"] or "answered" in again["error"],
            again["error"])
    c.check("and nothing ran again", len(ran) == 1, ran)

    c.section("declining runs nothing")
    e = _engine()
    ms.set_message_store(FakeStore())
    ran = []
    e._run_sequence = _fake_seq
    asyncio.run(e._step_offer("r1", _offer_step(), "[T 1/1]"))
    token = e.get_offers()[0]["token"]
    c.check("declined", e.decline_offer(token)["success"] is True)
    c.check("nothing ran", ran == [], ran)
    c.check("the offer is gone", e.get_offers() == [])
    c.check("declining twice is refused",
            e.decline_offer(token)["success"] is False)

    c.section("an offer addressed to somebody else cannot be answered")
    e = _engine()
    ms.set_message_store(FakeStore())
    e._run_sequence = _fake_seq
    asyncio.run(e._step_offer("r1", _offer_step(), "[T 1/1]"))
    token = e.get_offers()[0]["token"]
    r = asyncio.run(e.accept_offer(token, as_user="charlie"))
    c.check("refused", r["success"] is False, r)
    c.check("and still pending", len(e.get_offers()) == 1)
    c.check("the right person can answer",
            asyncio.run(e.accept_offer(token, as_user="sean"))["success"] is True)

    c.section("offers lapse rather than accumulate")
    e = _engine()
    ms.set_message_store(FakeStore())
    asyncio.run(e._step_offer("r1", _offer_step(expires_in=1), "[T 1/1]"))
    c.check("pending while fresh", len(e.get_offers()) == 1)
    list(e._offers.values())[0]["expires_at"] = time.time() - 1
    c.check("dropped once stale", e.get_offers() == [], e.get_offers())
    c.check("and cannot then be accepted",
            asyncio.run(e.accept_offer("whatever"))["success"] is False)

    c.section("a rule re-firing replaces its own question")
    e = _engine()
    ms.set_message_store(FakeStore())
    asyncio.run(e._step_offer("r1", _offer_step(), "[T 1/1]"))
    first = e.get_offers()[0]["token"]
    asyncio.run(e._step_offer("r1", _offer_step(), "[T 1/1]"))
    offers = e.get_offers()
    c.check("still only one", len(offers) == 1, offers)
    c.check("and it is the newer one", offers[0]["token"] != first)

    c.section("a different rule asking the same person is a separate question")
    e = _engine(rules=[{"id": "r1", "name": "A"}, {"id": "r2", "name": "B"}])
    ms.set_message_store(FakeStore())
    asyncio.run(e._step_offer("r1", _offer_step(), "[T 1/1]"))
    asyncio.run(e._step_offer("r2", _offer_step(), "[T 1/1]"))
    c.check("both stand", len(e.get_offers()) == 2, e.get_offers())

    c.section("unanswered offers cannot grow without bound")
    e = _engine(rules=[{"id": f"r{i}", "name": f"R{i}"}
                       for i in range(MAX_PENDING_OFFERS + 10)])
    ms.set_message_store(FakeStore())
    for i in range(MAX_PENDING_OFFERS + 10):
        asyncio.run(e._step_offer(f"r{i}", _offer_step(), "[T 1/1]"))
    c.check("capped", len(e.get_offers()) <= MAX_PENDING_OFFERS,
            len(e.get_offers()))
    c.check("the newest survived",
            any(o["rule_id"] == f"r{MAX_PENDING_OFFERS + 9}" for o in e.get_offers()))

    c.section("a missing message store is survivable")
    e = _engine()
    ms.set_message_store(None)
    asyncio.run(e._step_offer("r1", _offer_step(), "[T 1/1]"))
    c.check("no offer, no crash", e.get_offers() == [])

    return c


if __name__ == "__main__":
    checker = run()
    print(f"\n{checker.passed} passed, {len(checker.failures)} failed")
    sys.exit(1 if checker.failures else 0)
