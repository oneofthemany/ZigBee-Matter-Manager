"""The sidecar's loopback API (vision/server.py) over real HTTP, with fake
camera workers; and the pinned downloads (vision/assets.py)."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

from harness import Checker

from vision import assets
from vision.server import Hub, load_token, serve


class FakeDetector:
    ready, backend, want, note, ms, count = True, "cpu", "coral", "Coral not usable — running on the CPU", 21.5, 7


class FakeWorker:
    made, stopped = [], []

    def __init__(self, cfg, detector, on_change):
        self.cfg, self.on_change = cfg, on_change
        self.present = False
        FakeWorker.made.append(self)

    def start(self):
        pass

    def stop(self):
        FakeWorker.stopped.append(self.cfg["id"])

    def public(self):
        return {"online": True, "error": None, "frames": 1, "looks": 1,
                "objects": {"person": {"present": self.present}}, "last_at": None}

    def snapshot_rgb(self):
        return None

    def frame_rgb(self):
        return None


def call(port, method, path, token=None, body=None, timeout=10):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def run() -> Checker:
    c = Checker("sidecar_api")
    with tempfile.TemporaryDirectory() as tmp:
        c.section("token")
        path = Path(tmp) / "vision" / "token"
        tok = load_token(path)
        c.check("made on first use, 0600, and the same on every later read",
                len(tok) >= 32 and load_token(path) == tok and (path.stat().st_mode & 0o777) == 0o600)

        hub = Hub(FakeDetector(), worker_cls=FakeWorker)
        srv = serve(hub, tok, "127.0.0.1", 0)
        port = srv.server_address[1]
        try:
            c.section("auth")
            c.check("no token: refused", call(port, "GET", "/status")[0] == 401)
            c.check("wrong token: refused", call(port, "GET", "/status", "nope")[0] == 401)
            c.check("…for writes too", call(port, "PUT", "/config", None, {"cameras": []})[0] == 401)

            c.section("status and config")
            code, st = call(port, "GET", "/status", tok)
            c.check("status says what it runs on, and why that isn't what was asked for",
                    code == 200 and st["backend"] == "cpu" and st["wanted"] == "coral" and "Coral" in st["note"]
                    and st["cameras"] == {}, st)
            cams = [{"id": "front", "url": "http://u:p@127.0.0.1:1984/api/stream.mp4?src=zmm_front", "labels": ["person", "teapot"]},
                    {"id": "yard", "url": "rtsp://cam/2", "threshold": 5, "fps": 60}]
            code, body = call(port, "PUT", "/config", tok, {"cameras": cams})
            c.check("a camera list starts a worker each", code == 200 and sorted(hub.workers) == ["front", "yard"], body)
            c.check("unknown labels are dropped and numbers clamped",
                    hub.workers["front"].cfg["labels"] == ["person"] and hub.workers["yard"].cfg["threshold"] == 0.95
                    and hub.workers["yard"].cfg["fps"] == 5, hub.workers["yard"].cfg)
            _, st = call(port, "GET", "/status", tok)
            c.check("the config hash is reported back", st["config"] == body["config"] and st["config"])
            c.check("stream credentials never appear in status", "u:p@" not in json.dumps(st))
            made = len(FakeWorker.made)
            call(port, "PUT", "/config", tok, {"cameras": cams})
            c.check("the same list again restarts nothing", len(FakeWorker.made) == made and FakeWorker.stopped == [])
            cams[1]["url"] = "rtsp://cam/3"
            call(port, "PUT", "/config", tok, {"cameras": cams[1:]})
            c.check("a changed camera is restarted and a dropped one stopped",
                    sorted(FakeWorker.stopped) == ["front", "yard"] and list(hub.workers) == ["yard"]
                    and hub.workers["yard"].cfg["url"] == "rtsp://cam/3", FakeWorker.stopped)
            for bad, what in (({"cameras": [{"id": "../x", "url": "rtsp://c"}]}, "a bad id"),
                              ({"cameras": [{"id": "a", "url": "-f lavfi"}]}, "a URL that is an ffmpeg option"),
                              ({"cameras": [{"id": "a", "url": "rtsp://c -vf x"}]}, "a URL with a space"),
                              ({"cameras": "all"}, "not a list")):
                c.check(f"{what} is refused", call(port, "PUT", "/config", tok, bad)[0] == 400)
            c.check("…leaving what was running alone", list(hub.workers) == ["yard"])
            zone = {"id": "drive", "points": [[0, 0.5], [0.5, 0.5], [0.5, 1]], "labels": ["person", "teapot"]}
            call(port, "PUT", "/config", tok, {"cameras": [{**cams[1], "zones": [zone], "zones_only": 1}]})
            cfg = hub.workers["yard"].cfg
            c.check("zones reach the worker, labels limited to the camera's",
                    cfg["zones"] == [{"id": "drive", "labels": ["person"], "points": zone["points"]}]
                    and cfg["zones_only"] is True, cfg)
            for bad, what in (({**zone, "points": [[0, 0], [1, 1]]}, "a two-point zone"),
                              ({**zone, "id": "../x"}, "a bad zone id"),
                              ({**zone, "points": [[0], [1, 1], [0, 1]]}, "a point with one coordinate")):
                c.check(f"{what} is refused",
                        call(port, "PUT", "/config", tok, {"cameras": [{**cams[1], "zones": [bad]}]})[0] == 400)
            c.check("no frame yet: the zone editor's picture is a 404", call(port, "GET", "/frame/yard.jpg", tok)[0] == 404)
            call(port, "PUT", "/config", tok, {"cameras": cams[1:]})
            c.check("unknown paths are 404", call(port, "GET", "/nope", tok)[0] == 404)
            c.check("no detection yet: the snapshot is a 404, not an error",
                    call(port, "GET", "/snapshot/yard.jpg", tok)[0] == 404)

            c.section("long poll")
            _, st = call(port, "GET", "/status", tok)
            got = {}

            def poll():
                got["r"] = call(port, "GET", f"/status?after={st['version']}&wait=20", tok, timeout=30)
            t = threading.Thread(target=poll)
            t.start()
            t.join(0.4)
            c.check("with nothing new it waits", t.is_alive())
            w = hub.workers["yard"]
            w.present = True
            w.on_change()
            t.join(5)
            c.check("a change answers it at once, with the new state",
                    not t.is_alive() and got["r"][1]["version"] > st["version"]
                    and got["r"][1]["cameras"]["yard"]["objects"]["person"]["present"] is True, got.get("r"))
            code, late = call(port, "GET", f"/status?after={st['version']}&wait=20", tok)
            c.check("a poll that is already behind returns immediately", code == 200 and late["version"] > st["version"])
            c.check("junk in the query is a 400", call(port, "GET", "/status?after=x", tok)[0] == 400)
        finally:
            srv.shutdown()

        c.section("downloads")
        data = b"model bytes"
        root = Path(tmp) / "models"
        saved = dict(assets.FILES)
        try:
            assets.FILES["m.tflite"] = ("https://example/m", hashlib.sha256(data).hexdigest())
            calls = []

            def opener(url, timeout=0):
                calls.append(url)
                return io.BytesIO(data)
            p = assets.fetch("m.tflite", root, opener)
            c.check("a file is fetched and kept when its hash matches", p.read_bytes() == data and calls == ["https://example/m"])
            assets.fetch("m.tflite", root, opener)
            c.check("…and not fetched again", len(calls) == 1)
            p.write_bytes(b"tampered")
            assets.fetch("m.tflite", root, opener)
            c.check("a copy that no longer matches is replaced", p.read_bytes() == data and len(calls) == 2)
            assets.FILES["bad.tflite"] = ("https://example/bad", "0" * 64)
            try:
                assets.fetch("bad.tflite", root, opener)
                ok = False
            except RuntimeError as e:
                ok = "checksum" in str(e)
            c.check("a download that fails its hash is refused and not left on disk",
                    ok and not (root / "bad.tflite").exists() and not list(root.glob("*.part")))
        finally:
            assets.FILES.clear()
            assets.FILES.update(saved)
        c.check("every pinned file names a hash and an immutable URL",
                all(len(h) == 64 and ("/master/" not in u and "/main/" not in u and "latest" not in u)
                    for u, h in assets.FILES.values()), assets.FILES)
    return c


if __name__ == "__main__":
    import sys
    r = run()
    print(f"\n{r.passed} passed, {len(r.failures)} failed")
    sys.exit(1 if r.failures else 0)
