"""
EqStreamEngine — server-side graphic EQ for Cast targets.

Cast receivers expose no DSP API, so equalisation has to happen before the
audio reaches the device. When EQ is enabled for a Cast player the controller
routes playback through this engine instead of handing the device the source
URL directly: ffmpeg decodes the source (radio stream, Tidal AAC, the therapy
WAV — anything it can read) to raw PCM, the zmm_eq Rust biquad chain filters
it, and the result is served to the device as an endless WAV over
``/api/media/eq/stream/…`` — the same header trick the therapy stream uses.

The point of the Rust chain is LIVE control: slider changes swap biquad
coefficients atomically on the running stream (filter state preserved), so
dragging a band is heard on the speaker in well under a second with no
playback restart and no gap. Only two transitions need a restart of the
current track: turning EQ ON while an un-proxied stream is playing (the audio
path physically changes). Turning it OFF mid-stream just flips the chain to
bit-transparent bypass — seamless — and the next track starts direct again.

Enabled/preset/gains persist per player in ``data/media_eq.json``. The proxy
URL must be reachable BY THE DEVICE, so ``media.eq.base_url`` (or, as a
fallback, ``media.tidal.manifest_base_url``) has to point at this app on the
LAN — same rule as the Tidal DASH manifest route.

Costs while EQ is on, by design: the stream is re-encoded (Tidal lossless
becomes 44.1 kHz/16-bit PCM), the device reports no track duration (endless
WAV), and the stream dies with the app. EQ off = exactly the old direct-URL
behaviour, byte for byte.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import tempfile
from typing import AsyncIterator, Dict, List, Optional
from urllib.parse import quote

logger = logging.getLogger("modules.media.eq_stream")

try:
    import zmm_eq
    _HAVE_DSP = True
except ImportError:
    zmm_eq = None
    _HAVE_DSP = False
    logger.info("zmm_eq wheel not installed — Cast EQ disabled "
                "(build the image with --with-appender)")

RATE = 44100
CHUNK = 16384                    # bytes of s16le stereo ≈ 93 ms
BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
GAIN_LIMIT = 12.0                # UI range; the Rust chain clamps harder


def _wav_header() -> bytes:
    """Endless-stream WAV header: RIFF/data sizes pinned to 0xFFFFFFFF, the
    icecast trick — players read until the connection closes."""
    return (b"RIFF" + (0xFFFFFFFF).to_bytes(4, "little")
            + b"WAVEfmt " + (16).to_bytes(4, "little")
            + (1).to_bytes(2, "little")            # PCM
            + (2).to_bytes(2, "little")            # stereo
            + RATE.to_bytes(4, "little")
            + (RATE * 4).to_bytes(4, "little")     # byte rate
            + (4).to_bytes(2, "little")            # block align
            + (16).to_bytes(2, "little")           # bits
            + b"data" + (0xFFFFFFFF).to_bytes(4, "little"))


class _Session:
    """One live proxied stream: the ffmpeg decoder + its filter chain."""

    def __init__(self, token: str, source_url: str):
        self.token = token
        self.source_url = source_url
        self.chain = zmm_eq.EqChain(RATE)
        self.proc: Optional[asyncio.subprocess.Process] = None

    async def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass


class EqStreamEngine:
    def __init__(self, base_url: str = "", settings_file: str = "./data/media_eq.json",
                 fallback_base=None):
        self._base = (base_url or "").rstrip("/")
        # Zero-config default: () -> "http://<lan-ip>:<device-http-port>"
        # (see device_http.py). A configured base_url overrides it.
        self._fallback_base = fallback_base
        self._file = settings_file
        self._ffmpeg = shutil.which("ffmpeg") or ""
        self._settings: Dict[str, dict] = self._load()
        self._pending: Dict[str, tuple] = {}     # pid -> (token, source_url)
        self._sessions: Dict[str, _Session] = {}

    # ------------------------------------------------------------------
    # Capability / settings
    # ------------------------------------------------------------------
    def _resolve_base(self) -> str:
        """The URL prefix speakers fetch from: configured base_url if set,
        else the auto-derived plain-HTTP device listener address. An https
        override is ignored — devices reject the app's self-signed cert, and
        a proxy URL they can't fetch breaks ALL playback for EQ-on players."""
        if self._base and not self._base.startswith("https://"):
            return self._base
        if callable(self._fallback_base):
            try:
                return (self._fallback_base() or "").rstrip("/")
            except Exception:
                return ""
        return ""

    @property
    def available(self) -> bool:
        return _HAVE_DSP and bool(self._ffmpeg) and bool(self._resolve_base())

    def _unavailable_reason(self) -> str:
        if not _HAVE_DSP:
            return "DSP engine (zmm_eq) not installed in this build"
        if not self._ffmpeg:
            return "ffmpeg not found on the server"
        return ("Could not determine this app's LAN address — set "
                "media.eq.base_url so the speaker can reach this app")

    def status(self) -> dict:
        """Readiness summary for the Settings → Audio tab."""
        base = self._resolve_base()
        return {"available": self.available,
                "have_dsp": _HAVE_DSP,
                "have_ffmpeg": bool(self._ffmpeg),
                "base_url": base,
                "auto": not self._base,
                **({} if self.available
                   else {"reason": self._unavailable_reason()})}

    def _conf(self, player_id: str) -> dict:
        c = self._settings.get(player_id) or {}
        gains = c.get("gains")
        if not (isinstance(gains, list) and len(gains) == len(BANDS)):
            gains = [0.0] * len(BANDS)
        return {"enabled": bool(c.get("enabled")),
                "preset": c.get("preset") or "Flat",
                "gains": [max(-GAIN_LIMIT, min(GAIN_LIMIT, float(g))) for g in gains]}

    def wants(self, player_id: str, media_type: str = "") -> bool:
        """Should playback for this player be routed through the proxy?
        Announcements (tts) stay direct — they must be instant, and
        equalising a spoken alert serves nobody."""
        return (self.available
                and player_id.startswith("cast:")
                and media_type != "tts"
                and self._conf(player_id)["enabled"])

    def info(self, player_id: str) -> dict:
        c = self._conf(player_id)
        return {
            "mode": "bands",
            "bands": BANDS,
            "gain_limit": GAIN_LIMIT,
            "enabled": c["enabled"],
            "preset": c["preset"],
            "gains": c["gains"],
            "live": player_id in self._sessions,
            "available": self.available,
            **({} if self.available else {"reason": self._unavailable_reason()}),
        }

    def set(self, player_id: str, enabled: Optional[bool] = None,
            preset: Optional[str] = None, gains: Optional[List[float]] = None) -> dict:
        """Persist a change and apply it to any live session. Returns
        {"restart": bool} — True when the controller must replay the current
        track because the audio path itself changes (off→on with no proxy
        stream running)."""
        c = self._conf(player_id)
        was_enabled = c["enabled"]
        if enabled is not None:
            c["enabled"] = bool(enabled)
        if preset is not None:
            c["preset"] = str(preset)[:40]
        if gains is not None:
            if len(gains) != len(BANDS):
                raise ValueError(f"expected {len(BANDS)} gains")
            c["gains"] = [max(-GAIN_LIMIT, min(GAIN_LIMIT, float(g))) for g in gains]
        self._settings[player_id] = c
        self._save()

        session = self._sessions.get(player_id)
        if session:
            # Live path: retune / bypass the running chain, no interruption.
            if gains is not None:
                session.chain.set_gains(c["gains"])
            session.chain.set_enabled(c["enabled"])
        restart = bool(c["enabled"] and not was_enabled and not session)
        return {"restart": restart}

    # ------------------------------------------------------------------
    # Stream proxying
    # ------------------------------------------------------------------
    def wrap(self, player_id: str, url: str) -> tuple:
        """Register ``url`` as the next source for this player and return
        (proxy_url, content_type) for the device to play instead."""
        token = secrets.token_urlsafe(8)
        self._pending[player_id] = (token, url)
        proxy = (f"{self._resolve_base()}/api/media/eq/stream/"
                 f"{quote(player_id, safe='')}/{token}.wav")
        return proxy, "audio/wav"

    def knows(self, player_id: str, token: str) -> bool:
        """Is this a token we issued and haven't superseded? (route 404 guard)"""
        p = self._pending.get(player_id)
        a = self._sessions.get(player_id)
        return bool((p and p[0] == token) or (a and a.token == token))

    async def stream(self, player_id: str, token: str) -> AsyncIterator[bytes]:
        """The proxy stream body: decode → filter → endless WAV."""
        pending = self._pending.get(player_id)
        active = self._sessions.get(player_id)
        if pending and pending[0] == token:
            source_url = pending[1]
        elif active and active.token == token:
            # The device re-fetched the same URL (seek/reconnect) — restart
            # the decode from the top; for live radio that's simply "now".
            source_url = active.source_url
        else:
            raise KeyError("unknown or superseded stream token")

        # One stream per player: a new fetch supersedes whatever ran before.
        if active:
            await active.close()

        session = _Session(token, source_url)
        c = self._conf(player_id)
        session.chain.set_gains(c["gains"])
        session.chain.set_enabled(c["enabled"])
        self._sessions[player_id] = session
        self._pending.pop(player_id, None)

        cmd = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
        if source_url.startswith(("http://", "https://")):
            # Ride out radio-station hiccups instead of ending the "track".
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5"]
        cmd += ["-i", source_url, "-vn",
                "-ac", "2", "-ar", str(RATE), "-f", "s16le", "-"]

        logger.info(f"EQ stream start for {player_id} "
                    f"(gains={c['gains']}, source={source_url[:80]})")
        try:
            session.proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            self._sessions.pop(player_id, None)
            raise RuntimeError(f"ffmpeg start failed: {e}")

        # Drain stderr continuously — a full pipe would stall the decoder on
        # a long-running radio stream. Ends by itself at EOF.
        async def _drain():
            try:
                while True:
                    line = await session.proc.stderr.readline()
                    if not line:
                        return
                    logger.warning(f"EQ decoder [{player_id}]: "
                                   f"{line.decode(errors='replace').rstrip()}")
            except Exception:
                pass
        drain_task = asyncio.create_task(_drain())

        # Hold our own handle: close() nulls session.proc, and it can be called
        # from another task (a superseding fetch, or release() on stop) while
        # we are mid-read.
        proc = session.proc
        yield _wav_header()
        try:
            while True:
                chunk = await proc.stdout.read(CHUNK)
                if not chunk:
                    break
                # ~30 µs per chunk in Rust, GIL released — no executor needed.
                yield session.chain.process(chunk)
            rc = await proc.wait()
            logger.info(f"EQ stream finished for {player_id} (decoder rc={rc})")
        finally:
            drain_task.cancel()
            await session.close()
            if self._sessions.get(player_id) is session:
                self._sessions.pop(player_id, None)

    async def release(self, player_id: str) -> bool:
        """Tear down this player's proxy stream and invalidate its token.

        ``mc.stop()`` only stops the receiver: without this the decoder keeps
        running and ``knows()`` keeps saying yes, so the device — or the member
        that takes over a Cast group next — can re-fetch the same URL and
        resume a stream the user stopped. Returns True if a session was live."""
        self._pending.pop(player_id, None)
        session = self._sessions.pop(player_id, None)
        if session is None:
            return False
        await session.close()
        logger.info(f"EQ stream released for {player_id}")
        return True

    async def stop_all(self) -> None:
        for s in list(self._sessions.values()):
            await s.close()
        self._sessions.clear()
        self._pending.clear()

    # ------------------------------------------------------------------
    # Persistence (same atomic-replace pattern as the media prefs)
    # ------------------------------------------------------------------
    def _load(self) -> Dict[str, dict]:
        try:
            with open(self._file, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"Could not read {self._file}: {e}")
            return {}

    def _save(self) -> None:
        try:
            d = os.path.dirname(self._file) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, indent=2)
            os.replace(tmp, self._file)
        except Exception as e:
            logger.error(f"Could not write {self._file}: {e}")
