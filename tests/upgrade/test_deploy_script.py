"""scripts/test_deploy.py offers to keep a batch only once the app has booted,
and never sends local tooling folders."""

from __future__ import annotations

import builtins
import contextlib
import importlib.util
import io
import os
import sys

from harness import Checker, REPO

spec = importlib.util.spec_from_file_location("test_deploy_script2", REPO / "scripts" / "test_deploy.py")
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


class _Hub:
    """A hub whose batch needs a restart; health walks through `bringup`."""

    def __init__(self, bringup):
        self.bringup, self.calls, self.pending = list(bringup), [], False

    def call(self, method, path, body=None, timeout=30):
        self.calls.append(path)
        if path == "/api/editor/test-status":
            return 200, {"pending": self.pending, "remaining": 100}
        if path.startswith("/api/editor/file?path="):
            return 200, {"success": False}
        if path == "/api/editor/test-deploy":
            self.pending = True
            return 200, {"success": True, "action": "restart", "message": "deployed"}
        if path == "/api/editor/test-restart":
            return 200, {"success": True}
        if path == "/api/system/health":
            state = self.bringup.pop(0) if len(self.bringup) > 1 else self.bringup[0]
            if state == "failed":
                return 503, {"status": "error", "bringup": "failed", "error": "TypeError: boom"}
            return 200, {"status": "ok", "bringup": state}
        if path == "/api/editor/test-rollback":
            self.pending = False
            return 200, {"success": True, "message": "Rolled back."}
        if path == "/api/editor/test-confirm":
            self.pending = False
            return 200, {"success": True, "message": "Confirmed."}
        raise AssertionError(f"unexpected {method} {path}")


def _deploy(hub):
    asked = []
    saved = (script.Api.call, script.changed, script.entry, script.time.sleep, sys.argv, builtins.input)
    script.Api.call = lambda self, *a, **k: hub.call(*a, **k)
    script.changed = lambda ref: (["modules/x.py"], [])
    script.entry = lambda p: {"path": p, "content": "x\n"}
    script.time.sleep = lambda *_: None
    sys.argv = ["test_deploy.py", "base"]
    builtins.input = lambda *_: asked.append(1) or "y"
    os.environ.setdefault("ZMM_TOKEN", "t")
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = script.main()
    finally:
        (script.Api.call, script.changed, script.entry, script.time.sleep, sys.argv, builtins.input) = saved
    return code, out.getvalue(), asked


def run() -> Checker:
    c = Checker("test_deploy script")

    c.section("health answers while bring-up is still starting, then it fails")
    hub = _Hub(["starting", "starting", "failed"])
    code, out, asked = _deploy(hub)
    c.check("keeping is never offered", not asked, out)
    c.check("the batch is rolled back", "/api/editor/test-rollback" in hub.calls
            and "/api/editor/test-confirm" not in hub.calls, hub.calls)
    c.check("the reason is printed", "TypeError: boom" in out, out)
    c.check("and the run fails", code == 1, code)

    c.section("bring-up finishes")
    hub = _Hub(["starting", "ready"])
    code, out, asked = _deploy(hub)
    c.check("keeping is offered only after it has booted",
            asked and hub.calls.count("/api/system/health") >= 2 and "booted" in out, out)
    c.check("and confirmed", "/api/editor/test-confirm" in hub.calls and code == 0, hub.calls)

    c.section("what gets sent")
    c.check("local tooling folders never go to the hub",
            all(script.skipped(p) for p in (".rene/secrets/live-token", ".codegraph/.gitignore",
                                            ".github/workflows/ci.yml")))
    c.check("app files still do", not script.skipped("modules/go2rtc.py") and not script.skipped("main.py"))
    return c
