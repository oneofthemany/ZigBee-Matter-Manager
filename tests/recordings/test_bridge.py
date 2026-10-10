"""The sidecar's API over real HTTP (recorder/server.py), and ZMM's side of it
(modules/recordings.py): what it asks to be recorded and the signals it forwards."""

from __future__ import annotations

import asyncio
import json
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from harness import Checker

from modules import recordings as M
from recorder import core
from recorder import store as R
from recorder.server import load_token, serve
from test_store import FakeFfmpeg


def call(port, method, path, token=None, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=data)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class Go2rtc:
    def stream_url(self, name):
        return f"http://zmm:apipw@127.0.0.1:1984/api/stream.mp4?src={name}"


class Cams:
    def __init__(self):
        self.go2rtc, self.cameras, self.detection_snapshot = Go2rtc(), {}, None


class FakeClient:
    def __init__(self):
        self.down, self.config, self.hash, self.pushes = False, None, "", 0
        self.signals, self.thumbs, self.start_on = [], [], True

    def _check(self):
        if self.down:
            raise M.RecorderError("recorder unreachable (ConnectError)")

    async def status(self):
        self._check()
        return {"config": self.hash, "clips_made": 0,
                "cameras": {c["id"]: {"mode": c["record"]["mode"], "recording": True, "error": None, "event": False}
                            for c in self.config or []}}

    async def configure(self, cameras):
        self._check()
        self.config, self.pushes = cameras, self.pushes + 1
        self.hash = f"h{self.pushes}"
        return self.hash

    async def signal(self, cid, state):
        self._check()
        self.signals.append((cid, state))
        return self.start_on

    async def thumb(self, cid, jpeg):
        self.thumbs.append((cid, jpeg))


def run() -> Checker:
    c = Checker("bridge")
    with tempfile.TemporaryDirectory() as tmp:
        async def api():
            c.section("sidecar API")
            store = R.Store(Path(tmp) / "rec", ffmpeg=FakeFfmpeg(), disk_free=lambda: 10 ** 12)
            rec = core.Recorder(store, spawn=None)
            tok = load_token(store.root / "token")
            srv = serve(rec, tok, "127.0.0.1", 0, asyncio.get_running_loop())
            port = srv.server_address[1]
            go = lambda *a, **k: asyncio.to_thread(call, port, *a, **k)      # noqa: E731
            try:
                c.check("the token file is private", (store.root / "token").stat().st_mode & 0o777 == 0o600)
                c.check("no token, or the wrong one: refused", (await go("GET", "/status"))[0] == 401
                        and (await go("PUT", "/config", "nope", {"cameras": []}))[0] == 401
                        and (await go("POST", "/signal", None, {"camera": "front", "state": {}}))[0] == 401)
                cams = [{"id": "front", "url": "http://zmm:pw@127.0.0.1:1984/api/stream.mp4?src=zmm_front",
                         "record": {"mode": "events", "events": ["person"]}}]
                code, body = await go("PUT", "/config", tok, {"cameras": cams})
                code2, st = await go("GET", "/status", tok)
                c.check("a camera list is taken and its fingerprint reported back",
                        code == 200 and st["config"] == body["config"] and list(rec.cams) == ["front"], (body, st))
                c.check("…and never the stream address", "pw@" not in json.dumps(st))
                c.check("a bad list is a 400 and changes nothing",
                        (await go("PUT", "/config", tok, {"cameras": [{"id": "x", "url": "file:///etc/passwd"}]}))[0] == 400
                        and list(rec.cams) == ["front"])
                code, body = await go("POST", "/signal", tok, {"camera": "front", "state": {"person": True}})
                c.check("a signal starts an event and says a frame is wanted", code == 200 and body == {"started": True}
                        and "front" in rec._events, body)
                code, body = await go("PUT", "/thumb/front", tok, raw=b"\xff\xd8frame")
                c.check("the frame is accepted for it", code == 200 and body == {"kept": True}
                        and rec._events["front"]["thumb"] == b"\xff\xd8frame", body)
                c.check("junk for a signal is a 400", (await go("POST", "/signal", tok, {"camera": "front", "state": "on"}))[0] == 400)
                c.check("unknown paths are 404", (await go("GET", "/nope", tok))[0] == 404
                        and (await go("PUT", "/thumb/../x", tok, raw=b"x"))[0] == 404)
            finally:
                srv.shutdown()
        asyncio.run(api())

        async def bridge():
            c.section("what ZMM asks for")
            cams, client = Cams(), FakeClient()
            store = R.Store(Path(tmp) / "app", ffmpeg=FakeFfmpeg(), disk_free=lambda: 10 ** 12)
            b = M.RecorderBridge(cams, client, store)
            rec = {**R.RECORD_DEFAULTS, "mode": "events", "events": ["person", "motion"]}
            cams.cameras = {"front": {"id": "front", "enabled": True, "record": rec},
                            "yard": {"id": "yard", "enabled": True, "record": {**R.RECORD_DEFAULTS}},
                            "side": {"id": "side", "enabled": False, "record": {**rec, "mode": "continuous"}}}
            c.check("only enabled cameras with recording on, each on go2rtc's copy of its stream",
                    b.wanted() == [{"id": "front", "record": rec,
                                    "url": "http://zmm:apipw@127.0.0.1:1984/api/stream.mp4?src=zmm_front"}], b.wanted())
            c.check("…in a form the recorder accepts", core.clean_config(b.wanted())[0]["id"] == "front")
            await b.step()
            c.check("the first check sends the list", client.pushes == 1 and client.config == b.wanted())
            await b.step()
            c.check("…and not again while nothing changed", client.pushes == 1)
            cams.cameras["yard"]["record"] = {**rec, "mode": "continuous"}
            await b.step()
            c.check("a camera switched to recording is sent", client.pushes == 2 and len(client.config) == 2)
            client.hash = ""                              # a restarted sidecar that had nothing saved
            await b.step()
            c.check("a recorder that has lost its list gets it back", client.pushes == 3)
            pub = b.public()
            c.check("status for the UI, without stream addresses",
                    pub["reachable"] and pub["wanted"] == ["front", "yard"] and pub["cameras"]["front"]["recording"]
                    and "apipw" not in json.dumps(pub), pub)

            c.section("signals")
            async def frame(cid):
                return b"\xff\xd8boxed"
            cams.detection_snapshot = frame
            b.signal("front", {"motion": False, "person": True, "occupancy": False, "available": True, "vehicle": True})
            await asyncio.sleep(0.05)
            c.check("only the signals the camera records on are passed along",
                    client.signals == [("front", {"person": True, "motion": False})], client.signals)
            c.check("when that starts an event, the detection frame follows", client.thumbs == [("front", b"\xff\xd8boxed")])
            cams.cameras["yard"]["record"]["mode"] = "off"
            b.signal("yard", {"motion": True})
            b.signal("nope", {"motion": True})
            await asyncio.sleep(0.05)
            c.check("a camera that isn't recording sends nothing", len(client.signals) == 1)
            client.down = True
            b.signal("front", {"person": False})          # must not raise
            await asyncio.sleep(0.05)
            M.CHECK_S = 0.05
            await b.start()
            await asyncio.sleep(0.15)
            c.check("with the recorder down, signals are dropped quietly and the UI is told why",
                    b.public()["reachable"] is False and "unreachable" in b.public()["error"])
            client.down = False
            await asyncio.sleep(0.2)
            c.check("it reconnects by itself", b.public()["reachable"] is True)
            await b.stop()

            c.section("the folder, read by the app")
            t0 = R.unstamp("20261010T120000Z")
            d = store.root / "segments/front/20261010"
            d.mkdir(parents=True)
            (d / "20261010T120000Z.ts").write_bytes(b"x")
            meta = store.build_clip("front", t0 + 1, t0 + 2, 0, 0, ["person"])
            c.check("clips the recorder made are listed", [m["id"] for m in await b.clips()] == [meta["id"]])
            c.check("deleting one takes it off the list at once",
                    await b.delete_clip("front", meta["id"]) and await b.clips() == [])
        asyncio.run(bridge())
    return c


if __name__ == "__main__":
    import sys
    r = run()
    print(f"\n{r.passed} passed, {len(r.failures)} failed")
    sys.exit(1 if r.failures else 0)
