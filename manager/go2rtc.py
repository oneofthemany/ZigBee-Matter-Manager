"""
go2rtc sidecar lifecycle, driven from the manager like Beekeeper and Ollama.
The app writes go2rtc's config (API credentials, servers off) into
data/go2rtc/ and talks to its API; the manager owns the container. See
docs/cameras.md §go2rtc.

Standalone by design: the manager never imports from modules/. IMAGE is kept
in step with modules/go2rtc.IMAGE by convention.
"""
import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

from manager import containers

logger = logging.getLogger("manager.go2rtc")

IMAGE = "docker.io/alexxit/go2rtc:1.9.14"
CONTAINER = f"{containers.APP_CONTAINER}-go2rtc"
_DATA_DIR = os.environ.get("ZMM_DATA_DIR") or os.environ.get("DATA_DIR") \
    or "/opt/.zigbee-matter-manager"
_DIR = os.path.join(_DATA_DIR, "data", "go2rtc")
CONFIG = os.path.join(_DIR, "go2rtc.yaml")
# Whether the user wants it running — so the watchdog can tell a stop someone
# asked for from a container that died or didn't come back after a reboot.
_STATE = os.path.join(_DIR, "manager.json")
# Boot-time service, written on the host by scripts/sidecar_service.sh go2rtc.
_SVC_TRIGGER = os.path.join(_DIR, "service_action")
_SVC_STATUS = os.path.join(_DIR, "service_status.json")
_MAX_LOG = 200

_job: Optional[Dict[str, Any]] = None


def enabled() -> bool:
    try:
        with open(_STATE) as f:
            return bool(json.load(f).get("enabled"))
    except (OSError, ValueError):
        return False


def _set_enabled(on: bool) -> None:
    os.makedirs(_DIR, exist_ok=True)
    with open(_STATE, "w") as f:
        json.dump({"enabled": on, "at": time.time()}, f)


def service_status() -> Dict[str, Any]:
    """The host helper's last report on go2rtc's boot-time service (never raises)."""
    try:
        with open(_SVC_STATUS) as f:
            data = json.load(f)
        return {"known": True, **data, "pending": os.path.isfile(_SVC_TRIGGER)}
    except (OSError, ValueError):
        return {"known": False, "installed": False, "pending": os.path.isfile(_SVC_TRIGGER),
                "detail": "not checked yet — needs the host helper (install_watcher.sh)"}


def request_service(action: str) -> Dict[str, Any]:
    """Ask the host helper to install, remove or re-check the boot-time service."""
    if action not in ("install", "remove", "check"):
        return {"success": False, "error": "action must be install|remove|check"}
    try:
        os.makedirs(_DIR, exist_ok=True)
        with open(_SVC_TRIGGER, "w") as f:
            f.write(action)
        return {"success": True, "message": f"Autostart {action} requested"}
    except OSError as e:
        return {"success": False, "error": str(e)}


def busy() -> bool:
    return bool(_job and _job.get("status") == "running")


def _log(line: str) -> None:
    if _job is not None and line:
        _job["log"] = (_job["log"] + [line])[-_MAX_LOG:]


def _client(sock: str):
    import httpx
    return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=sock),
                             base_url="http://d", timeout=None)


async def _inspect(cx, name: str) -> Optional[Dict[str, Any]]:
    try:
        r = await cx.get(f"/containers/{name}/json")
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.debug("inspect %s failed: %s", name, e)
        return None


async def status() -> Dict[str, Any]:
    """Container state for the card and /status. Never raises."""
    out: Dict[str, Any] = {"available": False, "installed": False, "running": False,
                           "enabled": enabled(), "name": CONTAINER, "image": IMAGE,
                           "config": os.path.isfile(CONFIG), "state": None,
                           "service": service_status(),
                           "job": dict(_job) if _job else None, "error": None}
    sock = containers.detect_socket()
    if not sock:
        out["error"] = "no container socket mounted"
        return out
    out["available"] = True
    try:
        async with _client(sock) as cx:
            info = await _inspect(cx, CONTAINER)
    except Exception as e:
        out["error"] = str(e)
        return out
    if info:
        st = info.get("State") or {}
        out.update(installed=True, running=bool(st.get("Running")), state=st.get("Status"),
                   image=(info.get("Config") or {}).get("Image") or IMAGE)
    return out


def start_enable() -> Tuple[bool, str]:
    global _job
    if busy():
        return False, "A go2rtc job is already running."
    if not os.path.isfile(CONFIG):
        return False, ("go2rtc's config isn't there yet — it's written by ZMM when it "
                       "starts. Restart ZMM on this version, then enable again.")
    _set_enabled(True)
    # Busy from now, not from when the task first runs, so a second click or a
    # watchdog tick in between can't start another.
    _job = {"action": "enable", "status": "running", "log": [], "started": time.time()}
    asyncio.create_task(_run_enable())
    return True, "Enabling go2rtc…"


def _data_source(app_info: Dict[str, Any]) -> Optional[str]:
    for m in app_info.get("Mounts") or []:
        if m.get("Destination") == "/app/data" and m.get("Source"):
            return m["Source"]
    return None


async def _run_enable() -> None:
    """Start the sidecar, creating it — or recreating it on a new pinned image."""
    sock = containers.detect_socket()
    if not sock:
        _job.update(status="error", finished=time.time())
        _log("no container socket mounted")
        return
    try:
        async with _client(sock) as cx:
            existing = await _inspect(cx, CONTAINER)
            if existing and (existing.get("Config") or {}).get("Image") == IMAGE:
                r = await cx.post(f"/containers/{CONTAINER}/start")
                ok = r.status_code in (204, 304)
                _log("Started." if ok else f"start failed: {r.status_code} {r.text[:200]}")
                if ok:
                    request_service("install")
                _job.update(status="done" if ok else "error", finished=time.time())
                return
            if existing:
                _log(f"Moving to {IMAGE}…")
                await cx.post(f"/containers/{CONTAINER}/stop", params={"t": "10"})
                await cx.delete(f"/containers/{CONTAINER}", params={"force": "true"})

            app_info = await _inspect(cx, containers.APP_CONTAINER)
            src = _data_source(app_info or {})
            if not src:
                raise RuntimeError(f"can't find the data folder of '{containers.APP_CONTAINER}'")

            _log(f"Pulling {IMAGE}…")
            async with cx.stream("POST", "/images/create", params={"fromImage": IMAGE}) as resp:
                if resp.status_code >= 400:
                    raise RuntimeError(f"image pull failed: {resp.status_code}")
                last = None
                async for line in resp.aiter_lines():
                    try:
                        msg = json.loads(line).get("status") or ""
                    except ValueError:
                        msg = line[:160]
                    if msg and msg != last:
                        _log(msg[:160])
                        last = msg

            _log("Creating the container…")
            r = await cx.post("/containers/create", params={"name": CONTAINER}, json={
                "Image": IMAGE,
                "HostConfig": {
                    "NetworkMode": "host",
                    "Binds": [f"{src}/go2rtc:/config:rw"],
                    "RestartPolicy": {"Name": "unless-stopped"},
                    "SecurityOpt": ["label=disable"],
                },
            })
            if r.status_code != 201:
                raise RuntimeError(f"create failed: {r.status_code} {r.text[:200]}")
            r = await cx.post(f"/containers/{CONTAINER}/start")
            if r.status_code not in (204, 304):
                raise RuntimeError(f"start failed: {r.status_code} {r.text[:200]}")
            _log("Started. Asking the host for a boot-time service…")
            request_service("install")
            _job.update(status="done", finished=time.time())
    except Exception as e:
        logger.error("go2rtc enable failed: %s", e)
        _log(str(e))
        _job.update(status="error", finished=time.time())


async def disable(remove: bool = False) -> Dict[str, Any]:
    _set_enabled(False)
    sock = containers.detect_socket()
    if not sock:
        return {"success": False, "error": "no container socket mounted"}
    try:
        async with _client(sock) as cx:
            if not await _inspect(cx, CONTAINER):
                request_service("remove")
                return {"success": True, "message": "go2rtc is not installed."}
            # Before the stop: the service would restart a stopped container.
            request_service("remove")
            r = await cx.post(f"/containers/{CONTAINER}/stop", params={"t": "10"})
            if remove:
                await cx.delete(f"/containers/{CONTAINER}", params={"force": "true"})
                return {"success": True, "message": "go2rtc stopped and removed."}
            ok = r.status_code in (204, 304)
            return {"success": ok, "message" if ok else "error":
                    "go2rtc stopped." if ok else f"stop failed: {r.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def restart() -> Dict[str, Any]:
    ok = await containers.restart_container(CONTAINER)
    return {"success": ok, "message" if ok else "error":
            "go2rtc restarted." if ok else "restart failed"}


START_RETRY_S = 60
SERVICE_RETRY_S = 15 * 60


async def ensure(t: Dict[str, Any]) -> None:
    """Watchdog step for an enabled go2rtc: move it to the pinned image (once
    per image); ask the host for its boot-time service if it has none; and,
    only where there is no such service to own its running state, start it
    when it's stopped."""
    if not enabled() or busy():
        return
    info = await containers.inspect_container(CONTAINER)
    image = ((info or {}).get("Config") or {}).get("Image")
    if info is None or image != IMAGE:
        if t.get("tried") != IMAGE:
            t["tried"] = IMAGE
            logger.info("go2rtc %s — enabling on %s", "missing" if info is None else "on " + str(image), IMAGE)
            start_enable()
        return
    svc = service_status()
    running = bool((info.get("State") or {}).get("Running"))
    if running and not (svc.get("installed") or svc.get("pending") or svc.get("conflict")
                        or svc.get("backend") == "none") \
            and time.time() - t.get("service_at", 0) > SERVICE_RETRY_S:
        t["service_at"] = time.time()
        request_service("install")
        logger.info("go2rtc has no boot-time service — asked the host to install one")
    if svc.get("installed"):
        return                              # the host's service restarts it
    if not running and time.time() - t.get("started_at", 0) > START_RETRY_S:
        t["started_at"] = time.time()
        logger.info("go2rtc is enabled but stopped — starting it")
        start_enable()
