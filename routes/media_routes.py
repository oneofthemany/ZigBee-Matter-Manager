"""
Media player API routes.

Follows the module-level getter pattern (see routes/ai_api.py) so FastAPI's
lifespan owns the service instance and routes resolve it lazily.
"""
import logging
import re
from typing import List, Optional
from urllib.parse import quote, urljoin

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

logger = logging.getLogger("routes.media")

_HLS_TYPES = ("application/vnd.apple.mpegurl", "application/x-mpegurl",
              "audio/mpegurl", "audio/x-mpegurl")
_HLS_MAX_BYTES = 4 * 1024 * 1024      # a playlist this big is not a playlist
_HLS_URI_ATTR = re.compile(r'(URI=")([^"]+)(")')


def _proxy_path(url: str) -> str:
    return "/api/media/local/proxy?url=" + quote(url, safe="")


def _rewrite_hls(text: str, base: str) -> str:
    """Point every URI in an HLS playlist back through this proxy, so segments
    and keys stay same-origin. See docs/speaker_sync.md."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            # EXT-X-KEY / MEDIA / MAP / I-FRAME-STREAM-INF carry URI="…".
            out.append(_HLS_URI_ATTR.sub(
                lambda m: m.group(1) + _proxy_path(urljoin(base, m.group(2))) + m.group(3),
                line))
        else:
            out.append(_proxy_path(urljoin(base, stripped)))
    return "\n".join(out) + "\n"


class PlayBody(BaseModel):
    player_id: str
    url: Optional[str] = None
    station_uuid: Optional[str] = None
    station: Optional[dict] = None       # see LocalPlaylistBody.station
    title: str = ""
    artist: str = ""
    content_type: Optional[str] = None   # MIME hint (e.g. therapy's audio/wav)
    media_type: Optional[str] = None     # "live" for endless streams (therapy)


class LocalPlaylistBody(BaseModel):
    """Resolve something to a queue the browser can play itself.

    Either a radio ``station_uuid`` (one endless stream) or a Tidal
    ``kind``+``id`` (which may expand to a whole album/playlist).
    """
    station_uuid: Optional[str] = None
    station: Optional[dict] = None   # caller's snapshot, used if the directory is down
    kind: Optional[str] = None       # track | album | playlist | artist | mix
    id: Optional[str] = None
    mode: str = "play"               # play | radio


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


class EqBody(BaseModel):
    player_id: str
    enabled: Optional[bool] = None
    preset: Optional[str] = None
    gains: Optional[List[float]] = None   # 10-band dB values (Cast DSP proxy)


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


class TidalPlaylistCreateBody(BaseModel):
    name: str
    description: str = ""
    track_ids: List[str] = []    # seed it in one go ("add to a new playlist")


class TidalPlaylistEditBody(BaseModel):
    id: str
    action: str                  # add | remove | move | edit | delete | visibility
    track_ids: List[str] = []    # add
    track_id: Optional[str] = None   # remove | move
    position: int = 0                # move
    name: Optional[str] = None       # edit
    description: Optional[str] = None
    public: Optional[bool] = None    # visibility
    allow_duplicates: bool = False


class AnnounceBody(BaseModel):
    player_id: str
    text: str
    lang: Optional[str] = None
    volume: Optional[float] = None


class FadeBody(BaseModel):
    player_id: str
    volume: float             # target level 0.0–1.0
    fade_seconds: int = 300
    stop_at_end: bool = False


class VolumeAdjustBody(BaseModel):
    player_id: str
    delta: float              # signed change, -1.0–1.0 (e.g. 0.1 = +10%)


class KaraokeBody(BaseModel):
    enabled: bool


class RadioFavBody(BaseModel):
    uuid: str
    name: str = ""
    url: str = ""
    favicon: str = ""
    homepage: str = ""
    country: str = ""
    tags: str = ""
    codec: str = ""
    bitrate: int = 0


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

    @app.get("/api/media/position")
    async def media_position(player_id: str = None):
        """Fresh, on-demand playhead for ONE player — for tight lyric sync.
        Without player_id, auto-picks the currently-playing player (used by the
        standalone /static/lyrics.html page). Returns the full player dict."""
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        pid = player_id
        if not pid:
            for s in svc.controller.snapshot():
                st = getattr(s.state, "value", str(s.state))
                if st == "playing" and getattr(s, "now_playing_id", ""):
                    pid = s.player_id
                    break
        if not pid:
            return {"success": False, "error": "No player is playing"}
        s = await svc.controller.live_state(pid)
        if not s:
            return {"success": False, "error": "Player not found"}
        return {"success": True, "player": s.to_dict()}

    @app.post("/api/media/play")
    async def play(body: PlayBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        try:
            if body.station_uuid:
                item = await svc.play_radio_station(body.player_id, body.station_uuid,
                                                    body.station)
            elif body.url:
                from modules.media.models import MediaItem
                item = MediaItem(url=body.url, title=body.title, artist=body.artist,
                                 content_type=body.content_type or "audio/mpeg",
                                 media_type=body.media_type or "url")
                await svc.controller.play_url(body.player_id, item)
            else:
                return {"success": False, "error": "Provide url or station_uuid"}
            return {"success": True, "now_playing": item.to_dict()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Browser-local playback: the page plays audio itself via <audio>, so it needs
    # a directly-playable URL rather than a player to cast to. Tidal returns 320k
    # AAC for non-Cast providers, which is exactly what a browser plays natively.

    @app.post("/api/media/local/playlist")
    async def local_playlist(body: LocalPlaylistBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        try:
            if body.station_uuid:
                station = await svc.resolve_station(body.station_uuid, body.station)
                if not station:
                    return {"success": False,
                            "error": "Radio station not found (the radio directory "
                                     "is unreachable — star the station to pin it)"}
                items = [station.to_media_item()]
            elif body.kind and body.id:
                if not _tidal(svc):
                    return {"success": False, "error": "Tidal unavailable"}
                items = await svc.tidal_items(body.kind, body.id, body.mode)
            else:
                return {"success": False, "error": "Provide station_uuid or kind+id"}
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.warning(f"Local playlist resolve failed: {e}")
            return {"success": False, "error": str(e)}
        return {"success": True, "items": [i.to_dict() for i in items]}

    @app.get("/api/media/local/track_url")
    async def local_track_url(source_id: str):
        """Fresh, browser-playable URL for one Tidal track.

        Resolved just-in-time per track: the signed URLs are short-lived, so a
        long queue resolved up front would go stale before it got there.
        """
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if not src:
            return {"success": False, "error": "Tidal unavailable"}
        try:
            # provider="browser" → never Cast → AAC, not DASH (see _wants_lossless)
            got = await src.resolve_url(source_id, "browser")
        except Exception as e:
            logger.warning(f"Local track URL resolve failed for {source_id}: {e}")
            return {"success": False, "error": str(e)}
        if not got or not got.get("url"):
            return {"success": False, "error": "Could not resolve a playable URL"}
        return {"success": True, **got}

    @app.get("/api/media/local/proxy")
    async def local_stream_proxy(url: str, request: Request):
        """Same-origin passthrough for the browser player: carries streams that
        can't be loaded direct (no CORS headers, http source, HLS segments).

        Range headers pass through so seekable sources stay seekable; endless
        radio streams flow until the client disconnects. HLS playlists are
        rewritten rather than streamed. See docs/speaker_sync.md.
        """
        if not url.lower().startswith(("http://", "https://")):
            return Response("http(s) URLs only", status_code=400)
        if not _svc():
            return Response("Media service not enabled", status_code=503)
        import httpx
        from fastapi.responses import StreamingResponse
        fwd = {"User-Agent": "ZMM-Media/1.0", "Icy-MetaData": "0"}
        rng = request.headers.get("range")
        if rng:
            fwd["Range"] = rng
        client = httpx.AsyncClient(follow_redirects=True,
                                   timeout=httpx.Timeout(15, read=None))
        try:
            upstream = await client.send(
                client.build_request("GET", url, headers=fwd), stream=True)
        except Exception as e:
            await client.aclose()
            logger.warning(f"Local stream proxy fetch failed for {url}: {e}")
            return Response(f"upstream fetch failed: {e}", status_code=502)
        if upstream.status_code >= 400:
            code = upstream.status_code
            await upstream.aclose()
            await client.aclose()
            return Response(f"upstream returned {code}", status_code=502)

        ctype = upstream.headers.get("content-type", "").split(";")[0].strip().lower()
        final = str(upstream.url)     # post-redirect: the manifest's real base
        if ctype in _HLS_TYPES or final.split("?")[0].lower().endswith(".m3u8"):
            body = b""
            try:
                async for chunk in upstream.aiter_bytes(16384):
                    body += chunk
                    if len(body) > _HLS_MAX_BYTES:
                        raise ValueError("playlist exceeded size limit")
            except Exception as e:
                logger.warning(f"HLS playlist read failed for {url}: {e}")
                return Response(f"playlist read failed: {e}", status_code=502)
            finally:
                await upstream.aclose()
                await client.aclose()
            return Response(
                _rewrite_hls(body.decode("utf-8", "replace"), final),
                media_type=ctype or "application/vnd.apple.mpegurl",
                headers={"Cache-Control": "no-store"})

        async def gen():
            try:
                async for chunk in upstream.aiter_bytes(16384):
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        headers = {k: v for k, v in upstream.headers.items()
                   if k.lower() in ("content-type", "content-length",
                                    "content-range", "accept-ranges")}
        headers["Cache-Control"] = "no-store"
        return StreamingResponse(gen(), status_code=upstream.status_code,
                                 headers=headers)

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

    @app.post("/api/media/volume/adjust")
    async def volume_adjust(body: VolumeAdjustBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        try:
            level = await svc.controller.adjust_volume(body.player_id, body.delta)
            return {"success": True, "level": level}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Equaliser (device DSP — WiiM presets; Cast has none; the browser
    #    player EQs client-side and never calls these) ─────────────────────
    @app.get("/api/media/eq")
    async def eq_info(player_id: str):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        try:
            info = await svc.controller.eq_info(player_id)
        except Exception as e:
            return {"success": False, "error": str(e)}
        return {"success": True, "supported": info is not None, "eq": info}

    @app.post("/api/media/eq")
    async def eq_set(body: EqBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        if body.enabled is None and not body.preset and body.gains is None:
            return {"success": False, "error": "Provide enabled, preset and/or gains"}
        try:
            info = await svc.controller.set_eq(body.player_id, body.enabled,
                                               body.preset, body.gains)
            return {"success": True, "eq": info}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/media/eq/status")
    async def eq_status():
        """Cast EQ proxy readiness (Settings → Audio tab badge)."""
        svc = _svc()
        engine = getattr(svc, "eq_stream", None) if svc else None
        if not engine:
            return {"success": True, "available": False,
                    "reason": "Media service not enabled"}
        return {"success": True, **engine.status()}

    @app.get("/api/media/eq/stream/{player_id}/{token}.wav")
    async def eq_stream(player_id: str, token: str):
        """The Cast EQ proxy stream: source → ffmpeg decode → Rust biquad
        chain → endless WAV. Fetched by the speaker, not the browser."""
        svc = _svc()
        engine = getattr(svc, "eq_stream", None) if svc else None
        if not engine or not engine.available:
            return Response("EQ proxy not available", status_code=503)
        if not engine.knows(player_id, token):
            return Response("unknown or superseded stream token", status_code=404)
        from fastapi.responses import StreamingResponse
        return StreamingResponse(engine.stream(player_id, token),
                                 media_type="audio/wav",
                                 headers={"Cache-Control": "no-store",
                                          "Access-Control-Allow-Origin": "*"})

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

    @app.get("/api/media/recent")
    async def recent():
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        return {"success": True, "items": svc.controller.recently_played()}

    @app.post("/api/media/announce")
    async def announce(body: AnnounceBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        try:
            return await svc.announce(body.player_id, body.text, body.lang, body.volume)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/media/volume/fade")
    async def volume_fade(body: FadeBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        svc.controller.fade_volume(body.player_id, body.volume, body.fade_seconds, body.stop_at_end)
        return {"success": True}

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

    # Karaoke mode — cast synced lyrics to the custom receiver
    @app.get("/api/media/karaoke")
    async def karaoke_get():
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        configured = bool(getattr(getattr(svc, "cast", None), "lyrics_app_id", ""))
        return {"success": True, "enabled": svc.get_karaoke(),
                "receiver_configured": configured}

    @app.post("/api/media/karaoke")
    async def karaoke_set(body: KaraokeBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        return svc.set_karaoke(body.enabled)

    # Radio favourites — pinned stations, no re-search needed
    @app.get("/api/media/radio/favourites")
    async def radio_favourites_list():
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        return {"success": True, "stations": svc.radio_favourites.list()}

    @app.post("/api/media/radio/favourites")
    async def radio_favourite_add(body: RadioFavBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        return svc.radio_favourites.add(body.model_dump())

    @app.delete("/api/media/radio/favourites/{uuid}")
    async def radio_favourite_remove(uuid: str):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        return svc.radio_favourites.remove(uuid)

    @app.post("/api/media/radio/favourites/play")
    async def radio_favourite_play(body: PlayBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        if not body.station_uuid:
            return {"success": False, "error": "Provide station_uuid"}
        try:
            item = await svc.play_radio_favourite(body.player_id, body.station_uuid)
            return {"success": True, "now_playing": item.to_dict()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Queue
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
        await svc.controller.clear_queue(body.player_id)
        return {"success": True}

    # Tidal
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
    async def tidal_library(kind: str, limit: int = 100, offset: int = 0):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if not src:
            return {"success": False, "error": "Tidal unavailable"}
        kinds = src.LIBRARY_KINDS
        if kind not in kinds:
            return {"success": False, "error": f"kind must be {'|'.join(kinds)}"}
        try:
            # Paged: TIDAL caps a favourites request at 50 rows, so a library
            # larger than that was previously truncated in silence.
            return {"success": True, **await src.library(kind, limit, offset)}
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
            return await svc.play_tidal(body.player_id, body.kind, body.id, body.mode)
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

    @app.get("/api/media/tidal/track/{track_id}/context")
    async def tidal_track_context(track_id: str):
        """Artist of the now-playing track + their other albums — powers the
        'artist radio' and 'more from this artist' actions on the player card."""
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if not src:
            return {"success": False, "error": "Tidal unavailable"}
        artist = await src.track_artist(track_id)
        if not artist:
            return {"success": False, "error": "No artist info for this track"}
        albums = await src.artist_albums(artist["id"]) if artist.get("id") else []
        return {"success": True, "artist": artist, "albums": albums}

    @app.get("/api/media/tidal/playlist/{playlist_id}")
    async def tidal_playlist(playlist_id: str):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if not src:
            return {"success": False, "error": "Tidal unavailable"}
        detail = await src.playlist_detail(playlist_id)
        if not detail:
            return {"success": False, "error": "Playlist not found"}
        return {"success": True, "playlist": detail}

    @app.post("/api/media/tidal/playlist/create")
    async def tidal_playlist_create(body: TidalPlaylistCreateBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if not src:
            return {"success": False, "error": "Tidal unavailable"}
        res = await src.playlist_create(body.name, body.description or "")
        # A playlist created to hold tracks should come back holding them.
        if res.get("success") and body.track_ids:
            pid = res["playlist"]["id"]
            add = await src.playlist_write("add", pid, track_ids=body.track_ids)
            res["added"] = add.get("added", 0)
            if not add.get("success"):
                res["error"] = add.get("error")
        return res

    @app.post("/api/media/tidal/playlist/edit")
    async def tidal_playlist_edit(body: TidalPlaylistEditBody):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if not src:
            return {"success": False, "error": "Tidal unavailable"}
        if body.action not in ("add", "remove", "move", "edit", "delete",
                               "visibility"):
            return {"success": False, "error": "unknown playlist action"}
        return await src.playlist_write(
            body.action, body.id, track_ids=body.track_ids,
            track_id=body.track_id, position=body.position, name=body.name,
            description=body.description, public=body.public,
            allow_duplicates=body.allow_duplicates)

    @app.get("/api/media/tidal/favorites")
    async def tidal_favorites(refresh: bool = False):
        """Which ids are already favourited, so the UI can draw a heart in the
        state it is in. `ready` false means a build is running — ask again."""
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if not src:
            return {"success": False, "error": "Tidal unavailable"}
        try:
            return {"success": True, **await src.favourite_ids(refresh)}
        except Exception as e:
            return {"success": False, "error": str(e)}

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
        # CORS: the Cast receiver XHR-fetches the MPD from its google-hosted
        # origin, so without this header DASH playback fails on the device.
        return Response(content=mpd, media_type="application/dash+xml",
                        headers={"Access-Control-Allow-Origin": "*"})

    logger.info("Media routes registered")
