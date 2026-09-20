"""
OpenZone — synchronised multi-speaker casting.
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

from modules.media import latency_seed as _seed
from modules.media.trim_graph import TrimGraph
from modules.media import sync_align as _align
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
# Ceiling on the observation latency one lag reading may buy itself: the
# cooldown scales with the device's lag, so a bogus reading would otherwise
# blind the ladder for as long as it was wrong (§7.1).
STREAM_COOLDOWN_MAX_S = 45.0
STREAM_CONNECT_GRACE_S = 4.0     # ignore polls this long after a stream
STREAM_RECONNECT_GRACE_S = 15.0
# Floor between forced re-LOADs of one receiver: a LOAD costs its buffer and
# seconds of re-acquisition, so it must not be reachable in a tight loop.
STREAM_RELOAD_MIN_INTERVAL_S = 30.0
# Reloads of one device before the group is re-aligned instead. A reload
# converges only inside `target_lag - (delay_s + trim + precomp)`, so past a
# couple of attempts it cannot work rather than merely being slow (§7.1).
STREAM_RELOADS_BEFORE_REALIGN = 2
# A re-align re-LOADs every receiver, costing the whole zone several seconds.
STREAM_REALIGN_MIN_INTERVAL_S = 180.0
# A step corrects by the error it measured, so the next error should be a
# fraction of it. Still this large a cooldown later means the reader moved and
# the device did not follow (§7.1).
STREAM_STEP_FUTILE_FRACTION = 0.5
# Consecutive futile steps before escalating past the step rung. Each is an
# audible discontinuity, so two is a demonstration rather than a sample.
STREAM_FUTILE_STEPS = 2
# No playable media time for this long = out of the correction loop's reach
# (§7.1). Must exceed a re-LOAD's re-acquisition, stamped as a fresh reading.
STREAM_SILENT_MAX_S = 30.0
STREAM_CLAMP_LOG_EVERY_S = 10.0  # a clamped step now re-decides every poll
STREAM_JUMP_MIN_S = 0.10         # hard resync only beyond this
# Kept clear of the write head when seating or stepping: the slack
# sync_source._gap_close leaves above the furthest reader, plus the block it is
# about to serve. The bare head is the one seat a reader must never get (§A.2).
STREAM_HEAD_GUARD_S = _src.XFADE_READER_MARGIN_S + STREAM_BLOCK_S
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
# An interruption leaves the receiver's buffer intact, so it resumes exactly as
# far behind as it was held — an offset larger than the reader can step away.
# Read off player_state rather than inferred from silence (§7.1).
STREAM_INTERRUPT_MIN_S = 1.0
# Floor between interruption-driven reloads of one device: a receiver held down
# longer than one attempt is retried steadily rather than hammered.
STREAM_INTERRUPT_RELOAD_MIN_S = 10.0
# How often a parked device is probed for its return. Frequent enough to catch
# a multi-minute outage ending, and cheap: resolution is one mDNS lookup (§7.1).
STREAM_PARK_RETRY_S = 30.0

# --- Pre-roll latency probe (open-zone.md §7.3) ----------------------------
# Every device is LOADed onto a silent lead as the source opens and its
# pipeline latency read off that lead while the delay line fills. The two costs
# are independent, so acquisition is free inside the prime and content starts
# already aligned instead of being corrected in front of the listener.
PREROLL_POLL_S = 0.5
PREROLL_READS = 7            # median window: accuracy comes from here
PREROLL_MIN_READS = 4        # ...stability from this many inside the band
PREROLL_SETTLE_S = 0.100     # spread that reads as "the buffer has stopped
                             # filling" — a stability gate, not the accuracy
                             # of the answer, which the median supplies
PREROLL_QUORUM_S = 8.0       # after this, start on the devices that answered
PREROLL_MAX_S = 20.0         # never hold content longer than this
# Cap on the lead a reload opens to re-measure one device (open-zone.md §7.5).
# The wait is that speaker playing silence; past it the model is the answer.
STREAM_PROBE_MAX_S = 15.0
# Total silence a re-align may cost the zone — the lead *and* any acquisition
# after it, which is why it replaces STREAM_ACQUIRE_MAX_S rather than adding to
# it. Session start's budgets are the wrong shape here: nobody is listening
# when a session opens, where this lands mid-track. Sized to still measure the
# slowest device in the reference deployment (§3: ~6.3 s of pipeline, plus the
# settling window and a LOAD round trip) — past it the model is the answer.
STREAM_REALIGN_MAX_S = 12.0

SPECTRUM_FFT_N = 2048
SPECTRUM_BANDS = 48
SPECTRUM_FPS = 15                # display cadence; see _spectrum_feed on cost
SPECTRUM_F_LO = 25.0
SPECTRUM_F_HI = 18000.0
SPECTRUM_FLOOR_DB = -72.0

# The media block a session start accepts (SyncMediaBody). Named here because
# a zone stores one, and stored config must round-trip through the same
# vocabulary the start path reads. `items`/`start_index` are excluded on
# purpose — an explicit queue is one act of playback, not a standing choice.
MEDIA_FIELDS = ("url", "station_uuid", "source_id", "media_type", "kind",
                "title", "artwork_url", "artist", "loop", "owner")


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
    # An explicit list has no id of its own — without it, two different queues
    # collide and the second start is swallowed as a duplicate.
    items = tuple(str(r.get("source_id") or "") for r in (m.get("items") or []))
    return (str(m.get("kind") or "track").lower(),
            m.get("media_type") or "", m.get("source_id") or "",
            m.get("station_uuid") or "", (m.get("url") or "").strip(), items)


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
        self.reloads: int = 0        # receiver re-LOADs forced this session
        # Reloads since this device was last aligned, separate from the session
        # counter the UI reports: the ladder asks whether reloading still works
        # against the *current* target, which a re-align resets.
        self.reloads_since_align: int = 0
        # None, not 0.0: these gate a rung on elapsed time, and 0.0 reads as
        # "long ago" only because time.monotonic() is usually large. On a hub
        # inside its first minutes of uptime that suppresses the session's
        # first escalation, which is the one that matters (§7.1).
        self.last_reload: Optional[float] = None
        self.clamp_logged: float = 0.0     # rate-limit for the "not moved" warn
        # Last usable media time. A receiver out of PLAYING reports nothing,
        # which is silence the ladder cannot read as an error (_sweep_silent).
        self.last_lag_at: Optional[float] = None   # None = never seated
        # Interruption sensing: ``state`` is the last player_state seen,
        # ``interrupted_since`` is set when it stops being PLAYING and cleared
        # into ``interrupt_held`` when it returns, so the sweep sees both a
        # running interruption and one that ended between polls.
        # Parked is distinct from both: the provider cannot resolve the device
        # at all, so it is out of the group entirely (_park_stream).
        self.parked_since: Optional[float] = None
        self.park_probe_at: float = 0.0    # next resolution attempt
        self.park_probing: bool = False    # one probe in flight at a time
        self.parks: int = 0
        self.state: str = ""
        self.interrupted_since: Optional[float] = None
        self.interrupt_held: float = 0.0
        self.interrupts: int = 0
        self.last_interrupt_reload: Optional[float] = None
        # Pre-roll probe. ``opened_at`` is when this fetch began serving, which
        # is the origin the device's reported media time counts from, so
        # (now - opened_at) - ct is its whole pipeline latency.
        self.opened_at: Optional[float] = None
        self.preroll_frames: int = 0
        self.probe_hist: List[float] = []
        self.latency_s: float = 0.0
        # On a silent lead alone, being re-measured after a reload
        # (open-zone.md §7.5). Separate from OpenZone._preroll, which is the
        # whole zone. While set the generator serves the lead and the monitor
        # skips the device: every lag it could read describes the lead.
        self.probing: bool = False
        self.probe_task = None
        self.futile_steps: int = 0         # consecutive steps that did not take
        self.last_step_error: Optional[float] = None
        self.learn_lag: bool = True  # False once a re-align re-measured this
                                     # device: the lag it shows after a forced
                                     # LOAD is a re-acquisition figure, not the
                                     # device's natural startup latency, and
                                     # writing it to the model would seed every
                                     # future session with it (§7.2)
        self.stats: dict = {}
        self.rate_ppm: float = 0.0   # >0 = device clock slow, serve faster
        self.rate_prior: float = 0.0
        self.slew_s: float = 0.0     # pending offset to slew away (s, >0 =
        self.moved_s: float = 0.0
        self.err_hist: List[float] = []   # last 3 poll errors (median filter)
        self.acquired: bool = False
        self.inject: Optional[tuple] = None   # (timeline sample, wave)
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


class OpenZone:
    def __init__(self, cast_provider, cfg: dict):
        cfg = cfg or {}
        self.cast = cast_provider                      # CastPlayerProvider
        # (player_id) -> PlayerProvider, so model identity can be asked of
        # whichever ecosystem owns the device rather than only of Cast. Unset
        # falls back to the Cast provider (_model_key).
        self._provider_resolver = None
        self.http_port = int(cfg.get("http_port", 8010))
        self.app_id = (cfg.get("app_id") or "").strip()
        self._trims_file = cfg.get("trims_file", "./data/cast_sync_trims.json")
        self._trims: Dict[str, int] = {
            k: int(v) for k, v in self._read_json(self._trims_file).items()}
        self._model_trims_file = cfg.get("model_trims_file",
                                         "./data/cast_sync_model_trims.json")
        self._model_trims: Dict[str, int] = {
            k: int(v) for k, v in self._read_json(self._model_trims_file).items()}
        self._model_trims_reconciled = False
        self._graph_file = cfg.get("trim_graph_file",
                                   "./data/cast_sync_trim_graph.json")
        self._graph = TrimGraph(
            lambda: self._read_json(self._graph_file),
            lambda d: self._write_json(self._graph_file, d))
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
        self._aligning: Optional[dict] = None      # by-ear session (§7.7)
        self._mic_cache: Optional[Tuple[float, dict]] = None

        self._source = _GENERATED
        self._resampler_kind = str(cfg.get("resampler", "rust"))
        # The delay line's depth ahead of the play point. The serve loop paces
        # readers `STREAM_AHEAD_S` behind it, so the ladder's whole forward step
        # authority is `source_delay_s - STREAM_AHEAD_S` — 2.8 s here, enough to
        # step away a loop stall of the size actually observed (§7.1). Costs 2 s
        # of group latency (music only, §10.2) and a longer prime. It does NOT
        # widen what a reload can fix: target_lag grows with delay_s, so the
        # term cancels out of that budget (_escalate_shortfall).
        self._source_delay_s = float(cfg.get("source_delay_s", 4.0))
        # Must span the delay plus the widest startup pre-compensation (§4.1),
        # ~9.8 s for a five-device group at the delay above.
        self._ring_capacity_s = float(cfg.get("ring_capacity_s", 20.0))
        self._crossfade_s = float(cfg.get("crossfade_s", 0.0))
        self._session_crossfade_s = self._crossfade_s
        self._eq_engine = None
        # async (media_type, source_id, owner) -> url. `owner` names the user
        # whose source account it resolves against; a zone outlives the request
        # that started it, so the row carries it rather than a principal.
        self._url_resolver = None
        self._queue_resolver = None    # async (media_type, kind, id, owner) -> [items]
        self._queue: List[dict] = []
        self._queue_pos = 0
        self._trim_learn_tasks: Dict[str, asyncio.Task] = {}
        self._fade_start: Optional[float] = None

        self._http_server = None                       # uvicorn.Server
        self._http_task: Optional[asyncio.Task] = None
        self._producer: Optional[asyncio.Task] = None
        # Last now-playing pushed to the custom receivers, as (queue position,
        # timeline origin). The origin is in the key because an item can repeat
        # at the same position — a one-track loop is the same row starting
        # again, and its screens should say so.
        self._now_key: Optional[tuple] = None
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
        self._realigning: bool = False                 # _realign_group in flight
        self._last_realign: Optional[float] = None   # see _Stream.last_reload
        # Bounds on deriving _target_lag: nothing before _target_wait_until,
        # and after _acquire_deadline take whatever has reported. Both are set
        # at session start and again by _realign_group.
        self._target_wait_until: float = 0.0
        self._acquire_deadline: float = 0.0
        # Pre-roll: every reader is unseated and every device is playing a
        # silent lead while the delay line fills (_preroll_probe).
        self._preroll: bool = False
        self._preroll_task: Optional[asyncio.Task] = None
        self._preroll_target_s: float = 0.0
        # The pre-roll is a re-align's, not a session start's: the delay line is
        # already full, the readings must not teach the model, and readers hold
        # a stale seat rather than none (_realign_group).
        self._preroll_realign: bool = False
        # Origin of the acquisition budget. Not the epoch: a pre-roll that did
        # its job would have spent half of that budget before content started.
        self._acquire_from: float = 0.0
        # How long that budget runs. A re-align narrows it and starts it at the
        # re-align rather than at the end of the lead, so the two phases share
        # one bound instead of each getting its own (_realign_group).
        self._acquire_max_s: float = STREAM_ACQUIRE_MAX_S

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
        logger.info(f"OpenZone HTTP listener on :{self.http_port} "
                    f"(receiver at /cast/sync_receiver.html)")

    def stop(self):
        if self.running:
            asyncio.ensure_future(self.stop_session())
        if self._http_server is not None:
            self._http_server.should_exit = True
        self._http_task = None
        if _sdb is not None:
            _sdb.close_all()

    def set_eq_engine(self, engine) -> None:
        self._eq_engine = engine

    def set_url_resolver(self, fn) -> None:
        """``async (media_type, source_id, owner) -> url`` for sources whose URLs
        expire. Wired by MediaService; absent means URL-only sources."""
        self._url_resolver = fn

    def set_queue_resolver(self, fn) -> None:
        """``async (media_type, kind, id) -> [MediaItem-ish dicts]``, which is
        what makes a Tidal album, playlist, mix or artist playable to a zone
        rather than just a single track. Wired by MediaService."""
        self._queue_resolver = fn

    def now_playing(self) -> dict:
        """The item a zone is currently on, for the UI and the Cast display.
        ``position_ms`` is where the group *hears*, not where the decoder is,
        so it is comparable with any other player's."""
        q, i = self._queue, self._queue_pos
        item = q[i] if 0 <= i < len(q) else {}
        duration_ms = int(item.get("duration_ms") or 0)
        pos_s = self._source.item_position_s() if self.running else None
        position_ms = int(max(0.0, pos_s) * 1000) if pos_s is not None else 0
        if duration_ms:
            position_ms = min(position_ms, duration_ms)
        return {"title": item.get("title", ""), "artist": item.get("artist", ""),
                "artwork_url": item.get("artwork_url", ""),
                "source_id": item.get("source_id", ""),
                "media_type": (item.get("media_type")
                               or (self._session_media or {}).get("media_type", "")),
                "duration_ms": duration_ms, "position_ms": position_ms,
                "index": i if q else 0, "count": len(q)}

    async def skip_to(self, index: int) -> dict:
        """Move the zone's queue to ``index`` and re-cut the timeline there.
        The seam lands at the write head, so it is heard once the delay line
        drains — open-zone.md §4.1b."""
        if not self.running:
            return {"success": False, "error": "This zone is not playing"}
        n = len(self._queue)
        if not n:
            return {"success": False, "error": "This zone has no queue to move through"}
        if not 0 <= index < n:
            return {"success": False,
                    "error": f"No item {index + 1} in a queue of {n}"}
        if index == self._queue_pos:
            return {"success": True, "index": index, "unchanged": True}
        previous, self._queue_pos = self._queue_pos, index
        if not self._source.skip():
            self._queue_pos = previous
            return {"success": False,
                    "error": "This zone's source cannot be skipped"}
        logger.info(f"Zone queue moved {previous + 1} → {index + 1} of {n}")
        return {"success": True, "index": index}

    def set_provider_resolver(self, resolver) -> None:
        """Supply ``(player_id) -> PlayerProvider`` so model identity, and the
        trim defaults keyed on it, work for every ecosystem in a zone."""
        self._provider_resolver = resolver

    def _provider_for(self, player_id: str) -> object:
        resolve = self._provider_resolver
        if resolve is not None:
            try:
                prov = resolve(player_id)
                if prov is not None:
                    return prov
            except Exception:
                pass
        return getattr(self, "cast", None)

    def _model_key(self, player_id: str) -> str:
        try:
            get = getattr(self._provider_for(player_id), "model_key", None)
            return get(player_id) if callable(get) else ""
        except Exception:
            return ""

    def _reconcile_model_trims(self) -> None:
        """Fold legacy ``cast_type/model`` model-trim keys onto the model name.

        The key used to prefix cast_type, which mDNS reports inconsistently, so
        one physical device could write under two keys (open-zone.md §7.4).
        Runs once per process at session start, not at load: the tiebreak needs
        discovery up.

        One legacy value carries over. Several: an explicit per-device trim on
        a unit of that model decides, being a value the listener set
        deliberately. Failing that, values agreeing within
        ``TRIM_MODEL_AGREE_MS`` collapse to their median and the rest are
        dropped — contradictory evidence is the case §7.4 abandons the default
        for. Explicit per-device trims are untouched either way."""
        if self._model_trims_reconciled:
            return
        self._model_trims_reconciled = True
        if not any("/" in k for k in self._model_trims):
            return
        explicit: Dict[str, List[int]] = {}
        for pid, val in self._trims.items():
            key = self._model_key(pid)
            if key:
                explicit.setdefault(key, []).append(int(val))

        folded: Dict[str, List[int]] = {}
        for key, val in self._model_trims.items():
            folded.setdefault(key.split("/", 1)[-1].strip() or key,
                              []).append(int(val))

        out: Dict[str, int] = {}
        for model, vals in folded.items():
            uniq = sorted(set(vals))
            if len(uniq) == 1:
                out[model] = uniq[0]
                continue
            owned = explicit.get(model) or []
            match = [v for v in uniq
                     if any(abs(v - e) <= TRIM_MODEL_AGREE_MS for e in owned)]
            if len(set(match)) == 1:
                out[model] = match[0]
                logger.info(
                    f"Model trim for '{model}' resolved to {match[0]:+d} ms "
                    f"from {', '.join(f'{v:+d}' for v in uniq)} — the value a "
                    f"unit of this model is explicitly trimmed to")
            elif max(uniq) - min(uniq) <= TRIM_MODEL_AGREE_MS:
                out[model] = int(round(self._median([float(v) for v in uniq])))
            else:
                logger.warning(
                    f"Model trim for '{model}' dropped: the store held "
                    f"{', '.join(f'{v:+d}' for v in uniq)} ms under different "
                    f"cast_type keys for one model, and nothing decides between "
                    f"them — per-device trims are unaffected")
        if out != self._model_trims:
            self._model_trims = out
            self._write_json(self._model_trims_file, out)
            self._graph.invalidate()

    def trim_ms(self, player_id: str) -> int:
        """Effective trim: an explicit per-device value, else whatever this
        model has been found to need on this network, else the shipped prior
        for the model, else nothing.

        Direct beats derived beats shipped. The prior stays reachable after
        ``_learn_model_trim`` drops a disputed model: that dispute is between
        units each carrying an explicit trim, which this never sees, while an
        untrimmed unit still has the hardware constant to start from.
        """
        if player_id in self._trims:
            return int(self._trims[player_id])
        key = self._model_key(player_id)
        if not key:
            return 0
        learned = self._model_trims.get(key)
        if learned is not None:
            return int(learned)
        derived = self._solve_graph().get(key)
        if derived is not None:
            return int(derived)
        return _seed.trim_ms(key)

    def _solve_graph(self) -> Dict[str, int]:
        """Absolutes the trim graph implies, pinned to what this network has
        learned and, failing that, to the shipped table (open-zone.md §7.6)."""
        priors = {m: v for m in self._graph.models()
                  if m not in self._model_trims and (v := _seed.trim_ms(m))}
        return self._graph.solve(dict(self._model_trims), priors)

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
            # Explicit trims only: an effective one the graph supplied would
            # feed its own output back in (trim_graph.observe_session).
            self._graph.observe_session(
                [(self._model_key(pid), int(self._trims[pid]))
                 for pid in self._session_players if pid in self._trims])
            peers = [int(v) for pid, v in self._trims.items()
                     if pid != player_id and self._model_key(pid) == key]
            disputed = [v for v in peers if abs(v - trim_ms) > TRIM_MODEL_AGREE_MS]
            if disputed:
                if self._model_trims.pop(key, None) is not None:
                    self._write_json(self._model_trims_file, self._model_trims)
                    self._graph.invalidate()
                logger.info(
                    f"Model trim for '{key}' dropped: units disagree "
                    f"({trim_ms:+d} ms vs {', '.join(f'{v:+d}' for v in disputed)} ms) "
                    f"— this trim is positional, not a property of the hardware")
                return
            if self._model_trims.get(key) == trim_ms:
                return
            self._model_trims[key] = int(trim_ms)
            self._write_json(self._model_trims_file, self._model_trims)
            self._graph.invalidate()   # graph unchanged, its pinning is not
            logger.info(f"Learned trim {trim_ms:+d} ms for model '{key}' — new "
                        f"devices of this model will start pre-aligned")

        self._trim_learn_tasks[key] = asyncio.create_task(_settle())

    def session_snapshot(self) -> dict:
        """What it would take to stand this session back up, or {} when idle."""
        if not self.running or not self._streams:
            return {}
        media = self._session_media
        if media and media.get("items"):
            # Resume where the queue got to, not where it was told to start.
            media = {**media, "start_index": self._queue_pos}
        snap = {
            "group_id": self._active_group,
            "player_ids": [st.player_id for st in self._streams.values()],
            "media": media,
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
        # Whose source account this block plays on. A row may name its own —
        # a queue can mix accounts — and falls back to the block's.
        owner = media.get("owner") or ""
        kind = (media.get("kind") or "track").strip() or "track"
        loop_forever = bool(media.get("loop"))
        rows = media.get("items") or []
        if (sid or rows) and self._url_resolver is not None:
            if rows:
                # Order settled by the caller (shuffle/repeat already applied).
                self._queue = [dict(r) for r in rows
                               if r.get("source_id") or r.get("url")]
                if not self._queue:
                    return None, "that queue has no playable items"
                start = int(media.get("start_index") or 0)
                self._queue_pos = start if 0 <= start < len(self._queue) else 0
            elif kind != "track" and self._queue_resolver is not None:
                try:
                    self._queue = list(await self._queue_resolver(
                        stype, kind, sid, owner))
                except Exception as e:
                    return None, f"could not open {kind}: {e}"
                if not self._queue:
                    return None, (f"that {kind} is empty, unavailable, or needs "
                                  f"a signed-in {stype or 'source'} account")
            else:
                self._queue = [{"source_id": sid,
                                "title": (media.get("title") or "").strip(),
                                "artwork_url": media.get("artwork_url") or "",
                                "owner": owner}]

            async def provider(last_rc=None):
                if last_rc == 0:
                    self._queue_pos += 1
                    if self._queue_pos >= len(self._queue):
                        if not loop_forever:
                            return ""
                        self._queue_pos = 0
                item = self._queue[self._queue_pos]
                # Mixed queues: an item's own media_type wins over the block's.
                sub = item.get("source_id") or ""
                url = (await self._url_resolver(item.get("media_type") or stype,
                                                sub, item.get("owner") or owner)
                       if sub else (item.get("url") or ""))
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
            media["title"] = (media["title"]
                              or self._queue[self._queue_pos].get("title") or "")
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

    def _reader_ceiling(self, source) -> float:
        """The furthest a reader may be seated or stepped, in timeline samples.
        Two bounds, tighter wins (open-zone.md §A.2).

        **The write head, less STREAM_HEAD_GUARD_S.** A reader on the bare head
        overtakes it every block, and ``sync_source._gap_close`` covers the
        overtake with silence in the timeline the *whole zone* reads — a latch
        the ladder cannot even see, since the device's buffer absorbs the step
        and it goes on reporting an on-target lag.

        **The play point.** A reader past it has no decoded headroom left, so
        the next decoder hiccup puts it past the head regardless of the guard.
        """
        play_now = (time.monotonic() - self._epoch) * RATE
        _latest = getattr(source, "latest_sample", None)
        if not callable(_latest):
            return play_now
        return min(play_now,
                   _latest() - _rs.READ_MARGIN - RATE * STREAM_HEAD_GUARD_S)

    def _step_cooldown_s(self, lag: Optional[float] = None) -> float:
        """How long to ignore a device's polls after moving its reader
        (open-zone.md §7.1).

        The device reveals a step only after draining what it had buffered, so
        a shorter cooldown leaves pre-step readings in the median window and
        they re-authorise the step that produced them. What it holds is *its
        own* lag — a device the ladder moves is behind by `error` and so holds
        `target + error` — hence the reading that authorised the move, not the
        group target. Capped so one wrong reading cannot blind the ladder for
        as long as it was wrong.
        """
        observed = max(self._target_lag or 0.0, lag or 0.0)
        return min(STREAM_COOLDOWN_MAX_S,
                   max(STREAM_COOLDOWN_S, observed + STREAM_POLL_S))

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

        if stream_mode:
            # Needs discovery, so it cannot run at construction; once per
            # process, before any trim is latched into a stream.
            self._reconcile_model_trims()

        if stream_mode and _sdb is not None:
            try:
                db_model = await asyncio.to_thread(_sdb.query_device_model)
                for pid, m in db_model.items():
                    self._model[pid] = {**self._model.get(pid, {}), **m}
            except Exception as e:
                logger.debug(f"Sync model DB load failed (using JSON model): {e}")

        self._epoch = time.monotonic()
        self._buffer = []
        self._now_key = None
        self._receivers = {}
        self._streams = {}
        self._pending = {}
        self._target_lag = None
        self._realigning = False
        self._last_realign = None
        self._target_wait_until = 0.0
        self._acquire_deadline = self._epoch + 25
        self._acquire_from = self._epoch
        self._acquire_max_s = STREAM_ACQUIRE_MAX_S   # not a re-align's budget
        self._preroll = False
        self._preroll_realign = False
        self._fade_start = None      # every session re-acquires under silence
        source, err = await self._build_source(media, group_id)
        if source is None:
            self._active_group = ""
            return {"success": False, "error": f"Could not open media: {err}"}
        self._source = source
        self._session_media = media or None
        self.running = True
        self._session_started_at = time.monotonic()

        model_target = None
        if stream_mode:
            model_lags = {pid: self._model.get(pid, {}).get("lag_s")
                          for pid in player_ids}
            if all(v is not None for v in model_lags.values()):
                model_target = max(model_lags.values()) + STREAM_LAG_MARGIN_S
            max_precomp = max((max(0.0, (model_target or 0.0) - (lag or 0.0))
                               for lag in model_lags.values()), default=0.0)
            # The probe's own readings from previous sessions size the lead the
            # delay line must hold before content can start: they measure the
            # same spread, without the target lag's common term. Only a seed —
            # the pre-roll re-measures it before anything is seated against it.
            probes = [self._model.get(pid, {}).get("probe_s")
                      for pid in player_ids]
            if probes and all(v is not None for v in probes):
                max_precomp = max(max_precomp, max(probes) - min(probes))
            # No blocking prime: devices are LOADed onto a silent lead and the
            # delay line fills underneath them (_preroll_probe). The target lag
            # is left unset deliberately — the monitor derives it from the lags
            # the *measured* pre-comp produces, which is the derivation that
            # converges; a model target fixes it to a previous session's
            # numbers and makes every device chase a common offset.
            self._preroll = True
            self._preroll_target_s = self._source.delay_s + max_precomp
        else:
            await self._prime_source(0.0)
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
                    # A fallback only: the pre-roll overwrites this with what
                    # it measures, and reaches _end_preroll's else-branch — so
                    # keeps this — only if no device reported at all.
                    if model_target is not None and m.get("lag_s") is not None:
                        st.precomp_s = max(0.0, model_target - m["lag_s"])
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
            self._preroll_task = asyncio.create_task(self._preroll_probe())
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
        # Cleared before the task is cancelled so _end_preroll, which runs in
        # its finally, sees a session that is already over and touches nothing.
        self._preroll = False
        self._preroll_realign = False
        if self._preroll_task:
            self._preroll_task.cancel()
            self._preroll_task = None
        # Same order per device: cleared first so the _end_probe in the task's
        # finally has nothing left to release onto.
        for st in self._streams.values():
            st.probing = False
            if st.probe_task is not None:
                st.probe_task.cancel()
                st.probe_task = None
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
                "connected": (r is not None) or (s is not None and s.connected
                                                 and s.parked_since is None),
                "parked": bool(s is not None and s.parked_since is not None),
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
            "trim_graph": {"edges": self._graph.describe(),
                           "derived": self._solve_graph()},
            "align": self.align_status(),
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
            # Reducing a trim walks the reader forward, and the full ±2 s range
            # is enough to walk it into the write head — the same seat that
            # takes the whole zone to silence (_reader_ceiling). Bound it here
            # too; a trim the timeline cannot hold is one the listener has to
            # ask for again, which is cheap, and audible only to them.
            moved = -delta_s * RATE
            ceil = self._reader_ceiling(self._source)
            if s.pos + moved > ceil:
                moved = max(0.0, ceil - s.pos)
                logger.warning(
                    f"Sync stream trim {s.name} clamped to "
                    f"{moved / RATE * 1000:+.0f} ms — no timeline ahead of it")
            s.pos += moved
            s.shift += moved
            s.moved_s += moved / RATE
            if abs(delta_ms) > STREAM_TRIM_QUIET_MS:
                s.cooldown_until = time.monotonic() + self._step_cooldown_s()
                s.err_hist = []   # baseline moved — old medians invalid
            await self._record_samples([self._sample_row(s, "trim")])
        return {"success": True, "player_id": player_id, "trim_ms": trim_ms}

    # Alignment by ear (open-zone.md §7.7)

    def _align_streams(self, reference_id: str, subject_id: str):
        """The two live streams, or an error dict."""
        if not self.running or self.app_id:
            return {"success": False,
                    "error": "Alignment needs a running stream-mode session"}
        if self._calibrating:
            return {"success": False, "error": "Chirp calibration is running"}
        if self._target_lag is None:
            return {"success": False,
                    "error": "Devices still acquiring — try again in a few seconds"}
        if reference_id == subject_id:
            return {"success": False,
                    "error": "Pick two different speakers"}
        found = {}
        for st in self._streams.values():
            if st.player_id in (reference_id, subject_id):
                if st.connected and st.pos is not None and st.parked_since is None:
                    found[st.player_id] = st
        if len(found) < 2:
            return {"success": False,
                    "error": "Both speakers must be connected and playing"}
        return found[reference_id], found[subject_id]

    async def align_start(self, reference_id: str, subject_id: str) -> dict:
        """Begin a by-ear alignment of ``subject`` against ``reference``."""
        picked = self._align_streams(reference_id, subject_id)
        if isinstance(picked, dict):
            return picked
        ref, subj = picked
        self._aligning = {
            "search": _align.Bisection(),
            "ref_sid": ref.sid, "subj_sid": subj.sid,
            "reference": ref.name, "subject": subj.name,
            "subject_id": subject_id,
            "trim0": subj.trim_ms,
            "wave": _align.click_train(RATE),
        }
        logger.info(f"Alignment started: {subj.name} against {ref.name} "
                    f"(from trim {subj.trim_ms:+d} ms)")
        return await self._align_probe()

    async def _align_probe(self) -> dict:
        """Schedule one click train on each device, the subject's delayed by
        the bracket's midpoint, and report what the listener is about to hear."""
        a = self._aligning
        if a is None:
            return {"success": False, "error": "No alignment in progress"}
        ref = self._streams.get(a["ref_sid"])
        subj = self._streams.get(a["subj_sid"])
        if ref is None or subj is None or not (ref.connected and subj.connected):
            self._aligning = None
            return {"success": False, "error": "A speaker left the zone"}
        delta_ms = a["search"].delta_ms
        # Ahead of the furthest reader, with room for a negative delta to still
        # land in front of it.
        head = max((s.pos - s.shift) / RATE for s in (ref, subj))
        at = head + _chirp.CHIRP_LEAD_S + max(0, -delta_ms) / 1000.0
        ceil = self._reader_ceiling(self._source) / RATE
        if at + len(a["wave"]) / RATE > ceil:
            self._aligning = None
            return {"success": False,
                    "error": "No timeline ahead to place the clicks in"}
        ref.inject = (int(at * RATE), a["wave"])
        subj.inject = (int((at + delta_ms / 1000.0) * RATE), a["wave"])
        return {"success": True, **self.align_status()}

    async def align_answer(self, answer: str) -> dict:
        """Take one judgement — ``subject``, ``reference``, ``together`` — or
        ``replay`` to hear the same probe again."""
        a = self._aligning
        if a is None:
            return {"success": False, "error": "No alignment in progress"}
        if answer == "replay":
            return await self._align_probe()
        try:
            a["search"].answer(answer)
        except ValueError:
            return {"success": False, "error": f"Unknown answer '{answer}'"}
        if not a["search"].done:
            return await self._align_probe()
        return await self._align_finish()

    async def _align_finish(self) -> dict:
        a = self._aligning
        search = a["search"]
        delta = search.result()
        for sid in (a["ref_sid"], a["subj_sid"]):
            st = self._streams.get(sid)
            if st is not None:
                st.inject = None
        self._aligning = None
        if delta is None:
            logger.info(
                f"Alignment of {a['subject']} inconclusive after "
                f"{search.rounds} rounds — trim unchanged")
            return {"success": False, "done": True, "applied": False,
                    "rounds": search.rounds,
                    "error": "Answers did not converge — the speakers may be "
                             "too far apart to judge, or too close to separate"}
        trim = max(-2000, min(2000, a["trim0"] + delta))
        logger.info(f"Alignment of {a['subject']} against {a['reference']}: "
                    f"{delta:+d} ms in {search.rounds} rounds "
                    f"→ trim {trim:+d} ms")
        # Feeds _learn_model_trim, and through it the differential graph.
        await self.set_trim(a["subject_id"], trim)
        return {"success": True, "done": True, "applied": True,
                "subject": a["subject"], "reference": a["reference"],
                "rounds": search.rounds, "delta_ms": delta, "trim_ms": trim}

    def align_cancel(self) -> dict:
        a, self._aligning = self._aligning, None
        if a is None:
            return {"success": False, "error": "No alignment in progress"}
        for sid in (a["ref_sid"], a["subj_sid"]):
            st = self._streams.get(sid)
            if st is not None:
                st.inject = None
        logger.info(f"Alignment of {a['subject']} cancelled — trim unchanged")
        return {"success": True, "cancelled": True}

    def align_status(self) -> dict:
        a = self._aligning
        if a is None:
            return {"running": False}
        search = a["search"]
        return {"running": True, "subject": a["subject"],
                "reference": a["reference"], "round": search.rounds + 1,
                "remaining": search.remaining, "delta_ms": search.delta_ms,
                "bracket_ms": [search.lo, search.hi],
                "trim_ms": a["trim0"] + search.delta_ms}

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
                   if s.connected and s.pos is not None
                   and s.parked_since is None]
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
                s.inject = None
            self._calibrating = False

    async def _run_chirp_sequence(self, streams: List[_Stream]) -> dict:
        wave = _chirp.chirp_wave(RATE)
        head_s = max((s.pos - s.shift) / RATE for s in streams)
        plan = []          # (stream, expected arrival in elapsed-seconds)
        for i, s in enumerate(streams):
            slot_s = head_s + _chirp.CHIRP_LEAD_S + i * _chirp.CHIRP_GAP_S
            s.inject = (int(slot_s * RATE), wave)
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
                "play": self.group_config(gid),
            })
        return {"success": True, "groups": groups}

    @property
    def active_group(self) -> str:
        """Group id of the running session, "" when idle or when the session
        was started from a loose list of players."""
        return self._active_group if self.running else ""

    def group_config(self, group_id: str) -> dict:
        """What this zone plays when nobody says otherwise.

        A zone that only ever existed in a browser tab could not be started by
        a schedule or a rule; holding the choice here is what makes the zone a
        thing the server can act on. ``media`` and the two timing fields are
        read by the start path; ``key``, ``custom_url`` and ``loop`` are the
        picker's own memory, stored so any browser opens on the same state.
        None for a timing field means "never chosen" — the server default
        stands, which is not the same as a deliberate 0.
        """
        play = (self._groups.get(group_id) or {}).get("play") or {}
        dur, xf = play.get("duration_s"), play.get("crossfade_s")
        return {
            "key": str(play.get("key") or ""),
            "custom_url": str(play.get("custom_url") or ""),
            "loop": bool(play.get("loop")),
            "media": play.get("media") or None,
            "duration_s": None if dur is None else int(dur),
            "crossfade_s": None if xf is None else float(xf),
        }

    def set_group_config(self, group_id: str, cfg: dict) -> dict:
        """Replace a zone's playback config wholesale. The caller holds the
        whole block already, so a partial merge would only invite the two
        copies to disagree."""
        if group_id not in self._groups:
            return {"success": False, "error": "Unknown sync group"}
        cfg = cfg or {}
        media = cfg.get("media")
        if media is not None:
            if not isinstance(media, dict):
                return {"success": False, "error": "media must be an object"}
            # Only the fields the start path understands, so an old browser
            # cannot park arbitrary keys in the group file.
            media = {k: media[k] for k in MEDIA_FIELDS if k in media}
            if not (media.get("url") or media.get("station_uuid")
                    or media.get("source_id")):
                return {"success": False,
                        "error": "media needs a url, station_uuid or source_id"}
        dur, xf = cfg.get("duration_s"), cfg.get("crossfade_s")
        self._groups[group_id]["play"] = {
            "key": str(cfg.get("key") or "")[:128],
            "custom_url": str(cfg.get("custom_url") or "")[:2048],
            "loop": bool(cfg.get("loop")),
            "media": media,
            "duration_s": (None if dur is None
                           else min(max(int(dur), 0), 3600)),
            "crossfade_s": (None if xf is None
                            else min(max(float(xf), 0.0), self._crossfade_max())),
        }
        self._write_json(self._groups_file, self._groups)
        return {"success": True, "play": self.group_config(group_id)}

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
        existing = self._groups.get(gid) or {}
        self._groups[gid] = {"name": name, "members": members}
        # Renaming a zone or changing its speakers must not silently discard
        # what it plays — that config is edited through its own endpoint.
        if existing.get("play"):
            self._groups[gid]["play"] = existing["play"]
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
            # Resolution failed: the device is not on the network. This is the
            # only signal that separates absence from misalignment, and it is
            # the one every rung above needs, so it parks the device rather
            # than returning quietly and leaving the silence sweep to escalate
            # a fault no reload or re-align can fix (_park_stream).
            st = self._streams.get(sid)
            if st is not None:
                self._park_stream(st, "the cast provider cannot resolve it")
            else:
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
        """(artwork_url, title, artist) for the whole session.

        The default receiver reads this once, off the load that starts each
        device's stream, and there is no way to revise it while that stream
        runs (docs/open-zone.md §10.7) — so this labels the *set*, and the
        caller is expected to have named one. Falling back to the item the
        queue opens on is a last resort, and says how many tracks follow it
        rather than leaving a 40-track playlist looking like one song."""
        m = self._session_media or {}
        title = (m.get("title") or "").strip() or "ZMM OpenZone"
        art = (m.get("artwork_url") or "").strip()
        artist = (m.get("artist") or "").strip()
        if not art and self._queue:
            head = self._queue[self._queue_pos] if \
                0 <= self._queue_pos < len(self._queue) else self._queue[0]
            art = (head.get("artwork_url") or "").strip()
        if len(self._queue) > 1:
            artist = artist or f"{len(self._queue)} tracks"
        return art, title, artist

    def _now_payload(self) -> dict:
        """The item a custom receiver should be showing, and from when.

        ``at`` is a server-clock instant, not a duration: the receiver already
        estimates the server clock to schedule its audio, so a boundary sent
        this way lands on its screen at the moment the seam reaches its
        speakers — not when the decoder crossed it, which is one delay line and
        one buffer earlier. Without an origin (the test signal, or before the
        first item opens) it falls back to "now", which is the best a source
        with no item boundaries can mean.
        """
        np_ = self.now_playing()
        origin = None
        try:
            origin = self._source.item_origin_s()
        except Exception:
            pass
        at = (self._epoch + LEAD_SECONDS + origin) if origin is not None \
            else time.monotonic()
        return {"type": "now", "at": at,
                "title": np_.get("title", ""), "artist": np_.get("artist", ""),
                "artwork": np_.get("artwork_url", ""),
                "duration_ms": int(np_.get("duration_ms") or 0),
                "index": int(np_.get("index") or 0),
                "count": int(np_.get("count") or 0)}

    async def _push_now(self, receiver: Optional[_Receiver] = None) -> None:
        """Send the now-playing to one receiver, or to all of them."""
        payload = self._now_payload()
        targets = [receiver] if receiver is not None \
            else list(self._receivers.values())
        for r in targets:
            try:
                await r.ws.send_json(payload)
            except Exception:
                self._receivers.pop(r.sid, None)

    async def _push_now_if_changed(self) -> None:
        """Fan the now-playing out when the queue has moved on. Cheap enough to
        ask every chunk: it is two attribute reads until something changes."""
        if not self._receivers:
            return
        try:
            origin = self._source.item_origin_s()
        except Exception:
            origin = None
        key = (self._queue_pos, origin)
        if key == self._now_key:
            return
        self._now_key = key
        await self._push_now()

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
                if self._preroll:
                    continue     # nothing is seated yet — _preroll_probe owns
                                 # this phase, and every lag it could read
                                 # would be a reading of the silent lead
                if self._source_spent():
                    logger.info("Sync source finished and drained — "
                                "stopping the session")
                    asyncio.create_task(self.stop_session())
                    return
                items = [(sid, st) for sid, st in list(self._streams.items())
                         if st.connected and st.pos is not None
                         and st.parked_since is None
                         and not st.probing]
                # A probing device is skipped, not merely held in cooldown: its
                # media time counts from a silent lead, so every lag derived
                # from it describes the lead (_probe_reload).
                results = []
                if items:
                    results = await asyncio.gather(
                        *(self._measure_lag(st) for _, st in items),
                        return_exceptions=True)
                lags: Dict[str, float] = {
                    sid: r for (sid, _), r in zip(items, results)
                    if isinstance(r, float)}
                # Stamped for every device that answered, including ones still
                # in cooldown: a device being ignored is not a device that has
                # gone quiet, and the sweep below must not confuse the two.
                for sid in lags:
                    st = self._streams.get(sid)
                    if st is not None:
                        st.last_lag_at = time.monotonic()
                self._sweep_parked()
                self._sweep_interrupted()
                self._sweep_silent()
                if not lags:
                    continue
                if self._target_lag is None:
                    # A re-align clears the target so it can be re-derived, but
                    # every receiver is refilling at that moment and reporting a
                    # media time near zero — deriving from those would define
                    # "aligned" as the middle of the disturbance. Nothing is
                    # read until the re-acquisition cooldown has run out. Zero
                    # at session start, so that path is unchanged.
                    if time.monotonic() < self._target_wait_until:
                        continue
                    n_connected = len([s for s in self._streams.values()
                                       if s.connected
                                       and s.parked_since is None
                                       and not s.probing])
                    if (len(lags) < n_connected
                            and time.monotonic() < self._acquire_deadline):
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
                        if st.learn_lag:
                            self._model_learn(st, "lag_s", lag - st.precomp_s)
                        batch.append(self._sample_row(st, "startup", lag=lag))
                    error = lag - self._target_lag   # >0: behind, serve faster
                    # Rebuilt each poll, so every counter has to be restated
                    # here — anything written into stats elsewhere is wiped.
                    st.stats = {"offset_ms": round(error * 1000),
                                "rtt_ms": "n/a", "late": 0,
                                "resyncs": st.resyncs,
                                "reconnects": st.reconnects,
                                "reloads": st.reloads,
                                "interrupts": st.interrupts,
                                "parks": st.parks,
                                "latency_ms": round(st.latency_s * 1000),
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
                        # Reloading works against the current target, which is
                        # the only question the count asks. Otherwise unrelated
                        # interruptions accumulate over a session and re-align
                        # the zone for faults already fixed.
                        st.reloads_since_align = 0
                    concordant = (len(st.err_hist) >= 3
                                  and min(abs(e) for e in st.err_hist) > jump_min
                                  and min(st.err_hist) * max(st.err_hist) > 0)
                    # An offset with no timeline in front of it is not made
                    # reachable by a third reading, and waiting for one costs
                    # two polls of audible double-playback. Two same-signed
                    # readings past the ceiling escalate. This is the path of an
                    # interruption that kept reporting PLAYING while its clock
                    # stalled, which _sweep_interrupted cannot see (§7.1).
                    reach_s = max(0.0, self._reader_ceiling(self._source)
                                  - st.pos) / RATE
                    unreachable = (len(st.err_hist) >= 2
                                   and min(st.err_hist[-2:])
                                   > reach_s + STREAM_JUMP_MIN_S)
                    if abs(residual) > jump_min \
                            and (concordant or unreachable
                                 or (not st.acquired and len(st.err_hist) >= 2)):
                        # Bounded by the timeline that exists (open-zone.md
                        # A.2) — and by the guard band above it, which is the
                        # part that is not merely arithmetic (_reader_ceiling).
                        step = med3 * RATE
                        ceil = self._reader_ceiling(self._source)
                        shortfall = 0.0
                        if st.pos + step > ceil:
                            # Move nothing (§A.2). A partial step cannot close
                            # an offset the timeline is too short to hold, and
                            # every sample of it drags the reader toward the
                            # head. One device out of alignment is a local
                            # fault; a reader on the head silences the zone.
                            shortfall = (step - max(0.0, ceil - st.pos)) / RATE
                            step = 0.0
                            if time.monotonic() - st.clamp_logged \
                                    > STREAM_CLAMP_LOG_EVERY_S:
                                st.clamp_logged = time.monotonic()
                                logger.warning(
                                    f"Sync stream resync {st.name} not moved: "
                                    f"wanted {med3 * 1000:+.0f} ms with only "
                                    f"{max(0.0, ceil - st.pos) / RATE * 1000:.0f} ms "
                                    f"of timeline ahead of the reader — only a "
                                    f"reload or a group re-align fits")
                        if step:
                            # The step rung's own failure signal (§7.1). The
                            # cooldown outlasts the buffer, so the next error
                            # should be a fraction of this one; still this
                            # large and same-signed means the reader moved and
                            # the device did not follow. Without it, a device
                            # whose steps are all accepted and none effective
                            # resyncs indefinitely and never escalates.
                            prev = st.last_step_error
                            unmoved = (prev is not None
                                       and med3 * prev > 0
                                       and abs(med3) >= abs(prev)
                                       * STREAM_STEP_FUTILE_FRACTION)
                            st.futile_steps = st.futile_steps + 1 if unmoved else 0
                            st.last_step_error = med3
                            st.pos += step
                            st.shift += step
                            st.moved_s += step / RATE
                            st.slew_s = 0.0   # jump supersedes any pending slew
                            if st.resampler is not None:
                                st.resampler.reset()
                            st.resyncs += 1
                            # Long enough for the step to reach the device's
                            # output, measured against its own lag — what it
                            # holds undrained — not the group target.
                            st.cooldown_until = (time.monotonic()
                                                 + self._step_cooldown_s(lag))
                            st.err_hist = []
                            st.lag_hist = []  # device-buffer transient follows
                            st.fit_lost_at = time.monotonic()
                            batch.append(self._sample_row(st, "resync", lag=lag,
                                                          error=med3))
                            logger.info(f"Sync stream resync {st.name}: "
                                        f"{med3 * 1000:+.0f} ms")
                            if st.futile_steps >= STREAM_FUTILE_STEPS:
                                self._escalate_shortfall(
                                    st, f"{st.name} still {med3 * 1000:+.0f} ms "
                                        f"out after {st.futile_steps + 1} "
                                        f"consecutive steps")
                        # A clamped step is not a step (§A.4): no discontinuity
                        # to wait out, and clearing the history would cost three
                        # fresh readings on top of the blackout. Both left alone
                        # so the next poll re-confirms and the rung below fires
                        # on its own floor; the warning is rate-limited instead.
                        # Only a fresh LOAD closes this, by dropping the buffer
                        # the reader cannot reach. Not awaited: the poll pass
                        # owns the other speakers.
                        if shortfall > STREAM_JUMP_MIN_S:
                            self._escalate_shortfall(
                                st, f"{st.name} {med3 * 1000:+.0f} ms out with "
                                    f"only {shortfall * 1000:.0f} ms more "
                                    f"timeline than the reader can reach")
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
                        # Inside the sensor's own noise: whatever the last step
                        # was aimed at is gone, so it is not evidence about the
                        # next one.
                        st.slew_s = med3
                        st.futile_steps = 0
                        st.last_step_error = None
                        self._pll_update(st, lag)
                    st.stats["slew_ms"] = round(st.slew_s * 1000, 1)
                    st.stats["silent_s"] = round(
                        time.monotonic() - (st.last_lag_at or 0.0), 1)
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
        got = OpenZone._fit_slope_se(pts)
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
        spans the apply threshold.

        Fast again while any device is out of playback: there, the cadence is
        not buying precision, it is how long the zone goes on playing that
        device's stale buffer before the reload that ends it lands.
        """
        if any(st.interrupted_since is not None
               for st in self._streams.values()):
            return STREAM_POLL_FAST_S
        return STREAM_POLL_FAST_S if self._acquiring() else STREAM_POLL_S

    async def _read_media_time(self, st: _Stream) -> Optional[tuple]:
        """One status read → ``(read time, reported media time)``, or None.

        Also the session's only sensor for *interruption* (open-zone.md §A.3).
        A receiver that leaves PLAYING keeps its buffer and resumes into it, as
        far behind as it was held; that state must stay distinguishable from
        the ``None`` meaning unreachable, stale or not started, or the 30 s
        silence sweep is the only backstop (`_sweep_interrupted`).
        """
        uuid_str = st.player_id.split(":", 1)[1]
        cast = self.cast._casts.get(uuid_str)
        if cast is None:
            return None
        try:
            mc = cast.media_controller
            await asyncio.to_thread(mc.update_status)
            await asyncio.sleep(STREAM_STATUS_WAIT_S)   # let the push arrive
            status = mc.status
            state = getattr(status, "player_state", "") or ""
            st.state = state
            if state != "PLAYING":
                if st.interrupted_since is None:
                    st.interrupted_since = time.monotonic()
                return None
            if st.interrupted_since is not None:
                # Ended between two polls. Held here rather than acted on
                # inline: the sweep owns the rung, and it must see the length
                # of an interruption that is already over.
                st.interrupt_held = max(
                    st.interrupt_held, time.monotonic() - st.interrupted_since)
                st.interrupted_since = None
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
        return time.monotonic(), float(ct)

    async def _measure_lag_once(self, st: _Stream) -> Optional[float]:
        """One media-time read → lag vs server clock (s), trim excluded."""
        got = await self._read_media_time(st)
        if got is None:
            return None
        now, ct = got
        played_timeline_s = (st.start_pos + st.shift) / RATE + ct
        trim_s = st.trim_ms / 1000.0
        return (now - self._epoch) - played_timeline_s - trim_s

    async def _probe_once(self, st: _Stream) -> None:
        """One pre-roll latency reading for a device playing the silent lead.

        The reported media time counts from the first byte of this fetch, so
        against the moment the generator opened it the difference is the
        device's whole pipeline latency — network, receiver buffer, decoder,
        DAC. Measured on the device itself, this session, before a sample of
        content has been served; the model's ``lag_s`` only ever estimated it
        from a previous one.
        """
        if st.opened_at is None:
            return
        got = await self._read_media_time(st)
        if got is None:
            return
        now, ct = got
        lat = (now - st.opened_at) - ct
        if lat < 0.0:
            return          # a superseded fetch or a clock step, not a latency
        st.probe_hist = (st.probe_hist + [lat])[-PREROLL_READS:]

    @staticmethod
    def _probe_settled(st: _Stream) -> bool:
        """Whether this device's latency has stopped moving. The device
        buffers before it starts reporting, so the first readings describe a
        buffer that is still filling; only once consecutive readings agree
        does the number mean the pipeline's steady-state depth."""
        h = st.probe_hist[-PREROLL_MIN_READS:]
        return (len(h) >= PREROLL_MIN_READS
                and max(h) - min(h) <= PREROLL_SETTLE_S)

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
        """Every connected device has been measured and pulled into place.
        A parked device is not one the zone is waiting for: it is absent, and
        holding the fade-in for it would keep the group silent for as long as
        it stays off the network."""
        live = [s for s in self._streams.values()
                if s.connected and s.parked_since is None]
        return bool(live) and all(s.acquired for s in live)

    def _acquire_gain(self, frames: int):
        """Output gain for one block: None means unity (the common case)."""
        if self._fade_start is None:
            # From the end of the pre-roll, not the epoch: the budget is for
            # acquisition, and acquisition has not begun until content has.
            elapsed = time.monotonic() - (self._acquire_from or self._epoch)
            if self._group_locked() or elapsed > self._acquire_max_s:
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

    async def _preroll_probe(self) -> None:
        """Measure every device's latency while the delay line fills
        (open-zone.md §7.3).

        The prime and the receiver probe are independent costs, so the second
        runs inside the first: every device is LOADed onto a silent lead, its
        pipeline latency read off that lead (`_probe_once`), and content starts
        only once the buffer is deep enough *and* the spread is known. Readers
        are then seated with that spread pre-compensated, so the first content
        sample is aligned rather than corrected in front of the listener.

        Bounded at both ends: a device that never reports is left behind after
        ``PREROLL_QUORUM_S``, and the phase is capped at ``PREROLL_MAX_S``,
        past which the learned model beats more waiting.
        """
        start = time.monotonic()
        quorum_until = start + PREROLL_QUORUM_S
        deadline = start + (STREAM_REALIGN_MAX_S if self._preroll_realign
                            else max(PREROLL_MAX_S,
                                     self._preroll_target_s + 5.0))
        try:
            while self.running and self._preroll:
                await asyncio.sleep(PREROLL_POLL_S)
                if time.monotonic() > deadline:
                    logger.warning(
                        "OpenZone pre-roll timed out — starting content on "
                        "whatever the probe has")
                    break
                live = [st for st in self._streams.values()
                        if st.connected and st.opened_at is not None
                        and st.parked_since is None]
                if not live:
                    continue
                await asyncio.gather(*(self._probe_once(st) for st in live),
                                     return_exceptions=True)
                expected = len([st for st in self._streams.values()
                                if st.parked_since is None])
                if len(live) < expected and time.monotonic() < quorum_until:
                    continue          # a device is still coming up
                if not all(self._probe_settled(st) for st in live):
                    continue
                if self._preroll_realign:
                    break      # the delay line is already full and maintained;
                               # the measurement is the only thing being waited
                               # on (_realign_group)
                vals = [self._median(st.probe_hist) for st in live]
                # The lead the delay line has to hold is the source's own
                # delay, plus the widest pre-compensation the measurements are
                # about to ask for, plus the largest positive trim — every one
                # of those seats a reader further back, and seating one past
                # what the timeline reaches is what _seat_position clamps and
                # warns on.
                need = (self._source.delay_s + (max(vals) - min(vals))
                        + max(0.0, max(st.trim_ms for st in live) / 1000.0))
                if await self._source.prime(timeout=0.0, target_s=need):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"OpenZone pre-roll probe failed: {e}")
        finally:
            self._end_preroll()

    def _end_preroll(self) -> None:
        """Set the group's pre-compensation from what the probe measured and
        release the readers onto content.

        Only the *differences* are an alignment: the slowest device sets the
        group's clock and every other reader is seated that much further back,
        which is the same relation the model path builds from ``lag_s`` —
        except these numbers were measured seconds ago on this network, on
        this content, rather than averaged over previous sessions.

        The target lag itself is deliberately not set here. The monitor
        derives it from the lags that actually result, which is the one
        derivation known to converge (`_realign_group`); with the spread
        already compensated those lags agree on arrival, so it lands on the
        first poll instead of after a correction the listener can hear.
        """
        if not self._preroll:
            return
        # Only probes with a full median window. A device that reported once
        # or twice reported from inside a buffer that was still filling, and
        # that number is worse than the model it would replace.
        # Parked devices excluded: absent, so they must not set the group's
        # spread — and at a re-align their probe_hist is a stale session-start
        # reading that was never cleared (_realign_group).
        lats = {sid: self._median(st.probe_hist)
                for sid, st in self._streams.items()
                if len(st.probe_hist) >= PREROLL_MIN_READS
                and st.parked_since is None}
        for st in self._streams.values():
            # Coming up is not PLAYING either, so the lead's own startup has
            # been accumulating as an interruption. Carrying it past the
            # pre-roll would fire the reload rung on the first poll of every
            # session (_sweep_interrupted).
            st.interrupted_since = None
            st.interrupt_held = 0.0
        if lats:
            slowest = max(lats.values())
            for sid, lat in lats.items():
                st = self._streams.get(sid)
                if st is None:
                    continue
                st.latency_s = lat
                st.precomp_s = max(0.0, slowest - lat)
                if not self._preroll_realign:
                    # A re-align measures every receiver refilling at once,
                    # which is a re-acquisition figure rather than this
                    # device's natural startup latency (§A.4).
                    self._model_learn(st, "probe_s", lat)
            # A complete settled probe IS the lock: every connected device has
            # been measured and seated at the offset that measurement asks for,
            # which is exactly what _group_locked waits to observe. Holding the
            # zone silent for another two polls to re-derive it from content
            # would put the pre-roll's saving straight back.
            live = [st for st in self._streams.values()
                    if st.connected and st.parked_since is None]
            if live and all(st.sid in lats and self._probe_settled(st)
                            for st in live):
                self._fade_start = time.monotonic()
            logger.info(
                "OpenZone pre-roll measured "
                + ", ".join(f"{self._streams[sid].name} {lat * 1000:.0f} ms"
                            for sid, lat in lats.items())
                + f" — pre-compensating up to {slowest * 1000:.0f} ms")
        else:
            logger.warning("OpenZone pre-roll measured nothing — falling back "
                           "to the learned model")
        now = time.monotonic()
        # Nothing may be read until the lead has drained. Every device is
        # still playing silence off the front of its fetch, so its reported
        # media time is inside the lead and the lag computed from it is wrong
        # by whatever is left of it — deriving the group's target from those
        # readings would define "aligned" as the middle of the changeover.
        # Same hold, and for the same reason, as a re-align's (_realign_group).
        self._target_wait_until = (now + (max(lats.values()) if lats else 0.0)
                                   + STREAM_AHEAD_S + STREAM_POLL_S)
        self._acquire_deadline = self._target_wait_until + 25
        if self._preroll_realign:
            # The budget is NOT restarted here. A re-align's bound is on the
            # silence the zone hears, and the lead is part of that silence —
            # restarting would hand the acquisition a fresh budget on top of
            # the one the lead just spent (_realign_group).
            #
            # A reader seats on leaving the lead, so a device that never
            # fetched would keep its pre-realign seat. At session start there
            # is no seat to keep and the generator's own `pos is None` path
            # covers it; here the fallback has to be explicit (_end_probe).
            for st in self._streams.values():
                if (st.pos is not None and st.opened_at is None
                        and st.parked_since is None):
                    self._seat_position(st, self._source)
        else:
            # A budget for finding alignment in content: measuring it from the
            # epoch would have spent most of it before content was served.
            self._acquire_from = now
        self._preroll_realign = False
        self._preroll = False

    def _precomp_for_target(self, latency_s: float) -> Optional[float]:
        """Pre-compensation that seats a device of this latency on the group's
        current target lag; None when there is no target to aim at.

        A fresh seat satisfies ``lag = delay_s + precomp + latency`` (the trim
        cancels, §6.1) and the ladder drives every lag onto ``_target_lag``, so
        the pre-compensation is the difference. Not clamped at zero: a device
        slower than the one that set the target is served newer audio to come
        out on time, and ``_seat_position`` owns the only real bound (§A.2).
        """
        if self._target_lag is None:
            return None
        return self._target_lag - self._source.delay_s - latency_s

    async def _probe_reload(self, st: _Stream) -> None:
        """Measure one device's latency on the lead its reload opened, and seat
        it on the group's target from that rather than from the model
        (open-zone.md §7.5).

        The reading is not written to the model: it comes from a device just
        disturbed badly enough to need a reload, and the model seeds every
        future session's pre-compensation (§A.4).
        """
        deadline = time.monotonic() + STREAM_PROBE_MAX_S
        measured: Optional[float] = None
        try:
            while (self.running and st.probing
                   and self._streams.get(st.sid) is st):
                await asyncio.sleep(PREROLL_POLL_S)
                if st.parked_since is not None:
                    break      # absent: nothing to measure, and the lead would
                               # outlast the absence (_park_stream)
                if st.connected and st.opened_at is not None:
                    await self._probe_once(st)
                    if self._probe_settled(st):
                        measured = self._median(st.probe_hist)
                        break
                if time.monotonic() > deadline:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Sync reload probe failed for {st.name}: {e}")
        finally:
            self._end_probe(st, measured)

    def _end_probe(self, st: _Stream, measured: Optional[float]) -> None:
        """Apply what the probe measured and release the device onto content.
        Clearing ``probing`` is what lets the generator leave the lead, so it
        comes last: the seat it then takes reads the pre-comp set here."""
        if not st.probing:
            return
        precomp = (self._precomp_for_target(measured)
                   if measured is not None else None)
        if precomp is not None:
            st.latency_s = measured
            st.precomp_s = precomp
            logger.info(
                f"Sync stream {st.name} re-measured at {measured * 1000:.0f} ms "
                f"— seating at pre-comp {precomp * 1000:.0f} ms against a "
                f"{self._target_lag:.2f}s target")
        else:
            logger.info(
                f"Sync stream {st.name} probe did not settle — seating from "
                f"the learned model")
        # The generator seats on leaving the lead, so it only does so if it
        # ever entered one. A probe that ends before the device fetches would
        # otherwise leave the reader on its pre-reload seat — the seat the
        # unprobed path takes here is the one this replaces, not an extra.
        # Safe against the generator racing in: neither this nor the lead's
        # entry awaits between reading the flag and setting the other.
        if st.opened_at is None:
            self._seat_position(st, self._source)
        st.probing = False

    def _seat_after_preroll(self, st: _Stream, source) -> None:
        """Seat a reader whose device has already been playing this fetch.

        ``_seat_position`` assumes a fresh LOAD — media time restarting at zero,
        empty queue. After a lead neither holds, and the two corrections are
        independent.

        **The clock did not restart.** The device has consumed the same HTTP
        response since ``opened_at``, so its reported time is ``preroll_frames``
        past the first content sample; the lag reading is
        ``(start_pos + shift) / RATE + reported_time``, so the lead comes off
        the base or the session is wrong by its length.

        **The queue may not be empty.** ``latency_s`` is the steady part of the
        pipeline; the serve loop's pacing sawtooth is queue too. The caller
        pays it down before seating, so only its sleep-granularity residue
        arrives here — corrected anyway, since it is exact and one subtraction.

        ``precomp_s`` is restored afterwards: it is the model-comparable
        quantity ``_model_learn`` subtracts back out of the first stable lag,
        and a queue residue belongs to this changeover, not the device.
        """
        serve_ahead = max(0.0, st.preroll_frames / RATE
                          - (time.monotonic() - (st.opened_at or 0.0)))
        model_precomp = st.precomp_s
        st.precomp_s = model_precomp - serve_ahead
        try:
            self._seat_position(st, source)
        finally:
            st.precomp_s = model_precomp
        st.start_pos -= st.preroll_frames
        # Nothing may be read until the lead has drained, and what is left of
        # it at this moment is exactly the queue above: the part inside the
        # device plus the part the serve loop had run ahead. A reading taken
        # before that describes the silence, not the content.
        st.cooldown_until = max(
            st.cooldown_until,
            time.monotonic() + st.latency_s + serve_ahead + STREAM_POLL_S)
        logger.info(
            f"OpenZone {st.name} on content: latency "
            f"{st.latency_s * 1000:.0f} ms, pre-comp "
            f"{model_precomp * 1000:.0f} ms, lead "
            f"{st.preroll_frames / RATE:.1f}s ({serve_ahead * 1000:.0f} ms "
            f"of it still queued)")

    def _seat_position(self, st: _Stream, source) -> None:
        """Seat a reader for a device whose media clock starts at zero.

        Zeroes ``shift`` and re-bases ``start_pos`` because the lag reading is
        ``(start_pos + shift) / RATE + reported_time`` — a fresh LOAD restarts
        the reported time, so leaving either behind makes every subsequent
        measurement wrong by however far the session had already moved.
        Bounded by both ends of the delay line (open-zone.md §A.2)."""
        trim = int(st.trim_ms * RATE / 1000)
        precomp = int(st.precomp_s * RATE)
        pos = (int((time.monotonic() - self._epoch - source.delay_s) * RATE)
               - trim - precomp)
        floor = source.earliest_sample() + _rs.READ_MARGIN
        ceil = self._reader_ceiling(source)
        if pos < floor:
            logger.warning(
                f"Sync stream {st.name} clamped {(floor - pos) / RATE:.2f}s "
                f"forward — delay line was short at launch")
            pos = floor
        if pos > ceil >= floor:
            logger.warning(
                f"Sync stream {st.name} clamped {(pos - ceil) / RATE:.2f}s "
                f"back — seat was inside the write head's guard band")
            pos = ceil
        st.shift = 0.0
        st.moved_s = 0.0
        st.pos = float(pos)
        st.start_pos = int(pos)
        # Seating is the moment the device becomes the ladder's responsibility,
        # so it is also when its silence clock starts: one that never reports
        # is escalated rather than left in the zone unwatched.
        st.last_lag_at = time.monotonic()

    def _park_stream(self, st: _Stream, reason: str) -> None:
        """Take an absent device out of the group until it answers again
        (open-zone.md §7.1).

        Every rung assumes a device that is *present and wrong*, and the top
        one is actively harmful for a device that is not: it re-LOADs the whole
        zone on behalf of a speaker that cannot receive the LOAD. Absence is
        therefore its own state, not the bottom of the ladder — excluded from
        measurement, target derivation, the acquisition lock and re-alignment,
        and probed for its return on its own backoff (`_sweep_parked`).

        The signal is the cast provider failing to *resolve* the device, which
        is what separates absence from every kind of misbehaviour.

        Pre-compensation and trim are left alone: they are properties of the
        device and the room, unchanged by the absence, and they let it rejoin
        against the existing target instead of forcing a re-derivation.
        """
        now = time.monotonic()
        if st.parked_since is not None:
            st.park_probe_at = now + STREAM_PARK_RETRY_S
            return
        st.parked_since = now
        st.parks += 1
        st.park_probe_at = now + STREAM_PARK_RETRY_S
        st.acquired = False
        st.err_hist = []
        st.lag_hist = []
        st.slew_s = 0.0
        st.futile_steps = 0
        st.last_step_error = None
        st.interrupted_since = None
        st.interrupt_held = 0.0
        # An absence must not push the group toward a re-align. The count asks
        # whether reloading works against the current target, and a device
        # that was not on the network never answered that question.
        st.reloads_since_align = 0
        st.stats = {**st.stats, "parked": True, "parks": st.parks}
        logger.warning(
            f"Sync stream parking {st.name} — {reason}. Out of the group "
            f"until it answers; the zone is not re-aligned for a device that "
            f"is not on the network (park #{st.parks})")

    async def _rejoin_stream(self, st: _Stream) -> None:
        """Probe a parked device and put it back if it answers.

        The rejoin is a per-device reload, not a group event: the target is
        already established and this device's pre-compensation still describes
        it, so it re-seats against what the zone is already doing and the
        ladder converges whatever residue is left. Nothing the other speakers
        are doing changes.
        """
        try:
            if not self.running or self._streams.get(st.sid) is not st:
                return
            uuid_str = st.player_id.split(":", 1)[1]
            try:
                cast = await self.cast._get_cast(uuid_str)
            except Exception as e:
                logger.debug(f"Sync rejoin probe failed for {st.name}: {e}")
                cast = None
            if not cast:
                st.park_probe_at = time.monotonic() + STREAM_PARK_RETRY_S
                return
            away = time.monotonic() - (st.parked_since or time.monotonic())
            st.parked_since = None
            st.stats = {**st.stats, "parked": False}
            logger.info(
                f"Sync stream {st.name} answered again after {away:.0f}s away "
                f"— rejoining against the group's existing target")
            # If it drops again between this probe and the LOAD, _launch_stream
            # parks it once more and the backoff simply resumes.
            await self._reload_stream(st)
        finally:
            st.park_probing = False

    def _sweep_parked(self) -> None:
        """Probe each parked device for its return, on its own backoff.

        Off the poll: resolution can block for as long as the provider's own
        timeout, and the poll pass owns every other speaker in the zone.
        """
        if self._realigning:
            return
        now = time.monotonic()
        for st in list(self._streams.values()):
            if st.parked_since is None or st.park_probing:
                continue
            if now < st.park_probe_at:
                continue
            st.park_probing = True
            st.park_probe_at = now + STREAM_PARK_RETRY_S
            asyncio.create_task(self._rejoin_stream(st))

    def _sweep_interrupted(self) -> None:
        """Reload any receiver taken out of playback (open-zone.md §7.1).

        An interruption produces no measured error while it lasts, and the
        device resumes into its own buffer — as far behind as it was held,
        which is past the reader's forward authority. Every rung below the
        reload is unreachable by construction, so this decides on the cause
        (``player_state``) rather than waiting three polls for the symptom.

        Retried every ``STREAM_INTERRUPT_RELOAD_MIN_S`` while the device stays
        down; issuing a LOAD into a held receiver is still worth it, since it
        is the only thing that drops the buffer it would resume into.

        Gated on ``natural_lag`` so a device still starting is left to the
        launch path, and on the cooldown so a reload's own re-acquisition is
        not read as the fault. Not routed through ``_escalate_shortfall``: this
        is a local fault, and re-aligning the zone for it is wrong at any count.
        """
        if self._realigning or self._preroll:
            return
        now = time.monotonic()
        for st in list(self._streams.values()):
            if st.pos is None or st.natural_lag is None:
                continue
            if st.parked_since is not None:
                continue     # absent, not interrupted (_park_stream)
            if st.probing:
                continue     # already on the rung this would reach: it is
                             # playing a lead a reload opened (_probe_reload)
            held = (now - st.interrupted_since
                    if st.interrupted_since is not None else st.interrupt_held)
            if held < STREAM_INTERRUPT_MIN_S or now < st.cooldown_until:
                continue
            if (st.last_interrupt_reload is not None
                    and now - st.last_interrupt_reload
                    < STREAM_INTERRUPT_RELOAD_MIN_S):
                continue
            st.last_interrupt_reload = now
            st.interrupt_held = 0.0
            st.interrupts += 1
            st.stats["interrupts"] = st.interrupts
            logger.warning(
                f"Sync stream {st.name} left playback for {held:.1f}s "
                f"(state={st.state or 'unknown'}) — reloading rather than "
                f"waiting out an offset the reader cannot step away")
            asyncio.create_task(self._reload_stream(st))

    def _sweep_silent(self) -> None:
        """Escalate any device that has stopped reporting a playable position
        (open-zone.md §7.1).

        The ladder runs on measured error, so a device reporting none is not
        merely uncorrected but unseen — it falls out of ``lags`` and plays
        whatever it has left for the rest of the session. Silence is therefore
        its own fault class, escalated through the same ladder: only a fresh
        LOAD retrieves a receiver that has stopped following the stream.

        Held off during a re-align, when every receiver is expected to be
        quiet, and gated on ``last_lag_at`` so a device still coming up is not
        escalated before it has had a chance to report.
        """
        if self._realigning:
            return
        now = time.monotonic()
        for st in list(self._streams.values()):
            if st.pos is None or st.last_lag_at is None:
                continue
            if st.parked_since is not None:
                continue     # its silence is already accounted for, and the
                             # rungs this would reach cannot touch it
            if st.probing:
                continue     # silent by design, and bounded: the lead is
                             # capped at STREAM_PROBE_MAX_S (_probe_reload)
            silent = now - st.last_lag_at
            if silent < STREAM_SILENT_MAX_S:
                continue
            st.stats["silent_s"] = round(silent, 1)
            self._escalate_shortfall(
                st, f"{st.name} has reported no playable position for "
                    f"{silent:.0f}s")

    def _escalate_shortfall(self, st: _Stream, reason: str) -> None:
        """A correction the rung below could not deliver: pick the one above
        (open-zone.md §7.1).

        Which rung applies is a question of capability, not severity. A reload
        re-seats at ``play_now - delay_s - trim - precomp`` and the device plays
        that sample only once refilled, so it lands on target only if it
        resumes within ``target_lag - (delay_s + trim + precomp)`` — a budget
        fixed by the group's geometry, and a couple of hundred milliseconds for
        the most pre-compensated speaker against refills measured in seconds.
        Past ``STREAM_RELOADS_BEFORE_REALIGN`` it re-creates the fault rather
        than rescuing it, and only re-deriving the target converges."""
        if st.parked_since is not None:
            return       # absent: no rung applies, and the one above would
                         # re-LOAD the zone for it (_park_stream)
        now = time.monotonic()
        if st.reloads_since_align >= STREAM_RELOADS_BEFORE_REALIGN:
            # Past this point a reload is not a slower rescue but one that
            # cannot work, so a re-align held off by its own floor waits — it
            # must not fall back to re-dropping this device's buffer for
            # nothing, which is a loop that outlasts the outage it is treating.
            if (not self._realigning
                    and (self._last_realign is None
                         or now - self._last_realign
                         > STREAM_REALIGN_MIN_INTERVAL_S)):
                self._last_realign = now  # stamped on decision, not in the task
                asyncio.create_task(self._realign_group(
                    f"{reason}, after {st.reloads_since_align} reload(s)"))
            return
        # Rate-limited, and safe to lose to that limit: the rung below either
        # moved nothing or moved something the device did not follow, so a
        # deferred reload costs alignment on one device rather than leaving its
        # reader on the head.
        if (st.last_reload is None
                or now - st.last_reload > STREAM_RELOAD_MIN_INTERVAL_S):
            # Stamped here, not in the coroutine: the interval must close when
            # the reload is decided, or a second poll schedules another before
            # the first runs.
            st.last_reload = now
            logger.warning(f"Sync stream escalating — {reason}")
            asyncio.create_task(self._reload_stream(st))

    async def _realign_group(self, reason: str) -> None:
        """Re-establish the whole group's timing, as at session start
        (open-zone.md §7.1). The ladder's last rung.

        Session start converges because it does not aim at a fixed target: every
        device is LOADed together and ``_target_lag`` is *derived* from the lags
        that result, so whatever the receivers did defines aligned. A per-device
        reload inherits a target set under different conditions and cannot move
        it, which is how it reproduces one offset indefinitely.

        So: re-LOAD everyone onto the silent lead session start uses, re-measure
        every device on it, seat from that, and clear the target so the monitor
        re-derives it. The source is untouched — the timeline was never the
        problem, only the devices' relationship to it.

        The group is muted for the re-acquisition rather than playing through
        it (§7.5): pulling five devices together on content is the echo this
        exists to remove, and silence in every room is the better cost.

        The re-measured lags must NOT reach the model: they are re-acquisition
        figures, not natural startup latency, and would seed every future
        session's pre-compensation with this incident (§A.4).
        """
        if not self.running or self.app_id:
            return
        # A parked device is not re-LOADed with the group: it cannot receive
        # the LOAD, and including it would re-derive the target from a set the
        # zone is not actually playing on. It rejoins on its own probe.
        streams = [st for st in self._streams.values()
                   if st.pos is not None and st.parked_since is None]
        if not streams:
            return
        self._realigning = True
        try:
            logger.warning(f"Sync group re-aligning {len(streams)} device(s) "
                           f"— {reason}")
            # Read before clearing the target: _step_cooldown_s is derived from
            # it, and the cooldown below has to outlast a full re-acquisition.
            cooldown = self._step_cooldown_s()
            self._target_lag = None
            # One poll past the per-device cooldown, not level with it: the
            # target must come from readings taken after the devices became
            # eligible, not from the ones straddling the boundary.
            self._target_wait_until = time.monotonic() + cooldown + STREAM_POLL_S
            self._acquire_deadline = self._target_wait_until + 25
            model_lags = {st.player_id: self._model.get(st.player_id, {}).get("lag_s")
                          for st in streams}
            provisional = (max(model_lags.values()) + STREAM_LAG_MARGIN_S
                           if all(v is not None for v in model_lags.values())
                           else None)
            now = time.monotonic()
            for st in streams:
                lag = model_lags.get(st.player_id)
                st.precomp_s = (max(0.0, provisional - lag)
                                if provisional is not None and lag is not None
                                else 0.0)
                st.natural_lag = None     # re-derive the target from what lands
                st.learn_lag = False
                st.reloads_since_align = 0
                st.clamp_logged = 0.0
                st.err_hist = []
                st.lag_hist = []
                st.slew_s = 0.0
                st.acquired = False
                st.futile_steps = 0
                st.last_step_error = None
                # The device is about to be quiet for a re-acquisition, which
                # is expected and must not read as the silence fault — nor,
                # once it leaves PLAYING to take the LOAD, as an interruption.
                st.last_lag_at = now
                st.interrupted_since = None
                st.interrupt_held = 0.0
                st.preroll_frames = 0
                st.opened_at = None
                # Supersedes any probe in flight: this re-LOADs the device
                # anyway, and the probe aims at a target about to be cleared.
                st.probing = False
                if st.probe_task is not None:
                    st.probe_task.cancel()
                    st.probe_task = None
                st.probe_hist = []
                st.fit_lost_at = now
                st.cooldown_until = now + cooldown
                if st.resampler is not None:
                    st.resampler.reset()
                # Not seated here: the lead below re-measures where each reader
                # belongs, and _end_preroll seats from that. The model values
                # above stand as the fallback if nothing measures.
            # Re-enter the session's own pre-roll (§7.5). Set before the LOADs,
            # because the generator decides on this flag as each fetch opens.
            self._preroll_realign = True
            self._preroll = True
            self._preroll_target_s = 0.0      # the delay line is already full
            # Silence, not an echo: the group is muted until the probe settles,
            # so the re-acquisition is not performed in front of the listener
            # (_acquire_gain). _end_preroll releases the fade.
            #
            # One budget spans the lead and the acquisition after it, timed
            # from here, so the zone's total silence is bounded by
            # STREAM_REALIGN_MAX_S however the two divide.
            self._fade_start = None
            self._acquire_from = now
            self._acquire_max_s = STREAM_REALIGN_MAX_S
            if self._preroll_task is not None:
                self._preroll_task.cancel()
            self._preroll_task = asyncio.create_task(self._preroll_probe())
            await asyncio.gather(
                *(self._launch_stream(st.player_id, st.sid) for st in streams),
                return_exceptions=True)
        except Exception as e:
            logger.warning(f"Sync group re-align failed: {e}")
        finally:
            self._realigning = False

    async def _reload_stream(self, st: _Stream) -> None:
        """Re-LOAD one receiver and re-seat its reader at the live edge.

        The escape hatch for a device the ladder cannot reach: an assistant
        notification or an incoming call holds the receiver for long enough
        that the device ends up further behind than there is buffer ahead of
        it, and no move of the reader can close a gap that large on a live
        source (open-zone.md §A.2). Only the device's own buffer can be
        dropped, which is what a fresh LOAD does."""
        if not self.running or self._streams.get(st.sid) is not st:
            return
        st.reloads += 1
        st.reloads_since_align += 1
        st.last_reload = time.monotonic()
        # A fresh LOAD restarts the reported media time, so the silent lead
        # this device may have been seated against no longer exists — and the
        # probe's clock origin along with it.
        st.preroll_frames = 0
        st.opened_at = None
        st.probe_hist = []
        # Re-measure on the lead this fetch opens instead of re-seating from
        # the model (_probe_reload). Needs a target to aim at; during pre-roll
        # the device is already on the shared lead and _end_preroll owns it.
        probing = (self._target_lag is not None
                   and not self._preroll
                   and st.parked_since is None)
        st.probing = probing
        # This interruption has now been treated. Leaving the reading behind
        # would re-fire the rung one cooldown later on evidence the reload
        # already answered — and the reload's own re-acquisition, which is not
        # PLAYING either, re-arms it immediately if the device is still down.
        st.interrupted_since = None
        st.interrupt_held = 0.0
        logger.warning(f"Sync stream reloading {st.name} — the correction "
                       f"ladder could not reach it by moving the reader "
                       f"(reload #{st.reloads})")
        if not probing:
            self._seat_position(st, self._source)
        # else: the seat is what the probe is measuring, so it waits for the
        # lead to end (_seat_after_preroll). The stale `pos` stands until then;
        # nothing reads from it on the lead, and leaving it set keeps the
        # device present to the sweeps.
        st.err_hist = []
        st.lag_hist = []
        st.slew_s = 0.0
        st.acquired = False          # re-acquiring: step small offsets away
        st.futile_steps = 0          # a fresh LOAD is a fresh relationship
        st.last_step_error = None
        # A reload's own re-acquisition is silence the sweep must not charge
        # against the device, and it is also what paces the next escalation:
        # a receiver that does not come back reaches the rung above one
        # STREAM_SILENT_MAX_S later, not immediately.
        st.last_lag_at = time.monotonic()
        st.fit_lost_at = time.monotonic()
        # The heaviest disturbance there is — the device drops its buffer and
        # refills it — so it needs at least the full observation latency before
        # its readings mean anything (_step_cooldown_s). A reload demotes the
        # device to unacquired, where two readings authorise a step, so a
        # cooldown that expires early does not merely delay the recovery: the
        # stale pair steps the reader straight back to where the reload just
        # rescued it from.
        st.cooldown_until = time.monotonic() + self._step_cooldown_s()
        if st.resampler is not None:
            st.resampler.reset()
        if probing:
            # Before the LOAD, so the cap covers a launch that never lands —
            # otherwise the lead stays open indefinitely.
            if st.probe_task is not None:
                st.probe_task.cancel()
            st.probe_task = asyncio.create_task(self._probe_reload(st))
        await self._launch_stream(st.player_id, st.sid)

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
            block = int(RATE * STREAM_BLOCK_S)
            if self._preroll or st.probing:
                # A silent lead, paced to real time exactly as content is, so
                # that the device's own buffering shows up as a lag in its
                # reported media time and the probe can read it off
                # (_preroll_probe). Nothing is read from the source and no
                # reader is seated: the delay line is still filling, and where
                # this reader belongs is not known until the probe says so.
                # After a reload only the second reason applies, and it applies
                # unchanged (_probe_reload).
                silence = _encode_s16(np.zeros((block, CHANNELS),
                                               dtype=np.float32))
                if st.opened_at is None:
                    # Only a fresh LOAD restarts the media clock, so only a
                    # fresh LOAD re-origins the probe. A fetch superseding a
                    # dropped one (§A.5) is the same clock through a new socket:
                    # re-stamping would subtract the lead so far from the
                    # latency, re-zeroing it from the seat.
                    st.opened_at = time.monotonic()
                    st.preroll_frames = 0
                    st.probe_hist = []
                while (self.running and (self._preroll or st.probing)
                       and self._streams.get(st.sid) is st and st.gen == mine):
                    ahead = (st.preroll_frames / RATE
                             - (time.monotonic() - st.opened_at))
                    if ahead > STREAM_AHEAD_S:
                        await asyncio.sleep(STREAM_BLOCK_S / 2)
                        continue
                    st.preroll_frames += block
                    yield silence
                if not (self.running and self._streams.get(st.sid) is st
                        and st.gen == mine):
                    return       # superseded mid-lead: seating would describe
                                 # a fetch that is no longer being consumed
                # Pay the serve-ahead down before seating. Where in the serve
                # loop's sawtooth a generator sits when the lead ends is
                # arbitrary and per-device, but it is queue the device plays
                # before reaching content, so seating against it leaves
                # neighbours a block apart. Waiting costs nothing: the device
                # plays that queue either way, and it is silence.
                while (self.running and self._streams.get(st.sid) is st
                       and st.gen == mine):
                    over = (st.preroll_frames / RATE
                            - (time.monotonic() - st.opened_at))
                    if over <= 0.0:
                        break
                    await asyncio.sleep(min(over, STREAM_BLOCK_S / 2))
                if not (self.running and self._streams.get(st.sid) is st
                        and st.gen == mine):
                    return
                self._seat_after_preroll(st, source)
            if st.pos is None:
                self._seat_position(st, source)
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
                win = st.resampler.window(source, st.pos, block, adv, st.inject)
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
                # Cheapest place to notice a seam: this loop already wakes once
                # per chunk, and the screens are the only thing waiting on it.
                await self._push_now_if_changed()
                i += 1
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Cast sync producer died: {e}")

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
                        # What to show, before any audio: a receiver that
                        # joins mid-session must not sit blank until the next
                        # seam, which on an album is three minutes away.
                        await self._push_now(receiver)
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
