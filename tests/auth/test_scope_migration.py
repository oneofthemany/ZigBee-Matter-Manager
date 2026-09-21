"""
An existing hub keeps its users' access when scopes are split.

The coverage test only checks DEFAULT_GROUPS, which a store written before the
split never sees. This starts from such a store, as a real upgrade does.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from harness import Checker

from modules.auth import AuthManager, scope_matches
from modules.auth_scopes import scope_for_path

# A store as a hub had it before AUTH_SCHEMA 2: no schema key, old groups.
PRE_SPLIT = {
    "groups": [
        {"name": "admins", "scopes": ["admin"]},
        {"name": "users", "scopes": ["device:read", "device:write",
                                     "automation:read", "system:read"]},
        {"name": "viewers", "scopes": ["device:read", "system:read"]},
        {"name": "family", "scopes": ["device:*"]},
        {"name": "phones", "scopes": ["presence:write:*"]},
    ],
    "users": [
        {"username": "boss", "groups": ["admins"]},
        {"username": "resident", "groups": ["users"]},
        {"username": "guest", "groups": ["viewers"]},
        {"username": "kid", "groups": ["family"]},
        {"username": "direct", "groups": [], "extra_scopes": ["device:write"]},
    ],
    "tokens": [
        {"token_hash": "a" * 64, "label": "home-assistant", "user": "resident",
         "scopes": ["device:read", "device:write"]},
        {"token_hash": "b" * 64, "label": "phone", "user": "resident",
         "scopes": ["presence:write:resident"]},
    ],
    "mfa": [{"username": "resident", "enabled": True, "secret": "JBSWY3DPEHPK3PXP",
             "recovery_code_hashes": [], "used_recovery_hashes": []}],
}


def _load(store: Path):
    from modules.auth_secure import SecureAuthManager
    a = AuthManager(config_path=store)
    a.load()
    SecureAuthManager(a)            # installs the MFA-aware save, persists upgrade
    return a


def _can(a: AuthManager, user: str, method: str, path: str) -> bool:
    return scope_matches(scope_for_path(path, method), a.resolve_user_scopes(user))


def run() -> Checker:
    c = Checker("scope_migration")
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "auth.yaml"
        store.write_text(yaml.safe_dump(PRE_SPLIT))
        a = _load(store)

        c.section("ordinary users keep what they had before the upgrade")
        for path in ("/api/heating/zones", "/api/media/play",
                     "/api/octopus/rates", "/api/security/locks/front/unlock"):
            c.check(f"resident may POST {path}", _can(a, "resident", "POST", path))
            c.check(f"guest may GET {path}", _can(a, "guest", "GET", path))
            c.check(f"guest still may not POST {path}",
                    not _can(a, "guest", "POST", path))
        c.check("a custom group with device:* is upgraded too",
                _can(a, "kid", "POST", "/api/heating/zones"))
        c.check("a per-user device:write is upgraded",
                _can(a, "direct", "POST", "/api/media/play"))

        c.section("bearer tokens keep working")
        ha = next(t for t in a.tokens.values() if t.label == "home-assistant")
        c.check("the Home Assistant token can still set heating",
                scope_matches("heating:write", set(ha.scopes)), ha.scopes)
        phone = next(t for t in a.tokens.values() if t.label == "phone")
        c.check("a presence-only phone token gains nothing",
                phone.scopes == ["presence:write:resident"], phone.scopes)

        c.section("nothing is widened that should not be")
        c.check("admins untouched", a.groups["admins"].scopes == ["admin"])
        c.check("presence-only group untouched",
                a.groups["phones"].scopes == ["presence:write:*"])
        c.check("no one gains admin",
                not _can(a, "resident", "POST", "/api/editor/save"))

        c.section("the upgrade is saved, with MFA intact")
        disk = yaml.safe_load(store.read_text())
        c.check("schema 2 is recorded", disk.get("schema") == 2, disk.get("schema"))
        c.check("the mfa section survived the save", bool(disk.get("mfa")))

        c.section("it runs once")
        a.groups["users"].scopes.remove("heating:write")
        a._save_locked()
        a = _load(store)
        c.check("a scope removed after the upgrade stays removed",
                "heating:write" not in a.groups["users"].scopes,
                a.groups["users"].scopes)
    return c


if __name__ == "__main__":
    run()
