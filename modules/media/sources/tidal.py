"""
Tidal source, over the unofficial tidalapi.

Serves AAC via a directly playable URL so devices need no stream server;
lossless DASH/FLAC is a later phase. Hard-isolated — imported lazily, every
failure swallowed, and every blocking call wrapped in asyncio.to_thread — so a
Tidal breakage never affects Cast, WiiM or radio. See docs/speaker_sync.md.

Two classes: ``TidalAccount`` is one ZMM user's login — their session, their
caches, their credential file — and ``TidalSource`` is the registry of them
that the controller actually registers, since it addresses one source per
media type. See docs/plans/tidal-per-user-auth.md.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import threading
import time
from typing import List, Optional

from modules.media.models import MediaItem
from modules.media.sources.base import SourceProvider

logger = logging.getLogger("modules.media.tidal")

# One credential file per ZMM user, named for them. The pre-multi-user single
# file is adopted into this directory on first start (_migrate_legacy).
SESSION_DIR = "./data/media/tidal"
LEGACY_SESSION_PATH = "./data/media/tidal_session.json"

# Owner of an adopted legacy session when no ZMM user can be identified as its
# owner. Deliberately outside the username charset, so no real account can
# collide with it and no request can name one.
UNASSIGNED = "~unassigned"

# A username becomes a filename here, so this is what keeps a path separator
# out of one. Mirrors auth._VALID_ID_RE and must never be looser than it.
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\-]{2,32}$")

# TIDAL caps a favourites request at 50 rows, so a larger page is assembled
# from several requests. A ceiling, not a preference.
LIBRARY_PAGE = 50
LIBRARY_MAX = 500

# A DASH manifest's segment URLs are signed for minutes, so this caches only
# long enough to bridge the lossless check and the fetch that follows it.
MPD_CACHE_TTL_S = 30
MPD_CACHE_MAX = 64

# The manifest route is fetched by the speaker, which carries no session, so the
# URL itself has to say which account to serve. A token rather than a username:
# the route is reachable by anything on the LAN, and a URL naming its owner
# would let any of it stream that user's Tidal. Long enough to survive a Cast
# refetch mid-track, short enough to be worthless afterwards; it also stops the
# URL naming the track. 16 bytes, not the EQ stream's 8 — this one names an
# account rather than a stream already playing, so a guess is worth more.
MANIFEST_TOKEN_TTL_S = 600
MANIFEST_TOKEN_MAX = 256
MANIFEST_TOKEN_BYTES = 16

# Favourite-state cache (docs/speaker_sync.md → Tidal).
FAVOURITE_KINDS = ("track", "album", "artist", "playlist")
FAVOURITE_TTL_S = 600
FAVOURITE_WALK_MAX = 3000       # bound on the fallback walk, per kind
# users/<id>/favorites/ids answers every kind in one request; it is undocumented
# and not in tidalapi, so the paged walk stays as the fallback.
FAVOURITE_IDS_TYPES = {"TRACK": "track", "ALBUM": "album",
                       "ARTIST": "artist", "PLAYLIST": "playlist"}


def _quality_enum(tidalapi, want: str):
    """Map our quality name to a tidalapi Quality enum, defensive across versions.

    ``want='lossless'`` → FLAC (16/44 lossless). Anything else → 320k AAC
    ("HIGH"), the single-URL stream that plays on every device. tidalapi 0.8
    renamed the members (low_320k/high_lossless); 0.7 used high/lossless."""
    Q = tidalapi.Quality
    if want == "lossless":
        for name in ("high_lossless", "lossless", "hi_res_lossless"):
            if hasattr(Q, name):
                return getattr(Q, name)
    for name in ("high", "low_320k", "low_96k", "low"):
        if hasattr(Q, name):
            return getattr(Q, name)
    return list(Q)[0]


class TidalAccount:
    """One ZMM user's Tidal login: their session, their caches, their file.

    Nothing here is shared between users. tidalapi has no per-call quality
    override — ``audio_quality`` is a property of the session object — so the
    swap in ``_aac_url`` now contends only with the same user's own playback
    rather than with everyone's.
    """

    def __init__(self, username: str, quality: str = "high",
                 manifest_base_url: str = "", local_base=None,
                 available: bool = False, mint_token=None):
        # The ZMM user this login belongs to, and the name of its session file.
        self.username = username
        # "high" (320k AAC, single URL, every device) or "lossless" (FLAC via a
        # DASH manifest — Cast only; WiiM transparently falls back to AAC).
        self._quality = (quality or "high").lower()
        # Public base URL of *this* app, reachable by Cast devices on the LAN,
        # used to serve the DASH manifest for lossless (e.g. "https://192.168.1.1:8000").
        # Empty → lossless disabled (AAC everywhere), so nothing breaks if unset.
        self._manifest_base = (manifest_base_url or "").rstrip("/")
        # () -> base URL of this host's plain-HTTP device listener. Lossless to
        # a zone is decoded here, so it needs no operator-supplied address.
        self._local_base = local_base
        # (owner, track_id) -> token for the manifest URL. Held by the registry,
        # because the route that redeems it has no account to start from.
        self._mint_token = mint_token
        # track_id -> (fetched_at, mpd). The zone path checks a track has a
        # lossless variant before committing to it; without this the route
        # would then fetch the same manifest again seconds later.
        self._mpd_cache: dict = {}
        self._session = None
        # tidalapi importable — resolved once by the registry, not per user.
        self._available = available
        self._login_future = None
        self._login_link: Optional[str] = None
        self._pending = False
        # Serialises the brief session.audio_quality swap during stream resolution
        # (tidalapi has no per-call quality override; the swap is global).
        self._stream_lock = threading.Lock()
        # Favourited ids per kind, so a heart can be drawn in the state it is
        # actually in. Built off the request path — see favourite_ids().
        self._fav_ids: dict = {k: set() for k in FAVOURITE_KINDS}
        self._fav_built_at: float = 0.0
        self._fav_task: Optional[asyncio.Task] = None

    @property
    def session_path(self) -> str:
        return os.path.join(SESSION_DIR, f"{self.username}.json")

    async def load(self) -> None:
        """Build the session and restore this user's saved login, if any."""
        if not self._available:
            return
        await asyncio.to_thread(self._build_session)

    def _build_session(self):
        import tidalapi
        try:
            self._session = tidalapi.Session()
            self._session.audio_quality = _quality_enum(tidalapi, self._quality)
            saved = self._load_session_file()
            if saved:
                ok = self._session.load_oauth_session(
                    saved.get("token_type"),
                    saved.get("access_token"),
                    saved.get("refresh_token"),
                    saved.get("expiry_time"),
                )
                if ok and self._session.check_login():
                    logger.info(f"Tidal session restored for {self.username}")
                else:
                    logger.info(f"Tidal saved session invalid for {self.username} — login required")
        except Exception as e:
            logger.warning(f"Tidal session init failed for {self.username}: {e}")

    # Session persistence
    def _load_session_file(self) -> Optional[dict]:
        try:
            if os.path.exists(self.session_path):
                with open(self.session_path, "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.debug(f"Tidal session read failed for {self.username}: {e}")
        return None

    def _persist_session(self):
        try:
            os.makedirs(SESSION_DIR, exist_ok=True)
            s = self._session
            expiry = s.expiry_time
            with open(self.session_path, "w") as f:
                json.dump({
                    "token_type": s.token_type,
                    "access_token": s.access_token,
                    "refresh_token": s.refresh_token,
                    # expiry_time may be a datetime — store epoch for portability.
                    "expiry_time": expiry.timestamp() if hasattr(expiry, "timestamp") else expiry,
                }, f)
            # The file holds a refresh token, so it is readable only by us.
            try:
                os.chmod(self.session_path, 0o600)
            except OSError:
                pass
            logger.info(f"Tidal session persisted for {self.username}")
        except Exception as e:
            logger.warning(f"Tidal session persist failed for {self.username}: {e}")

    # Login (web flow)
    async def login_start(self) -> Optional[str]:
        if not self._available or not self._session:
            return None
        login, future = await asyncio.to_thread(self._session.login_oauth)
        link = getattr(login, "verification_uri_complete", "")
        self._login_link = link if link.startswith("http") else f"https://{link}"
        self._login_future = future
        self._pending = True
        asyncio.create_task(self._await_login())
        return self._login_link

    async def _await_login(self):
        try:
            await asyncio.to_thread(self._login_future.result)  # blocks until done/expiry
            if self._session.check_login():
                await asyncio.to_thread(self._persist_session)
                logger.info(f"Tidal login complete for {self.username}")
        except Exception as e:
            logger.warning(f"Tidal login did not complete for {self.username}: {e}")
        finally:
            self._pending = False
            self._login_future = None
            self._login_link = None

    async def status(self) -> dict:
        if not self._available:
            return {"state": "unavailable"}
        if self._pending:
            return {"state": "pending", "link": self._login_link}
        try:
            if self._session and await asyncio.to_thread(self._session.check_login):
                user = getattr(self._session, "user", None)
                name = getattr(user, "username", None) or getattr(user, "id", "")
                return {"state": "logged_in", "user": str(name)}
        except Exception:
            pass
        return {"state": "logged_out"}

    async def logout(self) -> None:
        try:
            if os.path.exists(self.session_path):
                os.remove(self.session_path)
        except Exception:
            pass
        # Rebuild a fresh, logged-out session.
        await asyncio.to_thread(self._build_session_fresh)

    def _build_session_fresh(self):
        import tidalapi
        self._session = tidalapi.Session()
        self._session.audio_quality = _quality_enum(tidalapi, self._quality)

    # URL resolution (registered with the controller for media_type="tidal")
    async def resolve_url(self, source_id: str, provider: Optional[str] = None):
        """Return a fresh, directly-playable stream for ``source_id``.

        Returns a ``{"url", "content_type"}`` dict (the controller applies both).
        Lossless is handed over as a URL to *our own* DASH-manifest route, which
        Cast fetches over the LAN and a zone's decoder fetches over loopback;
        otherwise a single 320k-AAC URL that plays on Cast and WiiM alike.
        See docs/speaker_sync.md → Tidal."""
        if not self._session:
            return None
        base = self._lossless_base(provider)
        if base and self._mint_token:
            token = self._mint_token(self.username, source_id)
            mpd = f"{base}/api/media/tidal/manifest/{token}.mpd"
            dash = {"url": mpd, "content_type": "application/dash+xml"}
            if provider != "zone":
                return dash
            # A zone's decoder retries one item forever rather than skipping,
            # so a track with no lossless variant has to be found here, where
            # falling back to AAC still costs nothing. Warms the manifest
            # cache the route then serves.
            if await self.dash_manifest(source_id):
                return dash
            logger.info(f"Tidal {source_id} has no lossless variant — zone gets AAC")
        url = await asyncio.to_thread(self._aac_url, source_id)
        return {"url": url, "content_type": "audio/mp4"} if url else None

    def _lossless_base(self, provider: Optional[str]) -> str:
        """Base URL the manifest route should be fetched from for this target,
        or "" when lossless doesn't apply.

        Cast needs an address reachable across the LAN, which only the operator
        can supply. A zone decodes on this host, so it needs nothing configured
        — the plain-HTTP device listener on loopback already serves the route.
        WiiM (LinkPlay) cannot play DASH at all.
        """
        if self._quality != "lossless":
            return ""
        if provider == "cast":
            return self._manifest_base
        if provider == "zone" and self._local_base:
            try:
                return (self._local_base() or "").rstrip("/")
            except Exception:
                return ""
        return ""

    def _aac_url(self, track_id: str) -> Optional[str]:
        """A single directly-playable 320k-AAC URL (the BTS manifest path).

        tidalapi 0.7 exposed ``Track.get_url()``; 0.8 removed it for
        ``get_stream()`` → a base64 manifest carrying plain ``urls``."""
        import tidalapi
        try:
            track = self._session.track(int(track_id))
        except Exception as e:
            logger.warning(f"Tidal track lookup failed for {track_id}: {e}")
            return None
        with self._stream_lock:
            prev = self._session.audio_quality
            self._session.audio_quality = _quality_enum(tidalapi, "high")
            try:
                getter = getattr(track, "get_url", None)   # 0.7 fast-path
                if callable(getter):
                    try:
                        u = getter()
                        if u:
                            return u
                    except Exception:
                        pass
                stream = track.get_stream()
            except Exception as e:
                logger.warning(f"Tidal get_stream failed for {track_id}: {e}")
                return None
            finally:
                self._session.audio_quality = prev
        return self._url_from_bts(stream)

    def _url_from_bts(self, stream) -> Optional[str]:
        try:
            urls = stream.get_stream_manifest().get_urls()
            if urls:
                return urls[0]
        except Exception:
            pass
        try:
            raw = base64.b64decode(stream.manifest).decode("utf-8")
            urls = (json.loads(raw) or {}).get("urls") or []
            if urls:
                return urls[0]
        except Exception as e:
            logger.warning(f"Tidal manifest parse failed: {e}")
        return None

    async def dash_manifest(self, track_id: str) -> Optional[str]:
        """The raw DASH MPD (XML) for a lossless track, segment URLs and all —
        served by the manifest route. Cached only for the seconds between the
        zone path checking a track is lossless and the route serving it; the
        segment URLs inside are short-lived, so nothing older is reused."""
        if not self._session:
            return None
        hit = self._mpd_cache.get(track_id)
        if hit and (time.time() - hit[0]) < MPD_CACHE_TTL_S:
            return hit[1]
        mpd = await asyncio.to_thread(self._dash_manifest, track_id)
        if mpd:
            if len(self._mpd_cache) > MPD_CACHE_MAX:
                self._mpd_cache.clear()
            self._mpd_cache[track_id] = (time.time(), mpd)
        return mpd

    def _dash_manifest(self, track_id: str) -> Optional[str]:
        import tidalapi
        try:
            track = self._session.track(int(track_id))
        except Exception as e:
            logger.warning(f"Tidal track lookup failed for {track_id}: {e}")
            return None
        with self._stream_lock:
            prev = self._session.audio_quality
            self._session.audio_quality = _quality_enum(tidalapi, "lossless")
            try:
                stream = track.get_stream()
            except Exception as e:
                logger.warning(f"Tidal lossless stream failed for {track_id}: {e}")
                return None
            finally:
                self._session.audio_quality = prev
        try:
            raw = base64.b64decode(stream.manifest).decode("utf-8")
            # A JSON (BTS) manifest means this track has no DASH/lossless variant.
            if raw.lstrip().startswith("{"):
                logger.info(f"Tidal track {track_id}: no DASH manifest (not lossless)")
                return None
            return raw
        except Exception as e:
            logger.warning(f"Tidal DASH decode failed for {track_id}: {e}")
            return None

    # Search / browse
    async def search(self, query: str, limit: int = 25) -> List[MediaItem]:
        if not self._session:
            return []
        return await asyncio.to_thread(self._search_tracks, query, limit)

    def _search_tracks(self, query: str, limit: int) -> List[MediaItem]:
        try:
            res = self._session.search(query, limit=limit)
        except TypeError:
            res = self._session.search(query)
        except Exception as e:
            logger.warning(f"Tidal search failed: {e}")
            return []
        tracks = (res or {}).get("tracks", []) if isinstance(res, dict) else getattr(res, "tracks", [])
        return [self._track_to_item(t) for t in (tracks or [])[:limit]]

    async def search_grouped(self, query: str, limit: int = 20) -> dict:
        """Tracks + artists + albums + playlists, for a richer browse UI."""
        if not self._session:
            return {"tracks": [], "artists": [], "albums": [], "playlists": []}
        return await asyncio.to_thread(self._search_grouped, query, limit)

    def _search_grouped(self, query: str, limit: int) -> dict:
        empty = {"tracks": [], "artists": [], "albums": [], "playlists": []}
        try:
            res = self._session.search(query, limit=limit)
        except Exception as e:
            logger.warning(f"Tidal search failed: {e}")
            return empty
        pick = (res.get if isinstance(res, dict)
                else lambda k, d: getattr(res, k, d))
        tracks = pick("tracks", []) or []
        artists = pick("artists", []) or []
        albums = pick("albums", []) or []
        playlists = pick("playlists", []) or []
        return {
            "tracks": [self._track_to_item(t).to_dict() for t in tracks[:limit]],
            "artists": [self._artist_summary(a) for a in artists[:limit]],
            "albums": [self._album_summary(a) for a in albums[:limit]],
            "playlists": [self._playlist_summary(p) for p in playlists[:limit]],
        }

    async def album_items(self, album_id: str) -> List[MediaItem]:
        if not self._session:
            return []
        return await asyncio.to_thread(self._album_items, album_id)

    def _album_items(self, album_id: str) -> List[MediaItem]:
        try:
            tracks = self._session.album(int(album_id)).tracks()
            return [self._track_to_item(t) for t in tracks]
        except Exception as e:
            logger.warning(f"Tidal album load failed: {e}")
            return []

    async def playlist_items(self, playlist_id: str) -> List[MediaItem]:
        if not self._session:
            return []
        return await asyncio.to_thread(self._playlist_items, playlist_id)

    def _playlist_items(self, playlist_id: str) -> List[MediaItem]:
        try:
            tracks = self._session.playlist(playlist_id).tracks()
            return [self._track_to_item(t) for t in tracks]
        except Exception as e:
            logger.warning(f"Tidal playlist load failed: {e}")
            return []

    # User library (favourites + own playlists)
    LIBRARY_KINDS = ("playlists", "albums", "artists", "tracks", "mixes")

    async def library(self, kind: str, limit: int = 100,
                      offset: int = 0) -> dict:
        """One page of the user's library.

        Returns ``{"items", "offset", "has_more", "total"}``. ``total`` is None
        when the kind can't report one. See docs/speaker_sync.md → Tidal.
        """
        empty = {"items": [], "offset": offset, "has_more": False, "total": None}
        if not self._session:
            return empty
        limit = max(1, min(int(limit or 100), LIBRARY_MAX))
        offset = max(0, int(offset or 0))
        return await asyncio.to_thread(self._library, kind, limit, offset)

    def _library(self, kind: str, limit: int, offset: int) -> dict:
        if kind == "mixes":
            # A curated page rather than an offset list — it arrives whole.
            rows = self._mixes()
            return {"items": rows, "offset": 0, "has_more": False,
                    "total": len(rows)}
        try:
            fetch, summarise = self._library_source(kind)
        except Exception as e:
            logger.warning(f"Tidal library({kind}) unavailable: {e}")
            fetch = None
        if fetch is None:
            return {"items": [], "offset": offset, "has_more": False,
                    "total": None}

        items, seen, pos, exhausted = [], set(), offset, False
        while len(items) < limit and not exhausted:
            want = min(LIBRARY_PAGE, limit - len(items))
            try:
                rows = list(fetch(want, pos) or [])
            except Exception as e:
                logger.warning(f"Tidal library({kind}) page at {pos} failed: {e}")
                break
            pos += len(rows)
            if len(rows) < want:
                exhausted = True
            for r in rows:
                try:
                    row = summarise(r)
                except Exception:
                    continue
                if row["id"] and row["id"] not in seen:
                    seen.add(row["id"])
                    items.append(row)
        return {"items": items, "offset": offset,
                "has_more": not exhausted,
                "total": self._library_total(kind) if offset == 0 else None}

    def _library_source(self, kind: str):
        """``((limit, offset) -> rows, row -> summary)`` for a library kind."""
        user = self._session.user
        fav = getattr(user, "favorites", None)
        if kind == "albums" and fav:
            return (lambda n, o: fav.albums(limit=n, offset=o),
                    self._album_summary)
        if kind == "artists" and fav:
            return (lambda n, o: fav.artists(limit=n, offset=o),
                    self._artist_summary)
        if kind == "tracks" and fav:
            return (lambda n, o: fav.tracks(limit=n, offset=o),
                    self._track_summary)
        if kind == "playlists":
            # Own + followed, which only this endpoint returns together; it
            # pages, where LoggedInUser.playlists() does not.
            fn = getattr(user, "playlist_and_favorite_playlists", None)
            if callable(fn):
                return (lambda n, o: fn(offset=o, limit=n),
                        self._playlist_row_summary)
            if fav:
                return (lambda n, o: fav.playlists(limit=n, offset=o),
                        self._playlist_row_summary)
        return None, None

    def _library_total(self, kind: str) -> Optional[int]:
        """Total rows for a kind — one limit=1 request, so cheap enough to ask
        for on the first page only."""
        fav = getattr(getattr(self._session, "user", None), "favorites", None)
        fn = getattr(fav, {
            "albums": "get_albums_count", "artists": "get_artists_count",
            "tracks": "get_tracks_count", "playlists": "get_playlists_count",
        }.get(kind, ""), None) if fav else None
        if not callable(fn):
            return None
        try:
            return int(fn())
        except Exception as e:
            logger.debug(f"Tidal {kind} count unavailable: {e}")
            return None

    def _playlist_row_summary(self, r) -> dict:
        # Some versions return (playlist, type) pairs.
        return self._playlist_summary(r[0] if isinstance(r, tuple) else r)

    # Artist play / radio (infinite)
    async def artist_tracks(self, artist_id: str) -> List[MediaItem]:
        if not self._session:
            return []
        return await asyncio.to_thread(self._artist_tracks, artist_id)

    def _artist_tracks(self, artist_id: str) -> List[MediaItem]:
        try:
            artist = self._session.artist(int(artist_id))
            tracks = artist.get_top_tracks(limit=50)
            return [self._track_to_item(t) for t in (tracks or [])]
        except Exception as e:
            logger.warning(f"Tidal artist tracks failed: {e}")
            return []

    async def artist_radio(self, artist_id: str) -> List[MediaItem]:
        if not self._session:
            return []
        return await asyncio.to_thread(self._artist_radio, artist_id)

    def _artist_radio(self, artist_id: str) -> List[MediaItem]:
        try:
            artist = self._session.artist(int(artist_id))
            tracks = artist.get_radio()
            return [self._track_to_item(t) for t in (tracks or [])]
        except Exception as e:
            logger.warning(f"Tidal artist radio failed: {e}")
            return []

    async def track_radio(self, track_id: str) -> List[MediaItem]:
        """Tracks similar to a seed track — used for 'radio' and infinite extend."""
        if not self._session:
            return []
        return await asyncio.to_thread(self._track_radio, track_id)

    def _track_radio(self, track_id: str) -> List[MediaItem]:
        try:
            # In 0.8 track radio lives on the Session.
            if hasattr(self._session, "get_track_radio"):
                tracks = self._session.get_track_radio(int(track_id))
            else:
                tracks = self._session.track(int(track_id)).get_track_radio()
            return [self._track_to_item(t) for t in (tracks or [])]
        except Exception as e:
            logger.warning(f"Tidal track radio failed: {e}")
            return []

    async def track_artist(self, track_id: str) -> Optional[dict]:
        """The primary artist of a track (id/name/picture) — lets the now-playing
        card offer artist radio + the artist's other albums."""
        if not self._session:
            return None
        return await asyncio.to_thread(self._track_artist, track_id)

    def _track_artist(self, track_id: str) -> Optional[dict]:
        try:
            artist = getattr(self._session.track(int(track_id)), "artist", None)
            return self._artist_summary(artist) if artist else None
        except Exception as e:
            logger.debug(f"Tidal track artist lookup failed for {track_id}: {e}")
            return None

    async def artist_albums(self, artist_id: str) -> List[dict]:
        """Summaries of an artist's albums (for 'more from this artist')."""
        if not self._session:
            return []
        return await asyncio.to_thread(self._artist_albums, artist_id)

    def _artist_albums(self, artist_id: str) -> List[dict]:
        try:
            albums = self._session.artist(int(artist_id)).get_albums() or []
            return [self._album_summary(a) for a in albums]
        except Exception as e:
            logger.warning(f"Tidal artist albums failed for {artist_id}: {e}")
            return []

    async def single_item(self, track_id: str) -> Optional[MediaItem]:
        if not self._session:
            return None
        return await asyncio.to_thread(self._single_item, track_id)

    def _single_item(self, track_id: str) -> Optional[MediaItem]:
        try:
            return self._track_to_item(self._session.track(int(track_id)))
        except Exception as e:
            logger.warning(f"Tidal track load failed: {e}")
            return None

    # Lyrics
    async def track_lyrics(self, track_id: str) -> Optional[dict]:
        if not self._session:
            return None
        return await asyncio.to_thread(self._track_lyrics, track_id)

    def _track_lyrics(self, track_id: str) -> Optional[dict]:
        try:
            lyr = self._session.track(int(track_id)).lyrics()
        except Exception as e:
            # tidalapi raises when a track simply has no lyrics — not an error.
            logger.debug(f"Tidal lyrics unavailable for {track_id}: {e}")
            return None
        if not lyr:
            return None
        text = getattr(lyr, "text", "") or ""
        synced = getattr(lyr, "subtitles", "") or ""   # LRC-style, time-tagged
        if not (text or synced):
            return None
        return {"text": text, "synced": synced, "is_synced": bool(synced)}

    # Favourites (write)
    async def set_favorite(self, kind: str, item_id: str, on: bool) -> bool:
        if not self._session:
            return False
        return await asyncio.to_thread(self._set_favorite, kind, item_id, on)

    def _set_favorite(self, kind: str, item_id: str, on: bool) -> bool:
        fav = getattr(getattr(self._session, "user", None), "favorites", None)
        if not fav:
            return False
        verbs = {
            "track":    ("add_track", "remove_track"),
            "album":    ("add_album", "remove_album"),
            "artist":   ("add_artist", "remove_artist"),
            "playlist": ("add_playlist", "remove_playlist"),
        }.get(kind)
        if not verbs:
            return False
        fn = getattr(fav, verbs[0] if on else verbs[1], None)
        if not callable(fn):
            return False
        try:
            # Playlist ids are UUID strings; tracks/albums/artists are ints.
            fn(item_id if kind == "playlist" else int(item_id))
        except Exception as e:
            logger.warning(f"Tidal favourite {kind} {'add' if on else 'remove'} failed: {e}")
            return False
        # Keep the cache in step rather than waiting out its TTL, or the heart
        # this click just filled would empty again on the next render.
        ids = self._fav_ids.get(kind)
        if ids is not None:
            ids.add(str(item_id)) if on else ids.discard(str(item_id))
        return True

    # Favourites (read) — which ids are already favourited
    async def favourite_ids(self, refresh: bool = False) -> dict:
        """Favourited ids per kind, for drawing a heart in the right state.

        Never blocks on a rebuild: a stale or absent cache is served as-is with
        ``ready`` false while a background task fills it, because the fallback
        walk costs one request per 50 rows and no render should wait for it.
        """
        out = {"ids": {k: sorted(v) for k, v in self._fav_ids.items()},
               "ready": self._fav_fresh(), "age_s": self._fav_age()}
        if not self._session:
            return {**out, "ready": False}
        if refresh or not self._fav_fresh():
            self._start_fav_build()
        return out

    def _fav_fresh(self) -> bool:
        return bool(self._fav_built_at
                    and (time.time() - self._fav_built_at) < FAVOURITE_TTL_S)

    def _fav_age(self) -> Optional[float]:
        return round(time.time() - self._fav_built_at, 1) if self._fav_built_at else None

    def _start_fav_build(self) -> None:
        if self._fav_task is not None and not self._fav_task.done():
            return
        self._fav_task = asyncio.create_task(self._build_fav_ids())

    async def _build_fav_ids(self) -> None:
        try:
            ids = await asyncio.to_thread(self._collect_fav_ids)
        except Exception as e:
            logger.warning(f"Tidal favourite ids build failed: {e}")
            return
        if ids is None:
            return
        self._fav_ids = ids
        self._fav_built_at = time.time()
        logger.info("Tidal favourites cached: "
                    + ", ".join(f"{len(v)} {k}s" for k, v in ids.items()))

    def _collect_fav_ids(self) -> Optional[dict]:
        return self._fav_ids_bulk() or self._fav_ids_walked()

    def _fav_ids_bulk(self) -> Optional[dict]:
        """One request for every kind, where the account's API offers it."""
        try:
            user = self._session.user
            resp = self._session.request.request(
                "GET", f"users/{user.id}/favorites/ids")
            data = resp.json() if hasattr(resp, "json") else resp
        except Exception as e:
            logger.debug(f"Tidal bulk favourite ids unavailable: {e}")
            return None
        if not isinstance(data, dict):
            return None
        out = {k: set() for k in FAVOURITE_KINDS}
        seen = False
        for api_type, kind in FAVOURITE_IDS_TYPES.items():
            rows = data.get(api_type)
            if isinstance(rows, list):
                seen = True
                out[kind] = {str(r) for r in rows if r not in (None, "")}
        return out if seen else None

    def _fav_ids_walked(self) -> dict:
        """Page every favourites list for its ids. The slow path, bounded."""
        out = {k: set() for k in FAVOURITE_KINDS}
        for kind in FAVOURITE_KINDS:
            try:
                fetch, summarise = self._library_source(f"{kind}s")
            except Exception:
                fetch = None
            if fetch is None:
                continue
            pos = 0
            while pos < FAVOURITE_WALK_MAX:
                try:
                    rows = list(fetch(LIBRARY_PAGE, pos) or [])
                except Exception as e:
                    logger.warning(f"Tidal favourite {kind} ids at {pos}: {e}")
                    break
                for r in rows:
                    try:
                        out[kind].add(summarise(r)["id"])
                    except Exception:
                        continue
                pos += len(rows)
                if len(rows) < LIBRARY_PAGE:
                    break
        return out

    # Playlist management (owned playlists only — see docs/speaker_sync.md)
    async def playlist_detail(self, playlist_id: str) -> Optional[dict]:
        """A playlist's metadata and ordered tracks, for an editing view."""
        if not self._session:
            return None
        return await asyncio.to_thread(self._playlist_detail, playlist_id)

    def _playlist_detail(self, playlist_id: str) -> Optional[dict]:
        try:
            pl = self._session.playlist(playlist_id)
            tracks = pl.tracks()
        except Exception as e:
            logger.warning(f"Tidal playlist {playlist_id} load failed: {e}")
            return None
        return {
            **self._playlist_summary(pl),
            "description": getattr(pl, "description", "") or "",
            "public": bool(getattr(pl, "public", False)),
            "tracks": [self._track_summary(t) for t in (tracks or [])],
        }

    async def playlist_create(self, name: str, description: str = "") -> dict:
        if not self._session:
            return {"success": False, "error": "Not signed in to Tidal"}
        name = (name or "").strip()
        if not name:
            return {"success": False, "error": "A playlist needs a name"}
        return await asyncio.to_thread(self._playlist_create, name, description)

    def _playlist_create(self, name: str, description: str) -> dict:
        try:
            user = self._session.user
            pl = user.create_playlist(name, description or "")
        except Exception as e:
            logger.warning(f"Tidal playlist create failed: {e}")
            return {"success": False, "error": f"Could not create the playlist: {e}"}
        logger.info(f"Tidal playlist created: {name}")
        return {"success": True, "playlist": self._playlist_summary(pl)}

    async def playlist_write(self, action: str, playlist_id: str, **kw) -> dict:
        """Every mutation of an owned playlist, behind one entry point so the
        ownership check and the error wording are stated once."""
        if not self._session:
            return {"success": False, "error": "Not signed in to Tidal"}
        return await asyncio.to_thread(self._playlist_write, action, playlist_id, kw)

    def _playlist_write(self, action: str, playlist_id: str, kw: dict) -> dict:
        try:
            pl = self._session.playlist(playlist_id).factory()
        except Exception as e:
            logger.warning(f"Tidal playlist {playlist_id} lookup failed: {e}")
            return {"success": False, "error": "Playlist not found"}
        # factory() hands back a plain Playlist for anything the user did not
        # create, which has no write methods at all — a followed playlist is
        # someone else's, and Tidal will not take an edit to it.
        if type(pl).__name__ != "UserPlaylist":
            return {"success": False,
                    "error": "That playlist belongs to someone else — "
                             "only your own can be edited"}
        try:
            ok, extra = self._playlist_apply(pl, action, kw)
        except Exception as e:
            logger.warning(f"Tidal playlist {action} on {playlist_id} failed: {e}")
            return {"success": False, "error": str(e)}
        if not ok:
            return {"success": False, "error": f"Tidal rejected the {action}"}
        logger.info(f"Tidal playlist {playlist_id}: {action}")
        return {"success": True, **(extra or {})}

    def _playlist_apply(self, pl, action: str, kw: dict):
        if action == "add":
            ids = [str(i) for i in (kw.get("track_ids") or []) if str(i).strip()]
            if not ids:
                raise ValueError("No tracks to add")
            added = pl.add(ids, allow_duplicates=bool(kw.get("allow_duplicates")))
            # SKIP on duplicates means an empty result is "already there",
            # which is a no-op rather than a failure worth reporting as one.
            return True, {"added": len(added or []), "requested": len(ids)}
        if action == "remove":
            # By id, not by index: the caller's view of the order may be stale,
            # and removing the wrong track is not a recoverable mistake.
            return bool(pl.remove_by_id(str(kw.get("track_id") or ""))), None
        if action == "move":
            return bool(pl.move_by_id(str(kw.get("track_id") or ""),
                                      int(kw.get("position", 0)))), None
        if action == "edit":
            name = (kw.get("name") or "").strip() or None
            desc = kw.get("description")
            return bool(pl.edit(title=name, description=desc)), None
        if action == "delete":
            return bool(pl.delete()), None
        if action == "visibility":
            return (bool(pl.set_playlist_public() if kw.get("public")
                         else pl.set_playlist_private()), None)
        raise ValueError(f"Unknown playlist action '{action}'")

    # Mixes (personalised — "My Daily Discovery", "Mix 1-8", "New Arrivals")
    def _mixes(self) -> List[dict]:
        rows = []
        for getter in (getattr(self._session, "mixes", None),
                       getattr(getattr(self._session, "user", None), "mixes", None)):
            if callable(getter):
                try:
                    rows = getter()
                    if rows:
                        break
                except Exception:
                    continue
        out = []
        for m in (rows or []):
            try:
                out.append(self._mix_summary(m))
            except Exception:
                continue
        return out

    async def mix_items(self, mix_id: str) -> List[MediaItem]:
        if not self._session:
            return []
        return await asyncio.to_thread(self._mix_items, mix_id)

    def _mix_items(self, mix_id: str) -> List[MediaItem]:
        try:
            items = self._session.mix(mix_id).items()
        except Exception as e:
            logger.warning(f"Tidal mix load failed: {e}")
            return []
        # A mix can contain videos too — keep tracks only (type-name is version-safe).
        return [self._track_to_item(it) for it in (items or [])
                if type(it).__name__ == "Track"]

    # Mapping helpers
    def _track_to_item(self, t) -> MediaItem:
        artist = getattr(getattr(t, "artist", None), "name", "") or ""
        artwork = ""
        try:
            album = getattr(t, "album", None)
            if album is not None:
                artwork = album.image(320)
        except Exception:
            pass
        # URL is intentionally left blank: the controller re-resolves it fresh at
        # play time via the registered resolver (resolve_url). Fetching a stream
        # URL per search result would be a network round-trip each — and Tidal
        # stream URLs are time-limited, so a search-time URL would be stale anyway.
        return MediaItem(
            url="",
            title=getattr(t, "name", "") or "Unknown",
            artist=artist,
            artwork_url=artwork or "",
            media_type="tidal",
            content_type="audio/mp4",        # HIGH = AAC in an MP4 container
            source_id=str(getattr(t, "id", "")),
            duration_ms=int((getattr(t, "duration", 0) or 0) * 1000),
            # Whose account this came from, and whose it must be played back
            # through when its signed URL is re-resolved later.
            owner=self.username,
        )

    def _track_summary(self, t) -> dict:
        artwork = ""
        try:
            album = getattr(t, "album", None)
            artwork = album.image(320) if album is not None else ""
        except Exception:
            pass
        return {
            "id": str(getattr(t, "id", "")),
            "name": getattr(t, "name", "") or "Unknown",
            "artist": getattr(getattr(t, "artist", None), "name", "") or "",
            "artwork": artwork,
            "type": "track",
            "duration_ms": int((getattr(t, "duration", 0) or 0) * 1000),
        }

    def _album_summary(self, a) -> dict:
        artwork = ""
        try:
            artwork = a.image(320)
        except Exception:
            pass
        return {
            "id": str(getattr(a, "id", "")),
            "name": getattr(a, "name", ""),
            "artist": getattr(getattr(a, "artist", None), "name", "") or "",
            "artwork": artwork,
            "type": "album",
        }

    def _playlist_summary(self, p) -> dict:
        artwork = ""
        try:
            artwork = p.image(320)
        except Exception:
            pass
        return {
            "id": str(getattr(p, "id", "")),
            "name": getattr(p, "name", ""),
            "artist": f"{getattr(p, 'num_tracks', '')} tracks",
            "artwork": artwork,
            "type": "playlist",
            # Only the creator can edit one, so the UI must know which is which
            # before it offers a rename or a remove.
            "owned": self._owns(p),
        }

    def _owns(self, p) -> bool:
        """Whether a write to this playlist can succeed.

        Two signals, either sufficient. Library rows arrive through
        ``parse_factory``, which has already upgraded the user's own to
        ``UserPlaylist`` — the very decision the write path repeats. A single
        playlist fetched by id has not been through it, so fall back to
        comparing creators. Answering with the write path's own test is what
        stops the UI offering an edit that Tidal will refuse.
        """
        if type(p).__name__ == "UserPlaylist":
            return True
        uid = getattr(getattr(self._session, "user", None), "id", None)
        cid = getattr(getattr(p, "creator", None), "id", None)
        return bool(uid is not None and cid is not None and str(cid) == str(uid))

    def _artist_summary(self, a) -> dict:
        artwork = ""
        try:
            artwork = a.image(320)        # artist picture
        except Exception:
            pass
        return {
            "id": str(getattr(a, "id", "")),
            "name": getattr(a, "name", ""),
            "artist": "Artist",
            "artwork": artwork,
            "type": "artist",
        }

    def _mix_summary(self, m) -> dict:
        artwork = ""
        try:
            artwork = m.image(320)
        except Exception:
            pass
        return {
            "id": str(getattr(m, "id", "")),
            "name": getattr(m, "title", "") or getattr(m, "name", "") or "Mix",
            "artist": getattr(m, "sub_title", "") or "Mix",
            "artwork": artwork,
            "type": "mix",
        }


class TidalSource(SourceProvider):
    """The registered source: a registry of per-user Tidal accounts.

    The controller addresses one source per media type, so this stays the
    single registered object. It owns no session itself — every call lands on
    the account for one ZMM user.

    Every caller names a user, save the handful that cannot — a rule written
    before a step carried an owner, and the four bound methods below — which
    take ``default_account()`` explicitly rather than by falling through to it.
    See docs/plans/tidal-per-user-auth.md.
    """
    source = "tidal"
    LIBRARY_KINDS = TidalAccount.LIBRARY_KINDS
    #: Owner of a login adopted from before Tidal was per-user, when no ZMM
    #: user could be identified for it. Exposed so callers can name it without
    #: reaching for a module constant.
    UNASSIGNED = UNASSIGNED

    def __init__(self, enabled: bool = False, quality: str = "high",
                 manifest_base_url: str = "", local_base=None,
                 owner: str = ""):
        self.enabled = enabled
        self._quality = (quality or "high").lower()
        self._manifest_base = (manifest_base_url or "").rstrip("/")
        self._local_base = local_base
        # media.tidal.owner — names the ZMM user who owns a session that
        # predates per-user auth, and the default account thereafter.
        self._owner_hint = (owner or "").strip()
        self._available = False           # tidalapi importable
        self._accounts: dict = {}         # username -> TidalAccount
        # token -> (owner, track_id, issued_at). What the manifest route
        # redeems: it is fetched by a speaker with no session, so this is the
        # only thing saying whose account to serve.
        self._manifest_tokens: dict = {}

    # Lifecycle
    async def start(self) -> None:
        if not self.enabled:
            return
        try:
            import tidalapi  # noqa: F401
        except ImportError:
            logger.warning("Tidal disabled: tidalapi not installed")
            return
        self._available = True
        self._migrate_legacy()
        names = self._stored_usernames()
        if not names:
            logger.info("Tidal enabled; no accounts linked yet")
            return
        await asyncio.gather(*(self._ensure(n).load() for n in names),
                             return_exceptions=True)
        logger.info(f"Tidal accounts loaded: {', '.join(sorted(names))}")

    async def stop(self) -> None:
        pass

    # Accounts
    def account(self, username: str, create: bool = False) -> Optional[TidalAccount]:
        """This user's account, or None.

        ``create`` builds an empty (logged-out) one, which is what linking a
        Tidal login to a ZMM user starts from. A name that could not have come
        from AuthManager is refused outright rather than reaching the
        filesystem, because it would name a file.
        """
        name = (username or "").strip()
        if not USERNAME_RE.match(name):
            logger.warning(f"Tidal: refusing account for invalid username {name!r}")
            return None
        acct = self._accounts.get(name)
        if acct is None and create:
            acct = self._ensure(name)
        return acct

    def accounts(self) -> List[dict]:
        """Who has linked an account, for the admin view. Never any token.

        The placeholder is listed only when it holds an adopted login nobody
        has been able to claim — that is a real thing an admin has to resolve,
        whereas the empty one is an artefact of somebody asking for a status.
        """
        rows = []
        for name in sorted(self._accounts):
            acct = self._accounts[name]
            linked = bool(acct._session) or os.path.exists(acct.session_path)
            if name == UNASSIGNED and not linked:
                continue
            rows.append({"username": name, "linked": linked})
        return rows

    def _ensure(self, username: str) -> TidalAccount:
        acct = self._accounts.get(username)
        if acct is None:
            acct = TidalAccount(
                username=username,
                quality=self._quality,
                manifest_base_url=self._manifest_base,
                local_base=self._local_base,
                available=self._available,
                mint_token=self.mint_manifest_token,
            )
            self._accounts[username] = acct
        return acct

    def _default(self) -> TidalAccount:
        """The account an un-namespaced call resolves to.

        Deterministic, in order: the configured owner, the only named account,
        then the unassigned one — created empty so a call with nothing linked
        still answers "logged out" rather than raising, exactly as the
        single-account source did.

        The placeholder is excluded from that count, or merely having asked for
        a status while nothing was linked would bring it into existence and
        leave a real account outnumbered by it for ever. Two named accounts is
        genuinely ambiguous and resolves to the placeholder rather than to
        whichever user sorts first.
        """
        if self._owner_hint and self._owner_hint in self._accounts:
            return self._accounts[self._owner_hint]
        named = [a for n, a in self._accounts.items() if n != UNASSIGNED]
        if len(named) == 1:
            return named[0]
        return self._ensure(UNASSIGNED)

    # Manifest tokens
    def mint_manifest_token(self, owner: str, track_id: str) -> str:
        """A single-use-ish handle for one user's manifest of one track.

        Minted when a lossless URL is built and redeemed by whatever fetches it,
        which is a Cast device on the LAN or this host's own decoder — neither
        of which can carry a session cookie. It expires on its own: the manifest
        is fetched at load rather than on a schedule, so there is no playback
        lifecycle to hang an explicit release off.
        """
        token = secrets.token_urlsafe(MANIFEST_TOKEN_BYTES)
        self._manifest_tokens[token] = (owner, str(track_id), time.time())
        # After the insert, so MANIFEST_TOKEN_MAX is a ceiling on the live set
        # rather than one less than what it actually holds.
        self._prune_manifest_tokens()
        return token

    def redeem_manifest_token(self, token: str) -> Optional[tuple]:
        """``(account, track_id)`` for a live token, else None.

        Fails closed the same way resolution does: a token naming a user whose
        account has since gone answers nothing rather than falling back.
        """
        got = self._manifest_tokens.get(token or "")
        if not got:
            return None
        owner, track_id, issued = got
        if (time.time() - issued) > MANIFEST_TOKEN_TTL_S:
            self._manifest_tokens.pop(token, None)
            return None
        acct = self._for(owner)
        return (acct, track_id) if acct else None

    def _prune_manifest_tokens(self) -> None:
        now = time.time()
        dead = [t for t, (_, _, at) in self._manifest_tokens.items()
                if (now - at) > MANIFEST_TOKEN_TTL_S]
        for t in dead:
            self._manifest_tokens.pop(t, None)
        # A speaker that never fetches leaves its token behind, so the live set
        # is bounded as well as aged.
        if len(self._manifest_tokens) > MANIFEST_TOKEN_MAX:
            for t, _ in sorted(self._manifest_tokens.items(),
                               key=lambda kv: kv[1][2])[:len(self._manifest_tokens)
                                                        - MANIFEST_TOKEN_MAX]:
                self._manifest_tokens.pop(t, None)

    def _stored_usernames(self) -> List[str]:
        try:
            names = os.listdir(SESSION_DIR)
        except OSError:
            return []
        out = []
        for fn in names:
            if not fn.endswith(".json"):
                continue
            stem = fn[:-5]
            if USERNAME_RE.match(stem) or stem == UNASSIGNED:
                out.append(stem)
            else:
                logger.warning(f"Tidal: ignoring session file {fn!r} — not a username")
        return out

    # Legacy adoption
    def _migrate_legacy(self) -> None:
        """Adopt the pre-multi-user session file for one owner, once.

        Skipped the moment any per-user file exists: the move has already
        happened, and a legacy file sitting next to per-user ones came from a
        restored backup, not from a session waiting to be claimed. Adoption is
        a rename, so it cannot leave two files holding the same login.
        """
        if not os.path.exists(LEGACY_SESSION_PATH):
            return
        if self._stored_usernames():
            logger.info("Tidal: per-user sessions exist — leaving the legacy "
                        "session file alone")
            return
        owner = self._legacy_owner()
        dest = os.path.join(SESSION_DIR, f"{owner}.json")
        try:
            os.makedirs(SESSION_DIR, exist_ok=True)
            os.replace(LEGACY_SESSION_PATH, dest)
            try:
                os.chmod(dest, 0o600)
            except OSError:
                pass
        except OSError as e:
            logger.warning(f"Tidal: could not adopt the legacy session file: {e}")
            return
        if owner == UNASSIGNED:
            logger.warning(
                "Tidal: adopted the existing login, but could not tell which "
                "ZMM user owns it — set media.tidal.owner to name them")
        else:
            logger.info(f"Tidal: adopted the existing login for {owner}")

    def _legacy_owner(self) -> str:
        """Who the pre-multi-user login belongs to.

        Config wins. Otherwise a single admin is an unambiguous answer and
        anything else is a guess, so the session is parked under UNASSIGNED
        rather than handed to whichever account happened to sort first.
        """
        if self._owner_hint:
            if USERNAME_RE.match(self._owner_hint):
                return self._owner_hint
            logger.warning(f"Tidal: media.tidal.owner {self._owner_hint!r} is "
                           "not a valid username — ignoring it")
        admins = self._admin_usernames()
        return admins[0] if len(admins) == 1 else UNASSIGNED

    def _admin_usernames(self) -> List[str]:
        """Enabled admins, by the same test the auth middleware's first-run
        gate uses. Resolved here rather than injected: this runs at startup,
        long after the auth manager is registered, and the media service is
        constructed before it exists."""
        try:
            from modules.auth import get_auth_manager
            mgr = get_auth_manager()
            if mgr is None:
                return []
            return sorted(
                u.username for u in mgr.users.values()
                if (not u.disabled)
                and ("admins" in u.groups or "admin" in u.extra_scopes)
            )
        except Exception as e:
            logger.debug(f"Tidal: admin lookup unavailable: {e}")
            return []

    def default_account(self) -> TidalAccount:
        """The account a caller that names no user gets.

        The last of the pre-multi-user surface: an automation rule written
        before a step carried an owner has none, and this is what it plays on.
        """
        return self._default()

    def default_username(self) -> str:
        """Name of the account an un-namespaced call resolves to.

        Lets the service stamp an owner on items while the routes still do not
        name a user. Goes away with the shim.
        """
        return self._default().username

    def _for(self, owner: str) -> Optional[TidalAccount]:
        """The account an off-request resolution must use, or None.

        Fails closed: an owner naming a user with no linked account resolves to
        nothing, so the track does not play. It must never fall back to
        whichever account happens to be linked — that is how one user's
        playback would end up on another's subscription.

        An empty owner is the one exception, and it means "queued before Tidal
        was per-user". Those go to the default account, which is what played
        them at the time. That is still fail-closed once there is more than one
        user: with two named accounts the default is the empty placeholder,
        which resolves nothing.
        """
        if not owner:
            return self._default()
        acct = self._accounts.get(owner)
        if acct is None:
            logger.warning(f"Tidal: no linked account for {owner!r} — "
                           "not resolving on anyone else's")
        return acct

    # Delegation
    #
    # The four calls that reach a source without naming a user. Each is taken as
    # a bound method and kept — search because SourceProvider makes it abstract,
    # and the other three because the media service hands them to the controller
    # and the Cast provider when it is constructed, before start() has loaded any
    # account — so each must resolve the account when it is called, never when it
    # is taken. Everything else names a user and goes through account().
    async def search(self, query: str, limit: int = 25) -> List[MediaItem]:
        return await self._default().search(query, limit)

    async def resolve_url(self, source_id: str, provider: Optional[str] = None,
                          owner: str = ""):
        acct = self._for(owner)
        return await acct.resolve_url(source_id, provider) if acct else None

    async def track_radio(self, track_id: str, owner: str = "") -> List[MediaItem]:
        acct = self._for(owner)
        return await acct.track_radio(track_id) if acct else []

    async def track_lyrics(self, track_id: str, owner: str = "") -> Optional[dict]:
        acct = self._for(owner)
        return await acct.track_lyrics(track_id) if acct else None
