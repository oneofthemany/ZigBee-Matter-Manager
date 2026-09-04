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
    Checker, FakeCapabilities, FakeDevice, SAMPLE_NAMES, SAMPLE_ROOMS,
    SAMPLE_SETTINGS, sample_network,
)

from modules.swarm import diagnostics as dx  # noqa: E402
from modules.swarm import suggestions as sg  # noqa: E402
from modules.swarm.network import describe_network  # noqa: E402
from modules.swarm.stigmergy import StigmergyStore  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
BUNDLED = str(REPO / "modules" / "swarm" / "patterns")


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

    c.section("a report says how far it can be trusted")
    # A service that has not polled yet looks exactly like one that is
    # misconfigured. Only the clock tells them apart, so the report carries it.
    from modules.swarm.diagnostics import WARMUP_SECONDS, WARMUP_SENSITIVE
    early = dx.diagnose(described, rooms=SAMPLE_ROOMS, uptime_s=12.0,
                        provider_status={"refreshes": 0})
    c.check("not settled during warm-up", early["settled"] is False, early["readiness"])
    c.check("the warm-up is explained", "warming_up" in _codes(early))
    c.check("and says how long it had been up",
            "12s after start" in _codes(early)["warming_up"]["message"],
            _codes(early)["warming_up"]["message"])
    c.check("it names both reasons",
            "warm-up" in _codes(early)["warming_up"]["message"]
            and "refreshed" in _codes(early)["warming_up"]["message"],
            _codes(early)["warming_up"]["message"])
    c.check("findings that read what devices report are marked provisional",
            all(f.get("provisional") for f in early["findings"]
                if f["code"] in WARMUP_SENSITIVE),
            [f["code"] for f in early["findings"] if f["code"] in WARMUP_SENSITIVE])
    c.check("and counted", early["counts"]["provisional"] >= 1, early["counts"])
    c.check("findings true immediately are not marked",
            not any(f.get("provisional") for f in early["findings"]
                    if f["code"] in ("patterns_loaded", "devices_unplaced",
                                     "rooms_empty")),
            [f["code"] for f in early["findings"] if f.get("provisional")])

    late = dx.diagnose(described, rooms=SAMPLE_ROOMS,
                       uptime_s=WARMUP_SECONDS + 10, provider_status={"refreshes": 4})
    c.check("settled once warmed and fetched", late["settled"] is True, late["readiness"])
    c.check("nothing provisional", late["counts"]["provisional"] == 0)
    c.check("no warm-up notice", "warming_up" not in _codes(late))

    # Warmed up but the services have never run: still not settled.
    stalled = dx.diagnose(described, rooms=SAMPLE_ROOMS,
                          uptime_s=WARMUP_SECONDS + 10, provider_status={"refreshes": 0})
    c.check("an unfetched service alone keeps it unsettled",
            stalled["settled"] is False, stalled["readiness"])

    # The CLI cannot know uptime; withholding everything there would make the
    # offline report useless.
    offline = dx.diagnose(described, rooms=SAMPLE_ROOMS)
    c.check("unknown uptime is treated as settled", offline["settled"] is True)
    c.check("so nothing is withheld", offline["counts"]["provisional"] == 0)
    c.check("readiness still reported", "readiness" in offline)

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

    c.section("a declaration the device contradicts is reported, not silently dropped")
    # A plug declaring the metering cluster that reports no power at all. The
    # capability is dropped so it cannot mis-describe the device, but the
    # contradiction is surfaced: reading "declares power, reports state" is how
    # the endpoint-suffix gap was found in the first place.
    quiet = sample_network()
    quiet["0xmute"] = FakeDevice(
        "0xmute", "Quiet Plug", {"state": "ON"},
        commands=[{"command": "on", "label": "On", "endpoint_id": 1},
                  {"command": "off", "label": "Off", "endpoint_id": 1}],
        capabilities=FakeCapabilities(["on_off", "power_monitoring"]))
    f = _codes(dx.diagnose(_net(quiet), rooms=SAMPLE_ROOMS))
    c.check("flagged", "capabilities_unproven" in f, list(f))
    entry = next((e for e in f["capabilities_unproven"]["entries"]
                  if e["ieee"] == "0xmute"), None)
    c.check("it names the device and capability",
            entry and entry["capability"] == "power", entry)
    c.check("and what it expected to see",
            "power" in (entry or {}).get("expected_attributes", []), entry)
    c.check("and what the device actually reports, so the mismatch is visible",
            (entry or {}).get("reports") == ["state"], entry)
    c.check("a working capability on the same device is not flagged",
            not any(e["ieee"] == "0xmute" and e["capability"] == "on_off"
                    for e in f["capabilities_unproven"]["entries"]))
    mute = next(d for d in _net(quiet) if d["ieee"] == "0xmute")
    c.check("the capability is dropped from the device, not just reported",
            "power" not in mute["capabilities"], mute["capabilities"])
    c.check("so the device is not mis-classified as a metering plug",
            mute["device_class"] == "switch", mute["device_class"])
    c.check("a healthy network reports none",
            "capabilities_unproven" not in
            _codes(dx.diagnose(described, rooms=SAMPLE_ROOMS)))

    c.section("a virtual service with no data is silent, not unproven")
    # A configured service that has not fetched yet is a real condition worth
    # reporting, not a bad guess about hardware — so it stays in the capability
    # list and is reported as silent instead of dropped.
    from modules.swarm.resolver import describe_device
    from modules.swarm.virtual import VirtualDevice
    tariff = describe_device("virtual::tariff",
                             VirtualDevice("virtual::tariff", "Tariff", "Pricing",
                                           ["tariff"]), "Tariff")
    c.check("the capability is kept", tariff["capabilities"] == ["tariff"],
            tariff["capabilities"])
    c.check("and nothing is dropped", tariff["unproven_capabilities"] == [])
    f = _codes(dx.diagnose(described + [tariff], rooms=SAMPLE_ROOMS))
    c.check("reported as silent", "capabilities_silent" in f, list(f))
    c.check("and not as a bad declaration", "capabilities_unproven" not in f, list(f))
    # There was a "condition only" finding here. Every capability that offers a
    # condition also offers a trigger, so it could only ever fire for a device
    # offering nothing at all — which capabilities_silent already reports, and
    # more precisely. It said "can be read as a condition" about a device with
    # no conditions.
    c.check("no misleading condition-only finding",
            "devices_without_offers" not in f, list(f))


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
