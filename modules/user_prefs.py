"""
Per-account UI preferences (a table's sort, and the like), so a choice follows
the user to every browser they sign in from.

One JSON file, {username: {key: value}}. Blocking file I/O: callers on the
event loop go through asyncio.to_thread.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Dict

logger = logging.getLogger("user_prefs")

PREFS_PATH = os.path.join(os.environ.get("ZMM_DATA_DIR", "./data"), "user_prefs.json")

KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
MAX_VALUE_BYTES = 4096
MAX_KEYS_PER_USER = 50

_lock = threading.Lock()


def _load(path: str) -> Dict[str, Dict[str, Any]]:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning(f"Unreadable {path}, starting empty: {e}")
        return {}


def get_all(username: str, path: str = PREFS_PATH) -> Dict[str, Any]:
    with _lock:
        return dict(_load(path).get(username) or {})


def set_pref(username: str, key: str, value: Any, path: str = PREFS_PATH) -> Dict[str, Any]:
    """Store one key (None deletes it); returns the user's prefs.
    Raises ValueError for a bad key, an oversized value or too many keys."""
    if not KEY_RE.match(key):
        raise ValueError("bad preference key")
    if len(json.dumps(value)) > MAX_VALUE_BYTES:
        raise ValueError("preference value too large")
    with _lock:
        data = _load(path)
        mine = dict(data.get(username) or {})
        if value is None:
            mine.pop(key, None)
        else:
            if key not in mine and len(mine) >= MAX_KEYS_PER_USER:
                raise ValueError("too many preferences")
            mine[key] = value
        data[username] = mine
        # Replace, never rewrite in place: a crash mid-write must not empty everyone's prefs.
        tmp = f"{path}.tmp"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return mine
