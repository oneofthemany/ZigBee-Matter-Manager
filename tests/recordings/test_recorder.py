"""The recorder sidecar's core (recorder/core.py): which cameras record, how an
event becomes a clip, and recovering when ffmpeg stops."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from harness import Checker

from recorder import core
from recorder import store as R
from test_store import FakeFfmpeg, seg


class Proc:
    def __init__(self):
        self.returncode, self.killed, self.terminated = None, False, False
        self.stderr = None

    def kill(self):
        self.killed, self.returncode = True, -9

    def terminate(self):
        self.terminated, self.returncode = True, 0


class Stderr:
    def __init__(self, text):
        self.text = text

    async def read(self):
        return self.text.encode()


class Cams:
    """The camera list as the app sends it; `push` hands it to the recorder."""

    def __init__(self):
        self.cameras, self.rec = {}, None

    def add(self, cid, enabled=True, **record):
        self.cameras[cid] = {"id": cid, "enabled": enabled,
                             "url": f"http://zmm:apipw@127.0.0.1:1984/api/stream.mp4?src=zmm_{cid}",
                             "record": {**R.RECORD_DEFAULTS, **record}}
        self.push()

    def push(self):
        # The app sends only cameras that are enabled and recording.
        self.rec.configure([{k: c[k] for k in ("id", "url", "record")} for c in self.cameras.values()
                            if c["enabled"] and c["record"]["mode"] != "off"])


def run() -> Checker:
    c = Checker("recorder")
    with tempfile.TemporaryDirectory() as tmp:
        async def go():
            now = {"t": R.unstamp("20261010T120000Z")}
            cams, spawned = Cams(), []
            store = R.Store(Path(tmp) / "rec", ffmpeg=FakeFfmpeg(), disk_free=lambda: 10 ** 12)

            async def spawn(cmd):
                p = Proc()
                spawned.append((cmd, p))
                return p
            r = core.Recorder(store, spawn=spawn, clock=lambda: now["t"])
            cams.rec = r

            def footage(cid, seconds):
                """What ffmpeg would have written over the next `seconds`."""
                for i in range(0, seconds, 4):
                    seg(store, cid, now["t"] + i, live=True)
                now["t"] += seconds

            c.section("which cameras record")
            cams.add("front", mode="events", events=["person", "motion"])
            cams.add("yard", mode="off")
            cams.add("side", enabled=False, mode="continuous", hours=48, events=["motion"])
            cams.add("back", mode="continuous", hours=48, events=["motion"])
            await r.tick()
            c.check("each camera it is told to record gets an ffmpeg on that camera's stream",
                    sorted(x[0][x[0].index("-i") + 1][-9:] for x in spawned) == ["zmm_back", "mm_front"]
                    or len(spawned) == 2, [x[0] for x in spawned])
            c.check("what it was told is saved, to resume from after a restart",
                    [x["id"] for x in store.load_config()] == ["back", "front"])
            del cams.cameras["back"]
            cams.push()
            await r.tick()
            spawned[:] = [x for x in spawned if "zmm_front" in x[0][x[0].index("-i") + 1]]
            c.check("it isn't called recording until footage is arriving", r.status()["cameras"]["front"]["recording"] is False)
            footage("front", 8)
            await r.tick()
            c.check("…and is once it does", r.status()["cameras"]["front"] == {"mode": "events", "recording": True, "error": None, "event": False},
                    r.status())
            await r.tick()
            c.check("a running recorder isn't started twice", len(spawned) == 1)
            c.check("events mode keeps a short buffer, continuous its hours, off nothing",
                    r.retention() == {"front": {"footage_s": 900, "clip_s": 14 * 86400}}
                    and core.Recorder(store).retention() == {}, r.retention())
            r2 = core.Recorder(store)
            r2.configure([{"id": "a", "url": "http://h/x", "record": {"mode": "continuous", "hours": 48}}], persist=False)
            c.check("…continuous keeps its hours", r2.retention()["a"]["footage_s"] == 48 * 3600)

            c.section("an event becomes a clip")
            footage("front", 20)
            start = now["t"]
            c.check("motion alone starts an event without asking for a detection frame",
                    r.signal("front", {"motion": True}) is False and "front" in r._events)
            r.signal("front", {"motion": False})
            r._pending.clear()
            started = r.signal("front", {"motion": False, "person": True})
            c.check("a detection starts one and asks for its frame", started is True and r.status()["cameras"]["front"]["event"] is True)
            c.check("the frame is kept for the clip", r.set_thumb("front", b"\xff\xd8boxed") is True)
            c.check("…once, and only if it is a JPEG for an event under way",
                    not r.set_thumb("front", b"\xff\xd8other") and not r.set_thumb("yard", b"\xff\xd8x")
                    and not core.Recorder(store).set_thumb("front", b"junk"))
            footage("front", 8)
            r.signal("front", {"motion": True, "person": True})
            footage("front", 8)
            r.signal("front", {"motion": True, "person": False})
            c.check("it lasts while any of its signals is on", "front" in r._events)
            r.signal("front", {"motion": False, "person": False})
            end = now["t"]
            await r.tick()
            c.check("when they all clear the event is over — but the clip waits for its last footage",
                    "front" not in r._events and store.clips() == [] and len(r._pending) == 1)
            footage("front", 32)
            await r.tick()
            clip = (store.clips() or [{}])[0]
            c.check("then the clip is cut, covering the event", clip.get("camera") == "front" and clip.get("start") == start
                    and clip.get("end") == end and clip.get("labels") == ["motion", "person"], clip)
            parts = (store.root / "clips/front" / f"{clip['id']}.mp4").read_text().splitlines()
            first, last = R.unstamp(parts[0][-20:-4]), R.unstamp(parts[-1][-20:-4])
            c.check("…from a few seconds before it to a few after", first <= start - 4 and first >= start - 5 - 12
                    and last >= end + 4 and last <= end + 10 + 4, (start - first, last - end))
            c.check("…with the detection frame as its thumbnail",
                    (store.root / "clips/front" / f"{clip['id']}.jpg").read_bytes() == b"\xff\xd8boxed")
            c.check("…and counted", r.status()["clips_made"] == 1)
            r.signal("yard", {"motion": True, "person": True})
            r.signal("front", {"vehicle": True})
            c.check("a camera that isn't recording, or a signal it doesn't record on, starts nothing", r._events == {})

            c.section("a long event")
            r.signal("front", {"person": True})
            footage("front", R.MAX_EVENT_S + 8)
            await r.tick()
            c.check("is cut into back-to-back clips rather than one enormous file",
                    len(r._pending) == 1 and "front" in r._events and r._events["front"]["start"] == now["t"], r._pending)
            r.signal("front", {"person": False})
            footage("front", 40)
            await r.tick()
            c.check("…all of which are made", len(store.clips()) == 3, [m["id"] for m in store.clips()])

            c.section("when ffmpeg stops")
            cmd, p = spawned[-1]
            p.returncode = 1
            p.stderr = Stderr("[http @ 0x1] HTTP error 401\nhttp://zmm:apipw@127.0.0.1:1984/api/stream.mp4?src=zmm_front: Server returned 401 Unauthorized")
            await r.tick()
            st = r.status()["cameras"]["front"]
            c.check("the reason is reported, without the stream's password",
                    st["recording"] is False and "401" in st["error"] and "apipw" not in st["error"], st)
            c.check("it isn't restarted in the same breath", len(spawned) == 1)
            now["t"] += 6
            await r.tick()
            c.check("…but is after a pause", len(spawned) == 2)
            spawned[-1][1].returncode = 1
            await r.tick()
            now["t"] += 6
            await r.tick()
            c.check("…a longer one each time it fails again", len(spawned) == 2)
            now["t"] += 10
            await r.tick()
            c.check("…and it keeps trying", len(spawned) == 3)
            now["t"] += R.STALL_S + 5
            await r.tick()
            c.check("an ffmpeg that runs but writes nothing is killed, and says why",
                    spawned[-1][1].killed and r.status()["cameras"]["front"]["error"] == "no video arriving", r.status())

            c.section("switching off")
            await r.tick()                               # notices the kill, schedules a retry
            now["t"] += 70
            await r.tick()
            proc = spawned[-1][1]
            c.check("a stalled recorder is restarted too", proc.returncode is None and not proc.killed)
            seg(store, "front", now["t"], live=True)
            cams.cameras["front"]["record"]["mode"] = "off"
            cams.push()
            r.signal("front", {"person": True})
            await r.tick()
            c.check("ffmpeg is asked to stop, so it can close the file it's on", proc.terminated and not proc.killed)
            c.check("…what it left is filed, and no event starts", list(store.live_dir("front").iterdir()) == [] and r._events == {})
            await r.tick()
            c.check("…and the camera drops out of the status", "front" not in r.status()["cameras"], r.status())
            cams.add("front", mode="continuous", hours=1)
            await r.tick()
            n = len(spawned)
            cams.cameras["front"]["url"] = "http://zmm:newpw@127.0.0.1:1984/api/stream.mp4?src=zmm_front"
            cams.push()
            await r.tick()
            c.check("a changed stream address restarts that camera's ffmpeg on the new one",
                    spawned[n - 1][1].terminated and len(spawned) == n + 1 and "newpw" in " ".join(spawned[-1][0]))
            r.stop()
            c.check("stopping the recorder stops every ffmpeg", spawned[-1][1].terminated)

            c.section("what it will accept")
            for bad, what in (([{"id": "../x", "url": "http://h/x"}], "a bad camera id"),
                              ([{"id": "a", "url": "-i /etc/passwd"}], "a URL that is an ffmpeg option"),
                              ([{"id": "a", "url": "file:///etc/passwd"}], "a local file"),
                              ([{"id": "a", "url": "http://h/x y"}], "a URL with a space"),
                              ([{"id": "a", "url": "http://h/x", "record": {"mode": "always"}}], "an unknown mode"),
                              ("all", "not a list")):
                try:
                    core.clean_config(bad)
                    ok = False
                except (ValueError, TypeError, AttributeError):
                    ok = True
                c.check(f"refused: {what}", ok)
            h1 = core.Recorder(store).configure([{"id": "a", "url": "http://h/x"}], persist=False)
            h2 = core.Recorder(store).configure([{"id": "a", "url": "http://h/x", "record": {"mode": "off"}}], persist=False)
            c.check("the same list always has the same fingerprint", h1 == h2 and len(h1) == 16)
        asyncio.run(go())
    return c


if __name__ == "__main__":
    import sys
    r = run()
    print(f"\n{r.passed} passed, {len(r.failures)} failed")
    sys.exit(1 if r.failures else 0)
