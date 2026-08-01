"""
Master PCM timeline sources for the sync engine (open-zone.md §4, §4.1).
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Optional

import numpy as np

logger = logging.getLogger("modules.media.sync_source")

try:
    import zmm_eq as _dsp
    _HAVE_RUST = hasattr(_dsp, "xfade_mix")
except ImportError:
    _dsp = None
    _HAVE_RUST = False

RATE = 44100
CHANNELS = 2
_S16_SCALE = 1.0 / 32768.0

XFADE_GUARD_S = 0.35
XFADE_MIN_S = 0.25
XFADE_PEAK_CEIL = 0.97
XFADE_GUARD_WIN_S = 0.005

# Slack between the furthest-ahead reader and anything reworked in place.
XFADE_READER_MARGIN_S = 0.5
# Rate limit for the gap warning; the counter in stats() carries the total.
GAP_LOG_EVERY_S = 5.0


# ----------------------------------------------------------------------
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
# ----------------------------------------------------------------------
def fade_pair(n: int, p: float) -> tuple:
    """Complementary fade-out/fade-in envelopes over ``n`` samples."""
    t = np.linspace(0.0, 1.0, int(n), endpoint=True, dtype=np.float32)
    a = np.maximum(np.cos(np.pi * t / 2.0), 0.0) ** p
    b = np.maximum(np.sin(np.pi * t / 2.0), 0.0) ** p
    return a.astype(np.float32), b.astype(np.float32)


def _guard_win(rate: int) -> int:
    """Look-ahead width of the peak guard, odd for a centred window."""
    return max(3, (int(XFADE_GUARD_WIN_S * rate) | 1))


def _peak_guard(mixed: np.ndarray, src_peak: float,
                rate: int = RATE) -> np.ndarray:
    """Keep the overlap from peaking higher than the material going into it."""
    ceiling = max(XFADE_PEAK_CEIL, float(src_peak))
    amp = np.abs(mixed).max(axis=1)
    if not (amp > ceiling).any():
        return mixed
    need = np.minimum(1.0, ceiling / np.maximum(amp, 1e-9)).astype(np.float32)
    w = _guard_win(rate)
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
    """``p`` for :func:`fade_pair`, from the correlation of the two overlaps."""
    n = min(len(tail), len(head))
    if n < 2:
        return 1.0
    a = tail[:n].mean(axis=1).astype(np.float64)
    b = head[:n].mean(axis=1).astype(np.float64)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 1.0
    r = float(np.dot(a, b) / (na * nb))
    return 1.0 + min(1.0, abs(r))


def mix_overlap(tail: np.ndarray, head: np.ndarray, p: float,
                rate: int = RATE) -> np.ndarray:
    """Fade ``tail`` out under ``head`` in, peak-guarded. ``zmm_eq.xfade_mix``
    where the wheel is present, numpy otherwise — identical output."""
    if _HAVE_RUST:
        raw = _dsp.xfade_mix(np.ascontiguousarray(tail, dtype=np.float32),
                             np.ascontiguousarray(head, dtype=np.float32),
                             tail.shape[1], float(p), XFADE_PEAK_CEIL,
                             _guard_win(rate))
        return np.frombuffer(raw, dtype="<f4").reshape(tail.shape)
    g_out, g_in = fade_pair(len(tail), p)
    mixed = tail * g_out[:, None] + head * g_in[:, None]
    return _peak_guard(mixed, max(float(np.abs(tail).max()),
                                  float(np.abs(head).max())), rate)


# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
class _Ring:
    """Absolute-sample-indexed ring over the master PCM feed.

    Unlocked: reads and writes are serialised by the event loop (§A.1)."""

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

    def fill_silence(self, upto: int) -> int:
        """Commit ``[end, upto)`` as silence, fixing what readers past ``end``
        were already served (open-zone.md §4.1)."""
        n = upto - self.end
        if n <= 0:
            return 0
        if n >= self._cap:
            self._buf[:] = 0.0
            self.end = upto
            self.start = self.end - self._cap
        else:
            self.write(np.zeros((n, self._ch), dtype=np.float32))
        return n

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
        if count and missing > 0 and self.end > self.start:
            self.underruns += 1
            self.underrun_samples += missing
        return out


# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
class MediaSource:
    """ffmpeg-decoded audio on the shared timeline, optionally equalised."""

    kind = "media"

    def __init__(self, url: str, epoch: float, delay_s: float = 2.0,
                 capacity_s: float = 20.0, eq_chain=None,
                 rate: int = RATE, channels: int = CHANNELS,
                 ffmpeg: str = "", loop_forever: bool = False,
                 title: str = "", url_provider=None,
                 crossfade_s: float = 0.0, reader_pos=None):
        self.url = url
        self.title = title
        self._url_provider = url_provider
        self._last_rc: Optional[int] = None
        self._finished = False
        self.epoch = epoch
        self.delay_s = float(delay_s)
        self.channels = channels
        self._rate = rate
        self._eq = eq_chain
        self._reader_pos = reader_pos
        self._loop = bool(loop_forever)
        self._ffmpeg = ffmpeg or shutil.which("ffmpeg") or ""
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
        self._gap_samples = 0
        self._gap_logged = 0.0

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
        """Fill the delay line before any device reads from it."""
        target = self.delay_s if target_s is None else float(target_s)
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self._primed_for(target):
                return True
            await asyncio.sleep(0.05)
        return self._primed_for(target)

    def _primed_for(self, target: float) -> bool:
        """Buffer depth alone is not the condition."""
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
        """Signal the decoder without awaiting it."""
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

    def _cmd(self) -> list:
        cmd = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
        if self._loop:
            cmd += ["-stream_loop", "-1"]
        if self.url.startswith(("http://", "https://")):
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
            fresh = await self._url_provider(self._last_rc)
        except Exception as e:
            logger.warning(f"Sync source URL refresh failed: {e}")
            return
        title = ""
        if isinstance(fresh, dict):     # queue-aware provider: url + metadata
            title, fresh = (fresh.get("title") or ""), (fresh.get("url") or "")
        if not fresh:
            if self._last_rc == 0:
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
        # The arm survives the spawn; _xfade_open runs on the first block.
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd(), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        proc = self._proc
        drain = asyncio.create_task(self._drain_stderr(proc))
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
                if not self._xfade_want:
                    self._gap_close()
                if self._xfade_arm:
                    self._xfade_open()
                if self._xfade_want:
                    self._xfade_collect(pcm)
                else:
                    self._ring.write(pcm)
            return await proc.wait() if proc.returncode is None else proc.returncode
        finally:
            drain.cancel()
            if self._xfade_want:
                self._xfade_commit()
            self._kill_nowait()

    def _reader_head(self) -> Optional[float]:
        """Furthest-ahead reader, asked for rather than modelled (§4.1a).
        None while nothing is reading — priming, or no group."""
        if self._reader_pos is None:
            return None
        try:
            return self._reader_pos()
        except Exception:
            return None

    def _unserved_samples(self) -> int:
        """Timeline written but not yet reached by the furthest-ahead reader."""
        head = self._reader_head()
        if head is None:
            head = (time.monotonic() - self.epoch) * self._rate
        room = self._ring.end - head - XFADE_READER_MARGIN_S * self._rate
        return int(max(0, room))

    def _gap_close(self) -> None:
        """Commit an overrun before decoded audio lands on top of it (§4.1)."""
        head = self._reader_head()
        if head is None:
            return
        n = self._ring.fill_silence(int(head + XFADE_READER_MARGIN_S * self._rate))
        if not n:
            return
        self._gap_samples += n
        now = time.monotonic()
        if now - self._gap_logged >= GAP_LOG_EVERY_S:   # a slow source gaps per block
            self._gap_logged = now
            logger.warning(
                f"Sync source gap {self._gap_samples / self._rate:.2f}s total — "
                "the readers overtook the write head; committed as silence so "
                "every device hears the same thing")

    def _xfade_open(self) -> None:
        """Decide the overlap, on the incoming item's first decoded block —
        after its URL resolve and decoder start have been paid for."""
        if not self._xfade_arm:
            return
        self._xfade_arm = False
        room = self._unserved_samples() - int(XFADE_GUARD_S * self._rate)
        want = min(int(self._xfade_s * self._rate), room)
        if want < int(XFADE_MIN_S * self._rate):
            self._xfade_last = "no headroom at the seam"
            logger.info("Sync crossfade skipped — only "
                        f"{max(0, room) / self._rate:.2f}s of reworkable "
                        "timeline left once the next item opened")
            return
        self._xfade_want = want
        self._xfade_have = 0
        self._xfade_head = []

    def _xfade_collect(self, pcm: np.ndarray) -> None:
        """Hold the incoming item's head back until the overlap is complete,
        or until the headroom that authorised it is down to what is in hand."""
        self._xfade_head.append(pcm)
        self._xfade_have += len(pcm)
        if (self._xfade_have >= self._xfade_want
                or self._unserved_samples() <= self._xfade_have
                                               + int(XFADE_GUARD_S * self._rate)):
            self._xfade_commit()

    def _xfade_commit(self) -> None:
        """Mix the collected head over the outgoing tail, then resume."""
        blocks, want = self._xfade_head or [], self._xfade_want
        self._xfade_head, self._xfade_want, self._xfade_have = None, 0, 0
        if not blocks:
            return
        head = np.concatenate(blocks)
        # Last check before the ring is reworked.
        n = min(want, len(head), self._unserved_samples())
        if n < int(XFADE_MIN_S * self._rate):
            self._xfade_last = "headroom gone while collecting"
            logger.info("Sync crossfade abandoned — the seam outlasted its "
                        "headroom; writing a plain splice")
            self._ring.write(head)
            return
        at = self._ring.end - n
        tail = self._ring.read(at, n, count=False)
        p = fade_exponent(tail, head[:n])
        mixed = mix_overlap(tail, head[:n], p, self._rate)
        self._ring.splice(mixed, at)
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
        """Hold the write head near ``delay_s`` ahead of the play point."""
        while self._running:
            play_now = (time.monotonic() - self.epoch) * self._rate
            if self._ring.end <= play_now + self.delay_s * self._rate:
                return
            await asyncio.sleep(0.05)

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
            "rework_s": round(self._unserved_samples() / self._rate, 2),
            "gap_ms": round(self._gap_samples * 1000 / self._rate, 1),
            **({"crossfade_last": self._xfade_last} if self._xfade_last else {}),
            **({"last_error": self._last_error[:200]} if self._last_error else {}),
        }
