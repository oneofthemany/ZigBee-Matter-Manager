"""The recorder's loopback API: the app sends what to record and each camera's
signal changes; it reads back status. See docs/recordings.md §Sidecar API."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .core import MAX_THUMB_BYTES, Recorder

logger = logging.getLogger("recorder.server")

MAX_BODY = 256 * 1024


def load_token(path: Path) -> str:
    """The shared secret between ZMM and this sidecar; whoever starts first makes it."""
    try:
        tok = path.read_text().strip()
        if tok:
            return tok
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok


def make_handler(rec: Recorder, token: str, loop: asyncio.AbstractEventLoop):
    def on_loop(fn, *args) -> Any:
        """Run on the recorder's loop: its state is touched from nowhere else."""
        async def call():
            return fn(*args)
        return asyncio.run_coroutine_threadsafe(call(), loop).result(timeout=10)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def _json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            if hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                return True
            self._json(401, {"error": "unauthorized"})
            return False

        def _body(self, limit: int) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= limit:
                raise ValueError("body too large or empty")
            return self.rfile.read(n)

        def do_GET(self) -> None:                         # noqa: N802
            if not self._authed():
                return
            if self.path.split("?")[0] == "/status":
                return self._json(200, on_loop(rec.status))
            self._json(404, {"error": "not found"})

        def do_PUT(self) -> None:                         # noqa: N802
            if not self._authed():
                return
            try:
                if self.path == "/config":
                    return self._json(200, {"config": on_loop(rec.configure, json.loads(self._body(MAX_BODY)).get("cameras"))})
                m = re.fullmatch(r"/thumb/([a-z0-9_-]+)", self.path)
                if m:
                    return self._json(200, {"kept": on_loop(rec.set_thumb, m.group(1), self._body(MAX_THUMB_BYTES))})
            except (ValueError, AttributeError, TypeError) as e:
                return self._json(400, {"error": str(e)})
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:                        # noqa: N802
            if not self._authed():
                return
            if self.path != "/signal":
                return self._json(404, {"error": "not found"})
            try:
                body = json.loads(self._body(MAX_BODY))
                cid, state = str(body.get("camera") or ""), body.get("state")
                if not isinstance(state, dict):
                    raise ValueError("state must be an object")
            except (ValueError, AttributeError, TypeError) as e:
                return self._json(400, {"error": str(e)})
            self._json(200, {"started": on_loop(rec.signal, cid, state)})

    return Handler


def serve(rec: Recorder, token: str, host: str, port: int, loop: asyncio.AbstractEventLoop) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(rec, token, loop))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    return srv
