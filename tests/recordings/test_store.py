"""The recordings folder (recorder/store.py): filing segments, cutting
clips, playback stretches, and what gets deleted when."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from harness import Checker

from recorder import core
from recorder import store as R

T0 = R.unstamp("20261010T120000Z")
GB = 1024 ** 3


class FakeFfmpeg:
    """Joins by writing the names of what it was given; grabs a 'frame'."""

    def __init__(self):
        self.calls, self.fail = [], False

    def __call__(self, args, timeout=120):
        self.calls.append(args)
        if self.fail:
            return False
        out = Path(args[-1])
        if "concat" in args:
            listing = Path(args[args.index("-i") + 1]).read_text()
            out.write_text(listing)
        else:
            out.write_bytes(b"\xff\xd8thumb")
        return True


def seg(store, cam, t, size=1000, live=False):
    d = store.live_dir(cam) if live else store.root / "segments" / cam / R.stamp(t)[:8]
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{R.stamp(t)}.ts"
    p.write_bytes(b"x" * size)
    return p


def run() -> Checker:
    c = Checker("store")
    with tempfile.TemporaryDirectory() as tmp:
        ff = FakeFfmpeg()
        free = {"v": 500 * GB}
        s = R.Store(Path(tmp) / "rec", ffmpeg=ff, disk_free=lambda: free["v"])

        c.section("filing segments")
        for i in range(4):
            seg(s, "front", T0 + i * 4, live=True)
        (s.live_dir("front") / R.stamp(T0 + 16)).with_suffix(".ts").write_bytes(b"")
        seg(s, "front", T0 + 20, live=True)
        s.adopt("front", writing=True)
        day = s.root / "segments/front/20261010"
        c.check("closed segments move to their day's folder; the one being written stays",
                len(list(day.iterdir())) == 4 and [p.name for p in s.live_dir("front").iterdir()] == ["20261010T120020Z.ts"],
                list(s.live_dir("front").iterdir()))
        c.check("an empty file left by a failed start is thrown away", not (day / "20261010T120016Z.ts").exists())
        s.adopt("front", writing=False)
        c.check("with ffmpeg stopped the last one is closed too", list(s.live_dir("front").iterdir()) == [] and len(list(day.iterdir())) == 5)
        c.check("timestamps round-trip through file names", R.unstamp(R.stamp(T0 + 7)) == T0 + 7)

        c.section("finding footage")
        got = [R.stamp(t)[9:] for t, _p in s.segments("front", T0 + 5, T0 + 9)]
        c.check("a stretch gets every segment that overlaps it, including the one it starts inside",
                got == ["120004Z", "120008Z"], got)
        c.check("nothing recorded then: nothing", s.segments("front", T0 + 4000, T0 + 4100) == [])
        c.check("another camera's footage isn't this one's", s.segments("yard", T0, T0 + 20) == [])
        midnight = R.unstamp("20261011T000000Z")
        seg(s, "front", midnight - 2)
        seg(s, "front", midnight + 2)
        got = [R.stamp(t) for t, _p in s.segments("front", midnight - 1, midnight + 3)]
        c.check("a stretch across midnight spans both day folders", got == ["20261010T235958Z", "20261011T000002Z"], got)
        cov = s.coverage("front", T0 - 60, T0 + 600)
        c.check("coverage merges back-to-back segments and shows the gaps",
                cov == [[T0, T0 + 16], [T0 + 20, T0 + 24]] or cov == [[T0, T0 + 24]], cov)

        c.section("clips")
        meta = s.build_clip("front", T0 + 9, T0 + 13, pre=5, post=5, labels=["person", "motion"])
        mp4 = s.root / "clips/front/20261010T120009Z.mp4"
        joined = mp4.read_text()
        c.check("a clip is the segments from before the event to after it, in order",
                [ln[-11:-4] for ln in joined.splitlines()] == ["120004Z", "120008Z", "120012Z"], joined)
        c.check("…described beside it", meta["labels"] == ["motion", "person"] and meta["camera"] == "front"
                and meta["start"] == T0 + 9 and meta["size"] == mp4.stat().st_size
                and json.loads(mp4.with_suffix(".json").read_text()) == meta, meta)
        c.check("…with a thumbnail taken from where the event starts in it",
                meta["thumb"] and ff.calls[-1][ff.calls[-1].index("-ss") + 1] == "5.0", ff.calls[-1])
        meta2 = s.build_clip("front", T0 + 1, T0 + 2, 0, 0, ["person"], thumb=b"\xff\xd8boxed")
        c.check("a detection frame, when there is one, is the thumbnail instead",
                (s.root / "clips/front" / f"{meta2['id']}.jpg").read_bytes() == b"\xff\xd8boxed")
        c.check("no footage for the time: no clip", s.build_clip("front", T0 + 5000, T0 + 5005, 5, 5, ["person"]) is None)
        ff.fail = True
        c.check("a failed join leaves nothing behind",
                s.build_clip("front", T0 + 21, T0 + 22, 0, 0, ["person"]) is None
                and not list((s.root / "clips/front").glob("*.part.mp4")) and not list((s.root / "clips/front").glob("*.txt")))
        ff.fail = False
        c.check("clips are listed newest first", [m["id"] for m in s.clips()] == ["20261010T120009Z", "20261010T120001Z"])
        c.check("a clip file is found by camera and id", s.clip_file("front", meta["id"], "mp4") == mp4)
        for cam, cid, ext in (("../front", meta["id"], "mp4"), ("front", "../../settings", "mp4"),
                              ("front", meta["id"], "json"), ("front", "20261010T999999Z", "mp4")):
            c.check(f"refused or missing: {cam}/{cid}.{ext}", s.clip_file(cam, cid, ext) is None)
        c.check("deleting removes the video, thumbnail and description",
                s.delete_clip("front", meta2["id"]) and not list((s.root / "clips/front").glob(f"{meta2['id']}.*")))
        c.check("…and says so if there was nothing to delete", not s.delete_clip("front", meta2["id"]))

        c.section("playback")
        p = s.play("front", T0, 12)
        c.check("a stretch of footage is joined into one file", p and len(p.read_text().splitlines()) == 3, p)
        n = len(ff.calls)
        c.check("…and reused while it's still wanted", s.play("front", T0, 12) == p and len(ff.calls) == n)
        c.check("no footage: nothing to play", s.play("front", T0 + 9000, 60) is None)

        c.section("settings")
        c.check("the space limit defaults to 20 GB", s.settings() == {"max_gb": 20})
        c.check("…and can be changed", s.save_settings({"max_gb": "50"}) == {"max_gb": 50.0} and s.settings()["max_gb"] == 50)
        for bad in ({"max_gb": 0}, {"max_gb": "lots"}, {}):
            try:
                s.save_settings(bad)
                ok = False
            except ValueError:
                ok = True
            c.check(f"refused: {bad}", ok)

    with tempfile.TemporaryDirectory() as tmp:
        c.section("retention")
        free = {"v": 500 * GB}
        s = R.Store(Path(tmp) / "rec", ffmpeg=FakeFfmpeg(), disk_free=lambda: free["v"])
        now = T0 + 3 * 3600
        for cam in ("front", "yard", "gone"):
            for age in (10, 1000, 7000):
                seg(s, cam, now - age)
        s.build_clip("front", now - 10, now - 8, 0, 0, ["person"])
        s.build_clip("gone", now - 1000, now - 998, 0, 0, ["person"])
        keep = {"front": {"footage_s": 3600, "clip_s": 86400}, "yard": {"footage_s": 900, "clip_s": 86400}}
        removed = s.prune(keep, now)
        left = {cam: len(s.segments(cam, now - 8000, now)) for cam in ("front", "yard", "gone")}
        c.check("each camera keeps footage for as long as its own setting says", left == {"front": 2, "yard": 1, "gone": 0}, left)
        c.check("a removed camera's footage goes, its clips stay for the default time",
                removed["segments"] == 6 and [m["camera"] for m in s.clips()] == ["front", "gone"], (removed, s.clips()))
        c.check("an emptied day folder is removed", not (s.root / "segments/gone/20261010").exists())
        s.prune({"front": {"footage_s": 3600, "clip_s": 5}, "yard": keep["yard"]}, now)
        c.check("clips past their days are deleted", [m["camera"] for m in s.clips()] == ["gone"])

        c.section("running out of room")
        for i in range(10):
            seg(s, "front", now - 500 + i * 4, size=1000)
        s.build_clip("front", now - 480, now - 478, 0, 0, ["person"])
        big = {"front": {"footage_s": 86400, "clip_s": 86400}, "yard": {"footage_s": 86400, "clip_s": 86400}}
        (s.root / "settings.json").write_text(json.dumps({"max_gb": 1}))
        s.prune(big, now)
        c.check("under the limit nothing more goes", len(s.segments("front", now - 600, now)) >= 10)
        real = R.Store.settings
        R.Store.settings = lambda self: {"max_gb": 6000 / GB}          # a limit small enough to test against
        try:
            s.prune(big, now)
            segs = s.segments("front", now - 8000, now) + s.segments("yard", now - 8000, now)
            used = sum(p.stat().st_size for _t, p in segs) + sum(m["size"] for m in s.clips())
            c.check("over the limit, the oldest footage is deleted until it fits", used <= 6000 and len(segs) > 0, (used, len(segs)))
            c.check("…before any clip is touched", len(s.clips()) == 2)
            newest = max(t for t, _p in segs)
            c.check("…and the newest footage is what's left", newest >= now - 20, now - newest)
            R.Store.settings = lambda self: {"max_gb": 1 / GB}
            s.prune(big, now)
            c.check("with no footage left to give, the oldest clips go", s.clips() == [] or len(s.clips()) < 2)
        finally:
            R.Store.settings = real
        seg(s, "front", now - 4)
        seg(s, "front", now - 8)
        free["v"] = R.MIN_FREE_BYTES - 1500
        s.prune(big, now)
        c.check("a nearly full disk is treated the same way, whatever the limit says",
                len(s.segments("front", now - 100, now)) < 2, s.segments("front", now - 100, now))
        free["v"] = 500 * GB
        cache = s.root / "cache"
        cache.mkdir(exist_ok=True)
        (cache / "old.mp4").write_text("x")
        os.utime(cache / "old.mp4", (now - 3600, now - 3600))
        (cache / "new.mp4").write_text("x")
        os.utime(cache / "new.mp4", (now - 5, now - 5))
        s.prune(big, now)
        c.check("joined playback files are dropped once nobody is watching", [p.name for p in cache.iterdir()] == ["new.mp4"])

        c.section("camera settings")
        n = R.normalise_record
        c.check("off by default", n(None, None, [])["mode"] == "off")
        c.check("switched on with nothing chosen: what the camera detects, else motion",
                n({"mode": "events"}, None, ["person", "vehicle"])["events"] == ["person", "vehicle"]
                and n({"mode": "events"}, None, [])["events"] == ["motion"])
        c.check("a thing the camera doesn't detect can't be an event", n({"mode": "events", "events": ["animal", "motion"]}, None, ["person"])["events"] == ["motion"])
        c.check("an edit keeps what it doesn't mention", n({"hours": 48}, {"mode": "continuous", "events": ["motion"], "pre_s": 9}, [])
                == {"mode": "continuous", "events": ["motion"], "pre_s": 9, "post_s": 10, "clip_days": 14, "hours": 48})
        for bad in ({"mode": "always"}, {"pre_s": 99}, {"hours": 0}, {"clip_days": "x"}):
            try:
                n(bad, None, [])
                ok = False
            except ValueError:
                ok = True
            c.check(f"refused: {bad}", ok)
        cmd = core.record_cmd("http://u:p@127.0.0.1:1984/api/stream.mp4?src=zmm_front", Path("/rec/live/front"))
        c.check("recording copies the stream, never re-encodes, into UTC-named segments",
                cmd[cmd.index("-c") + 1] == "copy" and "mpegts" in cmd and cmd[-1] == "/rec/live/front/%Y%m%dT%H%M%SZ.ts"
                and cmd[cmd.index("-i") + 1].startswith("http://u:p@"), cmd)
        c.section("what to record, kept for a restart")
        s2 = R.Store(Path(tmp) / "cfg")
        c.check("nothing saved yet: nothing to resume", s2.load_config() == [])
        cfg = [{"id": "front", "url": "http://u:p@h/x", "record": {"mode": "continuous"}}]
        s2.save_config(cfg)
        c.check("the camera list survives, in a file only its owner can read",
                s2.load_config() == cfg and (s2.root / "config.json").stat().st_mode & 0o777 == 0o600)
        (s2.root / "config.json").write_text("{not json")
        c.check("a damaged file is nothing to resume, not a crash", s2.load_config() == [])
    return c


if __name__ == "__main__":
    import sys
    r = run()
    print(f"\n{r.passed} passed, {len(r.failures)} failed")
    sys.exit(1 if r.failures else 0)
