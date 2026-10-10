"""
ZMM's side of object detection: tells the vision sidecar which cameras to
watch and turns what it sees into `person` / `vehicle` / `animal` on each
camera device. The ZMM Manager owns the container (manager/vision.py). See
docs/vision.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("vision")

# Kept in step with vision.GROUPS (tests/vision checks it).
OBJECT_GROUPS = ("person", "vehicle", "animal")
URL = os.environ.get("ZMM_VISION_URL", "http://127.0.0.1:8556").rstrip("/")
TOKEN_FILE = Path(os.environ.get("ZMM_VISION_DIR", "./data/vision")) / "token"
POLL_WAIT_S = 25
RETRY_S = 15

HttpFn = Callable[..., Awaitable[Tuple[int, Any]]]


class VisionError(Exception):
    pass


def load_token(path: Path = TOKEN_FILE) -> str:
    """The secret shared with the sidecar; whoever starts first makes it."""
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


async def _httpx(method: str, url: str, token: str, params=None, body=None,
                 raw: bool = False, timeout: float = 10) -> Tuple[int, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as cx:
        r = await cx.request(method, url, params=params, json=body,
                             headers={"Authorization": f"Bearer {token}"})
    if raw:
        return r.status_code, r.content
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {}


class VisionClient:
    def __init__(self, url: str = URL, token: Optional[Callable[[], str]] = None, http: Optional[HttpFn] = None):
        self.url, self._token, self._http = url, token or load_token, http or _httpx

    async def _call(self, method: str, path: str, **kw) -> Any:
        try:
            status, body = await self._http(method, self.url + path, await asyncio.to_thread(self._token), **kw)
        except Exception as e:                            # noqa: BLE001
            raise VisionError(f"detection sidecar unreachable ({type(e).__name__})") from e
        if status >= 400:
            detail = body.get("error") if isinstance(body, dict) else ""
            raise VisionError(f"detection sidecar answered {status}" + (f": {detail}" if detail else ""))
        return body

    async def status(self, after: Optional[int] = None, wait: float = POLL_WAIT_S) -> Dict[str, Any]:
        if after is None:
            return await self._call("GET", "/status")
        return await self._call("GET", "/status", params={"after": after, "wait": wait}, timeout=wait + 10)

    async def configure(self, cameras: List[Dict[str, Any]]) -> str:
        return str((await self._call("PUT", "/config", body={"cameras": cameras})).get("config") or "")

    async def snapshot(self, cid: str, kind: str = "snapshot") -> bytes:
        """`snapshot`: the latest detection, boxes drawn. `frame`: what the
        detector sees now, zones outlined — what zones are drawn on."""
        body = await self._call("GET", f"/{kind}/{cid}.jpg", raw=True)
        if not isinstance(body, (bytes, bytearray)) or not body.startswith(b"\xff\xd8"):
            raise VisionError("nothing detected on this camera yet")
        return bytes(body)


class VisionBridge:
    """Keeps the sidecar's camera list equal to ZMM's and applies its reports."""

    def __init__(self, cameras: Any, client: Optional[VisionClient] = None):
        self.cameras, self.client = cameras, client or VisionClient()
        self.status: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self._version: Optional[int] = None
        self._pushed: Optional[str] = None       # what we last sent
        self._accepted = ""                      # the sidecar's hash for it
        self._task: Optional[asyncio.Task] = None
        self._kick = asyncio.Event()

    def kick(self) -> None:
        """A camera changed: don't sit out the rest of the long poll."""
        self._kick.set()

    async def step(self) -> None:
        """One poll: push the camera list if the sidecar hasn't got ours, then
        apply what it reports."""
        st = await self.client.status(self._version)
        wanted = self.cameras.detect_config()
        key = json.dumps(wanted, sort_keys=True)
        # A restarted sidecar has no config: its hash no longer matches ours.
        if key != self._pushed or st.get("config") != self._accepted:
            self._accepted = await self.client.configure(wanted)
            self._pushed = key
            st = await self.client.status()
        self._version = st.get("version")
        self.status, self.error = st, None
        for cid, cam in (st.get("cameras") or {}).items():
            objects = cam.get("objects") or {}
            # The sidecar's "person:drive" is the device's `person_drive`.
            await self.cameras.apply_objects(cid, {k.replace(":", "_"): bool((objects.get(k) or {}).get("present"))
                                                   for k in objects})

    async def _loop(self) -> None:
        while True:
            try:
                poll = asyncio.create_task(self.step())
                kick = asyncio.create_task(self._kick.wait())
                done, pending = await asyncio.wait({poll, kick}, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                if kick in done:
                    self._kick.clear()
                    self._version = None
                if poll in done:
                    poll.result()
            except asyncio.CancelledError:
                raise
            except VisionError as e:
                if self.error is None:
                    logger.info("[vision] %s", e)
                self.error, self.status, self._version = str(e), None, None
                # Nothing is watching: don't leave a person standing in the hall forever.
                await self.cameras.clear_objects()
                await self._sleep(RETRY_S)
            except Exception as e:                        # noqa: BLE001
                logger.error("[vision] loop failed: %s", e)
                await self._sleep(RETRY_S)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._kick.wait(), timeout=seconds)
            self._kick.clear()
        except asyncio.TimeoutError:
            pass

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def public(self) -> Dict[str, Any]:
        st = self.status or {}
        return {"reachable": self.status is not None, "error": self.error,
                "ready": bool(st.get("ready")), "backend": st.get("backend"), "wanted": st.get("wanted"),
                "note": st.get("note"), "inference_ms": st.get("inference_ms"),
                "cameras": {cid: {k: c.get(k) for k in ("online", "error", "frames", "looks", "last_at")}
                            for cid, c in (st.get("cameras") or {}).items()}}


_bridge: Optional[VisionBridge] = None


def get_vision_bridge() -> Optional[VisionBridge]:
    return _bridge


def set_vision_bridge(b: Optional[VisionBridge]) -> None:
    global _bridge
    _bridge = b
