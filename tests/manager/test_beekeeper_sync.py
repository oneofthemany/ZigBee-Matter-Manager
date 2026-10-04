"""The watchdog keeps a running Beekeeper sidecar on the app's image after upgrades."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import tempfile
from pathlib import Path

from harness import Checker, FakeRuntime

APP, BK = "zigbee-matter-manager", "zigbee-matter-manager-beekeeper"


def _info(image_id: str, running: bool = True):
    return {"Image": image_id, "State": {"Running": running, "Status": "running" if running else "exited"}}


def run() -> Checker:
    c = Checker("beekeeper_sync")
    with tempfile.TemporaryDirectory() as data:
        rt = FakeRuntime()
        os.environ["ZMM_CONTAINER_SOCK"] = rt.sock
        os.environ["ZMM_DATA_DIR"] = data
        import manager.containers, manager.beekeeper, manager.watchdog
        importlib.reload(manager.containers)
        beekeeper = importlib.reload(manager.beekeeper)
        watchdog = importlib.reload(manager.watchdog)
        calls = []

        async def fake_enable():
            calls.append("enable")
            return {"success": True}
        beekeeper.enable = fake_enable
        t = {}
        sync = lambda: asyncio.run(watchdog._sync_beekeeper_image(t))

        c.section("after an upgrade")
        rt.inspect = {APP: _info("sha256:new"), BK: _info("sha256:old")}
        sync()
        c.check("a sidecar on an older image is recreated from the app's", calls == ["enable"], calls)
        sync()
        c.check("…once per app image, not every tick", calls == ["enable"], calls)
        rt.inspect[APP] = _info("sha256:newer")
        sync()
        c.check("the next upgrade triggers it again", calls == ["enable", "enable"], calls)

        c.section("left alone")
        calls.clear(); t.clear()
        rt.inspect = {APP: _info("sha256:same"), BK: _info("sha256:same")}
        sync()
        c.check("already on the app's image", calls == [])
        rt.inspect = {APP: _info("sha256:new"), BK: _info("sha256:old", running=False)}
        sync()
        c.check("a stopped sidecar (the user disabled it)", calls == [])
        rt.inspect = {APP: _info("sha256:new")}
        sync()
        c.check("no sidecar installed", calls == [])
        rt.inspect = {APP: _info("sha256:new"), BK: _info("sha256:old")}
        status = Path(watchdog.STATUS_FILE)
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(json.dumps({"state": "swapping"}))
        sync()
        c.check("while an upgrade is swapping containers", calls == [])
        status.write_text(json.dumps({"state": "idle"}))
        sync()
        c.check("…and it catches up once the upgrade is done", calls == ["enable"], calls)
        rt.close()
    return c
