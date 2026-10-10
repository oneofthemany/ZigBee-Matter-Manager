"""The object-detection sidecar from the manager's side: what it's created
with on each kind of hardware, and when it's recreated."""

from __future__ import annotations

import asyncio
import importlib
import os
import tempfile
from pathlib import Path

from harness import Checker, FakeRuntime

APP, VIS = "zigbee-matter-manager", "zigbee-matter-manager-vision"


def _coral(root: Path, bound: bool) -> None:
    d = root / "sys/bus/pci/devices/0000:02:00.0"
    d.mkdir(parents=True, exist_ok=True)
    (d / "vendor").write_text("0x1ac1\n")
    (d / "device").write_text("0x089a\n")
    (d / "class").write_text("0x088000\n")
    (root / "dev").mkdir(exist_ok=True)
    if bound:
        drv = root / "sys/bus/pci/drivers/apex"
        drv.mkdir(parents=True, exist_ok=True)
        os.symlink(drv, d / "driver")
        (root / "dev/apex_0").write_text("")


def run() -> Checker:
    c = Checker("vision_sidecar")
    saved = {k: os.environ.get(k) for k in ("ZMM_DATA_DIR", "ZMM_SYSFS_ROOT", "ZMM_DEV_ROOT", "ZMM_CONTAINER_SOCK")}
    try:
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as hw:
            rt = FakeRuntime()
            os.environ.update(ZMM_CONTAINER_SOCK=rt.sock, ZMM_DATA_DIR=data,
                              ZMM_SYSFS_ROOT=f"{hw}/sys", ZMM_DEV_ROOT=f"{hw}/dev")
            import manager.accelerators
            import manager.containers
            import manager.vision
            importlib.reload(manager.containers)

            def load():
                importlib.reload(manager.accelerators)
                return importlib.reload(manager.vision)
            v = load()
            trigger = Path(v._SVC_TRIGGER)
            run_ = asyncio.run
            app_info = {"Image": "sha256:app1", "Config": {"Image": "localhost/zmm:1.2.3"},
                        "Mounts": [{"Destination": "/app/data", "Source": "/opt/zmm/data"},
                                   {"Destination": "/app/logs", "Source": "/opt/zmm/logs"},
                                   {"Destination": "/app/config", "Source": "/opt/zmm/config"}]}

            c.section("no accelerator")
            rt.inspect = {APP: app_info}
            st = run_(v.status())
            c.check("before enabling: not installed, and it says the CPU is what it would use",
                    not st["installed"] and not st["enabled"] and st["would_use"] == "cpu", st)
            res = run_(v.enable())
            create = next((p for p in rt.posts if p["path"] == "/containers/create"), {})
            body, hc = create.get("body") or {}, (create.get("body") or {}).get("HostConfig", {})
            c.check("created from the app's own image, running the vision module",
                    res["success"] and body.get("Image") == "localhost/zmm:1.2.3" and body.get("Cmd") == ["python", "-m", "vision"], body)
            c.check("shares the app's data and logs, and not its config (where the secrets are)",
                    hc.get("Binds") == ["/opt/zmm/data:/app/data:rw", "/opt/zmm/logs:/app/logs:rw"], hc.get("Binds"))
            c.check("told to use the CPU, with no devices passed in",
                    body.get("Env") == ["ZMM_VISION_BACKEND=cpu"] and "Devices" not in hc and "DeviceCgroupRules" not in hc, body)
            c.check("host networking and unless-stopped, like the other sidecars",
                    hc.get("NetworkMode") == "host" and hc.get("RestartPolicy") == {"Name": "unless-stopped"})
            c.check("starts it, remembers it's wanted, and asks the host for a boot-time service",
                    any(p["path"] == f"/containers/{VIS}/start" for p in rt.posts) and v.enabled()
                    and trigger.read_text() == "install")

            c.section("with a Coral")
            _coral(Path(hw), bound=False)
            v = load()
            c.check("a Coral without its driver is not used", v.hardware()["backend"] == "cpu")
            _coral(Path(hw), bound=True)
            v = load()
            hwinfo = v.hardware()
            c.check("with the driver bound it is, and its device node is passed in",
                    hwinfo["backend"] == "coral" and hwinfo["devices"] == [
                        {"PathOnHost": "/dev/apex_0", "PathInContainer": "/dev/apex_0", "CgroupPermissions": "rwm"}], hwinfo)
            running_cpu = {"Image": "sha256:app1", "Config": {"Env": ["PATH=/x", "ZMM_VISION_BACKEND=cpu"]},
                           "State": {"Running": True, "Status": "running"}}
            rt.inspect[VIS] = running_cpu
            st = run_(v.status())
            c.check("a sidecar still on the CPU shows both what it runs on and what it should",
                    st["running"] and st["backend"] == "cpu" and st["would_use"] == "coral", st)
            rt.posts.clear()
            t = {}
            run_(v.ensure(t))
            create = next((p for p in rt.posts if p["path"] == "/containers/create"), {})
            c.check("the watchdog recreates it on the Coral",
                    (create.get("body") or {}).get("Env") == ["ZMM_VISION_BACKEND=coral"]
                    and (create.get("body") or {}).get("HostConfig", {}).get("Devices") == hwinfo["devices"], create)
            rt.posts.clear()
            run_(v.ensure(t))
            c.check("…once, not on every tick if it didn't take", rt.posts == [], rt.posts)

            running = {"Image": "sha256:app1", "Config": {"Env": ["ZMM_VISION_BACKEND=coral"]},
                       "State": {"Running": True, "Status": "running"}}
            rt.inspect[VIS] = running
            rt.posts.clear()
            run_(v.ensure({}))
            c.check("up to date and running: left alone", not any("create" in p["path"] for p in rt.posts), rt.posts)
            res = run_(v.enable())
            c.check("enabling again just starts it", res["success"] and res["created"] is False
                    and not any("create" in p["path"] for p in rt.posts))
            rt.inspect[APP] = {**app_info, "Image": "sha256:app2"}
            rt.posts.clear()
            run_(v.ensure({}))
            c.check("after an app upgrade it's recreated from the new image",
                    any(p["path"] == "/containers/create" for p in rt.posts))

            c.section("disabling")
            rt.inspect[VIS] = {**running, "Image": "sha256:app2"}
            rt.posts.clear()
            rt.on_post = lambda path: {"trigger_then": trigger.read_text() if trigger.exists() else None}
            res = run_(v.disable())
            stop = next(p for p in rt.posts if p["path"].endswith(f"/{VIS}/stop"))
            c.check("asks for the service's removal before stopping, so the unit can't bring it back",
                    res["success"] and stop["trigger_then"] == "remove" and not v.enabled(), stop)
            rt.on_post = None
            rt.posts.clear()
            run_(v.ensure({}))
            c.check("a disabled sidecar is not brought back by the watchdog", rt.posts == [])

            c.section("the recorder, the same way")
            import manager.recorder
            rec = importlib.reload(manager.recorder)
            rt.inspect[APP] = app_info
            rt.posts.clear()
            res = run_(rec.enable())
            create = next((p for p in rt.posts if p["path"] == "/containers/create"), {})
            body = create.get("body") or {}
            c.check("its own container from the app's image, running the recorder module",
                    res["success"] and create.get("query") == "name=zigbee-matter-manager-recorder"
                    and body.get("Cmd") == ["python", "-m", "recorder"] and body.get("Image") == "localhost/zmm:1.2.3", create)
            c.check("sharing the data folder the recordings live in, with no devices and no variant",
                    "/opt/zmm/data:/app/data:rw" in body["HostConfig"]["Binds"] and body.get("Env") == []
                    and "Devices" not in body["HostConfig"], body)
            c.check("its autostart is asked for under its own name, apart from detection's",
                    Path(rec._SVC_TRIGGER).read_text() == "install" and "/recorder/" in rec._SVC_TRIGGER
                    and rec._SVC_TRIGGER != v._SVC_TRIGGER)
            rt.inspect["zigbee-matter-manager-recorder"] = {"Image": "sha256:app1", "Config": {"Env": []},
                                                            "State": {"Running": True, "Status": "running"}}
            rt.posts.clear()
            run_(rec.ensure({}))
            c.check("running on the app's image: the watchdog leaves it be", not any("create" in p["path"] for p in rt.posts))
            rt.inspect[APP] = {**app_info, "Image": "sha256:app9"}
            run_(rec.ensure({}))
            c.check("after an app upgrade it is recreated, so it runs the new code",
                    any(p["path"] == "/containers/create" for p in rt.posts))
            rt.inspect[APP] = app_info
            del rt.inspect["zigbee-matter-manager-recorder"]
            run_(rec.disable())

            c.section("without the pieces")
            rt.inspect[APP] = {"Image": "sha256:app2", "Mounts": []}
            del rt.inspect[VIS]
            res = run_(v.enable())
            c.check("an app with no data mount is refused with the reason", not res["success"] and "/app/data" in res["error"], res)
            c.check("unknown service actions are refused", not v.request_service("explode")["success"])
    finally:
        for k, val in saved.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        import manager.accelerators
        import manager.containers
        import manager.recorder
        import manager.vision
        for mod in (manager.containers, manager.accelerators, manager.vision, manager.recorder):
            importlib.reload(mod)
    return c
