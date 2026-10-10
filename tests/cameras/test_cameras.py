"""
The camera manager (modules/cameras.py): URLs and credentials, keeping go2rtc
in step, snapshots, and ONVIF motion as a device signal.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from harness import Checker

from modules import cameras as C
from modules.go2rtc import Go2rtcError


class FakeGo2rtc:
    def __init__(self):
        self.streams_ = {}
        self.down = False
        self.snaps = 0

    async def streams(self):
        if self.down:
            raise Go2rtcError("go2rtc unreachable")
        return dict(self.streams_)

    async def put_stream(self, name, src):
        if self.down:
            raise Go2rtcError("go2rtc unreachable")
        self.streams_[name] = src

    async def delete_stream(self, name):
        self.streams_.pop(name, None)

    async def snapshot(self, name, width=None):
        if self.down:
            raise Go2rtcError("no image")
        self.snaps += 1
        return b"\xff\xd8jpeg"


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeOnvif:
    instances = []

    def __init__(self, host, port, username, password):
        self.args = (host, port, username, password)
        self.queue = asyncio.Queue()
        FakeOnvif.instances.append(self)

    async def subscribe(self):
        return "http://cam/pull"

    async def pull(self, address, wait_s=10):
        return [await self.queue.get()]

    async def renew(self, address):
        pass

    async def unsubscribe(self, address):
        pass


def run() -> Checker:
    c = Checker("cameras")

    c.section("URLs")
    s = C.split_url("rtsp://admin:p%40ss@192.168.1.50:554/stream1")
    c.check("credentials in a pasted URL are lifted out",
            s == {"url": "rtsp://192.168.1.50:554/stream1", "username": "admin", "password": "p@ss"}, s)
    c.check("and joined back, escaped, only for go2rtc",
            C.join_url(s["url"], "admin", "p@ss:x") == "rtsp://admin:p%40ss%3Ax@192.168.1.50:554/stream1")
    for bad in ("exec:ffmpeg -i x", "ffmpeg:rtsp://cam", "file:///etc/passwd", "rtsp://cam/a b", "rtsp:///nohost"):
        try:
            C.split_url(bad)
            c.check(f"'{bad}' is refused", False)
        except ValueError:
            c.check(f"'{bad}' is refused", True)

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            saved = C.SECRETS_FILE
            C.SECRETS_FILE = str(Path(tmp) / "secrets.yaml")
            evals = []

            async def evaluate(ieee, changed):
                evals.append((ieee, changed))
            g, clock = FakeGo2rtc(), Clock()
            m = C.CameraManager(path=Path(tmp) / "cameras.json", go2rtc=g, evaluate=evaluate,
                                onvif_factory=FakeOnvif, clock=clock)
            try:
                c.section("adding")
                cam = await m.add({"name": "Front Door", "url": "rtsp://admin:secret@192.168.1.50/s1",
                                   "onvif": {"host": "192.168.1.50", "port": 2020}, "motion": True})
                c.check("a camera gets an id from its name", cam["id"] == "front_door", cam)
                c.check("the registry never holds the password",
                        "secret" not in (Path(tmp) / "cameras.json").read_text())
                c.check("the secrets file does", "secret" in Path(C.SECRETS_FILE).read_text())
                c.check("the API view says credentials exist, without them",
                        cam["has_credentials"] and "password" not in cam, cam)
                c.check("go2rtc gets the stream with credentials joined in",
                        g.streams_ == {"zmm_front_door": "rtsp://admin:secret@192.168.1.50/s1"}, g.streams_)
                for bad, what in (({"name": "X", "url": "rtsp://cam/1", "id": "go2rtc"}, "a reserved id"),
                                  ({"name": "Front Door", "url": "rtsp://cam/1"}, "a duplicate"),
                                  ({"name": "", "url": "rtsp://cam/1"}, "no name"),
                                  ({"name": "Y", "url": "rtsp://cam/1", "onvif": {"host": "a/b"}}, "a bad ONVIF host")):
                    try:
                        await m.add(bad)
                        c.check(f"{what} is refused", False)
                    except ValueError:
                        c.check(f"{what} is refused", True)

                c.section("editing")
                await m.update("front_door", {"name": "Porch", "url": "rtsp://192.168.1.50/s2"})
                c.check("a blank password keeps the stored one",
                        g.streams_["zmm_front_door"] == "rtsp://admin:secret@192.168.1.50/s2", g.streams_)
                await m.update("front_door", {"enabled": False})
                c.check("disabling takes the stream out of go2rtc", "zmm_front_door" not in g.streams_)
                c.check("and the camera out of the rule engine", m.automation_devices() == {})
                await m.update("front_door", {"enabled": True})

                c.section("keeping go2rtc in step")
                g.streams_ = {"zmm_gone": "rtsp://x", "users_own": "rtsp://y"}
                await m.reconcile()
                c.check("after a go2rtc restart our streams are put back, stale ones of ours dropped, others left",
                        set(g.streams_) == {"zmm_front_door", "users_own"}, g.streams_)
                g.down = True
                await m.reconcile()
                c.check("go2rtc being down is reported", m.last_error and "unreachable" in m.last_error)
                g.down = False

                c.section("snapshots")
                await m.snapshot("front_door")
                await m.snapshot("front_door")
                c.check("snapshots are cached briefly so a grid doesn't hammer the camera", g.snaps == 1)
                clock.t += 5
                await m.snapshot("front_door")
                c.check("and refreshed after", g.snaps == 2)
                c.check("a working snapshot marks the camera online", m.devices["front_door"].online is True)

                c.section("motion")
                await asyncio.sleep(0.05)
                watcher = FakeOnvif.instances[-1]
                c.check("the motion watcher uses the stored credentials",
                        watcher.args == ("192.168.1.50", 2020, "admin", "secret"), watcher.args)
                await watcher.queue.put({"motion": True})
                await asyncio.sleep(0.05)
                dev = m.devices["front_door"]
                c.check("motion reaches the rule engine as motion and occupancy",
                        evals[-1] == ("camera::front_door", {"motion": True, "occupancy": True})
                        and dev.state["occupancy"], evals)
                n = len(evals)
                await watcher.queue.put({"motion": True})
                await asyncio.sleep(0.05)
                c.check("repeated 'motion on' isn't a new event", len(evals) == n)
                clock.t += C.MOTION_HOLD_S + 1
                await m.clear_stale_motion()
                c.check("a camera that never says 'off' is cleared after the hold time",
                        evals[-1][1] == {"motion": False, "occupancy": False}, evals[-1])
                entries = m.device_entries()
                c.check("cameras join the device list with no stream details",
                        entries[0]["type"] == "Camera" and entries[0]["camera_id"] == "front_door"
                        and "url" not in entries[0], entries)

                c.section("removing")
                await m.delete("front_door")
                c.check("removing drops stream, credentials and watcher",
                        "zmm_front_door" not in g.streams_ and "secret" not in Path(C.SECRETS_FILE).read_text()
                        and not m._watchers, (g.streams_, m._watchers))
            finally:
                await m.stop()
                C.SECRETS_FILE = saved

    asyncio.run(scenario())
    return c
