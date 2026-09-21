"""
A page on another site cannot make the browser write with the user's cookie.

SameSite=Lax already blocks most of this; the header check is the second
layer. It must refuse forged writes without breaking the UI, bearer clients,
or anything behind a tunnel that rewrites Host.

Needs FastAPI, so run_all skips it on a bare host.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from harness import Checker

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.auth import AuthManager, DEFAULT_GROUPS, Group
from modules.auth_middleware import (
    AuthMiddleware,
    _derive_session_secret,
    issue_session_cookie,
)


def _build(tmp: Path):
    auth = AuthManager(config_path=tmp / "auth.yaml")
    auth.load()
    for name, scopes in DEFAULT_GROUPS.items():
        auth.groups[name] = Group(name=name, scopes=list(scopes))

    async def setup():
        await auth.create_user("resident", "correct-horse", groups=["users"])
        return (await auth.issue_token("resident", "phone"))[0]

    token = asyncio.new_event_loop().run_until_complete(setup())
    cookie = issue_session_cookie(
        "resident", _derive_session_secret(str(auth.config_path)))

    app = FastAPI()
    app.post("/api/heating/zones")(lambda: {"ran": True})
    app.get("/api/heating/zones")(lambda: {"ran": True})
    app.add_middleware(AuthMiddleware, auth_manager=auth, enforce=True)
    return TestClient(app, raise_server_exceptions=False), cookie, token


def run() -> Checker:
    c = Checker("csrf")
    with tempfile.TemporaryDirectory() as td:
        client, cookie, token = _build(Path(td))
        client.cookies.set("zmm_session", cookie)

        def post(**headers):
            return client.post("/api/heating/zones", headers=headers).status_code

        c.section("the UI's own writes still work")
        c.check("same-origin fetch", post(**{"sec-fetch-site": "same-origin"}) == 200)
        c.check("user-initiated navigation", post(**{"sec-fetch-site": "none"}) == 200)
        c.check("Origin matching Host, no Sec-Fetch-Site",
                post(origin="https://testserver") == 200)

        c.section("forged writes are refused")
        c.check("cross-site", post(**{"sec-fetch-site": "cross-site"}) == 403)
        c.check("same-site sibling subdomain",
                post(**{"sec-fetch-site": "same-site"}) == 403)
        c.check("foreign Origin", post(origin="https://evil.example") == 403)
        c.check("opaque null Origin (sandboxed frame)", post(origin="null") == 403)
        r = client.post("/api/heating/zones", headers={"sec-fetch-site": "cross-site"})
        c.check("the refusal says why", r.json().get("csrf") is True, r.json())

        c.section("Sec-Fetch-Site wins over a rewritten Host")
        # Behind a tunnel Host may not match Origin; the browser's own verdict
        # is what counts.
        c.check("same-origin with mismatched Origin/Host is allowed",
                post(**{"sec-fetch-site": "same-origin",
                        "origin": "https://hub.example.com"}) == 200)

        c.section("what the check leaves alone")
        c.check("reads are never refused",
                client.get("/api/heating/zones",
                           headers={"sec-fetch-site": "cross-site"}).status_code == 200)
        c.check("a non-browser client with a cookie and no headers", post() == 200)

        client.cookies.clear()
        r = client.post("/api/heating/zones",
                        headers={"Authorization": f"Bearer {token}",
                                 "sec-fetch-site": "cross-site"})
        c.check("bearer tokens are exempt: the browser never attaches them",
                r.status_code == 200, r.status_code)
    return c


if __name__ == "__main__":
    run()
