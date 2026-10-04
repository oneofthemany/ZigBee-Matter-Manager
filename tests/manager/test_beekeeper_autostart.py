"""Beekeeper's boot-time service, from the manager's side: requests, ordering, watchdog."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import tempfile
import time
from pathlib import Path

from harness import Checker, FakeRuntime

APP, BK = "zigbee-matter-manager", "zigbee-matter-manager-beekeeper"


def run() -> Checker:
    c = Checker("beekeeper_autostart")
    with tempfile.TemporaryDirectory() as data:
        rt = FakeRuntime()
        os.environ["ZMM_CONTAINER_SOCK"] = rt.sock
        os.environ["ZMM_DATA_DIR"] = data
        import manager.containers, manager.beekeeper, manager.watchdog
        importlib.reload(manager.containers)
        beekeeper = importlib.reload(manager.beekeeper)
        watchdog = importlib.reload(manager.watchdog)
        trigger = Path(beekeeper._SVC_TRIGGER)
        status = Path(beekeeper._SVC_STATUS)
        run_ = asyncio.run
        app_info = {"Image": "sha256:app", "Config": {"Image": "localhost/zmm:04.10.2026-amd64"},
                    "Mounts": [{"Destination": "/app/data", "Source": "/opt/zmm/data"}]}

        c.section("enable / disable")
        rt.inspect = {APP: app_info}
        res = run_(beekeeper.enable())
        create = next((p for p in rt.posts if p["path"] == "/containers/create"), {})
        c.check("enabling creates it with restart policy unless-stopped (not always, which races the unit on podman)",
                res.get("success") and (create.get("body") or {}).get("HostConfig", {}).get("RestartPolicy") == {"Name": "unless-stopped"},
                create)
        c.check("…and asks the host for the boot-time service", trigger.read_text() == "install")
        trigger.unlink()

        rt.inspect = {APP: app_info, BK: {"Image": "sha256:app", "State": {"Running": True}}}
        rt.on_post = lambda path: {"trigger_then": trigger.read_text() if trigger.exists() else None}
        run_(beekeeper.disable())
        stop = next(p for p in rt.posts if p["path"].endswith(f"/{BK}/stop"))
        c.check("disabling asks for removal before the stop reaches the runtime, so Restart=always can't bring it back",
                stop["trigger_then"] == "remove", stop)
        rt.on_post = None
        trigger.unlink()

        c.section("status for the dashboard")
        c.check("no report yet: says so, not 'installed'",
                beekeeper.service_status()["known"] is False and beekeeper.service_status()["installed"] is False)
        status.write_text(json.dumps({"backend": "systemd", "unit": "zmm-beekeeper.service",
                                      "installed": True, "enabled": True, "active": True, "conflict": ""}))
        bk = run_(beekeeper.status())
        c.check("the Beekeeper status carries the service report", bk.get("service", {}).get("enabled") is True, bk.get("service"))
        c.check("an invalid action is refused", beekeeper.request_service("reboot")["success"] is False and not trigger.exists())

        c.section("watchdog: existing sidecars get a service")
        t = {}
        ensure = lambda: run_(watchdog._ensure_beekeeper_service(t))
        status.unlink()
        ensure()
        c.check("a running sidecar with no service report gets one requested", trigger.exists() and trigger.read_text() == "install")
        trigger.unlink()
        ensure()
        c.check("not re-requested every tick", not trigger.exists())
        t["requested_at"] = time.time() - 16 * 60
        ensure()
        c.check("…but retried after 15 minutes if still missing", trigger.exists())
        trigger.unlink()
        for label, report in [("already installed", {"installed": True}),
                              ("another unit manages it", {"installed": False, "conflict": "beekeeper-dns.service"}),
                              ("no service manager on the host", {"installed": False, "backend": "none"})]:
            status.write_text(json.dumps(report)); t.clear()
            ensure()
            c.check(f"left alone when {label}", not trigger.exists())
        status.unlink(); t.clear()
        rt.inspect[BK]["State"]["Running"] = False
        ensure()
        c.check("left alone when the sidecar is stopped (disabled)", not trigger.exists())
        rt.close()
    return c
