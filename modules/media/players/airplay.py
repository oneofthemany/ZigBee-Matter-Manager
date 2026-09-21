"""
AirPlay player provider, over RAOP (AirPlay audio) via pyatv.

Unlike Cast, Sonos and WiiM, an AirPlay receiver never fetches a URL itself:
this host decodes and pushes the audio. pyatv's decoder (miniaudio) takes MP3,
FLAC, WAV and Vorbis but not AAC, HLS or DASH, and of the formats it takes only
MP3 streams cleanly from a pipe (WAV needs a length ffmpeg cannot write to a
pipe; FLAC stalls). So every source goes through ffmpeg -> MP3 on a pipe ->
pyatv -> ALAC to the receiver. That covers radio (AAC/HLS), Tidal and TTS alike,
at the cost of one lossy transcode.

Because the stream is ours, playback state is tracked here rather than read
from the device. RAOP has no real pause (pyatv's stops the stream), so pause
stops and remembers the position, and resume restarts ffmpeg from it; a live
stream simply restarts. One stream per receiver; no native multi-room.

Receivers that require pairing (Apple TV with access control) pair once with a
PIN; credentials are persisted outside config.yaml through `credential_store`.
player_id is the pyatv device identifier ("airplay:<id>").
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import pyatv  # ImportError here is caught by MediaService (optional dep)
from pyatv.const import PairingRequirement, Protocol
from pyatv.interface import MediaMetadata

from modules.media.models import MediaItem, PlayerState, PlaybackState
from modules.media.players.base import PlayerProvider

logger = logging.getLogger("modules.media.airplay")

_LIVE_TYPES = {"radio", "live"}
SCAN_TIMEOUT = 5
STOP_GRACE_SECONDS = 3.0


def _pid(identifier: str) -> str:
    return f"airplay:{identifier}"


@dataclass
class _Session:
    item: MediaItem
    offset_ms: int = 0
    started_at: float = 0.0             # monotonic, while playing
    state: PlaybackState = PlaybackState.BUFFERING
    ended: bool = False
    stopping: bool = False
    task: Optional[asyncio.Task] = None
    proc: Optional[asyncio.subprocess.Process] = None
    error: str = ""

    @property
    def live(self) -> bool:
        return self.item.media_type in _LIVE_TYPES or not self.item.duration_ms

    def position_ms(self) -> int:
        if self.state == PlaybackState.PLAYING and self.started_at:
            return self.offset_ms + int((time.monotonic() - self.started_at) * 1000)
        return self.offset_ms


@dataclass
class _Device:
    config: object
    name: str
    model: str = ""
    atv: Optional[object] = None
    volume: float = 0.5
    muted_from: Optional[float] = None
    session: Optional[_Session] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class CredentialStore:
    """Where pairing credentials live. The default keeps them in memory only;
    MediaService passes a SecretsCredentialStore."""

    def __init__(self):
        self._creds: Dict[str, str] = {}

    def get(self, identifier: str) -> Optional[str]:
        return self._creds.get(identifier)

    async def set(self, identifier: str, credentials: str) -> None:
        self._creds[identifier] = credentials


class SecretsCredentialStore(CredentialStore):
    """Pairing credentials in the gitignored secrets file under
    `airplay.credentials`, never config.yaml — they authenticate this host to
    the receiver. The file is opened 0600 before anything is written."""

    def __init__(self, path: str):
        super().__init__()
        self._path = path
        try:
            import yaml
            with open(path, "r") as fh:
                data = (yaml.safe_load(fh) or {}).get("airplay") or {}
            self._creds = {str(k): str(v) for k, v in (data.get("credentials") or {}).items()}
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Could not read AirPlay credentials from {path}: {e}")

    async def set(self, identifier: str, credentials: str) -> None:
        await super().set(identifier, credentials)
        await asyncio.to_thread(self._write)

    def _write(self) -> None:
        import os
        import yaml
        from pathlib import Path

        path = Path(self._path)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if path.exists():
            try:
                existing = yaml.safe_load(path.read_text()) or {}
            except Exception:
                existing = {}
        existing.setdefault("airplay", {})["credentials"] = dict(self._creds)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            yaml.dump(existing, fh, default_flow_style=False, sort_keys=False)
        os.chmod(path, 0o600)


class AirPlayPlayerProvider(PlayerProvider):
    provider = "airplay"

    def __init__(self, device_hosts: Optional[List[str]] = None,
                 discovery: bool = True,
                 credential_store: Optional[CredentialStore] = None,
                 ffmpeg: Optional[str] = None,
                 rediscover_seconds: int = 300,
                 connect: Optional[Callable] = None):
        self._hosts = [h for h in (device_hosts or []) if h]
        self._discovery = discovery
        self._store = credential_store or CredentialStore()
        self._ffmpeg = ffmpeg or shutil.which("ffmpeg") or ""
        self._rediscover_seconds = rediscover_seconds
        self._connect = connect or pyatv.connect
        self._devices: Dict[str, _Device] = {}
        self._last_scan: Optional[float] = None  # None = never; 0.0 reads as recent on a freshly booted host.
        self._scan_task: Optional[asyncio.Task] = None
        self._pairings: Dict[str, object] = {}

    # Discovery
    async def start(self) -> None:
        if not self._ffmpeg:
            logger.warning("AirPlay: ffmpeg not found — playback will fail")
        await self._scan()

    async def stop(self) -> None:
        if self._scan_task is not None:
            self._scan_task.cancel()
        for dev in list(self._devices.values()):
            await self._end_session(dev)
            self._close(dev)

    def _maybe_rescan(self) -> None:
        """Background rescan when stale; never awaited by the poll, since a
        scan holds for its full timeout."""
        if (self._last_scan is not None
                and time.monotonic() - self._last_scan < self._rediscover_seconds):
            return
        if self._scan_task is not None and not self._scan_task.done():
            return
        self._scan_task = asyncio.create_task(self._scan())

    async def _scan(self) -> None:
        loop = asyncio.get_running_loop()
        found: List[object] = []
        try:
            if self._discovery:
                found += await pyatv.scan(loop, timeout=SCAN_TIMEOUT, protocol=Protocol.RAOP)
            if self._hosts:
                found += await pyatv.scan(loop, timeout=SCAN_TIMEOUT, hosts=self._hosts,
                                          protocol=Protocol.RAOP)
        except Exception as e:
            logger.warning(f"AirPlay scan failed: {e}")
        finally:
            self._last_scan = time.monotonic()

        new = []
        for conf in found:
            ident = getattr(conf, "identifier", None)
            if not ident or conf.get_service(Protocol.RAOP) is None:
                continue
            creds = self._store.get(ident)
            if creds:
                conf.set_credentials(Protocol.RAOP, creds)
            dev = self._devices.get(ident)
            if dev is None:
                self._devices[ident] = _Device(config=conf, name=conf.name, model=_model(conf))
                new.append(conf.name)
            elif dev.atv is None:
                # A receiver that moved address is reachable again once its
                # fresh config replaces the stale one; a live connection keeps
                # the config it connected with.
                dev.config, dev.name = conf, conf.name
        if new:
            logger.info("AirPlay discovered: " + ", ".join(sorted(new)))

    def _device(self, player_id: str) -> _Device:
        dev = self._devices.get(player_id.split(":", 1)[-1])
        if dev is None:
            raise ValueError(f"Unknown AirPlay player {player_id}")
        return dev

    # Connection
    async def _ensure_connected(self, dev: _Device):
        if dev.atv is not None:
            return dev.atv
        loop = asyncio.get_running_loop()
        atv = await self._connect(dev.config, loop, protocol=Protocol.RAOP)
        try:
            atv.listener = _ConnectionListener(self, dev)
        except Exception:
            pass
        dev.atv = atv
        try:
            dev.volume = float(atv.audio.volume) / 100.0
        except Exception:
            pass
        return atv

    def _close(self, dev: _Device) -> None:
        atv, dev.atv = dev.atv, None
        if atv is not None:
            try:
                atv.close()
            except Exception as e:
                logger.debug(f"AirPlay close {dev.name}: {e}")

    # State
    async def list_players(self) -> List[PlayerState]:
        self._maybe_rescan()
        return [self._state(ident, dev) for ident, dev in self._devices.items()]

    async def get_state(self, player_id: str) -> Optional[PlayerState]:
        dev = self._devices.get(player_id.split(":", 1)[-1])
        if dev is None:
            return None
        return self._state(player_id.split(":", 1)[-1], dev)

    def _state(self, ident: str, dev: _Device) -> PlayerState:
        s = dev.session
        needs_pairing = self.pairing_required(_pid(ident))
        state = s.state if s else PlaybackState.IDLE
        return PlayerState(
            player_id=_pid(ident),
            provider=self.provider,
            name=dev.name,
            # A receiver that needs pairing is listed but cannot be targeted yet.
            available=not needs_pairing,
            state=state,
            volume=0.0 if dev.muted_from is not None else dev.volume,
            muted=dev.muted_from is not None,
            title=s.item.title if s else "",
            artist=s.item.artist if s else "",
            artwork_url=s.item.artwork_url if s else "",
            media_type=s.item.media_type if s else "",
            ended=bool(s and s.ended),
            position_ms=s.position_ms() if s else 0,
            duration_ms=0 if not s or s.live else s.item.duration_ms,
        )

    # Pairing
    def pairing_required(self, player_id: str) -> bool:
        dev = self._devices.get(player_id.split(":", 1)[-1])
        if dev is None:
            return False
        service = dev.config.get_service(Protocol.RAOP)
        return (service is not None and service.pairing == PairingRequirement.Mandatory
                and not self._store.get(player_id.split(":", 1)[-1]))

    async def pair_begin(self, player_id: str) -> dict:
        ident = player_id.split(":", 1)[-1]
        dev = self._device(player_id)
        await self.pair_cancel(player_id)
        handler = await pyatv.pair(dev.config, Protocol.RAOP, asyncio.get_running_loop())
        await handler.begin()
        self._pairings[ident] = handler
        return {"device_provides_pin": bool(handler.device_provides_pin)}

    async def pair_finish(self, player_id: str, pin: str) -> bool:
        ident = player_id.split(":", 1)[-1]
        dev = self._device(player_id)
        handler = self._pairings.pop(ident, None)
        if handler is None:
            raise ValueError("No pairing in progress — start pairing first")
        try:
            handler.pin(pin)
            await handler.finish()
            if not handler.has_paired:
                return False
            creds = handler.service.credentials
            await self._store.set(ident, creds)
            dev.config.set_credentials(Protocol.RAOP, creds)
            self._close(dev)            # reconnect with the new credentials
            return True
        finally:
            await handler.close()

    async def pair_cancel(self, player_id: str) -> None:
        handler = self._pairings.pop(player_id.split(":", 1)[-1], None)
        if handler is not None:
            await handler.close()

    # Playback
    def _ffmpeg_cmd(self, url: str, offset_ms: int) -> List[str]:
        cmd = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
        if url.startswith(("http://", "https://")):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if offset_ms > 0:
            cmd += ["-ss", f"{offset_ms / 1000:.3f}"]
        return cmd + ["-i", url, "-vn", "-ac", "2", "-ar", "44100",
                      "-c:a", "libmp3lame", "-b:a", "320k", "-f", "mp3", "-"]

    async def play_url(self, player_id: str, item: MediaItem) -> None:
        await self._play(self._device(player_id), item, 0)

    async def _play(self, dev: _Device, item: MediaItem, offset_ms: int) -> None:
        if not self._ffmpeg:
            raise RuntimeError("ffmpeg is required for AirPlay playback")
        if self.pairing_required(_pid(dev.config.identifier)):
            raise RuntimeError(f"{dev.name} needs pairing — pair it in Settings → APIs → AirPlay")
        async with dev.lock:
            await self._end_session(dev)
            atv = await self._ensure_connected(dev)
            session = _Session(item=item, offset_ms=offset_ms)
            dev.session = session
            session.task = asyncio.create_task(self._stream(dev, atv, session))

    async def _stream(self, dev: _Device, atv, session: _Session) -> None:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._ffmpeg_cmd(session.item.url, session.offset_ms),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            session.proc = proc
            meta = MediaMetadata(
                title=session.item.title or None,
                artist=session.item.artist or None,
                duration=None if session.live else session.item.duration_ms / 1000,
            )
            session.state = PlaybackState.PLAYING
            session.started_at = time.monotonic()
            await atv.stream.stream_file(proc.stdout, metadata=meta)
            if not session.stopping:
                # The stream ran out on its own: a finished track, or a live
                # stream that dropped. Only a finite track counts as ended.
                session.offset_ms = session.position_ms()
                session.state = PlaybackState.IDLE
                session.ended = not session.live
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not session.stopping:
                logger.warning(f"AirPlay stream to {dev.name} failed: {e}")
                session.error = str(e)
                session.state = PlaybackState.IDLE
                # A dead connection is rebuilt on the next play.
                self._close(dev)
        finally:
            if proc is not None:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                # Reaping alone leaves the stdout pipe open until garbage
                # collection — one leaked descriptor per pause or track change.
                # Draining to EOF closes it; the process is already dead, so
                # this returns as soon as the buffered tail is read.
                try:
                    await asyncio.wait_for(proc.communicate(), STOP_GRACE_SECONDS)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass

    async def _end_session(self, dev: _Device) -> None:
        """Stop the current stream and wait for it to wind down, so the next
        stream_file does not collide with pyatv's one-stream-per-device guard."""
        s = dev.session
        if s is None or s.task is None or s.task.done():
            return
        s.stopping = True
        s.offset_ms = s.position_ms()
        if dev.atv is not None:
            try:
                await dev.atv.remote_control.stop()
            except Exception as e:
                logger.debug(f"AirPlay stop {dev.name}: {e}")
        if s.proc is not None and s.proc.returncode is None:
            try:
                s.proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(asyncio.shield(s.task), STOP_GRACE_SECONDS)
        except (asyncio.TimeoutError, Exception):
            s.task.cancel()
            try:
                await s.task
            except (asyncio.CancelledError, Exception):
                pass

    async def pause(self, player_id: str) -> None:
        dev = self._device(player_id)
        async with dev.lock:
            s = dev.session
            if s is None or s.state not in (PlaybackState.PLAYING, PlaybackState.BUFFERING):
                return
            await self._end_session(dev)
            if s.live:
                s.offset_ms = 0
            s.state = PlaybackState.PAUSED

    async def resume(self, player_id: str) -> None:
        dev = self._device(player_id)
        s = dev.session
        if s is None or s.state != PlaybackState.PAUSED:
            return
        await self._play(dev, s.item, 0 if s.live else s.offset_ms)

    async def stop_playback(self, player_id: str) -> None:
        dev = self._device(player_id)
        async with dev.lock:
            await self._end_session(dev)
            dev.session = None

    async def set_volume(self, player_id: str, level: float) -> None:
        dev = self._device(player_id)
        level = max(0.0, min(1.0, float(level)))
        atv = await self._ensure_connected(dev)
        await atv.audio.set_volume(level * 100.0)
        dev.volume, dev.muted_from = level, None

    async def set_muted(self, player_id: str, muted: bool) -> None:
        # RAOP has no mute; volume 0 and back is the equivalent.
        dev = self._device(player_id)
        atv = await self._ensure_connected(dev)
        if muted and dev.muted_from is None:
            dev.muted_from = dev.volume
            await atv.audio.set_volume(0.0)
        elif not muted and dev.muted_from is not None:
            restore, dev.muted_from = dev.muted_from, None
            await atv.audio.set_volume(restore * 100.0)
            dev.volume = restore


class _ConnectionListener:
    """pyatv DeviceListener: drop the cached connection when it goes away."""

    def __init__(self, provider: AirPlayPlayerProvider, dev: _Device):
        self._provider, self._dev = provider, dev

    def connection_lost(self, exception) -> None:
        logger.info(f"AirPlay {self._dev.name} connection lost: {exception}")
        self._dev.atv = None

    def connection_closed(self) -> None:
        self._dev.atv = None


def _model(conf) -> str:
    try:
        info = conf.device_info
        return str(getattr(info, "raw_model", None) or getattr(info, "model", "") or "")
    except Exception:
        return ""
