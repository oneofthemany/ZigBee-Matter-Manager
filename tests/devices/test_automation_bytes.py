"""
A device whose state holds a raw octet string (an Aqara FP300's 0x019A) can
still be picked in the rule builder: its attributes and state reach the page
as JSON, through the real routes and FastAPI's own encoder.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from harness import Checker

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.automation import AutomationEngine
from modules.automation_api import register_automation_routes

IEEE = "54:ef:44:10:01:65:58:b3"


def run() -> Checker:
    c = Checker("automation_bytes")
    import zigpy.types as t
    dev = NS(state={"occupancy": True, "temperature": 21.5,
                    "opple_0x019a": t.LVBytes(bytes.fromhex("0003ffffff"))})
    engine = AutomationEngine.__new__(AutomationEngine)
    engine._get_all_devices = lambda: {IEEE: dev}
    engine._get_all_names = lambda: {IEEE: "Motion - Hallway"}
    app = FastAPI()
    register_automation_routes(app, lambda: engine)
    client = TestClient(app, raise_server_exceptions=False)

    c.section("the rule builder's two reads of a device")
    r = client.get(f"/api/automations/device/{IEEE}/attributes")
    c.check("its attributes are JSON", r.status_code == 200, r.status_code)
    raw = next((a for a in (r.json() if r.status_code == 200 else []) if a["attribute"] == "opple_0x019a"), None)
    c.check("the byte string is shown as hex", raw and raw["current_value"] == "0003ffffff", raw)
    r = client.get(f"/api/automations/device/{IEEE}/state")
    c.check("its state is JSON", r.status_code == 200, r.status_code)
    c.check("with the same hex", r.status_code == 200 and r.json()["state"]["opple_0x019a"] == "0003ffffff")
    c.check("and the device's own state is left as it was",
            isinstance(dev.state["opple_0x019a"], bytes))
    return c


if __name__ == "__main__":
    run()
