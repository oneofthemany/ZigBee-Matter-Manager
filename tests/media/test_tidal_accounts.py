"""
Per-user Tidal accounts: the registry, the credential files, and the adoption
of the single session that predates multi-user auth.

Step 1 of docs/plans/tidal-per-user-auth.md, whose contract is that nothing
downstream changes yet — so the last section asserts the compatibility shim
still answers exactly as the single-account source did.
"""

from __future__ import annotations

import asyncio
import os
import stat

from harness import Checker, TempSessions, auth_clear, auth_with

from modules.media.sources import tidal as T
from modules.media.sources.tidal import TidalAccount, TidalSource, UNASSIGNED


def _source(tmp, **kw) -> TidalSource:
    """A source that believes tidalapi is installed, without importing it.

    start() is never called in these tests — it would import tidalapi and log
    in — so _available is set directly, which is the only thing start() would
    contribute that the account paths care about.
    """
    src = TidalSource(enabled=True, **kw)
    src._available = True
    return src


def _files(c: Checker) -> None:
    c.section("credential files")
    with TempSessions(T) as tmp:
        acct = TidalAccount("alice")
        c.check("a session file is named for its user",
                acct.session_path == os.path.join(str(tmp.dir), "alice.json"),
                acct.session_path)

        # _persist_session needs only these three attributes off the session.
        class FakeSession:
            token_type, access_token, refresh_token = "Bearer", "a", "r"
            expiry_time = 1234.0

        acct._session = FakeSession()
        acct._persist_session()
        mode = stat.S_IMODE(os.stat(acct.session_path).st_mode)
        c.check("a persisted session is owner-only", mode == 0o600, oct(mode))
        c.check("it round-trips", (acct._load_session_file() or {}).get("access_token") == "a")

        acct2 = TidalAccount("bob")
        acct2._session = FakeSession()
        acct2._persist_session()
        c.check("two users keep two files",
                tmp.names() == ["alice.json", "bob.json"], tmp.names())


def _naming(c: Checker) -> None:
    c.section("account naming")
    with TempSessions(T) as tmp:
        src = _source(tmp)
        c.check("an unknown user has no account yet", src.account("alice") is None)
        c.check("create makes one", src.account("alice", create=True) is not None)
        c.check("and it is the same one next time",
                src.account("alice") is src.account("alice", create=True))

        for bad in ("../../etc/passwd", "a/b", "", "x", "a" * 33, "bob;rm", UNASSIGNED):
            c.check(f"refuses {bad!r} as a username",
                    src.account(bad, create=True) is None)
        c.check("and none of them reached the filesystem",
                tmp.names() == [], tmp.names())

        tmp.write_account("carol")
        (tmp.dir / "notes.txt").write_text("x")
        (tmp.dir / "../escape.json").resolve().write_text("x")
        c.check("only username-shaped .json files are loaded",
                src._stored_usernames() == ["carol"], src._stored_usernames())


def _migration(c: Checker) -> None:
    c.section("legacy adoption")

    with TempSessions(T) as tmp:
        tmp.write_legacy()
        src = _source(tmp, owner="dave")
        src._migrate_legacy()
        c.check("config names the owner", tmp.names() == ["dave.json"], tmp.names())
        c.check("the legacy file is moved, not copied", not tmp.legacy.exists())
        mode = stat.S_IMODE(os.stat(tmp.dir / "dave.json").st_mode)
        c.check("the adopted file is owner-only", mode == 0o600, oct(mode))

    with TempSessions(T) as tmp:
        tmp.write_legacy()
        auth_with(("erin", ["admins"]), ("frank", []))
        src = _source(tmp)
        src._migrate_legacy()
        c.check("a sole admin is an unambiguous owner",
                tmp.names() == ["erin.json"], tmp.names())
        auth_clear()

    with TempSessions(T) as tmp:
        tmp.write_legacy()
        auth_with(("erin", ["admins"]), ("gail", ["admins"]))
        src = _source(tmp)
        src._migrate_legacy()
        c.check("two admins is a guess, so it is parked",
                tmp.names() == [f"{UNASSIGNED}.json"], tmp.names())
        auth_clear()

    with TempSessions(T) as tmp:
        tmp.write_legacy()
        auth_with(("erin", ["admins"]), ("gail", ["admins"]))
        src = _source(tmp, owner="gail")
        src._migrate_legacy()
        c.check("config still wins over two admins",
                tmp.names() == ["gail.json"], tmp.names())
        auth_clear()

    with TempSessions(T) as tmp:
        tmp.write_legacy("old")
        tmp.write_account("harry", "current")
        src = _source(tmp)
        src._migrate_legacy()
        c.check("a legacy file beside per-user ones is left alone",
                tmp.names() == ["harry.json"] and tmp.legacy.exists(), tmp.names())
        c.check("and it does not overwrite a live session",
                "current" in (tmp.dir / "harry.json").read_text())

    with TempSessions(T) as tmp:
        tmp.write_legacy()
        src = _source(tmp, owner="dave")
        src._migrate_legacy()
        src._migrate_legacy()
        c.check("adoption is idempotent", tmp.names() == ["dave.json"], tmp.names())

    with TempSessions(T) as tmp:
        src = _source(tmp, owner="dave")
        src._migrate_legacy()
        c.check("nothing to adopt is not an error", tmp.names() == [], tmp.names())

    with TempSessions(T) as tmp:
        tmp.write_legacy()
        src = _source(tmp, owner="../etc/x")
        src._migrate_legacy()
        c.check("an invalid configured owner is ignored, not obeyed",
                tmp.names() == [f"{UNASSIGNED}.json"], tmp.names())


def _default(c: Checker) -> None:
    c.section("the default account")
    with TempSessions(T) as tmp:
        src = _source(tmp)
        c.check("nothing linked resolves to the unassigned account",
                src._default().username == UNASSIGNED)

        src = _source(tmp)
        src._ensure("alice")
        c.check("a single linked account is the default",
                src._default().username == "alice")

        src._ensure("bob")
        c.check("two accounts with no owner set fall back to unassigned",
                src._default().username == UNASSIGNED)

        src = _source(tmp, owner="bob")
        src._ensure("alice")
        src._ensure("bob")
        c.check("the configured owner wins", src._default().username == "bob")

        src = _source(tmp, owner="nobody")
        src._ensure("alice")
        c.check("an owner with no account does not shadow the only one",
                src._default().username == "alice")


def _default_surface(c: Checker) -> None:
    c.section("what a caller that names no user gets")
    with TempSessions(T) as tmp:
        src = _source(tmp)
        status = asyncio.run(src.default_account().status())
        c.check("status with nothing linked reads logged out",
                status == {"state": "logged_out"}, status)
        c.check("the library kinds are on the source itself",
                src.LIBRARY_KINDS == TidalAccount.LIBRARY_KINDS)
        c.check("search answers empty rather than raising",
                asyncio.run(src.search("anything")) == [])
        c.check("favourite_ids answers unready rather than raising",
                asyncio.run(src.default_account().favourite_ids())
                .get("ready") is False)

        src._ensure("alice")
        c.check("the default account is the only linked one",
                src.default_account().session_path.endswith("alice.json"),
                src.default_account().session_path)
        c.check("and default_username names it",
                src.default_username() == "alice", src.default_username())

        # The registry used to forward anything it did not define to the default
        # account. It no longer does: a call that does not name a user has to
        # say so, or it is a bug rather than a silent choice of somebody's
        # account. Guards against the shim coming back.
        for name in ("status", "favourite_ids", "library", "playlist_create"):
            raised = False
            try:
                getattr(src, name)
            except AttributeError:
                raised = True
            c.check(f"{name}() is not silently forwarded", raised)

    with TempSessions(T) as tmp:
        # The service captures these three as bound methods when it is built,
        # which is before start() has adopted or loaded anything — so they have
        # to resolve the account when they are called, not when they are taken.
        src = _source(tmp)
        resolver, extender, lyrics = (src.resolve_url, src.track_radio,
                                      src.track_lyrics)
        tmp.write_legacy()
        src._owner_hint = "erin"
        src._migrate_legacy()
        src._ensure("erin")
        seen = []

        def spy(name):
            async def fn(*a, **k):
                seen.append(name)
            return fn

        for name in ("resolve_url", "track_radio", "track_lyrics"):
            setattr(src.account("erin"), name, spy(name))
        asyncio.run(resolver("1", "cast"))
        asyncio.run(extender("1"))
        asyncio.run(lyrics("1"))
        c.check("a captured callable follows the account that appears later",
                seen == ["resolve_url", "track_radio", "track_lyrics"], seen)

        # Accounts are built from the registry's config, not their own defaults.
        src2 = _source(tmp, quality="lossless", manifest_base_url="https://h:8000/")
        acct = src2.account("alice", create=True)
        c.check("an account inherits the configured quality",
                acct._quality == "lossless", acct._quality)
        c.check("and the manifest base, trailing slash stripped",
                acct._manifest_base == "https://h:8000", acct._manifest_base)
        c.check("and knows tidalapi is importable", acct._available is True)


def _admin_view(c: Checker) -> None:
    c.section("admin view")
    with TempSessions(T) as tmp:
        src = _source(tmp)
        src._ensure("bob")
        src._ensure("alice")._session = object()
        rows = src.accounts()
        c.check("accounts are listed by name",
                [r["username"] for r in rows] == ["alice", "bob"], rows)
        c.check("with linked state", [r["linked"] for r in rows] == [True, False], rows)
        c.check("and never a token",
                all(set(r) == {"username", "linked"} for r in rows), rows)

        src._default()      # an empty placeholder, made by asking for a status
        c.check("the empty placeholder is not listed as a user",
                [r["username"] for r in src.accounts()] == ["alice", "bob"],
                src.accounts())

        tmp.write_account(UNASSIGNED)
        c.check("an adopted but unclaimed login is listed, so it can be assigned",
                {"username": UNASSIGNED, "linked": True} in src.accounts(),
                src.accounts())


def run() -> Checker:
    c = Checker("tidal-accounts")
    for part in (_files, _naming, _migration, _default, _default_surface,
                 _admin_view):
        part(c)
    auth_clear()
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
