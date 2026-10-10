"""
The Shelly/ESPHome setup API over real FastAPI: admin-only, and passwords
never come back. Skipped without FastAPI.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from harness import Checker

import test_shelly as T


def run() -> Checker:
    c = Checker("lan_routes")
    try:
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
    except ImportError:
        print("\n  skipped (fastapi not installed)")
        return c

    from modules import lan_devices as L
    from modules import shelly as S
    from modules.auth import User
    from modules.auth_middleware import Principal
    from modules.auth_scopes import scope_for_path
    from routes.lan_device_routes import register_lan_device_routes

    with tempfile.TemporaryDirectory() as tmp:
        saved = L.SECRETS_FILE
        L.SECRETS_FILE = str(Path(tmp) / "secrets.yaml")
        hub = S.ShellyHub(path=Path(tmp) / "s.json",
                          client_factory=lambda h, p=80, u="", pw="", g=None: T.FakeClient(h, p, u, pw, g))
        try:
            app = FastAPI()

            @app.middleware("http")
            async def sign_in(request: Request, call_next):
                user = request.headers.get("X-User")
                if user:
                    scopes = {"admin"} if user == "root" else {"device:read", "device:write"}
                    request.state.principal = Principal(User(username=user), scopes, auth_method="cookie")
                return await call_next(request)

            register_lan_device_routes(app, "shelly", lambda: hub)
            api = TestClient(app)
            root, alex = {"X-User": "root"}, {"X-User": "alex"}

            c.check("a household member can't add devices",
                    api.post("/api/shelly", headers=alex, json={"host": "10.0.0.9"}).status_code == 403)
            c.check("or list their addresses", api.get("/api/shelly", headers=alex).status_code == 403)
            r = api.post("/api/shelly", headers=root, json={"host": "10.0.0.9", "password": "hunter2"})
            c.check("an admin adds one", r.status_code == 200 and r.json()["has_credentials"], r.text)
            c.check("the password never comes back",
                    "hunter2" not in r.text and "hunter2" not in api.get("/api/shelly", headers=root).text)
            c.check("a bad host is a 400", api.post("/api/shelly", headers=root, json={"host": "a b"}).status_code == 400)
            c.check("editing an unknown device is a 404",
                    api.put("/api/shelly/nope", headers=root, json={"name": "x"}).status_code == 404)
            dev_id = r.json()["id"]
            c.check("an admin renames it", api.put(f"/api/shelly/{dev_id}", headers=root,
                                                   json={"name": "Porch"}).json()["name"] == "Porch")
            c.check("and removes it", api.delete(f"/api/shelly/{dev_id}", headers=root).status_code == 200)

            c.section("scope table")
            c.check("setup paths are admin", scope_for_path("/api/esphome/discover", "POST") == "admin"
                    and scope_for_path("/api/shelly", "GET") == "admin")
            c.check("using a device is device:write, like any other",
                    scope_for_path("/api/device/command", "POST") == "device:write")
        finally:
            L.SECRETS_FILE = saved
    return c
