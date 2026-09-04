"""
API tests — the read-only HTTP surface.

    python3 tests/swarm/test_api.py

Routes are registered against a real FastAPI app and driven through Starlette's
TestClient, so the checks cover the wiring (getters, 404s, response shape) as
well as the payloads.
"""

from __future__ import annotations

import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    Checker, SAMPLE_NAMES, SAMPLE_SETTINGS, sample_network,
)

# Imported at module level so run_all.py skips this file rather than failing it
# on a machine without the app's web dependencies installed.
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from modules.swarm.api import register_swarm_routes  # noqa: E402


class FakeEngine:
    """The two accessors the swarm reads off the automation engine."""

    def __init__(self, devices, names):
        self._devices = devices
        self._names = names

    def _get_all_devices(self):
        return self._devices

    def _get_all_names(self):
        return self._names


class FakeService:
    def __init__(self, settings):
        self.device_settings = settings


class RecordingEngine(FakeEngine):
    """Adds the rule surface the suggestion routes use.

    The real validators are borrowed from the real engine class so the API
    tests exercise the same checks production does, without needing a radio.
    """

    def __init__(self, devices, names):
        super().__init__(devices, names)
        self.rules = []
        self.added = []

    def get_rules(self):
        return list(self.rules)

    def add_rule(self, data):
        from modules.automation import AutomationEngine
        engine = AutomationEngine.__new__(AutomationEngine)
        for check, args in (
                ("_validate_conditions", (data["conditions"],)),
                ("_validate_prerequisites", (data["prerequisites"],)),
                ("_validate_sequence", (data["then_sequence"], "THEN")),
                ("_validate_sequence", (data["else_sequence"], "ELSE"))):
            err = getattr(engine, check)(*args)
            if err:
                return {"success": False, "error": err}
        rule = {**data, "id": f"auto_{len(self.rules)}"}
        self.rules.append(rule)
        self.added.append(rule)
        return {"success": True, "rule": rule}

    def _validate_conditions(self, conds):
        from modules.automation import AutomationEngine
        return AutomationEngine._validate_conditions(
            AutomationEngine.__new__(AutomationEngine), conds)

    def _validate_prerequisites(self, prereqs):
        from modules.automation import AutomationEngine
        return AutomationEngine._validate_prerequisites(
            AutomationEngine.__new__(AutomationEngine), prereqs)

    def _validate_sequence(self, steps, label, depth=0):
        from modules.automation import AutomationEngine
        return AutomationEngine._validate_sequence(
            AutomationEngine.__new__(AutomationEngine), steps, label, depth)

    def _validate_zone_source(self, conds, source_ieee):
        dev = self._devices.get(source_ieee)
        state = getattr(dev, "state", None) if dev else None
        if any(c.get("type") == "zone" for c in conds):
            if not state or "place" not in state:
                return "Enters/leaves conditions need a presence user"
        return None


def run() -> Checker:
    c = Checker("test_api")

    app = FastAPI()
    engine = RecordingEngine(sample_network(), SAMPLE_NAMES)
    register_swarm_routes(app, lambda: engine, lambda: FakeService(SAMPLE_SETTINGS))
    client = TestClient(app)

    c.section("vocabulary")
    r = client.get("/api/swarm/vocabulary")
    c.check("200", r.status_code == 200, r.status_code)
    body = r.json()
    c.check("capabilities returned", "presence" in body["capabilities"])
    c.check("params returned", "dark_lux" in body["params"])

    c.section("network capabilities")
    r = client.get("/api/swarm/capabilities")
    c.check("200", r.status_code == 200, r.status_code)
    body = r.json()
    c.check("every device described", len(body["devices"]) == 8, len(body["devices"]))
    c.check("summary present", body["summary"]["devices"] == 8)
    c.check("offers are serialisable",
            all("triggers" in d and "actions" in d for d in body["devices"]))

    c.section("one device")
    r = client.get("/api/swarm/capabilities/0xradar")
    c.check("200", r.status_code == 200, r.status_code)
    body = r.json()
    c.check("room resolved from settings", body["room"] == "hallway", body["room"])
    c.check("presence trigger present",
            any(t["key"] == "presence:detected" for t in body["triggers"]))
    c.check("unknown device is 404",
            client.get("/api/swarm/capabilities/nope").status_code == 404)

    c.section("pairings")
    r = client.get("/api/swarm/pairings/0xradar")
    c.check("200", r.status_code == 200, r.status_code)
    body = r.json()
    c.check("outbound wiring returned", body["outbound"], body["outbound_total"])
    c.check("top pairing is the same-room light",
            body["outbound"][0]["target_ieee"] == "0xhalllight",
            body["outbound"][0]["target_ieee"])
    c.check("confidence filter is honoured",
            all(p["confidence"] == "high" for p in
                client.get("/api/swarm/pairings/0xradar?min_confidence=high")
                .json()["outbound"]))
    c.check("unknown device is 404",
            client.get("/api/swarm/pairings/nope").status_code == 404)

    c.section("rooms")
    r = client.get("/api/swarm/rooms")
    c.check("200", r.status_code == 200, r.status_code)
    rooms = {x["name"]: x for x in r.json()["rooms"]}
    c.check("unassigned bucket exists", "Unassigned" in rooms, list(rooms))
    c.check("unplaced devices land there",
            set(rooms["Unassigned"]["devices"]) ==
            {"nuki_1", "user::sean", "user::charlie"},
            rooms["Unassigned"]["devices"])

    c.section("stigmergy patterns")
    r = client.get("/api/swarm/stigmergy")
    c.check("200", r.status_code == 200, r.status_code)
    c.check("patterns listed", len(r.json()["patterns"]) >= 20)
    c.check("no load errors", r.json()["errors"] == [], r.json()["errors"])
    c.check("one pattern fetches",
            client.get("/api/swarm/stigmergy/arrival_unlock").status_code == 200)
    c.check("unknown pattern is 404",
            client.get("/api/swarm/stigmergy/nope").status_code == 404)
    c.check("reload works",
            client.post("/api/swarm/stigmergy/reload").json()["success"] is True)

    c.section("pattern validation endpoint")
    r = client.post("/api/swarm/validate", json={"id": "x", "title": "X",
                                                 "slots": {}, "emits": {}})
    c.check("invalid pattern reports errors", r.json()["valid"] is False)
    c.check("errors are listed", r.json()["errors"], r.json())

    c.section("suggestions")
    r = client.get("/api/swarm/suggestions")
    c.check("200", r.status_code == 200, r.status_code)
    body = r.json()
    c.check("suggestions returned", body["suggestions"], body["summary"])
    c.check("nothing was withheld", body["rejected"] == [], body["rejected"])
    c.check("coverage included", "percent" in body["coverage"])
    c.check("trace withheld by default", "trace" not in body)
    c.check("trace on request",
            "trace" in client.get("/api/swarm/suggestions?include_trace=true").json())
    c.check("filtering by room works",
            all(s["room"] == "hallway" for s in
                client.get("/api/swarm/suggestions?room=hallway").json()["suggestions"]))
    c.check("filtering by category works",
            all(s["category"] == "safety" for s in
                client.get("/api/swarm/suggestions?category=safety").json()["suggestions"]))

    first = body["suggestions"][0]
    c.check("one suggestion fetches",
            client.get(f"/api/swarm/suggestions/{first['id']}").status_code == 200)
    c.check("unknown suggestion is 404",
            client.get("/api/swarm/suggestions/sg_nope").status_code == 404)

    c.section("applying a suggestion creates the rule")
    before = len(engine.rules)
    r = client.post(f"/api/swarm/suggestions/{first['id']}/apply", json={})
    c.check("200", r.status_code == 200, r.text[:200])
    c.check("the engine accepted it", len(engine.rules) == before + 1)
    c.check("the rule is returned", r.json()["rule"]["id"] is not None)

    c.check("re-applying is refused as a conflict",
            client.post(f"/api/swarm/suggestions/{first['id']}/apply",
                        json={}).status_code == 409)
    c.check("and it now reads as active",
            client.get(f"/api/swarm/suggestions/{first['id']}").json()["status"]
            == "active")

    c.section("parameters and names are honoured on apply")
    tunable = next((s for s in client.get("/api/swarm/suggestions").json()["suggestions"]
                    if s["status"] == "available" and
                    any(p["id"] == "dark_lux" for p in s["params"])), None)
    if tunable:
        r = client.post(f"/api/swarm/suggestions/{tunable['id']}/apply",
                        json={"params": {"dark_lux": 42}, "name": "My rule"})
        c.check("applied with overrides", r.status_code == 200, r.text[:200])
        rule = r.json()["rule"]
        c.check("the name was used", rule["name"] == "My rule", rule["name"])
        lux = [x for x in rule["conditions"]
               if x.get("attribute") == "illuminance_lux"]
        c.check("the override reached the rule",
                lux and lux[0]["value"] == 42, lux)
    else:
        c.check("a tunable suggestion was available", False, "none found")

    c.section("coverage endpoint")
    r = client.get("/api/swarm/coverage")
    c.check("200", r.status_code == 200)
    c.check("gaps listed", "gaps" in r.json()["coverage"])

    c.section("diagnostics")
    r = client.get("/api/swarm/diagnostics")
    c.check("200", r.status_code == 200, r.status_code)
    report = r.json()
    c.check("findings returned", report["findings"], report)
    c.check("counts present", set(report["counts"]) == {"error", "warning", "info"})

    c.section("explain")
    r = client.get("/api/swarm/explain/presence_light_when_dark")
    c.check("200", r.status_code == 200)
    c.check("trace per room", len(r.json()["trace"]) >= 2, r.json()["trace"])
    c.check("unknown pattern is 404",
            client.get("/api/swarm/explain/nope").status_code == 404)

    r = client.get("/api/swarm/explain/presence_light_when_dark/slot/light")
    c.check("slot inspection works", r.status_code == 200)
    c.check("it lists what could fill the slot", r.json()["offered_by"])
    c.check("unknown slot is 404",
            client.get("/api/swarm/explain/presence_light_when_dark/slot/zz")
            .status_code == 404)

    c.section("a missing engine is a 503, not a crash")
    app2 = FastAPI()
    register_swarm_routes(app2, lambda: None)
    c.check("503", TestClient(app2).get("/api/swarm/capabilities").status_code == 503)

    return c


if __name__ == "__main__":
    checker = run()
    print(f"\n{checker.passed} passed, {len(checker.failures)} failed")
    sys.exit(1 if checker.failures else 0)
