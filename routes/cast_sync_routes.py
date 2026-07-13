"""
Cast sync PoC routes — control the synchronised multi-speaker experiment.

Thin wrappers over modules.media.cast_sync.CastSyncPoc (owned by MediaService
as ``media_service.cast_sync``; None unless media.cast.sync.enabled). Same
lazy-getter pattern as the other media routes.
"""
import logging
from typing import List

from fastapi import FastAPI
from pydantic import BaseModel

logger = logging.getLogger("routes.cast_sync")


class SyncStartBody(BaseModel):
    player_ids: List[str]        # cast:<uuid> ids (individual devices, not groups)


class SyncTrimBody(BaseModel):
    player_id: str
    trim_ms: int                 # ±ms; positive = play later


def register_cast_sync_routes(app: FastAPI, get_media):
    def _sync():
        svc = get_media()
        return getattr(svc, "cast_sync", None) if svc else None

    @app.get("/api/media/sync/status")
    async def sync_status():
        sync = _sync()
        if sync is None:
            return {"running": False, "configured": False,
                    "error": "Sync PoC disabled — set media.cast.sync.enabled"}
        return sync.status()

    @app.post("/api/media/sync/start")
    async def sync_start(body: SyncStartBody):
        sync = _sync()
        if sync is None:
            return {"success": False,
                    "error": "Sync PoC disabled — set media.cast.sync.enabled"}
        if not body.player_ids:
            return {"success": False, "error": "No players given"}
        return await sync.start_session(body.player_ids)

    @app.post("/api/media/sync/stop")
    async def sync_stop():
        sync = _sync()
        if sync is None:
            return {"success": False, "error": "Sync PoC disabled"}
        return await sync.stop_session()

    @app.post("/api/media/sync/trim")
    async def sync_trim(body: SyncTrimBody):
        sync = _sync()
        if sync is None:
            return {"success": False, "error": "Sync PoC disabled"}
        return await sync.set_trim(body.player_id, body.trim_ms)
