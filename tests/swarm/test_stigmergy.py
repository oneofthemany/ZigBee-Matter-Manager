"""
Stigmergy tests — pattern schema, validation, and the shipped set.

    python3 tests/swarm/test_stigmergy.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker  # noqa: E402

from modules.swarm.capabilities import CAPABILITIES, PARAMS  # noqa: E402
from modules.swarm.stigmergy import (  # noqa: E402
    StigmergyStore, literal_slot_refs, validate,
)

REPO = Path(__file__).resolve().parents[2]
BUNDLED = REPO / "modules" / "swarm" / "patterns"


def _minimal():
    return {
        "id": "t", "title": "T", "scope": "room",
        "slots": {
            "a": {"role": "trigger", "offer": "presence:detected"},
            "b": {"role": "action", "offer": "on_off:turn_on"},
        },
        "emits": {"source": "a", "conditions": ["a"], "then": ["b"]},
    }


def run() -> Checker:
    c = Checker("test_stigmergy")

    c.section("the shipped set loads clean")
    store = StigmergyStore(bundled_dir=str(BUNDLED), user_dir="/nonexistent")
    c.check("no load errors", store.errors == [], store.errors)
    c.check("patterns loaded", len(store.all()) >= 20, len(store.all()))
    ids = [p["id"] for p in store.all()]
    c.check("ids are unique", len(ids) == len(set(ids)))
    c.check("every pattern is marked bundled",
            all(p["source"] == "bundled" for p in store.all()))
    c.check("lookup by id works", store.get(ids[0]) is not None)
    c.check("unknown id returns None", store.get("nope") is None)

    c.section("every shipped pattern references a real offer")
    for p in store.all():
        for name, spec in p["slots"].items():
            cap, offer_id = spec["offer"].split(":", 1)
            pool = CAPABILITIES[cap][spec["role"] + "s"]
            ok = any(o["id"] == offer_id or o.get("expand") for o in pool)
            if not c.check(f"{p['id']}.{name} -> {spec['offer']}", ok):
                break

    c.section("validation rejects what would fail at save")
    bad = _minimal(); bad["emits"]["source"] = "b"
    c.check("source must be a trigger slot",
            any("must have role 'trigger'" in e for e in validate(bad)), validate(bad))

    bad = _minimal(); bad["emits"]["source"] = "zz"
    c.check("source must be a real slot",
            any("is not a slot" in e for e in validate(bad)), validate(bad))

    bad = _minimal(); bad["slots"]["a"]["offer"] = "presence:nope"
    c.check("unknown offer id caught",
            any("no trigger 'nope'" in e for e in validate(bad)), validate(bad))

    bad = _minimal(); bad["slots"]["a"]["offer"] = "nosuchcap:x"
    c.check("unknown capability caught",
            any("unknown capability" in e for e in validate(bad)), validate(bad))

    bad = _minimal(); bad["emits"]["then"] = []
    c.check("a pattern with no action is rejected",
            any("at least one action" in e for e in validate(bad)), validate(bad))

    bad = _minimal(); bad["slots"]["c"] = {"role": "condition",
                                           "offer": "illuminance:is_dark"}
    c.check("an unreferenced slot is rejected",
            any("never used" in e for e in validate(bad)), validate(bad))

    bad = _minimal(); bad["slots"]["a"]["params"] = {"nope": 1}
    c.check("an unknown slot parameter is caught",
            any("unknown parameter" in e for e in validate(bad)), validate(bad))

    good_slot = _minimal(); good_slot["slots"]["a"]["params"] = {"dark_lux": 30}
    c.check("a known slot parameter is accepted",
            validate(good_slot) == [], validate(good_slot))

    bad = _minimal(); bad["params"] = {"not_a_param": 1}
    c.check("unknown parameter caught",
            any("unknown parameter" in e for e in validate(bad)), validate(bad))

    bad = _minimal(); bad["slots"]["a"]["optional"] = True
    c.check("the source slot cannot be optional",
            any("cannot be optional" in e for e in validate(bad)), validate(bad))

    bad = _minimal(); bad["emits"]["condition_logic"] = "xor"
    c.check("condition_logic is checked",
            any("condition_logic" in e for e in validate(bad)), validate(bad))

    bad = _minimal(); bad["slots"]["b"]["prefer"] = "same_device"
    c.check("prefer needs prefer_slot",
            any("needs 'prefer_slot'" in e for e in validate(bad)), validate(bad))

    bad = _minimal(); bad["scope"] = "planet"
    c.check("scope is checked", any("scope must be" in e for e in validate(bad)))

    c.check("a valid pattern has no errors", validate(_minimal()) == [], validate(_minimal()))

    c.section("literal steps may reference slots")
    good = _minimal()
    good["slots"]["who"] = {"role": "action", "offer": "notify:message"}
    good["emits"]["then"] = ["b", {"type": "request", "to_user": "$who",
                                   "message": "$trigger_device did something"}]
    c.check("a slot used only inside a literal counts as used",
            validate(good) == [], validate(good))
    c.check("reserved placeholders are not slots",
            literal_slot_refs({"m": "$trigger_device"}) == [])
    c.check("slot refs are found in nested values",
            literal_slot_refs({"a": ["$who", {"b": "$other"}]}) == ["who", "other"])

    bad = copy.deepcopy(good)
    bad["emits"]["then"] = ["b", {"type": "request", "to_user": "$ghost",
                                  "message": "x"}]
    c.check("a literal referencing an unknown slot is rejected",
            any("$ghost" in e for e in validate(bad)), validate(bad))

    c.section("a bad file does not take the store down")
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "broken.json"), "w") as f:
            f.write("{not json")
        with open(os.path.join(tmp, "good.json"), "w") as f:
            json.dump(_minimal(), f)
        s2 = StigmergyStore(bundled_dir=tmp, user_dir="/nonexistent")
        c.check("the valid pattern still loaded", len(s2.all()) == 1, len(s2.all()))
        c.check("the broken file is reported", len(s2.errors) == 1, s2.errors)

    c.section("the user directory overrides by id")
    with tempfile.TemporaryDirectory() as user:
        override = _minimal()
        override["id"] = store.all()[0]["id"]
        override["title"] = "Overridden"
        with open(os.path.join(user, "mine.json"), "w") as f:
            json.dump(override, f)
        s3 = StigmergyStore(bundled_dir=str(BUNDLED), user_dir=user)
        got = s3.get(override["id"])
        c.check("user wins", got["title"] == "Overridden", got["title"])
        c.check("and is marked as such", got["source"] == "user", got["source"])
        c.check("the rest survive", len(s3.all()) == len(store.all()))

    return c


if __name__ == "__main__":
    checker = run()
    print(f"\n{checker.passed} passed, {len(checker.failures)} failed")
    sys.exit(1 if checker.failures else 0)
