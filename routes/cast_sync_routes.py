"""
Cast sync PoC routes — control the synchronised multi-speaker experiment.

Thin wrappers over modules.media.cast_sync.CastSyncPoc (owned by MediaService
as ``media_service.cast_sync``; None unless media.cast.sync.enabled). Same
lazy-getter pattern as the other media routes.
"""
import logging
from typing import List, Optional

from fastapi import FastAPI
from pydantic import BaseModel

logger = logging.getLogger("routes.cast_sync")


class SyncMediaBody(BaseModel):
    url: str = ""                # anything ffmpeg can open; "" = test signal
    station_uuid: str = ""       # ...or a radio-directory id, resolved here
    source_id: str = ""          # ...or a source id (Tidal), re-resolved as
    media_type: str = ""         #    it expires; needs media_type to route it
    kind: str = "track"          # track | album | playlist | artist | mix —
    #                              anything but "track" is expanded to a queue
    title: str = ""
    artwork_url: str = ""        # album art / station logo for the displays
    artist: str = ""
    loop: bool = False           # for finite sources (a file); ignored on live


class SyncStartBody(BaseModel):
    player_ids: List[str] = []   # cast:<uuid> ids (individual devices, not groups)
    group_id: str = ""           # ...or a saved sync-group id
    duration_s: int = 0          # auto-stop after this many seconds (0 = manual)
    media: Optional[SyncMediaBody] = None   # omit for the generated test signal
    # Overlap between queue items. Omitted (None) means "use the server
    # default" — distinct from 0.0, which is a caller explicitly asking for
    # plain seams and must not be overridden by config.
    crossfade_s: Optional[float] = None


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
        media = body.media.model_dump() if body.media else None
        if media and media.get("station_uuid") and not media.get("url"):
            # Resolve here rather than storing a URL in the picker: directory
            # stream URLs move, and a favourite saved months ago should still
            # start. Same source the ordinary play path uses.
            svc = get_media()
            station = None
            if svc is not None and getattr(svc, "radio", None) is not None:
                station = await svc.radio.get_station(media["station_uuid"])
            if station is None:
                return {"success": False,
                        "error": "Radio station not found (or directory unreachable)"}
            media["url"] = station.url
            media["title"] = media.get("title") or station.name
            # The directory's logo is what a screened speaker shows while the
            # station plays — same picture the single-player path sends.
            media["artwork_url"] = media.get("artwork_url") or station.favicon
        if media:
            kind = (media.get("kind") or "track").strip().lower()
            if kind not in ("track", "album", "playlist", "artist", "mix"):
                return {"success": False,
                        "error": "kind must be track|album|playlist|artist|mix"}
            media["kind"] = kind
            if kind != "track" and not media.get("source_id"):
                return {"success": False,
                        "error": f"a {kind} needs a source_id to expand"}
            url = (media.get("url") or "").strip()
            # A source_id block carries no URL yet on purpose — the engine
            # resolves one at session start and again whenever it expires.
            if not url and not media.get("source_id"):
                return {"success": False,
                        "error": "Media given with no url, station_uuid or source_id"}
            if url.startswith("-"):
                # The decoder takes its input as a bare argument, so a leading
                # dash would be read as an option instead of a source.
                return {"success": False, "error": "URL may not start with '-'"}
            media["url"] = url
        return await sync.start_session(body.player_ids, group_id=body.group_id,
                                        duration_s=min(max(body.duration_s, 0),
                                                       3600),
                                        media=media,
                                        crossfade_s=body.crossfade_s)

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

    @app.post("/api/media/sync/calibrate")
    async def sync_calibrate():
        """Chirp calibration: measure in-air inter-device offsets with the
        server mic and set trims automatically (needs a running session)."""
        sync = _sync()
        if sync is None:
            return {"success": False, "error": "Sync PoC disabled"}
        return await sync.calibrate()

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
                    "error": "OpenZone is disabled — enable it under Settings → Audio"}
        return sync.save_group(body.name, body.members, body.id)

    @app.post("/api/media/sync/groups/delete")
    async def sync_group_delete(body: SyncGroupDeleteBody):
        sync = _sync()
        if sync is None:
            return {"success": False, "error": "Sync PoC disabled"}
        return await sync.delete_group(body.id)

    @app.get("/api/media/sync/history")
    async def sync_history(group_id: str = "", hours: int = 24,
                           bucket_minutes: int = 0):
        """Lag/hysteresis history from the group's own DuckDB — raw rows, or
        median-bucketed per player when bucket_minutes > 0."""
        import asyncio
        try:
            from modules.media import sync_db
            samples = await asyncio.to_thread(
                sync_db.query_history,
                group_id, min(int(hours), 24 * 30), int(bucket_minutes))
            return {"success": True, "samples": samples}
        except Exception as e:
            logger.warning(f"sync history query failed: {e}")
            return {"success": False, "error": str(e), "samples": []}

    @app.get("/api/media/sync/sessions")
    async def sync_sessions(group_id: str = "", days: int = 30):
        """Session index for the Sync Lab's picker."""
        import asyncio
        try:
            from modules.media import sync_db
            sessions = await asyncio.to_thread(
                sync_db.query_sessions, group_id, min(int(days), 90))
            return {"success": True, "sessions": sessions}
        except Exception as e:
            return {"success": False, "error": str(e), "sessions": []}

    @app.get("/api/media/sync/session")
    async def sync_session_detail(group_id: str = "", session_id: str = ""):
        """Full series + per-speaker stats for one session (latest if
        session_id omitted)."""
        import asyncio
        try:
            from modules.media import sync_db
            if not session_id:
                sessions = await asyncio.to_thread(
                    sync_db.query_sessions, group_id, 90)
                if not sessions:
                    return {"success": True, "session_id": "",
                            "series": [], "players": []}
                session_id = sessions[0]["session_id"]
            detail = await asyncio.to_thread(
                sync_db.query_session_detail, group_id, session_id)
            return {"success": True, **detail}
        except Exception as e:
            return {"success": False, "error": str(e),
                    "series": [], "players": []}

    @app.get("/api/media/sync/trend")
    async def sync_trend(group_id: str = "", limit: int = 20):
        """Per-session learning trend (start misalignment, time to lock)."""
        import asyncio
        try:
            from modules.media import sync_db
            trend = await asyncio.to_thread(
                sync_db.query_group_trend, group_id, min(int(limit), 50))
            return {"success": True, "trend": trend}
        except Exception as e:
            return {"success": False, "error": str(e), "trend": []}

    @app.get("/api/media/sync/model")
    async def sync_model():
        """The learned per-device model, trained across all group DBs."""
        import asyncio
        try:
            from modules.media import sync_db
            model = await asyncio.to_thread(sync_db.query_device_model)
            return {"success": True, "model": model}
        except Exception as e:
            return {"success": False, "error": str(e), "model": {}}
