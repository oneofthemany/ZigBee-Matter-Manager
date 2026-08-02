"""
Runs an SGLang model server as a sibling container, mirroring OllamaManager.

Differs in being GPU-only — install is refused without a viable backend and
NVIDIA CDI passthrough — and in having no separate pull step, since the model is
given at launch and changing it means recreating the container.
See docs/upgrades.md.
"""

import asyncio
import json
import logging
import os
import re
import socket
import time
import urllib.request
from typing import Any, Dict, List, Optional

from modules.ollama_manager import detect_runtime

logger = logging.getLogger("modules.sglang_manager")

SGLANG_IMAGE = "docker.io/lmsysorg/sglang:latest"
CONTAINER_NAME = "sglang"
VOLUME_NAME = "sglang-hf-cache"
PORT = 30000
CDI_SPEC = "/etc/cdi/nvidia.yaml"
SHM_BYTES = 8 * 1024 ** 3  # SGLang wants a large /dev/shm for NCCL/tokenizer
# HF repo paths: org/name with common tag chars.
_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_MAX_LOG = 300


class SGLangManager:
    def __init__(self, assessor=None):
        from modules.llm_host import HostCapabilityAssessor
        self._assessor = assessor or HostCapabilityAssessor()
        self._job: Optional[Dict[str, Any]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._url = os.environ.get("ZMM_SGLANG_URL", "").rstrip("/") or None

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
        running = self._port_open()
        info = self._model_info() if running else {}
        return {
            "mode": mode,
            "runtime": detail,
            "running": running,
            "installed": running or self._container_exists_sync(mode, detail),
            "gpu_passthrough": self._gpu_available(),
            "base_url": f"{self._reachable_base()}/v1",
            "model": info.get("model_path"),
            "job": self._job,
        }

    def job_status(self) -> Dict[str, Any]:
        return self._job or {"status": "idle"}

    # Install / start

    def install(self, model: str, hf_token: Optional[str] = None) -> Dict[str, Any]:
        if self._busy():
            return {"success": False, "error": "A job is already running."}
        if not model or not _MODEL_RE.match(model):
            return {"success": False,
                    "error": "Model must be a HuggingFace path like "
                             "Qwen/Qwen2.5-7B-Instruct"}

        mode, detail = detect_runtime()
        if not mode:
            return {"success": False,
                    "error": "No container runtime reachable. Mount the host "
                             "podman socket into ZMM and enable it with: "
                             "systemctl enable --now podman.socket"}

        backends = self._assessor.assess().get("backends", {})
        sg = backends.get("sglang", {})
        if not sg.get("viable"):
            return {"success": False,
                    "error": sg.get("reason",
                                    "Host assessment says SGLang isn't viable here.")}
        if not self._gpu_available():
            return {"success": False,
                    "error": "NVIDIA CDI spec not found (/etc/cdi/nvidia.yaml) — "
                             "GPU passthrough isn't configured for the container "
                             "runtime. Run: sudo nvidia-ctk cdi generate "
                             "--output=/etc/cdi/nvidia.yaml"}

        if mode == "cli":
            self._spawn(self._run_cli_install(detail, model, hf_token))
        else:
            self._spawn(self._run_rest_install(detail, model, hf_token))
        return {"success": True, "started": True, "mode": mode, "model": model}

    # CLI mode

    def _launch_cmd(self, model: str) -> List[str]:
        return ["python3", "-m", "sglang.launch_server",
                "--model-path", model, "--host", "0.0.0.0", "--port", str(PORT)]

    async def _run_cli_install(self, rt: str, model: str, hf_token: Optional[str]):
        # Model is baked into the container's command line, so a model change
        # means recreate: remove any existing container first.
        pre: List[List[str]] = []
        if await self._cli_container_exists(rt):
            pre.append([rt, "rm", "-f", CONTAINER_NAME])
        cmd = [rt, "run", "-d", "--name", CONTAINER_NAME,
               "--restart", "unless-stopped",
               "-p", f"0.0.0.0:{PORT}:{PORT}",
               "-v", f"{VOLUME_NAME}:/root/.cache/huggingface",
               "--shm-size", "8g",
               "--device", "nvidia.com/gpu=all",
               "--security-opt", "label=disable"]
        if hf_token:
            cmd += ["-e", f"HF_TOKEN={hf_token}"]
        cmd += [SGLANG_IMAGE] + self._launch_cmd(model)

        self._job_start("install", f"{rt} run … {SGLANG_IMAGE} (model {model})",
                        model=model)
        try:
            for c in pre + [cmd]:
                proc = await asyncio.create_subprocess_exec(
                    *c, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    self._job_log(raw.decode(errors="replace").rstrip())
                rc = await proc.wait()
                if rc != 0:
                    self._job_finish(False, f"[exit {rc}] {' '.join(c[:3])}")
                    return
            self._job_log("Container started — model weights download on first "
                          "boot; watch readiness on the status badge.")
            self._job_finish(True, None)
        except Exception as e:
            logger.error(f"SGLang CLI install failed: {e}")
            self._job_finish(False, str(e))

    async def _cli_container_exists(self, rt: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                rt, "ps", "-a", "--filter", f"name=^{CONTAINER_NAME}$",
                "--format", "{{.Names}}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await proc.communicate()
            return CONTAINER_NAME in out.decode().split()
        except Exception:
            return False

    # Socket mode (Docker-compatible REST)

    async def _run_rest_install(self, sock: str, model: str,
                                hf_token: Optional[str]):
        self._job_start("install", f"REST via {sock} (model {model})", model=model)
        try:
            import httpx
        except Exception:
            self._job_finish(False, "httpx not available in image")
            return
        transport = httpx.AsyncHTTPTransport(uds=sock)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://d",
                                         timeout=None) as cx:
                # Model change = recreate; drop any existing container.
                r = await cx.get("/containers/json",
                                 params={"all": "true",
                                         "filters": json.dumps(
                                             {"name": [CONTAINER_NAME]})})
                if r.status_code == 200 and any(
                        CONTAINER_NAME in [n.lstrip("/") for n in (c.get("Names") or [])]
                        for c in r.json()):
                    self._job_log("Removing existing 'sglang' container…")
                    await cx.delete(f"/containers/{CONTAINER_NAME}",
                                    params={"force": "true"})

                self._job_log(f"Pulling image {SGLANG_IMAGE} (large — several GB)…")
                async with cx.stream("POST", "/images/create",
                                     params={"fromImage": SGLANG_IMAGE}) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode(errors="replace")
                        self._job_finish(False,
                                         f"image pull failed: {resp.status_code} {body}")
                        return
                    last = None
                    async for line in resp.aiter_lines():
                        msg = self._compact_progress(line)
                        if msg and msg != last:
                            self._job_log(msg)
                            last = msg

                self._job_log("Creating 'sglang' container…")
                cfg = self._rest_create_config(model, hf_token)
                r = await cx.post("/containers/create",
                                  params={"name": CONTAINER_NAME}, json=cfg)
                if r.status_code != 201:
                    self._job_finish(False, f"create failed: {r.status_code} {r.text}")
                    return
                cid = r.json().get("Id", CONTAINER_NAME)
                r = await cx.post(f"/containers/{cid}/start")
                ok = r.status_code in (204, 304)
                if ok:
                    self._job_log("Started — model weights download on first boot; "
                                  "the server answers on :30000 once loaded.")
                self._job_finish(ok, None if ok else f"start failed: {r.status_code} {r.text}")
        except Exception as e:
            logger.error(f"SGLang REST install failed: {e}")
            self._job_finish(False, str(e))

    def _rest_create_config(self, model: str, hf_token: Optional[str]) -> Dict[str, Any]:
        host_config: Dict[str, Any] = {
            "PortBindings": {f"{PORT}/tcp": [{"HostIp": "0.0.0.0",
                                              "HostPort": str(PORT)}]},
            "RestartPolicy": {"Name": "unless-stopped"},
            "Binds": [f"{VOLUME_NAME}:/root/.cache/huggingface"],
            "ShmSize": SHM_BYTES,
            "SecurityOpt": ["label=disable"],
            "Devices": [{"PathOnHost": "nvidia.com/gpu=all",
                         "PathInContainer": "nvidia.com/gpu=all",
                         "CgroupPermissions": "rwm"}],
        }
        cfg: Dict[str, Any] = {
            "Image": SGLANG_IMAGE,
            "Cmd": self._launch_cmd(model),
            "ExposedPorts": {f"{PORT}/tcp": {}},
            "HostConfig": host_config,
        }
        if hf_token:
            cfg["Env"] = [f"HF_TOKEN={hf_token}"]
        return cfg

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
        if self._url:
            return self._url
        mode, _ = detect_runtime()
        if mode == "cli" or self._port_open("127.0.0.1"):
            return f"http://127.0.0.1:{PORT}"
        return f"http://10.0.2.2:{PORT}"

    @staticmethod
    def _gpu_available() -> bool:
        return os.path.exists(CDI_SPEC)

    def _port_open(self, host: Optional[str] = None, timeout: float = 0.4) -> bool:
        hosts = [host] if host else ["127.0.0.1", "10.0.2.2"]
        if self._url:
            m = re.search(r"://([^:/]+)", self._url)
            if m:
                hosts = [m.group(1)] + hosts
        for h in hosts:
            try:
                with socket.create_connection((h, PORT), timeout=timeout):
                    return True
            except Exception:
                continue
        return False

    def _container_exists_sync(self, mode: Optional[str],
                               detail: Optional[str]) -> bool:
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
        # Socket mode: cheap running-port heuristic (same trade-off as Ollama).
        if mode == "socket":
            return self._port_open()
        return False

    def _model_info(self) -> Dict[str, Any]:
        """Ask the running server which model it serves (native endpoint,
        falling back to the OpenAI-compatible model list)."""
        base = self._reachable_base()
        for url, extract in (
                (f"{base}/get_model_info",
                 lambda d: d.get("model_path")),
                (f"{base}/v1/models",
                 lambda d: (d.get("data") or [{}])[0].get("id"))):
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    data = json.loads(r.read().decode())
                mp = extract(data)
                if mp:
                    return {"model_path": mp}
            except Exception:
                continue
        return {}
