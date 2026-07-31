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

# Crossfade bounds. The guard is the slack left between the end of the fade and
# the play point while the incoming head is being fetched — without it a slow
# first block lands behind the readers and the fade is written into audio that
# has already been served. The minimum is the point below which an overlap
# stops being a transition and becomes a click with extra steps.
XFADE_GUARD_S = 0.35
XFADE_MIN_S = 0.25
# Just under the serve path's hard clip (cast_sync._to_s16 clips at 0.98).
XFADE_PEAK_CEIL = 0.97
# Gain-envelope window for that guard: long enough that the reduction itself is
# inaudible, short enough not to duck audio either side of a brief peak.
XFADE_GUARD_WIN_S = 0.005


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
    finished = False           # a closed-form timeline never runs out

    def read(self, n0: int, frames: int) -> np.ndarray:
        mono = _gen_float_mono(n0, frames).astype(np.float32)
        return np.repeat(mono[:, None], CHANNELS, axis=1)

    def peek(self, n0: int, frames: int) -> np.ndarray:
        """Observer read. Closed-form, so identical to read()."""
        return self.read(n0, frames)

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
# Crossfade law
# ----------------------------------------------------------------------
def fade_pair(n: int, p: float) -> tuple:
    """Complementary fade-out/fade-in envelopes over ``n`` samples.

        g_out = cos(pi*t/2)**p        g_in = sin(pi*t/2)**p

    One exponent spans the two laws a crossfade has to choose between, and
    both ends are exact rather than approximate:

        p = 1  ->  g_out^2 + g_in^2 == 1   constant POWER
        p = 2  ->  g_out   + g_in   == 1   constant AMPLITUDE

    Which one is correct depends on the material, and getting it wrong is
    audible as a dip in the middle of the fade — the thing that makes a
    crossfade sound like a crossfade instead of a transition. Uncorrelated
    tracks add as power, so equal-amplitude gains dip 3 dB at the midpoint;
    correlated ones add as amplitude, so equal-power *gains* 3 dB. Setting
    p = 1 + |r| from the measured correlation puts the level where it belongs
    at both extremes and slides between them, holding the envelope within
    +0.26 dB of unity at every correlation in between.

    That bound is on LEVEL, not on sample peak, and the difference matters:
    two uncorrelated signals can hold a flat RMS through the fade while their
    instantaneous peaks still add to +3 dB. Clipping is :func:`_peak_guard`'s
    job, not this function's.
    """
    t = np.linspace(0.0, 1.0, int(n), endpoint=True, dtype=np.float32)
    # Clamped before the power, and this is load-bearing rather than tidy:
    # cos(pi/2) evaluates a hair BELOW zero in float32, and a negative base
    # raised to a fractional exponent is NaN. Only the integer exponents (the
    # two endpoint laws) survive without this, so every adaptive fade would
    # write NaN onto the timeline — silence at best on whatever consumes it.
    a = np.maximum(np.cos(np.pi * t / 2.0), 0.0) ** p
    b = np.maximum(np.sin(np.pi * t / 2.0), 0.0) ** p
    return a.astype(np.float32), b.astype(np.float32)


def _peak_guard(mixed: np.ndarray, src_peak: float,
                rate: int = RATE) -> np.ndarray:
    """Keep the overlap from peaking higher than the material going into it.

    :func:`fade_pair` holds the *level* constant, which is what the ear tracks,
    but level is not peak: two uncorrelated signals at equal power keep a flat
    RMS while their instantaneous peaks still add, and two loud tracks can
    touch +3 dB for a sample or two mid-fade. The serve path clips hard at 0.98
    (``cast_sync._to_s16``), so left alone that is audible distortion placed
    exactly where the listener is paying attention.

    The ceiling is the louder source's own peak, not an absolute number, and
    that distinction is the whole design. At the edges of the fade the mix *is*
    one source at unity gain, so an absolute ceiling below that source's peak
    would demand a trim that cannot be applied without stepping the level
    against the audio either side. The job here is only to remove the excess
    the SUM creates — material that was already hot stays exactly as hot as it
    was, and is clipped (or not) by the serve path on the same terms as the
    rest of the track.

    The reduction is a per-sample gain envelope, not one scaled shape over the
    whole region. A single depth cannot satisfy constraints that sit at
    different places: one overshoot near an edge, where any fixed shape gives
    little reduction, forces a depth that annihilates the middle — measured at
    −47 dB mid-fade before this was a running envelope.

    So: the exact gain each sample needs, a running minimum over a short window
    to give the reduction look-ahead and release, smoothing so the gain movement
    is itself inaudible, and a final elementwise minimum because smoothing a
    running minimum can rise back above what a sample actually needed.
    """
    ceiling = max(XFADE_PEAK_CEIL, float(src_peak))
    amp = np.abs(mixed).max(axis=1)
    if not (amp > ceiling).any():
        return mixed
    need = np.minimum(1.0, ceiling / np.maximum(amp, 1e-9)).astype(np.float32)
    w = max(3, (int(XFADE_GUARD_WIN_S * rate) | 1))     # odd, for a centred window
    pad = w // 2
    win = np.lib.stride_tricks.sliding_window_view(
        np.pad(need, pad, mode="edge"), w)
    g = win.min(axis=1)
    k = np.hanning(w).astype(np.float32)
    k /= k.sum()
    g = np.convolve(np.pad(g, pad, mode="edge"), k, mode="same")[pad:pad + len(need)]
    g = np.minimum(g, need)
    return mixed * g[:, None]


def fade_exponent(tail: np.ndarray, head: np.ndarray) -> float:
    """``p`` for :func:`fade_pair`, from the correlation of the two overlaps.

    Correlation is measured on the level-normalised mono sum: what decides the
    law is whether the two signals reinforce or add in quadrature, and that is
    a property of their shapes, not of which one happens to be louder."""
    n = min(len(tail), len(head))
    if n < 2:
        return 1.0
    a = tail[:n].mean(axis=1).astype(np.float64)
    b = head[:n].mean(axis=1).astype(np.float64)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        # One side is silence. Nothing to reinforce, and equal-power is the
        # law that leaves the audible side untouched at the midpoint.
        return 1.0
    r = float(np.dot(a, b) / (na * nb))
    return 1.0 + min(1.0, abs(r))


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

    def splice(self, block: np.ndarray, at: int) -> int:
        """Overwrite resident samples in place, WITHOUT moving ``end``."""
        n = len(block)
        if n == 0:
            return 0
        lo, hi = max(at, self.start), min(at + n, self.end)
        if hi <= lo:
            return 0
        block = block[lo - at:hi - at]
        p = lo % self._cap
        m = hi - lo
        first = min(m, self._cap - p)
        self._buf[p:p + first] = block[:first]
        if m > first:
            self._buf[:m - first] = block[first:]
        return m

    def read(self, n0: int, frames: int, count: bool = True) -> np.ndarray:
        """``count=False`` for observers — the spectrum tap reads the timeline
        to look at it, not to play it, so a read that lands outside the window
        is its own problem and must not show up as a decoder underrun in the
        session's health stats."""
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
        if count and missing > 0 and self.end > self.start:
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
                 title: str = "", url_provider=None,
                 crossfade_s: float = 0.0):
        self.url = url
        self.title = title
        # Optional ``async (last_rc) -> url | {"url", "title"} | ""``,
        # consulted before every decoder start.
        #
        # Some sources issue URLs that expire (Tidal signs them for minutes,
        # not hours); a session outliving one would otherwise decode to EOF and
        # stay dead. Re-resolving on each start turns expiry into the same
        # thing as a station dropping: a gap, then it continues.
        #
        # ``last_rc`` is why the previous decode ended, and it is the whole
        # difference between a retry and an advance: None on the first start
        # (or when the decoder raised rather than exited), 0 when a finite
        # track played out cleanly, non-zero when ffmpeg failed. Only the
        # provider can know what should follow, so it decides — returning ""
        # after a clean end means "nothing follows", and the source finishes
        # instead of re-resolving the same track forever.
        self._url_provider = url_provider
        self._last_rc: Optional[int] = None
        self._finished = False
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

        self._xfade_s = max(0.0, float(crossfade_s))
        self._xfade_arm = False
        self._xfade_head: Optional[list] = None   # incoming blocks, pre-commit
        self._xfade_have = 0                      # samples collected so far
        self._xfade_want = 0                      # 0 = not collecting
        self._xfades = 0
        self._xfade_last = ""

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
                head = self._ring.end
                self._last_rc = await self._decode_once()
                if not self._running:
                    return
                if self._finished:      # the provider said there is no more
                    logger.info(f"Sync source finished: {self.title or self.url[:60]}")
                    return
                # A finite item that played out is not a fault: it costs no
                # restart in the health stats and, crucially, no backoff —
                # the gap before the next item is audible. Guarded on having
                # produced audio, so a URL that decodes to nothing falls
                # through to the error path instead of spinning.
                if (self._last_rc == 0 and self._url_provider is not None
                        and self._ring.end > head):
                    logger.info("Sync source item ended: "
                                f"{self.title or self.url[:60]}")
                    self._xfade_arm = self._xfade_s > 0
                    backoff = 0.5
                    continue
                self._restarts += 1
                logger.warning(f"Sync decoder for {self.url[:60]} exited "
                               f"(rc={self._last_rc}) — restarting in {backoff:.1f}s")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._running:
                    return
                # Raised rather than exited: no return code to reason about,
                # so the provider is told nothing and retries the same track.
                self._last_rc = None
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
            # The provider is told why the last decode ended: a clean exit is a
            # finite track finishing, which for a queue means "next one"; an
            # error is the same track needing another go with a fresh URL.
            fresh = await self._url_provider(self._last_rc)
        except Exception as e:
            # Keep the URL we have and let the supervisor retry. A resolver
            # blip (expired session, network) must not end the session when
            # the next attempt may well succeed.
            logger.warning(f"Sync source URL refresh failed: {e}")
            return
        title = ""
        if isinstance(fresh, dict):     # queue-aware provider: url + metadata
            title, fresh = (fresh.get("title") or ""), (fresh.get("url") or "")
        if not fresh:
            if self._last_rc == 0:
                # Played out cleanly and nothing follows: this is the end of
                # the material, not a failure. Say so, and let the supervisor
                # stop rather than re-resolve the same track for ever.
                self._finished = True
            return
        if fresh != self.url:
            logger.info(
                "Sync source URL re-resolved "
                f"({'next item' if self._last_rc == 0 else 'previous one expired or rotated'})")
        self.url = fresh
        if title:
            self.title = title

    async def _decode_once(self) -> Optional[int]:
        await self._refresh_url()
        if self._finished:
            return self._last_rc      # nothing more to play — don't start ffmpeg
        if not self.url:
            raise RuntimeError("no playable URL for this source")
        self._xfade_open()
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
                if self._xfade_want:
                    self._xfade_collect(pcm)
                else:
                    self._ring.write(pcm)
            return await proc.wait() if proc.returncode is None else proc.returncode
        finally:
            drain.cancel()
            # An item that ends while its head is still being collected never
            # reached the ring at all. Commit what there is: a short fade is a
            # worse crossfade, silently dropping the audio is a worse bug.
            if self._xfade_want:
                self._xfade_commit()
            self._kill_nowait()

    # -- crossfade --------------------------------------------
    def _unserved_samples(self) -> int:
        """Timeline written but not yet reached by the furthest-ahead reader."""
        play_now = (time.monotonic() - self.epoch) * self._rate
        return int(max(0, self._ring.end - play_now))

    def _xfade_open(self) -> None:
        """Decide the overlap for the item about to start."""
        if not self._xfade_arm:
            return
        self._xfade_arm = False
        # Leave the readers room to reach the fade before it is written: the
        # incoming head takes time to arrive, and a fade committed behind the
        # play point is audio that has already gone out.
        room = self._unserved_samples() - int(XFADE_GUARD_S * self._rate)
        want = min(int(self._xfade_s * self._rate), room)
        if want < int(XFADE_MIN_S * self._rate):
            self._xfade_last = "no headroom at the seam"
            logger.debug("Sync crossfade skipped — "
                         f"only {max(0, room) / self._rate:.2f}s unserved")
            return
        self._xfade_want = want
        self._xfade_have = 0
        self._xfade_head = []

    def _xfade_collect(self, pcm: np.ndarray) -> None:
        """Hold the incoming item's head back until the overlap is complete."""
        self._xfade_head.append(pcm)
        self._xfade_have += len(pcm)
        if self._xfade_have >= self._xfade_want:
            self._xfade_commit()

    def _xfade_commit(self) -> None:
        """Mix the collected head over the outgoing tail, then resume."""
        blocks, want = self._xfade_head or [], self._xfade_want
        self._xfade_head, self._xfade_want, self._xfade_have = None, 0, 0
        if not blocks:
            return
        head = np.concatenate(blocks)
        # Re-check: collecting took wall time and the readers used it.
        n = min(want, len(head), self._unserved_samples())
        if n < int(XFADE_MIN_S * self._rate):
            self._xfade_last = "headroom gone while collecting"
            logger.debug("Sync crossfade abandoned — writing plain seam")
            self._ring.write(head)
            return
        at = self._ring.end - n
        tail = self._ring.read(at, n, count=False)
        p = fade_exponent(tail, head[:n])
        g_out, g_in = fade_pair(n, p)
        mixed = tail * g_out[:, None] + head[:n] * g_in[:, None]
        mixed = _peak_guard(mixed, max(float(np.abs(tail).max()),
                                       float(np.abs(head[:n]).max())),
                            self._rate)
        self._ring.splice(mixed, at)
        # Whatever came in past the overlap is ordinary new timeline.
        if len(head) > n:
            self._ring.write(head[n:])
        self._xfades += 1
        self._xfade_last = f"{n / self._rate:.2f}s, p={p:.2f}"
        logger.info(f"Sync crossfade {n / self._rate:.2f}s "
                    f"(p={p:.2f}) into {self.title or self.url[:40]}")

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
    @property
    def finished(self) -> bool:
        """The material ran out: everything played and the provider had
        nothing to follow it with. The session owner polls this to stop the
        group instead of leaving it on silence."""
        return self._finished

    def read(self, n0: int, frames: int) -> np.ndarray:
        return self._ring.read(n0, frames)

    def peek(self, n0: int, frames: int) -> np.ndarray:
        """Observer read — see ``_Ring.read``'s ``count`` flag."""
        return self._ring.read(n0, frames, count=False)

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
            "finished": self._finished,
            "eq": self._eq is not None,
            "crossfade_s": self._xfade_s,
            "crossfades": self._xfades,
            **({"crossfade_last": self._xfade_last} if self._xfade_last else {}),
            **({"last_error": self._last_error[:200]} if self._last_error else {}),
        }
