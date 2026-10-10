"""
Cameras: the registry, their go2rtc streams, snapshots, and ONVIF motion as a
device signal. See docs/cameras.md.

The registry (data/cameras.json) never holds credentials; they live in
config/secrets.yaml under `cameras` and are joined to the URL only when a
stream is handed to go2rtc. A camera is an engine device, `camera::<id>`, whose
`motion`/`occupancy` attributes feed rules, notification rules and alarm zones
like any motion sensor. With detection on (docs/vision.md) it also carries
`person`, `vehicle` and `animal`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import quote, unquote, urlparse, urlunparse

from modules.go2rtc import Go2rtc, Go2rtcError
from modules.vision import OBJECT_GROUPS

logger = logging.getLogger("cameras")

DATA_PATH = Path("./data/cameras.json")
SECRETS_FILE = os.environ.get("ZMM_SECRETS_FILE", "./config/secrets.yaml")
IEEE_PREFIX = "camera::"
STREAM_PREFIX = "zmm_"
# A camera's smaller stream, when it has one set for detection. Its own
# prefix: a camera id may itself end in anything.
DETECT_PREFIX = "zmmd_"
# go2rtc also takes exec:, ffmpeg: and other sources that run commands.
SCHEMES = ("rtsp", "rtsps", "http", "https")
MAX_CAMERAS = 64
RECONCILE_S = 60
SNAPSHOT_CACHE_S = 2.0
# Cameras that only ever send "motion on" are cleared after this long quiet.
MOTION_HOLD_S = 120
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
RESERVED_IDS = ("go2rtc", "discover", "probe", "vision")      # fixed API paths under /api/cameras/


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:32] or "camera"


# Credentials

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


def _write_credentials(creds: Dict[str, Dict[str, str]]) -> None:
    import yaml
    path = Path(SECRETS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_secrets()
    existing["cameras"] = creds
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        yaml.dump(existing, fh, default_flow_style=False, sort_keys=False)
    os.chmod(path, 0o600)


def split_url(url: str) -> Dict[str, str]:
    """A stream URL with any user:pass@ lifted out, and the scheme checked."""
    url = (url or "").strip()
    if any(c in url for c in "\r\n "):
        raise ValueError("The stream URL can't contain spaces or line breaks")
    u = urlparse(url)
    if u.scheme.lower() not in SCHEMES or not u.hostname:
        raise ValueError(f"The stream URL must start with one of {', '.join(s + '://' for s in SCHEMES)}")
    host = u.hostname if ":" not in u.hostname else f"[{u.hostname}]"
    netloc = f"{host}:{u.port}" if u.port else host
    return {"url": urlunparse(u._replace(netloc=netloc)),
            "username": unquote(u.username or ""), "password": unquote(u.password or "")}


def join_url(url: str, username: str, password: str) -> str:
    if not username:
        return url
    u = urlparse(url)
    auth = quote(username, safe="") + (":" + quote(password, safe="") if password else "")
    return urlunparse(u._replace(netloc=f"{auth}@{u.netloc}"))


# Devices

class _NoCaps:
    def has_capability(self, cap: str) -> bool:
        return False

    def get_capabilities(self) -> List[str]:
        return []


class CameraDevice:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.ieee = IEEE_PREFIX + cfg["id"]
        self.friendly_name = cfg["name"]
        self.manufacturer = "Camera"
        self.model = cfg.get("model") or "IP camera"
        self.capabilities = _NoCaps()
        self.last_seen = 0.0
        # occupancy mirrors motion: it's the key motion sensors report, so
        # rules and alarm zones written for them work unchanged.
        self.state: Dict[str, Any] = {"motion": False, "occupancy": False, "available": True}
        self.online: Optional[bool] = None
        self.motion_at = 0.0
        self.sync_objects()

    def sync_objects(self) -> None:
        """Object keys exist only for what the camera is asked to detect, so
        rule pickers don't offer signals that can never fire."""
        d = self.cfg.get("detect") or {}
        want = d.get("labels", []) if d.get("enabled") else []
        for g in OBJECT_GROUPS:
            if g in want:
                self.state.setdefault(g, False)
            else:
                self.state.pop(g, None)

    def is_available(self) -> bool:
        return self.online is not False

    def get_control_commands(self) -> List[Dict[str, Any]]:
        return []


class CameraManager:
    def __init__(self, path: Path = DATA_PATH, go2rtc: Optional[Go2rtc] = None,
                 evaluate: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
                 onvif_factory: Optional[Callable[..., Any]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.path = path
        self.go2rtc = go2rtc or Go2rtc()
        self._evaluate = evaluate
        self._onvif_factory = onvif_factory
        self._clock = clock
        self.cameras: Dict[str, Dict[str, Any]] = {}
        self.devices: Dict[str, CameraDevice] = {}
        self._snap_cache: Dict[str, tuple] = {}
        self._watchers: Dict[str, asyncio.Task] = {}
        self._task: Optional[asyncio.Task] = None
        self.last_error: Optional[str] = None
        self.on_change: Optional[Callable[[], None]] = None
        self.load()

    # Storage
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.error("[cameras] unreadable %s: %s", self.path, e)
            return
        for c in raw.get("cameras") or []:
            if isinstance(c, dict) and _ID_RE.match(str(c.get("id") or "")):
                self.cameras[c["id"]] = c
                self.devices[c["id"]] = CameraDevice(c)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"cameras": list(self.cameras.values())}, indent=1))
        os.replace(tmp, self.path)

    def _creds(self) -> Dict[str, Dict[str, str]]:
        return dict(_read_secrets().get("cameras") or {})

    def public(self, cam: Dict[str, Any]) -> Dict[str, Any]:
        dev = self.devices.get(cam["id"])
        creds = self._creds().get(cam["id"]) or {}
        return {**cam, "has_credentials": bool(creds.get("username")),
                "username": creds.get("username", ""),
                "online": dev.online if dev else None,
                "motion": bool(dev and dev.state["motion"]),
                "objects": {g: dev.state[g] for g in OBJECT_GROUPS if dev and g in dev.state},
                "ieee": IEEE_PREFIX + cam["id"]}

    def list(self) -> List[Dict[str, Any]]:
        return [self.public(c) for c in sorted(self.cameras.values(), key=lambda c: c["name"].lower())]

    # Editing (admin)
    def _normalise(self, data: Dict[str, Any], current: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        cam = dict(current or {})
        if current is None:
            cid = str(data.get("id") or _slug(str(data.get("name") or ""))).lower()
            if not _ID_RE.match(cid) or cid in RESERVED_IDS:
                raise ValueError("Id: lower-case letters, digits, '-' and '_' (not go2rtc, discover, probe or vision)")
            if cid in self.cameras:
                raise ValueError(f"A camera '{cid}' already exists")
            if len(self.cameras) >= MAX_CAMERAS:
                raise ValueError(f"At most {MAX_CAMERAS} cameras")
            cam["id"] = cid
        if "name" in data or current is None:
            name = str(data.get("name") or "").strip()
            if not name or len(name) > 60:
                raise ValueError("A camera needs a name (up to 60 characters)")
            cam["name"] = name
        if "url" in data or current is None:
            cam["url"] = split_url(str(data.get("url") or ""))["url"]
        if "onvif" in data:
            o = data.get("onvif") or None
            if o:
                host = str(o.get("host") or "").strip()
                if not host or any(c in host for c in "/@ \r\n"):
                    raise ValueError("ONVIF host must be a hostname or IP")
                port = int(o.get("port") or 80)
                if not 1 <= port <= 65535:
                    raise ValueError("ONVIF port must be 1-65535")
                o = {"host": host, "port": port}
            cam["onvif"] = o
        cam.setdefault("onvif", None)
        if "motion" in data or current is None:
            cam["motion"] = bool(data.get("motion")) and bool(cam.get("onvif"))
        if "enabled" in data or current is None:
            cam["enabled"] = bool(data.get("enabled", True))
        if "model" in data:
            cam["model"] = str(data.get("model") or "")[:60]
        if "detect" in data:
            d = data.get("detect") or {}
            labels = [g for g in OBJECT_GROUPS if g in (d.get("labels") or OBJECT_GROUPS)]
            try:
                threshold = float(d.get("threshold") or 0.5)
            except (TypeError, ValueError):
                raise ValueError("Detection confidence must be a number")
            if not 0.3 <= threshold <= 0.95:
                raise ValueError("Detection confidence must be between 30% and 95%")
            # A second, smaller stream of the same camera; it uses the same login.
            url = split_url(str(d["url"]))["url"] if d.get("url") else ""
            cam["detect"] = {"enabled": bool(d.get("enabled")) and bool(labels), "labels": labels,
                             "threshold": round(threshold, 2), "url": url}
        return cam

    def _set_creds(self, cid: str, data: Dict[str, Any], url_creds: Dict[str, str]) -> None:
        """A user:pass in the pasted URL counts; a blank password keeps the stored one."""
        creds = self._creds()
        cur = creds.get(cid) or {}
        username = str(data.get("username") or url_creds.get("username") or cur.get("username") or "")
        password = str(data.get("password") or url_creds.get("password") or "")
        if not password and username == cur.get("username"):
            password = cur.get("password", "")
        if data.get("clear_credentials"):
            username = password = ""
        for v in (username, password):
            if "\r" in v or "\n" in v:
                raise ValueError("Credentials must be a single line")
        if username:
            creds[cid] = {"username": username, "password": password}
        else:
            creds.pop(cid, None)
        _write_credentials(creds)

    async def add(self, data: Dict[str, Any]) -> Dict[str, Any]:
        cam = self._normalise(data, None)
        self._set_creds(cam["id"], data, split_url(str(data.get("url") or "")))
        self.cameras[cam["id"]] = cam
        self.devices[cam["id"]] = CameraDevice(cam)
        self._save()
        await self._push(cam)
        self._restart_watcher(cam["id"])
        self._changed()
        return self.public(cam)

    async def update(self, cid: str, data: Dict[str, Any]) -> Dict[str, Any]:
        cur = self.cameras.get(cid)
        if cur is None:
            raise KeyError(cid)
        cam = self._normalise(data, cur)
        url_creds = split_url(str(data["url"])) if data.get("url") else {}
        self._set_creds(cid, data, url_creds)
        self.cameras[cid] = cam
        dev = self.devices[cid]
        dev.cfg, dev.friendly_name = cam, cam["name"]
        dev.sync_objects()
        self._save()
        await self._push(cam)
        self._restart_watcher(cid)
        self._changed()
        return self.public(cam)

    async def delete(self, cid: str) -> bool:
        if self.cameras.pop(cid, None) is None:
            return False
        self.devices.pop(cid, None)
        self._stop_watcher(cid)
        creds = self._creds()
        if creds.pop(cid, None) is not None:
            _write_credentials(creds)
        self._save()
        self._changed()
        try:
            for prefix in (STREAM_PREFIX, DETECT_PREFIX):
                await self.go2rtc.delete_stream(prefix + cid)
        except Go2rtcError as e:
            logger.info("[cameras] go2rtc delete of %s: %s", cid, e)
        return True

    # go2rtc
    def source(self, cid: str) -> str:
        cam = self.cameras[cid]
        c = self._creds().get(cid) or {}
        return join_url(cam["url"], c.get("username", ""), c.get("password", ""))

    def _wanted(self, cam: Dict[str, Any]) -> Dict[str, str]:
        """The go2rtc streams a camera should have: name -> source."""
        if not cam.get("enabled", True):
            return {}
        cid = cam["id"]
        out = {STREAM_PREFIX + cid: self.source(cid)}
        d = cam.get("detect") or {}
        if d.get("enabled") and d.get("url"):
            c = self._creds().get(cid) or {}
            out[DETECT_PREFIX + cid] = join_url(d["url"], c.get("username", ""), c.get("password", ""))
        return out

    async def _push(self, cam: Dict[str, Any]) -> None:
        want = self._wanted(cam)
        try:
            for name in (STREAM_PREFIX + cam["id"], DETECT_PREFIX + cam["id"]):
                if name in want:
                    await self.go2rtc.put_stream(name, want[name])
                else:
                    await self.go2rtc.delete_stream(name)
            self.last_error = None
        except Go2rtcError as e:
            self.last_error = str(e)
            logger.warning("[cameras] go2rtc: %s", e)

    async def reconcile(self) -> None:
        """go2rtc forgets API-added streams when it restarts; put them back,
        and drop ours that no longer exist."""
        try:
            have = await self.go2rtc.streams()
        except Go2rtcError as e:
            self.last_error = str(e)
            return
        self.last_error = None
        want: Dict[str, str] = {}
        for cam in self.cameras.values():
            want.update(self._wanted(cam))
        try:
            for name, src in want.items():
                if name not in have:
                    await self.go2rtc.put_stream(name, src)
        except Go2rtcError as e:
            self.last_error = str(e)
            logger.warning("[cameras] go2rtc: %s", e)
        for name in have:
            if name.startswith((STREAM_PREFIX, DETECT_PREFIX)) and name not in want:
                try:
                    await self.go2rtc.delete_stream(name)
                except Go2rtcError:
                    pass

    async def snapshot(self, cid: str) -> bytes:
        if cid not in self.cameras:
            raise KeyError(cid)
        hit = self._snap_cache.get(cid)
        if hit and self._clock() - hit[0] < SNAPSHOT_CACHE_S:
            return hit[1]
        dev = self.devices.get(cid)
        try:
            img = await self.go2rtc.snapshot(STREAM_PREFIX + cid)
        except Go2rtcError:
            if dev:
                dev.online = False
            raise
        if dev:
            dev.online = True
        self._snap_cache[cid] = (self._clock(), img)
        return img

    # Engine and device list
    def automation_devices(self) -> Dict[str, CameraDevice]:
        return {d.ieee: d for cid, d in self.devices.items() if self.cameras[cid].get("enabled", True)}

    def device_entries(self) -> List[Dict[str, Any]]:
        out = []
        for cid, cam in self.cameras.items():
            dev = self.devices[cid]
            out.append({
                "ieee": dev.ieee, "camera_id": cid, "friendly_name": cam["name"],
                "type": "Camera", "protocol": "wifi", "manufacturer": "Camera",
                "model": cam.get("model") or "IP camera",
                "available": dev.online if dev.online is not None else None,
                "state": {k: v for k, v in dev.state.items() if k != "available"},
            })
        return out

    async def _motion(self, cid: str, on: bool) -> None:
        dev = self.devices.get(cid)
        if dev is None:
            return
        if on:
            dev.motion_at = self._clock()
        if dev.state["motion"] == on:
            return
        dev.state.update(motion=on, occupancy=on)
        dev.last_seen = time.time()
        if self._evaluate:
            try:
                await self._evaluate(dev.ieee, {"motion": on, "occupancy": on})
            except Exception as e:                        # noqa: BLE001
                logger.warning("[cameras] evaluating %s failed: %s", dev.ieee, e)

    # Object detection (docs/vision.md): the sidecar reads go2rtc's copy of
    # the stream, so the camera is connected to once and its login never
    # leaves ZMM and go2rtc.
    def _changed(self) -> None:
        if self.on_change:
            self.on_change()

    def detect_config(self) -> List[Dict[str, Any]]:
        out = []
        for cid, cam in sorted(self.cameras.items()):
            d = cam.get("detect") or {}
            if not cam.get("enabled", True) or not d.get("enabled"):
                continue
            name = (DETECT_PREFIX if d.get("url") else STREAM_PREFIX) + cid
            out.append({"id": cid, "labels": list(d.get("labels") or []), "threshold": d.get("threshold", 0.5),
                        "url": self.go2rtc.stream_url(name)})
        return out

    async def apply_objects(self, cid: str, objects: Dict[str, bool]) -> None:
        dev = self.devices.get(cid)
        if dev is None:
            return
        changed = {g: on for g, on in objects.items() if g in dev.state and dev.state[g] != on}
        if not changed:
            return
        dev.state.update(changed)
        dev.last_seen = time.time()
        if self._evaluate:
            try:
                await self._evaluate(dev.ieee, changed)
            except Exception as e:                        # noqa: BLE001
                logger.warning("[cameras] evaluating %s failed: %s", dev.ieee, e)

    async def clear_objects(self) -> None:
        for cid in list(self.devices):
            await self.apply_objects(cid, {g: False for g in OBJECT_GROUPS})

    async def clear_stale_motion(self) -> None:
        for cid, dev in list(self.devices.items()):
            if dev.state["motion"] and self._clock() - dev.motion_at > MOTION_HOLD_S:
                await self._motion(cid, False)

    # Lifecycle
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        for cid in self.cameras:
            self._restart_watcher(cid)

    async def stop(self) -> None:
        for cid in list(self._watchers):
            self._stop_watcher(cid)
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.reconcile()
                await self.clear_stale_motion()
                await asyncio.sleep(RECONCILE_S)
            except asyncio.CancelledError:
                break
            except Exception as e:                        # noqa: BLE001
                logger.error("[cameras] loop failed: %s", e)
                await asyncio.sleep(RECONCILE_S)

    def _stop_watcher(self, cid: str) -> None:
        t = self._watchers.pop(cid, None)
        if t and not t.done():
            t.cancel()

    def _restart_watcher(self, cid: str) -> None:
        self._stop_watcher(cid)
        cam = self.cameras.get(cid)
        if not cam or not cam.get("enabled", True) or not cam.get("motion") or not cam.get("onvif"):
            return
        try:
            self._watchers[cid] = asyncio.get_running_loop().create_task(self._watch(cid))
        except RuntimeError:
            pass

    def _onvif(self, cid: str):
        from modules.onvif import OnvifCamera
        cam = self.cameras[cid]
        c = self._creds().get(cid) or {}
        factory = self._onvif_factory or OnvifCamera
        return factory(cam["onvif"]["host"], cam["onvif"]["port"],
                       c.get("username", ""), c.get("password", ""))

    async def _watch(self, cid: str) -> None:
        """Pull-point loop: subscribe, pull, renew; back off and resubscribe on errors."""
        backoff = 5
        while cid in self.cameras:
            cam_onvif = self._onvif(cid)
            address = None
            try:
                address = await cam_onvif.subscribe()
                dev = self.devices.get(cid)
                if dev:
                    dev.online = True
                backoff = 5
                renewed = self._clock()
                while True:
                    for ev in await cam_onvif.pull(address, wait_s=10):
                        await self._motion(cid, ev["motion"])
                    if self._clock() - renewed > 60:
                        await cam_onvif.renew(address)
                        renewed = self._clock()
            except asyncio.CancelledError:
                if address:
                    await cam_onvif.unsubscribe(address)
                raise
            except Exception as e:                        # noqa: BLE001
                dev = self.devices.get(cid)
                if dev:
                    dev.online = False
                logger.info("[cameras] %s motion events: %s (retry in %ss)", cid, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


_manager: Optional[CameraManager] = None


def get_camera_manager() -> Optional[CameraManager]:
    return _manager


def set_camera_manager(m: Optional[CameraManager]) -> None:
    global _manager
    _manager = m
