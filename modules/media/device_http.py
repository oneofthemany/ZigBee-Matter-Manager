"""
Plain-HTTP listener for device-fetched audio.

Cast and WiiM refuse the app's self-signed HTTPS, so this serves only the URLs a
speaker fetches: the EQ proxy stream, the Tidal DASH manifest and the therapy
soundscape. No user data and no control surface. With it up, media.eq.base_url
needs no configuration. See docs/speaker_sync.md.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import Optional

logger = logging.getLogger("modules.media.device_http")


def lan_ip() -> str:
    """This host's outbound LAN address (UDP connect trick — no packet sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 53))
            return s.getsockname()[0]
    except OSError:
        return ""


class DeviceAudioListener:
    """Owned by MediaService; started/stopped with it (idempotent)."""

    def __init__(self, service, port: int = 8011):
        self._svc = service
        self.port = int(port)
        # The therapy TTS service is built after MediaService — main.py wires
        # this getter once it exists. None → therapy streams speech-free.
        self.get_tts = None
        self._server = None                    # uvicorn.Server
        self._task: Optional[asyncio.Task] = None

    def start(self):
        if self._task is not None:
            return
        import uvicorn
        config = uvicorn.Config(
            self._build_app(), host="0.0.0.0", port=self.port,
            log_level="warning", lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        logger.info(f"Device-audio HTTP listener on :{self.port} "
                    f"(EQ stream / Tidal manifest / therapy)")

    def stop(self):
        if self._server is not None:
            self._server.should_exit = True
        self._task = None

    def _build_app(self):
        from fastapi import FastAPI, Response
        from fastapi.responses import StreamingResponse

        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        svc = self._svc

        @app.get("/health")
        async def health():
            return {"ok": True}

        @app.get("/api/media/eq/stream/{player_id}/{token}.wav")
        async def eq_stream(player_id: str, token: str):
            engine = getattr(svc, "eq_stream", None)
            if not engine or not engine.available:
                return Response("EQ proxy not available", status_code=503)
            if not engine.knows(player_id, token):
                return Response("unknown or superseded stream token",
                                status_code=404)
            return StreamingResponse(engine.stream(player_id, token),
                                     media_type="audio/wav",
                                     headers={"Cache-Control": "no-store",
                                              "Access-Control-Allow-Origin": "*"})

        @app.get("/api/media/tidal/manifest/{track_id}.mpd")
        async def tidal_manifest(track_id: str):
            src = getattr(svc, "tidal", None)
            if not src:
                return Response("tidal unavailable", status_code=503)
            mpd = await src.dash_manifest(track_id)
            if not mpd:
                return Response("no lossless manifest for track",
                                status_code=404)
            return Response(content=mpd, media_type="application/dash+xml",
                            headers={"Access-Control-Allow-Origin": "*"})

        @app.get("/api/therapy/stream")
        async def therapy_stream(mode: str = "relaxation", speech: int = 1,
                                 voice: str = "", speed: float = 0.75,
                                 pitch: float = 1.0, interval: int = 45,
                                 breath: str = "4-7-8"):
            from modules.media.therapy_stream import TherapyStream
            tts = self.get_tts() if callable(self.get_tts) else None
            tts_ok = bool(tts) and await tts.status()
            ts = TherapyStream(tts if tts_ok else None, mode,
                               speech=bool(speech) and tts_ok,
                               voice=voice or "af_heart", speech_speed=speed,
                               pitch=pitch, interval=interval, breath=breath)
            return StreamingResponse(ts.wav_stream(), media_type="audio/wav",
                                     headers={"Cache-Control": "no-store",
                                              "Access-Control-Allow-Origin": "*"})

        return app
