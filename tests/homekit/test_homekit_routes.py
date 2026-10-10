"""
The HomeKit /api/devices hook, driven through the real route module.

Skipped where FastAPI is not installed. A stub controller stands in for the
TVs, so this checks the hook's contract: it never waits on a TV, and the open
device table is told to refetch once the first background refresh lands.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

from harness import Checker


class StubController:
    def __init__(self, cfg):
        self.enabled = True
        self.last_error = None
        self._status = {}

    def reload(self, cfg):
        pass

    def device_ids(self):
        return ["tv1"]

    def cached_status(self, device_id):
        entry = self._status.get(device_id)
        return None if entry is None else (time.monotonic() - entry[0], entry[1])

    async def list_devices(self, max_age=0):
        self._status["tv1"] = (time.monotonic(), {"name": "Lounge TV", "online": True, "power": True})
        return [self._status["tv1"][1]]


def _settle():
    async def go(hook):
        first = await hook()
        # The hook schedules the refresh in the background; let it land.
        for _ in range(50):
            await asyncio.sleep(0)
        return first, await hook()
    return go


def run() -> Checker:
    c = Checker("homekit_routes")
    try:
        from fastapi import FastAPI
    except ImportError:
        print("\n  skipped (fastapi not installed)")
        return c

    import modules.homekit_controller as H
    import routes.websocket_routes as W

    tmp = tempfile.TemporaryDirectory()
    cwd = os.getcwd()
    os.chdir(tmp.name)
    Path("config").mkdir()
    Path("config/config.yaml").write_text("homekit:\n  enabled: true\n")

    real_controller, real_broadcast = H.HomeKitController, W.broadcast_event
    pushed = []

    async def fake_broadcast(event_type, data):
        pushed.append(event_type)

    H.HomeKitController, W.broadcast_event = StubController, fake_broadcast
    try:
        sys.modules.pop("routes.homekit_routes", None)
        from routes.homekit_routes import register_homekit_routes

        app = FastAPI()
        register_homekit_routes(app)
        hook = app.state.homekit_device_entries

        c.section("device list")
        before, after = asyncio.run(_settle()(hook))
        c.check("the device list never waits on a TV", before == [], before)
        c.check("a TV joins /api/devices once its status is cached",
                len(after) == 1 and after[0]["homekit_device_id"] == "tv1"
                and after[0]["friendly_name"] == "Lounge TV", after)
        c.check("the open device table is told to refetch when the TV first arrives",
                pushed == ["devices_changed"], pushed)
    finally:
        H.HomeKitController, W.broadcast_event = real_controller, real_broadcast
        os.chdir(cwd)
        tmp.cleanup()
    return c
