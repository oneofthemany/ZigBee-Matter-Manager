"""Motion, regions, presence and the per-camera analyser (vision/), driven by
synthetic frames and a scripted detector."""

from __future__ import annotations

import numpy as np

from harness import Checker

from vision.motion import MotionDetector, from_input, region_for, to_input
from vision.tracker import Presence
from vision.worker import Analyser, ffmpeg_cmd

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

    c.section("ffmpeg")
    cmd = ffmpeg_cmd("rtsp://u:p@cam/s", 2)
    c.check("RTSP is read over TCP, video only, as small raw frames",
            cmd[cmd.index("-rtsp_transport") + 1] == "tcp" and "-an" in cmd and "rawvideo" in cmd
            and "scale=640:360" in cmd[cmd.index("-vf") + 1] and "fps=2" in cmd[cmd.index("-vf") + 1], cmd)
    c.check("the URL is one argument, never a shell string", cmd[cmd.index("-i") + 1] == "rtsp://u:p@cam/s")
    return c


if __name__ == "__main__":
    import sys
    r = run()
    print(f"\n{r.passed} passed, {len(r.failures)} failed")
    sys.exit(1 if r.failures else 0)
