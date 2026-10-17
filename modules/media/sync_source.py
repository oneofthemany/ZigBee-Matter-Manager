"""
Master PCM timeline sources for the sync engine (open-zone.md §4, §4.1).
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections import deque
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

# Decoding ahead of a seam (open-zone.md §4.1c).
DECODE_HW_S = 1.0          # read-ahead of the decoder that is playing
PREFETCH_LEAD_S = 20.0     # successor starts this long before the item ends
PREFETCH_HEAD_S = 2.0      # successor's read-ahead beyond any crossfade
SKIP_HEAD_S = 1.0          # a skip splices once this much is in hand
SKIP_READY_MAX_S = 10.0    # ...or gives up and splices at the write head
SKIP_FADE_S = 0.05         # declick at a skip splice
SKIP_GUARD_S = 0.25        # beyond the reader margin: the splice's own block
_READ_S = 0.1              # pipe read size


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

    def latest_sample(self) -> int:
        return 1 << 62             # closed-form: no write head to overtake

    def buffered_s(self) -> float:
        return float("inf")

    async def close(self) -> None:
        pass

    def skip(self) -> bool:
        return False               # one endless item — there is nothing to skip

    def item_position_s(self) -> Optional[float]:
        return None                # no item boundaries to measure from

    def item_origin_s(self) -> Optional[float]:
        return None                # ...so nothing starts anywhere either

    def stats(self) -> dict:
        return {"kind": self.kind, "title": "Sync test signal", "delay_s": 0.0}


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

    def truncate(self, at: int) -> None:
        """Drop ``[at, end)``. Only for samples no reader has reached."""
        self.end = max(self.start, min(self.end, at))

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


class _Decoder:
    """One ffmpeg process for one URL, read ahead into memory up to
    ``high_water`` bytes. Frame-aligned chunks; ``read`` returns b"" at EOF."""

    def __init__(self, cmd: list, url: str, frame: int, high_water: int,
                 rate: int = RATE, on_error=None):
        self.url = url
        self.high_water = high_water
        self.rc: Optional[int] = None
        self.total = 0                     # bytes decoded, ever
        self.eof = False
        self._cmd = cmd
        self._frame = frame
        self._want = frame * int(rate * _READ_S)
        self._on_error = on_error
        self._chunks: deque = deque()
        self._have = 0
        self._wake = asyncio.Event()       # data arrived, or EOF
        self._room = asyncio.Event()       # consumer drained below high water
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._tasks: list = []

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        self._tasks = [asyncio.create_task(self._pump()),
                       asyncio.create_task(self._drain_stderr())]

    @property
    def failed(self) -> bool:
        return self.eof and self.total == 0

    @property
    def buffered(self) -> int:
        return self._have

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _pump(self) -> None:
        proc, carry = self._proc, b""
        try:
            while True:
                while self._have >= self.high_water:
                    self._room.clear()
                    await self._room.wait()
                data = await proc.stdout.read(self._want)
                if not data:
                    break
                data, carry = carry + data, b""
                cut = len(data) - len(data) % self._frame
                data, carry = data[:cut], data[cut:]
                if data:
                    self._chunks.append(data)
                    self._have += len(data)
                    self.total += len(data)
                    self._wake.set()
            self.rc = await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._on_error:
                self._on_error(self.url, str(e))
        finally:
            self.eof = True
            self._wake.set()

    async def _drain_stderr(self) -> None:
        """A full stderr pipe stalls the decoder on a long-running stream."""
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                if self._on_error:
                    self._on_error(self.url,
                                   line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def read(self) -> bytes:
        while not self._chunks:
            if self.eof:
                return b""
            self._wake.clear()
            await self._wake.wait()
        return self.take()

    def take(self) -> bytes:
        """The next buffered chunk without waiting; b"" when none is."""
        if not self._chunks:
            return b""
        data = self._chunks.popleft()
        self._have -= len(data)
        self._room.set()
        return data

    async def wait_buffered(self, n: int, timeout: float) -> bool:
        """Until ``n`` bytes are in hand or the item ended; False on timeout."""
        deadline = time.monotonic() + timeout
        while self._have < n and not self.eof:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), left)
            except asyncio.TimeoutError:
                return False
        return True

    def kill_nowait(self) -> None:
        for t in self._tasks:
            t.cancel()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except (ProcessLookupError, Exception):
                pass

    async def close(self) -> None:
        """Kill and reap, bounded: teardown must not block on a wedged ffmpeg.
        Drained to EOF, not just waited on: an unread pipe stays paused and
        holds its transport and descriptors open after the process is gone."""
        self.kill_nowait()
        proc = self._proc
        if proc is None:
            return
        try:
            await asyncio.wait_for(proc.communicate(), timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            pass


class MediaSource:
    """ffmpeg-decoded audio on the shared timeline, optionally equalised."""

    kind = "media"

    def __init__(self, url: str, epoch: float, delay_s: float = 2.0,
                 capacity_s: float = 20.0, eq_chain=None,
                 rate: int = RATE, channels: int = CHANNELS,
                 ffmpeg: str = "", loop_forever: bool = False,
                 title: str = "", url_provider=None,
                 crossfade_s: float = 0.0, reader_pos=None,
                 next_provider=None, advance=None, item_duration=None,
                 before_splice=None):
        self.url = url
        self.title = title
        self._url_provider = url_provider
        # Decoding ahead (§4.1c): ``async () -> {"url", "title", ...} | None``
        # peeks the successor without moving the cursor, ``advance(rec)``
        # moves it there (False if a skip moved it first), and
        # ``item_duration() -> s | None`` says when to start.
        self._next_provider = next_provider
        self._advance = advance
        self._item_duration = item_duration
        # Called as a skip cuts in, before the furthest reader is read: lets
        # the owner pull readers back first (open-zone.md §4.1b).
        self._before_splice = before_splice
        self._dec: Optional[_Decoder] = None        # the item being written
        self._next: Optional[_Decoder] = None       # its successor, ahead
        self._next_rec: Optional[dict] = None
        self._next_task: Optional[asyncio.Task] = None
        self._prefetched_at: Optional[int] = None   # item origin it was for
        self._skip_dec: Optional[_Decoder] = None   # a skip's target, ready
        self._retired: list = []                    # reaps still running
        self._skip_title = ""
        self._skip_task: Optional[asyncio.Task] = None
        self._item_decoded = 0       # samples of the current item decoded
        self._gapless = 0
        self._splices = 0
        self._splice_last = ""
        self._last_rc: Optional[int] = None
        self._finished = False
        # Skip in flight: the exit that follows is a seam, not a fault.
        self._skipping = False
        # Timeline sample the current item starts at (open-zone.md §4.1b).
        self._item_origin: Optional[int] = None
        self.epoch = epoch
        self.delay_s = float(delay_s)
        self.channels = channels
        self._rate = rate
        self._eq = eq_chain
        self._reader_pos = reader_pos
        self._loop = bool(loop_forever)
        self._ffmpeg = ffmpeg or shutil.which("ffmpeg") or ""
        self._ring = _Ring(int(capacity_s * rate), channels, origin=0)
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

    def latest_sample(self) -> int:
        """Newest timeline sample written (open-zone.md §A.2). A reader at or
        past this overtakes the write head, which is committed as silence for
        the whole zone — so it is the ceiling for any move."""
        return self._ring.end

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
        for t in (self._next_task, self._skip_task):
            if t is not None:
                t.cancel()
        self._next_task = self._skip_task = None
        decs = [self._dec, self._next, self._skip_dec]
        self._dec = self._next = self._skip_dec = None
        for d in decs:
            if d is not None:
                await d.close()
        reaps, self._retired = self._retired, []
        if reaps:
            await asyncio.wait(reaps, timeout=5.0)

    def _cmd(self, url: str) -> list:
        cmd = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
        if self._loop:
            cmd += ["-stream_loop", "-1"]
        if url.startswith(("http://", "https://")):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5"]
        return cmd + ["-i", url, "-vn", "-ac", str(self.channels),
                      "-ar", str(self._rate), "-f", "s16le", "-"]

    def _frame(self) -> int:
        return self.channels * 2

    def _decoder_error(self, url: str, msg: str) -> None:
        self._last_error = msg
        logger.warning(f"Sync decoder [{url[:40]}]: {msg}")

    def _retire(self, dec: _Decoder) -> None:
        """Kill a decoder and reap it in the background, off the decode path;
        close() waits for any reap still running."""
        dec.kill_nowait()
        self._retired = [t for t in self._retired if not t.done()]
        self._retired.append(asyncio.ensure_future(dec.close()))

    async def _spawn(self, url: str, ahead_s: float) -> _Decoder:
        dec = _Decoder(self._cmd(url), url, self._frame(),
                       self._frame() * int(ahead_s * self._rate),
                       self._rate, self._decoder_error)
        await dec.start()
        return dec

    async def _supervise(self) -> None:
        backoff = 0.5
        while self._running:
            try:
                self._item_decoded = 0
                self._last_rc = await self._decode_once()
                if not self._running:
                    return
                # Charged like a clean item end: no restart count, no backoff.
                if self._skipping:
                    self._xfade_arm = self._xfade_s > 0
                    backoff = 0.5
                    continue
                if self._finished:      # the provider said there is no more
                    logger.info(f"Sync source finished: {self.title or self.url[:60]}")
                    return
                if (self._last_rc == 0 and self._url_provider is not None
                        and self._item_decoded > 0):
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
        skipped, self._skipping = self._skipping, False
        if self._url_provider is None:
            return
        try:
            # A skip resolves the position the caller set, whatever the exit.
            fresh = await self._url_provider(None if skipped else self._last_rc)
        except Exception as e:
            logger.warning(f"Sync source URL refresh failed: {e}")
            return
        title = ""
        if isinstance(fresh, dict):     # queue-aware provider: url + metadata
            title, fresh = (fresh.get("title") or ""), (fresh.get("url") or "")
        if not fresh:
            # Only a clean play-out means the material ran out, never a skip.
            if self._last_rc == 0 and not skipped:
                self._finished = True
            return
        if fresh != self.url:
            reason = ("skipped to" if skipped else
                      "next item" if self._last_rc == 0 else
                      "previous one expired or rotated")
            logger.info(f"Sync source URL re-resolved ({reason})")
        self.url = fresh
        if title:
            self.title = title

    async def _decode_once(self) -> Optional[int]:
        if self._skip_task is not None:
            # The item ran out under a skip still decoding: the skip wins.
            try:
                await self._skip_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._skip_dec is not None and not self._skipping:
            dec = self._splice_skip(None)
        else:
            dec = await self._adopt_next()
            if dec is None:
                await self._refresh_url()
                if self._finished:
                    return self._last_rc  # nothing more to play
                if not self.url:
                    raise RuntimeError("no playable URL for this source")
                # The arm survives the spawn; _xfade_open runs on the first block.
                dec = await self._spawn(self.url, DECODE_HW_S)
            # A restart re-decodes from the top, so a fault re-origins like a seam.
            self._item_origin = self._ring.end
            self._dec = dec
        try:
            while self._running:
                if self._skip_dec is not None:
                    dec = self._splice_skip(dec)
                    continue
                await self._throttle()
                if self._skip_dec is not None:
                    continue
                data = await dec.read()
                if not data:
                    break
                self._commit(data)
                self._maybe_prefetch()
            return dec.rc
        finally:
            if self._xfade_want:
                self._xfade_commit()
            self._retire(dec)
            if self._dec is dec:
                self._dec = None

    def _pcm(self, data: bytes) -> np.ndarray:
        """s16 bytes to float32 frames, through the EQ chain. One chain, so
        every item passes it in timeline order, never two at once."""
        if self._eq is not None:
            try:
                data = self._eq.process(data)
            except Exception as e:
                logger.warning(f"Sync EQ chain failed, bypassing: {e}")
                self._eq = None
        return (np.frombuffer(data, dtype="<i2")
                .reshape(-1, self.channels).astype(np.float32) * _S16_SCALE)

    def _commit(self, data: bytes) -> None:
        pcm = self._pcm(data)
        self._item_decoded += len(pcm)
        if not self._xfade_want:
            self._gap_close()
        if self._xfade_arm:
            self._xfade_open()
        if self._xfade_want:
            self._xfade_collect(pcm)
        else:
            self._ring.write(pcm)

    def _drop_next(self) -> None:
        if self._next_task is not None:
            self._next_task.cancel()
            self._next_task = None
        if self._next is not None:
            self._retire(self._next)
        self._next, self._next_rec = None, None

    def _maybe_prefetch(self) -> None:
        """Start the successor's decoder once the current item is within
        PREFETCH_LEAD_S of its end, so the seam spends no time resolving."""
        if (self._next_provider is None or self._item_duration is None
                or self._loop or self._next is not None
                or self._next_task is not None or self._skip_task is not None
                or self._skip_dec is not None
                or self._prefetched_at == self._item_origin):
            return
        try:
            dur = self._item_duration()
        except Exception:
            dur = None
        if not dur or dur - self._item_decoded / self._rate > PREFETCH_LEAD_S:
            return
        self._prefetched_at = self._item_origin
        self._next_task = asyncio.create_task(self._prefetch())

    async def _prefetch(self) -> None:
        try:
            rec = await self._next_provider()
            url = (rec or {}).get("url") or ""
            if not url or not self._running:
                return
            dec = await self._spawn(url, self._xfade_s + PREFETCH_HEAD_S)
            if not self._running:
                self._retire(dec)
                return
            self._next, self._next_rec = dec, rec
            logger.info(f"Sync source decoding ahead: "
                        f"{rec.get('title') or url[:60]}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Sync source could not decode ahead ({e}) — "
                           "resolving at the seam instead")
        finally:
            if self._next_task is asyncio.current_task():
                self._next_task = None

    async def _adopt_next(self) -> Optional[_Decoder]:
        """The decoded-ahead successor at a clean seam, or None to resolve as
        before. Only a play-out advances: a fault retries the same item, and a
        skip has already moved the cursor somewhere else."""
        if self._last_rc != 0 or self._skipping:
            return None
        if self._next_task is not None:
            try:
                await self._next_task      # ending before it landed: still ahead
            except (asyncio.CancelledError, Exception):
                pass
        dec, rec = self._next, self._next_rec
        self._next, self._next_rec = None, None
        if dec is None:
            return None
        if dec.failed or not self._advance or not self._advance(rec):
            self._retire(dec)
            return None
        dec.high_water = self._frame() * int(DECODE_HW_S * self._rate)
        self.url = rec["url"]
        self.title = rec.get("title") or self.title
        self._gapless += 1
        logger.info(f"Sync source next item, decoded ahead: "
                    f"{self.title or self.url[:60]}")
        return dec

    async def _prepare_skip(self) -> None:
        """Decode the skip's target alongside the item it replaces, then hand
        it to the decode loop to splice (_splice_skip). Failing that, the old
        path: end the item and resolve at the write head."""
        dec = None
        try:
            fresh = await self._url_provider(None)
            title = ""
            if isinstance(fresh, dict):
                title, fresh = fresh.get("title") or "", fresh.get("url") or ""
            if not fresh:
                raise RuntimeError("no playable URL")
            dec = await self._spawn(fresh, SKIP_HEAD_S + PREFETCH_HEAD_S)
            ok = await dec.wait_buffered(
                self._frame() * int(SKIP_HEAD_S * self._rate), SKIP_READY_MAX_S)
            if dec.failed or not ok:
                raise RuntimeError("nothing decoded in time")
            if not self._running:
                self._retire(dec)
                return
            self.url = fresh
            self._skip_title = title
            self._skip_dec, dec = dec, None
        except asyncio.CancelledError:
            if dec is not None:
                self._retire(dec)
            raise
        except Exception as e:
            if dec is not None:
                self._retire(dec)
            logger.warning(f"Sync skip could not decode ahead ({e}) — "
                           "splicing at the write head")
            self._skipping = True
            if self._dec is not None:
                self._retire(self._dec)
        finally:
            if self._skip_task is asyncio.current_task():
                self._skip_task = None

    def _splice_skip(self, old: Optional[_Decoder]) -> _Decoder:
        """Cut to the skip's target just ahead of the furthest reader rather
        than at the write head (open-zone.md §4.1b). The target's head is
        already in hand, so the write head lands well clear of every reader
        and the decoder runs on ahead of real time from there."""
        new, self._skip_dec = self._skip_dec, None
        if old is not None:
            self._retire(old)
        self._xfade_head, self._xfade_want, self._xfade_have = None, 0, 0
        self._xfade_arm = False
        if self._skip_title:
            self.title = self._skip_title
        if self._before_splice is not None:
            try:
                self._before_splice()
            except Exception as e:
                logger.debug(f"Sync splice hook failed: {e}")
        end = self._ring.end
        at = end
        head = self._reader_head()
        if head is not None:
            margin = (XFADE_READER_MARGIN_S + SKIP_GUARD_S) * self._rate
            at = int(min(end, max(self._ring.start, head + margin)))
        blocks = []
        while new.buffered:
            blocks.append(self._pcm(new.take()))
        pcm = np.concatenate(blocks) if blocks else np.zeros(
            (0, self.channels), dtype=np.float32)
        n = min(int(SKIP_FADE_S * self._rate), end - at, len(pcm))
        if n > 1:
            tail = self._ring.read(at, n, count=False)
            pcm = pcm.copy()
            pcm[:n] = mix_overlap(tail, pcm[:n], 1.0, self._rate)
        self._ring.truncate(at)
        self._ring.write(pcm)
        self._item_origin = at
        self._item_decoded = len(pcm)
        self._prefetched_at = None
        self._splices += 1
        self._splice_last = f"{(end - at) / self._rate:.2f}s sooner"
        logger.info(f"Sync skip spliced {(end - at) / self._rate:.2f}s ahead of "
                    f"the write head: {self.title or self.url[:60]}")
        self._dec = new
        new.high_water = self._frame() * int(DECODE_HW_S * self._rate)
        return new

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
        """Commit an overrun before decoded audio lands on top of it (§4.1).

        Bounded by the play point, and that bound is what keeps one reader's
        fault from becoming the zone's. A reader past ``play_now`` is asking
        for audio that has not happened yet, so no amount of silence settles
        it — and committing that silence is harmful twice over. It goes into
        the timeline *every other device* reads, and it pushes the write head
        past ``_throttle``'s ceiling, which stalls the decoder. The two
        together latch: the head is dragged forward by the reader, the reader
        keeps pace with the head, and the source settles at a few percent real
        audio until something happens to reseat the reader. Live session
        2026-08-05 held 93% silence for an unbroken hour this way.

        A reader that has genuinely run off the end is better served by
        ``_Ring.read``, which zero-fills for that reader alone and counts the
        underrun against it — a local fault, reported as one.

        The bound never binds on a healthy reader: those are seated at
        ``play_now - delay_s`` and paced to stay at most ``STREAM_AHEAD_S``
        ahead of that, so they sit below the play point by construction. A
        source that has genuinely fallen behind real time still gaps normally,
        because its readers are behind the play point too.
        """
        head = self._reader_head()
        if head is None:
            return
        head = min(head, (time.monotonic() - self.epoch) * self._rate)
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

    async def _throttle(self) -> None:
        """Hold the write head near ``delay_s`` ahead of the play point. A
        skip ready to splice lets go: it is about to pull the head back."""
        while self._running and self._skip_dec is None:
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

    def skip(self) -> bool:
        """Move to the queue's current position, which the caller has just
        set. The target is decoded alongside the outgoing item and spliced in
        ahead of the furthest reader (open-zone.md §4.1b)."""
        if self._finished or not self._running:
            return False
        self._drop_next()                   # it followed the old position
        if self._skip_task is not None:
            self._skip_task.cancel()
        if self._skip_dec is not None:
            self._retire(self._skip_dec)
            self._skip_dec = None
        if self._url_provider is None or self._dec is None:
            # Between items, or nothing to resolve against: end the item.
            self._skip_task = None
            self._skipping = True
            if self._dec is not None:
                self._retire(self._dec)
            return True
        self._skip_task = asyncio.create_task(self._prepare_skip())
        return True

    def item_position_s(self) -> Optional[float]:
        """How far into the current item the group is *hearing*, in timeline
        samples rather than wall clock. None before the first item starts."""
        if self._item_origin is None:
            return None
        played = (time.monotonic() - self.epoch - self.delay_s) * self._rate
        return max(0.0, (played - self._item_origin) / self._rate)

    def item_origin_s(self) -> Optional[float]:
        """Where the current item starts, in timeline seconds. None before the
        first one does.

        The timeline is the one clock every device shares, so a boundary
        expressed in it is a boundary every device can act on at the same
        moment — which is what lets a receiver flip its display when the seam
        is *heard* rather than when the decoder reached it, seconds earlier."""
        if self._item_origin is None:
            return None
        return self._item_origin / self._rate

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
            "decoding": bool(self._dec and self._dec.alive),
            "decoding_ahead": bool(self._next and self._next.alive),
            "gapless": self._gapless,
            "skip_splices": self._splices,
            **({"skip_last": self._splice_last} if self._splice_last else {}),
            "finished": self._finished,
            "eq": self._eq is not None,
            "crossfade_s": self._xfade_s,
            "crossfades": self._xfades,
            "rework_s": round(self._unserved_samples() / self._rate, 2),
            "gap_ms": round(self._gap_samples * 1000 / self._rate, 1),
            **({"crossfade_last": self._xfade_last} if self._xfade_last else {}),
            **({"last_error": self._last_error[:200]} if self._last_error else {}),
        }
