"""
Neural TTS routes for the therapy SPA (static/therapy).

The SPA's contract predates ZMM (it was the standalone Node server):
POST /api/tts returns audio/wav; /api/tts/voices and /api/tts/status shape
their JSON exactly as App.jsx expects, so the vendored frontend runs unpatched.
"""
import logging
from typing import Optional

from fastapi import FastAPI, Response
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

    logger.info("TTS routes registered")
