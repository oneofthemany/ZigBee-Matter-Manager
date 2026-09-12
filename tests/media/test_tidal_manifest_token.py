"""
The lossless manifest token.

`/api/media/tidal/manifest/` is anonymous to LAN clients because a Cast device
fetches it itself and carries no session cookie. With one Tidal login that was
harmless; per user it is not, so the URL carries a minted token rather than a
username, and the route redeems it for an account. Step 4 of
docs/plans/tidal-per-user-auth.md.

Both handlers — the main app's and the loopback listener a zone fetches over —
are checked by AST for the same redemption, since neither can be driven without
FastAPI.
"""

from __future__ import annotations

import ast
import asyncio
import time

from harness import Checker, REPO, TempSessions

from modules.media.sources import tidal as T
from modules.media.sources.tidal import TidalSource


def _source(**kw) -> TidalSource:
    src = TidalSource(enabled=True, **kw)
    src._available = True
    return src


def _mint(c: Checker) -> None:
    c.section("minting")
    with TempSessions(T):
        src = _source()
        src._ensure("alice")
        t1 = src.mint_manifest_token("alice", "42")
        t2 = src.mint_manifest_token("alice", "42")
        c.check("a token is opaque and long enough to be unguessable",
                len(t1) >= 20, t1)
        c.check("the same track twice gives different tokens", t1 != t2)
        c.check("a token names neither the user nor the track",
                "alice" not in t1 and "42" not in t1, t1)

        acct, track = src.redeem_manifest_token(t1)
        c.check("redeeming gives the owning account", acct.username == "alice")
        c.check("and the track it was minted for", track == "42", track)


def _redeem(c: Checker) -> None:
    c.section("redeeming")
    with TempSessions(T):
        src = _source()
        src._ensure("alice")
        src._ensure("bob")
        token = src.mint_manifest_token("alice", "42")

        c.check("an unknown token gives nothing",
                src.redeem_manifest_token("not-a-token") is None)
        c.check("an empty token gives nothing",
                src.redeem_manifest_token("") is None)
        c.check("and so does None", src.redeem_manifest_token(None) is None)

        # Bob must not be able to reach Alice's manifest by guessing an id.
        c.check("alice's token does not reach bob's account",
                src.redeem_manifest_token(token)[0].username == "alice")

        stale = src.mint_manifest_token("alice", "43")
        src._manifest_tokens[stale] = ("alice", "43",
                                       time.time() - T.MANIFEST_TOKEN_TTL_S - 1)
        c.check("an expired token gives nothing",
                src.redeem_manifest_token(stale) is None)
        c.check("and is dropped rather than kept for ever",
                stale not in src._manifest_tokens)

        # An account removed after the token was minted — a logout mid-track.
        orphan = src.mint_manifest_token("carol", "44")
        c.check("a token naming an account that has gone fails closed",
                src.redeem_manifest_token(orphan) is None)


def _bounded(c: Checker) -> None:
    c.section("the token set stays bounded")
    with TempSessions(T):
        src = _source()
        src._ensure("alice")
        for i in range(T.MANIFEST_TOKEN_MAX + 50):
            src.mint_manifest_token("alice", str(i))
        c.check("a speaker that never fetches cannot grow the set for ever",
                len(src._manifest_tokens) <= T.MANIFEST_TOKEN_MAX,
                len(src._manifest_tokens))

        src2 = _source()
        src2._ensure("alice")
        old = src2.mint_manifest_token("alice", "1")
        src2._manifest_tokens[old] = ("alice", "1",
                                      time.time() - T.MANIFEST_TOKEN_TTL_S - 1)
        src2.mint_manifest_token("alice", "2")
        c.check("expired tokens are pruned as new ones are minted",
                old not in src2._manifest_tokens)


def _url(c: Checker) -> None:
    c.section("the URL that reaches the speaker")
    with TempSessions(T):
        src = _source(quality="lossless", manifest_base_url="https://hub:8000")
        acct = src._ensure("alice")

        # resolve_url needs a session to get as far as the lossless branch;
        # this stands in for one, since no test logs in to Tidal.
        acct._session = object()
        got = asyncio.run(acct.resolve_url("42", "cast"))
        c.check("lossless resolves to a manifest URL",
                got and got["content_type"] == "application/dash+xml", got)
        url = got["url"]
        c.check("the URL names neither the user nor the track",
                "alice" not in url and "/42." not in url, url)
        c.check("and its token redeems to that user and track",
                src.redeem_manifest_token(url.rsplit("/", 1)[1][:-4])
                == (acct, "42"), url)


def _routes(c: Checker) -> None:
    c.section("both handlers redeem rather than trust the path")
    for rel in ("routes/media_routes.py", "modules/media/device_http.py"):
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == "tidal_manifest"), None)
        if not c.check(f"{rel} has the manifest handler", fn is not None):
            continue
        args = [a.arg for a in fn.args.args]
        c.check(f"{rel} takes a token, not a track id",
                args == ["token"], args)
        c.check(f"{rel} redeems it",
                any(isinstance(n, ast.Attribute)
                    and n.attr == "redeem_manifest_token"
                    for n in ast.walk(fn)))
        # The track id must come from the token, never from the request.
        c.check(f"{rel} never reads a track id off the path",
                "track_id: str" not in ast.unparse(fn.args))


def run() -> Checker:
    c = Checker("tidal-manifest-token")
    for part in (_mint, _redeem, _bounded, _url, _routes):
        part(c)
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
