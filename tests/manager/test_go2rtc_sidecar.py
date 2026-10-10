"""go2rtc from the manager's side: enabling, the boot-time service, and the watchdog."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import tempfile
import time
from pathlib import Path

from harness import Checker, FakeRuntime

APP, G2 = "zigbee-matter-manager", "zigbee-matter-manager-go2rtc"


def run() -> Checker:
    c = Checker("go2rtc_sidecar")
    with tempfile.TemporaryDirectory() as data:
        rt = FakeRuntime()
        os.environ["ZMM_CONTAINER_SOCK"] = rt.sock
        os.environ["ZMM_DATA_DIR"] = data
        import manager.containers
        import manager.go2rtc
        importlib.reload(manager.containers)
        g = importlib.reload(manager.go2rtc)
        trigger, status = Path(g._SVC_TRIGGER), Path(g._SVC_STATUS)
        run_ = asyncio.run
        app_info = {"Image": "sha256:app", "Mounts": [{"Destination": "/app/data", "Source": "/opt/zmm/data"}]}

        async def enable_and_wait():
            ok, msg = g.start_enable()
            for _ in range(200):
                if not g.busy():
                    break
                await asyncio.sleep(0.01)
            return ok, msg

        c.section("enabling")
        rt.inspect = {APP: app_info}
        ok, msg = run_(enable_and_wait())
        c.check("refused until ZMM has written go2rtc's config", not ok and "config" in msg, msg)
        c.check("…and not marked enabled", not g.enabled())
        Path(g.CONFIG).parent.mkdir(parents=True, exist_ok=True)
        Path(g.CONFIG).write_text("api: {}\n")
        ok, msg = run_(enable_and_wait())
        create = next((p for p in rt.posts if p["path"] == "/containers/create"), {})
        hc = (create.get("body") or {}).get("HostConfig", {})
        c.check("pulls the pinned image", any(p["path"] == "/images/create" and "go2rtc%3A1.9.14" in p["query"]
                                              for p in rt.posts), [p["query"] for p in rt.posts])
        c.check("creates it from the pinned image on host networking, sharing only ZMM's go2rtc folder",
                create.get("body", {}).get("Image") == g.IMAGE and hc.get("NetworkMode") == "host"
                and hc.get("Binds") == ["/opt/zmm/data/go2rtc:/config:rw"], create)
        c.check("restart policy unless-stopped, so it can't race the host unit",
                hc.get("RestartPolicy") == {"Name": "unless-stopped"})
        c.check("starts it and asks the host for a boot-time service",
                any(p["path"] == f"/containers/{G2}/start" for p in rt.posts) and trigger.read_text() == "install")
        c.check("remembers it's wanted", g.enabled())
        trigger.unlink()

        async def twice():
            first = g.start_enable()
            second = g.start_enable()          # before the first task has run at all
            while g.busy():
                await asyncio.sleep(0.01)
            return first, second
        first, second = run_(twice())
        c.check("a second enable while one is under way is refused, not run twice",
                first[0] and not second[0] and "already" in second[1], (first, second))
        if trigger.exists():
            trigger.unlink()

        c.section("disabling")
        rt.inspect[G2] = {"Config": {"Image": g.IMAGE}, "State": {"Running": True}}
        rt.on_post = lambda path: {"trigger_then": trigger.read_text() if trigger.exists() else None}
        res = run_(g.disable())
        stop = next(p for p in rt.posts if p["path"].endswith(f"/{G2}/stop"))
        c.check("asks for the service's removal before stopping, so the unit can't bring it back",
                res["success"] and stop["trigger_then"] == "remove", stop)
        c.check("…and remembers it isn't wanted", not g.enabled())
        rt.on_post = None
        trigger.unlink()

        c.section("watchdog")
        t = {}
        rt.posts.clear()
        rt.inspect[G2]["State"]["Running"] = False
        run_(g.ensure(t))
        c.check("does nothing for a go2rtc someone disabled", not rt.posts and not trigger.exists())

        g._set_enabled(True)
        rt.inspect[G2]["State"]["Running"] = True
        run_(g.ensure(t))
        c.check("a running go2rtc with no service gets one requested", trigger.exists() and trigger.read_text() == "install")
        trigger.unlink()
        run_(g.ensure(t))
        c.check("not re-requested every tick", not trigger.exists())
        t["service_at"] = time.time() - 16 * 60
        run_(g.ensure(t))
        c.check("…but retried after 15 minutes", trigger.exists())
        trigger.unlink()

        status.write_text(json.dumps({"backend": "systemd", "installed": True, "enabled": True, "active": True}))
        rt.inspect[G2]["State"]["Running"] = False
        rt.posts.clear()
        run_(g.ensure({}))
        c.check("with the host's service installed, the watchdog leaves starting it to the service",
                not any(p["path"].endswith("/start") for p in rt.posts), rt.posts)
        c.check("the status carries the service report", run_(g.status())["service"]["installed"] is True)

        status.write_text(json.dumps({"backend": "none", "installed": False}))
        t = {}
        async def ensure_and_wait():
            await g.ensure(t)
            for _ in range(200):
                if not g.busy():
                    break
                await asyncio.sleep(0.01)
        run_(ensure_and_wait())
        c.check("with no service manager, the watchdog starts a stopped go2rtc itself",
                any(p["path"] == f"/containers/{G2}/start" for p in rt.posts), rt.posts)

        rt.posts.clear()
        rt.inspect[G2] = {"Config": {"Image": "docker.io/alexxit/go2rtc:1.9.9"}, "State": {"Running": True}}
        run_(ensure_and_wait())
        c.check("an older pinned image is replaced with the current one",
                any((p.get("body") or {}).get("Image") == g.IMAGE for p in rt.posts if p["path"] == "/containers/create"))
        rt.posts.clear()
        run_(ensure_and_wait())
        c.check("…once per image, not every tick", not any(p["path"] == "/containers/create" for p in rt.posts))
        rt.close()
    return c
