"""
The images/containers routes on the real manager app: reads open, changes and
container detail behind the token. Needs FastAPI (the lockfile venv, see
AGENTS.md); reported as skipped without it.
"""

from __future__ import annotations

import importlib
import json
import os
import tempfile
import time
from pathlib import Path

from harness import Checker, FakeRuntime, container, image


def run() -> Checker:
    c = Checker("routes")
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("  SKIPPED: needs FastAPI (see AGENTS.md, The dev box)")
        return c
    with tempfile.TemporaryDirectory() as data:
        rt = FakeRuntime()
        os.environ["ZMM_CONTAINER_SOCK"] = rt.sock
        os.environ["ZMM_DATA_DIR"] = data
        os.environ["ZMM_WATCHDOG_DISABLED"] = "1"
        import manager.containers, manager.upgrade, manager.images, manager.host
        for m in (manager.containers, manager.upgrade, manager.images, manager.host):
            importlib.reload(m)
        import manager.app as app_mod
        app_mod = importlib.reload(app_mod)
        upgrade = manager.upgrade
        Path(upgrade.VERSION_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(upgrade.VERSION_STATE_FILE).write_text(json.dumps({"current_version": "04.10.2026", "retention_count": 1}))
        now = int(time.time())
        old = image("b2", ["localhost/zigbee-matter-manager:09.2026-amd64"], now - 9 * 86400)
        rt.images = [image("b1", ["localhost/zigbee-matter-manager:04.10.2026-amd64"], now - 86400), old]
        rt.containers = [container("zigbee-matter-manager", "b1")]
        rt.inspect["zigbee-matter-manager"] = {"Image": "sha256:b1", "Config": {"Env": ["A=1"]},
                                               "HostConfig": {}, "State": {"Running": False}}
        with TestClient(app_mod.app) as api:
            auth = {"Authorization": f"Bearer {upgrade.get_token()}"}

            c.section("reads are open")
            c.check("GET /images lists the images", len(api.get("/images").json()["images"]) == 2)
            c.check("GET /images/cleanup-plan offers the old version",
                    [x["id"] for x in api.get("/images/cleanup-plan").json()["candidates"]] == [old["Id"]])

            c.section("changes and detail need the token")
            r = api.post("/images/cleanup", json={"ids": [old["Id"]]})
            c.check("clean-up without the token: 401, nothing removed", r.status_code == 401 and rt.deleted == [], r.text)
            c.check("container detail without the token: 401",
                    api.get("/containers/zigbee-matter-manager/detail").status_code == 401)
            c.check("a malformed clean-up body: 400",
                    api.post("/images/cleanup", json={"ids": "all"}, headers=auth).status_code == 400)
            r = api.post("/images/cleanup", json={"ids": [old["Id"]]}, headers=auth)
            c.check("with the token: the old version is removed", r.json().get("removed") == 1
                    and rt.deleted == ["localhost/zigbee-matter-manager:09.2026-amd64"], r.text)
            c.check("detail with the token", api.get("/containers/zigbee-matter-manager/detail", headers=auth).status_code == 200)
            c.check("detail of a container outside this deployment: 404",
                    api.get("/containers/postgres/detail", headers=auth).status_code == 404)
        rt.close()
    return c
