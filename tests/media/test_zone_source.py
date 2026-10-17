"""
OpenZone: decoding ahead of the seam (open-zone.md §4.1b, §4.1c).

The real engine builds the source from a queue and real ffmpeg decodes it.
Each item is a WAV of one constant level, so what landed where on the
timeline can be read straight off the delay line. A fake reader stands where
the furthest-ahead device would, paced to real time.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import wave

import numpy as np

from harness import Checker

from modules.media.cast_sync import OpenZone, _Stream
from modules.media.sync_source import RATE, XFADE_READER_MARGIN_S, SKIP_GUARD_S

LEVEL = {"a": 1000, "b": 2000, "c": 3000}
READER_BEHIND_S = 0.1


def _wav(path: str, level: int, secs: float) -> None:
    frames = np.full((int(secs * RATE), 2), level, dtype="<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(frames.tobytes())


class _Rig:
    """An engine with a queue of WAVs and one reader paced to real time."""

    def __init__(self, tmp: str, items: list, delay_s: float,
                 crossfade_s: float = 0.0, resolve_delay_s: float = 0.0,
                 durations: bool = True, loop: bool = False):
        self.tmp = tmp
        self.items = items
        self.resolves: list = []
        z = OpenZone(None, {
            "source_delay_s": delay_s, "ring_capacity_s": 20.0,
            "crossfade_s": crossfade_s,
            "trims_file": f"{tmp}/t.json", "model_trims_file": f"{tmp}/mt.json",
            "groups_file": f"{tmp}/g.json", "model_file": f"{tmp}/m.json",
            "policy_file": f"{tmp}/p.json", "last_file": f"{tmp}/l.json",
            "trim_graph_file": f"{tmp}/tg.json",
        })

        # Slow only once playing: the session's own start is not under test.
        self.resolve_delay_s = resolve_delay_s
        self._slow = False

        async def resolver(media_type, source_id, owner=""):
            self.resolves.append(source_id)
            if self._slow:
                await asyncio.sleep(self.resolve_delay_s)
            return f"{tmp}/{source_id}.wav"

        z._url_resolver = resolver
        self.z = z
        self.rows = [{"source_id": sid, "title": sid.upper(),
                      **({"duration_ms": int(secs * 1000)} if durations else {})}
                     for sid, secs in items]
        self.loop = loop
        for sid, secs in items:
            _wav(f"{tmp}/{sid}.wav", LEVEL[sid], secs)

    async def start(self):
        z = self.z
        z._epoch = time.monotonic()
        src, err = await z._build_source(
            {"items": self.rows, "media_type": "x", "loop": self.loop}, "g")
        assert src is not None, err
        z._source, z.running = src, True
        self.src = src
        # Readers are seated once the delay line is primed, as in a session.
        await src.prime(timeout=5.0, target_s=0.5)
        st = _Stream("s", "cast:x", "X")
        st.pos = (time.monotonic() - z._epoch - READER_BEHIND_S) * RATE
        z._streams = {"s": st}
        self._pace = asyncio.create_task(self._reader())
        self._slow = True
        return src

    async def _reader(self):
        st = self.z._streams["s"]
        while True:
            st.pos = (time.monotonic() - self.z._epoch - READER_BEHIND_S) * RATE
            await asyncio.sleep(0.01)

    def level(self, t0: float, t1: float) -> int:
        """Mean level of the timeline over [t0, t1) seconds."""
        pcm = self.src.peek(int(t0 * RATE), int((t1 - t0) * RATE))
        return int(round(float(pcm.mean()) * 32768))

    async def stop(self):
        self._pace.cancel()
        await self.src.close()


def _gapless(c: Checker, tmp: str) -> None:
    c.section("a successor decoded ahead plays on with no gap")

    async def run(durations: bool) -> tuple:
        rig = _Rig(tmp, [("a", 5.0), ("b", 4.0)], delay_s=1.0,
                   resolve_delay_s=1.5, durations=durations)
        src = await rig.start()
        await asyncio.sleep(5.5)
        stats, pos = src.stats(), rig.z._queue_pos
        seam = rig.level(4.9, 5.0), rig.level(5.0, 5.1)
        await rig.stop()
        return stats, pos, seam

    async def go():
        stats, pos, seam = await run(durations=True)
        c.check("the queue moves to the second item", pos == 1, pos)
        c.check("the seam is taken from the decoded-ahead successor",
                stats["gapless"] == 1, stats)
        c.check("a 1.5 s resolve at the seam costs no silence",
                stats["gap_ms"] == 0.0, stats["gap_ms"])
        c.check("the second item starts on the sample after the first ends",
                seam == (LEVEL["a"], LEVEL["b"]), seam)
        stats, _, _ = await run(durations=False)
        c.check("without durations the same seam still gaps (control)",
                stats["gapless"] == 0 and stats["gap_ms"] > 500, stats["gap_ms"])
    asyncio.run(go())


def _crossfade(c: Checker, tmp: str) -> None:
    c.section("a crossfade is no longer charged the successor's start-up")

    async def run(durations: bool) -> dict:
        rig = _Rig(tmp, [("a", 8.0), ("b", 4.0)], delay_s=3.0, crossfade_s=1.5,
                   resolve_delay_s=2.3, durations=durations)
        src = await rig.start()
        await asyncio.sleep(6.0)
        stats = src.stats()
        await rig.stop()
        return stats

    async def go():
        stats = await run(durations=True)
        c.check("the 1.5 s crossfade is granted",
                stats["crossfades"] == 1
                and stats.get("crossfade_last", "").startswith("1.50s"), stats)
        stats = await run(durations=False)
        c.check("resolving at the seam leaves no room for it (control)",
                stats["crossfades"] == 0, stats.get("crossfade_last"))
    asyncio.run(go())


def _skip(c: Checker, tmp: str) -> None:
    c.section("a skip is spliced just ahead of the furthest reader")

    async def go():
        rig = _Rig(tmp, [("a", 12.0), ("b", 6.0), ("c", 6.0)], delay_s=3.0)
        src = await rig.start()
        await asyncio.sleep(1.5)
        end_at_skip = src._ring.end
        res = await rig.z.skip_to(2)
        c.check("the skip is accepted", res.get("success"), res)
        for _ in range(100):
            if src.stats()["skip_splices"]:
                break
            await asyncio.sleep(0.05)
        reader = rig.z._reader_head()
        origin = src._item_origin
        stats = src.stats()
        c.check("it is spliced, not left to the write head",
                stats["skip_splices"] == 1, stats)
        ahead = (origin - reader) / RATE
        c.check("the cut lands just past the reader's margin",
                XFADE_READER_MARGIN_S <= ahead
                <= XFADE_READER_MARGIN_S + SKIP_GUARD_S + 0.3, ahead)
        c.check("…seconds before the old write head",
                (end_at_skip - origin) / RATE > 2.0,
                (end_at_skip - origin) / RATE)
        t = origin / RATE
        c.check("the outgoing item plays up to the cut",
                rig.level(t - 0.2, t) == LEVEL["a"], rig.level(t - 0.2, t))
        c.check("the target plays from it, past a short declick",
                rig.level(t + 0.06, t + 0.5) == LEVEL["c"],
                rig.level(t + 0.06, t + 0.5))
        mid = rig.level(t + 0.02, t + 0.03)
        c.check("the declick blends the two rather than stepping",
                LEVEL["a"] < mid < LEVEL["c"], mid)
        c.check("the successor decoded for the old position is dropped",
                src._next is None and stats["gapless"] == 0, src._next_rec)
        c.check("no reader is starved by the cut", stats["gap_ms"] == 0.0,
                stats["gap_ms"])
        c.check("the queue cursor is on the target", rig.z._queue_pos == 2)
        await rig.stop()
    asyncio.run(go())


def _double_skip(c: Checker, tmp: str) -> None:
    c.section("a second skip before the first lands replaces it")

    async def go():
        rig = _Rig(tmp, [("a", 12.0), ("b", 6.0), ("c", 6.0)], delay_s=3.0,
                   resolve_delay_s=0.3)
        src = await rig.start()
        await asyncio.sleep(1.0)
        await rig.z.skip_to(1)
        await asyncio.sleep(0.05)
        await rig.z.skip_to(2)
        await asyncio.sleep(2.0)
        t = src._item_origin / RATE
        c.check("only one splice is made", src.stats()["skip_splices"] == 1,
                src.stats())
        c.check("…to the second target",
                rig.level(t + 0.06, t + 0.5) == LEVEL["c"],
                rig.level(t + 0.06, t + 0.5))
        await rig.stop()
    asyncio.run(go())


def _no_leak(c: Checker, tmp: str) -> None:
    c.section("skipping back and forth does not leak descriptors")

    def fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    async def go():
        rig = _Rig(tmp, [("a", 30.0), ("b", 30.0), ("c", 30.0)], delay_s=3.0)
        src = await rig.start()

        async def hop(i):
            await rig.z.skip_to(i)
            for _ in range(60):
                if src._skip_task is None and src._skip_dec is None:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.3)

        await hop(1)
        base = fds()
        for i in (2, 1, 2, 1, 2, 1):
            await hop(i)
        await asyncio.sleep(0.5)
        c.check("six more splices are made", src.stats()["skip_splices"] == 7,
                src.stats()["skip_splices"])
        c.check("the open descriptors are where they were", fds() <= base + 1,
                (base, fds()))
        await rig.stop()
    asyncio.run(go())


def _repeat_all(c: Checker, tmp: str) -> None:
    c.section("repeat-all walks the queue instead of looping its first item")

    async def go():
        rig = _Rig(tmp, [("a", 1.0), ("b", 1.0)], delay_s=3.0, loop=True)
        src = await rig.start()
        await asyncio.sleep(1.5)
        c.check("the second item plays after the first",
                rig.level(1.1, 1.9) == LEVEL["b"], rig.level(1.1, 1.9))
        c.check("…and the queue wraps back to the first",
                rig.level(2.1, 2.9) == LEVEL["a"], rig.level(2.1, 2.9))
        await rig.stop()
    asyncio.run(go())


def _teardown(c: Checker, tmp: str) -> None:
    c.section("closing reaps every decoder, including the one ahead")

    async def go():
        rig = _Rig(tmp, [("a", 30.0), ("b", 6.0)], delay_s=1.0)
        rig.rows[0]["duration_ms"] = 10_000      # successor starts at once
        src = await rig.start()
        for _ in range(60):
            if src._next is not None:
                break
            await asyncio.sleep(0.05)
        procs = [d._proc for d in (src._dec, src._next) if d is not None]
        c.check("two decoders are running", len(procs) == 2
                and all(p.returncode is None for p in procs), procs)
        await rig.stop()
        c.check("both have exited after close",
                all(p.returncode is not None for p in procs),
                [p.returncode for p in procs])
    asyncio.run(go())


def run() -> Checker:
    c = Checker("zone_source")
    if not shutil.which("ffmpeg"):
        c.check("ffmpeg is installed", False, "needed to decode the queue")
        return c
    tmp = tempfile.mkdtemp(prefix="zone_source_")
    try:
        _gapless(c, tmp)
        _crossfade(c, tmp)
        _skip(c, tmp)
        _double_skip(c, tmp)
        _no_leak(c, tmp)
        _repeat_all(c, tmp)
        _teardown(c, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
