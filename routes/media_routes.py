"""
Media player API routes.

Follows the module-level getter pattern (see routes/ai_api.py) so FastAPI's
lifespan owns the service instance and routes resolve it lazily.
"""
import logging
from typing import List, Optional

from fastapi import FastAPI, Response
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


class QueueModeBody(BaseModel):
    player_id: str
    repeat: Optional[str] = None      # off | one | all
    shuffle: Optional[bool] = None


class TidalPlayBody(BaseModel):
    player_id: str
    kind: str                 # track | album | playlist | artist | mix
    id: str
    mode: str = "play"        # play | radio  (radio = infinite, auto-extends)


class TidalFavoriteBody(BaseModel):
    kind: str                 # track | album | artist | playlist
    id: str
    action: str               # add | remove


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

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------
    @app.get("/api/media/queue")
    async def get_queue(player_id: str):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        return {"success": True, "queue": svc.controller.get_queue(player_id)}

    @app.post("/api/media/queue/mode")
    async def queue_mode(body: QueueModeBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        try:
            if body.repeat is not None:
                svc.controller.set_repeat(body.player_id, body.repeat)
            if body.shuffle is not None:
                svc.controller.set_shuffle(body.player_id, body.shuffle)
            return {"success": True, "queue": svc.controller.get_queue(body.player_id)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/media/queue/clear")
    async def queue_clear(body: ControlBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        svc.controller.clear_queue(body.player_id)
        return {"success": True}

    # ------------------------------------------------------------------
    # Tidal
    # ------------------------------------------------------------------
    def _tidal(svc):
        return svc.controller.get_source("tidal")

    @app.get("/api/media/tidal/status")
    async def tidal_status():
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if not src:
            return {"success": True, "status": {"state": "unavailable"}}
        return {"success": True, "status": await src.status()}

    @app.post("/api/media/tidal/login")
    async def tidal_login():
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        link = await src.login_start() if src else None
        if not link:
            return {"success": False, "error": "Tidal unavailable (not installed/enabled)"}
        return {"success": True, "link": link}

    @app.post("/api/media/tidal/logout")
    async def tidal_logout():
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if src:
            await src.logout()
        return {"success": True}

    @app.get("/api/media/tidal/search")
    async def tidal_search(q: str, limit: int = 20):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if not src:
            return {"success": False, "error": "Tidal unavailable"}
        try:
            return {"success": True, "results": await src.search_grouped(q, limit)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/media/tidal/library")
    async def tidal_library(kind: str):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if not src:
            return {"success": False, "error": "Tidal unavailable"}
        if kind not in ("playlists", "albums", "artists", "mixes"):
            return {"success": False, "error": "kind must be playlists|albums|artists|mixes"}
        try:
            return {"success": True, "items": await src.library(kind)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/media/tidal/play")
    async def tidal_play(body: TidalPlayBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if not src:
            return {"success": False, "error": "Tidal unavailable"}
        try:
            radio = body.mode == "radio"
            items = []
            if body.kind == "track":
                if radio:
                    items = await src.track_radio(body.id)
                else:
                    item = await src.single_item(body.id)
                    items = [item] if item else []
            elif body.kind == "album":
                items = await src.album_items(body.id)
            elif body.kind == "playlist":
                items = await src.playlist_items(body.id)
            elif body.kind == "artist":
                items = await (src.artist_radio(body.id) if radio else src.artist_tracks(body.id))
            elif body.kind == "mix":
                items = await src.mix_items(body.id)
            else:
                return {"success": False, "error": "kind must be track|album|playlist|artist|mix"}
            if not items:
                return {"success": False, "error": "Nothing to play (empty, not found, or login required)"}
            await svc.controller.play_items(body.player_id, items, auto_extend=radio)
            return {"success": True, "count": len(items), "radio": radio}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/media/tidal/lyrics")
    async def tidal_lyrics(track_id: str):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if not src:
            return {"success": False, "error": "Tidal unavailable"}
        lyrics = await src.track_lyrics(track_id)
        if not lyrics:
            return {"success": False, "error": "No lyrics for this track"}
        return {"success": True, "lyrics": lyrics}

    @app.post("/api/media/tidal/favorite")
    async def tidal_favorite(body: TidalFavoriteBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if not src:
            return {"success": False, "error": "Tidal unavailable"}
        if body.kind not in ("track", "album", "artist", "playlist"):
            return {"success": False, "error": "kind must be track|album|artist|playlist"}
        if body.action not in ("add", "remove"):
            return {"success": False, "error": "action must be add|remove"}
        ok = await src.set_favorite(body.kind, body.id, body.action == "add")
        if not ok:
            return {"success": False, "error": "Favourite update failed (login required?)"}
        return {"success": True, "favorited": body.action == "add"}

    @app.get("/api/media/tidal/manifest/{track_id}.mpd")
    async def tidal_manifest(track_id: str):
        # Served to Cast for lossless playback: a fresh DASH MPD whose segment
        # URLs point straight at Tidal's CDN. Generated on each fetch (URLs expire).
        svc = _svc()
        if not svc:
            return Response("media service not enabled", status_code=503)
        src = _tidal(svc)
        if not src:
            return Response("tidal unavailable", status_code=503)
        mpd = await src.dash_manifest(track_id)
        if not mpd:
            return Response("no lossless manifest for track", status_code=404)
        return Response(content=mpd, media_type="application/dash+xml")

    logger.info("Media routes registered")
