"""
go2rtc: the streaming sidecar that turns camera RTSP into browser video.
ZMM writes its config and drives it through its HTTP API; the ZMM Manager owns
the container (manager/go2rtc.py). Browsers never reach go2rtc — ZMM proxies
snapshots and streams behind its own auth. See docs/cameras.md §go2rtc.

The config shuts every go2rtc server but the API: its RTSP server would
otherwise re-publish every camera on :8554, and it never asks a loopback
client for the password. The object-detection sidecar reads the API's stream
endpoint instead, which does — so a camera is still connected to only once.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("go2rtc")

SECRETS_FILE = os.environ.get("ZMM_SECRETS_FILE", "./config/secrets.yaml")
CONFIG_DIR = Path(os.environ.get("ZMM_GO2RTC_DIR", "./data/go2rtc"))
# The manager runs this image (manager/go2rtc.IMAGE, kept in step).
IMAGE = "docker.io/alexxit/go2rtc:1.9.14"
PORT = 1984
TIMEOUT_S = 10

HttpFn = Callable[..., Awaitable[Tuple[int, Any]]]


class Go2rtcError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _mask(text: str) -> str:
    """go2rtc's errors can quote a stream URL, credentials and all."""
    import re
    return re.sub(r"://[^@/\s]+@", "://***@", text)


# Settings: where go2rtc is and the API credentials ZMM generated for it.

def _read_secrets() -> Dict[str, Any]:
    try:
        import yaml
        with open(SECRETS_FILE, "r") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"Could not read {SECRETS_FILE}: {e}")
        return {}


def _write_secrets_section(name: str, value: Any) -> None:
    import yaml
    path = Path(SECRETS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_secrets()
    existing[name] = value
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        yaml.dump(existing, fh, default_flow_style=False, sort_keys=False)
    os.chmod(path, 0o600)


def load_settings() -> Dict[str, Any]:
    raw = _read_secrets().get("go2rtc") or {}
    return {"url": str(raw.get("url") or f"http://127.0.0.1:{PORT}").rstrip("/"),
            "listen": str(raw.get("listen") or f"127.0.0.1:{PORT}"),
            "username": str(raw.get("username") or ""),
            "password": str(raw.get("password") or "")}


def save_settings(changes: Dict[str, Any]) -> Dict[str, Any]:
    s = load_settings()
    if "url" in changes:
        u = urlparse(str(changes["url"] or ""))
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("go2rtc URL must be http(s)://host:port")
        s["url"] = str(changes["url"]).rstrip("/")
    if "listen" in changes:
        listen = str(changes["listen"] or "").strip()
        if not listen or ":" not in listen or any(c in listen for c in " \r\n\"'"):
            raise ValueError("listen must look like 127.0.0.1:1984 or :1984")
        s["listen"] = listen
    for k in ("username", "password"):
        if changes.get(k):
            s[k] = str(changes[k])
    _write_secrets_section("go2rtc", s)
    return s


def ensure_credentials() -> Dict[str, Any]:
    s = load_settings()
    if not s["username"] or not s["password"]:
        s["username"] = s["username"] or "zmm"
        s["password"] = s["password"] or secrets.token_urlsafe(24)
        _write_secrets_section("go2rtc", s)
    return s


def config_yaml(s: Dict[str, Any]) -> str:
    """go2rtc.yaml: API only, behind auth even from localhost."""
    import yaml
    return yaml.safe_dump({
        "api": {"listen": s["listen"], "username": s["username"], "password": s["password"],
                "local_auth": True},
        "rtsp": {"listen": ""},
        "webrtc": {"listen": ""},
        "srtp": {"listen": ""},
        "log": {"level": "warn"},
        # No `streams:` key. go2rtc saves each stream ZMM puts by text-patching
        # this file, and can't patch under an inline `{}` — it answers 400.
        # With no key it appends its own block.
    }, sort_keys=False)


def write_config(s: Optional[Dict[str, Any]] = None) -> Path:
    s = s or ensure_credentials()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / "go2rtc.yaml"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(config_yaml(s))
    return path


# API client

async def _httpx(method: str, url: str, params=None, auth=None, raw: bool = False) -> Tuple[int, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as cx:
        r = await cx.request(method, url, params=params, auth=auth)
    if raw:
        return r.status_code, r.content
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:300]


class Go2rtc:
    def __init__(self, settings: Optional[Dict[str, Any]] = None, http: Optional[HttpFn] = None):
        self.settings = settings or load_settings()
        self._http = http or _httpx

    @property
    def _auth(self):
        s = self.settings
        return (s["username"], s["password"]) if s.get("username") else None

    async def _call(self, method: str, path: str, params=None, raw: bool = False):
        try:
            status, body = await self._http(method, f"{self.settings['url']}{path}",
                                            params=params, auth=self._auth, raw=raw)
        except Exception as e:                            # noqa: BLE001
            raise Go2rtcError(f"go2rtc unreachable at {self.settings['url']} ({type(e).__name__})") from e
        if status == 401:
            raise Go2rtcError("go2rtc refused ZMM's credentials")
        if status >= 400:
            detail = body.decode(errors="replace") if isinstance(body, (bytes, bytearray)) else body
            detail = _mask(str(detail).strip())[:200] if isinstance(detail, str) else ""
            raise Go2rtcError(f"go2rtc answered {status}" + (f": {detail}" if detail else ""), status)
        return body

    async def healthy(self) -> bool:
        try:
            await self._call("GET", "/api")
            return True
        except Go2rtcError:
            return False

    async def streams(self) -> Dict[str, Any]:
        body = await self._call("GET", "/api/streams")
        return body if isinstance(body, dict) else {}

    async def put_stream(self, name: str, src: str) -> None:
        try:
            await self._call("PUT", "/api/streams", params={"name": name, "src": src})
        except Go2rtcError as e:
            # go2rtc creates the stream, then saves it to its config; a failed
            # save is a 400 for a stream that is nonetheless live.
            if e.status != 400 or name not in await self.streams():
                raise
            logger.info("[go2rtc] %s is live but go2rtc couldn't save it: %s", name, e)

    async def restart(self) -> None:
        """Re-read the config ZMM just wrote."""
        await self._call("POST", "/api/restart")

    async def delete_stream(self, name: str) -> None:
        try:
            await self._call("DELETE", "/api/streams", params={"src": name})
        except Go2rtcError as e:
            # Same shape as put: removed from go2rtc, the config save refused.
            if e.status != 400 or name in await self.streams():
                raise

    async def snapshot(self, name: str, width: Optional[int] = None) -> bytes:
        params: Dict[str, Any] = {"src": name}
        if width:
            params["width"] = int(width)
        body = await self._call("GET", "/api/frame.jpeg", params=params, raw=True)
        if not isinstance(body, (bytes, bytearray)) or not body.startswith(b"\xff\xd8"):
            raise Go2rtcError("go2rtc returned no image (is the camera reachable?)")
        return bytes(body)

    def stream_url(self, name: str) -> str:
        """A stream as fMP4 over the authenticated API, credentials included:
        what another local process is given so it shares go2rtc's connection
        to the camera rather than opening its own."""
        from urllib.parse import quote
        u = urlparse(self.settings["url"])
        auth = f"{quote(self._auth[0], safe='')}:{quote(self._auth[1], safe='')}@" if self._auth else ""
        return f"{u.scheme}://{auth}{u.netloc}/api/stream.mp4?src={quote(name, safe='')}"

    def ws_target(self, name: str) -> Tuple[str, Dict[str, str]]:
        """URL and headers for go2rtc's stream websocket."""
        import base64
        from urllib.parse import quote
        u = urlparse(self.settings["url"])
        scheme = "wss" if u.scheme == "https" else "ws"
        headers = {}
        if self._auth:
            token = base64.b64encode(f"{self._auth[0]}:{self._auth[1]}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        return f"{scheme}://{u.netloc}/api/ws?src={quote(name, safe='')}", headers
