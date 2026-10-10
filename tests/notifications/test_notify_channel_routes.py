"""
The channel API over real FastAPI: each user sees only their own destinations,
the hub setup is admin-only, and its secrets never come back.

Needs the app's dependencies (see AGENTS.md "The dev box"); run_all skips it
without FastAPI.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from harness import Checker

import test_notify_channels as T


def run() -> Checker:
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from modules import notify_channels as N
    from modules.auth import User
    from modules.auth_middleware import Principal
    from routes.notify_channel_routes import register_notify_channel_routes

    c = Checker("notify_channel_routes")
    with tempfile.TemporaryDirectory() as tmp:
        saved_file = N.SECRETS_FILE
        N.SECRETS_FILE = str(Path(tmp) / "secrets.yaml")
        http = T.FakeHttp()
        m = T._mgr(tmp, http)
        N.set_channel_manager(m)
        try:
            app = FastAPI()

            @app.middleware("http")
            async def sign_in(request: Request, call_next):
                # X-User names who is signed in; "root" is the admin.
                user = request.headers.get("X-User")
                if user:
                    scopes = {"admin"} if user == "root" else {"device:read"}
                    request.state.principal = Principal(User(username=user), scopes, auth_method="cookie")
                return await call_next(request)

            register_notify_channel_routes(app)
            api = TestClient(app)
            alice, bob, root = {"X-User": "alice"}, {"X-User": "bob"}, {"X-User": "root"}

            c.section("per-user settings")
            c.check("channels need a signed-in user", api.get("/api/notify-channels").status_code == 401)
            r = api.put("/api/notify-channels", headers=alice,
                        json={"ntfy": {"enabled": True, "topic": "zmm-alice-topic-1"}})
            c.check("a user saves their own topic",
                    r.status_code == 200 and r.json()["settings"]["ntfy"]["topic"] == "zmm-alice-topic-1", r.text)
            c.check("another user doesn't see it",
                    api.get("/api/notify-channels", headers=bob).json()["settings"]["ntfy"]["topic"] == "")
            c.check("a bad value is a 400 with the reason",
                    api.put("/api/notify-channels", headers=alice,
                            json={"email": {"address": "nope"}}).status_code == 400)
            c.check("the user view never carries a hub secret",
                    "BOTSECRET" not in api.get("/api/notify-channels", headers=alice).text)

            r = api.post("/api/notify-channels/test", headers=alice).json()
            c.check("a test reaches the caller's channels and reports each",
                    r["channels"].get("ntfy", {}).get("ok") is True, r)
            c.check("the test went to the caller's topic only",
                    [x["json"]["topic"] for x in http.to("ntfy.example")] == ["zmm-alice-topic-1"])

            c.section("hub setup")
            c.check("a non-admin can't read the hub setup",
                    api.get("/api/notify-channels/hub", headers=alice).status_code == 403)
            c.check("a non-admin can't change it",
                    api.put("/api/notify-channels/hub", headers=alice,
                            json={"ntfy_server": "http://attacker.example"}).status_code == 403)
            r = api.put("/api/notify-channels/hub", headers=root,
                        json={"pushover_app_token": "NEWSECRET"})
            c.check("an admin saves a token and gets back only that it is set",
                    r.status_code == 200 and r.json()["pushover_app_token_set"]
                    and "NEWSECRET" not in r.text and "BOTSECRET" not in r.text, r.text)
            c.check("an admin read never returns secrets",
                    "SECRET" not in api.get("/api/notify-channels/hub", headers=root).text)
        finally:
            N.SECRETS_FILE = saved_file
            N.set_channel_manager(None)
    return c
