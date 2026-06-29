"""
Tidal source — unofficial `tidalapi`.

Phase 2 serves **AAC** (HIGH quality) via a *directly playable* URL so Cast/WiiM
can fetch it without our stream server. Lossless/HiRes (DASH/FLAC manifests,
parsed/served server-side) is Phase 3 — explicitly not here.

Hard-isolated: `tidalapi` is imported lazily and every failure is swallowed so a
Tidal breakage never affects Cast/WiiM/radio. The lib is blocking (`requests`),
so every call is wrapped in `asyncio.to_thread`.

Session persists to `data/media/tidal_session.json` (a token, not user-edited
config — so not in config.yaml). Login is a device/OAuth flow: we hand the UI a
`link.tidal.com` URL and a background task waits for the user to authorise.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
import time
from typing import List, Optional

from modules.media.models import MediaItem
from modules.media.sources.base import SourceProvider

logger = logging.getLogger("modules.media.tidal")

SESSION_PATH = "./data/media/tidal_session.json"


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


class TidalSource(SourceProvider):
    source = "tidal"

    def __init__(self, enabled: bool = False, quality: str = "high",
                 manifest_base_url: str = ""):
        self.enabled = enabled
        # "high" (320k AAC, single URL, every device) or "lossless" (FLAC via a
        # DASH manifest — Cast only; WiiM transparently falls back to AAC).
        self._quality = (quality or "high").lower()
        # Public base URL of *this* app, reachable by Cast devices on the LAN,
        # used to serve the DASH manifest for lossless (e.g. "https://192.168.1.1:8000").
        # Empty → lossless disabled (AAC everywhere), so nothing breaks if unset.
        self._manifest_base = (manifest_base_url or "").rstrip("/")
        self._session = None
        self._available = False           # tidalapi importable + session usable
        self._login_future = None
        self._login_link: Optional[str] = None
        self._pending = False
        # Serialises the brief session.audio_quality swap during stream resolution
        # (tidalapi has no per-call quality override; the swap is global).
        self._stream_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if not self.enabled:
            return
        try:
            import tidalapi  # noqa: F401
        except ImportError:
            logger.warning("Tidal disabled: tidalapi not installed")
            return
        self._available = True
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
                    logger.info("Tidal session restored")
                else:
                    logger.info("Tidal saved session invalid — login required")
        except Exception as e:
            logger.warning(f"Tidal session init failed: {e}")

    async def stop(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------
    def _load_session_file(self) -> Optional[dict]:
        try:
            if os.path.exists(SESSION_PATH):
                with open(SESSION_PATH, "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.debug(f"Tidal session read failed: {e}")
        return None

    def _persist_session(self):
        try:
            os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)
            s = self._session
            expiry = s.expiry_time
            with open(SESSION_PATH, "w") as f:
                json.dump({
                    "token_type": s.token_type,
                    "access_token": s.access_token,
                    "refresh_token": s.refresh_token,
                    # expiry_time may be a datetime — store epoch for portability.
                    "expiry_time": expiry.timestamp() if hasattr(expiry, "timestamp") else expiry,
                }, f)
            logger.info("Tidal session persisted")
        except Exception as e:
            logger.warning(f"Tidal session persist failed: {e}")

    # ------------------------------------------------------------------
    # Login (web flow)
    # ------------------------------------------------------------------
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
                logger.info("Tidal login complete")
        except Exception as e:
            logger.warning(f"Tidal login did not complete: {e}")
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
            if os.path.exists(SESSION_PATH):
                os.remove(SESSION_PATH)
        except Exception:
            pass
        # Rebuild a fresh, logged-out session.
        await asyncio.to_thread(self._build_session_fresh)

    def _build_session_fresh(self):
        import tidalapi
        self._session = tidalapi.Session()
        self._session.audio_quality = _quality_enum(tidalapi, self._quality)

    # ------------------------------------------------------------------
    # URL resolution (registered with the controller for media_type="tidal")
    # ------------------------------------------------------------------
    async def resolve_url(self, source_id: str, provider: Optional[str] = None):
        """Return a fresh, directly-playable stream for ``source_id``.

        Returns a ``{"url", "content_type"}`` dict (the controller applies both).
        For a Cast target with lossless enabled we hand back a URL to *our own*
        DASH-manifest route (served fresh on fetch, segment URLs being short-lived);
        otherwise a single 320k-AAC URL that plays on Cast and WiiM alike."""
        if not self._session:
            return None
        if self._wants_lossless(provider):
            return {
                "url": f"{self._manifest_base}/api/media/tidal/manifest/{source_id}.mpd",
                "content_type": "application/dash+xml",
            }
        url = await asyncio.to_thread(self._aac_url, source_id)
        return {"url": url, "content_type": "audio/mp4"} if url else None

    def _wants_lossless(self, provider: Optional[str]) -> bool:
        # FLAC/DASH only works on Cast, and only if we have a base URL Cast can
        # reach to fetch the manifest. WiiM (LinkPlay) can't play DASH → AAC.
        return (self._quality == "lossless"
                and provider == "cast"
                and bool(self._manifest_base))

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
        served to Cast by the manifest route. Fetched fresh so URLs aren't stale."""
        if not self._session:
            return None
        return await asyncio.to_thread(self._dash_manifest, track_id)

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

    # ------------------------------------------------------------------
    # Search / browse
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # User library (favourites + own playlists)
    # ------------------------------------------------------------------
    async def library(self, kind: str) -> List[dict]:
        """kind: 'playlists' | 'albums' | 'artists' → summary dicts with artwork."""
        if not self._session:
            return []
        return await asyncio.to_thread(self._library, kind)

    def _library(self, kind: str) -> List[dict]:
        try:
            if kind == "mixes":
                return self._mixes()
            user = self._session.user
            fav = getattr(user, "favorites", None)
            if kind == "albums":
                rows = fav.albums() if fav else []
                return [self._album_summary(a) for a in (rows or [])]
            if kind == "artists":
                rows = fav.artists() if fav else []
                return [self._artist_summary(a) for a in (rows or [])]
            if kind == "playlists":
                # User's own playlists + followed/favourited playlists.
                rows = []
                for getter in ("playlist_and_favorite_playlists", "playlists"):
                    fn = getattr(user, getter, None)
                    if callable(fn):
                        try:
                            rows = fn()
                            break
                        except Exception:
                            continue
                if not rows and fav:
                    rows = fav.playlists()
                # Normalise tuples (some versions return (playlist, type) pairs).
                out, seen = [], set()
                for r in (rows or []):
                    pl = r[0] if isinstance(r, tuple) else r
                    pid = str(getattr(pl, "id", ""))
                    if pid and pid not in seen:
                        seen.add(pid)
                        out.append(self._playlist_summary(pl))
                return out
        except Exception as e:
            logger.warning(f"Tidal library({kind}) failed: {e}")
        return []

    # ------------------------------------------------------------------
    # Artist play / radio (infinite)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Lyrics
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Favourites (write)
    # ------------------------------------------------------------------
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
            return True
        except Exception as e:
            logger.warning(f"Tidal favourite {kind} {'add' if on else 'remove'} failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Mixes (personalised — "My Daily Discovery", "Mix 1-8", "New Arrivals")
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------
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
        )

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
        }

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
