"""
Shared scaffolding for the manager tests.

FakeRuntime is a stand-in podman/docker: a small HTTP server on a unix socket
serving fixture /images/json, /containers/json, inspect and stats, and
recording DELETE /images/... — so manager modules talk to it exactly as they
would to the real socket, and a test can see what they tried to remove.
"""

from __future__ import annotations

import json
import os
import socketserver
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class Checker:
    """Collects pass/fail lines so a module can report as a group."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.failures: list[str] = []
        self.passed = 0

    def section(self, title: str) -> None:
        print(f"\n  {title}")

    def check(self, label: str, ok: bool, detail: object = "") -> bool:
        if ok:
            self.passed += 1
            print(f"    ok   {label}")
        else:
            self.failures.append(f"{self.name}: {label}")
            print(f"    FAIL {label}  <- {detail!r}"[:600])
        return bool(ok)


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class FakeRuntime:
    def __init__(self) -> None:
        self.images: List[Dict[str, Any]] = []
        self.containers: List[Dict[str, Any]] = []
        self.inspect: Dict[str, Dict[str, Any]] = {}
        self.stats: Dict[str, Dict[str, Any]] = {}
        self.deleted: List[str] = []
        self.refuse_delete: Dict[str, int] = {}     # ref -> status code to answer with
        self.posts: List[Dict[str, Any]] = []       # every POST: path, query, body
        self.on_post = None                         # optional hook(path) -> extra fields to record
        self._dir = tempfile.TemporaryDirectory()
        self.sock = os.path.join(self._dir.name, "runtime.sock")
        rt = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def address_string(self):
                return "unix"

            def _send(self, code: int, body: Any = None):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/images/json":
                    return self._send(200, rt.images)
                if path == "/containers/json":
                    return self._send(200, rt.containers)
                parts = path.strip("/").split("/")
                if len(parts) == 3 and parts[0] == "containers":
                    name = unquote(parts[1])
                    if parts[2] == "json" and name in rt.inspect:
                        return self._send(200, rt.inspect[name])
                    if parts[2] == "stats" and name in rt.stats:
                        return self._send(200, rt.stats[name])
                return self._send(404, {"message": "no such object"})

            def do_POST(self):
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                rec = {"path": u.path, "query": u.query, "body": body}
                if rt.on_post:
                    rec.update(rt.on_post(u.path) or {})
                rt.posts.append(rec)
                if u.path == "/containers/create":
                    return self._send(201, {"Id": "new-container"})
                return self._send(204)

            def do_DELETE(self):
                path = urlparse(self.path).path
                if path.startswith("/images/"):
                    ref = unquote(path[len("/images/"):])
                    if ref in rt.refuse_delete:
                        return self._send(rt.refuse_delete[ref], {"message": "image is in use"})
                    rt.deleted.append(ref)
                    return self._send(200, [{"Untagged": ref}])
                return self._send(404, {"message": "no such object"})

        self._server = _UnixHTTPServer(self.sock, Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._dir.cleanup()


def image(short: str, tags: List[str], created: int, size: int = 100_000_000) -> Dict[str, Any]:
    return {"Id": "sha256:" + short.ljust(64, "0"), "RepoTags": tags or None, "Created": created, "Size": size}


def container(name: str, image_short: str, state: str = "running") -> Dict[str, Any]:
    return {"Names": ["/" + name], "ImageID": "sha256:" + image_short.ljust(64, "0"),
            "Image": "x", "State": state, "Status": state}
