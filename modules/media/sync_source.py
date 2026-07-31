"""
Master PCM timeline sources for the sync engine (open-zone.md §4, §4.1).

Everything downstream of here — the per-device delay, the variable resampler,
the drift estimator, the chirp calibrator — addresses audio by absolute sample
position on a single shared timeline whose origin is the session epoch. A
source is just the thing that answers "give me ``frames`` samples starting at
timeline sample ``n0``".

``GeneratedSource`` answers from a closed-form test signal, so it is infinitely
seekable in both directions and has no delay. That property is what the Sync
Lab depends on: any device can be jumped anywhere on the timeline instantly.

``MediaSource`` answers from a ring buffer fed by an ffmpeg decoder, which is
the delay line of §4.1. Real audio is not seekable-in-the-future, so the
timeline it serves runs a fixed ``delay_s`` behind the live edge; readers are
offset back by the same amount and the group consequently plays that far
behind the source. The ring must span the full spread of per-device read
positions — the source delay, plus the largest startup-lag pre-compensation,
plus manual trim — because the furthest-behind device is reading history the
furthest-ahead device has long passed.

Underruns (a read wholly or partly outside the buffered window) are filled with
silence and counted rather than raised: a decoder hiccup should cost a gap, not
a dead session on every speaker.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Optional

import numpy as np

logger = logging.getLogger("modules.media.sync_source")

RATE = 44100
CHANNELS = 2
_S16_SCALE = 1.0 / 32768.0


# ----------------------------------------------------------------------
# Generated test signal (the original PoC timeline)
# ----------------------------------------------------------------------
def _gen_float_mono(n0: int, frames: int) -> np.ndarray:
    """Chord pad with a slow swell plus a 1 kHz click every 2 s. The click is
    the sync "ruler": it makes even ~10 ms misalignment audible as flam."""
    t = (n0 + np.arange(frames)) / RATE
    sig = (0.10 * np.sin(2 * np.pi * 220.0 * t)
           + 0.08 * np.sin(2 * np.pi * 277.18 * t)
           + 0.08 * np.sin(2 * np.pi * 329.63 * t))
    sig *= 0.7 + 0.3 * np.sin(2 * np.pi * 0.05 * t)
    ph = np.mod(t, 2.0)
    m = ph < 0.008
    if m.any():
        sig[m] += 0.85 * np.sin(2 * np.pi * 1000.0 * ph[m]) * np.exp(-ph[m] / 0.002)
    return sig


class GeneratedSource:
    """Closed-form timeline: a pure function of sample position, so every
    receiver and every stream that joins or seeks later lands on a
    bit-identical timeline."""

    kind = "generated"
    delay_s = 0.0
    channels = CHANNELS

    def read(self, n0: int, frames: int) -> np.ndarray:
        mono = _gen_float_mono(n0, frames).astype(np.float32)
        return np.repeat(mono[:, None], CHANNELS, axis=1)

    async def start(self) -> None:
        pass

    async def prime(self, timeout: float = 0.0, target_s: float = None) -> bool:
        return True

    def earliest_sample(self) -> int:
        return -(1 << 62)          # closed-form: seekable arbitrarily far back

    def buffered_s(self) -> float:
        return float("inf")

    async def close(self) -> None:
        pass

    def stats(self) -> dict:
        return {"kind": self.kind, "title": "Sync test signal", "delay_s": 0.0}


# ----------------------------------------------------------------------
# Ring buffer (§4.1 delay line)
# ----------------------------------------------------------------------
class _Ring:
    """Absolute-sample-indexed ring over the master PCM feed.

    ``end`` is the timeline sample one past the newest written sample; ``start``
    is the oldest still resident. Reads outside [start, end) are zero-filled."""

    def __init__(self, capacity: int, channels: int, origin: int = 0):
        self._buf = np.zeros((capacity, channels), dtype=np.float32)
        self._cap = capacity
        self._ch = channels
        self.start = origin
        self.end = origin
        self.underruns = 0
        self.underrun_samples = 0

    def write(self, block: np.ndarray) -> None:
        n = len(block)
        if n == 0:
            return
        if n >= self._cap:                     # only the newest fits
            block = block[-self._cap:]
            n = self._cap
        p = self.end % self._cap
        first = min(n, self._cap - p)
        self._buf[p:p + first] = block[:first]
        if n > first:
            self._buf[:n - first] = block[first:]
        self.end += n
        self.start = max(self.start, self.end - self._cap)

    def read(self, n0: int, frames: int) -> np.ndarray:
        out = np.zeros((frames, self._ch), dtype=np.float32)
        lo, hi = max(n0, self.start), min(n0 + frames, self.end)
        if hi > lo:
            p = lo % self._cap
            n = hi - lo
            first = min(n, self._cap - p)
            out[lo - n0:lo - n0 + first] = self._buf[p:p + first]
            if n > first:
                out[lo - n0 + first:hi - n0] = self._buf[:n - first]
        missing = frames - max(0, hi - lo)
        # Before the first write the timeline legitimately has nothing: that is
        # pipeline pre-roll, not a decoder underrun, and must not be counted.
        if missing > 0 and self.end > self.start:
            self.underruns += 1
            self.underrun_samples += missing
        return out


# ----------------------------------------------------------------------
# Real media
# ----------------------------------------------------------------------
class MediaSource:
    """ffmpeg-decoded audio on the shared timeline, optionally equalised.

    The decoder is supervised: if it exits (station drop, transcode error) it
    is restarted and decoding resumes at the *current* write head, so the
    timeline stays continuous and the outage appears as a gap of silence rather
    than as a permanent offset in everything downstream."""

    kind = "media"

    def __init__(self, url: str, epoch: float, delay_s: float = 2.0,
                 capacity_s: float = 20.0, eq_chain=None,
                 rate: int = RATE, channels: int = CHANNELS,
                 ffmpeg: str = "", loop_forever: bool = False,
                 title: str = "", url_provider=None):
        self.url = url
        self.title = title
        # Optional ``async () -> url``, consulted before every decoder start.
        # Some sources issue URLs that expire (Tidal signs them for minutes,
        # not hours); a session outliving one would otherwise decode to EOF and
        # stay dead. Re-resolving on each start turns expiry into the same
        # thing as a station dropping: a gap, then it continues.
        self._url_provider = url_provider
        self.epoch = epoch
        self.delay_s = float(delay_s)
        self.channels = channels
        self._rate = rate
        self._eq = eq_chain
        self._loop = bool(loop_forever)
        self._ffmpeg = ffmpeg or shutil.which("ffmpeg") or ""
        # The timeline origin is 0; readers sit `delay_s` behind real time, so
        # the ring only ever needs history, never future.
        self._ring = _Ring(int(capacity_s * rate), channels, origin=0)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reaping: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._restarts = 0
        self._last_error = ""

    # -- lifecycle ----------------------------------------------------
    async def start(self) -> None:
        if self._task is not None:
            return
        if not self._ffmpeg:
            raise RuntimeError("ffmpeg not found on the server")
        self._running = True
        self._task = asyncio.create_task(self._supervise())

    def earliest_sample(self) -> int:
        """Oldest timeline sample still resident. A reader positioned before
        this gets silence, so it is the floor for any start position."""
        return self._ring.start

    def buffered_s(self) -> float:
        return (self._ring.end - self._ring.start) / self._rate

    async def prime(self, timeout: float = 0.0, target_s: float = None) -> bool:
        """Fill the delay line before any device reads from it.

        Waiting for merely the first sample is not enough. A live decoder
        produces at 1x real time while the reader also advances at 1x, so the
        gap between the write head and the read position is whatever it was at
        the start — permanently. That gap is the headroom the per-device
        serve-ahead is drawn from, so it has to be established up front, and it
        has to cover ``delay_s`` plus the widest startup pre-compensation of
        any device about to join."""
        target = self.delay_s if target_s is None else float(target_s)
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self._primed_for(target):
                return True
            await asyncio.sleep(0.05)
        return self._primed_for(target)

    def _primed_for(self, target: float) -> bool:
        """Buffer depth alone is not the condition.

        A reader's start position is derived from *elapsed* time since the
        epoch, not from how much audio happens to be buffered. A source that
        bursts on connect — most HTTP streams do — fills the ring far faster
        than the clock advances, so depth is reached while elapsed is still
        short and the reader lands before the start of the timeline. Both have
        to be satisfied: enough audio, and enough clock."""
        return (self.buffered_s() >= target
                and (time.monotonic() - self.epoch) >= target)

    async def close(self) -> None:
        """Stop decoding and reap the decoder. Bounded at every step: session
        teardown must not be able to block on a wedged ffmpeg."""
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                pass
        await self._kill()

    def _kill_nowait(self) -> None:
        """Signal the decoder without awaiting it.

        Called from the decode loop's ``finally``, which may be running because
        the task was cancelled — and an ``await`` there can be re-cancelled
        immediately or block indefinitely. Reaping is left to ``close``."""
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except (ProcessLookupError, Exception):
                pass
        self._reaping = proc

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        proc = proc or getattr(self, "_reaping", None)
        self._reaping = None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
        except (ProcessLookupError, Exception):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            pass

    # -- decoding -----------------------------------------------------
    def _cmd(self) -> list:
        cmd = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
        if self._loop:
            cmd += ["-stream_loop", "-1"]
        if self.url.startswith(("http://", "https://")):
            # Ride out station hiccups inside ffmpeg before we resort to a
            # restart, which costs a gap.
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5"]
        return cmd + ["-i", self.url, "-vn", "-ac", str(self.channels),
                      "-ar", str(self._rate), "-f", "s16le", "-"]

    async def _supervise(self) -> None:
        backoff = 0.5
        while self._running:
            try:
                rc = await self._decode_once()
                if not self._running:
                    return
                self._restarts += 1
                logger.warning(f"Sync decoder for {self.url[:60]} exited "
                               f"(rc={rc}) — restarting in {backoff:.1f}s")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._running:
                    return
                self._last_error = str(e)
                self._restarts += 1
                logger.warning(f"Sync decoder error for {self.url[:60]}: {e} "
                               f"— restarting in {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    async def _refresh_url(self) -> None:
        if self._url_provider is None:
            return
        try:
            fresh = await self._url_provider()
        except Exception as e:
            logger.warning(f"Sync source URL refresh failed: {e}")
            return
        if fresh and fresh != self.url:
            logger.info("Sync source URL re-resolved (previous one expired "
                        "or rotated)")
        if fresh:
            self.url = fresh

    async def _decode_once(self) -> Optional[int]:
        await self._refresh_url()
        if not self.url:
            raise RuntimeError("no playable URL for this source")
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd(), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        proc = self._proc
        drain = asyncio.create_task(self._drain_stderr(proc))
        # s16le frame = channels * 2 bytes; read whole frames only.
        frame = self.channels * 2
        want = frame * int(self._rate * 0.1)
        try:
            carry = b""
            while self._running:
                await self._throttle()
                data = await proc.stdout.read(want)
                if not data:
                    break
                if carry:
                    data, carry = carry + data, b""
                if len(data) % frame:
                    cut = len(data) - (len(data) % frame)
                    data, carry = data[:cut], data[cut:]
                if not data:
                    continue
                if self._eq is not None:
                    try:
                        data = self._eq.process(data)
                    except Exception as e:
                        logger.warning(f"Sync EQ chain failed, bypassing: {e}")
                        self._eq = None
                pcm = (np.frombuffer(data, dtype="<i2")
                       .reshape(-1, self.channels).astype(np.float32) * _S16_SCALE)
                self._ring.write(pcm)
            return await proc.wait() if proc.returncode is None else proc.returncode
        finally:
            drain.cancel()
            self._kill_nowait()

    async def _drain_stderr(self, proc) -> None:
        """A full stderr pipe stalls the decoder on a long-running stream."""
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                msg = line.decode(errors="replace").rstrip()
                self._last_error = msg
                logger.warning(f"Sync decoder [{self.url[:40]}]: {msg}")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _throttle(self) -> None:
        """Hold the write head near ``delay_s`` ahead of the play point.

        A live stream paces itself, but a file or a station that bursts on
        reconnect would otherwise run away and overwrite the history the
        furthest-behind device is still reading."""
        while self._running:
            # time.monotonic(), not loop.time(): the session epoch in cast_sync
            # is monotonic and every timeline conversion must share one clock.
            play_now = (time.monotonic() - self.epoch) * self._rate
            if self._ring.end <= play_now + self.delay_s * self._rate:
                return
            await asyncio.sleep(0.05)

    # -- timeline -----------------------------------------------------
    def read(self, n0: int, frames: int) -> np.ndarray:
        return self._ring.read(n0, frames)

    def stats(self) -> dict:
        buffered = (self._ring.end - self._ring.start) / self._rate
        return {
            "kind": self.kind,
            "title": self.title,
            "url": self.url[:120],
            "delay_s": self.delay_s,
            "buffered_s": round(buffered, 2),
            "head_s": round(self._ring.end / self._rate, 2),
            "underruns": self._ring.underruns,
            "underrun_ms": round(self._ring.underrun_samples * 1000 / self._rate, 1),
            "restarts": self._restarts,
            "decoding": bool(self._proc and self._proc.returncode is None),
            "eq": self._eq is not None,
            **({"last_error": self._last_error[:200]} if self._last_error else {}),
        }
