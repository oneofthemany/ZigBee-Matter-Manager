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


def run() -> Checker:
    c = Checker("test_api")

    app = FastAPI()
    engine = FakeEngine(sample_network(), SAMPLE_NAMES)
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

    c.section("a missing engine is a 503, not a crash")
    app2 = FastAPI()
    register_swarm_routes(app2, lambda: None)
    c.check("503", TestClient(app2).get("/api/swarm/capabilities").status_code == 503)

    return c


if __name__ == "__main__":
    checker = run()
    print(f"\n{checker.passed} passed, {len(checker.failures)} failed")
    sys.exit(1 if checker.failures else 0)
