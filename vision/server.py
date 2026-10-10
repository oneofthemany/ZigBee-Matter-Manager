"""The sidecar's loopback API. ZMM pushes which cameras to watch, then
long-polls for presence. Config is held in memory only: it carries stream
credentials. See docs/vision.md §Sidecar API."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from . import GROUPS
from .worker import CameraWorker, encode_jpeg

logger = logging.getLogger("vision.server")

MAX_BODY = 256 * 1024
MAX_WAIT_S = 30
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


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


class Hub:
    """Workers, and a version number that moves whenever anything observable does."""

    def __init__(self, detector: Any, worker_cls=CameraWorker):
        self.detector, self._worker_cls = detector, worker_cls
        self.workers: Dict[str, CameraWorker] = {}
        self.config_hash = ""
        self.version = 0
        self._cond = threading.Condition()

    def bump(self) -> None:
        with self._cond:
            self.version += 1
            self._cond.notify_all()

    def wait(self, after: int, timeout: float) -> None:
        with self._cond:
            self._cond.wait_for(lambda: self.version != after, timeout=timeout)

    def configure(self, cameras: Any) -> None:
        if not isinstance(cameras, list) or len(cameras) > 64:
            raise ValueError("cameras must be a list of at most 64")
        want: Dict[str, Dict[str, Any]] = {}
        for c in cameras:
            cid, url = str(c.get("id") or ""), str(c.get("url") or "")
            if not _ID_RE.match(cid) or not url or url.startswith("-") or any(ch in url for ch in "\r\n "):
                raise ValueError(f"bad camera entry '{cid}'")
            want[cid] = {"id": cid, "url": url,
                         "labels": [g for g in (c.get("labels") or list(GROUPS)) if g in GROUPS],
                         "threshold": min(max(float(c.get("threshold") or 0.5), 0.3), 0.95),
                         "fps": min(max(float(c.get("fps") or 2), 0.5), 5)}
        for cid in list(self.workers):
            if self.workers[cid].cfg != want.get(cid):
                self.workers.pop(cid).stop()
        for cid, cfg in want.items():
            if cid not in self.workers:
                w = self._worker_cls(cfg, self.detector, self.bump)
                self.workers[cid] = w
                w.start()
        self.config_hash = hashlib.sha256(json.dumps(want, sort_keys=True).encode()).hexdigest()[:16]
        self.bump()

    def status(self) -> Dict[str, Any]:
        d = self.detector
        return {"version": self.version, "config": self.config_hash, "ready": d.ready,
                "backend": d.backend, "wanted": d.want, "note": d.note,
                "inference_ms": round(d.ms, 1), "inferences": d.count,
                "cameras": {cid: w.public() for cid, w in self.workers.items()}}


def make_handler(hub: Hub, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):                        # the access log is noise
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj).encode())

        def _authed(self) -> bool:
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got, f"Bearer {token}"):
                return True
            self._json(401, {"error": "unauthorized"})
            return False

        def do_GET(self) -> None:                         # noqa: N802
            if not self._authed():
                return
            u = urlparse(self.path)
            if u.path == "/status":
                q = parse_qs(u.query)
                if "after" in q:
                    try:
                        hub.wait(int(q["after"][0]), min(float(q.get("wait", ["25"])[0]), MAX_WAIT_S))
                    except ValueError:
                        return self._json(400, {"error": "bad after/wait"})
                return self._json(200, hub.status())
            m = re.fullmatch(r"/snapshot/([a-z0-9_-]+)\.jpg", u.path)
            if m:
                w = hub.workers.get(m.group(1))
                rgb = w.snapshot_rgb() if w else None
                if rgb is None:
                    return self._json(404, {"error": "nothing detected yet"})
                try:
                    return self._send(200, encode_jpeg(rgb), "image/jpeg")
                except Exception as e:                    # noqa: BLE001
                    return self._json(500, {"error": str(e)})
            self._json(404, {"error": "not found"})

        def do_PUT(self) -> None:                         # noqa: N802
            if not self._authed():
                return
            if urlparse(self.path).path != "/config":
                return self._json(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= MAX_BODY:
                    raise ValueError("body too large or empty")
                hub.configure(json.loads(self.rfile.read(n)).get("cameras"))
            except (ValueError, AttributeError, TypeError) as e:
                return self._json(400, {"error": str(e)})
            self._json(200, {"config": hub.config_hash})

    return Handler


def serve(hub: Hub, token: str, host: str, port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(hub, token))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    return srv
