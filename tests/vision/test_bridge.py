"""ZMM's side (modules/vision.py and the detection parts of modules/cameras.py):
what the sidecar is told to watch, and its reports becoming camera signals."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from harness import Checker

import vision
import vision.server
from modules import cameras as C
from modules import vision as V


class FakeGo2rtc:
    def __init__(self):
        self.streams_ = {}

    async def streams(self):
        return dict(self.streams_)

    async def put_stream(self, name, src):
        self.streams_[name] = src

    async def delete_stream(self, name):
        self.streams_.pop(name, None)

    async def snapshot(self, name, width=None):
        self.snaps = getattr(self, "snaps", []) + [(name, width)]
        if getattr(self, "down", False):
            raise C.Go2rtcError("go2rtc unreachable")
        return b"\xff\xd8live"

    def stream_url(self, name):
        return f"http://zmm:apipw@127.0.0.1:1984/api/stream.mp4?src={name}"


class FakeSidecar:
    """Stands in for VisionClient: holds a config like the real sidecar does."""

    def __init__(self):
        self.down = False
        self.config, self.hash, self.version = None, "", 1
        self.present = {}
        self.pushes = 0

    def _check(self):
        if self.down:
            raise V.VisionError("detection sidecar unreachable (ConnectError)")

    async def status(self, after=None, wait=25):
        self._check()
        cams = {c["id"]: {"online": True, "error": None, "frames": 5, "looks": 2, "last_at": None,
                          "objects": {g: {"present": self.present.get((c["id"], g), False)} for g in c["labels"]}}
                for c in self.config or []}
        return {"version": self.version, "config": self.hash, "ready": True, "backend": "coral", "wanted": "coral",
                "note": None, "inference_ms": 12.0, "cameras": cams}

    async def configure(self, cameras):
        self._check()
        self.config, self.pushes = cameras, self.pushes + 1
        self.hash = f"h{self.pushes}"
        self.version += 1
        return self.hash

    def restart(self):
        self.config, self.hash, self.present = None, "", {}
        self.version = 1


def run() -> Checker:
    c = Checker("bridge")
    c.check("ZMM's object groups are the sidecar's", tuple(vision.GROUPS) == V.OBJECT_GROUPS, vision.GROUPS)

    with tempfile.TemporaryDirectory() as tmp:
        C.SECRETS_FILE = str(Path(tmp) / "secrets.yaml")
        events = []

        async def evaluate(ieee, changed):
            events.append((ieee, dict(changed)))

        async def go():
            g, side = FakeGo2rtc(), FakeSidecar()
            m = C.CameraManager(path=Path(tmp) / "cameras.json", go2rtc=g, evaluate=evaluate)
            bridge = V.VisionBridge(m, side)
            kicks = []
            m.on_change = lambda: kicks.append(1)

            c.section("camera settings")
            await m.add({"name": "Front", "url": "rtsp://admin:secret@192.168.1.50/main"})
            dev = m.devices["front"]
            c.check("a camera without detection carries no object signals", "person" not in dev.state, dev.state)
            c.check("…and the sidecar is asked to watch nothing", m.detect_config() == [])
            cam = await m.update("front", {"detect": {"enabled": True, "labels": ["person", "animal", "teapot"]}})
            c.check("switching it on keeps only labels that exist",
                    cam["detect"] == {"enabled": True, "labels": ["person", "animal"], "threshold": 0.5, "url": ""}, cam["detect"])
            c.check("the device gains exactly those signals, off",
                    dev.state.get("person") is False and dev.state.get("animal") is False and "vehicle" not in dev.state, dev.state)
            c.check("changing a camera nudges the bridge", len(kicks) == 2, kicks)
            cfg = m.detect_config()
            c.check("with no sub-stream, detection shares go2rtc's copy of the main stream",
                    cfg == [{"id": "front", "labels": ["person", "animal"], "threshold": 0.5,
                             "url": "http://zmm:apipw@127.0.0.1:1984/api/stream.mp4?src=zmm_front"}], cfg)
            c.check("…so the camera's own login is never sent to the sidecar", "secret" not in str(cfg))
            c.check("…and go2rtc holds one stream for it", set(g.streams_) == {"zmm_front"}, g.streams_)
            await m.update("front", {"detect": {"enabled": True, "labels": ["person"], "url": "rtsp://192.168.1.50/sub",
                                                "threshold": 0.7}})
            c.check("a sub-stream becomes a second go2rtc stream, with the camera's login",
                    g.streams_.get("zmmd_front") == "rtsp://admin:secret@192.168.1.50/sub", g.streams_)
            c.check("…which is what detection then reads", m.detect_config()[0]["url"].endswith("src=zmmd_front")
                    and m.detect_config()[0]["threshold"] == 0.7)
            c.check("a label taken away takes its signal with it", "animal" not in dev.state)
            for bad in ({"enabled": True, "threshold": 2}, {"enabled": True, "threshold": "high"},
                        {"enabled": True, "url": "exec:rm -rf /"}):
                try:
                    await m.update("front", {"detect": bad})
                    ok = False
                except ValueError:
                    ok = True
                c.check(f"refused: {bad}", ok)
            g.streams_["zmmd_gone"] = "rtsp://x"
            del g.streams_["zmmd_front"]
            await m.reconcile()
            c.check("reconcile restores a lost detection stream and drops a stale one",
                    "zmmd_front" in g.streams_ and "zmmd_gone" not in g.streams_, g.streams_)
            await m.update("front", {"detect": {"enabled": False}})
            c.check("switching detection off removes its stream and signals",
                    "zmmd_front" not in g.streams_ and "person" not in dev.state and m.detect_config() == [])
            await m.update("front", {"detect": {"enabled": True, "labels": ["person", "vehicle"]}})

            c.section("the bridge")
            await bridge.step()
            c.check("the first poll pushes the camera list", side.pushes == 1 and side.config == m.detect_config())
            await bridge.step()
            c.check("…and not again while nothing changed", side.pushes == 1)
            side.present[("front", "person")] = True
            side.version += 1
            events.clear()
            await bridge.step()
            c.check("a person seen becomes `person: true` on the camera device, through the rule engine",
                    dev.state["person"] is True and events == [("camera::front", {"person": True})], events)
            await bridge.step()
            c.check("an unchanged report evaluates nothing", len(events) == 1)
            c.check("the camera list shows it", m.list()[0]["objects"] == {"person": True, "vehicle": False})
            c.check("…and so does the device list", m.device_entries()[0]["state"]["person"] is True)
            await m.update("front", {"detect": {"enabled": True, "labels": ["person"]}})
            await bridge.step()
            c.check("an edited camera is pushed again", side.pushes == 2 and side.config[0]["labels"] == ["person"])
            side.restart()
            events.clear()
            await bridge.step()
            c.check("a restarted sidecar (no config) gets the list back", side.pushes == 3 and side.config == m.detect_config())
            c.check("…and what it no longer sees is cleared", dev.state["person"] is False and events == [("camera::front", {"person": False})], events)
            pub = bridge.public()
            c.check("status for the UI: reachable, backend, per-camera health, and no stream URLs",
                    pub["reachable"] and pub["backend"] == "coral" and pub["cameras"]["front"]["online"]
                    and "apipw" not in str(pub), pub)

            c.section("pictures for notifications")
            async def boxed(cid):
                return b"\xff\xd8boxed"
            m.detection_snapshot = boxed
            side.present[("front", "person")] = True
            side.version += 1
            await bridge.step()
            c.check("while a person is detected, the picture is the detected frame",
                    await m.notification_image("front") == b"\xff\xd8boxed")
            side.present.clear()
            side.version += 1
            await bridge.step()
            img = await m.notification_image("front")
            c.check("with nothing detected it is what the camera sees now, sized for a phone",
                    img == b"\xff\xd8live" and g.snaps[-1] == ("zmm_front", 1280), g.snaps)

            async def broken(cid):
                raise V.VisionError("nothing detected yet")
            m.detection_snapshot = broken
            await m.apply_objects("front", {"person": True})
            c.check("no detection frame to be had: the live view instead",
                    await m.notification_image("front") == b"\xff\xd8live")
            await m.apply_objects("front", {"person": False})
            g.down = True
            c.check("go2rtc down: no picture, and no error for the notification to trip on",
                    await m.notification_image("front") is None)
            g.down = False
            c.check("an unknown camera has none", await m.notification_image("nope") is None)

            c.section("sidecar down")
            side.present[("front", "person")] = True
            side.version += 1
            await bridge.step()
            side.down = True
            V.RETRY_S = 0.05
            await bridge.start()
            await asyncio.sleep(0.2)
            c.check("presence is cleared rather than left standing", dev.state["person"] is False)
            c.check("…and the UI is told why", bridge.public()["reachable"] is False and "unreachable" in bridge.public()["error"])
            side.down = False
            await asyncio.sleep(0.3)
            c.check("it picks up again by itself when the sidecar returns", bridge.public()["reachable"] and dev.state["person"] is True)
            await bridge.stop()

            c.section("client")
            calls = []

            async def http(method, url, token, **kw):
                calls.append((method, url, token, kw))
                if url.endswith(".jpg"):
                    return 200, b"\xff\xd8jpeg"
                return (401, {"error": "unauthorized"}) if token == "bad" else (200, {"config": "abc", "version": 3})
            cl = V.VisionClient("http://127.0.0.1:8556", token=lambda: "tok", http=http)
            await cl.status(after=3)
            c.check("a long poll asks for changes after the version it has, with a longer timeout than the wait",
                    calls[-1][1].endswith("/status") and calls[-1][3]["params"]["after"] == 3
                    and calls[-1][3]["timeout"] > calls[-1][3]["params"]["wait"], calls[-1])
            c.check("the token goes with every call", all(x[2] == "tok" for x in calls))
            c.check("a snapshot comes back as JPEG bytes", (await cl.snapshot("front")).startswith(b"\xff\xd8"))
            try:
                await V.VisionClient("http://x", token=lambda: "bad", http=http).status()
                ok = False
            except V.VisionError as e:
                ok = "401" in str(e)
            c.check("a refused token is an error, not an empty status", ok)
            tok_path = Path(tmp) / "vision" / "token"
            c.check("the app and the sidecar agree on the token file's format",
                    V.load_token(tok_path) == vision.server.load_token(tok_path))

        asyncio.run(go())
    return c


if __name__ == "__main__":
    import sys
    r = run()
    print(f"\n{r.passed} passed, {len(r.failures)} failed")
    sys.exit(1 if r.failures else 0)
