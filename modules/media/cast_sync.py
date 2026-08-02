"""
CastSyncPoc — synchronised multi-speaker casting.
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
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse

try:
    from modules.media import sync_db as _sdb
except Exception:                                    # pragma: no cover
    _sdb = None

from modules.media import sync_chirp as _chirp
from modules.media import sync_resample as _rs
from modules.media import sync_source as _src
from modules.media.sync_source import (RATE, CHANNELS, GeneratedSource,
                                       MediaSource)

logger = logging.getLogger("modules.media.cast_sync")

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
STREAM_POLL_FAST_S = 1.0         # cadence while acquiring
STREAM_STATUS_READS = 3          # media-time reads per poll (median)
STREAM_STATUS_READS_ACQ = 5      # ...while acquiring: denser + more robust.
STREAM_STATUS_WAIT_S = 0.2       # wait for each status push to land
STREAM_STATUS_MAX_AGE_S = 5.0
STREAM_COOLDOWN_S = 7.0          # ignore polls this long after a jump
STREAM_CONNECT_GRACE_S = 4.0     # ignore polls this long after a stream
STREAM_RECONNECT_GRACE_S = 15.0
STREAM_JUMP_MIN_S = 0.10         # hard resync only beyond this
STREAM_SLEW_FAST_PPM = 1000.0    # |offset| > fast threshold (≈1.7 cents, inaudible)
STREAM_SLEW_GENTLE_PPM = 20.0    # steady-state slew cap
STREAM_SLEW_FAST_THRESH_S = 0.030
STREAM_SLEW_MAX_S = STREAM_JUMP_MIN_S
STREAM_RATE_MAX_PPM = 50.0
STREAM_FIT_MIN_POINTS = 8        # polls needed before the drift fit runs
STREAM_FIT_MAX_POINTS = 360      # ≈20 min of polls
STREAM_FIT_APPLY_SPAN_S = 60.0   # shorter baselines are all noise — hold off
STREAM_RATE_EMA = 0.25           # max blend of a new fit into rate_ppm
STREAM_RATE_EMA_SPAN_S = 240.0   # EMA scales up linearly to full at this span
STREAM_FIT_MIN_SIGMA = 2.0
STREAM_RATE_STALE_S = 90.0       # unsupported this long → fall back to prior
STREAM_RATE_DECAY = 0.05         # per poll, toward rate_prior
TRIM_MODEL_AGREE_MS = 25         # units of one model trimmed further apart
STREAM_TRIM_QUIET_MS = 10        # trim steps at or below this are inside the
STREAM_TRIM_SETTLE_S = 3.0
STREAM_ACQUIRE_MAX_S = 15.0      # never hold the group silent longer than this
START_DEDUPE_S = 30.0
STREAM_FADE_IN_S = 0.4

SPECTRUM_FFT_N = 2048
SPECTRUM_BANDS = 48
SPECTRUM_FPS = 15                # display cadence; see _spectrum_feed on cost
SPECTRUM_F_LO = 25.0
SPECTRUM_F_HI = 18000.0
SPECTRUM_FLOOR_DB = -72.0


_GENERATED = GeneratedSource()


def _ws_clients() -> bool:
    """Whether any browser is connected. Imported lazily and defensively: the
    spectrum feed is decoration, and a websocket layer that is absent or
    mid-reload must degrade to "no display" rather than raise into the
    session."""
    try:
        from routes.websocket_routes import manager
        return bool(manager.active_connections)
    except Exception:
        return False


async def _broadcast_spectrum(payload: dict) -> None:
    try:
        from routes.websocket_routes import broadcast_event
        await broadcast_event("zone_spectrum", payload)
    except Exception:
        pass


def _media_key(media: Optional[dict]) -> tuple:
    """Identity of a start request's media, for duplicate detection."""
    m = media or {}
    return (str(m.get("kind") or "track").lower(),
            m.get("media_type") or "", m.get("source_id") or "",
            m.get("station_uuid") or "", (m.get("url") or "").strip())


def _encode_s16(pcm: np.ndarray) -> bytes:
    """Interleaved s16le from float samples shaped ``(frames, CHANNELS)``."""
    return (np.clip(pcm, -0.98, 0.98) * 32767).astype("<i2").tobytes()


def _gen_samples(n0: int, frames: int) -> bytes:
    """Unity-ratio PCM straight off the integer grid (WS/chunk mode)."""
    return _encode_s16(_GENERATED.read(n0, frames))


def _chunk_pcm(source, index: int) -> bytes:
    """WS-mode framing: fixed CHUNK_FRAMES chunk ``index`` of the timeline."""
    return _encode_s16(source.read(index * CHUNK_FRAMES, CHUNK_FRAMES))


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
        self.gen = 0          # newest fetch wins (open-zone.md §A.5)
        self.pos: Optional[float] = None  # next timeline sample (fractional)
        self.start_pos: int = 0      # timeline sample of the first PCM byte
        self.shift: float = 0.0      # cumulative deliberate moves (samples)
        self.natural_lag: Optional[float] = None   # first stable lag (s)
        self.trim_ms: int = 0  # LATCHED for the session (open-zone.md §A.4)
        self.cooldown_until: float = 0.0   # skip polls until then after a jump
        self.resyncs: int = 0
        self.reconnects: int = 0     # control-socket resets seen this session
        self.stats: dict = {}
        self.rate_ppm: float = 0.0   # >0 = device clock slow, serve faster
        self.rate_prior: float = 0.0
        self.slew_s: float = 0.0     # pending offset to slew away (s, >0 =
        self.moved_s: float = 0.0
        self.err_hist: List[float] = []   # last 3 poll errors (median filter)
        self.acquired: bool = False
        self.chirp: Optional[tuple] = None
        self.lag_hist: List[tuple] = []   # (t, lag+moved_s) for drift fitting
        self.fit_lost_at: float = 0.0
        self.precomp_s: float = 0.0  # model-predicted lag pre-compensation
        self.resampler = None


class _ConnWatch:
    """Control-socket status listener for one Cast device."""

    __slots__ = ("st", "_dropped")

    def __init__(self, st: Optional[_Stream] = None):
        self.st = st
        self._dropped = False

    def new_connection_status(self, status) -> None:
        st = self.st
        state = getattr(status, "status", "") or ""
        if state in ("DISCONNECTED", "LOST", "FAILED", "FAILED_RESOLVE"):
            self._dropped = True
            return
        if state != "CONNECTED" or not self._dropped:
            return          # first connect of the session is not a RE-connect
        self._dropped = False
        if st is None:
            return
        st.reconnects += 1
        st.cooldown_until = max(st.cooldown_until,
                                time.monotonic() + STREAM_RECONNECT_GRACE_S)
        st.err_hist = []
        st.lag_hist = []
        st.fit_lost_at = time.monotonic()
        st.stats["reconnects"] = st.reconnects
        logger.info(f"Sync control socket reconnected: {st.name} — holding "
                    f"corrections {STREAM_RECONNECT_GRACE_S:.0f}s "
                    f"(reconnect #{st.reconnects})")


class CastSyncPoc:
    def __init__(self, cast_provider, cfg: dict):
        cfg = cfg or {}
        self.cast = cast_provider                      # CastPlayerProvider
        self.http_port = int(cfg.get("http_port", 8010))
        self.app_id = (cfg.get("app_id") or "").strip()
        self._trims_file = cfg.get("trims_file", "./data/cast_sync_trims.json")
        self._trims: Dict[str, int] = {
            k: int(v) for k, v in self._read_json(self._trims_file).items()}
        self._model_trims_file = cfg.get("model_trims_file",
                                         "./data/cast_sync_model_trims.json")
        self._model_trims: Dict[str, int] = {
            k: int(v) for k, v in self._read_json(self._model_trims_file).items()}
        self._groups_file = cfg.get("groups_file", "./data/cast_sync_groups.json")
        self._groups: Dict[str, dict] = self._read_json(self._groups_file)
        self._active_group: str = ""               # gid of the running session
        self._session_media: Optional[dict] = None  # media of the running session
        self._conn_watch: Dict[str, _ConnWatch] = {}
        self._session_players: List[str] = []
        self._session_started_at: float = 0.0
        self._model_file = cfg.get("model_file", "./data/cast_sync_model.json")
        self._model: Dict[str, dict] = self._read_json(self._model_file)
        self._session_id: str = ""
        self._mic_device = cfg.get("mic_device") or None
        self._calibrating = False
        self._mic_cache: Optional[Tuple[float, dict]] = None

        self._source = _GENERATED
        self._resampler_kind = str(cfg.get("resampler", "rust"))
        self._source_delay_s = float(cfg.get("source_delay_s", 2.0))
        self._ring_capacity_s = float(cfg.get("ring_capacity_s", 20.0))
        self._crossfade_s = float(cfg.get("crossfade_s", 0.0))
        self._session_crossfade_s = self._crossfade_s
        self._eq_engine = None
        self._url_resolver = None      # async (media_type, source_id) -> url
        self._queue_resolver = None    # async (media_type, kind, id) -> [items]
        self._queue: List[dict] = []
        self._queue_pos = 0
        self._trim_learn_tasks: Dict[str, asyncio.Task] = {}
        self._fade_start: Optional[float] = None

        self._http_server = None                       # uvicorn.Server
        self._http_task: Optional[asyncio.Task] = None
        self._producer: Optional[asyncio.Task] = None
        self._auto_stop: Optional[asyncio.Task] = None
        self._duration_s: int = 0                      # 0 = run until stopped
        self._launch_tasks: List[asyncio.Task] = []

        self.running = False
        self._session_lock = asyncio.Lock()
        self._epoch: float = 0.0
        self._buffer: List[bytes] = []                 # last N framed chunks
        self._receivers: Dict[str, _Receiver] = {}     # sid -> _Receiver
        self._pending: Dict[str, dict] = {}            # sid -> {player_id, name}
        self._controllers: Dict[str, object] = {}      # cast uuid -> controller
        self._streams: Dict[str, _Stream] = {}         # sid -> _Stream
        self._monitor: Optional[asyncio.Task] = None
        self._spectrum: Optional[asyncio.Task] = None
        self._target_lag: Optional[float] = None       # common lag target (s)

    # ------------------------------------------------------------------
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
    # ------------------------------------------------------------------
    def set_eq_engine(self, engine) -> None:
        self._eq_engine = engine

    def set_url_resolver(self, fn) -> None:
        """``async (media_type, source_id) -> url`` for sources whose URLs
        expire. Wired by MediaService; absent means URL-only sources."""
        self._url_resolver = fn

    def set_queue_resolver(self, fn) -> None:
        """``async (media_type, kind, id) -> [MediaItem-ish dicts]``, which is
        what makes a Tidal album, playlist, mix or artist playable to a zone
        rather than just a single track. Wired by MediaService."""
        self._queue_resolver = fn

    def now_playing(self) -> dict:
        """The item a zone is currently on, for the UI and the Cast display."""
        q, i = self._queue, self._queue_pos
        item = q[i] if 0 <= i < len(q) else {}
        return {"title": item.get("title", ""), "artist": item.get("artist", ""),
                "artwork_url": item.get("artwork_url", ""),
                "index": i if q else 0, "count": len(q)}

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _model_key(self, player_id: str) -> str:
        try:
            get = getattr(getattr(self, "cast", None), "model_key", None)
            return get(player_id) if callable(get) else ""
        except Exception:
            return ""

    def trim_ms(self, player_id: str) -> int:
        """Effective trim: an explicit per-device value, else whatever this
        model has been found to need, else nothing."""
        if player_id in self._trims:
            return int(self._trims[player_id])
        key = self._model_key(player_id)
        return int(self._model_trims.get(key, 0)) if key else 0

    def _learn_model_trim(self, player_id: str, trim_ms: int) -> None:
        """Record a settled trim against the device's model."""
        key = self._model_key(player_id)
        if not key:
            return
        task = self._trim_learn_tasks.pop(key, None)
        if task is not None:
            task.cancel()

        async def _settle():
            try:
                await asyncio.sleep(STREAM_TRIM_SETTLE_S)
            except asyncio.CancelledError:
                return
            self._trim_learn_tasks.pop(key, None)
            peers = [int(v) for pid, v in self._trims.items()
                     if pid != player_id and self._model_key(pid) == key]
            disputed = [v for v in peers if abs(v - trim_ms) > TRIM_MODEL_AGREE_MS]
            if disputed:
                if self._model_trims.pop(key, None) is not None:
                    self._write_json(self._model_trims_file, self._model_trims)
                logger.info(
                    f"Model trim for '{key}' dropped: units disagree "
                    f"({trim_ms:+d} ms vs {', '.join(f'{v:+d}' for v in disputed)} ms) "
                    f"— this trim is positional, not a property of the hardware")
                return
            if self._model_trims.get(key) == trim_ms:
                return
            self._model_trims[key] = int(trim_ms)
            self._write_json(self._model_trims_file, self._model_trims)
            logger.info(f"Learned trim {trim_ms:+d} ms for model '{key}' — new "
                        f"devices of this model will start pre-aligned")

        self._trim_learn_tasks[key] = asyncio.create_task(_settle())

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def session_snapshot(self) -> dict:
        """What it would take to stand this session back up, or {} when idle."""
        if not self.running or not self._streams:
            return {}
        snap = {
            "group_id": self._active_group,
            "player_ids": [st.player_id for st in self._streams.values()],
            "media": self._session_media,
        }
        if self._duration_s:
            elapsed = time.monotonic() - self._epoch
            snap["remaining_s"] = max(0, int(self._duration_s - elapsed))
        return snap

    async def resume_session(self, rec: dict, age_s: float) -> bool:
        """Re-launch a session that a restart interrupted."""
        if self.running or not rec:
            return False
        remaining = rec.get("remaining_s")
        if remaining is not None:
            remaining = int(remaining - age_s)
            if remaining <= 5:
                logger.info("Not resuming sync session — its window had expired")
                return False
        players = rec.get("player_ids") or []
        gid = rec.get("group_id") or ""
        if not gid and not players:
            return False
        logger.info(f"Resuming sync session after restart ({age_s:.0f}s gap)")
        res = await self.start_session(players if not gid else None,
                                       group_id=gid,
                                       duration_s=int(remaining or 0),
                                       media=rec.get("media"))
        if not res.get("success"):
            logger.warning(f"Sync session resume failed: {res.get('error')}")
        return bool(res.get("success"))

    async def _build_source(self, media: Optional[dict], group_id: str):
        """Master timeline for this session: real media when given, else the
        generated test signal. Failing to open the media is reported rather
        than silently falling back — a group playing a test tone when the user
        asked for a station is worse than an error."""
        media = dict(media or {})
        provider = None
        self._queue, self._queue_pos = [], 0
        stype, sid = media.get("media_type") or "", media.get("source_id") or ""
        kind = (media.get("kind") or "track").strip() or "track"
        loop_forever = bool(media.get("loop"))
        if sid and self._url_resolver is not None:
            if kind != "track" and self._queue_resolver is not None:
                try:
                    self._queue = list(await self._queue_resolver(stype, kind, sid))
                except Exception as e:
                    return None, f"could not open {kind}: {e}"
                if not self._queue:
                    return None, (f"that {kind} is empty, unavailable, or needs "
                                  f"a signed-in {stype or 'source'} account")
            else:
                self._queue = [{"source_id": sid,
                                "title": (media.get("title") or "").strip(),
                                "artwork_url": media.get("artwork_url") or ""}]

            async def provider(last_rc=None):
                if last_rc == 0:
                    self._queue_pos += 1
                    if self._queue_pos >= len(self._queue):
                        if not loop_forever:
                            return ""
                        self._queue_pos = 0
                item = self._queue[self._queue_pos]
                url = await self._url_resolver(stype, item.get("source_id") or "")
                if not url:
                    return ""
                return {"url": url, "title": item.get("title") or ""}

            try:
                first = await provider()
            except Exception as e:
                return None, f"could not resolve {stype or 'source'}: {e}"
            media["url"] = (first or {}).get("url") if isinstance(first, dict) else first
            if not media["url"]:
                return None, (f"{stype or 'source'} returned no playable stream "
                              f"(is the account still signed in?)")
            media.setdefault("title", "")
            media["title"] = media["title"] or self._queue[0].get("title") or ""
        if not media or not (media.get("url") or "").strip():
            return _GENERATED, ""
        chain = None
        if self._eq_engine is not None:
            try:
                chain = self._eq_engine.make_chain(f"syncgroup:{group_id}")
            except Exception as e:
                logger.debug(f"Sync EQ chain unavailable: {e}")
        src = MediaSource(media["url"].strip(), self._epoch,
                          delay_s=self._source_delay_s,
                          capacity_s=self._ring_capacity_s, eq_chain=chain,
                          loop_forever=loop_forever,
                          title=(media.get("title") or "").strip(),
                          url_provider=provider,
                          crossfade_s=self._session_crossfade_s,
                          reader_pos=self._reader_head)
        try:
            await src.start()
        except Exception as e:
            await src.close()
            return None, str(e)
        return src, ""

    async def _prime_source(self, max_precomp_s: float) -> None:
        """Fill the delay line before any device reads from it."""
        src = self._source
        if src is _GENERATED:
            return
        target = src.delay_s + max(0.0, max_precomp_s)
        if await src.prime(timeout=target + 3.0, target_s=target):
            logger.info(f"Sync source primed {target:.1f}s of timeline")
        else:
            logger.warning(
                f"Sync source primed only {src.buffered_s():.1f}s of the "
                f"{target:.1f}s needed — expect silence at session start")

    def _reader_head(self) -> Optional[int]:
        """Furthest-ahead device read position in timeline samples, for the
        source's rework budget (open-zone.md §4.1a). None = nothing reading."""
        heads = [st.pos for st in self._streams.values() if st.pos is not None]
        return max(heads) if heads else None

    def _crossfade_max(self) -> float:
        """Ceiling advertised to the UI. The overlap actually granted is
        decided per seam against measured headroom (open-zone.md §4.1a)."""
        return max(0.0, self._source_delay_s - _src.XFADE_GUARD_S)

    async def start_session(self, player_ids: Optional[List[str]] = None,
                            group_id: str = "", duration_s: int = 0,
                            media: Optional[dict] = None,
                            crossfade_s: Optional[float] = None) -> dict:
        async with self._session_lock:
            return await self._start_session_locked(
                player_ids, group_id, duration_s, media, crossfade_s)

    async def _start_session_locked(self, player_ids: Optional[List[str]],
                                    group_id: str, duration_s: int,
                                    media: Optional[dict],
                                    crossfade_s: Optional[float]) -> dict:
        if group_id:
            group = self._groups.get(group_id)
            if not group:
                return {"success": False, "error": "Unknown sync group"}
            player_ids = group.get("members", [])
        if not player_ids:
            return {"success": False, "error": "No players to start"}
        if (self.running
                and self._active_group == group_id
                and sorted(player_ids) == sorted(self._session_players)
                and _media_key(media) == _media_key(self._session_media)
                and time.monotonic() - self._session_started_at < START_DEDUPE_S):
            logger.info("Duplicate sync start ignored — same request "
                        f"{time.monotonic() - self._session_started_at:.1f}s "
                        "after the session it asks for started")
            return {"success": True, "duplicate": True,
                    "launched": [{"player_id": i["player_id"],
                                  "name": i["name"], "sid": sid}
                                 for sid, i in self._pending.items()],
                    "errors": {},
                    "mode": "stream" if not self.app_id else "receiver",
                    "source": self._source.kind,
                    "duration_s": self._duration_s}
        if self.running:
            await self._stop_session_locked()
        self._active_group = group_id
        self._session_players = list(player_ids)
        stream_mode = not self.app_id   # no registered receiver -> default receiver
        self._session_id = uuid_mod.uuid4().hex[:8]
        self._duration_s = max(0, int(duration_s or 0))
        self._session_crossfade_s = (
            self._crossfade_s if crossfade_s is None
            else min(max(float(crossfade_s), 0.0), self._crossfade_max()))

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
        self._fade_start = None      # every session re-acquires under silence
        source, err = await self._build_source(media, group_id)
        if source is None:
            self._active_group = ""
            return {"success": False, "error": f"Could not open media: {err}"}
        self._source = source
        self._session_media = media or None
        self.running = True
        self._session_started_at = time.monotonic()

        model_lags = {pid: self._model.get(pid, {}).get("lag_s")
                      for pid in player_ids}
        if stream_mode and all(v is not None for v in model_lags.values()):
            self._target_lag = max(model_lags.values()) + STREAM_LAG_MARGIN_S
            logger.info(f"Sync stream target lag from model: {self._target_lag:.2f}s")
        max_precomp = max((max(0.0, (self._target_lag or 0.0) - (lag or 0.0))
                           for lag in model_lags.values()), default=0.0)
        await self._prime_source(max_precomp)
        if not stream_mode:
            self._producer = asyncio.create_task(self._produce())

        launched, errors = [], {}
        for pid in player_ids:
            sid = uuid_mod.uuid4().hex[:12]
            name = self._player_name(pid)
            self._pending[sid] = {"player_id": pid, "name": name}
            try:
                if stream_mode:
                    st = _Stream(sid, pid, name)
                    st.trim_ms = self.trim_ms(pid)   # latched for the session
                    st.resampler = _rs.make(self._resampler_kind, RATE, CHANNELS)
                    m = self._model.get(pid, {})
                    if self._target_lag is not None and m.get("lag_s") is not None:
                        st.precomp_s = max(0.0, self._target_lag - m["lag_s"])
                    st.rate_ppm = max(-STREAM_RATE_MAX_PPM,
                                      min(STREAM_RATE_MAX_PPM,
                                          float(m.get("drift_ppm", 0.0))))
                    st.rate_prior = st.rate_ppm
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
        self._spectrum = asyncio.create_task(self._spectrum_feed())
        if self._duration_s:
            self._auto_stop = asyncio.create_task(
                self._auto_stop_after(self._duration_s))
        logger.info(f"Cast sync session started for {len(launched)} device(s) "
                    f"({'default-receiver stream' if stream_mode else 'custom receiver'} mode"
                    f", source={self._source.kind}"
                    f"{f', {self._duration_s}s window' if self._duration_s else ''})")
        return {"success": True, "launched": launched, "errors": errors,
                "mode": "stream" if stream_mode else "receiver",
                "source": self._source.kind,
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
        async with self._session_lock:
            return await self._stop_session_locked()

    async def _stop_session_locked(self) -> dict:
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
        if self._spectrum:
            self._spectrum.cancel()
            self._spectrum = None
        if self._auto_stop and self._auto_stop is not asyncio.current_task():
            self._auto_stop.cancel()
        self._auto_stop = None
        if self._streams:    # persist what this session taught the model
            self._write_json(self._model_file, self._model)
        for st in self._streams.values():
            if st.resampler is not None:
                try:
                    st.resampler.close()
                except Exception:
                    pass
        self._streams = {}   # generators see running=False and finish
        for w in self._conn_watch.values():
            w.st = None
        if self._source is not _GENERATED:
            try:
                await self._source.close()
            except Exception as e:
                logger.debug(f"Sync source close failed: {e}")
            self._source = _GENERATED
        for r in list(self._receivers.values()):
            try:
                await r.ws.close()
            except Exception:
                pass
        self._receivers = {}
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
        self._session_media = None
        self._session_players = []
        self._session_started_at = 0.0
        self._queue, self._queue_pos = [], 0
        self._fade_start = None
        for t in self._trim_learn_tasks.values():
            t.cancel()
        self._trim_learn_tasks = {}
        logger.info("Cast sync session stopped")
        return {"success": True}

    def _mic_status(self) -> dict:
        """Capture-device probe for the OpenZone mic badge (cached 30 s)."""
        now = time.monotonic()
        if self._mic_cache and now - self._mic_cache[0] < 30.0:
            return self._mic_cache[1]
        info: dict = {"available": False, "selected": None,
                      "configured": self._mic_device, "inputs": []}
        try:
            import sounddevice as sd
            prev = self._mic_cache[1] if self._mic_cache else None
            if not self._calibrating and (prev is None
                                          or not prev["available"]):
                sd._terminate()
                sd._initialize()
            info["inputs"] = [d["name"] for d in sd.query_devices()
                              if d["max_input_channels"] > 0]
            sel = (sd.query_devices(self._mic_device, "input")
                   if self._mic_device is not None
                   else sd.query_devices(kind="input"))
            info["selected"] = sel["name"]
            info["available"] = True
        except Exception as e:
            info["error"] = str(e)
        self._mic_cache = (now, info)
        return info

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
                "trim_ms": (s.trim_ms if s is not None
                            else self.trim_ms(info["player_id"])),
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
            "mic": self._mic_status(),
            "now_playing": self.now_playing(),
            "source": self._source.stats(),
            "resampler": {"kind": self._resampler_kind, **_rs.available()},
            "crossfade": {
                "default_s": self._crossfade_s,
                "session_s": self._session_crossfade_s,
                "max_s": round(self._crossfade_max(), 2),
                "min_s": _src.XFADE_MIN_S,
            },
            "devices": devices,
        }

    async def set_trim(self, player_id: str, trim_ms: int) -> dict:
        trim_ms = max(-2000, min(2000, int(trim_ms)))
        self._trims[player_id] = trim_ms
        self._write_json(self._trims_file, self._trims)
        self._learn_model_trim(player_id, trim_ms)
        for r in self._receivers.values():
            if r.player_id == player_id:
                try:
                    await r.ws.send_json({"type": "trim", "trim_ms": trim_ms})
                except Exception as e:
                    logger.debug(f"trim push failed for {player_id}: {e}")
        for s in self._streams.values():
            if s.player_id != player_id:
                continue
            delta_ms = trim_ms - s.trim_ms
            delta_s = delta_ms / 1000.0
            s.trim_ms = trim_ms
            if s.pos is None:
                continue          # not serving yet: picked up when it opens
            s.pos -= delta_s * RATE
            s.shift -= delta_s * RATE
            s.moved_s -= delta_s
            if abs(delta_ms) > STREAM_TRIM_QUIET_MS:
                s.cooldown_until = time.monotonic() + STREAM_COOLDOWN_S
                s.err_hist = []   # baseline moved — old medians invalid
            await self._record_samples([self._sample_row(s, "trim")])
        return {"success": True, "player_id": player_id, "trim_ms": trim_ms}

    # ------------------------------------------------------------------
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
        head_s = max((s.pos - s.shift) / RATE for s in streams)
        plan = []          # (stream, expected arrival in elapsed-seconds)
        for i, s in enumerate(streams):
            slot_s = head_s + _chirp.CHIRP_LEAD_S + i * _chirp.CHIRP_GAP_S
            s.chirp = (int(slot_s * RATE), wave)
            trim_s = s.trim_ms / 1000.0
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
            detail = ", ".join(f"{d['name']} peak×{d['quality']}"
                               f"{'' if d['detected'] else ' (no chirp)'}"
                               for d in devices)
            logger.warning(
                f"Chirp calibration found no usable arrivals — trims unchanged. "
                f"Needs a mic that can hear the speakers (min peak ratio "
                f"{_chirp.MIN_PEAK_RATIO}): {detail}")
            return {"success": False, "devices": devices,
                    "error": "Chirps not detected on enough speakers — "
                             "check the mic and its input level"}
        mean_d = sum(deltas.values()) / len(deltas)
        rows = []
        for info, (s, _) in zip(devices, plan):
            if s.sid not in deltas:
                continue
            rel = deltas[s.sid] - mean_d
            info["rel_ms"] = round(rel * 1000, 1)
            new_trim = int(round(s.trim_ms - rel * 1000))
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
                    "trim_ms": self.trim_ms(pid),
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
            "trim_ms": self.trim_ms(player_id),
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
        self._watch_connection(cast, uuid_str, self._streams.get(sid))
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

    def _watch_connection(self, cast, uuid_str: str,
                          st: Optional[_Stream]) -> None:
        """Point this device's connection watcher at the current stream,
        registering it with pychromecast the first time we see the device.
        """
        try:
            watch = self._conn_watch.get(uuid_str)
            if watch is None:
                sock = getattr(cast, "socket_client", None)
                reg = getattr(sock, "register_connection_listener", None)
                if reg is None:
                    return
                watch = _ConnWatch()
                reg(watch)
                self._conn_watch[uuid_str] = watch
            watch.st = st
        except Exception as e:
            logger.debug(f"Sync connection watch unavailable for {uuid_str}: {e}")

    def _session_art(self) -> tuple:
        """(artwork_url, title, artist) for what this zone is playing."""
        m = self._session_media or {}
        title = (m.get("title") or "").strip() or "ZMM OpenZone"
        art = (m.get("artwork_url") or "").strip()
        artist = (m.get("artist") or "").strip()
        if not art and self._queue:
            art = (self._queue[0].get("artwork_url") or "").strip()
        if len(self._queue) > 1:
            artist = artist or f"{len(self._queue)} tracks"
        return art, title, artist

    def _play_stream(self, cast, url: str):
        cast.wait(timeout=10)
        mc = cast.media_controller
        art, title, artist = self._session_art()
        meta = {"metadataType": 3, "title": title, "artist": artist}
        if art:
            meta["images"] = [{"url": art}]
        kwargs = dict(content_type="audio/wav", stream_type="LIVE",
                      title=title, thumb=art or None, metadata=meta)
        try:
            mc.play_media(url, **kwargs)
        except TypeError:
            mc.play_media(url, "audio/wav", stream_type="LIVE", title=title)
        mc.block_until_active(timeout=15)
        deadline = time.time() + 8
        while time.time() < deadline:
            if mc.status.player_state in ("PLAYING", "BUFFERING"):
                return
            time.sleep(0.5)
        raise RuntimeError(f"media did not start (state={mc.status.player_state})")

    def _source_spent(self) -> bool:
        """True once the material has run out AND been heard."""
        src = self._source
        if not getattr(src, "finished", False):
            return False
        try:
            head_s = float(src.stats().get("head_s") or 0.0)
        except Exception:
            return True
        return (time.monotonic() - self._epoch) >= head_s + (self._target_lag or 0.0)

    async def _stream_monitor(self):
        """Converge all stream-mode devices onto a common lag behind the
        server clock. Each poll reads the default receiver's reported media
        time; deliberate stream jumps land after the device drains its HTTP
        buffer, hence the post-jump cooldown."""
        try:
            while self.running:
                await asyncio.sleep(self._poll_interval())
                if self._source_spent():
                    logger.info("Sync source finished and drained — "
                                "stopping the session")
                    asyncio.create_task(self.stop_session())
                    return
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
                    st.err_hist = (st.err_hist + [error])[-3:]
                    med3 = self._median(st.err_hist)
                    residual = med3 - st.slew_s
                    jump_min = (STREAM_JUMP_MIN_S if st.acquired
                                else STREAM_SLEW_FAST_THRESH_S)
                    if not st.acquired and len(st.err_hist) >= 2 \
                            and abs(med3) <= STREAM_SLEW_FAST_THRESH_S:
                        st.acquired = True
                    concordant = (len(st.err_hist) >= 3
                                  and min(abs(e) for e in st.err_hist) > jump_min
                                  and min(st.err_hist) * max(st.err_hist) > 0)
                    if abs(residual) > jump_min \
                            and (concordant
                                 or (not st.acquired and len(st.err_hist) >= 2)):
                        # Clamp to the timeline that exists (open-zone.md §A.2).
                        # Defensive: this runs inside the monitor's catch-all,
                        # so a source without the accessor would not degrade
                        # the clamp — it would kill every correction.
                        step = med3 * RATE
                        _latest = getattr(self._source, "latest_sample", None)
                        ceil = ((_latest() - _rs.READ_MARGIN
                                 - RATE * STREAM_BLOCK_S)
                                if callable(_latest) else float("inf"))
                        if st.pos + step > ceil:
                            step = max(0.0, ceil - st.pos)
                            logger.warning(
                                f"Sync stream resync {st.name} clamped to the "
                                f"write head: wanted {med3 * 1000:+.0f} ms, "
                                f"moved {step / RATE * 1000:+.0f} ms")
                        st.pos += step
                        st.shift += step
                        st.moved_s += step / RATE
                        st.slew_s = 0.0       # jump supersedes any pending slew
                        if st.resampler is not None:
                            st.resampler.reset()
                        st.resyncs += 1
                        st.cooldown_until = time.monotonic() + STREAM_COOLDOWN_S
                        st.err_hist = []
                        st.lag_hist = []      # device-buffer transient follows
                        st.fit_lost_at = time.monotonic()
                        batch.append(self._sample_row(st, "resync", lag=lag,
                                                      error=med3))
                        logger.info(f"Sync stream resync {st.name}: "
                                    f"{med3 * 1000:+.0f} ms")
                    elif abs(st.slew_s) > STREAM_SLEW_FAST_THRESH_S \
                            or len(st.err_hist) < 2:
                        self._pll_update(st, lag)
                    elif abs(med3) > STREAM_SLEW_FAST_THRESH_S:
                        fresh = abs(med3 - st.slew_s) > 0.010
                        # Ceiling keeps the jump rung armed (§A.4).
                        st.slew_s = max(-STREAM_SLEW_MAX_S,
                                        min(STREAM_SLEW_MAX_S, med3))
                        if fresh:
                            batch.append(self._sample_row(st, "slew", lag=lag,
                                                          error=error))
                            logger.info(f"Sync stream slew {st.name}: "
                                        f"{st.slew_s * 1000:+.0f} ms @ "
                                        f"{STREAM_SLEW_FAST_PPM:.0f} ppm"
                                        + (f" (of {med3 * 1000:+.0f} ms —"
                                           " excess left for resync)"
                                           if abs(med3) > STREAM_SLEW_MAX_S
                                           else ""))
                        self._pll_update(st, lag)
                    else:
                        st.slew_s = med3
                        self._pll_update(st, lag)
                    st.stats["slew_ms"] = round(st.slew_s * 1000, 1)
                await self._record_samples(batch)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Sync stream monitor died: {e}")

    async def _spectrum_feed(self) -> None:
        """Broadcast the zone's live spectrum for the EQ display."""
        n = SPECTRUM_FFT_N
        win = np.hanning(n).astype(np.float32)
        edges = SPECTRUM_F_LO * (SPECTRUM_F_HI / SPECTRUM_F_LO) ** (
            np.arange(SPECTRUM_BANDS + 1) / SPECTRUM_BANDS)
        bins = np.clip((edges / (RATE / 2) * (n // 2)).astype(int), 0, n // 2)
        try:
            while self.running:
                await asyncio.sleep(1.0 / SPECTRUM_FPS)
                src = self._source
                if src is None:
                    continue
                if not _ws_clients():
                    continue
                lag = self._target_lag if self._target_lag is not None \
                    else getattr(src, "delay_s", 0.0)
                n0 = int((time.monotonic() - self._epoch - lag) * RATE)
                if n0 < getattr(src, "earliest_sample", lambda: 0)():
                    continue
                block = src.peek(n0, n)
                if block is None or len(block) < n:
                    continue
                mono = block.mean(axis=1) * win
                mag = np.abs(np.fft.rfft(mono))
                out = np.empty(SPECTRUM_BANDS, dtype=np.float32)
                for b in range(SPECTRUM_BANDS):
                    i0 = bins[b]
                    i1 = max(i0 + 1, bins[b + 1])
                    out[b] = mag[i0:i1].max()
                db = 20.0 * np.log10(np.maximum(out * (4.0 / n), 1e-7))
                lvl = np.clip((db - SPECTRUM_FLOOR_DB)
                              / (0.0 - SPECTRUM_FLOOR_DB), 0.0, 1.0)
                await _broadcast_spectrum({
                    "group_id": self._active_group,
                    "session_id": self._session_id,
                    "bands": [int(v) for v in np.round(lvl * 255)],
                    "f_lo": SPECTRUM_F_LO, "f_hi": SPECTRUM_F_HI,
                    "floor_db": SPECTRUM_FLOOR_DB,
                    "peak_db": round(float(db.max()), 1),
                    "eq": bool(getattr(src, "_eq", None) is not None),
                })
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Sync spectrum feed stopped: {e}")

    def _sample_row(self, st: _Stream, kind: str, lag: Optional[float] = None,
                    error: Optional[float] = None) -> dict:
        return {"session_id": self._session_id,
                "player_id": st.player_id, "kind": kind, "lag_s": lag,
                "error_ms": None if error is None else error * 1000,
                "rate_ppm": st.rate_ppm,
                "trim_ms": st.trim_ms,
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
    def _median(xs: List[float]) -> float:
        """True median. ``sorted(xs)[len(xs) // 2]`` is the upper of the two
        middle values on an even-length list, so on two samples it returns the
        MORE POSITIVE one rather than their midpoint — a selector biased toward
        reporting a device as behind, and toward reporting the larger of any
        pair. Every jump ran through that expression."""
        s = sorted(xs)
        n = len(s)
        if not n:
            return 0.0
        return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])

    @staticmethod
    def _fit_slope(pts: List[tuple]) -> Optional[float]:
        """Least-squares slope of (t, y) points; None if degenerate."""
        got = CastSyncPoc._fit_slope_se(pts)
        return None if got is None else got[0]

    @staticmethod
    def _fit_slope_se(pts: List[tuple]) -> Optional[tuple]:
        """Least-squares slope **and its standard error**."""
        n = len(pts)
        if n < 3:
            return None
        tm = sum(p[0] for p in pts) / n
        ym = sum(p[1] for p in pts) / n
        den = sum((p[0] - tm) ** 2 for p in pts)
        if den <= 0:
            return None
        slope = sum((p[0] - tm) * (p[1] - ym) for p in pts) / den
        resid = [p[1] - (ym + slope * (p[0] - tm)) for p in pts]
        s2 = sum(r * r for r in resid) / (n - 2)
        return slope, (s2 / den) ** 0.5

    def _pll_update(self, st: _Stream, lag: float):
        """Drift estimator (open-zone.md §7.2): fit the slope of the device's
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
            if st.fit_lost_at and (time.monotonic() - st.fit_lost_at
                                   > STREAM_RATE_STALE_S):
                st.rate_ppm += STREAM_RATE_DECAY * (st.rate_prior - st.rate_ppm)
                st.stats["drift_fit"] = "stale — decaying to prior"
            return
        st.fit_lost_at = 0.0
        slope = self._fit_slope(st.lag_hist)
        if slope is None:
            return
        n = len(st.lag_hist)
        tm = sum(p[0] for p in st.lag_hist) / n
        ym = sum(p[1] for p in st.lag_hist) / n
        resid = [p[1] - (ym + slope * (p[0] - tm)) for p in st.lag_hist]
        sd = (sum(r * r for r in resid) / n) ** 0.5
        fit = self._fit_slope_se(st.lag_hist)
        if sd > 0:
            kept = [p for p, r in zip(st.lag_hist, resid) if abs(r) <= 3 * sd]
            if STREAM_FIT_MIN_POINTS <= len(kept) < n:
                fit = self._fit_slope_se(kept)
        if fit is None:
            return
        slope, se = fit
        if abs(slope) * 1e6 > 4 * STREAM_RATE_MAX_PPM:
            st.lag_hist = st.lag_hist[-1:]
            st.fit_lost_at = time.monotonic()
            return
        if se <= 0 or abs(slope) < STREAM_FIT_MIN_SIGMA * se:
            st.stats["drift_fit"] = "below noise"
            return
        confidence = min(1.0, abs(slope) / se - STREAM_FIT_MIN_SIGMA)
        st.stats["drift_fit"] = f"{slope * 1e6:+.1f}±{se * 1e6:.1f} ppm"
        w = confidence * min(1.0, span / STREAM_RATE_EMA_SPAN_S)
        blended = w * slope * 1e6 + (1.0 - w) * st.rate_prior
        if abs(blended) >= STREAM_RATE_MAX_PPM:
            st.rate_ppm = st.rate_prior
            st.lag_hist = st.lag_hist[-1:]
            st.fit_lost_at = time.monotonic()
            st.stats["drift_fit"] = f"rejected {blended:+.0f} ppm (at bound)"
            return
        st.rate_ppm = blended
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
            lu = getattr(status, "last_updated", None)
            if lu is not None:
                try:
                    ref = datetime.now(lu.tzinfo) if lu.tzinfo else datetime.now()
                    age = (ref - lu).total_seconds()
                except Exception:
                    age = 0.0
                if age > STREAM_STATUS_MAX_AGE_S or age < -1.0:
                    st.stats["stale_reads"] = st.stats.get("stale_reads", 0) + 1
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
        trim_s = st.trim_ms / 1000.0
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
        return self._median(reads)

    def _group_locked(self) -> bool:
        """Every connected device has been measured and pulled into place."""
        live = [s for s in self._streams.values() if s.connected]
        return bool(live) and all(s.acquired for s in live)

    def _acquire_gain(self, frames: int):
        """Output gain for one block: None means unity (the common case)."""
        if self._fade_start is None:
            elapsed = time.monotonic() - self._epoch
            if self._group_locked() or elapsed > STREAM_ACQUIRE_MAX_S:
                self._fade_start = time.monotonic()
                logger.info(
                    f"Sync group locked after {elapsed:.1f}s — fading in"
                    f"{'' if self._group_locked() else ' (acquisition timed out)'}")
            else:
                return np.zeros(frames, dtype=np.float32)
        t0 = time.monotonic() - self._fade_start
        if t0 >= STREAM_FADE_IN_S:
            return None
        ramp = (t0 + np.arange(frames) / RATE) / STREAM_FADE_IN_S
        return np.clip(ramp, 0.0, 1.0).astype(np.float32)

    async def _pcm_stream(self, st: _Stream):
        """Async generator: endless WAV cut from the shared timeline for one
        device, paced to stay at most STREAM_AHEAD_S ahead of real time."""
        st.gen += 1
        mine = st.gen
        st.connected = True
        st.cooldown_until = max(st.cooldown_until,
                                time.monotonic() + STREAM_CONNECT_GRACE_S)
        logger.info(f"Sync stream opened: {st.name}"
                    f"{f' (fetch #{mine}, superseding #{mine - 1})' if mine > 1 else ''}")
        try:
            yield _wav_header()
            source = self._source
            delay = source.delay_s
            if st.pos is None:
                trim = int(st.trim_ms * RATE / 1000)
                precomp = int(st.precomp_s * RATE)
                st.pos = (int((time.monotonic() - self._epoch - delay) * RATE)
                          - trim - precomp)
                floor = source.earliest_sample() + _rs.READ_MARGIN
                if st.pos < floor:
                    logger.warning(
                        f"Sync stream {st.name} clamped {(floor - st.pos) / RATE:.2f}s "
                        f"forward — delay line was short at launch")
                    st.pos = floor
                st.start_pos = st.pos
            block = int(RATE * STREAM_BLOCK_S)
            while (self.running and self._streams.get(st.sid) is st
                   and st.gen == mine):
                ahead = ((st.pos - st.shift) / RATE + delay
                         - (time.monotonic() - self._epoch))
                if ahead > STREAM_AHEAD_S:
                    await asyncio.sleep(STREAM_BLOCK_S / 2)
                    continue
                rm = 0.0
                if st.slew_s:
                    ppm = (STREAM_SLEW_FAST_PPM
                           if abs(st.slew_s) > STREAM_SLEW_FAST_THRESH_S
                           else STREAM_SLEW_GENTLE_PPM)
                    lim = (block / RATE) * ppm / 1e6
                    rm = max(-lim, min(lim, st.slew_s))
                adv = block * (1.0 + st.rate_ppm / 1e6) + rm * RATE
                # Window on the loop, filter off it (open-zone.md §A.1).
                pos0 = st.pos
                win = st.resampler.window(source, st.pos, block, adv, st.chirp)
                out, used = await asyncio.to_thread(st.resampler.render, win)
                if st.pos != pos0:
                    continue     # jump landed mid-render; drop, consume nothing
                st.slew_s -= rm
                st.pos += used
                st.shift += used - block
                st.moved_s += (used - block) / RATE   # decompensate drift fit
                gain = self._acquire_gain(block)
                if gain is not None:
                    out = out * gain[:, None]
                yield _encode_s16(out)
        except asyncio.CancelledError:
            pass
        finally:
            if st.gen == mine:
                st.connected = False
            logger.info(f"Sync stream closed: {st.name}"
                        f"{'' if st.gen == mine else f' (superseded fetch #{mine})'}")

    # ------------------------------------------------------------------
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
                pcm = await asyncio.to_thread(_chunk_pcm, self._source, i)
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
                            "trim_ms": self.trim_ms(info["player_id"]),
                        })
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
