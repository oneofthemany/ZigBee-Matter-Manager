"""
KokoroTTS — in-process neural TTS for the therapy SPA (default engine).

Runs the Kokoro-82M model (Apache-2.0) directly inside ZMM via kokoro-onnx +
onnxruntime — no sidecar container, no Wyoming hop. Speed is a native model
parameter (length control), so unlike the wyoming-piper path there is no
client-side time-stretch approximation. Pitch is applied client-side by the
SPA (playbackRate), never here; it only participates in the cache key.

The ~340 MB model files are NOT shipped in the image. They download on demand
into data/tts_models/ (a persistent volume) when the operator clicks
"Download voice model" on the therapy page — surfaced via the
/api/tts/setup/* endpoints. Everything privileged/expensive is user-triggered.

Same duck-typed API as TherapyTTS (status/voices/synthesize + setup_*), so
routes and the SPA are engine-agnostic. Select per config:

Config (config.yaml):
  media:
    therapy:
      enabled: true
      engine: kokoro            # kokoro (in-process) | wyoming (external piper)
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("modules.media.kokoro_tts")

MODEL_URL = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
             "model-files-v1.0/kokoro-v1.0.onnx")
VOICES_URL = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
              "model-files-v1.0/voices-v1.0.bin")
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"
DOWNLOAD_MB = 340          # shown in the UI before the operator commits
DEFAULT_VOICE = "af_heart"

_CACHE_MAX_FILES = 512
_CACHE_TRIM_BATCH = 64
_MAX_LOG = 300

# Kokoro voice ids are <lang><gender>_<name>: af_heart = American female.
_LANG_PREFIX = {
    "a": ("en-US", "en-us"), "b": ("en-GB", "en-gb"),
    "e": ("es-ES", "es"),    "f": ("fr-FR", "fr-fr"),
    "h": ("hi-IN", "hi"),    "i": ("it-IT", "it"),
    "j": ("ja-JP", "ja"),    "p": ("pt-BR", "pt-br"),
    "z": ("zh-CN", "cmn"),
}

# The SPA may still request wyoming-piper voice names (older cached settings);
# map them onto the closest Kokoro voice instead of failing.
_PIPER_ALIASES = {
    "en_US": {"female": "af_heart", "male": "am_michael"},
    "en_GB": {"female": "bf_emma", "male": "bm_george"},
    "es_ES": {"female": "ef_dora", "male": "em_alex"},
    "fr_FR": {"female": "ff_siwis", "male": "ff_siwis"},
    "it_IT": {"female": "if_sara", "male": "im_nicola"},
    "pt_BR": {"female": "pf_dora", "male": "pm_alex"},
    "zh_CN": {"female": "zf_xiaobei", "male": "zm_yunjian"},
}
_FEMALE_HINTS = ("amy", "alba", "siwis", "huayan", "jenny", "cori",
                 "kathleen", "lessac", "libritts", "southern_english_female")


class KokoroTTS:
    def __init__(self, config: dict, cache_dir: str = "data/tts_cache",
                 model_dir: str = "data/tts_models"):
        config = config or {}
        self.enabled = config.get("enabled", True)
        self.model_dir = Path(config.get("model_dir", model_dir))
        self.cache_dir = Path(cache_dir)
        self._engine = None            # kokoro_onnx.Kokoro, loaded lazily
        self._engine_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(1)   # onnx synth is CPU-bound; serialise
        self._job: Optional[Dict[str, Any]] = None

    # ── Model files ─────────────────────────────────────────────────────

    @property
    def _model_path(self) -> Path:
        return self.model_dir / MODEL_FILE

    @property
    def _voices_path(self) -> Path:
        return self.model_dir / VOICES_FILE

    def model_ready(self) -> bool:
        return self._model_path.exists() and self._voices_path.exists()

    async def _get_engine(self):
        if self._engine is not None:
            return self._engine
        async with self._engine_lock:
            if self._engine is None:
                if not self.model_ready():
                    raise RuntimeError("Kokoro model files not downloaded")
                from kokoro_onnx import Kokoro
                t0 = time.monotonic()
                self._engine = await asyncio.to_thread(
                    Kokoro, str(self._model_path), str(self._voices_path))
                logger.info("Kokoro model loaded in %.1fs",
                            time.monotonic() - t0)
        return self._engine

    # ── Public API (mirrors TherapyTTS) ─────────────────────────────────

    async def status(self) -> bool:
        """True when the engine can synthesize (model files present)."""
        return self.enabled and self.model_ready()

    async def voices(self) -> List[dict]:
        """Voice catalog in the SPA's shape."""
        engine = await self._get_engine()
        out = []
        for name in sorted(engine.get_voices()):
            lang = _LANG_PREFIX.get(name[:1], ("en-US", "en-us"))[0]
            gender = ("female" if len(name) > 1 and name[1] == "f"
                      else "male" if len(name) > 1 and name[1] == "m"
                      else "unknown")
            label = name.split("_", 1)[-1].replace("_", " ").title()
            out.append({
                "id": name,
                "label": f"{label} ({lang} {gender})",
                "lang": lang,
                "gender": gender,
                "quality": "kokoro-82M",
                "installed": True,     # single model file covers all voices
            })
        return out

    async def synthesize(self, text: str, voice: str = DEFAULT_VOICE,
                         speed: float = 1.0, pitch: float = 1.0) -> bytes:
        """Return WAV bytes, serving from the disk cache when possible.

        pitch only participates in the cache key (the SPA shifts pitch
        locally but keys its expectations on it) — it never changes audio.
        """
        speed = max(0.5, min(2.0, float(speed or 1.0)))
        voice = self._resolve_voice(voice)
        key = hashlib.md5(f"kokoro:{voice}:{speed}:{pitch}:{text}".encode()).hexdigest()
        cache_path = self.cache_dir / f"{key}.wav"
        if cache_path.exists():
            return cache_path.read_bytes()

        engine = await self._get_engine()
        espeak_lang = _LANG_PREFIX.get(voice[:1], ("en-US", "en-us"))[1]
        async with self._sem:
            samples, rate = await asyncio.to_thread(
                engine.create, text, voice=voice, speed=speed, lang=espeak_lang)

        import numpy as np
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(pcm.tobytes())
        wav = buf.getvalue()

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(wav)
            self._trim_cache()
        except OSError as exc:
            logger.warning("TTS cache write failed: %s", exc)
        return wav

    def _resolve_voice(self, voice: Optional[str]) -> str:
        voice = (voice or "").strip()
        if not voice:
            return DEFAULT_VOICE
        # Native Kokoro id: <lang><f|m>_<name>
        if "_" in voice and voice[:1] in _LANG_PREFIX and voice[1:2] in "fm":
            return voice
        # wyoming-piper style: en_GB-southern_english_female-low
        locale = voice.split("-", 1)[0]
        lname = voice.lower()
        gender = "female" if any(h in lname for h in _FEMALE_HINTS) else "male"
        mapped = _PIPER_ALIASES.get(locale, {}).get(gender)
        if mapped:
            return mapped
        logger.debug("Unknown voice %r — falling back to %s", voice, DEFAULT_VOICE)
        return DEFAULT_VOICE

    def _trim_cache(self) -> None:
        files = sorted(self.cache_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        if len(files) > _CACHE_MAX_FILES:
            for stale in files[:_CACHE_TRIM_BATCH]:
                stale.unlink(missing_ok=True)

    # ── Setup (model download; /api/tts/setup/*) ────────────────────────

    def setup_status(self) -> Dict[str, Any]:
        return {
            "engine": "kokoro",
            "ready": self.model_ready(),
            "installable": True,
            "download_mb": DOWNLOAD_MB,
            "model_dir": str(self.model_dir),
            "job": self._job,
        }

    def setup_start(self) -> Dict[str, Any]:
        if self._busy():
            return {"success": False, "error": "A download is already running."}
        if self.model_ready():
            return {"success": False, "error": "Model is already downloaded."}
        asyncio.create_task(self._run_download())
        return {"success": True, "started": True}

    def setup_job(self) -> Dict[str, Any]:
        return self._job or {"status": "idle"}

    async def _run_download(self):
        self._job_start("download", f"{MODEL_URL} + {VOICES_URL}")
        try:
            import httpx
            self.model_dir.mkdir(parents=True, exist_ok=True)
            async with httpx.AsyncClient(follow_redirects=True,
                                         timeout=httpx.Timeout(30, read=120)) as cx:
                for url, dest in ((MODEL_URL, self._model_path),
                                  (VOICES_URL, self._voices_path)):
                    if dest.exists():
                        continue
                    await self._download_file(cx, url, dest)
            # Load once now so the first synthesis doesn't pay the cost.
            await self._get_engine()
            self._job_log("Kokoro TTS is ready.")
            self._job_finish(True, None)
        except Exception as e:
            logger.error("Kokoro model download failed: %s", e)
            self._job_finish(False, str(e))

    async def _download_file(self, cx, url: str, dest: Path):
        part = dest.with_suffix(dest.suffix + ".part")
        self._job_log(f"Downloading {dest.name}…")
        try:
            async with cx.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length") or 0)
                done = 0
                last_pct = -10
                with part.open("wb") as f:
                    async for chunk in resp.aiter_bytes(1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = int(done * 100 / total)
                            if pct >= last_pct + 10:
                                last_pct = pct
                                self._job_log(f"{dest.name}: {pct}% "
                                              f"({done >> 20}/{total >> 20} MB)")
            part.rename(dest)
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    # ── Job bookkeeping ─────────────────────────────────────────────────

    def _job_start(self, action: str, command: str):
        self._job = {"action": action, "status": "running",
                     "log": [], "started": time.time(), "command": command}

    def _job_log(self, line: str):
        if not line or not self._job:
            return
        self._job["log"].append(line)
        if len(self._job["log"]) > _MAX_LOG:
            self._job["log"] = self._job["log"][-_MAX_LOG:]

    def _job_finish(self, ok: bool, err: Optional[str]):
        if not self._job:
            return
        self._job["status"] = "done" if ok else "error"
        if err:
            self._job_log(err)
        self._job["finished"] = time.time()

    def _busy(self) -> bool:
        return bool(self._job and self._job.get("status") == "running")
