"""
CastSyncPoc — proof-of-concept synchronised multi-speaker casting.

Goal: measure whether we can play the SAME audio on several Cast devices in
sync (echo-free) WITHOUT a Google-Home group, using our own custom Web
Receiver (static/cast/sync_receiver.html) that schedules timestamped PCM
chunks with the Web Audio API against a shared server clock.

How it works
  * A tiny plain-HTTP listener (uvicorn, config ``media.cast.sync.http_port``)
    serves the receiver page and a same-origin WebSocket. Plain HTTP matters:
    the main app's self-signed HTTPS is rejected by the Cast device's browser,
    and an https page may not open a ws:// socket (mixed content). Register
    ``http://<host>:<port>/cast/sync_receiver.html`` as a *development* custom
    receiver in the Cast console and put its App ID in config.
  * The server clock is ``time.monotonic()``. Receivers estimate their offset
    to it NTP-style over the WebSocket (ping/pong, min-RTT filtering).
  * Audio is a generated test signal (soft chord pad + a sharp click every
    2 s — clicks make even ~10 ms misalignment audible as flam/echo).
    44.1 kHz stereo s16le, CHUNK_SECONDS per chunk, each chunk prefixed with
    the server-clock time it must start playing (8-byte big-endian double).
  * Chunks are produced AHEAD_SECONDS before their play time and fanned out to
    every connected receiver, which schedules them sample-accurately and
    corrects clock drift continuously. Per-device manual trim (±ms) covers the
    device's fixed output-pipeline latency.

Fallback stream mode (no Cast console account)
  * When ``media.cast.sync.app_id`` is EMPTY, sessions run in "stream mode":
    each device gets the built-in default media receiver (CC1AD845 — no
    registration, no $5 console fee) pointed at a per-device live WAV stream
    (``/sync/stream/<sid>.wav``) cut from the same shared timeline. Because
    the test signal is a pure function of timeline position, the server can
    seek any device's stream instantly: a monitor polls each device's
    reported media time and computes its lag against the server clock.
    Hard stream jumps are reserved for acquisition/rebuffer (>±100 ms);
    every smaller correction — clock drift, residual offset, trim changes —
    goes through a per-device fractional-position resampler as a bounded
    rate slew (fast 1000 ppm ≈ 1.7 cents, steady-state 20 ppm), so
    corrections are inaudible and the buffer never steps (OpenZone §5.2).
    Needs nothing from Google; manual trim does the final alignment by ear.

This is deliberately PoC-scoped: one global session, generated audio only,
stats surfaced via /api/media/sync/status and the sync_test.html page.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
import tempfile
import time
import uuid as uuid_mod
from typing import Dict, List, Optional

import numpy as np
# Module-level on purpose: with ``from __future__ import annotations`` the
# ``ws: WebSocket`` annotation is a string FastAPI resolves against module
# globals — imported inside _build_http_app it silently 403s every handshake.
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse

# Per-group DuckDB stores (data/sync/<group>.duckdb) are the model's store
# of record — separate files so the zmm_telemetry appender's lock on
# telemetry.duckdb is never contended. The JSON model file is only the
# fallback when duckdb is unavailable.
try:
    from modules.media import sync_db as _sdb
except Exception:                                    # pragma: no cover
    _sdb = None

from modules.media import sync_chirp as _chirp

logger = logging.getLogger("modules.media.cast_sync")

RATE = 44100
CHANNELS = 2
CHUNK_SECONDS = 0.5
CHUNK_FRAMES = int(RATE * CHUNK_SECONDS)
LEAD_SECONDS = 2.0       # first chunk plays this long after session start
AHEAD_SECONDS = 1.5      # send each chunk this early (receiver scheduling slack)
BUFFER_CHUNKS = 6        # kept for late joiners
SYNC_NAMESPACE = "urn:x-cast:zmm.sync"

DEFAULT_APP_ID = "CC1AD845"      # built-in default media receiver (no registration)
STREAM_BLOCK_S = 0.2             # stream-mode PCM block size
STREAM_AHEAD_S = 1.2             # serve at most this far ahead of the timeline
STREAM_LAG_MARGIN_S = 0.35       # common target lag = max natural lag + this
STREAM_POLL_S = 2.5              # monitor poll interval once baselines exist
# Sensor improvements ahead of the acoustic calibrator: poll fast while any
# device's drift-fit baseline is still building (more points → the slope
# fit beats the noise sooner; a jump clears the baseline, so reacquisition
# speeds itself up), and take several media-time reads per poll — the
# median knocks per-measurement noise down ~√N and shrugs off one bogus
# status. Neither changes what is measured, only how well.
STREAM_POLL_FAST_S = 1.0         # cadence while acquiring
STREAM_STATUS_READS = 3          # media-time reads per poll (median)
STREAM_STATUS_READS_ACQ = 5      # ...while acquiring: denser + more robust.
#                                  Beyond ~5 the returns die: consecutive
#                                  reads share the device's underlying status
#                                  report (quantised + extrapolated), so more
#                                  reads re-sample the same value — the DB is
#                                  not the constraint, information is.
STREAM_STATUS_WAIT_S = 0.2       # wait for each status push to land
STREAM_COOLDOWN_S = 7.0          # ignore polls this long after a jump
#                                  (device drains its HTTP buffer; wall-time
#                                  so the fast cadence doesn't shorten it)
STREAM_CONNECT_GRACE_S = 4.0     # ignore polls this long after a stream
#                                  (re)connect — the device is still filling
#                                  its buffer and its early media time is
#                                  junk (observed: first poll off by 550 ms,
#                                  causing a jump ping-pong at every start)
# Correction policy (OpenZone §5.2): jumps only for acquisition/rebuffer;
# every correction below the jump threshold is applied as a bounded rate
# slew through the fractional resampler — inaudible, no buffer steps.
STREAM_JUMP_MIN_S = 0.10         # hard resync only beyond this
STREAM_SLEW_FAST_PPM = 1000.0    # |offset| > fast threshold (≈1.7 cents, inaudible)
STREAM_SLEW_GENTLE_PPM = 20.0    # steady-state slew cap
# ~2σ of the media-time poll noise (measured ±10–25 ms): below this a single
# poll can't be told apart from noise, so fast slews also require the median
# of the last 3 polls to agree (a lone spike must not move real audio).
STREAM_SLEW_FAST_THRESH_S = 0.030
# Drift-cancel bounds: consumer DAC crystals sit within ±50 ppm, so any
# estimate outside that is estimator noise, not clock error.
STREAM_RATE_MAX_PPM = 50.0
# Drift-fit statistics: resolving ppm-grade slopes through ±12 ms poll noise
# needs baseline, not cleverness — slope noise falls as span^1.5, so the
# window GROWS (capped ~20 min to re-track thermal drift) and the fit is
# applied only once the span can beat the noise (simulated: estimate lands
# within ±2–4 ppm of true drift by ~6 min; a 100 s cap railed the clamp).
STREAM_FIT_MIN_POINTS = 8        # polls needed before the drift fit runs
STREAM_FIT_MAX_POINTS = 360      # ≈20 min of polls
STREAM_FIT_APPLY_SPAN_S = 60.0   # shorter baselines are all noise — hold off
STREAM_RATE_EMA = 0.25           # max blend of a new fit into rate_ppm
STREAM_RATE_EMA_SPAN_S = 240.0   # EMA scales up linearly to full at this span


def _gen_float(n0: int, frames: int) -> np.ndarray:
    """Test-signal samples (float mono) for ``frames`` samples starting at
    absolute timeline sample ``n0`` — pure function of the sample position,
    so any receiver or stream joining/seeking later gets a bit-identical
    timeline. Chord pad with a slow swell + a 1 kHz click every 2 s.
    This is the integer-grid source the resampler interpolates over; step 2
    replaces it with the real-media ring buffer — at which point the linear
    interpolator must be upgraded too (soxr per OpenZone §5.2): linear is
    clean on this ≤1 kHz signal but rolls off HF audibly on real music."""
    t = (n0 + np.arange(frames)) / RATE
    sig = (0.10 * np.sin(2 * np.pi * 220.0 * t)
           + 0.08 * np.sin(2 * np.pi * 277.18 * t)
           + 0.08 * np.sin(2 * np.pi * 329.63 * t))
    sig *= 0.7 + 0.3 * np.sin(2 * np.pi * 0.05 * t)
    ph = np.mod(t, 2.0)
    m = ph < 0.008
    if m.any():   # sharp exponentially-decaying tick — the sync "ruler"
        sig[m] += 0.85 * np.sin(2 * np.pi * 1000.0 * ph[m]) * np.exp(-ph[m] / 0.002)
    return sig


def _encode_s16(sig: np.ndarray) -> bytes:
    s16 = (np.clip(sig, -0.98, 0.98) * 32767).astype("<i2")
    return np.repeat(s16[:, None], CHANNELS, axis=1).tobytes()


def _gen_samples(n0: int, frames: int) -> bytes:
    """Unity-ratio PCM straight off the integer grid (WS/chunk mode)."""
    return _encode_s16(_gen_float(n0, frames))


def _resample_block(pos: float, frames: int, adv: float,
                    extra=None) -> bytes:
    """Fractional-position resampler (the OpenZone §5.2 actuator): emit
    ``frames`` output samples reading the timeline from float sample
    position ``pos``, consuming ``adv`` timeline samples in total, i.e. a
    ratio of adv/frames modulated a few hundred ppm around unity. Sub-sample
    linear interpolation between adjacent integer-grid samples — at unity
    ratio from an integer position it degenerates to a bit-exact copy; at
    fractional positions the worst-case error on this test signal is about
    −84 dBFS (measured) — a shade above the −90 dBFS s16 LSB, far below
    audibility. Real media needs soxr instead (see _gen_float note)."""
    idx = pos + np.arange(frames) * (adv / frames)
    base = np.floor(idx).astype(np.int64)
    frac = idx - base
    i0 = int(base[0])
    n = int(base[-1]) - i0 + 2
    src = _gen_float(i0, n)
    if extra is not None:
        # Per-device timeline-domain injection (calibration chirp): mixed
        # into the integer grid before interpolation, so it rides through
        # the resampler exactly like programme material.
        c0, wave = extra
        a, b = max(i0, c0), min(i0 + n, c0 + len(wave))
        if a < b:
            src[a - i0:b - i0] += wave[a - c0:b - c0]
    rel = base - i0
    return _encode_s16(src[rel] * (1.0 - frac) + src[rel + 1] * frac)


def _gen_chunk(index: int) -> bytes:
    """WS-mode framing: fixed CHUNK_FRAMES chunk ``index`` of the timeline."""
    return _gen_samples(index * CHUNK_FRAMES, CHUNK_FRAMES)


def _wav_header() -> bytes:
    """WAV header for an endless live stream (RIFF/data sizes maxed out —
    the default receiver treats it as unbounded)."""
    byte_rate = RATE * CHANNELS * 2
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, CHANNELS, RATE,
                                    byte_rate, CHANNELS * 2, 16)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


class _SyncMessageController:
    """pychromecast controller for our custom namespace — created lazily so
    pychromecast stays an optional import (mirrors MediaService's approach)."""

    def __new__(cls):
        from pychromecast.controllers import BaseController

        class _Ctrl(BaseController):
            def __init__(self):
                super().__init__(SYNC_NAMESPACE)

            def receive_message(self, _message, _data):  # receiver → sender (unused)
                return True

            def push(self, payload: dict):
                self.send_message(payload)

        return _Ctrl()


class _Receiver:
    """One connected receiver WebSocket (== one Cast device running the app)."""

    def __init__(self, sid: str, ws):
        self.sid = sid
        self.ws = ws
        self.player_id: str = ""     # filled from the session's launch table
        self.name: str = ""
        self.stats: dict = {}
        self.connected_at = time.monotonic()


class _Stream:
    """Stream-mode per-device state (the default-receiver counterpart of
    _Receiver). ``pos`` is the (fractional) next timeline sample its WAV
    stream will serve; every deliberate timeline move (jump, slew, trim)
    must also be reflected in ``shift`` so the monitor's position estimate
    stays consistent."""

    def __init__(self, sid: str, player_id: str, name: str):
        self.sid = sid
        self.player_id = player_id
        self.name = name
        self.connected = False       # WAV stream currently being consumed
        self.pos: Optional[float] = None  # next timeline sample (fractional)
        self.start_pos: int = 0      # timeline sample of the first PCM byte
        self.shift: float = 0.0      # cumulative deliberate moves (samples)
        self.natural_lag: Optional[float] = None   # first stable lag (s)
        self.cooldown_until: float = 0.0   # skip polls until then after a jump
        self.resyncs: int = 0
        self.stats: dict = {}
        # Software PLL: the resampler serves fractionally more/fewer timeline
        # samples per output second to cancel the device's clock drift.
        self.rate_ppm: float = 0.0   # >0 = device clock slow, serve faster
        self.slew_s: float = 0.0     # pending offset to slew away (s, >0 =
        #                              device behind → consume timeline faster)
        # Cumulative deliberate timeline motion (s): rate term + drained slew
        # + jumps. Added back onto measured lag before drift fitting, so the
        # fit sees the device's FREE-RUNNING clock, never our own corrections
        # (feeding corrections into the fit is what railed the old PLL).
        self.moved_s: float = 0.0
        self.err_hist: List[float] = []   # last 3 poll errors (median filter)
        # Until first lock nobody is listening to this device as part of a
        # group yet, so sub-100 ms offsets are stepped away instantly rather
        # than ground down by a 30 s slew (observed: 32 ms residuals taking
        # half a minute to clear at every start). Sticky once locked.
        self.acquired: bool = False
        # Calibration chirp scheduled on this stream: (timeline_sample, wave)
        self.chirp: Optional[tuple] = None
        self.lag_hist: List[tuple] = []   # (t, lag+moved_s) for drift fitting
        self.precomp_s: float = 0.0  # model-predicted lag pre-compensation


class CastSyncPoc:
    def __init__(self, cast_provider, cfg: dict):
        cfg = cfg or {}
        self.cast = cast_provider                      # CastPlayerProvider
        self.http_port = int(cfg.get("http_port", 8010))
        self.app_id = (cfg.get("app_id") or "").strip()
        self._trims_file = cfg.get("trims_file", "./data/cast_sync_trims.json")
        self._trims: Dict[str, int] = {
            k: int(v) for k, v in self._read_json(self._trims_file).items()}
        # Named sync groups (built in the Media tab's group builder):
        # gid -> {"name": str, "members": [player_id, ...]}
        self._groups_file = cfg.get("groups_file", "./data/cast_sync_groups.json")
        self._groups: Dict[str, dict] = self._read_json(self._groups_file)
        self._active_group: str = ""               # gid of the running session
        # Learned per-device latency model (stream mode): startup lag +
        # clock-drift rate, EMA-updated every session so later sessions
        # start pre-aligned. player_id -> {lag_s, drift_ppm, sessions}
        self._model_file = cfg.get("model_file", "./data/cast_sync_model.json")
        self._model: Dict[str, dict] = self._read_json(self._model_file)
        self._session_id: str = ""
        # Acoustic calibration (mic + chirps): optional input device name /
        # index for sounddevice; None = system default.
        self._mic_device = cfg.get("mic_device") or None
        self._calibrating = False

        self._http_server = None                       # uvicorn.Server
        self._http_task: Optional[asyncio.Task] = None
        self._producer: Optional[asyncio.Task] = None
        self._auto_stop: Optional[asyncio.Task] = None
        self._duration_s: int = 0                      # 0 = run until stopped
        self._launch_tasks: List[asyncio.Task] = []

        self.running = False
        self._epoch: float = 0.0
        self._buffer: List[bytes] = []                 # last N framed chunks
        self._receivers: Dict[str, _Receiver] = {}     # sid -> _Receiver
        self._pending: Dict[str, dict] = {}            # sid -> {player_id, name}
        self._controllers: Dict[str, object] = {}      # cast uuid -> controller
        # Stream (default-receiver) mode:
        self._streams: Dict[str, _Stream] = {}         # sid -> _Stream
        self._monitor: Optional[asyncio.Task] = None
        self._target_lag: Optional[float] = None       # common lag target (s)

    # ------------------------------------------------------------------
    # Lifecycle (called from MediaService)
    # ------------------------------------------------------------------
    def start(self):
        """Bring up the plain-HTTP receiver/WS listener (idempotent)."""
        if self._http_task is not None:
            return
        import uvicorn
        config = uvicorn.Config(
            self._build_http_app(), host="0.0.0.0", port=self.http_port,
            log_level="warning", lifespan="off",
        )
        self._http_server = uvicorn.Server(config)
        self._http_task = asyncio.create_task(self._http_server.serve())
        logger.info(f"Cast sync PoC HTTP listener on :{self.http_port} "
                    f"(receiver at /cast/sync_receiver.html)")

    def stop(self):
        if self.running:
            asyncio.ensure_future(self.stop_session())
        if self._http_server is not None:
            self._http_server.should_exit = True
        self._http_task = None
        if _sdb is not None:
            _sdb.close_all()

    # ------------------------------------------------------------------
    # Session control (called from routes)
    # ------------------------------------------------------------------
    async def start_session(self, player_ids: Optional[List[str]] = None,
                            group_id: str = "", duration_s: int = 0) -> dict:
        if group_id:
            group = self._groups.get(group_id)
            if not group:
                return {"success": False, "error": "Unknown sync group"}
            player_ids = group.get("members", [])
        if not player_ids:
            return {"success": False, "error": "No players to start"}
        if self.running:
            await self.stop_session()
        self._active_group = group_id
        stream_mode = not self.app_id   # no registered receiver -> default receiver
        self._session_id = uuid_mod.uuid4().hex[:8]
        # Fixed-length runs keep sessions comparable: the learned model and
        # the Sync Lab trends are evaluated over identical time windows.
        self._duration_s = max(0, int(duration_s or 0))

        # Prefer the DuckDB-trained model (robust medians over raw history,
        # across every group's DB); the in-memory/JSON EMAs remain the fallback.
        if stream_mode and _sdb is not None:
            try:
                db_model = await asyncio.to_thread(_sdb.query_device_model)
                for pid, m in db_model.items():
                    self._model[pid] = {**self._model.get(pid, {}), **m}
            except Exception as e:
                logger.debug(f"Sync model DB load failed (using JSON model): {e}")

        self._epoch = time.monotonic()
        self._buffer = []
        self._receivers = {}
        self._streams = {}
        self._pending = {}
        self._target_lag = None
        self.running = True
        if not stream_mode:
            self._producer = asyncio.create_task(self._produce())

        # If the model knows every member's startup lag, fix the session
        # target now and pre-compensate each stream so devices start already
        # roughly aligned (the monitor only mops up the residual).
        model_lags = {pid: self._model.get(pid, {}).get("lag_s")
                      for pid in player_ids}
        if stream_mode and all(v is not None for v in model_lags.values()):
            self._target_lag = max(model_lags.values()) + STREAM_LAG_MARGIN_S
            logger.info(f"Sync stream target lag from model: {self._target_lag:.2f}s")

        launched, errors = [], {}
        for pid in player_ids:
            sid = uuid_mod.uuid4().hex[:12]
            name = self._player_name(pid)
            self._pending[sid] = {"player_id": pid, "name": name}
            try:
                if stream_mode:
                    st = _Stream(sid, pid, name)
                    m = self._model.get(pid, {})
                    if self._target_lag is not None and m.get("lag_s") is not None:
                        st.precomp_s = max(0.0, self._target_lag - m["lag_s"])
                    # Clamp to the physical bound — models trained before the
                    # estimator fix may hold rail values (±300 ppm).
                    st.rate_ppm = max(-STREAM_RATE_MAX_PPM,
                                      min(STREAM_RATE_MAX_PPM,
                                          float(m.get("drift_ppm", 0.0))))
                    self._streams[sid] = st
                    task = asyncio.create_task(self._launch_stream(pid, sid))
                else:
                    task = asyncio.create_task(self._launch(pid, sid))
                self._launch_tasks.append(task)
                launched.append({"player_id": pid, "name": name, "sid": sid})
            except Exception as e:
                errors[pid] = str(e)
        if stream_mode:
            self._monitor = asyncio.create_task(self._stream_monitor())
        if self._duration_s:
            self._auto_stop = asyncio.create_task(
                self._auto_stop_after(self._duration_s))
        logger.info(f"Cast sync session started for {len(launched)} device(s) "
                    f"({'default-receiver stream' if stream_mode else 'custom receiver'} mode"
                    f"{f', {self._duration_s}s window' if self._duration_s else ''})")
        return {"success": True, "launched": launched, "errors": errors,
                "mode": "stream" if stream_mode else "receiver",
                "duration_s": self._duration_s}

    async def _auto_stop_after(self, secs: int):
        """End the session when its fixed test window elapses."""
        try:
            await asyncio.sleep(secs)
            if self.running:
                logger.info(f"Sync session test window over ({secs}s) — stopping")
                await self.stop_session()
        except asyncio.CancelledError:
            pass

    async def stop_session(self) -> dict:
        self.running = False
        for t in self._launch_tasks:
            t.cancel()
        self._launch_tasks = []
        if self._producer:
            self._producer.cancel()
            self._producer = None
        if self._monitor:
            self._monitor.cancel()
            self._monitor = None
        # May be called FROM the auto-stop task — cancelling ourselves here
        # would abort the rest of this cleanup.
        if self._auto_stop and self._auto_stop is not asyncio.current_task():
            self._auto_stop.cancel()
        self._auto_stop = None
        if self._streams:    # persist what this session taught the model
            self._write_json(self._model_file, self._model)
        self._streams = {}   # generators see running=False and finish
        for r in list(self._receivers.values()):
            try:
                await r.ws.close()
            except Exception:
                pass
        self._receivers = {}
        # Quit our app on every device we launched so they drop back to idle.
        for info in list(self._pending.values()):
            uuid_str = info["player_id"].split(":", 1)[1]
            cast = self.cast._casts.get(uuid_str)
            if cast is not None:
                try:
                    await asyncio.to_thread(cast.quit_app)
                except Exception as e:
                    logger.debug(f"quit_app failed for {info['player_id']}: {e}")
        self._pending = {}
        self._active_group = ""
        logger.info("Cast sync session stopped")
        return {"success": True}

    def status(self) -> dict:
        devices = []
        for sid, info in self._pending.items():
            r = self._receivers.get(sid)
            s = self._streams.get(sid)
            devices.append({
                "sid": sid,
                "player_id": info["player_id"],
                "name": info["name"],
                "connected": (r is not None) or (s is not None and s.connected),
                "trim_ms": self._trims.get(info["player_id"], 0),
                "stats": (r.stats if r else (s.stats if s else {})),
            })
        elapsed = (time.monotonic() - self._epoch) if self.running else 0
        return {
            "running": self.running,
            "configured": bool(self.app_id),
            "mode": "receiver" if self.app_id else "stream",
            "http_port": self.http_port,
            "group_id": self._active_group,
            "elapsed_s": elapsed,
            "duration_s": self._duration_s if self.running else 0,
            "remaining_s": (max(0, self._duration_s - elapsed)
                            if self.running and self._duration_s else None),
            "devices": devices,
        }

    async def set_trim(self, player_id: str, trim_ms: int) -> dict:
        trim_ms = max(-2000, min(2000, int(trim_ms)))
        old = self._trims.get(player_id, 0)
        self._trims[player_id] = trim_ms
        self._write_json(self._trims_file, self._trims)
        for r in self._receivers.values():
            if r.player_id == player_id:
                try:
                    await r.ws.send_json({"type": "trim", "trim_ms": trim_ms})
                except Exception as e:
                    logger.debug(f"trim push failed for {player_id}: {e}")
        # Stream mode: positive trim = play later = serve older timeline.
        # Trims apply as an IMMEDIATE buffer step, deliberately breaking the
        # §5.2 no-step rule: trim exists for by-ear alignment, and a slewed
        # trim takes |Δ|×1000 s to become audible — users chased ±400 ms in
        # circles because they couldn't hear their own adjustments land.
        # The step is decompensated via ``moved_s``, so the drift fit keeps
        # its baseline; the cooldown covers the device-buffer transient.
        # The chirp calibrator will set trims automatically; the slider then
        # remains as a manual override for taste.
        delta_s = (trim_ms - old) / 1000.0
        for s in self._streams.values():
            if s.player_id == player_id and s.pos is not None:
                s.pos -= delta_s * RATE
                s.shift -= delta_s * RATE
                s.moved_s -= delta_s
                s.cooldown_until = time.monotonic() + STREAM_COOLDOWN_S
                s.err_hist = []       # baseline moved — old medians invalid
                await self._record_samples([self._sample_row(s, "trim")])
        return {"success": True, "player_id": player_id, "trim_ms": trim_ms}

    # ------------------------------------------------------------------
    # Acoustic calibration (OpenZone §7 chirp mode): measure the audio in
    # the AIR, which the status sensor cannot see, and set trims from it.
    # ------------------------------------------------------------------
    async def calibrate(self) -> dict:
        """Chirp sequence → GCC-PHAT arrivals → trims. Runs during normal
        playback: each device plays a 100 ms 2–8 kHz chirp in its own time
        slot; one mic recording covers all slots, so every common-mode
        error (mic start latency, mic clock, shared path) cancels when the
        arrivals are differenced across devices."""
        if not self.running or self.app_id:
            return {"success": False,
                    "error": "Calibration needs a running stream-mode session"}
        if self._calibrating:
            return {"success": False, "error": "Calibration already running"}
        if self._target_lag is None:
            return {"success": False,
                    "error": "Devices still acquiring — try again in a few seconds"}
        streams = [s for s in self._streams.values()
                   if s.connected and s.pos is not None]
        if len(streams) < 2:
            return {"success": False,
                    "error": "Need at least two connected speakers"}
        try:
            import sounddevice  # noqa: F401 — fail early with a clear error
        except Exception as e:
            return {"success": False,
                    "error": f"Mic unavailable (sounddevice/PortAudio): {e}"}
        self._calibrating = True
        try:
            return await self._run_chirp_sequence(streams)
        finally:
            for s in streams:
                s.chirp = None
            self._calibrating = False

    async def _run_chirp_sequence(self, streams: List[_Stream]) -> dict:
        wave = _chirp.chirp_wave(RATE)
        # Slots must land beyond every serve head — audio already generated
        # (and buffered device-side) can't be changed. Because devices
        # buffer several seconds, the chirps sound target_lag later.
        head_s = max((s.pos - s.shift) / RATE for s in streams)
        plan = []          # (stream, expected arrival in elapsed-seconds)
        for i, s in enumerate(streams):
            slot_s = head_s + _chirp.CHIRP_LEAD_S + i * _chirp.CHIRP_GAP_S
            s.chirp = (int(slot_s * RATE), wave)
            trim_s = self._trims.get(s.player_id, 0) / 1000.0
            plan.append((s, slot_s + self._target_lag + trim_s))
        rec_start = time.monotonic() - self._epoch
        rec_dur = (max(t for _, t in plan) - rec_start
                   + _chirp.SEARCH_S + _chirp.CHIRP_S + 0.5)
        if not 0 < rec_dur <= 30:
            return {"success": False,
                    "error": f"Calibration window infeasible ({rec_dur:.0f}s)"}
        logger.info(f"Chirp calibration: {len(plan)} device(s), "
                    f"recording {rec_dur:.1f}s")
        try:
            mic = await asyncio.to_thread(
                _chirp.record, rec_dur, RATE, self._mic_device)
        except Exception as e:
            return {"success": False, "error": f"Mic capture failed: {e}"}

        devices, deltas = [], {}
        for s, t_exp in plan:
            a = max(0, int((t_exp - _chirp.SEARCH_S - rec_start) * RATE))
            b = min(len(mic),
                    int((t_exp + _chirp.SEARCH_S + _chirp.CHIRP_S
                         - rec_start) * RATE))
            idx, quality = _chirp.gcc_phat(mic[a:b].astype(np.float64), wave)
            info = {"player_id": s.player_id, "name": s.name,
                    "quality": round(quality, 1), "detected": False}
            if idx is not None and quality >= _chirp.MIN_PEAK_RATIO:
                t_arr = rec_start + (a + idx) / RATE
                deltas[s.sid] = t_arr - t_exp
                info["detected"] = True
            devices.append(info)
        if len(deltas) < 2:
            return {"success": False, "devices": devices,
                    "error": "Chirps not detected on enough speakers — "
                             "check the mic and its input level"}
        # Differencing: only relative arrival matters; the mean keeps the
        # group's overall timing where it is.
        mean_d = sum(deltas.values()) / len(deltas)
        rows = []
        for info, (s, _) in zip(devices, plan):
            if s.sid not in deltas:
                continue
            rel = deltas[s.sid] - mean_d
            info["rel_ms"] = round(rel * 1000, 1)
            new_trim = int(round(self._trims.get(s.player_id, 0) - rel * 1000))
            info["trim_ms"] = max(-2000, min(2000, new_trim))
            rows.append(self._sample_row(s, "chirp", error=rel))
            await self.set_trim(s.player_id, new_trim)
            logger.info(f"Chirp calibration {s.name}: {rel * 1000:+.1f} ms "
                        f"in-air → trim {info['trim_ms']} ms")
        await self._record_samples(rows)
        return {"success": True, "devices": devices,
                "spread_ms": round((max(deltas.values()) - min(deltas.values()))
                                   * 1000, 1)}

    # ------------------------------------------------------------------
    # Named groups (managed from the Media tab's group builder)
    # ------------------------------------------------------------------
    def list_groups(self) -> dict:
        groups = []
        for gid, g in self._groups.items():
            groups.append({
                "id": gid,
                "name": g.get("name", gid),
                "members": [{
                    "player_id": pid,
                    "name": self._player_name(pid),
                    "trim_ms": self._trims.get(pid, 0),
                } for pid in g.get("members", [])],
                "active": self.running and self._active_group == gid,
            })
        return {"success": True, "groups": groups}

    def save_group(self, name: str, members: List[str],
                   group_id: str = "") -> dict:
        name = (name or "").strip()
        members = [m for m in (members or []) if m.startswith("cast:")]
        if not name:
            return {"success": False, "error": "Group needs a name"}
        if len(members) < 2:
            return {"success": False, "error": "Pick at least two cast speakers"}
        gid = group_id or uuid_mod.uuid4().hex[:8]
        if group_id and group_id not in self._groups:
            return {"success": False, "error": "Unknown sync group"}
        self._groups[gid] = {"name": name, "members": members}
        self._write_json(self._groups_file, self._groups)
        return {"success": True, "id": gid}

    async def delete_group(self, group_id: str) -> dict:
        if group_id not in self._groups:
            return {"success": False, "error": "Unknown sync group"}
        if self.running and self._active_group == group_id:
            await self.stop_session()
        self._groups.pop(group_id)
        self._write_json(self._groups_file, self._groups)
        return {"success": True}

    # ------------------------------------------------------------------
    # Device launch
    # ------------------------------------------------------------------
    def _player_name(self, player_id: str) -> str:
        uuid_str = player_id.split(":", 1)[1]
        info = self.cast._infos.get(uuid_str)
        return getattr(info, "friendly_name", player_id) if info else player_id

    async def _launch(self, player_id: str, sid: str):
        """Launch the sync receiver app and hand it its session id + trim.
        The start message is re-sent until the receiver's WS hello arrives —
        the page may still be loading when the first message goes out."""
        uuid_str = player_id.split(":", 1)[1]
        cast = await self.cast._get_cast(uuid_str)
        if not cast:
            logger.warning(f"Sync launch: {player_id} unreachable")
            return
        ctrl = self._controllers.get(uuid_str)
        if ctrl is None:
            ctrl = _SyncMessageController()
            await asyncio.to_thread(cast.register_handler, ctrl)
            self._controllers[uuid_str] = ctrl
        await asyncio.to_thread(self.cast._ensure_app, cast, self.app_id)
        payload = {
            "type": "start",
            "sid": sid,
            "trim_ms": self._trims.get(player_id, 0),
        }
        deadline = time.monotonic() + 30
        while self.running and time.monotonic() < deadline:
            if sid in self._receivers:
                return                     # receiver connected — done
            try:
                await asyncio.to_thread(ctrl.push, payload)
            except Exception as e:
                logger.debug(f"Sync start message to {player_id} failed: {e}")
            await asyncio.sleep(2)
        if self.running and sid not in self._receivers:
            logger.warning(f"Sync receiver on {player_id} never connected "
                           f"(app_id registered? device serial enabled for dev?)")

    # ------------------------------------------------------------------
    # Stream (default-receiver) mode — no Cast console registration needed
    # ------------------------------------------------------------------
    def _local_ip_for(self, host: str) -> str:
        """Our LAN IP as seen from ``host`` (the cast device) — the stream
        URL must be reachable from the device, not from localhost."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((host, 9))    # no packets sent for UDP connect
                return s.getsockname()[0]
            finally:
                s.close()
        except Exception:
            return socket.gethostbyname(socket.gethostname())

    async def _launch_stream(self, player_id: str, sid: str):
        """Point the built-in default media receiver at this device's live
        WAV stream. One retry, mirroring the provider's cold-start hardening."""
        uuid_str = player_id.split(":", 1)[1]
        cast = await self.cast._get_cast(uuid_str)
        if not cast:
            logger.warning(f"Sync stream launch: {player_id} unreachable")
            return
        host = getattr(getattr(cast, "cast_info", None), "host", None) or \
            getattr(getattr(cast, "socket_client", None), "host", "")
        url = (f"http://{self._local_ip_for(host)}:{self.http_port}"
               f"/sync/stream/{sid}.wav")
        for attempt in (1, 2):
            if not self.running:
                return
            try:
                await asyncio.to_thread(self._play_stream, cast, url)
                logger.info(f"Sync stream playing on {self._pending.get(sid, {}).get('name', player_id)}")
                return
            except Exception as e:
                logger.warning(f"Sync stream launch attempt {attempt} failed "
                               f"for {player_id}: {e}")
                await asyncio.sleep(2)

    def _play_stream(self, cast, url: str):
        cast.wait(timeout=10)
        mc = cast.media_controller
        mc.play_media(url, "audio/wav", stream_type="LIVE",
                      title="ZMM speaker-sync test")
        mc.block_until_active(timeout=15)
        deadline = time.time() + 8
        while time.time() < deadline:
            if mc.status.player_state in ("PLAYING", "BUFFERING"):
                return
            time.sleep(0.5)
        raise RuntimeError(f"media did not start (state={mc.status.player_state})")

    async def _stream_monitor(self):
        """Converge all stream-mode devices onto a common lag behind the
        server clock. Each poll reads the default receiver's reported media
        time; deliberate stream jumps land after the device drains its HTTP
        buffer, hence the post-jump cooldown."""
        try:
            while self.running:
                await asyncio.sleep(self._poll_interval())
                # Measure all devices concurrently — one poll pass costs one
                # device's round-trip instead of N, and the readings land
                # close together in time (less skew inside a spread sample).
                items = [(sid, st) for sid, st in list(self._streams.items())
                         if st.connected and st.pos is not None]
                if not items:
                    continue
                results = await asyncio.gather(
                    *(self._measure_lag(st) for _, st in items),
                    return_exceptions=True)
                lags: Dict[str, float] = {
                    sid: r for (sid, _), r in zip(items, results)
                    if isinstance(r, float)}
                if not lags:
                    continue
                # Fix the common target from the slowest starter, once. Don't
                # wait forever for stragglers — after 25 s use whoever reports.
                if self._target_lag is None:
                    n_connected = len([s for s in self._streams.values()
                                       if s.connected])
                    if (len(lags) < n_connected
                            and time.monotonic() - self._epoch < 25):
                        continue     # wait until every connected device reports
                    self._target_lag = max(lags.values()) + STREAM_LAG_MARGIN_S
                    logger.info(f"Sync stream target lag: {self._target_lag:.2f}s")
                batch = []
                for sid, lag in lags.items():
                    st = self._streams.get(sid)
                    if st is None:
                        continue
                    if time.monotonic() < st.cooldown_until:
                        continue
                    if st.natural_lag is None:
                        st.natural_lag = lag
                        self._model_learn(st, "lag_s", lag - st.precomp_s)
                        batch.append(self._sample_row(st, "startup", lag=lag))
                    error = lag - self._target_lag   # >0: behind, serve faster
                    st.stats = {"offset_ms": round(error * 1000),
                                "rtt_ms": "n/a", "late": 0,
                                "resyncs": st.resyncs,
                                "drift_ppm": round(st.rate_ppm)}
                    batch.append(self._sample_row(st, "poll", lag=lag,
                                                  error=error))
                    # The measured error still contains whatever slew is
                    # pending (undrained), so decisions are made on the
                    # RESIDUAL — what would remain once the slew finishes.
                    # Deciding on the raw error re-corrected work already
                    # scheduled (a 68 ms trim slew once triggered a jump).
                    st.err_hist = (st.err_hist + [error])[-3:]
                    med3 = sorted(st.err_hist)[len(st.err_hist) // 2]
                    residual = med3 - st.slew_s
                    # Correction ladder (OpenZone §5.2): buffer jumps only
                    # for acquisition/rebuffer; everything inside ±100 ms is
                    # a bounded rate slew through the resampler. Jumps also
                    # wait for 2 agreeing polls and act on the median — a
                    # single reading can be wrong by 100s of ms right after
                    # (re)connect, and jumping on it caused an audible
                    # overshoot-then-jump-back at every session start.
                    # Before first lock the jump threshold drops to the slew
                    # window: the device isn't audibly in the group yet, so
                    # stepping a 30–100 ms startup offset away instantly
                    # beats grinding it out over 30 s.
                    jump_min = (STREAM_JUMP_MIN_S if st.acquired
                                else STREAM_SLEW_FAST_THRESH_S)
                    if not st.acquired and len(st.err_hist) >= 2 \
                            and abs(med3) <= STREAM_SLEW_FAST_THRESH_S:
                        st.acquired = True
                    if abs(residual) > jump_min \
                            and len(st.err_hist) >= 2:
                        st.pos += med3 * RATE
                        st.shift += med3 * RATE
                        st.moved_s += med3
                        st.slew_s = 0.0       # jump supersedes any pending slew
                        st.resyncs += 1
                        st.cooldown_until = time.monotonic() + STREAM_COOLDOWN_S
                        st.err_hist = []
                        st.lag_hist = []      # device-buffer transient follows
                        batch.append(self._sample_row(st, "resync", lag=lag,
                                                      error=med3))
                        logger.info(f"Sync stream resync {st.name}: "
                                    f"{med3 * 1000:+.0f} ms")
                    elif abs(st.slew_s) > STREAM_SLEW_FAST_THRESH_S \
                            or len(st.err_hist) < 2:
                        # Fast slew still draining, or only one reading since
                        # the history was reset — observe, don't act. (A
                        # lone reading has been seen 550 ms wrong.)
                        self._pll_update(st, lag)
                    elif abs(med3) > STREAM_SLEW_FAST_THRESH_S:
                        # Fast slew: 1000 ppm ≈ 1.7 cents, inaudible; drains
                        # in |error| × 1000 s. Gated on the 3-poll median so
                        # a single noise spike can't move real audio, and
                        # slewing the median, not the spike. Assignment, not
                        # +=: pending slew is already inside the measurement.
                        # Refreshes are routine while a slew drains — only a
                        # meaningful change is worth an event row.
                        fresh = abs(med3 - st.slew_s) > 0.010
                        st.slew_s = med3
                        if fresh:
                            batch.append(self._sample_row(st, "slew", lag=lag,
                                                          error=error))
                            logger.info(f"Sync stream slew {st.name}: "
                                        f"{med3 * 1000:+.0f} ms @ "
                                        f"{STREAM_SLEW_FAST_PPM:.0f} ppm")
                        self._pll_update(st, lag)
                    else:
                        # Steady state: gentle trickle (≤20 ppm) on the
                        # denoised error, drift fit on every poll.
                        st.slew_s = med3
                        self._pll_update(st, lag)
                    st.stats["slew_ms"] = round(st.slew_s * 1000, 1)
                await self._record_samples(batch)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Sync stream monitor died: {e}")

    def _sample_row(self, st: _Stream, kind: str, lag: Optional[float] = None,
                    error: Optional[float] = None) -> dict:
        return {"session_id": self._session_id,
                "player_id": st.player_id, "kind": kind, "lag_s": lag,
                "error_ms": None if error is None else error * 1000,
                "rate_ppm": st.rate_ppm,
                "trim_ms": self._trims.get(st.player_id, 0),
                "precomp_s": st.precomp_s, "target_lag_s": self._target_lag}

    async def _record_samples(self, rows: List[dict]):
        """Append measurement rows to the session group's DB (best-effort)."""
        if not rows or _sdb is None:
            return
        try:
            await asyncio.to_thread(_sdb.write_samples, self._active_group, rows)
        except Exception as e:
            logger.debug(f"Sync sample write failed: {e}")

    @staticmethod
    def _fit_slope(pts: List[tuple]) -> Optional[float]:
        """Least-squares slope of (t, y) points; None if degenerate."""
        n = len(pts)
        tm = sum(p[0] for p in pts) / n
        ym = sum(p[1] for p in pts) / n
        den = sum((p[0] - tm) ** 2 for p in pts)
        if den <= 0:
            return None
        return sum((p[0] - tm) * (p[1] - ym) for p in pts) / den

    def _pll_update(self, st: _Stream, lag: float):
        """Drift estimator (OpenZone §7.2): fit the slope of the device's
        FREE-RUNNING lag — measured lag with every deliberate timeline move
        (rate term, slews, jumps) added back via ``moved_s``. That slope IS
        the device's clock drift, measured independently of whatever
        correction is currently applied, so the estimate cannot chase its
        own actuator. (The old integral form folded 0.5× the slope of the
        *corrected* lag into rate_ppm every poll: correction-induced motion
        and ±15 ms poll noise over a 30 s window — a ~300 ppm slope noise
        floor — fed straight back into the estimate, which is why it railed
        at the clamp and sign-flipped on a ~90 s period in live sessions.)"""
        st.lag_hist.append((time.monotonic(), lag + st.moved_s))
        del st.lag_hist[:-STREAM_FIT_MAX_POINTS]
        span = st.lag_hist[-1][0] - st.lag_hist[0][0]
        if len(st.lag_hist) < STREAM_FIT_MIN_POINTS \
                or span < STREAM_FIT_APPLY_SPAN_S:
            return
        slope = self._fit_slope(st.lag_hist)
        if slope is None:
            return
        # One robust pass: drop >3σ outliers (bogus media status, WiFi
        # hiccup) and refit — §7.2's outlier rejection.
        n = len(st.lag_hist)
        tm = sum(p[0] for p in st.lag_hist) / n
        ym = sum(p[1] for p in st.lag_hist) / n
        resid = [p[1] - (ym + slope * (p[0] - tm)) for p in st.lag_hist]
        sd = (sum(r * r for r in resid) / n) ** 0.5
        if sd > 0:
            kept = [p for p, r in zip(st.lag_hist, resid) if abs(r) <= 3 * sd]
            if STREAM_FIT_MIN_POINTS <= len(kept) < n:
                slope = self._fit_slope(kept)
                if slope is None:
                    return
        # A slope no crystal can produce means a step landed in the window —
        # a server stall (observed: 4 s event-loop freeze), a device pipeline
        # hiccup — not drift. Learning from it rails the clamp; start the
        # baseline over instead.
        if abs(slope) * 1e6 > 4 * STREAM_RATE_MAX_PPM:
            st.lag_hist = st.lag_hist[-1:]
            return
        # slope = d(free-running lag)/dt: device clock slow → lag grows →
        # positive ppm → serve faster. The EMA gain scales with the fit's
        # baseline (short spans are noise-dominated); clamp to the physical
        # crystal bound. The model only learns span-backed estimates.
        ema = STREAM_RATE_EMA * min(1.0, span / STREAM_RATE_EMA_SPAN_S)
        st.rate_ppm = max(-STREAM_RATE_MAX_PPM,
                          min(STREAM_RATE_MAX_PPM,
                              st.rate_ppm + ema * (slope * 1e6 - st.rate_ppm)))
        if span >= STREAM_RATE_EMA_SPAN_S:
            self._model_learn(st, "drift_ppm", st.rate_ppm)

    def _model_learn(self, st: _Stream, key: str, value: float):
        """EMA-update one field of the device's learned latency model."""
        m = self._model.setdefault(st.player_id, {})
        old = m.get(key)
        m[key] = round(value if old is None else 0.7 * old + 0.3 * value, 4)
        m["sessions"] = m.get("sessions", 0) + (1 if key == "lag_s" else 0)

    def _acquiring(self) -> bool:
        """True while any device's drift-fit baseline is still building
        (fresh session, or a jump cleared it) — the phase where extra
        measurements buy convergence."""
        for st in self._streams.values():
            if not st.connected or st.pos is None:
                continue
            span = (st.lag_hist[-1][0] - st.lag_hist[0][0]
                    if len(st.lag_hist) >= 2 else 0.0)
            if span < STREAM_FIT_APPLY_SPAN_S:
                return True
        return False

    def _poll_interval(self) -> float:
        """Fast cadence while acquiring — more points early is what lets
        the slope fit beat the sensor noise. Relax once every baseline
        spans the apply threshold."""
        return STREAM_POLL_FAST_S if self._acquiring() else STREAM_POLL_S

    async def _measure_lag_once(self, st: _Stream) -> Optional[float]:
        """One media-time read → lag vs server clock (s), trim excluded."""
        uuid_str = st.player_id.split(":", 1)[1]
        cast = self.cast._casts.get(uuid_str)
        if cast is None:
            return None
        try:
            mc = cast.media_controller
            await asyncio.to_thread(mc.update_status)
            await asyncio.sleep(STREAM_STATUS_WAIT_S)   # let the push arrive
            status = mc.status
            if getattr(status, "player_state", "") != "PLAYING":
                return None
            ct = getattr(status, "adjusted_current_time", None)
            if ct is None:
                ct = getattr(status, "current_time", None)
            if not ct or ct <= 0:
                return None
        except Exception as e:
            logger.debug(f"Sync stream status failed for {st.player_id}: {e}")
            return None
        now = time.monotonic()
        played_timeline_s = (st.start_pos + st.shift) / RATE + float(ct)
        trim_s = self._trims.get(st.player_id, 0) / 1000.0
        return (now - self._epoch) - played_timeline_s - trim_s

    async def _measure_lag(self, st: _Stream) -> Optional[float]:
        """Median of several consecutive reads (more while acquiring).
        adjusted_current_time extrapolates each report to read time, so the
        reads target the same quantity and the median suppresses ~√N of the
        per-read noise while discarding a single bogus status outright."""
        n = (STREAM_STATUS_READS_ACQ if self._acquiring()
             else STREAM_STATUS_READS)
        reads: List[float] = []
        for _ in range(n):
            if not self.running:
                break
            lag = await self._measure_lag_once(st)
            if lag is not None:
                reads.append(lag)
        if not reads:
            return None
        return sorted(reads)[len(reads) // 2]

    async def _pcm_stream(self, st: _Stream):
        """Async generator: endless WAV cut from the shared timeline for one
        device, paced to stay at most STREAM_AHEAD_S ahead of real time."""
        st.connected = True
        st.cooldown_until = max(st.cooldown_until,
                                time.monotonic() + STREAM_CONNECT_GRACE_S)
        logger.info(f"Sync stream opened: {st.name}")
        try:
            yield _wav_header()
            if st.pos is None:
                trim = int(self._trims.get(st.player_id, 0) * RATE / 1000)
                precomp = int(st.precomp_s * RATE)
                st.pos = (int((time.monotonic() - self._epoch) * RATE)
                          - trim - precomp)
                st.start_pos = st.pos
            block = int(RATE * STREAM_BLOCK_S)
            while self.running and self._streams.get(st.sid) is st:
                ahead = (st.pos - st.shift) / RATE - (time.monotonic() - self._epoch)
                if ahead > STREAM_AHEAD_S:
                    await asyncio.sleep(STREAM_BLOCK_S / 2)
                    continue
                # Actuator (OpenZone §5.2): ratio = 1 + drift + slew. The
                # drift term cancels the device clock; the slew term drains
                # the scheduled offset at a bounded rate — fast (1000 ppm)
                # above 15 ms remaining, gentle (20 ppm) below. All of it is
                # fractional through the resampler; the buffer never steps.
                rm = 0.0
                if st.slew_s:
                    ppm = (STREAM_SLEW_FAST_PPM
                           if abs(st.slew_s) > STREAM_SLEW_FAST_THRESH_S
                           else STREAM_SLEW_GENTLE_PPM)
                    lim = (block / RATE) * ppm / 1e6
                    rm = max(-lim, min(lim, st.slew_s))
                    st.slew_s -= rm
                adv = block * (1.0 + st.rate_ppm / 1e6) + rm * RATE
                pcm = _resample_block(st.pos, block, adv, st.chirp)
                st.pos += adv
                st.shift += adv - block
                st.moved_s += (adv - block) / RATE   # decompensate drift fit
                yield pcm
        except asyncio.CancelledError:
            pass
        finally:
            st.connected = False
            logger.info(f"Sync stream closed: {st.name}")

    # ------------------------------------------------------------------
    # Chunk producer
    # ------------------------------------------------------------------
    async def _produce(self):
        """Generate chunks on the shared timeline and fan out to receivers.
        Chunk i must start playing at epoch + LEAD + i*CHUNK_SECONDS (server
        clock); we emit it AHEAD_SECONDS early."""
        i = 0
        try:
            while self.running:
                play_at = self._epoch + LEAD_SECONDS + i * CHUNK_SECONDS
                wait = (play_at - AHEAD_SECONDS) - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                pcm = await asyncio.to_thread(_gen_chunk, i)
                frame = struct.pack(">d", play_at) + pcm
                self._buffer.append(frame)
                if len(self._buffer) > BUFFER_CHUNKS:
                    self._buffer.pop(0)
                for r in list(self._receivers.values()):
                    try:
                        await r.ws.send_bytes(frame)
                    except Exception:
                        self._receivers.pop(r.sid, None)
                i += 1
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Cast sync producer died: {e}")

    # ------------------------------------------------------------------
    # HTTP sub-app (plain HTTP: receiver page + WebSocket)
    # ------------------------------------------------------------------
    def _build_http_app(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/cast/sync_receiver.html")
        async def receiver_page():
            return FileResponse("static/cast/sync_receiver.html",
                                media_type="text/html")

        @app.get("/health")
        async def health():
            return {"ok": True, "running": self.running}

        @app.get("/sync/stream/{sid}.wav")
        async def stream_wav(sid: str):
            st = self._streams.get(sid)
            if st is None or not self.running:
                return Response(status_code=404)
            return StreamingResponse(self._pcm_stream(st),
                                     media_type="audio/wav",
                                     headers={"Cache-Control": "no-store"})

        @app.websocket("/ws")
        async def ws_endpoint(ws: WebSocket):
            await ws.accept()
            receiver: Optional[_Receiver] = None
            try:
                while True:
                    msg = await ws.receive_json()
                    mtype = msg.get("type")
                    if mtype == "ping":
                        # NTP-style: echo the receiver's timestamp + our clock.
                        await ws.send_json({"type": "pong", "t": msg.get("t"),
                                            "s": time.monotonic()})
                    elif mtype == "hello":
                        sid = str(msg.get("sid") or "")
                        info = self._pending.get(sid)
                        if not info:
                            await ws.send_json({"type": "error",
                                                "error": "unknown sid"})
                            continue
                        receiver = _Receiver(sid, ws)
                        receiver.player_id = info["player_id"]
                        receiver.name = info["name"]
                        self._receivers[sid] = receiver
                        logger.info(f"Sync receiver connected: {info['name']}")
                        await ws.send_json({
                            "type": "hello_ack",
                            "rate": RATE, "channels": CHANNELS,
                            "chunk_s": CHUNK_SECONDS,
                            "trim_ms": self._trims.get(info["player_id"], 0),
                        })
                        # Catch the joiner up with still-future buffered chunks.
                        now = time.monotonic()
                        for frame in list(self._buffer):
                            (play_at,) = struct.unpack(">d", frame[:8])
                            if play_at > now + 0.2:
                                await ws.send_bytes(frame)
                    elif mtype == "stats" and receiver is not None:
                        receiver.stats = {k: v for k, v in msg.items()
                                          if k != "type"}
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.debug(f"Sync WS error: {e}")
            finally:
                if receiver is not None:
                    self._receivers.pop(receiver.sid, None)
                    logger.info(f"Sync receiver disconnected: {receiver.name}")

        return app

    # ------------------------------------------------------------------
    # JSON persistence (trims + groups)
    # ------------------------------------------------------------------
    @staticmethod
    def _read_json(path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"Could not read {path}: {e}")
            return {}

    @staticmethod
    def _write_json(path: str, obj: dict) -> None:
        try:
            d = os.path.dirname(path) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.error(f"Could not write {path}: {e}")
