"""
/api/system/manager: the hub's own answer to "is the ZMM Manager up?", from a
stand-in manager on a spare port. Needs FastAPI (the lockfile venv, see
AGENTS.md); reported as skipped without it.
"""

from __future__ import annotations

import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from harness import Checker


class _Healthz(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path == "/healthz"
        self.send_response(200 if ok else 404)
        self.end_headers()
        self.wfile.write(b'{"manager":"ok"}' if ok else b"")

    def log_message(self, *a):
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run() -> Checker:
    c = Checker("manager_info")
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes.system_routes import register_system_routes
    except ImportError:
        print("  SKIPPED: needs FastAPI (see AGENTS.md, The dev box)")
        return c

    app = FastAPI()
    register_system_routes(app, lambda: None, lambda: None, lambda: None)
    api = TestClient(app)
    port = _free_port()
    saved = os.environ.get("ZMM_MANAGER_PORT")
    os.environ["ZMM_MANAGER_PORT"] = str(port)
    try:
        c.section("manager running (plain HTTP, as when the app has no cert)")
        server = HTTPServer(("127.0.0.1", port), _Healthz)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        info = api.get("/api/system/manager").json()
        c.check("reports it up, on the scheme that answered", info["up"] is True and info["scheme"] == "http", info)
        c.check("uses the configured manager port", info["port"] == port, info)
        c.check("offers a LAN address on that scheme and port",
                info["lan_url"] is None or (info["lan_url"].startswith("http://") and info["lan_url"].endswith(f":{port}/")), info)

        c.section("manager stopped")
        server.shutdown(); server.server_close()
        info = api.get("/api/system/manager").json()
        c.check("reports it down rather than hanging", info["up"] is False and info["scheme"] is None, info)
    finally:
        if saved is None:
            os.environ.pop("ZMM_MANAGER_PORT", None)
        else:
            os.environ["ZMM_MANAGER_PORT"] = saved
    return c
