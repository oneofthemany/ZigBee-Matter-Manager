"""
Radio favourites — tiny JSON-backed store.

Lets the user pin Radio-Browser stations so they don't have to search the
directory every time. Each favourite is a full RadioStation snapshot (uuid,
name, resolved url, favicon, …) so a favourite plays instantly without a
network round-trip — and keeps working even if the directory is unreachable.

Persisted to ``data/radio_favourites.json`` (same convention as the rest of
``data/*.json``). Ordering is insertion order; dedup is by stable ``uuid``.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Any, Dict, List, Optional

from modules.media.models import https_url

logger = logging.getLogger("modules.media.favourites")

# Fields we keep from a RadioStation dict. Anything else the caller sends is
# dropped so the file stays small and predictable.
_FIELDS = ("uuid", "name", "url", "favicon", "homepage",
           "country", "tags", "codec", "bitrate")


class RadioFavourites:
    def __init__(self, path: str = "./data/radio_favourites.json"):
        self._path = path
        self._lock = threading.Lock()
        self._items: List[Dict[str, Any]] = self._load()

    # ── Public API ───────────────────────────────────────────────────────────
    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(s) for s in self._items]

    def get(self, uuid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return next((dict(s) for s in self._items if s.get("uuid") == uuid), None)

    def add(self, station: Dict[str, Any]) -> Dict[str, Any]:
        clean = {k: station.get(k) for k in _FIELDS}
        clean["favicon"] = https_url(clean.get("favicon") or "")
        uuid = (clean.get("uuid") or "").strip()
        if not uuid:
            return {"success": False, "error": "Station has no uuid"}
        if not (clean.get("url") or "").strip():
            return {"success": False, "error": "Station has no stream url"}
        clean["uuid"] = uuid
        with self._lock:
            existing = next((s for s in self._items if s.get("uuid") == uuid), None)
            if existing:
                existing.update(clean)          # refresh stored metadata
            else:
                self._items.append(clean)
            self._save()
        return {"success": True, "favourited": True}

    def remove(self, uuid: str) -> Dict[str, Any]:
        with self._lock:
            before = len(self._items)
            self._items = [s for s in self._items if s.get("uuid") != uuid]
            if len(self._items) != before:
                self._save()
        return {"success": True, "favourited": False}

    # ── Persistence ──────────────────────────────────────────────────────────
    def _load(self) -> List[Dict[str, Any]]:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                items = [{k: d.get(k) for k in _FIELDS}
                         for d in data if isinstance(d, dict) and d.get("uuid")]
                for s in items:  # favourites saved before the https upgrade
                    s["favicon"] = https_url(s.get("favicon") or "")
                return items
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Could not read {self._path}: {e}")
        return []

    def _save(self) -> None:
        # Atomic write: temp file in the same dir + rename, so a crash mid-write
        # never corrupts the favourites list.
        d = os.path.dirname(self._path) or "."
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._items, f, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.error(f"Could not write {self._path}: {e}")
