"""
The Blueair HTTP routes, driven through FastAPI's TestClient.

Skipped where FastAPI is not installed (a bare dev box). Where it is — the
container, or a venv built from requirements.lock — the real route module runs
against a temporary config directory and the fake blueair_api from
test_blueair_controller, so this checks what reading cannot: the admin gate on
account endpoints, the password never coming back, the secrets file and
config.yaml each receiving only their own half, and purifiers joining
/api/devices from cache without a cloud call on the request path.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import tempfile
import types
from pathlib import Path

from harness import Checker

import test_blueair_controller as T


def run() -> Checker:
    c = Checker("blueair_routes")
    try:
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
    except ImportError:
        print("\n  skipped (fastapi not installed)")
        return c

    from modules import blueair_controller as B

    tmp = tempfile.TemporaryDirectory()
    cwd = os.getcwd()
    os.chdir(tmp.name)
    Path("config").mkdir()
    Path("config/config.yaml").write_text("mqtt:\n  broker_host: localhost\n")

    dev = T.FakeAws("P1", name="Lounge")
    env = T.Env(lib=T._fake_library(aws=[dev]))
    env.__enter__()
    B.SECRETS_FILE = str(Path(tmp.name) / "config" / "secrets.yaml")
    try:
        sys.modules.pop("routes.blueair_routes", None)
        from routes.blueair_routes import register_blueair_routes

        app = FastAPI()

        @app.middleware("http")
        async def fake_auth(request: Request, call_next):
            scopes = request.headers.get("x-test-scopes")
            if scopes is not None:
                request.state.principal = types.SimpleNamespace(scopes=set(scopes.split(",")))
            return await call_next(request)

        register_blueair_routes(app)
        client = TestClient(app)
        admin = {"x-test-scopes": "admin"}
        viewer = {"x-test-scopes": "device:read"}

        c.section("account endpoints")
        c.check("config needs a login", client.get("/api/blueair/config").status_code == 401)
        c.check("config needs admin", client.get("/api/blueair/config", headers=viewer).status_code == 403)

        r = client.get("/api/blueair/config", headers=admin).json()
        c.check("fresh install: not configured, disabled, EU default",
                not r["configured"] and not r["enabled"] and r["region"] == "eu", r)

        r = client.post("/api/blueair/config", headers=admin,
                        json={"username": "me@example.com", "password": "hunter2",
                              "enabled": True, "region": "us"})
        body = r.json()
        c.check("saving an account succeeds", r.status_code == 200 and body["success"], body)
        c.check("the password is not echoed back", "hunter2" not in r.text)
        secrets = Path(B.SECRETS_FILE)
        c.check("the password lands in the 0600 secrets file",
                secrets.exists() and stat.S_IMODE(secrets.stat().st_mode) == 0o600
                and "hunter2" in secrets.read_text())
        cfg_text = Path("config/config.yaml").read_text()
        c.check("config.yaml gets enabled + region, never the credentials",
                "blueair" in cfg_text and "region: us" in cfg_text
                and "hunter2" not in cfg_text and "me@example.com" not in cfg_text, cfg_text)
        c.check("existing config.yaml content is preserved", "broker_host: localhost" in cfg_text)

        r = client.get("/api/blueair/config", headers=admin)
        c.check("reading config back never includes the password",
                r.json()["configured"] and "hunter2" not in r.text, r.text)

        r = client.post("/api/blueair/config", headers=admin, json={"username": "other@example.com"})
        c.check("switching account without a password is refused", r.status_code == 400, r.text)
        r = client.post("/api/blueair/config", headers=admin, json={"region": "mars"})
        c.check("an unknown region is refused", r.status_code == 400, r.text)
        r = client.post("/api/blueair/config", headers=admin, json={"enabled": True, "region": "eu"})
        c.check("a blank password keeps the stored one",
                r.status_code == 200 and B.resolve_credentials() == ("me@example.com", "hunter2"))

        r = client.post("/api/blueair/test", headers=admin, json={})
        c.check("test login falls back to the stored account",
                r.json().get("success") and r.json()["devices"][0]["id"] == "P1", r.text)
        c.check("test login needs admin", client.post("/api/blueair/test", headers=viewer, json={}).status_code == 403)

        c.section("devices")
        hook = app.state.blueair_device_entries

        async def first_list():
            entries = await hook()
            # The hook schedules the cloud refresh in the background; let it land.
            for _ in range(50):
                await asyncio.sleep(0)
            return entries, await hook()
        before, after = asyncio.run(first_list())
        c.check("the device list never waits on the cloud", before == [], before)
        c.check("purifiers join /api/devices once cached",
                len(after) == 1 and after[0]["blueair_device_id"] == "P1"
                and after[0]["type"] == "AirPurifier" and after[0]["ieee"] == "blueair_P1", after)

        r = client.get("/api/blueair/devices/P1/status").json()
        c.check("status endpoint returns the normalised status", r["success"] and r["status"]["id"] == "P1", r)
        r = client.post("/api/blueair/devices/P1/control", json={"power": False}).json()
        c.check("control endpoint applies and returns fresh status",
                r["success"] and r["status"]["power"] is False and ("set_standby", True) in dev.writes, r)
        r = client.post("/api/blueair/devices/NOPE/control", json={"power": True}).json()
        c.check("control on an unknown device fails cleanly", r["success"] is False and "Unknown" in r["error"], r)

        client.post("/api/blueair/config", headers=admin, json={"enabled": False})
        c.check("disabling removes purifiers from /api/devices", asyncio.run(hook()) == [])
    finally:
        env.__exit__(None, None, None)
        os.chdir(cwd)
        tmp.cleanup()
    return c


if __name__ == "__main__":
    checker = run()
    print(f"\n{checker.passed} passed, {len(checker.failures)} failed")
    sys.exit(1 if checker.failures else 0)
