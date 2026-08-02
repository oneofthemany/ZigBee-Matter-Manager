"""
Beekeeper sidecar lifecycle, driven from the manager, which already owns
container lifecycle. The day-to-day dashboard lives in the app's Beekeeper tab.

The sidecar reuses the app's image and its exact config/data/logs mounts, so it
sees the same config.yaml, and runs on host networking to serve the LAN on :53.
Best-effort throughout: a failed socket call returns a structured error.
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional

from manager import containers

logger = logging.getLogger("manager.beekeeper")

APP_CONTAINER = containers.APP_CONTAINER
BEEKEEPER_CONTAINER = f"{APP_CONTAINER}-beekeeper"

# Host-side firewall helper (scripts/beekeeper_firewall.sh) talks to the manager
# through these files under the mounted DATA_DIR, exactly like the OS-update flow.
_DATA_DIR = os.environ.get("ZMM_DATA_DIR") or os.environ.get("DATA_DIR") \
    or "/opt/.zigbee-matter-manager"
_FW_DIR = os.path.join(_DATA_DIR, "data", "beekeeper")
_FW_TRIGGER = os.path.join(_FW_DIR, "firewall_action")
_FW_STATUS = os.path.join(_FW_DIR, "firewall_status.json")


def firewall_status() -> Dict[str, Any]:
    """Read the host helper's last firewall result (never raises)."""
    try:
        with open(_FW_STATUS) as f:
            data = json.load(f)
        return {"available": True, **data}
    except (OSError, ValueError):
        return {"available": True, "backend": None, "port_53_open": None,
                "detail": "not checked yet", "updated_at": None}


def request_firewall(action: str = "open") -> Dict[str, Any]:
    """Ask the host helper to open (or re-check) :53 by writing its trigger file.

    Needs the host path unit installed (install_watcher.sh / a current build.sh).
    If it isn't, the trigger is simply never consumed — surfaced as a stale
    status in the UI.
    """
    if action not in ("open", "check"):
        return {"success": False, "error": "action must be open|check"}
    try:
        os.makedirs(_FW_DIR, exist_ok=True)
        with open(_FW_TRIGGER, "w") as f:
            f.write(action)
        return {"success": True, "message": f"Firewall {action} requested — the host "
                "helper applies it within a second or two; refresh to see the result."}
    except OSError as e:
        return {"success": False, "error": str(e)}

# App-container mount destinations the sidecar needs to share.
_SHARE_DESTS = ("/app/config", "/app/data", "/app/logs")


def _client(sock: str):
    import httpx
    transport = httpx.AsyncHTTPTransport(uds=sock)
    return httpx.AsyncClient(transport=transport, base_url="http://d", timeout=30.0)


async def _inspect(cx, name: str) -> Optional[Dict[str, Any]]:
    try:
        r = await cx.get(f"/containers/{name}/json")
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.debug("inspect %s failed: %s", name, e)
        return None


async def status() -> Dict[str, Any]:
    """Report whether the Beekeeper sidecar exists and is running.

    Shape: {available, installed, running, name, image, state, error}. The main
    app checks the control API for the *operational* state (bound/blocking); the
    manager only reports the container's existence/run state.
    """
    sock = containers.detect_socket()
    if not sock:
        return {"available": False, "installed": False, "running": False,
                "name": BEEKEEPER_CONTAINER, "error": "no container socket mounted"}
    try:
        async with _client(sock) as cx:
            info = await _inspect(cx, BEEKEEPER_CONTAINER)
    except Exception as e:
        return {"available": False, "installed": False, "running": False,
                "name": BEEKEEPER_CONTAINER, "error": str(e)}
    if not info:
        return {"available": True, "installed": False, "running": False,
                "name": BEEKEEPER_CONTAINER, "firewall": firewall_status(),
                "error": None}
    state = (info.get("State") or {})
    return {
        "available": True,
        "installed": True,
        "running": bool(state.get("Running")),
        "name": BEEKEEPER_CONTAINER,
        "image": (info.get("Config") or {}).get("Image"),
        "state": state.get("Status"),   # running | exited | created | ...
        "firewall": firewall_status(),
        "error": None,
    }


def _binds_from_app(app_info: Dict[str, Any]) -> List[str]:
    """Reuse the app container's config/data/logs mounts for the sidecar."""
    binds: List[str] = []
    for m in (app_info.get("Mounts") or []):
        dest = m.get("Destination")
        src = m.get("Source")
        if dest in _SHARE_DESTS and src:
            mode = "rw"
            binds.append(f"{src}:{dest}:{mode}")
    return binds


async def _create_config(app_info: Dict[str, Any]) -> Dict[str, Any]:
    image = (app_info.get("Config") or {}).get("Image") or app_info.get("Image")
    return {
        "Image": image,
        "Cmd": ["python", "-m", "beekeeper"],
        "Env": [f"ZMM_CONTAINER_NAME={APP_CONTAINER}"],
        "HostConfig": {
            "NetworkMode": "host",          # serve the LAN on :53
            "Binds": _binds_from_app(app_info),
            "RestartPolicy": {"Name": "always"},
            "SecurityOpt": ["label=disable"],
        },
    }


async def _create_and_start(cx, app_info: Dict[str, Any]) -> Dict[str, Any]:
    cfg = await _create_config(app_info)
    if not cfg["Image"]:
        return {"success": False, "error": "app image reference unresolved"}
    if not cfg["HostConfig"]["Binds"]:
        return {"success": False, "error": "app container has no /app/config or "
                "/app/data mounts to share (running from source?). Use "
                "scripts/install_beekeeper.sh instead."}
    r = await cx.post("/containers/create",
                      params={"name": BEEKEEPER_CONTAINER}, json=cfg)
    if r.status_code not in (201,):
        return {"success": False, "error": f"create failed: {r.status_code} {r.text}"}
    cid = r.json().get("Id", BEEKEEPER_CONTAINER)
    r = await cx.post(f"/containers/{cid}/start")
    if r.status_code in (204, 304):
        return {"success": True, "created": True,
                "message": "Beekeeper installed and started.",
                "image": cfg["Image"], "binds": cfg["HostConfig"]["Binds"]}
    return {"success": False, "error": f"start failed: {r.status_code} {r.text}"}


async def enable() -> Dict[str, Any]:
    """Create (if needed) and start the Beekeeper sidecar. Idempotent.

    If the sidecar already exists but was built from a *different* image than the
    app is now running (i.e. the app was upgraded), it's recreated from the new
    image so a rebuild actually ships the new Beekeeper code — otherwise Enable
    would just restart the stale container.
    """
    sock = containers.detect_socket()
    if not sock:
        return {"success": False, "error": "No container socket mounted — cannot "
                "manage the Beekeeper container from the manager."}
    try:
        async with _client(sock) as cx:
            app_info = await _inspect(cx, APP_CONTAINER)
            if not app_info:
                return {"success": False, "error": f"could not inspect app container "
                        f"'{APP_CONTAINER}' to derive image/mounts"}
            app_image_id = app_info.get("Image")   # resolved image ID (sha256:…)

            existing = await _inspect(cx, BEEKEEPER_CONTAINER)
            if existing:
                existing_image_id = existing.get("Image")
                if app_image_id and existing_image_id and app_image_id != existing_image_id:
                    logger.info("beekeeper image differs from app (%s != %s) — "
                                "recreating from new image", existing_image_id[:19],
                                app_image_id[:19])
                    await cx.post(f"/containers/{BEEKEEPER_CONTAINER}/stop", params={"t": "10"})
                    await cx.delete(f"/containers/{BEEKEEPER_CONTAINER}", params={"force": "true"})
                    return await _create_and_start(cx, app_info)
                # Same image → just (re)start the existing container.
                r = await cx.post(f"/containers/{BEEKEEPER_CONTAINER}/start")
                if r.status_code in (204, 304):
                    return {"success": True, "created": False, "message": "Beekeeper started."}
                return {"success": False, "error": f"start failed: {r.status_code} {r.text}"}

            return await _create_and_start(cx, app_info)
    except Exception as e:
        logger.error("Beekeeper enable failed: %s", e)
        return {"success": False, "error": str(e)}


async def disable(remove: bool = False) -> Dict[str, Any]:
    """Stop the Beekeeper sidecar (optionally remove the container)."""
    sock = containers.detect_socket()
    if not sock:
        return {"success": False, "error": "no container socket mounted"}
    try:
        async with _client(sock) as cx:
            if not await _inspect(cx, BEEKEEPER_CONTAINER):
                return {"success": True, "message": "Beekeeper is not installed."}
            r = await cx.post(f"/containers/{BEEKEEPER_CONTAINER}/stop", params={"t": "10"})
            stopped = r.status_code in (204, 304)
            if remove:
                await cx.delete(f"/containers/{BEEKEEPER_CONTAINER}", params={"force": "true"})
                return {"success": True, "message": "Beekeeper stopped and removed."}
            return {"success": stopped,
                    "message" if stopped else "error":
                        "Beekeeper stopped." if stopped else f"stop failed: {r.status_code}"}
    except Exception as e:
        logger.error("Beekeeper disable failed: %s", e)
        return {"success": False, "error": str(e)}


async def restart() -> Dict[str, Any]:
    ok = await containers.restart_container(BEEKEEPER_CONTAINER)
    return {"success": ok, "message" if ok else "error":
            "Beekeeper restarted." if ok else "restart failed"}
