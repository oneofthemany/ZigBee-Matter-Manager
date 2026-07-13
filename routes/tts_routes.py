"""
Neural TTS routes for the therapy SPA (static/therapy).

The SPA's contract predates ZMM (it was the standalone Node server):
POST /api/tts returns audio/wav; /api/tts/voices and /api/tts/status shape
their JSON exactly as App.jsx expects, so the vendored frontend runs unpatched.
"""
import logging
from typing import Optional

from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger("routes.tts")

DEFAULT_VOICE = "en_GB-southern_english_female-low"


class TTSBody(BaseModel):
    text: str
    voice: Optional[str] = None
    speed: Optional[float] = 1.0
    pitch: Optional[float] = 1.0


def register_tts_routes(app: FastAPI, get_tts):

    def _svc():
        svc = get_tts()
        if not svc or not svc.enabled:
            return None
        return svc

    @app.post("/api/tts")
    async def tts_synthesize(body: TTSBody):
        svc = _svc()
        if not svc:
            return Response('{"error": "TTS not enabled"}', status_code=503,
                            media_type="application/json")
        text = (body.text or "").strip()
        if not text:
            return Response('{"error": "text is required"}', status_code=400,
                            media_type="application/json")
        try:
            wav = await svc.synthesize(text, voice=body.voice or DEFAULT_VOICE,
                                       speed=body.speed or 1.0,
                                       pitch=body.pitch or 1.0)
            return Response(content=wav, media_type="audio/wav")
        except Exception as exc:
            logger.warning("TTS synthesis failed: %s", exc)
            return Response('{"error": "TTS service unavailable"}',
                            status_code=503, media_type="application/json")

    @app.get("/api/tts/voices")
    async def tts_voices():
        svc = _svc()
        if not svc:
            return {"available": False, "voices": []}
        try:
            voices = await svc.voices()
            return {"available": True, "voices": voices, "count": len(voices)}
        except Exception as exc:
            logger.warning("TTS voice listing failed: %s", exc)
            return {"available": False, "voices": []}

    @app.get("/api/tts/status")
    async def tts_status():
        svc = _svc()
        return {"connected": bool(svc) and await svc.status()}

    # ── Engine setup (kokoro: model download; wyoming: external server) ─────

    @app.get("/api/tts/setup/status")
    async def tts_setup_status():
        svc = get_tts()
        if not svc:
            return {"installable": False}
        return svc.setup_status()

    @app.post("/api/tts/setup/start")
    async def tts_setup_start():
        svc = get_tts()
        if not svc:
            return {"success": False, "error": "TTS not configured"}
        return svc.setup_start()

    @app.get("/api/tts/setup/job")
    async def tts_setup_job():
        svc = get_tts()
        return svc.setup_job() if svc else {"status": "idle"}

    # ── Endless soundscape stream for casting to media players ──────────
    # Played on Cast/WiiM through POST /api/media/play, exactly like a
    # radio station URL — the server synthesizes the therapy bed + speech.

    @app.get("/api/therapy/stream")
    async def therapy_stream(mode: str = "relaxation", speech: int = 1,
                             voice: str = "", speed: float = 0.75,
                             pitch: float = 1.0, interval: int = 45,
                             breath: str = "4-7-8"):
        from modules.media.therapy_stream import TherapyStream
        svc = _svc()
        tts_ok = bool(svc) and await svc.status()
        ts = TherapyStream(svc if tts_ok else None, mode,
                           speech=bool(speech) and tts_ok,
                           voice=voice or "af_heart", speech_speed=speed,
                           pitch=pitch, interval=interval, breath=breath)
        return StreamingResponse(ts.wav_stream(), media_type="audio/wav",
                                 headers={"Cache-Control": "no-store"})

    logger.info("TTS routes registered")
