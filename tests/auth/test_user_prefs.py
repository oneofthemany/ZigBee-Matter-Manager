"""
Per-account UI preferences: each user reads back only their own, a value
survives a reload from disk, and the store refuses what it can't bound.
"""

from __future__ import annotations

import json
import os
import tempfile

from harness import Checker

from modules import user_prefs


def run() -> Checker:
    c = Checker("user_prefs")
    path = os.path.join(tempfile.mkdtemp(prefix="zmm_prefs_"), "user_prefs.json")

    c.check("a user with nothing saved reads back nothing",
            user_prefs.get_all("alice", path=path) == {})

    sort = {"col": "last_seen", "dir": "desc"}
    user_prefs.set_pref("alice", "devices.sort", sort, path=path)
    c.check("a saved value reads back from disk",
            user_prefs.get_all("alice", path=path) == {"devices.sort": sort})
    c.check("another user does not see it",
            user_prefs.get_all("bob", path=path) == {})

    user_prefs.set_pref("bob", "devices.sort", {"col": "name", "dir": "asc"}, path=path)
    c.check("a second user's save leaves the first user's alone",
            user_prefs.get_all("alice", path=path) == {"devices.sort": sort})

    user_prefs.set_pref("alice", "devices.sort", None, path=path)
    c.check("saving None removes the key",
            user_prefs.get_all("alice", path=path) == {})

    def refused(fn) -> bool:
        try:
            fn()
        except ValueError:
            return True
        return False

    c.check("a key that isn't a plain name is refused",
            refused(lambda: user_prefs.set_pref("alice", "../x", 1, path=path)))
    c.check("an oversized value is refused",
            refused(lambda: user_prefs.set_pref("alice", "big", "x" * 5000, path=path)))
    for i in range(user_prefs.MAX_KEYS_PER_USER):
        user_prefs.set_pref("carol", f"k{i}", i, path=path)
    c.check("a key past the per-user limit is refused",
            refused(lambda: user_prefs.set_pref("carol", "one.more", 1, path=path)))
    c.check("an existing key can still be changed at the limit",
            not refused(lambda: user_prefs.set_pref("carol", "k0", 99, path=path)))

    with open(path, "w") as f:
        f.write("{not json")
    c.check("an unreadable file reads as empty instead of failing",
            user_prefs.get_all("alice", path=path) == {})
    user_prefs.set_pref("alice", "devices.sort", sort, path=path)
    with open(path) as f:
        c.check("and the next save writes a valid file",
                json.load(f)["alice"] == {"devices.sort": sort})
    c.check("no temp file is left behind", not os.path.exists(path + ".tmp"))
    return c
