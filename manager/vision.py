"""
Object-detection sidecar lifecycle. Like Beekeeper it runs from the app's own
image (``python -m vision``), so it needs no image of its own and follows the
app through upgrades; like go2rtc it is off until enabled here. The app pushes
the camera list and reads results over loopback. See docs/vision.md §Sidecar.

Standalone by design: the manager never imports from modules/.
"""
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from manager import accelerators, containers

logger = logging.getLogger("manager.vision")

CONTAINER = f"{containers.APP_CONTAINER}-vision"
_DATA_DIR = os.environ.get("ZMM_DATA_DIR") or os.environ.get("DATA_DIR") \
    or "/opt/.zigbee-matter-manager"
_DIR = os.path.join(_DATA_DIR, "data", "vision")
# Whether the user wants it running — so the watchdog can tell a stop someone
# asked for from a container that died.
_STATE = os.path.join(_DIR, "manager.json")
# Boot-time service, written on the host by scripts/sidecar_service.sh vision.
_SVC_TRIGGER = os.path.join(_DIR, "service_action")
_SVC_STATUS = os.path.join(_DIR, "service_status.json")
_SHARE = ("/app/data", "/app/logs")
BACKEND_ENV = "ZMM_VISION_BACKEND"


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
    """The host helper's last report on the boot-time service (never raises)."""
    try:
        with open(_SVC_STATUS) as f:
            data = json.load(f)
        return {"known": True, **data, "pending": os.path.isfile(_SVC_TRIGGER)}
    except (OSError, ValueError):
        return {"known": False, "installed": False, "pending": os.path.isfile(_SVC_TRIGGER),
                "detail": "not checked yet — needs the host helper (install_watcher.sh)"}


def request_service(action: str) -> Dict[str, Any]:
    if action not in ("install", "remove", "check"):
        return {"success": False, "error": "action must be install|remove|check"}
    try:
        os.makedirs(_DIR, exist_ok=True)
        with open(_SVC_TRIGGER, "w") as f:
            f.write(action)
        return {"success": True, "message": f"Autostart {action} requested"}
    except OSError as e:
        return {"success": False, "error": str(e)}


def hardware() -> Dict[str, Any]:
    """Which backend the sidecar should use, and the devices it needs passed in.
    Only the Coral has a backend so far; anything else detects on the CPU."""
    devices: List[Dict[str, str]] = []
    binds: List[str] = []
    rules: List[str] = []
    corals = [d for d in accelerators.probe().get("devices", []) if d["kind"] == "coral" and d["ready"]]
    for d in corals:
        if d["bus"] == "pci":
            devices += [{"PathOnHost": f"/dev/{n}", "PathInContainer": f"/dev/{n}", "CgroupPermissions": "rwm"}
                        for n in d.get("device_nodes") or []]
        else:
            # The stick re-enumerates under a new node once its firmware loads,
            # so the whole USB bus is passed rather than one device.
            binds, rules = ["/dev/bus/usb:/dev/bus/usb"], ["c 189:* rwm"]
    usable = bool(devices or binds)
    return {"backend": "coral" if usable else "cpu", "devices": devices, "binds": binds, "rules": rules}


def _client(sock: str):
    import httpx
    return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=sock),
                             base_url="http://d", timeout=60.0)


async def _inspect(cx, name: str) -> Optional[Dict[str, Any]]:
    try:
        r = await cx.get(f"/containers/{name}/json")
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.debug("inspect %s failed: %s", name, e)
        return None


def _backend_of(info: Dict[str, Any]) -> Optional[str]:
    for e in (info.get("Config") or {}).get("Env") or []:
        if e.startswith(BACKEND_ENV + "="):
            return e.split("=", 1)[1]
    return None


async def status() -> Dict[str, Any]:
    """Container state for the card and /status. Never raises."""
    hw = hardware()
    out: Dict[str, Any] = {"available": False, "installed": False, "running": False,
                           "enabled": enabled(), "name": CONTAINER, "state": None,
                           "backend": None, "would_use": hw["backend"],
                           "service": service_status(), "error": None}
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
                   backend=_backend_of(info))
    return out


def _spec(app_info: Dict[str, Any], hw: Dict[str, Any]) -> Dict[str, Any]:
    binds = [f"{m['Source']}:{m['Destination']}:rw" for m in app_info.get("Mounts") or []
             if m.get("Destination") in _SHARE and m.get("Source")]
    if not any(b.split(":")[1] == "/app/data" for b in binds):
        raise RuntimeError(f"'{containers.APP_CONTAINER}' has no /app/data mount to share")
    host = {
        # Loopback only: the app reaches it on 127.0.0.1, nothing else should.
        "NetworkMode": "host",
        "Binds": binds + hw["binds"],
        "RestartPolicy": {"Name": "unless-stopped"},
        "SecurityOpt": ["label=disable"],
    }
    if hw["devices"]:
        host["Devices"] = hw["devices"]
    if hw["rules"]:
        host["DeviceCgroupRules"] = hw["rules"]
    return {"Image": (app_info.get("Config") or {}).get("Image") or app_info.get("Image"),
            "Cmd": ["python", "-m", "vision"],
            "Env": [f"{BACKEND_ENV}={hw['backend']}"],
            "HostConfig": host}


async def enable() -> Dict[str, Any]:
    """Create (if needed) and start the sidecar. Recreates it when the app's
    image has moved on or the detection hardware has changed. Idempotent."""
    _set_enabled(True)
    sock = containers.detect_socket()
    if not sock:
        return {"success": False, "error": "No container socket mounted — cannot manage the sidecar."}
    try:
        async with _client(sock) as cx:
            app_info = await _inspect(cx, containers.APP_CONTAINER)
            if not app_info:
                return {"success": False, "error": f"could not inspect '{containers.APP_CONTAINER}'"}
            hw = hardware()
            existing = await _inspect(cx, CONTAINER)
            if existing and existing.get("Image") == app_info.get("Image") and _backend_of(existing) == hw["backend"]:
                r = await cx.post(f"/containers/{CONTAINER}/start")
                if r.status_code not in (204, 304):
                    return {"success": False, "error": f"start failed: {r.status_code} {r.text[:200]}"}
                request_service("install")
                return {"success": True, "created": False, "message": "Object detection started."}
            if existing:
                request_service("remove")          # its unit would restart the old container
                await cx.post(f"/containers/{CONTAINER}/stop", params={"t": "10"})
                await cx.delete(f"/containers/{CONTAINER}", params={"force": "true"})
            r = await cx.post("/containers/create", params={"name": CONTAINER}, json=_spec(app_info, hw))
            if r.status_code != 201:
                return {"success": False, "error": f"create failed: {r.status_code} {r.text[:200]}"}
            r = await cx.post(f"/containers/{CONTAINER}/start")
            if r.status_code not in (204, 304):
                return {"success": False, "error": f"start failed: {r.status_code} {r.text[:200]}"}
            request_service("install")
            where = "the Coral" if hw["backend"] == "coral" else "the CPU"
            return {"success": True, "created": True, "backend": hw["backend"],
                    "message": f"Object detection started on {where}."}
    except Exception as e:
        logger.error("vision enable failed: %s", e)
        return {"success": False, "error": str(e)}


async def disable(remove: bool = False) -> Dict[str, Any]:
    _set_enabled(False)
    sock = containers.detect_socket()
    if not sock:
        return {"success": False, "error": "no container socket mounted"}
    try:
        async with _client(sock) as cx:
            # Before the stop: the service would restart a stopped container.
            request_service("remove")
            if not await _inspect(cx, CONTAINER):
                return {"success": True, "message": "Object detection is not installed."}
            r = await cx.post(f"/containers/{CONTAINER}/stop", params={"t": "10"})
            if remove:
                await cx.delete(f"/containers/{CONTAINER}", params={"force": "true"})
                return {"success": True, "message": "Object detection stopped and removed."}
            ok = r.status_code in (204, 304)
            return {"success": ok, "message" if ok else "error":
                    "Object detection stopped." if ok else f"stop failed: {r.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def restart() -> Dict[str, Any]:
    ok = await containers.restart_container(CONTAINER)
    return {"success": ok, "message" if ok else "error":
            "Object detection restarted." if ok else "restart failed"}


START_RETRY_S = 60
SERVICE_RETRY_S = 15 * 60


async def ensure(t: Dict[str, Any]) -> None:
    """Watchdog step for an enabled sidecar: recreate it when the app's image
    or the detection hardware changed (once per combination), ask the host for
    its boot-time service, and start it where no service owns that."""
    if not enabled():
        return
    info = await containers.inspect_container(CONTAINER)
    app_info = await containers.inspect_container(containers.APP_CONTAINER)
    if not app_info:
        return
    want = (app_info.get("Image"), hardware()["backend"])
    if info is None or (info.get("Image"), _backend_of(info)) != want:
        if t.get("tried") != want:
            t["tried"] = want
            res = await enable()
            logger.info("object detection %s: %s", "recreated" if info else "created",
                        res.get("message") or res.get("error"))
        return
    svc = service_status()
    running = bool((info.get("State") or {}).get("Running"))
    if running and not (svc.get("installed") or svc.get("pending") or svc.get("conflict")
                        or svc.get("backend") == "none") \
            and time.time() - t.get("service_at", 0) > SERVICE_RETRY_S:
        t["service_at"] = time.time()
        request_service("install")
    if svc.get("installed"):
        return
    if not running and time.time() - t.get("started_at", 0) > START_RETRY_S:
        t["started_at"] = time.time()
        logger.info("object detection is enabled but stopped — starting it")
        await enable()
