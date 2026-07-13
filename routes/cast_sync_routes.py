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
    player_ids: List[str] = []   # cast:<uuid> ids (individual devices, not groups)
    group_id: str = ""           # ...or a saved sync-group id


class SyncTrimBody(BaseModel):
    player_id: str
    trim_ms: int                 # ±ms; positive = play later


class SyncGroupBody(BaseModel):
    name: str
    members: List[str]           # cast:<uuid> ids
    id: str = ""                 # set to update an existing group


class SyncGroupDeleteBody(BaseModel):
    id: str


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
        if not body.player_ids and not body.group_id:
            return {"success": False, "error": "No players or group given"}
        return await sync.start_session(body.player_ids, group_id=body.group_id)

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

    @app.get("/api/media/sync/groups")
    async def sync_groups():
        sync = _sync()
        if sync is None:
            return {"success": True, "groups": [], "configured": False}
        return sync.list_groups()

    @app.post("/api/media/sync/groups")
    async def sync_group_save(body: SyncGroupBody):
        sync = _sync()
        if sync is None:
            return {"success": False,
                    "error": "Speaker sync is disabled — enable it under Settings → Speakers"}
        return sync.save_group(body.name, body.members, body.id)

    @app.post("/api/media/sync/groups/delete")
    async def sync_group_delete(body: SyncGroupDeleteBody):
        sync = _sync()
        if sync is None:
            return {"success": False, "error": "Sync PoC disabled"}
        return await sync.delete_group(body.id)
