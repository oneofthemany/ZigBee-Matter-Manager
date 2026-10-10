"""
Recordings API: clips and continuous footage. Watching is camera:read;
deleting and the space limit are admin. See docs/recordings.md.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from modules.auth_middleware import require_scope
from modules.recordings import get_recorder
from recorder.store import MAX_PLAY_S

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# Recordings are of people at home: never let a shared cache keep one.
_PRIVATE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def register_recording_routes(app: FastAPI) -> None:

    def _rec():
        r = get_recorder()
        if not r:
            raise HTTPException(503, "Recordings not initialised")
        return r

    def _cam(camera: str) -> str:
        if not _ID_RE.match(camera):
            raise HTTPException(404, "No such camera")
        return camera

    @app.get("/api/recordings")
    async def list_clips(camera: Optional[str] = None, before: Optional[float] = None, limit: int = 60,
                         _=Depends(require_scope("camera:read"))):
        clips = [c for c in await _rec().clips() if (not camera or c["camera"] == camera)
                 and (before is None or c["start"] < before)]
        limit = min(max(limit, 1), 200)
        return {"clips": clips[:limit], "more": len(clips) > limit}

    @app.get("/api/recordings/status")
    async def status(_=Depends(require_scope("camera:read"))):
        r = _rec()
        return {**r.public(), "usage": await asyncio.to_thread(r.store.usage),
                "settings": r.store.settings(), "clips": len(await r.clips())}

    @app.put("/api/recordings/settings")
    async def settings(body: Dict[str, Any], _=Depends(require_scope("admin"))):
        try:
            return await asyncio.to_thread(_rec().store.save_settings, body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/recordings/clips/{camera}/{name}")
    async def clip(camera: str, name: str, download: int = 0, _=Depends(require_scope("camera:read"))):
        cid, _dot, ext = name.rpartition(".")
        path = _rec().store.clip_file(_cam(camera), cid, ext)
        if path is None:
            raise HTTPException(404, "No such clip")
        return FileResponse(path, media_type="video/mp4" if ext == "mp4" else "image/jpeg", headers=_PRIVATE,
                            filename=f"{camera}-{cid}.{ext}" if download else None)

    @app.delete("/api/recordings/clips/{camera}/{cid}")
    async def delete_clip(camera: str, cid: str, _=Depends(require_scope("admin"))):
        if not await _rec().delete_clip(_cam(camera), cid):
            raise HTTPException(404, "No such clip")
        return {"success": True}

    @app.get("/api/recordings/footage/{camera}")
    async def coverage(camera: str, start: float, end: float, _=Depends(require_scope("camera:read"))):
        """What stretches of [start, end] (epoch seconds, at most two days) there is footage for."""
        if not 0 < end - start <= 2 * 86400:
            raise HTTPException(400, "Ask for up to two days at a time")
        return {"ranges": await asyncio.to_thread(_rec().store.coverage, _cam(camera), start, end),
                "now": time.time()}

    @app.get("/api/recordings/footage/{camera}/play")
    async def play(camera: str, start: float, seconds: float = 300, _=Depends(require_scope("camera:read"))):
        """A stretch of continuous footage as one MP4 (up to ten minutes)."""
        if not 0 < seconds <= MAX_PLAY_S:
            raise HTTPException(400, f"Up to {MAX_PLAY_S // 60} minutes at a time")
        path = await asyncio.to_thread(_rec().store.play, _cam(camera), start, seconds)
        if path is None:
            raise HTTPException(404, "No footage for that time")
        return FileResponse(path, media_type="video/mp4", headers=_PRIVATE)
