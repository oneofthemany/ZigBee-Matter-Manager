"""
ZMM's side of camera recordings. The recorder sidecar (recorder/, owned by the
ZMM Manager) does the recording, so it carries on while this app restarts; the
app tells it which cameras to record, forwards their motion and detection
signals so it can cut clips, and reads the recordings folder to list and play
them. See docs/recordings.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from recorder.server import load_token
from recorder.store import ROOT, Store

logger = logging.getLogger("recordings")

URL = os.environ.get("ZMM_RECORDER_URL", "http://127.0.0.1:8557").rstrip("/")
CHECK_S = 15
CLIP_CACHE_S = 5

HttpFn = Callable[..., Awaitable[Tuple[int, Any]]]


class RecorderError(Exception):
    pass


async def _httpx(method: str, url: str, token: str, body=None, content=None) -> Tuple[int, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=10) as cx:
        r = await cx.request(method, url, json=body, content=content,
                             headers={"Authorization": f"Bearer {token}"})
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {}


class RecorderClient:
    def __init__(self, url: str = URL, token: Optional[Callable[[], str]] = None, http: Optional[HttpFn] = None):
        self.url, self._http = url, http or _httpx
        self._token = token or (lambda: load_token(ROOT / "token"))

    async def _call(self, method: str, path: str, **kw) -> Any:
        try:
            status, body = await self._http(method, self.url + path, await asyncio.to_thread(self._token), **kw)
        except Exception as e:                            # noqa: BLE001
            raise RecorderError(f"recorder unreachable ({type(e).__name__})") from e
        if status >= 400:
            detail = body.get("error") if isinstance(body, dict) else ""
            raise RecorderError(f"recorder answered {status}" + (f": {detail}" if detail else ""))
        return body

    async def status(self) -> Dict[str, Any]:
        return await self._call("GET", "/status")

    async def configure(self, cameras: List[Dict[str, Any]]) -> str:
        return str((await self._call("PUT", "/config", body={"cameras": cameras})).get("config") or "")

    async def signal(self, cid: str, state: Dict[str, Any]) -> bool:
        return bool((await self._call("POST", "/signal", body={"camera": cid, "state": state})).get("started"))

    async def thumb(self, cid: str, jpeg: bytes) -> None:
        await self._call("PUT", f"/thumb/{cid}", content=jpeg)


class RecorderBridge:
    """Keeps the sidecar's camera list equal to ZMM's and passes signals on."""

    def __init__(self, cameras: Any, client: Optional[RecorderClient] = None, store: Optional[Store] = None):
        self.cameras, self.client, self.store = cameras, client or RecorderClient(), store or Store()
        self.status: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self._pushed: Optional[str] = None
        self._accepted = ""
        self._task: Optional[asyncio.Task] = None
        self._kick = asyncio.Event()
        self._clips: Tuple[float, List[Dict[str, Any]]] = (0.0, [])

    def wanted(self) -> List[Dict[str, Any]]:
        from modules.cameras import STREAM_PREFIX
        out = []
        for cid, cam in sorted(self.cameras.cameras.items()):
            rec = cam.get("record") or {}
            if cam.get("enabled", True) and rec.get("mode", "off") != "off":
                # go2rtc's copy of the stream: still one connection to the camera.
                out.append({"id": cid, "url": self.cameras.go2rtc.stream_url(STREAM_PREFIX + cid), "record": rec})
        return out

    def kick(self) -> None:
        self._kick.set()

    async def step(self) -> None:
        st = await self.client.status()
        wanted = self.wanted()
        key = json.dumps(wanted, sort_keys=True)
        if key != self._pushed or st.get("config") != self._accepted:
            self._accepted = await self.client.configure(wanted)
            self._pushed = key
            st = await self.client.status()
        self.status, self.error = st, None

    def signal(self, cid: str, state: Dict[str, Any]) -> None:
        """A camera's signals moved. Fire and forget: a recorder that is down
        must never hold up the rule engine."""
        rec = (self.cameras.cameras.get(cid) or {}).get("record") or {}
        if rec.get("mode", "off") == "off":
            return
        keys = {k: bool(state.get(k)) for k in rec.get("events") or []}

        async def send():
            try:
                if await self.client.signal(cid, keys):
                    snap = getattr(self.cameras, "detection_snapshot", None)
                    if snap:
                        await self.client.thumb(cid, await asyncio.wait_for(snap(cid), 5))
            except Exception as e:                        # noqa: BLE001
                logger.debug("[recordings] signal for %s not delivered: %s", cid, e)
        try:
            asyncio.get_running_loop().create_task(send())
        except RuntimeError:
            pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except RecorderError as e:
                if self.error is None and self.wanted():
                    logger.info("[recordings] %s", e)
                self.error, self.status = str(e), None
            except Exception as e:                        # noqa: BLE001
                logger.error("[recordings] loop failed: %s", e)
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=CHECK_S)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    # The folder, read directly: both containers mount it.
    async def clips(self) -> List[Dict[str, Any]]:
        at, clips = self._clips
        if time.monotonic() - at > CLIP_CACHE_S:
            clips = await asyncio.to_thread(self.store.clips)
            self._clips = (time.monotonic(), clips)
        return clips

    async def delete_clip(self, cam: str, cid: str) -> bool:
        ok = await asyncio.to_thread(self.store.delete_clip, cam, cid)
        self._clips = (0.0, [])
        return ok

    def public(self) -> Dict[str, Any]:
        st = self.status or {}
        return {"reachable": self.status is not None, "error": self.error,
                "wanted": [c["id"] for c in self.wanted()], "cameras": st.get("cameras") or {}}


_bridge: Optional[RecorderBridge] = None


def get_recorder() -> Optional[RecorderBridge]:
    return _bridge


def set_recorder(b: Optional[RecorderBridge]) -> None:
    global _bridge
    _bridge = b
