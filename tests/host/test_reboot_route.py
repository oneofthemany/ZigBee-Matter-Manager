"""
POST /host/reboot on the real manager app: token required, then the trigger.
Needs FastAPI (the lockfile venv, see AGENTS.md); reported as skipped without it.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

from harness import REPO, Checker


def run() -> Checker:
    c = Checker("reboot_route")
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("  SKIPPED: needs FastAPI (see AGENTS.md, The dev box)")
        return c
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ZMM_DATA_DIR"] = tmp
        sys.path.insert(0, str(REPO))
        import manager.host, manager.upgrade
        importlib.reload(manager.host)
        importlib.reload(manager.upgrade)
        import manager.app as app_mod
        app_mod = importlib.reload(app_mod)
        api = TestClient(app_mod.app)
        trigger = Path(manager.host.REBOOT_TRIGGER)
        token = manager.upgrade.get_token()

        c.section("token")
        c.check("no token: refused, and nothing is written",
                api.post("/host/reboot").status_code == 401 and not trigger.exists())
        c.check("a wrong token: refused", api.post("/host/reboot", headers={"Authorization": "Bearer nope"}).status_code == 401)
        r = api.post("/host/reboot", headers={"Authorization": f"Bearer {token}"})
        c.check("the right token: accepted and the trigger is written", r.status_code == 200 and trigger.exists(), r.text)
        r = api.post("/host/reboot", headers={"Authorization": f"Bearer {token}"})
        c.check("a second request while pending: 409", r.status_code == 409, r.text)
    return c
