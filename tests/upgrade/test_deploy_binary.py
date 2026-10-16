"""
scripts/test_deploy.py and the editor's test flow carry binary files (images)
byte for byte, and never send one to a hub whose flow would write the base64
text into it. A re-run sends only what the hub doesn't already have.

Needs the app's deps for the flow half, so run_all skips it on a bare host.
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from harness import Checker

REPO = Path(__file__).resolve().parents[2]
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4

spec = importlib.util.spec_from_file_location("test_deploy_script", REPO / "scripts" / "test_deploy.py")
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


def _git(repo: Path, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"})


def _checkout() -> Path:
    """A repo where, since `base`, one file changed, one changed the same way the
    hub already has, and an image was added."""
    repo = Path(tempfile.mkdtemp(prefix="zmm_deploy_"))
    _git(repo, "init", "-q")
    (repo / "static" / "images").mkdir(parents=True)
    (repo / "static" / "app.js").write_text("old\n")
    (repo / "static" / "same.js").write_text("old\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "tag", "base")
    (repo / "static" / "app.js").write_text("new\n")
    (repo / "static" / "same.js").write_text("hub has this\n")
    (repo / "static" / "images" / "logo.png").write_bytes(PNG)
    return repo


class _Hub:
    def __init__(self, binary: bool, files: dict):
        self.binary, self.files, self.deployed, self.pending = binary, dict(files), None, False

    def call(self, method, path, body=None, timeout=30):
        if path == "/api/editor/test-status":
            st = {"pending": self.pending, "remaining": 100}
            if self.binary:
                st["binary"] = True
            return 200, st
        if path.startswith("/api/editor/file?path="):
            from urllib.parse import unquote
            p = unquote(path.split("=", 1)[1])
            return 200, ({"success": True, "content": self.files[p]} if p in self.files
                         else {"success": False, "error": "File not found"})
        if path == "/api/editor/test-deploy":
            self.deployed, self.pending = body["files"], True
            return 200, {"success": True, "action": "reload", "message": "deployed"}
        if path == "/api/editor/test-confirm":
            self.pending = False
            return 200, {"success": True, "message": "Confirmed."}
        raise AssertionError(f"unexpected {method} {path}")


def _deploy(repo: Path, hub: _Hub):
    script.REPO = repo
    script.Api.call = lambda self, *a, **k: hub.call(*a, **k)
    out = io.StringIO()
    argv, real_input = sys.argv, builtins.input
    sys.argv = ["test_deploy.py", "base"]
    builtins.input = lambda *_: "y"
    token = os.environ.get("ZMM_TOKEN")
    os.environ["ZMM_TOKEN"] = "t"
    try:
        with contextlib.redirect_stdout(out):
            code = script.main()
    finally:
        sys.argv, builtins.input = argv, real_input
        if token is None:
            os.environ.pop("ZMM_TOKEN", None)
        else:
            os.environ["ZMM_TOKEN"] = token
    return code, out.getvalue()


def _flow(c: Checker, entries: list) -> None:
    """The batch the script built, through the real flow on a throwaway project."""
    import modules.test_recovery as tr
    root = Path(tempfile.mkdtemp(prefix="zmm_flow_"))
    saved = (tr.PROJECT_ROOT, tr.BACKUP_DIR, tr.DATA_DIR, tr.PENDING_FILE)
    tr.PROJECT_ROOT, tr.DATA_DIR = root, root / "data"
    tr.BACKUP_DIR, tr.PENDING_FILE = root / "data" / ".editor_backups", root / "data" / ".test_pending"
    (root / "static" / "images").mkdir(parents=True)
    (root / "static" / "images" / "old.png").write_bytes(b"\x89PNG old")
    try:
        async def go():
            m = tr.TestRecoveryManager()
            overwrite = [{"path": "static/images/old.png", "content_b64": base64.b64encode(PNG).decode()}]
            r = await m.deploy_test_batch(entries + overwrite)
            c.check("the flow takes text and images in one batch", r.get("success"), r)
            c.check("an image lands byte for byte",
                    (root / "static" / "images" / "logo.png").read_bytes() == PNG)
            c.check("text lands as text", (root / "static" / "app.js").read_text() == "new\n")
            rb = await m.rollback()
            c.check("rolling back removes a new image and restores a replaced one",
                    rb.get("success") and not (root / "static" / "images" / "logo.png").exists()
                    and (root / "static" / "images" / "old.png").read_bytes() == b"\x89PNG old", rb)
            bad = await m.deploy_test_batch([{"path": "static/x.png", "content_b64": "not base64!"}])
            c.check("content that isn't base64 is refused, with nothing written",
                    not bad.get("success") and not (root / "static" / "x.png").exists(), bad)
            if m._confirm_task:
                m._confirm_task.cancel()
        asyncio.run(go())
    finally:
        tr.PROJECT_ROOT, tr.BACKUP_DIR, tr.DATA_DIR, tr.PENDING_FILE = saved
        shutil.rmtree(root, ignore_errors=True)


def run() -> Checker:
    c = Checker("deploy binary files")
    hub_text = {"static/same.js": "hub has this\n", "static/app.js": "old\n"}

    c.section("to a hub whose flow is text-only")
    repo = _checkout()
    hub = _Hub(binary=False, files=hub_text)
    code, out = _deploy(repo, hub)
    sent = {e["path"]: e for e in hub.deployed or []}
    c.check("the changed text file is sent", "static/app.js" in sent, list(sent))
    c.check("the image is held back, not sent as text", "static/images/logo.png" not in sent
            and not any("content_b64" in e for e in sent.values()), list(sent))
    c.check("and the run says to go again once confirmed",
            code == 0 and "run the same command again" in out, out)
    c.check("a file the hub already has is not sent", "static/same.js" not in sent)

    hub = _Hub(binary=False, files={**hub_text, "static/app.js": "new\n"})
    code, out = _deploy(repo, hub)
    c.check("with only the image left, nothing is sent and the run fails, saying why",
            hub.deployed is None and code == 1 and "can't take them" in out, out)

    c.section("to a hub whose flow takes binary files")
    hub = _Hub(binary=True, files=hub_text)
    code, out = _deploy(repo, hub)
    sent = {e["path"]: e for e in hub.deployed or []}
    png = sent.get("static/images/logo.png", {})
    c.check("the image is sent as base64 of its exact bytes",
            "content" not in png and base64.b64decode(png.get("content_b64", "")) == PNG, list(png))
    c.check("text is still sent as text", sent.get("static/app.js", {}).get("content") == "new\n")
    c.check("and nothing is said about holding back", code == 0 and "Held back" not in out, out)
    _flow(c, list(sent.values()))

    hub = _Hub(binary=True, files={**hub_text, "static/app.js": "new\n"})
    code, out = _deploy(repo, hub)
    c.check("a re-run sends only what is left", [e["path"] for e in hub.deployed] == ["static/images/logo.png"],
            hub.deployed and [e["path"] for e in hub.deployed])
    shutil.rmtree(repo, ignore_errors=True)
    return c


if __name__ == "__main__":
    run()
