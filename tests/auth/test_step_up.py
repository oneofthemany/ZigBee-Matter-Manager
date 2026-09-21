"""
Code-execution routes demand a freshly re-verified second factor.

Scopes stop a non-admin reaching the editor; this stops a *stolen admin
session* reaching it, which scopes cannot. The window is short and bound to
the credential that verified, so one browser cannot authorise another.

Needs FastAPI, so run_all skips it on a bare host.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from harness import Checker

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.auth import AuthManager, DEFAULT_GROUPS, Group
from modules.auth_middleware import AuthMiddleware
from modules.auth_mfa import _b32_decode, _hotp
from modules.auth_scopes import needs_step_up
from modules.auth_secure import (
    STEP_UP_WINDOW_S,
    SecureAuthManager,
    set_secure_auth_manager,
)


def _totp(secret: str) -> str:
    return _hotp(_b32_decode(secret), int(time.time()) // 30)


def _build(tmp: Path):
    auth = AuthManager(config_path=tmp / "auth.yaml")
    auth.load()
    for name, scopes in DEFAULT_GROUPS.items():
        auth.groups[name] = Group(name=name, scopes=list(scopes))

    async def setup():
        await auth.create_user("boss", "correct-horse", groups=["admins"])
        await auth.create_user("plain", "correct-horse", groups=["admins"])
        return {w: (await auth.issue_token(w, f"{w}-tok"))[0]
                for w in ("boss", "plain")}

    tokens = asyncio.new_event_loop().run_until_complete(setup())
    sec = SecureAuthManager(auth)
    set_secure_auth_manager(sec)

    # 'boss' enrols; 'plain' deliberately does not.
    secret, _ = asyncio.new_event_loop().run_until_complete(
        sec.begin_enrolment("boss"))
    asyncio.new_event_loop().run_until_complete(
        sec.finish_enrolment("boss", _totp(secret)))

    app = FastAPI()
    for path in ("/api/editor/save", "/api/editor/test-deploy",
                 "/api/backup/restore", "/api/backup/create",
                 "/api/heating/zones"):
        app.post(path)(lambda: {"ran": True})
    app.get("/api/editor/read")(lambda: {"ran": True})
    app.add_middleware(AuthMiddleware, auth_manager=auth, enforce=True)
    return TestClient(app, raise_server_exceptions=False), tokens, sec, secret


def run() -> Checker:
    c = Checker("step_up")

    with tempfile.TemporaryDirectory() as td:
        client, tok, sec, secret = _build(Path(td))

        def post(who, path):
            return client.post(path, headers={"Authorization": f"Bearer {tok[who]}"})

        c.section("admin alone no longer reaches the editor")
        r = post("boss", "/api/editor/save")
        c.check("save is refused before any step-up", r.status_code == 403,
                r.status_code)
        c.check("the refusal is machine-readable",
                r.json().get("step_up_required") is True, r.json())
        c.check("and says MFA is enrolled", r.json().get("mfa_enrolled") is True)

        c.section("reads are not code execution")
        c.check("GET editor/read is allowed",
                client.get("/api/editor/read",
                           headers={"Authorization": f"Bearer {tok['boss']}"}
                           ).status_code == 200)
        c.check("unrelated writes are unaffected",
                post("boss", "/api/heating/zones").status_code == 200)
        c.check("backup create is not restore",
                post("boss", "/api/backup/create").status_code == 200)

        c.section("a verified second factor opens the window")
        from modules.auth_middleware import credential_id_for

        class _Req:
            cookies: dict = {}
        principal = type("P", (), {"token": type("T", (), {
            "token_hash": __import__("hashlib").sha256(
                tok["boss"].encode()).hexdigest()})()})()
        cred = credential_id_for(_Req(), principal)
        ok, reason = asyncio.new_event_loop().run_until_complete(
            sec.verify_step_up("boss", _totp(secret), "127.0.0.1", cred))
        c.check("step-up verifies", ok, reason)
        c.check("editor save now succeeds",
                post("boss", "/api/editor/save").status_code == 200)
        c.check("restore now succeeds",
                post("boss", "/api/backup/restore").status_code == 200)

        c.section("the window expires")
        sec._step_ups[("boss", cred)] = time.time() - STEP_UP_WINDOW_S - 1
        c.check("an aged step-up is not valid", not sec.step_up_valid("boss", cred))
        c.check("and the editor closes again",
                post("boss", "/api/editor/save").status_code == 403)

        c.section("a step-up does not travel between credentials")
        asyncio.new_event_loop().run_until_complete(
            sec.verify_step_up("boss", _totp(secret), "127.0.0.1", cred))
        c.check("this credential is valid", sec.step_up_valid("boss", cred))
        c.check("another is not", not sec.step_up_valid("boss", "c:elsewhere"))

        c.section("an admin without MFA is told to enrol, not let through")
        r = post("plain", "/api/editor/save")
        c.check("refused", r.status_code == 403, r.status_code)
        c.check("flagged as not enrolled",
                r.json().get("mfa_enrolled") is False, r.json())
        c.check("message points at enrolment",
                "MFA" in r.json().get("detail", ""), r.json())
        ok, reason = asyncio.new_event_loop().run_until_complete(
            sec.verify_step_up("plain", "000000", "127.0.0.1", "c:x"))
        c.check("and cannot step up at all", not ok and "not enrolled" in reason,
                reason)

        c.section("a wrong code does not open it")
        ok, reason = asyncio.new_event_loop().run_until_complete(
            sec.verify_step_up("boss", "000000", "127.0.0.1", "c:fresh"))
        c.check("bad code refused", not ok, reason)
        c.check("no window opened", not sec.step_up_valid("boss", "c:fresh"))

        c.section("disabling MFA drops the open window")
        asyncio.new_event_loop().run_until_complete(
            sec.verify_step_up("boss", _totp(secret), "127.0.0.1", cred))
        asyncio.new_event_loop().run_until_complete(sec.disable_mfa("boss"))
        c.check("step-up cleared", not sec.step_up_valid("boss", cred))

    c.section("the path list is the intended shape")
    for method, path, want in [("POST", "/api/editor/save", True),
                               ("POST", "/api/editor/test-deploy", True),
                               ("GET", "/api/editor/read", False),
                               ("POST", "/api/backup/restore", True),
                               ("POST", "/api/backup/create", False),
                               ("POST", "/api/config/save", False)]:
        c.check(f"{method} {path} step-up={want}",
                needs_step_up(path, method) is want)

    return c


if __name__ == "__main__":
    run()
