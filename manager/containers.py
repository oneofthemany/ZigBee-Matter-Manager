"""Container inspection + restart over the mounted runtime socket.

Talks to podman/docker's Docker-compatible REST API over a unix socket (the same
mechanism modules/ollama_manager.py uses). Visibility covers this deployment's
containers (ZMM_CONTAINER_NAME prefix) plus the extra sibling containers the
manager also looks after (ZMM_EXTRA_CONTAINERS, default: ollama).
"""
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("manager.containers")

# Where the host's container socket may be mounted (root podman first).
_SOCK_CANDIDATES = (
    "/run/podman/podman.sock",
    "/var/run/podman/podman.sock",
    "/var/run/docker.sock",
    "/run/docker.sock",
)

# Only surface containers that belong to this deployment.
APP_CONTAINER = os.environ.get("ZMM_CONTAINER_NAME", "zigbee-matter-manager")

# Sibling containers the manager also watches (exact names, not prefixes, so an
# unrelated "ollama-webui" isn't grabbed). Comma-separated; empty to opt out.
EXTRA_CONTAINERS = tuple(
    x.strip() for x in os.environ.get("ZMM_EXTRA_CONTAINERS", "ollama").split(",")
    if x.strip())


def visible_container(name: str) -> bool:
    """Visibility rule shared by list_containers and the log streamer: the app
    container and its -manager/-previous siblings (prefix), plus extras (exact)."""
    if not name or "/" in name:
        return False
    return name.startswith(APP_CONTAINER) or name in EXTRA_CONTAINERS


def detect_socket() -> Optional[str]:
    """Return a reachable container-runtime unix socket path, or None.
    Honours ZMM_CONTAINER_SOCK / CONTAINER_HOST / DOCKER_HOST (unix:// form)."""
    for env in ("ZMM_CONTAINER_SOCK", "CONTAINER_HOST", "DOCKER_HOST"):
        v = os.environ.get(env)
        if v:
            v = v.replace("unix://", "")
            if os.path.exists(v):
                return v
    for p in _SOCK_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


async def list_containers() -> Dict[str, Any]:
    """List this deployment's containers (name, state, status, image). Never raises.
    Returns {"available": bool, "socket": str|None, "containers": [...], "error": str|None}."""
    sock = detect_socket()
    if not sock:
        return {"available": False, "socket": None,
                "error": "no container socket mounted", "containers": []}
    try:
        import httpx
    except Exception:
        return {"available": False, "socket": sock,
                "error": "httpx unavailable in image", "containers": []}

    out: List[Dict[str, Any]] = []
    try:
        transport = httpx.AsyncHTTPTransport(uds=sock)
        async with httpx.AsyncClient(transport=transport, base_url="http://d",
                                     timeout=10.0) as cx:
            r = await cx.get("/containers/json", params={"all": "true"})
            if r.status_code != 200:
                return {"available": False, "socket": sock,
                        "error": f"containers/json returned {r.status_code}", "containers": []}
            for c in r.json():
                names = [n.lstrip("/") for n in (c.get("Names") or [])]
                if not any(visible_container(n) for n in names):
                    continue
                out.append({
                    "name": names[0] if names else "",
                    "state": c.get("State"),    # running | exited | created | ...
                    "status": c.get("Status"),  # e.g. "Up 4 minutes (healthy)"
                    "image": c.get("Image"),
                })
    except Exception as e:
        logger.warning("list_containers failed: %s", e)
        return {"available": False, "socket": sock, "error": str(e), "containers": []}
    return {"available": True, "socket": sock, "error": None, "containers": out}


async def inspect_container(name: str) -> Optional[Dict[str, Any]]:
    """Return the raw /containers/{name}/json inspect dict, or None. Used by the
    watchdog to read State.Running and State.StartedAt. Never raises."""
    sock = detect_socket()
    if not sock:
        return None
    try:
        import httpx
        transport = httpx.AsyncHTTPTransport(uds=sock)
        async with httpx.AsyncClient(transport=transport, base_url="http://d",
                                     timeout=10.0) as cx:
            r = await cx.get(f"/containers/{name}/json")
            return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.debug("inspect_container(%s) failed: %s", name, e)
        return None


async def volume_exists(name: str) -> bool:
    """True if a named volume exists on the runtime. Never raises."""
    sock = detect_socket()
    if not sock:
        return False
    try:
        import httpx
        transport = httpx.AsyncHTTPTransport(uds=sock)
        async with httpx.AsyncClient(transport=transport, base_url="http://d",
                                     timeout=10.0) as cx:
            r = await cx.get(f"/volumes/{name}")
            return r.status_code == 200
    except Exception as e:
        logger.debug("volume_exists(%s) failed: %s", name, e)
        return False


async def restart_container(name: str, timeout: int = 10) -> bool:
    """Restart a container via the socket. Returns True on success. Never raises."""
    sock = detect_socket()
    if not sock:
        return False
    try:
        import httpx
        transport = httpx.AsyncHTTPTransport(uds=sock)
        async with httpx.AsyncClient(transport=transport, base_url="http://d",
                                     timeout=float(timeout) + 30.0) as cx:
            r = await cx.post(f"/containers/{name}/restart", params={"t": str(timeout)})
            return r.status_code in (204, 304)
    except Exception as e:
        logger.warning("restart_container(%s) failed: %s", name, e)
        return False
