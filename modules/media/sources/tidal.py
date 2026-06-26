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
import json
import logging
import os
import time
from typing import List, Optional

from modules.media.models import MediaItem
from modules.media.sources.base import SourceProvider

logger = logging.getLogger("modules.media.tidal")

SESSION_PATH = "./data/media/tidal_session.json"


def _pick_quality(tidalapi):
    """Return the HIGH/AAC quality enum, defensive across tidalapi versions."""
    Q = tidalapi.Quality
    for name in ("high", "low_320k", "high_lossless", "low"):
        if hasattr(Q, name):
            return getattr(Q, name)
    # Last resort: first enum member.
    return list(Q)[0]


class TidalSource(SourceProvider):
    source = "tidal"

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self._session = None
        self._available = False           # tidalapi importable + session usable
        self._login_future = None
        self._login_link: Optional[str] = None
        self._pending = False

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
            self._session.audio_quality = _pick_quality(tidalapi)
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
        self._session.audio_quality = _pick_quality(tidalapi)

    # ------------------------------------------------------------------
    # URL resolution (registered with the controller for media_type="tidal")
    # ------------------------------------------------------------------
    async def resolve_url(self, source_id: str) -> Optional[str]:
        if not self._session:
            return None
        return await asyncio.to_thread(self._track_url, source_id)

    def _track_url(self, track_id: str) -> Optional[str]:
        try:
            track = self._session.track(int(track_id))
            return track.get_url()
        except Exception as e:
            logger.warning(f"Tidal URL resolve failed for {track_id}: {e}")
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
        """Tracks + albums + playlists, for a richer browse UI."""
        if not self._session:
            return {"tracks": [], "albums": [], "playlists": []}
        return await asyncio.to_thread(self._search_grouped, query, limit)

    def _search_grouped(self, query: str, limit: int) -> dict:
        try:
            res = self._session.search(query, limit=limit)
        except Exception as e:
            logger.warning(f"Tidal search failed: {e}")
            return {"tracks": [], "albums": [], "playlists": []}
        if isinstance(res, dict):
            tracks = res.get("tracks", []) or []
            albums = res.get("albums", []) or []
            playlists = res.get("playlists", []) or []
        else:
            tracks = getattr(res, "tracks", []) or []
            albums = getattr(res, "albums", []) or []
            playlists = getattr(res, "playlists", []) or []
        return {
            "tracks": [self._track_to_item(t).to_dict() for t in tracks[:limit]],
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
        # URL is resolved fresh at play time via resolve_url(); we still try to
        # fill it here so an immediate single-track play works without a round-trip.
        url = ""
        try:
            url = t.get_url()
        except Exception:
            pass
        return MediaItem(
            url=url,
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
