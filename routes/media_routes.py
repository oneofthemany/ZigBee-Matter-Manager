"""
Media player API routes.

Follows the module-level getter pattern (see routes/ai_api.py) so FastAPI's
lifespan owns the service instance and routes resolve it lazily.
"""
import logging
import re
from typing import List, Optional
from urllib.parse import quote, urljoin

from fastapi import Depends, FastAPI, Request, Response
from pydantic import BaseModel

from modules.auth_middleware import (
    Principal, require_authenticated, require_scope,
)

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


class AirPlayPinBody(BaseModel):
    pin: str


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
        # What each ecosystem can do, so the UI offers grouping where it exists
        # instead of carrying its own copy of the list.
        providers = {
            key: {"label": getattr(p, "label", key.title()),
                  "zone_transport": bool(getattr(p, "zone_transport", False)),
                  "groups_natively": bool(getattr(p, "groups_natively", False))}
            for key, p in (svc.controller._players or {}).items()}
        return {"success": True, "providers": providers,
                "players": [p.to_dict() for p in players]}

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
    async def local_playlist(body: LocalPlaylistBody,
                             principal: Principal = Depends(require_authenticated)):
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
                items = await svc.tidal_items(body.kind, body.id, body.mode,
                                              principal.user.username)
            else:
                return {"success": False, "error": "Provide station_uuid or kind+id"}
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.warning(f"Local playlist resolve failed: {e}")
            return {"success": False, "error": str(e)}
        return {"success": True, "items": [i.to_dict() for i in items]}

    @app.get("/api/media/local/track_url")
    async def local_track_url(source_id: str,
                              principal: Principal = Depends(require_authenticated)):
        """Fresh, browser-playable URL for one Tidal track.

        Resolved just-in-time per track: the signed URLs are short-lived, so a
        long queue resolved up front would go stale before it got there.
        """
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _acct(svc, principal)
        if not src:
            return dict(NOT_LINKED)
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
    #
    # Every endpoint below resolves the caller's own account. Tidal is per-ZMM-user:
    # you browse your library, your favourites and your playlists, and the account a
    # track is played on is the one it was queued from. See docs/plans/tidal-per-user-auth.md.
    def _tidal(svc):
        """The registry. Only the routes that do not belong to a user use this."""
        return svc.controller.get_source("tidal")

    def _acct(svc, principal, create: bool = False):
        """The calling user's Tidal account, or None if they have not linked one.

        ``create`` is for the login flow, which is where an account starts.
        """
        src = _tidal(svc)
        if not src:
            return None
        return src.account(principal.user.username, create=create)

    #: What an endpoint answers when the caller has no Tidal of their own. Not an
    #: error: it is the same "log in" state the UI has always rendered, now asked
    #: of one user rather than of the hub.
    NOT_LINKED = {"success": False, "error": "Link your Tidal account first "
                                             "(Settings → APIs → Media)"}

    @app.get("/api/media/tidal/status")
    async def tidal_status(principal: Principal = Depends(require_authenticated)):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if not src:
            return {"success": True, "status": {"state": "unavailable"}}
        acct = _acct(svc, principal)
        if acct is None:
            # Nothing linked for this user. "unavailable" would be wrong — the
            # source works, they just have no account on it yet.
            return {"success": True, "status": {"state": "logged_out"}}
        return {"success": True, "status": await acct.status()}

    @app.post("/api/media/tidal/login")
    async def tidal_login(principal: Principal = Depends(require_authenticated)):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        # The one endpoint that creates an account: linking is what makes one.
        acct = _acct(svc, principal, create=True)
        link = await acct.login_start() if acct else None
        if not link:
            return {"success": False, "error": "Tidal unavailable (not installed/enabled)"}
        return {"success": True, "link": link}

    @app.post("/api/media/tidal/logout")
    async def tidal_logout(principal: Principal = Depends(require_authenticated)):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        acct = _acct(svc, principal)
        if acct:
            await acct.logout()
        return {"success": True}

    @app.get("/api/media/tidal/search")
    async def tidal_search(q: str, limit: int = 20,
                           principal: Principal = Depends(require_authenticated)):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _acct(svc, principal)
        if not src:
            return dict(NOT_LINKED)
        try:
            return {"success": True, "results": await src.search_grouped(q, limit)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/media/tidal/library")
    async def tidal_library(kind: str, limit: int = 100, offset: int = 0,
                            principal: Principal = Depends(require_authenticated)):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _acct(svc, principal)
        if not src:
            return dict(NOT_LINKED)
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
    async def tidal_play(body: TidalPlayBody,
                         principal: Principal = Depends(require_authenticated)):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        if not _acct(svc, principal):
            return dict(NOT_LINKED)
        try:
            # The username is stamped on every queued item, so the stream URL is
            # re-resolved on this account long after the request has gone.
            return await svc.play_tidal(body.player_id, body.kind, body.id,
                                        body.mode, principal.user.username)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/media/tidal/lyrics")
    async def tidal_lyrics(track_id: str,
                           principal: Principal = Depends(require_authenticated)):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _acct(svc, principal)
        if not src:
            return dict(NOT_LINKED)
        lyrics = await src.track_lyrics(track_id)
        if not lyrics:
            return {"success": False, "error": "No lyrics for this track"}
        return {"success": True, "lyrics": lyrics}

    @app.get("/api/media/tidal/track/{track_id}/context")
    async def tidal_track_context(track_id: str,
                                  principal: Principal = Depends(require_authenticated)):
        """Artist of the now-playing track + their other albums — powers the
        'artist radio' and 'more from this artist' actions on the player card."""
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _acct(svc, principal)
        if not src:
            return dict(NOT_LINKED)
        artist = await src.track_artist(track_id)
        if not artist:
            return {"success": False, "error": "No artist info for this track"}
        albums = await src.artist_albums(artist["id"]) if artist.get("id") else []
        return {"success": True, "artist": artist, "albums": albums}

    @app.get("/api/media/tidal/playlist/{playlist_id}")
    async def tidal_playlist(playlist_id: str,
                             principal: Principal = Depends(require_authenticated)):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _acct(svc, principal)
        if not src:
            return dict(NOT_LINKED)
        detail = await src.playlist_detail(playlist_id)
        if not detail:
            return {"success": False, "error": "Playlist not found"}
        return {"success": True, "playlist": detail}

    @app.post("/api/media/tidal/playlist/create")
    async def tidal_playlist_create(body: TidalPlaylistCreateBody,
                                    principal: Principal = Depends(require_authenticated)):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _acct(svc, principal)
        if not src:
            return dict(NOT_LINKED)
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
    async def tidal_playlist_edit(body: TidalPlaylistEditBody,
                                  principal: Principal = Depends(require_authenticated)):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _acct(svc, principal)
        if not src:
            return dict(NOT_LINKED)
        if body.action not in ("add", "remove", "move", "edit", "delete",
                               "visibility"):
            return {"success": False, "error": "unknown playlist action"}
        return await src.playlist_write(
            body.action, body.id, track_ids=body.track_ids,
            track_id=body.track_id, position=body.position, name=body.name,
            description=body.description, public=body.public,
            allow_duplicates=body.allow_duplicates)

    @app.get("/api/media/tidal/favorites")
    async def tidal_favorites(refresh: bool = False,
                              principal: Principal = Depends(require_authenticated)):
        """Which ids are already favourited, so the UI can draw a heart in the
        state it is in. `ready` false means a build is running — ask again.

        The *viewer's* favourites, not the owner's: the heart on a now-playing
        card reflects your library, whoever queued the track."""
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _acct(svc, principal)
        if not src:
            return dict(NOT_LINKED)
        try:
            return {"success": True, **await src.favourite_ids(refresh)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/media/tidal/favorite")
    async def tidal_favorite(body: TidalFavoriteBody,
                             principal: Principal = Depends(require_authenticated)):
        """Hearting a track adds it to *your* library, even one a housemate
        queued — which is the point of the account being yours."""
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _acct(svc, principal)
        if not src:
            return dict(NOT_LINKED)
        if body.kind not in ("track", "album", "artist", "playlist"):
            return {"success": False, "error": "kind must be track|album|artist|playlist"}
        if body.action not in ("add", "remove"):
            return {"success": False, "error": "action must be add|remove"}
        ok = await src.set_favorite(body.kind, body.id, body.action == "add")
        if not ok:
            return {"success": False, "error": "Favourite update failed (login required?)"}
        return {"success": True, "favorited": body.action == "add"}

    # AirPlay pairing. Only receivers with access control (Apple TV, HomePods
    # set to "require password/pairing") need it; the PIN appears on the TV.

    def _airplay(svc):
        return getattr(svc, "airplay", None) if svc else None

    @app.get("/api/media/airplay/devices")
    async def airplay_devices(_: Principal = Depends(require_scope("admin"))):
        ap = _airplay(_svc())
        if ap is None:
            return {"success": False, "error": "AirPlay is not enabled (or the media engine is off)"}
        players = await ap.list_players()
        return {"success": True, "devices": [
            {**p.to_dict(), "pairing_required": ap.pairing_required(p.player_id)}
            for p in players]}

    @app.post("/api/media/airplay/{player_id}/pair/begin")
    async def airplay_pair_begin(player_id: str,
                                 _: Principal = Depends(require_scope("admin"))):
        ap = _airplay(_svc())
        if ap is None:
            return {"success": False, "error": "AirPlay is not enabled"}
        try:
            return {"success": True, **await ap.pair_begin(player_id)}
        except Exception as e:
            logger.warning(f"AirPlay pairing start failed for {player_id}: {e}")
            return {"success": False, "error": str(e)}

    @app.post("/api/media/airplay/{player_id}/pair/finish")
    async def airplay_pair_finish(player_id: str, body: AirPlayPinBody,
                                  _: Principal = Depends(require_scope("admin"))):
        ap = _airplay(_svc())
        if ap is None:
            return {"success": False, "error": "AirPlay is not enabled"}
        try:
            if await ap.pair_finish(player_id, body.pin.strip()):
                return {"success": True}
            return {"success": False, "error": "Pairing failed — check the PIN and try again"}
        except Exception as e:
            logger.warning(f"AirPlay pairing failed for {player_id}: {e}")
            return {"success": False, "error": str(e)}

    @app.get("/api/media/tidal/accounts")
    async def tidal_accounts(_: Principal = Depends(require_scope("admin"))):
        """Who has linked a Tidal account. Admin-only, and read-only.

        Names and linked-state, never a token and never anyone's library: it
        exists so an admin can see who is set up and spot a login that was
        adopted from before Tidal was per-user and still needs claiming.
        """
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Media service not enabled"}
        src = _tidal(svc)
        if not src:
            return {"success": True, "accounts": [], "unassigned": ""}
        rows = src.accounts()
        return {"success": True, "accounts": rows,
                # Named separately so the UI can say what to do about it,
                # rather than showing a user nobody recognises.
                "unassigned": next((r["username"] for r in rows
                                    if r["username"] == src.UNASSIGNED), "")}

    @app.get("/api/media/tidal/manifest/{token}.mpd")
    async def tidal_manifest(token: str):
        # Served to Cast for lossless playback: a fresh DASH MPD whose segment
        # URLs point straight at Tidal's CDN. Generated on each fetch (URLs expire).
        #
        # The one Tidal route with no principal — the speaker fetches it itself
        # and carries no session — so the token in the URL is what says whose
        # account to serve, and which track. It is minted when the lossless URL
        # is built and expires on its own; a URL naming the user would let
        # anything on the LAN stream their Tidal.
        svc = _svc()
        if not svc:
            return Response("media service not enabled", status_code=503)
        src = _tidal(svc)
        if not src:
            return Response("tidal unavailable", status_code=503)
        got = src.redeem_manifest_token(token)
        if not got:
            # Unknown, expired, or naming an account that has since gone.
            return Response("unknown or expired manifest token", status_code=404)
        acct, track_id = got
        mpd = await acct.dash_manifest(track_id)
        if not mpd:
            return Response("no lossless manifest for track", status_code=404)
        # CORS: the Cast receiver XHR-fetches the MPD from its google-hosted
        # origin, so without this header DASH playback fails on the device.
        return Response(content=mpd, media_type="application/dash+xml",
                        headers={"Access-Control-Allow-Origin": "*"})

    logger.info("Media routes registered")
