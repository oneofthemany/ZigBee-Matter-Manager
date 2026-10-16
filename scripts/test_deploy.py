#!/usr/bin/env python3
"""
Send changed files to a running ZMM through its own test-and-deploy flow
(the editor's "time machine"): one batch, backed up, restarted if any file
needs it, and rolled back automatically unless confirmed. See
docs/upgrades.md §Boot Guard.

    ZMM_TOKEN=<admin API token> scripts/test_deploy.py <git-ref> [--url URL] [--yes]

Sends every file changed between <git-ref> and the working tree, except
tests/ and VERSION, and except files the hub already has as they are here. A
file deleted since <git-ref> is reported, not deleted: the flow only writes.
Binary files (images) go base64, and only to a hub whose flow says it takes
them. Standard library only, so it runs from any checkout.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# scripts/ run on the host, installed by install_watcher.sh, not from /app.
SKIP = ("tests/", "scripts/", "VERSION")


def changed(ref: str):
    out = subprocess.run(["git", "-C", str(REPO), "diff", "--name-status", ref, "--"],
                         capture_output=True, text=True, check=True).stdout
    keep, deleted = [], []
    for line in out.splitlines():
        status, *paths = line.split("\t")
        path = paths[-1]
        if path.startswith(SKIP) or path in SKIP:
            continue
        (deleted if status.startswith("D") else keep).append(path)
    untracked = subprocess.run(["git", "-C", str(REPO), "ls-files", "--others", "--exclude-standard"],
                               capture_output=True, text=True, check=True).stdout.split()
    keep += [p for p in untracked if not p.startswith(SKIP) and p not in keep]
    return sorted(keep), deleted


def entry(path: str) -> dict:
    """The file as the deploy flow takes it: text as itself, anything else base64."""
    raw = (REPO / path).read_bytes()
    try:
        return {"path": path, "content": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return {"path": path, "content_b64": base64.b64encode(raw).decode("ascii")}


class Api:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token
        # The hub's certificate is self-signed.
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname, self.ctx.verify_mode = False, ssl.CERT_NONE

    def call(self, method: str, path: str, body=None, timeout: float = 30):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    def up(self) -> bool:
        try:
            return self.call("GET", "/api/system/health", timeout=5)[0] == 200
        except (OSError, urllib.error.URLError):
            return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("ref", help="what the hub is running, e.g. v10.02.10.2026 or a commit")
    ap.add_argument("--url", default=os.environ.get("ZMM_URL", "https://192.168.1.1:8000"))
    ap.add_argument("--yes", action="store_true", help="confirm automatically once the app is healthy")
    a = ap.parse_args()
    token = os.environ.get("ZMM_TOKEN")
    if not token:
        print("Set ZMM_TOKEN to an admin API token (Settings → User Accounts → New token).")
        return 2

    try:
        files, deleted = changed(a.ref)
    except subprocess.CalledProcessError:
        print(f"git doesn't know '{a.ref}' — give a commit or tag in this checkout.")
        return 2
    if not files:
        print("Nothing changed since", a.ref)
        return 0
    print(f"{len(files)} file(s) since {a.ref}:")
    for f in files:
        print("  ", f)
    if deleted:
        print("Not deleted on the hub (the test flow only writes):", ", ".join(deleted))

    api = Api(a.url, token)
    code, st = api.call("GET", "/api/editor/test-status")
    if code == 401 or code == 403:
        print("The token was refused — it needs the admin scope.")
        return 1
    if st.get("pending"):
        print(f"A test batch is already pending ({st.get('count')} file(s)); confirm or roll it back first.")
        return 1

    batch = [entry(f) for f in files]

    # A hub already holding a file as it is here needn't have it again, so a
    # re-run sends only what is left rather than restarting for nothing.
    same = []
    for e in batch:
        if "content" in e:
            code, cur = api.call("GET", "/api/editor/file?path=" + urllib.parse.quote(e["path"]))
            if code == 200 and cur.get("success") and cur.get("content") == e["content"]:
                same.append(e["path"])
    batch = [e for e in batch if e["path"] not in same]
    if same:
        print(f"Already on the hub, not sent: {len(same)} file(s).")

    held = []
    if not st.get("binary"):
        held = [e["path"] for e in batch if "content_b64" in e]
        batch = [e for e in batch if "content_b64" not in e]
    if not batch:
        if held:
            print("Only binary files are left, and this hub's deploy flow can't take them:",
                  ", ".join(held), "— they need a release, or a hub running a newer flow.")
            return 1
        print("The hub already has every file.")
        return 0

    code, r = api.call("POST", "/api/editor/test-deploy", {"files": batch}, timeout=120)
    if not r.get("success"):
        print("Not deployed:", r.get("error") or f"HTTP {code}")
        return 1
    print(r.get("message"))

    if r.get("action") == "restart":
        code, rr = api.call("POST", "/api/editor/test-restart")
        if code == 409 or not rr.get("success", True):
            print("Restart refused:", rr.get("error"), "— the batch stays pending; it will roll back unless confirmed.")
            return 1
        print("Restarting", end="", flush=True)
        time.sleep(5)
        for _ in range(60):
            if api.up():
                break
            print(".", end="", flush=True)
            time.sleep(3)
        else:
            print("\nNo answer after 3 minutes. The boot guard rolls back a batch that won't start.")
            return 1
        print(" up.")

    code, st = api.call("GET", "/api/editor/test-status")
    if not st.get("pending"):
        print("The batch is no longer pending — it was rolled back (see the app's logs).")
        return 1
    left = st.get("remaining", 0)
    if not a.yes:
        print(f"Check the app now. {left}s to confirm, or it rolls back by itself.")
        try:
            answer = input("Keep these changes? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "y":
            code, rb = api.call("POST", "/api/editor/test-rollback")
            print(rb.get("message") or f"Rollback: HTTP {code}")
            return 1
    code, c = api.call("POST", "/api/editor/test-confirm")
    print(c.get("message") or ("Confirmed." if c.get("success") else f"Confirm failed: HTTP {code}"))
    ok = c.get("success", code == 200)
    if ok and held:
        # Sent as text, an older flow would write the base64 into the file.
        print(f"Held back {len(held)} binary file(s) this hub's flow couldn't take:", ", ".join(held))
        print("If this batch brought the newer flow, run the same command again to send them.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
