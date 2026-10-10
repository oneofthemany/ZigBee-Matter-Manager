"""Motion, regions, presence and the per-camera analyser (vision/), driven by
synthetic frames and a scripted detector."""

from __future__ import annotations

import numpy as np

from harness import Checker

from vision.motion import MotionDetector, from_input, region_for, to_input
from vision.tracker import Presence
from vision import zones as Z
from vision.worker import Analyser, CameraWorker, ffmpeg_cmd

W, H = 640, 360


def scene(box=None, level=90):
    f = np.full((H, W, 3), level, np.uint8)
    if box:
        x0, y0, x1, y1 = box
        f[y0:y1, x0:x1] = 230
    return f


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class ScriptedDetector:
    """Answers with whatever `say` holds, and records what it was shown."""
    ready, size = True, 320

    def __init__(self):
        self.say, self.seen = [], []

    def detect(self, rgb, min_score=0.4):
        self.seen.append(rgb)
        return [d for d in self.say if d[1] >= min_score]


def run() -> Checker:
    c = Checker("pipeline")

    c.section("motion")
    m = MotionDetector()
    c.check("the first frame is the background, not motion", m.update(scene()) is None)
    c.check("a still scene has none", m.update(scene()) is None)
    box = m.update(scene((200, 120, 260, 240)))
    c.check("something appearing is boxed, to the cell", box == (200, 120, 264, 240), box)
    c.check("the whole picture changing (IR cut, a light) is not motion", m.update(scene(level=200)) is None)
    c.check("…and the frame after it is still again", m.update(scene(level=200)) is None)
    m = MotionDetector()
    m.update(scene())
    c.check("a speck is ignored", m.update(scene((8, 8, 16, 16))) is None)
    m = MotionDetector()
    m.update(scene())
    m.update(scene((200, 120, 260, 240)))
    c.check("something that stops is no longer motion", m.update(scene((200, 120, 260, 240))) is None)
    c.check("…and leaving is", m.update(scene()) == (200, 120, 264, 240))

    c.section("regions")
    c.check("a small box gets a square with room around it, not below the minimum",
            region_for((300, 150, 340, 190), W, H) == (240, 90, 400, 250), region_for((300, 150, 340, 190), W, H))
    c.check("near an edge the square slides inside the frame", region_for((0, 0, 40, 40), W, H) == (0, 0, 160, 160))
    c.check("too wide for a square: the whole frame", region_for((20, 100, 620, 300), W, H) == (0, 0, W, H))
    frame = scene((300, 150, 340, 190))
    region = (240, 90, 400, 250)
    inp, scale, ox, oy = to_input(frame, region, 320)
    c.check("a square region fills the input, scaled up", inp.shape == (320, 320, 3) and scale == 2.0 and (ox, oy) == (0, 0))
    c.check("…with the object where it should be", inp[160, 160, 0] == 230 and inp[10, 10, 0] == 90)
    c.check("a box in input terms maps back to frame pixels",
            from_input((0.375, 0.375, 0.625, 0.625), region, 320, scale, ox, oy) == (300, 150, 340, 190))
    inp, scale, ox, oy = to_input(frame, (0, 0, W, H), 320)
    c.check("the whole frame is letterboxed, not stretched", scale == 0.5 and (ox, oy) == (0, 70) and not inp[:70].any())
    back = from_input((0.0, 0.0, 1.0, 1.0), (0, 0, W, H), 320, scale, ox, oy)
    c.check("a box reaching into the letterbox bars is clamped to the frame", back == (0, 0, W, H), back)

    c.section("presence")
    p = Presence(["person", "vehicle"])
    hit = {"person": (0.8, (1, 2, 3, 4), "person")}
    c.check("one sighting isn't presence", p.looked(10.0, hit) == [] and not p.state["person"]["present"])
    c.check("two running is", p.looked(10.5, hit) == ["person"] and p.state["person"]["present"])
    c.check("a look that finds nothing doesn't clear it", p.looked(11.0, {}) == [] and p.state["person"]["present"])
    c.check("…nor does time, within the hold", p.expire(20.0) == [])
    c.check("gone for longer than the hold clears it", p.expire(23.0) == ["person"] and not p.state["person"]["present"])
    p = Presence(["person"])
    p.looked(10.0, hit)
    p.looked(11.0, {})
    c.check("a miss between two sightings starts the count again", p.looked(12.0, hit) == [])
    p = Presence(["person"])
    p.looked(10.0, hit)
    c.check("two sightings far apart aren't 'running'", p.looked(30.0, hit) == [])
    c.check("other groups are untouched", "vehicle" not in Presence(["person"]).state)

    c.section("one camera, end to end")
    clock, det = Clock(), ScriptedDetector()
    a = Analyser(det, ["person", "animal"], threshold=0.5, clock=clock)
    for _ in range(3):
        a.frame(scene())
        clock.t += 0.5
    c.check("a still scene never reaches the detector", a.looks == 0 and a.frames == 3)
    det.say = [("person", 0.8, (0.2, 0.2, 0.8, 0.8)), ("car", 0.9, (0.1, 0.1, 0.9, 0.9)), ("dog", 0.45, (0, 0, 1, 1))]
    walker = (300, 120, 340, 220)
    first = a.frame(scene(walker))
    clock.t += 0.5
    second = a.frame(scene((310, 120, 350, 220)))
    c.check("movement does", a.looks == 2)
    c.check("…and it's shown the area around the movement, not the whole frame",
            det.seen[0].shape == (320, 320, 3) and det.seen[0].mean() > scene(walker).mean() + 10, det.seen[0].mean())
    c.check("a person seen twice is reported once", (first, second) == ([], ["person"]), (first, second))
    c.check("groups the camera wasn't asked for and low scores are dropped",
            a.presence.public()["animal"]["present"] is False and "vehicle" not in a.presence.public())
    box = a.presence.public()["person"]["box"]
    c.check("the reported box is in frame pixels", box and 0 <= box[0] < box[2] <= W and 0 <= box[1] < box[3] <= H, box)
    looks = a.looks
    still = scene((310, 120, 350, 220))
    for _ in range(60):                                # 30 s standing still
        clock.t += 0.5
        a.frame(still)
    c.check("standing still: still present", a.presence.state["person"]["present"])
    c.check("…with the detector run only now and then, not every frame", 4 <= a.looks - looks <= 20, a.looks - looks)
    det.say = []
    changes = []
    for _ in range(60):
        clock.t += 0.5
        changes += a.frame(scene())
    c.check("once gone, it clears after the hold", changes == ["person"] and not a.presence.state["person"]["present"], changes)
    looks = a.looks
    for _ in range(20):
        clock.t += 0.5
        a.frame(scene())
    c.check("…and the detector goes back to idle", a.looks == looks)
    det.ready = False
    a2 = Analyser(det, ["person"], clock=clock)
    a2.frame(scene())
    c.check("frames before the model has loaded are not sent to it", a2.frame(scene(walker)) == [] and a2.looks == 0)

    c.section("zones")
    square = [(100, 100), (300, 100), (300, 300), (100, 300)]
    c.check("a point inside a polygon is inside, one outside is not",
            Z.inside((200, 200), square) and not Z.inside((50, 200), square) and not Z.inside((200, 350), square))
    ell = [(0, 0), (300, 0), (300, 100), (100, 100), (100, 300), (0, 300)]
    c.check("a concave shape's notch is outside", Z.inside((50, 250), ell) and Z.inside((250, 50), ell)
            and not Z.inside((200, 200), ell))
    c.check("an object is where its feet are, not its head", Z.foot((100, 50, 140, 250)) == (120.0, 248))
    zs = Z.prepare([{"id": "drive", "points": [[0, 0.5], [0.5, 0.5], [0.5, 1], [0, 1]], "labels": ["person", "teapot"]}],
                   W, H, ["person", "vehicle"])
    c.check("config points are fractions of the frame, labels limited to the camera's",
            zs == [{"id": "drive", "poly": [(0, 180), (320, 180), (320, 360), (0, 360)], "labels": ["person"]}], zs)
    for bad in ([{"id": "a", "points": [[0, 0], [1, 1]]}], [{"id": "", "points": [[0, 0], [1, 0], [1, 1]]}], "x",
                [{"id": "a", "points": [[0, 0], [1, 0], ["x", 1]]}]):
        try:
            Z.prepare(bad, W, H, ["person"])
            ok = False
        except (ValueError, TypeError):
            ok = True
        c.check(f"malformed zones are refused: {str(bad)[:40]}", ok)

    drive = {"id": "drive", "points": [[0, 0.5], [0.5, 0.5], [0.5, 1], [0, 1]]}        # bottom-left quarter
    on_drive, on_street = (100, 200, 140, 300), (500, 60, 540, 160)

    def watch(zones_only, where, detected=("person", 0.9, (0.3, 0.2, 0.7, 0.8))):
        clock, det = Clock(), ScriptedDetector()
        a = Analyser(det, ["person", "vehicle"], clock=clock, zones=[drive], zones_only=zones_only)
        a.frame(scene())
        det.say = [detected]
        for dx in (0, 8):
            clock.t += 0.5
            a.frame(scene((where[0] + dx, where[1], where[2] + dx, where[3])))
        return a

    a = watch(False, on_drive)
    p = a.presence.public()
    c.check("someone standing in a zone sets the zone's signal and the camera's",
            p["person:drive"]["present"] and p["person"]["present"], p)
    c.check("a zone has a signal per thing it looks for", set(p) == {"person", "vehicle", "person:drive", "vehicle:drive"}, set(p))
    a = watch(False, on_street)
    p = a.presence.public()
    c.check("outside the zone: the camera sees them, the zone doesn't",
            p["person"]["present"] and not p["person:drive"]["present"], p)
    a = watch(True, on_street)
    c.check("with 'ignore outside the zones', movement nowhere near a zone isn't even looked at", a.looks == 0)
    c.check("…and the camera's own signal stays off", not a.presence.public()["person"]["present"])
    a = watch(True, on_drive)
    c.check("…while someone in the zone still counts", a.presence.public()["person"]["present"] and a.looks == 2)
    # Movement near the zone, but the detector puts their feet outside it.
    a = watch(True, (300, 150, 340, 250), detected=("person", 0.9, (0.0, 0.6, 0.3, 0.9)))
    c.check("near the zone but not standing in it: looked at, not counted",
            a.looks > 0 and not a.presence.public()["person"]["present"], (a.looks, a.presence.public()))
    a = Analyser(ScriptedDetector(), ["person"], zones=[], zones_only=True)
    c.check("'only zones' with no zones drawn doesn't blind the camera", a.zones_only is False)

    w = CameraWorker({"id": "x", "url": "rtsp://c", "labels": ["person"], "zones": [drive]}, ScriptedDetector(), lambda: None)
    c.check("before a frame arrives there is no picture to draw zones on", w.frame_rgb() is None)
    w.analyser.frame(scene())
    f = w.frame_rgb()
    c.check("the frame handed to the zone editor has the zones outlined, the original untouched",
            tuple(f[180, 100]) == (255, 220, 0) and tuple(w.analyser.latest[180, 100]) == (90, 90, 90), f[180, 100])

    c.section("ffmpeg")
    cmd = ffmpeg_cmd("rtsp://u:p@cam/s", 2)
    c.check("RTSP is read over TCP, video only, as small raw frames",
            cmd[cmd.index("-rtsp_transport") + 1] == "tcp" and "-an" in cmd and "rawvideo" in cmd
            and "scale=640:360" in cmd[cmd.index("-vf") + 1] and "fps=2" in cmd[cmd.index("-vf") + 1], cmd)
    c.check("the URL is one argument, never a shell string", cmd[cmd.index("-i") + 1] == "rtsp://u:p@cam/s")
    c.check("frames are stretched, not letterboxed, so zone fractions mean the camera's own picture",
            "pad=" not in cmd[cmd.index("-vf") + 1] and "force_original_aspect_ratio" not in cmd[cmd.index("-vf") + 1])
    return c


if __name__ == "__main__":
    import sys
    r = run()
    print(f"\n{r.passed} passed, {len(r.failures)} failed")
    sys.exit(1 if r.failures else 0)
