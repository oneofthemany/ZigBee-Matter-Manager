"""Ollama sibling-container management for the manager sidecar.

The AI stack runs Ollama as a separate host container (created from the app's
Settings → AI via modules/ollama_manager.py). This module gives the manager its
own view + controls: status for /status and the dashboard card, model
list/pull/delete via the Ollama API, and an image update (pull latest, recreate
the container, models survive in the named volume).

Standalone by design: the manager is the disaster-recovery surface, so it never
imports from modules/ — the container create-config, job bookkeeping and
progress compaction below are duplicated BY CONVENTION from
modules/ollama_manager.py (keep them in sync if that file changes).

Reachability: Ollama publishes 11434 on the host; the manager runs off-pod with
host.containers.internal mapped to the host gateway, so the default base URL
works without extra deploy config (override with ZMM_OLLAMA_URL).
"""
import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

from manager import containers

logger = logging.getLogger("manager.ollama")

# Duplicated by convention from modules/ollama_manager.py — keep in sync.
IMAGE = "docker.io/ollama/ollama:latest"
CONTAINER = "ollama"
VOLUME = "ollama-models"
PORT = 11434
CDI_SPEC = "/etc/cdi/nvidia.yaml"
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")
_MAX_LOG = 300

OLLAMA_URL = (os.environ.get("ZMM_OLLAMA_URL", "").rstrip("/")
              or f"http://host.containers.internal:{PORT}")

# While an image update is running the container is stopped/removed/recreated
# on purpose — the watchdog checks this flag and stands down (the manager is a
# single process, so an in-process flag is enough).
update_active = False

_job: Optional[Dict[str, Any]] = None

# /images/{id}/json lookups cached by image id (immutable per id).
_image_cache: Dict[str, Dict[str, Any]] = {}


def updating() -> bool:
    return update_active


# ── Job bookkeeping (duplicated from modules/ollama_manager.py) ──────────────

def _job_start(action: str, command: str, model: Optional[str] = None):
    global _job
    _job = {"action": action, "model": model, "status": "running",
            "log": [], "started": time.time(), "command": command}


def _job_log(line: str):
    if not line or not _job:
        return
    _job["log"].append(line)
    if len(_job["log"]) > _MAX_LOG:
        _job["log"] = _job["log"][-_MAX_LOG:]


def _job_finish(ok: bool, err: Optional[str]):
    if not _job:
        return
    _job["status"] = "done" if ok else "error"
    if err:
        _job_log(err)
    _job["finished"] = time.time()


def _busy() -> bool:
    return bool(_job and _job.get("status") == "running")


def _compact_progress(line: str) -> Optional[str]:
    line = (line or "").strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except Exception:
        return line[:160]
    if "error" in obj:
        return f"error: {obj['error']}"
    status = obj.get("status") or ""
    return status[:160] or None


# ── Ollama API helpers ───────────────────────────────────────────────────────

async def _api_get(path: str, timeout: float = 5.0) -> Optional[Any]:
    """GET {OLLAMA_URL}{path} → parsed JSON, or None on any failure."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout) as cx:
            r = await cx.get(f"{OLLAMA_URL}{path}")
            return r.json() if r.status_code == 200 else None
    except Exception:
        return None


async def is_healthy() -> bool:
    """Watchdog health probe: the Ollama API answers /api/version."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2.0) as cx:
            r = await cx.get(f"{OLLAMA_URL}/api/version")
            return r.status_code == 200
    except Exception:
        return False


async def _image_info(image_id: str) -> Dict[str, Any]:
    """Short id + created date for an image id, cached (ids are immutable)."""
    if not image_id:
        return {}
    if image_id in _image_cache:
        return _image_cache[image_id]
    info: Dict[str, Any] = {"id": image_id.replace("sha256:", "")[:12]}
    sock = containers.detect_socket()
    if sock:
        try:
            import httpx
            transport = httpx.AsyncHTTPTransport(uds=sock)
            async with httpx.AsyncClient(transport=transport, base_url="http://d",
                                         timeout=10.0) as cx:
                r = await cx.get(f"/images/{image_id}/json")
                if r.status_code == 200:
                    info["created"] = (r.json().get("Created") or "")[:10]
        except Exception as e:
            logger.debug("image inspect failed: %s", e)
    _image_cache[image_id] = info
    return info


def _job_public() -> Optional[Dict[str, Any]]:
    return dict(_job) if _job else None


# ── Status ───────────────────────────────────────────────────────────────────

async def summary() -> Dict[str, Any]:
    """Cheap status for GET /status and the dashboard cells. Never raises."""
    out: Dict[str, Any] = {
        "installed": False, "present": False, "running": False,
        "healthy": False, "version": None, "model_count": None,
        "image": {}, "updating": update_active,
        "job": ({"action": _job.get("action"), "status": _job.get("status")}
                if _job else None),
    }
    try:
        info = await containers.inspect_container(CONTAINER)
        if info is None:
            out["installed"] = await containers.volume_exists(VOLUME)
            return out
        out["present"] = out["installed"] = True
        out["running"] = bool((info.get("State") or {}).get("Running"))
        out["image"] = await _image_info(info.get("Image") or "")
        if out["running"]:
            ver = await _api_get("/api/version", timeout=2.0)
            if ver:
                out["healthy"] = True
                out["version"] = ver.get("version")
            tags = await _api_get("/api/tags", timeout=3.0)
            if tags is not None:
                out["model_count"] = len(tags.get("models") or [])
    except Exception as e:
        logger.debug("ollama summary failed: %s", e)
    return out


async def detail() -> Dict[str, Any]:
    """Full card payload for GET /ollama: models, loaded set, disk usage, job log."""
    out = await summary()
    out["job"] = _job_public()
    out["models"] = []
    out["disk_bytes"] = 0
    if not out["running"]:
        return out
    tags = await _api_get("/api/tags", timeout=5.0) or {}
    ps = await _api_get("/api/ps", timeout=5.0) or {}
    loaded = {m.get("name") for m in (ps.get("models") or [])}
    for m in tags.get("models") or []:
        size = int(m.get("size") or 0)
        out["disk_bytes"] += size
        out["models"].append({
            "name": m.get("name"),
            "size": size,
            "modified": (m.get("modified_at") or "")[:10],
            "loaded": m.get("name") in loaded,
        })
    out["model_count"] = len(out["models"])
    return out


# ── Model pull / delete (via the Ollama API) ─────────────────────────────────

def pull_model(model: str) -> Tuple[bool, str]:
    """Start a background model pull. Returns (accepted, message)."""
    if _busy():
        return False, "A job is already running."
    if not model or not _MODEL_RE.match(model):
        return False, "Invalid model name."
    asyncio.create_task(_run_model_pull(model))
    return True, f"Pulling {model}…"


async def _run_model_pull(model: str):
    _job_start("pull", f"ollama pull {model}", model=model)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=None) as cx:
            async with cx.stream("POST", f"{OLLAMA_URL}/api/pull",
                                 json={"name": model}) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    _job_finish(False, f"pull failed: {resp.status_code} {body}")
                    return
                last = None
                async for line in resp.aiter_lines():
                    msg = _compact_progress(line)
                    if msg and msg != last:
                        _job_log(msg)
                        last = msg
        _job_finish(True, None)
    except Exception as e:
        logger.error("Ollama model pull failed: %s", e)
        _job_finish(False, str(e))


async def delete_model(model: str) -> Tuple[bool, str]:
    """Delete a model via the Ollama API (synchronous — deletes are quick)."""
    if not model or not _MODEL_RE.match(model):
        return False, "Invalid model name."
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as cx:
            r = await cx.request("DELETE", f"{OLLAMA_URL}/api/delete",
                                 json={"name": model})
            if r.status_code == 200:
                return True, f"Deleted {model}"
            if r.status_code == 404:
                return False, f"Model {model} not found"
            return False, f"delete failed: {r.status_code} {r.text[:200]}"
    except Exception as e:
        return False, str(e)


# ── Image update: pull latest, recreate container (volume survives) ──────────

def start_update() -> Tuple[bool, str]:
    """Start a background image update / container recreate."""
    if _busy():
        return False, "A job is already running."
    asyncio.create_task(_run_image_update())
    return True, "Update started"


def _create_config() -> Dict[str, Any]:
    # Duplicated by convention from modules/ollama_manager._rest_create_config
    # — keep in sync.
    host_config: Dict[str, Any] = {
        "PortBindings": {f"{PORT}/tcp": [{"HostIp": "0.0.0.0",
                                          "HostPort": str(PORT)}]},
        "RestartPolicy": {"Name": "unless-stopped"},
        "Binds": [f"{VOLUME}:/root/.ollama"],
    }
    if os.path.exists(CDI_SPEC):
        host_config["Devices"] = [{"PathOnHost": "nvidia.com/gpu=all",
                                   "PathInContainer": "nvidia.com/gpu=all",
                                   "CgroupPermissions": "rwm"}]
    return {
        "Image": IMAGE,
        "ExposedPorts": {f"{PORT}/tcp": {}},
        "HostConfig": host_config,
    }


async def _run_image_update():
    global update_active
    _job_start("update", f"pull {IMAGE} + recreate container")
    sock = containers.detect_socket()
    if not sock:
        _job_finish(False, "no container socket mounted")
        return
    try:
        import httpx
    except Exception:
        _job_finish(False, "httpx not available in image")
        return
    update_active = True
    try:
        transport = httpx.AsyncHTTPTransport(uds=sock)
        async with httpx.AsyncClient(transport=transport, base_url="http://d",
                                     timeout=None) as cx:
            _job_log(f"Pulling image {IMAGE}…")
            async with cx.stream("POST", "/images/create",
                                 params={"fromImage": IMAGE}) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    _job_finish(False, f"image pull failed: {resp.status_code} {body}")
                    return
                last = None
                async for line in resp.aiter_lines():
                    msg = _compact_progress(line)
                    if msg and msg != last:
                        _job_log(msg)
                        last = msg

            _job_log("Stopping 'ollama' container…")
            r = await cx.post(f"/containers/{CONTAINER}/stop", params={"t": "10"})
            if r.status_code not in (204, 304, 404):
                _job_finish(False, f"stop failed: {r.status_code} {r.text[:200]}")
                return

            _job_log("Removing old container…")
            r = await cx.delete(f"/containers/{CONTAINER}")
            if r.status_code not in (204, 404):
                _job_finish(False, f"remove failed: {r.status_code} {r.text[:200]}")
                return

            # From here the old container is gone; on failure the models volume
            # is intact and pressing Update again safely retries the recreate.
            _job_log("Creating 'ollama' container from the new image…")
            r = await cx.post("/containers/create",
                              params={"name": CONTAINER}, json=_create_config())
            if r.status_code != 201:
                _job_finish(False, f"container removed, recreate failed "
                            f"({r.status_code} {r.text[:200]}) — press Update "
                            f"again to retry; models are safe in the volume")
                return
            cid = r.json().get("Id", CONTAINER)
            r = await cx.post(f"/containers/{cid}/start")
            ok = r.status_code in (204, 304)
            _job_log("Started." if ok else f"start failed: {r.status_code}")
            _job_finish(ok, None if ok else
                        f"{r.text[:200]} — press Update again to retry")
    except Exception as e:
        logger.error("Ollama image update failed: %s", e)
        _job_finish(False, f"{e} — press Update again to retry")
    finally:
        update_active = False
