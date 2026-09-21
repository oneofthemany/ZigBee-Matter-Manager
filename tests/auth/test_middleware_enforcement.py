"""
The middleware actually refuses the request — not just the table saying it should.

test_scope_coverage proves the table resolves correctly; this drives real
requests through the real AuthMiddleware with a real AuthManager and real
bearer tokens, because a correct table wired in wrongly is still an open door.

Needs FastAPI, so run_all skips it on a bare host (AGENTS.md §The dev box).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from harness import Checker

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.auth import AuthManager, DEFAULT_GROUPS, Group
from modules.auth_middleware import AuthMiddleware


def _build(tmp: Path):
    """An app carrying one route per scope class, guarded only by the middleware."""
    auth = AuthManager(config_path=tmp / "auth.yaml")
    auth.load()
    for name, scopes in DEFAULT_GROUPS.items():
        auth.groups[name] = Group(name=name, scopes=list(scopes))

    async def setup():
        await auth.create_user("boss", "correct-horse", groups=["admins"])
        await auth.create_user("resident", "correct-horse", groups=["users"])
        await auth.create_user("guest", "correct-horse", groups=["viewers"])
        # A companion phone: the exact principal the roadmap says must not be
        # able to reach the editor.
        await auth.create_user("phone", None, extra_scopes=["presence:read:phone",
                                                            "presence:write:phone"])
        out = {}
        for who in ("boss", "resident", "guest", "phone"):
            out[who], _ = await auth.issue_token(who, f"{who}-token")
        return out

    tokens = asyncio.new_event_loop().run_until_complete(setup())

    app = FastAPI()

    # Deliberately undecorated: the middleware is the only thing standing
    # between these and the caller, which is the property under test.
    for path in ("/api/editor/save", "/api/system/restart", "/api/backup/restore",
                 "/api/config/save", "/api/heating/zones", "/api/media/play",
                 "/api/security/locks/front/unlock", "/api/brand_new_thing/go"):
        app.post(path)(lambda: {"ran": True})
    for path in ("/api/devices", "/api/heating/zones", "/api/media/players",
                 "/api/system/status-ish", "/api/auth/tokens"):
        app.get(path)(lambda: {"ran": True})

    app.add_middleware(AuthMiddleware, auth_manager=auth, enforce=True)
    return TestClient(app, raise_server_exceptions=False), tokens


def run() -> Checker:
    c = Checker("middleware_enforcement")
    with tempfile.TemporaryDirectory() as td:
        client, tok = _build(Path(td))

        def post(who, path):
            return client.post(path, headers={"Authorization": f"Bearer {tok[who]}"})

        def get(who, path):
            return client.get(path, headers={"Authorization": f"Bearer {tok[who]}"})

        c.section("the RCE path is closed")
        for who in ("resident", "guest", "phone"):
            c.check(f"'{who}' is refused POST /api/editor/save",
                    post(who, "/api/editor/save").status_code == 403,
                    post(who, "/api/editor/save").status_code)
        c.check("admin still reaches the editor",
                post("boss", "/api/editor/save").status_code == 200,
                post("boss", "/api/editor/save").status_code)

        c.section("a leaked phone token is inert")
        for path in ("/api/system/restart", "/api/backup/restore",
                     "/api/config/save", "/api/security/locks/front/unlock"):
            c.check(f"phone token refused POST {path}",
                    post("phone", path).status_code == 403,
                    post("phone", path).status_code)

        c.section("an unmapped route is closed by default")
        c.check("resident refused an unmapped POST",
                post("resident", "/api/brand_new_thing/go").status_code == 403)
        c.check("only admin reaches it",
                post("boss", "/api/brand_new_thing/go").status_code == 200)

        c.section("ordinary use still works")
        c.check("resident can set heating",
                post("resident", "/api/heating/zones").status_code == 200,
                post("resident", "/api/heating/zones").status_code)
        c.check("resident can control media",
                post("resident", "/api/media/play").status_code == 200)
        c.check("resident can unlock",
                post("resident", "/api/security/locks/front/unlock").status_code == 200)
        c.check("guest can read heating",
                get("guest", "/api/heating/zones").status_code == 200)
        c.check("guest can read devices",
                get("guest", "/api/devices").status_code == 200)

        c.section("viewers are read-only")
        c.check("guest cannot set heating",
                post("guest", "/api/heating/zones").status_code == 403,
                post("guest", "/api/heating/zones").status_code)
        c.check("guest cannot play media",
                post("guest", "/api/media/play").status_code == 403)
        c.check("guest cannot unlock the door",
                post("guest", "/api/security/locks/front/unlock").status_code == 403)

        c.section("self-service survives the gate")
        for who in ("resident", "guest", "phone"):
            c.check(f"'{who}' can still list their own tokens",
                    get(who, "/api/auth/tokens").status_code == 200,
                    get(who, "/api/auth/tokens").status_code)

        c.section("no credentials is still 401, not 403")
        r = client.post("/api/editor/save")
        c.check("anonymous gets 401", r.status_code == 401, r.status_code)

        c.section("the refusal says what was needed")
        r = post("guest", "/api/heating/zones")
        c.check("403 names the missing scope",
                r.json().get("required_scope") == "heating:write", r.json())

    return c


if __name__ == "__main__":
    run()
