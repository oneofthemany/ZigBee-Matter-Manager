"""
Runs a local Ollama model server as a sibling container.

ZMM's own image carries no podman/docker CLI, so this drives the host runtime
over its Docker-compatible REST API via a mounted socket, falling back to the
CLI when one is on PATH. Every privileged action is user-triggered and gated on
the HostCapabilityAssessor. See docs/upgrades.md.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("modules.ollama_manager")

OLLAMA_IMAGE = "docker.io/ollama/ollama:latest"
CONTAINER_NAME = "ollama"
VOLUME_NAME = "ollama-models"
PORT = 11434
CDI_SPEC = "/etc/cdi/nvidia.yaml"
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")
_MAX_LOG = 300

# Where the host's container socket may be mounted (root podman first — ZMM is a
# root container; then docker; then a rootless fallback). Override with
# ZMM_CONTAINER_SOCK or a unix:// DOCKER_HOST/CONTAINER_HOST.
_SOCK_CANDIDATES = (
    "/run/podman/podman.sock",
    "/var/run/podman/podman.sock",
    "/var/run/docker.sock",
    "/run/docker.sock",
)


def detect_container_socket() -> Optional[str]:
    """Return a reachable container-runtime unix socket path, or None.

    Honours ZMM_CONTAINER_SOCK / CONTAINER_HOST / DOCKER_HOST (unix:// form),
    then well-known mount points, then the rootless per-user path.
    """
    for env in ("ZMM_CONTAINER_SOCK", "CONTAINER_HOST", "DOCKER_HOST"):
        val = os.environ.get(env)
        if not val:
            continue
        path = val[len("unix://"):] if val.startswith("unix://") else val
        if path.startswith("/") and os.path.exists(path):
            return path
    for p in _SOCK_CANDIDATES:
        if os.path.exists(p):
            return p
    # Rootless fallback: /run/user/<uid>/podman/podman.sock
    try:
        uid = os.getuid()
        rootless = f"/run/user/{uid}/podman/podman.sock"
        if os.path.exists(rootless):
            return rootless
    except AttributeError:
        pass
    return None


def detect_runtime() -> Tuple[Optional[str], Optional[str]]:
    """Return (mode, detail). mode ∈ {'cli','socket',None}."""
    for rt in ("podman", "docker"):
        if shutil.which(rt):
            return "cli", rt
    sock = detect_container_socket()
    if sock:
        return "socket", sock
    return None, None


class OllamaManager:
    def __init__(self, assessor=None):
        from modules.llm_host import HostCapabilityAssessor
        self._assessor = assessor or HostCapabilityAssessor()
        self._job: Optional[Dict[str, Any]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Where ZMM reaches the running Ollama (the sibling publishes on the host).
        self._ollama_url = os.environ.get("ZMM_OLLAMA_URL", "").rstrip("/") or None

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        """Remember the app loop so worker-thread calls can schedule coroutines."""
        self._loop = loop

    def _spawn(self, coro):
        """Schedule a coroutine on the app loop from any thread."""
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    # Status

    def status(self) -> Dict[str, Any]:
        mode, detail = detect_runtime()
        running = self._port_open() or bool(self._list_models())
        exists = self._container_exists(mode, detail) if mode else False
        return {
            "mode": mode,                       # cli | socket | None
            "runtime": detail,                  # binary name or socket path
            "running": running,
            "container_exists": exists,
            "installed": running or exists,
            "gpu_passthrough": self._gpu_available(),
            "base_url": f"{self._reachable_base()}/v1",
            "models": self._list_models() if running else [],
            "job": self._job,
        }

    def job_status(self) -> Dict[str, Any]:
        return self._job or {"status": "idle"}

    # Install / start

    def install(self) -> Dict[str, Any]:
        if self._busy():
            return {"success": False, "error": "A job is already running."}
        mode, detail = detect_runtime()
        if not mode:
            return {"success": False,
                    "error": "No container runtime reachable. Mount the host "
                             "podman socket into ZMM (e.g. "
                             "/run/podman/podman.sock) and enable it on the host "
                             "with: systemctl enable --now podman.socket"}

        verdict = self._assessor.assess().get("verdict", {})
        if not verdict.get("llm_viable"):
            return {"success": False,
                    "error": "Host assessment says a local model isn't viable here. "
                             "Use a remote provider instead."}

        if mode == "cli":
            self._spawn(self._run_cli_install(detail))
        else:
            self._spawn(self._run_rest_install(detail))
        return {"success": True, "started": True, "mode": mode}

    # Pull a model

    def pull(self, model: str) -> Dict[str, Any]:
        if self._busy():
            return {"success": False, "error": "A job is already running."}
        if not model or not _MODEL_RE.match(model):
            return {"success": False, "error": "Invalid model name."}
        if not (self._port_open() or self._list_models()):
            return {"success": False,
                    "error": "Ollama isn't running — install/start it first."}
        # Model pulls go through the Ollama API itself (works in both modes and
        # avoids needing `podman exec`).
        self._spawn(self._run_model_pull(model))
        return {"success": True, "started": True, "model": model}

    # CLI mode (native podman/docker on PATH)

    async def _run_cli_install(self, rt: str):
        if self._container_exists("cli", rt):
            cmd = [rt, "start", CONTAINER_NAME]
        else:
            cmd = [rt, "run", "-d", "--name", CONTAINER_NAME,
                   "--restart", "unless-stopped",
                   "-p", f"0.0.0.0:{PORT}:{PORT}",
                   "-v", f"{VOLUME_NAME}:/root/.ollama"]
            if self._gpu_available():
                cmd += ["--device", "nvidia.com/gpu=all",
                        "--security-opt", "label=disable"]
            cmd += [OLLAMA_IMAGE]
        await self._run_cli_job(cmd, "install")

    async def _run_cli_job(self, cmd: List[str], action: str):
        self._job_start(action, " ".join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            assert proc.stdout is not None
            async for raw in proc.stdout:
                self._job_log(raw.decode(errors="replace").rstrip())
            rc = await proc.wait()
            self._job["returncode"] = rc
            self._job_finish(rc == 0, None if rc == 0 else f"[exit {rc}]")
        except Exception as e:
            logger.error(f"Ollama {action} job failed: {e}")
            self._job_finish(False, str(e))

    # Socket mode (Docker-compatible REST over a mounted unix socket)

    async def _run_rest_install(self, sock: str):
        self._job_start("install", f"REST via {sock}")
        try:
            import httpx
        except Exception:
            self._job_finish(False, "httpx not available in image")
            return
        transport = httpx.AsyncHTTPTransport(uds=sock)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://d",
                                         timeout=None) as cx:
                # Start an existing container if present.
                if await self._rest_container_exists(cx):
                    self._job_log("Starting existing 'ollama' container…")
                    r = await cx.post(f"/containers/{CONTAINER_NAME}/start")
                    self._job_finish(r.status_code in (204, 304),
                                     None if r.status_code in (204, 304)
                                     else f"start failed: {r.status_code} {r.text}")
                    return

                # Pull the image (streamed progress).
                self._job_log(f"Pulling image {OLLAMA_IMAGE}…")
                async with cx.stream("POST", "/images/create",
                                     params={"fromImage": OLLAMA_IMAGE}) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode(errors="replace")
                        self._job_finish(False, f"image pull failed: {resp.status_code} {body}")
                        return
                    async for line in resp.aiter_lines():
                        msg = self._compact_progress(line)
                        if msg:
                            self._job_log(msg)

                # Create + start.
                self._job_log("Creating 'ollama' container…")
                cfg = self._rest_create_config()
                r = await cx.post("/containers/create",
                                  params={"name": CONTAINER_NAME}, json=cfg)
                if r.status_code not in (201,):
                    self._job_finish(False, f"create failed: {r.status_code} {r.text}")
                    return
                cid = r.json().get("Id", CONTAINER_NAME)
                r = await cx.post(f"/containers/{cid}/start")
                ok = r.status_code in (204, 304)
                self._job_log("Started." if ok else f"start failed: {r.status_code}")
                self._job_finish(ok, None if ok else r.text)
        except Exception as e:
            logger.error(f"Ollama REST install failed: {e}")
            self._job_finish(False, str(e))

    def _rest_create_config(self) -> Dict[str, Any]:
        host_config: Dict[str, Any] = {
            "PortBindings": {f"{PORT}/tcp": [{"HostIp": "0.0.0.0",
                                              "HostPort": str(PORT)}]},
            "RestartPolicy": {"Name": "unless-stopped"},
            "Binds": [f"{VOLUME_NAME}:/root/.ollama"],
        }
        if self._gpu_available():
            host_config["Devices"] = [{"PathOnHost": "nvidia.com/gpu=all",
                                       "PathInContainer": "nvidia.com/gpu=all",
                                       "CgroupPermissions": "rwm"}]
        return {
            "Image": OLLAMA_IMAGE,
            "ExposedPorts": {f"{PORT}/tcp": {}},
            "HostConfig": host_config,
        }

    async def _rest_container_exists(self, cx) -> bool:
        try:
            r = await cx.get("/containers/json",
                             params={"all": "true",
                                     "filters": json.dumps({"name": [CONTAINER_NAME]})})
            if r.status_code != 200:
                return False
            for c in r.json():
                names = [n.lstrip("/") for n in (c.get("Names") or [])]
                if CONTAINER_NAME in names:
                    return True
        except Exception:
            pass
        return False

    @staticmethod
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

    # Model pull (via the Ollama API — both modes)

    async def _run_model_pull(self, model: str):
        self._job_start("pull", f"ollama pull {model}", model=model)
        try:
            import httpx
        except Exception:
            self._job_finish(False, "httpx not available in image")
            return
        url = f"{self._reachable_base()}/api/pull"
        try:
            async with httpx.AsyncClient(timeout=None) as cx:
                async with cx.stream("POST", url, json={"name": model}) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode(errors="replace")
                        self._job_finish(False, f"pull failed: {resp.status_code} {body}")
                        return
                    last = None
                    async for line in resp.aiter_lines():
                        msg = self._compact_progress(line)
                        if msg and msg != last:
                            self._job_log(msg)
                            last = msg
            self._job_finish(True, None)
        except Exception as e:
            logger.error(f"Ollama model pull failed: {e}")
            self._job_finish(False, str(e))

    # Job bookkeeping

    def _job_start(self, action: str, command: str, model: Optional[str] = None):
        self._job = {"action": action, "model": model, "status": "running",
                     "log": [], "started": time.time(), "command": command}

    def _job_log(self, line: str):
        if not line or not self._job:
            return
        self._job["log"].append(line)
        if len(self._job["log"]) > _MAX_LOG:
            self._job["log"] = self._job["log"][-_MAX_LOG:]

    def _job_finish(self, ok: bool, err: Optional[str]):
        if not self._job:
            return
        self._job["status"] = "done" if ok else "error"
        if err:
            self._job_log(err)
        self._job["finished"] = time.time()

    def _busy(self) -> bool:
        return bool(self._job and self._job.get("status") == "running")

    # Reachability / helpers

    def _reachable_base(self) -> str:
        if self._ollama_url:
            return self._ollama_url
        # Native: localhost. Containerised: the slirp host gateway.
        mode, _ = detect_runtime()
        if mode == "cli" or self._port_open("127.0.0.1"):
            return f"http://127.0.0.1:{PORT}"
        return f"http://10.0.2.2:{PORT}"

    @staticmethod
    def _gpu_available() -> bool:
        return os.path.exists(CDI_SPEC)

    def _port_open(self, host: Optional[str] = None, timeout: float = 0.4) -> bool:
        hosts = [host] if host else ["127.0.0.1", "10.0.2.2"]
        if self._ollama_url:
            m = re.search(r"://([^:/]+)", self._ollama_url)
            if m:
                hosts = [m.group(1)] + hosts
        for h in hosts:
            try:
                with socket.create_connection((h, PORT), timeout=timeout):
                    return True
            except Exception:
                continue
        return False

    def _container_exists(self, mode: Optional[str], detail: Optional[str]) -> bool:
        if mode == "cli" and detail:
            try:
                import subprocess
                out = subprocess.run(
                    [detail, "ps", "-a", "--filter", f"name=^{CONTAINER_NAME}$",
                     "--format", "{{.Names}}"],
                    capture_output=True, text=True, timeout=6)
                return CONTAINER_NAME in out.stdout.split()
            except Exception:
                return False
        if mode == "socket" and detail:
            try:
                import urllib.request as _u
                # Socket REST exists check is async elsewhere; for status() use a
                # cheap blocking probe via the running-port heuristic instead.
                return self._port_open()
            except Exception:
                return False
        return False

    def _list_models(self) -> List[Dict[str, Any]]:
        try:
            with urllib.request.urlopen(
                    f"{self._reachable_base()}/api/tags", timeout=3) as r:
                data = json.loads(r.read().decode())
            return [{"name": m.get("name"),
                     "size_gb": round((m.get("size") or 0) / (1024 ** 3), 1)}
                    for m in data.get("models", [])]
        except Exception:
            return []
