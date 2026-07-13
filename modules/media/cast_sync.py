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

This is deliberately PoC-scoped: one global session, generated audio only,
stats surfaced via /api/media/sync/status and the sync_test.html page.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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
from fastapi.responses import FileResponse

logger = logging.getLogger("modules.media.cast_sync")

RATE = 44100
CHANNELS = 2
CHUNK_SECONDS = 0.5
CHUNK_FRAMES = int(RATE * CHUNK_SECONDS)
LEAD_SECONDS = 2.0       # first chunk plays this long after session start
AHEAD_SECONDS = 1.5      # send each chunk this early (receiver scheduling slack)
BUFFER_CHUNKS = 6        # kept for late joiners
SYNC_NAMESPACE = "urn:x-cast:zmm.sync"


def _gen_chunk(index: int) -> bytes:
    """Test-signal PCM for chunk ``index`` — pure function of the absolute
    sample position, so any receiver joining later gets a bit-identical
    timeline. Chord pad with a slow swell + a 1 kHz click every 2 s."""
    n0 = index * CHUNK_FRAMES
    t = (n0 + np.arange(CHUNK_FRAMES)) / RATE
    sig = (0.10 * np.sin(2 * np.pi * 220.0 * t)
           + 0.08 * np.sin(2 * np.pi * 277.18 * t)
           + 0.08 * np.sin(2 * np.pi * 329.63 * t))
    sig *= 0.7 + 0.3 * np.sin(2 * np.pi * 0.05 * t)
    ph = np.mod(t, 2.0)
    m = ph < 0.008
    if m.any():   # sharp exponentially-decaying tick — the sync "ruler"
        sig[m] += 0.85 * np.sin(2 * np.pi * 1000.0 * ph[m]) * np.exp(-ph[m] / 0.002)
    s16 = (np.clip(sig, -0.98, 0.98) * 32767).astype("<i2")
    return np.repeat(s16[:, None], CHANNELS, axis=1).tobytes()


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

        self._http_server = None                       # uvicorn.Server
        self._http_task: Optional[asyncio.Task] = None
        self._producer: Optional[asyncio.Task] = None
        self._launch_tasks: List[asyncio.Task] = []

        self.running = False
        self._epoch: float = 0.0
        self._buffer: List[bytes] = []                 # last N framed chunks
        self._receivers: Dict[str, _Receiver] = {}     # sid -> _Receiver
        self._pending: Dict[str, dict] = {}            # sid -> {player_id, name}
        self._controllers: Dict[str, object] = {}      # cast uuid -> controller

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

    # ------------------------------------------------------------------
    # Session control (called from routes)
    # ------------------------------------------------------------------
    async def start_session(self, player_ids: Optional[List[str]] = None,
                            group_id: str = "") -> dict:
        if not self.app_id:
            return {"success": False,
                    "error": "media.cast.sync.app_id not set — register the "
                             "receiver in the Cast console first (see static/cast/README.md)"}
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

        self._epoch = time.monotonic()
        self._buffer = []
        self._receivers = {}
        self._pending = {}
        self.running = True
        self._producer = asyncio.create_task(self._produce())

        launched, errors = [], {}
        for pid in player_ids:
            sid = uuid_mod.uuid4().hex[:12]
            name = self._player_name(pid)
            self._pending[sid] = {"player_id": pid, "name": name}
            try:
                task = asyncio.create_task(self._launch(pid, sid))
                self._launch_tasks.append(task)
                launched.append({"player_id": pid, "name": name, "sid": sid})
            except Exception as e:
                errors[pid] = str(e)
        logger.info(f"Cast sync session started for {len(launched)} device(s)")
        return {"success": True, "launched": launched, "errors": errors}

    async def stop_session(self) -> dict:
        self.running = False
        for t in self._launch_tasks:
            t.cancel()
        self._launch_tasks = []
        if self._producer:
            self._producer.cancel()
            self._producer = None
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
            devices.append({
                "sid": sid,
                "player_id": info["player_id"],
                "name": info["name"],
                "connected": r is not None,
                "trim_ms": self._trims.get(info["player_id"], 0),
                "stats": (r.stats if r else {}),
            })
        return {
            "running": self.running,
            "configured": bool(self.app_id),
            "http_port": self.http_port,
            "group_id": self._active_group,
            "elapsed_s": (time.monotonic() - self._epoch) if self.running else 0,
            "devices": devices,
        }

    async def set_trim(self, player_id: str, trim_ms: int) -> dict:
        trim_ms = max(-2000, min(2000, int(trim_ms)))
        self._trims[player_id] = trim_ms
        self._write_json(self._trims_file, self._trims)
        for r in self._receivers.values():
            if r.player_id == player_id:
                try:
                    await r.ws.send_json({"type": "trim", "trim_ms": trim_ms})
                except Exception as e:
                    logger.debug(f"trim push failed for {player_id}: {e}")
        return {"success": True, "player_id": player_id, "trim_ms": trim_ms}

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
