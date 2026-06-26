"""
Media player API routes.

Follows the module-level getter pattern (see routes/ai_api.py) so FastAPI's
lifespan owns the service instance and routes resolve it lazily.
"""
import logging
from typing import List, Optional

from fastapi import FastAPI
from pydantic import BaseModel

logger = logging.getLogger("routes.media")


class PlayBody(BaseModel):
    player_id: str
    url: Optional[str] = None
    station_uuid: Optional[str] = None
    title: str = ""
    artist: str = ""


class ControlBody(BaseModel):
    player_id: str
    action: str  # pause | resume | stop | next | prev


class VolumeBody(BaseModel):
    player_id: str
    level: float            # 0.0–1.0
    muted: Optional[bool] = None


class GroupBody(BaseModel):
    master_id: str
    member_ids: List[str] = []


def register_media_routes(app: FastAPI, get_media_service):

    def _svc():
        svc = get_media_service()
        if not svc or not svc.enabled:
            return None
        return svc

    @app.get("/api/media/players")
    async def list_players():
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        # Return the cached snapshot for snappiness; the poll loop keeps it fresh.
        players = svc.controller.snapshot()
        if not players:
            players = await svc.controller.refresh()
        return {"success": True, "players": [p.to_dict() for p in players]}

    @app.post("/api/media/play")
    async def play(body: PlayBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        try:
            if body.station_uuid:
                item = await svc.play_radio_station(body.player_id, body.station_uuid)
            elif body.url:
                from modules.media.models import MediaItem
                item = MediaItem(url=body.url, title=body.title, artist=body.artist)
                await svc.controller.play_url(body.player_id, item)
            else:
                return {"success": False, "error": "Provide url or station_uuid"}
            return {"success": True, "now_playing": item.to_dict()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/media/control")
    async def control(body: ControlBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        try:
            await svc.controller.control(body.player_id, body.action)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/media/volume")
    async def volume(body: VolumeBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        try:
            if body.muted is not None:
                await svc.controller.set_muted(body.player_id, body.muted)
            await svc.controller.set_volume(body.player_id, body.level)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/media/group")
    async def group(body: GroupBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        try:
            await svc.controller.join_group(body.master_id, body.member_ids)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/media/ungroup")
    async def ungroup(body: GroupBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        try:
            await svc.controller.ungroup(body.master_id)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/media/radio/search")
    async def radio_search(q: str, limit: int = 25):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        source = svc.controller.get_source("radio_browser")
        if not source:
            return {"success": False, "error": "Radio-Browser source not enabled"}
        try:
            stations = await source.search_stations(q, limit)
            return {"success": True, "stations": [s.to_dict() for s in stations]}
        except Exception as e:
            return {"success": False, "error": str(e)}

    logger.info("Media routes registered")
