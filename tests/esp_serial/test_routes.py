"""The speaker serial API over real FastAPI: admin-only, refusals with their reasons. Skipped without FastAPI."""

from __future__ import annotations

import tempfile
from pathlib import Path

from harness import Checker, fake_sysfs


def run() -> Checker:
    c = Checker("routes")
    try:
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
    except ImportError:
        print("\n  skipped (fastapi not installed)")
        return c

    from modules import esp_serial as S
    from modules.auth import User
    from modules.auth_middleware import Principal
    from modules.auth_scopes import scope_for_path
    from routes.esp_serial_routes import register_esp_serial_routes

    c.check("every path is admin in the scope table",
            all(scope_for_path(p, m) == "admin" for p, m in
                (("/api/esp-serial/ports", "GET"), ("/api/esp-serial/ports/ttyACM0/flash", "POST"),
                 ("/api/esp-serial/backups/x/download", "GET"))))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sysfs = fake_sysfs(root, {"ttyACM0": ("303a", "A0:85:E3:E8:8A:58")})
        (root / "dev").mkdir()
        (root / "dev" / "ttyACM0").touch()
        m = S.EspSerialManager(lambda: None, sysfs=str(sysfs), dev_root=str(root / "dev"), root=root / "data")
        app = FastAPI()

        @app.middleware("http")
        async def sign_in(request: Request, call_next):
            user = request.headers.get("X-User")
            if user:
                scopes = {"admin"} if user == "root" else {"device:read", "device:write"}
                request.state.principal = Principal(User(username=user), scopes, auth_method="cookie")
            return await call_next(request)

        register_esp_serial_routes(app, lambda: m)
        api = TestClient(app)
        root_h, alex = {"X-User": "root"}, {"X-User": "alex"}
        c.check("a household member can't list speakers", api.get("/api/esp-serial/ports", headers=alex).status_code == 403)
        r = api.get("/api/esp-serial/ports", headers=root_h)
        c.check("an admin can", r.status_code == 200 and r.json()["ports"][0]["name"] == "ttyACM0", r.text)
        r = api.post("/api/esp-serial/ports/ttyACM0/info", headers=root_h, json={})
        c.check("a restart without acknowledgement is a 400 that says why", r.status_code == 400 and "safe mode" in r.text, r.text)
        m.ledger.admit("usb:A0:85:E3:E8:8A:58", False)
        r = api.post("/api/esp-serial/ports/ttyACM0/backup", headers=root_h, json={"acknowledge_restart": True})
        c.check("inside the cooldown it is a 429 with Retry-After",
                r.status_code == 429 and int(r.headers.get("retry-after", "0")) > 0, (r.status_code, r.text))
        c.check("an unknown port is a 404", api.get("/api/esp-serial/ports/ttyUSB0/log", headers=root_h).status_code == 404)
        r = api.post("/api/esp-serial/images?name=x.bin", headers={**root_h, "Content-Type": "application/octet-stream"}, content=b"PK\x03\x04")
        c.check("a non-image upload is a 400", r.status_code == 400, r.text)
    return c
