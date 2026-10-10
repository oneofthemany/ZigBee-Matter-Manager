"""
go2rtc: the streaming sidecar that turns camera RTSP into browser video.
ZMM writes its config, installs the container over the host's container socket,
and drives it through its HTTP API. Browsers never reach go2rtc — ZMM proxies
snapshots and streams behind its own auth. See docs/cameras.md §go2rtc.

The config shuts every go2rtc server but the API: its RTSP server would
otherwise re-publish every camera, unauthenticated, on :8554.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("go2rtc")

SECRETS_FILE = os.environ.get("ZMM_SECRETS_FILE", "./config/secrets.yaml")
CONFIG_DIR = Path(os.environ.get("ZMM_GO2RTC_DIR", "./data/go2rtc"))
APP_DATA = "/app/data"             # the app's data mount, whose host source the sidecar shares
IMAGE = "docker.io/alexxit/go2rtc:1.9.14"
APP_CONTAINER = os.environ.get("ZMM_CONTAINER_NAME", "zigbee-matter-manager")
CONTAINER = f"{APP_CONTAINER}-go2rtc"
PORT = 1984
TIMEOUT_S = 10

HttpFn = Callable[..., Awaitable[Tuple[int, Any]]]


class Go2rtcError(Exception):
    pass


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
        "streams": {},          # ZMM puts its cameras through the API
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
            raise Go2rtcError(f"go2rtc answered {status}")
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
        await self._call("PUT", "/api/streams", params={"name": name, "src": src})

    async def delete_stream(self, name: str) -> None:
        await self._call("DELETE", "/api/streams", params={"src": name})

    async def snapshot(self, name: str, width: Optional[int] = None) -> bytes:
        params: Dict[str, Any] = {"src": name}
        if width:
            params["width"] = int(width)
        body = await self._call("GET", "/api/frame.jpeg", params=params, raw=True)
        if not isinstance(body, (bytes, bytearray)) or not body.startswith(b"\xff\xd8"):
            raise Go2rtcError("go2rtc returned no image (is the camera reachable?)")
        return bytes(body)

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


# Sidecar lifecycle over the host's container socket

def _socket() -> Optional[str]:
    from modules.ollama_manager import detect_container_socket
    return detect_container_socket()


async def sidecar_status() -> Dict[str, Any]:
    sock = _socket()
    out: Dict[str, Any] = {"socket": bool(sock), "installed": False, "running": False,
                           "container": CONTAINER, "image": IMAGE}
    if not sock:
        return out
    try:
        import httpx
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=sock),
                                     base_url="http://d", timeout=5) as cx:
            r = await cx.get(f"/containers/{CONTAINER}/json")
            if r.status_code == 200:
                st = r.json().get("State") or {}
                out.update(installed=True, running=bool(st.get("Running")),
                           image=(r.json().get("Config") or {}).get("Image") or IMAGE)
    except Exception as e:                                # noqa: BLE001
        out["error"] = str(e)
    return out


def _data_source(app_info: Dict[str, Any]) -> Optional[str]:
    for m in app_info.get("Mounts") or []:
        if m.get("Destination") == APP_DATA and m.get("Source"):
            return m["Source"]
    return None


async def install() -> Dict[str, Any]:
    """Pull, create and start the sidecar; start it if it already exists."""
    sock = _socket()
    if not sock:
        raise Go2rtcError("No container socket is mounted into ZMM. Mount the podman or docker "
                          "socket, or run go2rtc yourself (docs/cameras.md §Running go2rtc yourself).")
    import httpx
    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=sock),
                                 base_url="http://d", timeout=None) as cx:
        r = await cx.get(f"/containers/{CONTAINER}/json")
        if r.status_code == 200:
            await cx.post(f"/containers/{CONTAINER}/start")
            return await sidecar_status()

        r = await cx.get(f"/containers/{APP_CONTAINER}/json")
        if r.status_code != 200:
            raise Go2rtcError(f"Can't inspect the app container '{APP_CONTAINER}' to find its data folder")
        src = _data_source(r.json())
        if not src:
            raise Go2rtcError("The app container has no /app/data mount to share (running from source?)")

        engine = ""
        v = await cx.get("/version")
        if v.status_code == 200:
            engine = " ".join(c.get("Name", "") for c in v.json().get("Components") or [])
        # Podman runs ZMM in a host-network pod, so loopback reaches the
        # sidecar; elsewhere it has to listen beyond it (docs/cameras.md).
        if "podman" not in engine.lower():
            save_settings({"listen": f":{PORT}", "url": f"http://host.docker.internal:{PORT}"})
        write_config()

        async with cx.stream("POST", "/images/create", params={"fromImage": IMAGE}) as resp:
            if resp.status_code >= 400:
                raise Go2rtcError(f"pulling {IMAGE} failed: {resp.status_code}")
            async for _ in resp.aiter_lines():
                pass
        cfg = {
            "Image": IMAGE,
            "HostConfig": {
                "NetworkMode": "host",
                "Binds": [f"{src}/go2rtc:/config:rw"],
                "RestartPolicy": {"Name": "unless-stopped"},
                "SecurityOpt": ["label=disable"],
            },
        }
        r = await cx.post("/containers/create", params={"name": CONTAINER}, json=cfg)
        if r.status_code != 201:
            raise Go2rtcError(f"creating the container failed: {r.status_code} {r.text[:200]}")
        r = await cx.post(f"/containers/{CONTAINER}/start")
        if r.status_code not in (204, 304):
            raise Go2rtcError(f"starting the container failed: {r.status_code} {r.text[:200]}")
    logger.info("[go2rtc] sidecar installed from %s", IMAGE)
    return await sidecar_status()


async def start_if_installed() -> None:
    """At app start: on podman, `unless-stopped` doesn't survive a reboot, and
    the app's own service is what comes back — so it brings go2rtc with it."""
    st = await sidecar_status()
    if st["installed"] and not st["running"]:
        try:
            await install()
        except Exception as e:                            # noqa: BLE001
            logger.warning("[go2rtc] could not start the sidecar: %s", e)


async def restart_sidecar() -> None:
    sock = _socket()
    if not sock:
        return
    import httpx
    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=sock),
                                 base_url="http://d", timeout=30) as cx:
        await cx.post(f"/containers/{CONTAINER}/restart")
    await asyncio.sleep(1)
