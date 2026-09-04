"""
Diagnostics tests — the triage surface.

    python3 tests/swarm/test_diagnostics.py

Each check here corresponds to a failure that is otherwise invisible: from the
outside, a pattern that failed to load, one that matched nothing, and one whose
suggestion is already built all look the same — an absence.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    Checker, FakeDevice, SAMPLE_NAMES, SAMPLE_ROOMS, SAMPLE_SETTINGS, sample_network,
)

from modules.swarm import diagnostics as dx  # noqa: E402
from modules.swarm import suggestions as sg  # noqa: E402
from modules.swarm.network import describe_network  # noqa: E402
from modules.swarm.stigmergy import StigmergyStore  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
BUNDLED = str(REPO / "data" / "stigmergy")


def _net(devices=None, settings=None, rooms=None):
    return describe_network(devices or sample_network(), SAMPLE_NAMES,
                            settings if settings is not None else SAMPLE_SETTINGS,
                            rooms if rooms is not None else SAMPLE_ROOMS)["devices"]


def _codes(report):
    return {f["code"]: f for f in report["findings"]}


def run() -> Checker:
    c = Checker("test_diagnostics")
    described = _net()

    c.section("a healthy network reports no errors")
    report = dx.diagnose(described, rooms=SAMPLE_ROOMS)
    c.check("ok", report["ok"] is True, report["counts"])
    c.check("timing reported", isinstance(report["took_ms"], float))
    c.check("patterns_loaded is info", _codes(report)["patterns_loaded"]["level"] == "info")

    c.section("an empty network is an error, not a silent pass")
    report = dx.diagnose([], rooms=SAMPLE_ROOMS)
    c.check("flagged", "no_devices" in _codes(report))
    c.check("not ok", report["ok"] is False)

    c.section("no rooms defined explains every room-scoped miss at once")
    report = dx.diagnose(described, rooms={})
    f = _codes(report).get("no_rooms")
    c.check("flagged", f is not None)
    c.check("names the fix", f and "config.yaml" in f["message"], f)

    c.section("unplaced devices are named")
    f = _codes(dx.diagnose(described, rooms=SAMPLE_ROOMS))["devices_unplaced"]
    c.check("counted", "1 device" in f["message"], f["message"])
    c.check("named", f["devices"][0]["name"] == "Front Door Lock", f["devices"])

    c.section("a device the resolver cannot read is surfaced")
    devices = sample_network()
    devices["0xmystery"] = FakeDevice("0xmystery", "Mystery Thing", {"foo": 1})
    f = _codes(dx.diagnose(_net(devices), rooms=SAMPLE_ROOMS))
    c.check("flagged", "devices_without_capabilities" in f)
    c.check("named", any(d["name"] == "Mystery Thing"
                         for d in f["devices_without_capabilities"]["devices"]))
    c.check("warned when commands were available",
            f["devices_without_capabilities"]["level"] == "warning")

    c.section("offline, an actuator with no command list is not reported as a fault")
    f = _codes(dx.diagnose(_net(devices), rooms=SAMPLE_ROOMS,
                           commands_available=False))
    c.check("downgraded to info",
            f["devices_without_capabilities"]["level"] == "info")
    c.check("and says why", "state cache" in
            f["devices_without_capabilities"]["message"])

    c.section("absent capabilities explain a class of missing suggestions")
    f = _codes(dx.diagnose(described, rooms=SAMPLE_ROOMS))["capabilities_absent"]
    c.check("co2 is listed as absent", "co2" in f["capabilities"])
    c.check("presence is not", "presence" not in f["capabilities"])

    c.section("broken existing rules are reported, not swallowed")
    report = dx.diagnose(described, rules=[{"id": "r1", "source_ieee": "0xgone",
                                            "conditions": []}], rooms=SAMPLE_ROOMS)
    f = _codes(report)
    c.check("an orphaned source is flagged", "rules_orphaned" in f)
    c.check("it names the rule", f["rules_orphaned"]["rules"][0]["id"] == "r1")

    report = dx.diagnose(described, rules=[{"id": "r2", "enabled": False,
                                            "source_ieee": "0xradar"}],
                         rooms=SAMPLE_ROOMS)
    c.check("a disabled rule is noted", "rules_disabled" in _codes(report))

    c.section("patterns that failed to load are an error, distinct from no match")
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "bad.json"), "w") as fh:
            json.dump({"id": "broken", "title": "B", "slots": {}}, fh)
        import modules.swarm.stigmergy as st
        saved = st._store
        st._store = StigmergyStore(bundled_dir=tmp, user_dir="/nonexistent")
        try:
            report = dx.diagnose(described, rooms=SAMPLE_ROOMS)
            f = _codes(report)
            c.check("load failure is an error", f["pattern_load_failed"]["level"] == "error")
            c.check("no usable patterns is its own error", "no_patterns" in f)
            c.check("report is not ok", report["ok"] is False)
        finally:
            st._store = saved

    c.section("withheld candidates are reported as defects in a pattern")
    store = StigmergyStore(bundled_dir=BUNDLED, user_dir="/nonexistent")
    built = sg.build(described, rules=[], rooms=SAMPLE_ROOMS, patterns=store.all())
    built["rejected"] = [{"pattern": "p", "stage": "compile", "error": "boom"}]
    f = _codes(dx.diagnose(described, built=built, rooms=SAMPLE_ROOMS))
    c.check("flagged as an error", f["suggestions_rejected"]["level"] == "error")
    c.check("grouped by stage",
            f["suggestions_rejected"]["by_stage"] == {"compile": 1})

    c.section("unmatched patterns say which slot blocked them")
    built = sg.build(described, rules=[], rooms=SAMPLE_ROOMS, patterns=store.all())
    f = _codes(dx.diagnose(described, built=built, rooms=SAMPLE_ROOMS))
    unmatched = {p["id"]: p for p in f["patterns_unmatched"]["patterns"]}
    c.check("the button pattern is unmatched", "button_toggle_light" in unmatched)
    c.check("and names the blocking slot",
            unmatched["button_toggle_light"]["blocked_slots"] == ["press"],
            unmatched["button_toggle_light"])
    c.check("with a readable reason",
            any("press" in r for r in unmatched["button_toggle_light"]["reasons"]),
            unmatched["button_toggle_light"]["reasons"])

    c.section("explain covers one pattern in every scope")
    r = dx.explain("presence_light_when_dark", described, SAMPLE_ROOMS)
    c.check("matched", r["outcome"] == "matched")
    by_room = {t["room"]: t for t in r["trace"]}
    c.check("the hallway matched", by_room["hallway"]["outcome"] == "matched")
    c.check("each slot is accounted for",
            set(by_room["hallway"]["slots"]) == {"trig", "dark", "light", "off"},
            list(by_room["hallway"]["slots"]))
    c.check("the same-device note is recorded",
            by_room["hallway"]["slots"]["dark"]["note"] == "same device as trig")
    c.check("the lounge did not match", by_room["lounge"]["outcome"] == "no_match")
    c.check("and says which slot blocked", by_room["lounge"]["blocked_by"] == "trig")

    c.check("an unknown pattern lists the known ones",
            "known" in dx.explain("nope", described, SAMPLE_ROOMS))

    c.section("slot inspection answers 'does anything offer this at all?'")
    pattern = store.get("button_toggle_light")
    c.check("nothing offers a button press",
            dx.offers_for_slot(pattern["slots"]["press"], described) == [])
    pattern = store.get("presence_light_when_dark")
    offered = dx.offers_for_slot(pattern["slots"]["light"], described)
    c.check("several devices could be the light, ignoring room",
            len(offered) >= 2, [o["name"] for o in offered])
    c.check("each names its room so the mismatch is visible",
            all("room_label" in o for o in offered))

    return c


if __name__ == "__main__":
    checker = run()
    print(f"\n{checker.passed} passed, {len(checker.failures)} failed")
    sys.exit(1 if checker.failures else 0)
