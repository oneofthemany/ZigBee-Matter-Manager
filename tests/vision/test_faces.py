"""Faces (vision/faces.py): the geometry on real numbers, and the gallery over
the sidecar's real HTTP API with a scripted model standing in for YuNet+SFace."""

from __future__ import annotations

import io
import json
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

from harness import Checker

from vision import faces as F
from vision.server import Hub, serve


class FakeDetector:
    ready, backend, want, note, ms, count = True, "cpu", "cpu", None, 1.0, 1


def vec(*hot: int) -> np.ndarray:
    v = np.zeros(128, np.float32)
    v[list(hot)] = 1
    return v / np.linalg.norm(v)


def face(v=None, eye_px=80.0, pose="front", **kw):
    f = {"score": 0.9, "box": [0, 0, 10, 10], "pose": pose, "yaw": 0.0, "pitch": 0.6, "eye_px": eye_px,
         "sharp": 300.0, "crop": np.full((112, 112, 3), 90, np.uint8), **kw}
    if v is not None:
        f["vector"] = v
    return f


class ScriptedModels:
    """read() returns whatever the test queued, whatever the picture."""
    ready, note = True, None

    def __init__(self):
        self.next: list = []

    def read(self, rgb):
        return self.next


def jpeg(w=64, h=48, exif=None) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 90, 60)).save(buf, "JPEG", **({"exif": exif} if exif else {}))
    return buf.getvalue()


def call(port, method, path, body=None, raw=None, token="t"):
    data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=data,
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            b = r.read()
            return r.status, (b if r.headers["Content-Type"] == "image/jpeg" else json.loads(b or b"{}"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def refused(fn) -> str:
    try:
        fn()
    except F.FaceError as e:
        return str(e)
    return ""


def run() -> Checker:
    c = Checker("faces")

    c.section("pose from five landmarks")
    T = F.TEMPLATE
    c.check("the template itself is a front-on face", F.measure(T)["pose"] == "front", F.measure(T))
    turned = T.copy()
    turned[2, 0] -= 12
    c.check("a nose well toward image-left is turned left", F.measure(turned)["pose"] == "left", F.measure(turned))
    turned[2, 0] += 24
    c.check("…and toward image-right, right", F.measure(turned)["pose"] == "right")
    down = T.copy()
    down[2, 1] += 12
    c.check("a nose dropped toward the mouth is head down", F.measure(down)["pose"] == "down", F.measure(down))
    c.check("eye distance is reported in the picture's pixels", abs(F.measure(T * 3)["eye_px"] - 3 * 35.24) < 0.5)

    c.section("alignment")
    a = np.deg2rad(25)
    rot = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]], np.float32)
    kp = (T @ rot.T) * 2.5 + [140, 60]
    m = F.similarity(kp, T)
    back = kp @ m[:, :2].T + m[:, 2]
    c.check("a rotated, scaled, shifted face maps back onto the template", float(np.abs(back - T).max()) < 0.01)
    img = np.zeros((600, 600, 3), np.uint8)
    for x, y in kp:
        img[int(y) - 3:int(y) + 4, int(x) - 3:int(x) + 4] = 255
    crop = F.align(img, kp)
    c.check("the aligned crop has each landmark where the embedder expects it",
            crop.shape == (112, 112, 3) and all(crop[int(round(y)), int(round(x))].mean() > 150 for x, y in T))
    rng = np.random.default_rng(1)
    c.check("a flat crop is less sharp than a detailed one",
            F.sharpness(np.full((112, 112, 3), 100, np.uint8)) < F.sharpness(rng.integers(0, 255, (112, 112, 3), np.uint8)))

    c.section("which face a photo is of")
    c.check("no face is refused, in words", "No face" in refused(lambda: F.subject([])))
    c.check("two faces of similar size is refused", "More than one" in refused(lambda: F.subject([face(vec(1)), face(vec(2), 60)])))
    c.check("a bystander in the background isn't", F.subject([face(vec(1)), face(vec(2), 20)])["eye_px"] == 80.0)
    c.check("a face too small to compare is refused",
            "too small" in refused(lambda: F.subject([face(None, 10, error="Face too small — move closer")])))
    c.check("a front-on photo in the left slot gets a note", "straight on" in (F.slot_note("left", face(vec(1))) or ""))
    c.check("…and a turned one doesn't", F.slot_note("left", face(vec(1), pose="right")) is None)

    try:
        from PIL import Image
    except ImportError:
        print("    skipped: gallery and pictures need Pillow")
        return c

    c.section("pictures")
    exif = Image.Exif()
    exif[0x0112] = 6                                       # stored on its side, as a phone does
    c.check("a phone photo stored sideways comes out upright", F.decode(jpeg(64, 48, exif)).shape[:2] == (64, 48))
    c.check("something that isn't a picture is refused", "Couldn't read" in refused(lambda: F.decode(b"hello")))

    c.section("gallery, over the API")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gallery"
        models = ScriptedModels()
        srv = serve(Hub(FakeDetector(), faces=models, gallery=F.Gallery(root)), "t", "127.0.0.1", 0)
        port = srv.server_address[1]
        bare = serve(Hub(FakeDetector()), "t", "127.0.0.1", 0)
        try:
            c.check("without a token nothing is listed", call(port, "GET", "/faces", token="no")[0] == 401)
            c.check("a sidecar with no gallery says so", call(bare.server_address[1], "GET", "/faces")[0] == 404)
            code, sean = call(port, "POST", "/faces", {"name": "  Sean  M "})
            c.check("a person gets an id from their name, tidied", code == 200 and sean == {**sean, "id": "sean_m", "name": "Sean M"}, sean)
            c.check("the same name again is refused", call(port, "POST", "/faces", {"name": "sean m"})[0] == 400)
            c.check("an empty name is refused", call(port, "POST", "/faces", {"name": " "})[0] == 400)
            call(port, "POST", "/faces", {"name": "Ada"})

            models.next = [face(vec(1, 2))]
            code, s1 = call(port, "PUT", "/faces/sean_m/slot/front", raw=jpeg())
            c.check("a photo fills its slot and its crop is kept", code == 200 and s1["slot"] == "front"
                    and (root / "sean_m" / f"{s1['id']}.jpg").is_file(), s1)
            c.check("the vector never leaves the sidecar", "vector" not in s1
                    and "vector" not in json.dumps(call(port, "GET", "/faces")[1]))
            models.next = [face(vec(1, 3))]
            _, s2 = call(port, "PUT", "/faces/sean_m/slot/front", raw=jpeg())
            c.check("a second photo replaces the first, crop and all",
                    not (root / "sean_m" / f"{s1['id']}.jpg").exists() and (root / "sean_m" / f"{s2['id']}.jpg").is_file()
                    and len(call(port, "GET", "/faces")[1]["people"][0]["samples"]) == 1)
            models.next = [face(vec(1, 2))]
            code, s3 = call(port, "PUT", "/faces/sean_m/slot/left", raw=jpeg())
            c.check("a mis-posed photo is kept, with a note", code == 200 and "straight on" in s3["note"], s3)
            models.next = []
            c.check("a photo with no face is a 422 the phone can show",
                    call(port, "PUT", "/faces/sean_m/slot/down", raw=jpeg()) == (422, {"error": "No face found — face the camera, in good light"}))
            c.check("an unknown slot is refused", call(port, "PUT", "/faces/sean_m/slot/up", raw=jpeg())[0] == 400)
            c.check("an unknown person is a 404", call(port, "PUT", "/faces/nobody/slot/front", raw=jpeg())[0] == 404)
            c.check("an id can't climb out of the gallery", call(port, "GET", "/faces/../token.jpg")[0] == 404
                    and call(port, "DELETE", "/faces/..")[0] == 404)

            c.section("from a camera")
            models.next = [face(vec(1, 2)), face(vec(9)), face(None, 12, error="Face too small")]
            code, found = call(port, "POST", "/faces/detect?camera=door", raw=jpeg())
            fs = found["faces"]
            c.check("every face comes back with a thumbnail and who it looks like",
                    code == 200 and len(fs) == 3 and all(f["thumb"].startswith("data:image/jpeg;base64,") for f in fs)
                    and fs[0]["match"]["id"] == "sean_m" and fs[0]["match"]["match"], [f["match"] for f in fs])
            c.check("a stranger is named as no match", fs[1]["match"]["match"] is False, fs[1]["match"])
            c.check("a face too small says why", fs[2]["error"] == "Face too small" and fs[2]["match"] is None)
            code, took = call(port, "POST", "/faces/ada/samples", {"token": found["token"], "face": 1})
            c.check("picking a face adds it to the person, marked as from that camera",
                    code == 200 and took["source"] == "camera" and took["camera"] == "door" and took["slot"] is None, took)
            c.check("a face too small can't be picked", call(port, "POST", "/faces/ada/samples", {"token": found["token"], "face": 2})[0] == 422)
            c.check("a made-up token is refused", call(port, "POST", "/faces/ada/samples", {"token": "x", "face": 0})[0] == 400)

            c.section("how well the gallery holds together")
            people = {p["id"]: p for p in call(port, "GET", "/faces")[1]["people"]}
            agree = {s["slot"]: s["agree"] for s in people["sean_m"]["samples"]}
            c.check("each picture scores against the person's others", agree == {"front": 0.5, "left": 0.5}, agree)
            c.check("a person's only picture has nothing to agree with", people["ada"]["samples"][0]["agree"] is None)
            c.check("the nearest other person is named with a score",
                    people["sean_m"]["nearest"] == {"id": "ada", "name": "Ada", "score": 0.0}, people["sean_m"]["nearest"])

            c.section("renaming and removing")
            code, r = call(port, "PATCH", "/faces/sean_m", {"name": "Sean"})
            c.check("a rename keeps the id", code == 200 and r["id"] == "sean_m" and r["name"] == "Sean", r)
            c.check("…but not onto someone else's name", call(port, "PATCH", "/faces/sean_m", {"name": "ADA"})[0] == 400)
            code, img = call(port, "GET", f"/faces/sean_m/{s2['id']}.jpg")
            c.check("a picture's crop is served as a JPEG", code == 200 and img[:2] == b"\xff\xd8")
            call(port, "DELETE", f"/faces/sean_m/samples/{s2['id']}")
            c.check("removing a picture removes its crop", not (root / "sean_m" / f"{s2['id']}.jpg").exists()
                    and call(port, "GET", f"/faces/sean_m/{s2['id']}.jpg")[0] == 404)
            call(port, "DELETE", "/faces/sean_m")
            c.check("removing a person removes every picture of them", not (root / "sean_m").exists()
                    and [p["id"] for p in call(port, "GET", "/faces")[1]["people"]] == ["ada"])
        finally:
            srv.shutdown()
            bare.shutdown()
    return c
