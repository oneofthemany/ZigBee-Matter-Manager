"""The first boot after a safe deploy: checked, recorded for the ZMM Manager,
and rolled back on a failed bring-up only when that is switched on."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

from harness import Checker

import modules.safe_deploy as SD


def _rig(statuses, http_ok=True, rollback=False, error=None):
    """Run the check with a marker present; return what it did and recorded."""
    seen = {"restored": [], "restarted": 0, "http": 0, "record": None}
    saved = (SD._read_deploy_marker, SD._remove_deploy_marker, SD._restore_backup,
             SD._restart_service, SD.HEALTH_CHECK_INTERVAL, SD.DEPLOY_CHECK_FILE, asyncio.sleep)
    real_sleep = asyncio.sleep
    it = iter(statuses)
    last = {"s": None}

    def status():
        last["s"] = next(it, last["s"])
        return last["s"]

    class Resp:
        status = 200 if http_ok else 500
        async def __aenter__(self): seen["http"] += 1; return self
        async def __aexit__(self, *a): return False

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def get(self, *a, **k): return Resp()

    import aiohttp
    saved_session = aiohttp.ClientSession
    record = os.path.join(tempfile.mkdtemp(prefix="zmm_deploy_check_"), "last_deploy_check.json")
    try:
        SD._read_deploy_marker = lambda: {"backup_id": "b1"}
        SD._remove_deploy_marker = lambda: None
        SD._restore_backup = lambda b: seen["restored"].append(b) or {"success": True}
        SD._restart_service = lambda: seen.__setitem__("restarted", seen["restarted"] + 1)
        SD.HEALTH_CHECK_INTERVAL = 0
        SD.DEPLOY_CHECK_FILE = record
        asyncio.sleep = lambda *_a, **_k: real_sleep(0)
        aiohttp.ClientSession = Session
        asyncio.run(SD.check_deploy_on_startup(status, rollback_on_bringup_failure=rollback,
                                               bringup_error=lambda: error))
        if os.path.exists(record):
            with open(record) as f:
                seen["record"] = json.load(f)
    finally:
        (SD._read_deploy_marker, SD._remove_deploy_marker, SD._restore_backup,
         SD._restart_service, SD.HEALTH_CHECK_INTERVAL, SD.DEPLOY_CHECK_FILE, asyncio.sleep) = saved
        aiohttp.ClientSession = saved_session
    return seen


def run() -> Checker:
    c = Checker("deploy_rollback")
    why = "TypeError: read_section() takes 1 positional argument but 2 were given"

    c.section("bring-up fails, auto-rollback off (the default)")
    s = _rig(["starting", "failed"], error=why)
    c.check("the deploy is kept", s["restored"] == [] and s["restarted"] == 0, s)
    r = s["record"] or {}
    c.check("the manager is told it was kept despite the failure", r.get("outcome") == "kept", r)
    c.check("and why bring-up failed", r.get("error") == why and r.get("bringup") == "failed", r)

    c.section("bring-up fails, auto-rollback on")
    s = _rig(["starting", "starting", "failed"], rollback=True, error=why)
    c.check("the backup is restored", s["restored"] == ["b1"], s)
    c.check("and the app restarted on it", s["restarted"] == 1, s)
    c.check("without trusting an HTTP answer from the half-started app", s["http"] == 0, s)
    r = s["record"] or {}
    c.check("the manager is told it was rolled back, and why",
            r.get("outcome") == "rolled_back" and r.get("error") == why, r)

    c.section("bring-up finishes")
    s = _rig(["starting", "ready"])
    c.check("nothing is restored", s["restored"] == [] and s["restarted"] == 0, s)
    c.check("the app is still asked to serve devices", s["http"] == 1, s)
    c.check("a healthy deploy is recorded as such", (s["record"] or {}).get("outcome") == "ok", s["record"])

    c.section("bring-up finishes but the app cannot serve")
    s = _rig(["ready"], http_ok=False)
    c.check("it is rolled back whatever the option says",
            s["restored"] == ["b1"] and s["restarted"] == 1, s)
    c.check("with a reason the manager can show",
            "did not answer" in str((s["record"] or {}).get("error")), s["record"])
    return c
