"""
TherapyTTS — Wyoming (external piper) backend for the therapy SPA.

Legacy/alternative engine: talks the Wyoming protocol to a wyoming-piper
container (e.g. the one HA voice hosts already run), assembles the streamed
PCM into a WAV, and caches results on disk keyed by (voice, speed, pitch,
text). The default engine is the in-process KokoroTTS — see
modules/media/kokoro_tts.py and the create_therapy_tts() factory below.

Piper applies speech speed via length_scale, which wyoming-piper does not
expose per-request, so speed != 1.0 is approximated here with a WSOLA
time-stretch (pitch-preserving). numpy is required for the stretch; without
it audio is returned at natural speed. Pitch is applied client-side by the
SPA (playbackRate), never here.

Config (config.yaml):
  media:
    therapy:
      enabled: true
      engine: wyoming       # kokoro (default, in-process) | wyoming
      wyoming:
        host: "127.0.0.1"   # wyoming-piper server, host network
        port: 10200
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import time
import wave
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("modules.media.therapy_tts")

try:
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None

_CACHE_MAX_FILES = 512
_CACHE_TRIM_BATCH = 64

# Piper voice names carry no gender metadata over Wyoming; same heuristic the
# original piper sidecar used.
_FEMALE_HINTS = ("amy", "alba", "siwis", "huayan", "jenny", "cori",
                 "kathleen", "lessac", "libritts", "southern_english_female")
_MALE_HINTS = ("danny", "alan", "thorsten", "davefx", "riccardo", "faber",
               "dmitri", "ryan", "joe", "northern_english_male")


class TherapyTTS:
    def __init__(self, config: dict, cache_dir: str = "data/tts_cache"):
        config = config or {}
        self.enabled = config.get("enabled", True)
        # media.therapy.wyoming.{host,port}; legacy piper_host/piper_port
        # (pre-kokoro configs) still honoured.
        wy = config.get("wyoming") or {}
        self.host = wy.get("host", config.get("piper_host", "127.0.0.1"))
        self.port = int(wy.get("port", config.get("piper_port", 10200)))
        self.cache_dir = Path(cache_dir)
        self._sem = asyncio.Semaphore(2)
        self._stretch_warned = False

    # ── Wyoming protocol ────────────────────────────────────────────────
    # Header line {"type", "version", "data_length"?, "payload_length"?}
    # + "\n" + data JSON bytes + payload bytes (matching wyoming's
    # async_write_event; data inline in the header is read-side only).

    @staticmethod
    async def _write_event(writer, etype: str, data: dict = None,
                           payload: bytes = None) -> None:
        header = {"type": etype, "version": "1.10.0"}
        data_bytes = (json.dumps(data, ensure_ascii=False).encode()
                      if data else None)
        if data_bytes:
            header["data_length"] = len(data_bytes)
        if payload:
            header["payload_length"] = len(payload)
        writer.write(json.dumps(header, ensure_ascii=False).encode() + b"\n")
        if data_bytes:
            writer.write(data_bytes)
        if payload:
            writer.write(payload)
        await writer.drain()

    @staticmethod
    async def _read_event(reader) -> Optional[dict]:
        line = await reader.readline()
        if not line:
            return None
        header = json.loads(line)
        data = header.get("data") or {}
        data_length = header.get("data_length")
        if data_length:
            extra = await reader.readexactly(data_length)
            data = {**data, **json.loads(extra)}
        payload = None
        payload_length = header.get("payload_length")
        if payload_length:
            payload = await reader.readexactly(payload_length)
        return {"type": header.get("type"), "data": data, "payload": payload}

    async def _connect(self, timeout: float):
        return await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=timeout)

    # ── Public API ──────────────────────────────────────────────────────

    async def status(self) -> bool:
        """True when the piper container answers a describe round-trip."""
        try:
            reader, writer = await self._connect(2.0)
            try:
                await self._write_event(writer, "describe")
                event = await asyncio.wait_for(self._read_event(reader), timeout=3.0)
                return bool(event and event["type"] == "info")
            finally:
                writer.close()
        except (OSError, asyncio.TimeoutError, json.JSONDecodeError):
            return False

    async def voices(self) -> List[dict]:
        """Voice catalog from wyoming-piper, in the SPA's shape."""
        reader, writer = await self._connect(3.0)
        try:
            await self._write_event(writer, "describe")
            event = await asyncio.wait_for(self._read_event(reader), timeout=5.0)
        finally:
            writer.close()
        if not event or event["type"] != "info":
            return []
        out = []
        for program in event["data"].get("tts", []):
            for v in program.get("voices", []):
                name = v.get("name", "")
                lname = name.lower()
                gender = ("female" if any(h in lname for h in _FEMALE_HINTS)
                          else "male" if any(h in lname for h in _MALE_HINTS)
                          else "unknown")
                langs = v.get("languages") or ["en"]
                out.append({
                    "id": name,
                    "label": v.get("description") or name.replace("-", " ").replace("_", " ").title(),
                    "lang": langs[0].replace("_", "-"),
                    "gender": gender,
                    "quality": name.rsplit("-", 1)[-1] if "-" in name else "medium",
                    "installed": v.get("installed", False),
                })
        # Installed voices synthesize instantly; the rest download on demand.
        out.sort(key=lambda v: (not v["installed"], v["id"]))
        return out

    async def synthesize(self, text: str, voice: str = "en_GB-southern_english_female-low",
                         speed: float = 1.0, pitch: float = 1.0) -> bytes:
        """Return WAV bytes, serving from the disk cache when possible.

        pitch only participates in the cache key (the SPA shifts pitch
        locally but keys its expectations on it) — it never changes audio.
        """
        speed = max(0.3, min(2.0, float(speed or 1.0)))
        key = hashlib.md5(f"{voice}:{speed}:{pitch}:{text}".encode()).hexdigest()
        cache_path = self.cache_dir / f"{key}.wav"
        if cache_path.exists():
            return cache_path.read_bytes()

        async with self._sem:
            pcm, rate, width, channels = await self._synthesize_pcm(text, voice)
        if speed != 1.0:
            pcm = self._time_stretch(pcm, rate, width, channels, speed)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(width)
            wf.setframerate(rate)
            wf.writeframes(pcm)
        wav = buf.getvalue()

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(wav)
            self._trim_cache()
        except OSError as exc:
            logger.warning("TTS cache write failed: %s", exc)
        return wav

    # ── Internals ───────────────────────────────────────────────────────

    async def _synthesize_pcm(self, text: str, voice: str):
        # Generous timeout: wyoming-piper downloads a voice (~80 MB) the
        # first time it is requested.
        reader, writer = await self._connect(3.0)
        try:
            await self._write_event(writer, "synthesize",
                                    {"text": text, "voice": {"name": voice}})
            pcm = bytearray()
            rate, width, channels = 22050, 2, 1
            deadline = time.monotonic() + 120
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("piper synthesis timed out")
                event = await asyncio.wait_for(self._read_event(reader),
                                               timeout=remaining)
                if event is None:
                    break
                if event["type"] == "audio-start":
                    d = event["data"]
                    rate = d.get("rate", rate)
                    width = d.get("width", width)
                    channels = d.get("channels", channels)
                elif event["type"] == "audio-chunk" and event["payload"]:
                    pcm.extend(event["payload"])
                elif event["type"] == "audio-stop":
                    break
                elif event["type"] == "error":
                    raise RuntimeError(event["data"].get("text", "piper error"))
            if not pcm:
                raise RuntimeError("piper produced no audio")
            return bytes(pcm), rate, width, channels
        finally:
            writer.close()

    def _time_stretch(self, pcm: bytes, rate: int, width: int,
                      channels: int, speed: float) -> bytes:
        """Pitch-preserving WSOLA stretch (speed < 1 slows the speech)."""
        if _np is None or width != 2 or channels != 1:
            if not self._stretch_warned:
                logger.warning(
                    "TTS speed control unavailable (numpy missing or non-mono "
                    "PCM) — returning natural-speed audio")
                self._stretch_warned = True
            return pcm

        x = _np.frombuffer(pcm, dtype=_np.int16).astype(_np.float64)
        win = max(2, int(0.030 * rate)) & ~1        # 30 ms analysis window
        if len(x) < win * 2:
            return pcm
        hop_syn = win // 2
        hop_ana = max(1, int(round(hop_syn * speed)))
        tol = int(0.005 * rate)                     # ±5 ms alignment search
        window = _np.hanning(win)

        n_frames = max(1, (len(x) - win - tol) // hop_ana)
        out = _np.zeros(n_frames * hop_syn + win)
        norm = _np.zeros_like(out)

        prev_start = 0
        for i in range(n_frames):
            target = i * hop_ana
            if i == 0:
                start = 0
            else:
                # Natural continuation of the previous frame, aligned by
                # cross-correlation within ±tol around the analysis target.
                desired = x[prev_start + hop_syn: prev_start + hop_syn + hop_syn]
                lo = max(0, target - tol)
                hi = min(len(x) - win, target + tol)
                seg = x[lo: hi + hop_syn]
                corr = _np.correlate(seg, desired, mode="valid")
                start = lo + int(_np.argmax(corr))
            frame = x[start: start + win] * window
            pos = i * hop_syn
            out[pos: pos + win] += frame
            norm[pos: pos + win] += window
            prev_start = start

        norm[norm < 1e-8] = 1.0
        out = _np.clip(out / norm, -32768, 32767)
        return out.astype(_np.int16).tobytes()

    def _trim_cache(self) -> None:
        files = sorted(self.cache_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        if len(files) > _CACHE_MAX_FILES:
            for stale in files[:_CACHE_TRIM_BATCH]:
                stale.unlink(missing_ok=True)

    # ── Setup (/api/tts/setup/*) — nothing to install for this engine ───

    def setup_status(self) -> dict:
        return {"engine": "wyoming", "ready": None, "installable": False,
                "hint": f"Expects a wyoming-piper server at "
                        f"{self.host}:{self.port} (media.therapy config)."}

    def setup_start(self) -> dict:
        return {"success": False,
                "error": "The wyoming engine uses an external piper server — "
                         "start it on the host, or switch media.therapy.engine "
                         "to 'kokoro' for the built-in engine."}

    def setup_job(self) -> dict:
        return {"status": "idle"}


def create_therapy_tts(config: dict):
    """Engine factory for the therapy TTS (media.therapy.engine)."""
    engine = ((config or {}).get("engine") or "kokoro").lower()
    if engine == "wyoming":
        return TherapyTTS(config)
    if engine != "kokoro":
        logger.warning("Unknown media.therapy.engine %r — using kokoro", engine)
    from modules.media.kokoro_tts import KokoroTTS
    return KokoroTTS(config)
