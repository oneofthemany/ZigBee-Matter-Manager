"""
A Nuki lock renamed in ZMM shows that name in the device list and the lock
window, over the real security routes with a stand-in bridge.

Needs FastAPI and aiohttp (the lockfile venv, see AGENTS.md); reported as
skipped without them.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from harness import Checker

BRIDGE_LOCK = {"nukiId": 42, "name": "Front Door", "deviceType": 0,
               "lastKnownState": {"state": 1, "stateName": "locked"}}


def run() -> Checker:
    c = Checker("nuki_names")
    try:
        import aiohttp  # noqa: F401
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except ImportError:
        print("  SKIPPED: needs FastAPI and aiohttp (see AGENTS.md, The dev box)")
        return c

    import routes.security_routes as sec
    from modules.nuki_controller import NukiBridgeClient

    class FakeBridge(NukiBridgeClient):
        async def list_devices(self):
            return [dict(BRIDGE_LOCK)]

    class FakeService:
        friendly_names: dict = {}

    saved = (sec.CONFIG_PATH, sec.LOCKS_STORE_PATH, sec.NukiBridgeClient)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yaml"
        cfg.write_text("security:\n  nuki:\n    enabled: true\n"
                       "    bridge: {host: 10.0.0.9, token: t, enabled: true}\n")
        sec.CONFIG_PATH, sec.LOCKS_STORE_PATH = str(cfg), str(Path(tmp) / "locks.json")
        sec.NukiBridgeClient = FakeBridge
        try:
            svc = FakeService()
            app = FastAPI()
            sec.register_security_routes(app, get_matter_bridge=lambda: None,
                                         get_zigbee_service=lambda: svc)
            api = TestClient(app)
            locks = lambda **q: api.get("/api/security/nuki/locks", params=q).json()["locks"]

            c.section("before any rename")
            c.check("the lock window shows the name from the Nuki app", [l["name"] for l in locks()] == ["Front Door"])

            c.section("after renaming in ZMM")
            svc.friendly_names["nuki_42"] = "Sean's Front Door"     # what /api/device/rename stores
            c.check("the lock window shows the ZMM name", [l["name"] for l in locks()] == ["Sean's Front Door"])
            c.check("…also when served from the bridge cache",
                    [l["name"] for l in locks(max_age=60)] == ["Sean's Front Door"])
            entries = asyncio.run(app.state.nuki_device_entries())
            c.check("the device list shows the ZMM name", [e["friendly_name"] for e in entries] == ["Sean's Front Door"], entries)

            c.section("the cache keeps the Nuki app's name")
            svc.friendly_names.clear()
            c.check("clearing the ZMM name falls back to the Nuki app's name, so the cache wasn't overwritten",
                    [l["name"] for l in locks(max_age=60)] == ["Front Door"])
        finally:
            sec.CONFIG_PATH, sec.LOCKS_STORE_PATH, sec.NukiBridgeClient = saved
    return c
