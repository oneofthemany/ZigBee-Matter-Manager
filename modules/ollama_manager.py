"""
Ollama Container Manager
========================
Enables a local Ollama model server for ZigBee Manager's AI features, using the
rootless **podman** container pattern (Porky-style) rather than a host
`curl | sh` installer — the sane choice on an immutable / SBC host.

Everything privileged here is *user-triggered*: the install/pull endpoints exist,
but they only run when the operator explicitly clicks through in the UI, and only
when the HostCapabilityAssessor says the host can actually back a model.

Long operations (image pull, model download) run as a single background job with
a streamed log the frontend polls — so an HTTP request never blocks on a
multi-GB download. GPU passthrough flags are added only when an NVIDIA CDI spec
is present (produced by the one-time GPU setup).
"""

import asyncio
import logging
import re
import shutil
import socket
import time
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger("modules.ollama_manager")

OLLAMA_IMAGE = "docker.io/ollama/ollama:latest"
CONTAINER_NAME = "ollama"
VOLUME_NAME = "ollama-models"
PORT = 11434
CDI_SPEC = "/etc/cdi/nvidia.yaml"
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")
_MAX_LOG = 300


class OllamaManager:
    def __init__(self, assessor=None):
        from modules.llm_host import HostCapabilityAssessor
        self._assessor = assessor or HostCapabilityAssessor()
        self._job: Optional[Dict[str, Any]] = None

    # ── Status ───────────────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        rt = self._runtime()
        running = self._port_open()
        exists = self._container_exists(rt) if rt else False
        return {
            "runtime": rt,
            "running": running,
            "container_exists": exists,
            "installed": running or exists,
            "gpu_passthrough": self._gpu_available(),
            "base_url": f"http://localhost:{PORT}/v1",
            "models": self._list_models() if running else [],
            "job": self._job,
        }

    def job_status(self) -> Dict[str, Any]:
        return self._job or {"status": "idle"}

    # ── Install / start ──────────────────────────────────────────────────────

    def install(self) -> Dict[str, Any]:
        if self._busy():
            return {"success": False, "error": "A job is already running."}
        rt = self._runtime()
        if not rt:
            return {"success": False, "error": "No podman/docker runtime found."}

        verdict = self._assessor.assess().get("verdict", {})
        if not verdict.get("llm_viable"):
            return {"success": False,
                    "error": "Host assessment says a local model isn't viable here. "
                             "Use a remote provider instead."}

        if self._container_exists(rt):
            cmd = [rt, "start", CONTAINER_NAME]
        else:
            cmd = [rt, "run", "-d", "--name", CONTAINER_NAME,
                   "--restart", "unless-stopped",
                   "-p", f"127.0.0.1:{PORT}:{PORT}",
                   "-v", f"{VOLUME_NAME}:/root/.ollama"]
            if self._gpu_available():
                cmd += ["--device", "nvidia.com/gpu=all",
                        "--security-opt", "label=disable"]
            cmd += [OLLAMA_IMAGE]

        asyncio.create_task(self._run_job(cmd, "install"))
        return {"success": True, "started": True, "command": " ".join(cmd)}

    # ── Pull a model ─────────────────────────────────────────────────────────

    def pull(self, model: str) -> Dict[str, Any]:
        if self._busy():
            return {"success": False, "error": "A job is already running."}
        if not model or not _MODEL_RE.match(model):
            return {"success": False, "error": "Invalid model name."}
        rt = self._runtime()
        if not rt:
            return {"success": False, "error": "No podman/docker runtime found."}
        if not self._port_open():
            return {"success": False,
                    "error": "Ollama isn't running — install/start it first."}
        cmd = [rt, "exec", CONTAINER_NAME, "ollama", "pull", model]
        asyncio.create_task(self._run_job(cmd, "pull", model=model))
        return {"success": True, "started": True, "model": model,
                "command": " ".join(cmd)}

    # ── Background job runner ────────────────────────────────────────────────

    async def _run_job(self, cmd: List[str], action: str,
                       model: Optional[str] = None):
        self._job = {"action": action, "model": model, "status": "running",
                     "log": [], "started": time.time(), "command": " ".join(cmd)}
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    self._job["log"].append(line)
                    if len(self._job["log"]) > _MAX_LOG:
                        self._job["log"] = self._job["log"][-_MAX_LOG:]
            rc = await proc.wait()
            self._job["returncode"] = rc
            self._job["status"] = "done" if rc == 0 else "error"
            if rc != 0:
                self._job.setdefault("log", []).append(f"[exit {rc}]")
        except Exception as e:
            logger.error(f"Ollama {action} job failed: {e}")
            self._job["status"] = "error"
            self._job["log"].append(str(e))
        finally:
            self._job["finished"] = time.time()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _busy(self) -> bool:
        return bool(self._job and self._job.get("status") == "running")

    @staticmethod
    def _runtime() -> Optional[str]:
        for rt in ("podman", "docker"):
            if shutil.which(rt):
                return rt
        return None

    @staticmethod
    def _gpu_available() -> bool:
        import os
        return os.path.exists(CDI_SPEC)

    @staticmethod
    def _port_open(timeout: float = 0.4) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=timeout):
                return True
        except Exception:
            return False

    @staticmethod
    def _container_exists(rt: str) -> bool:
        try:
            import subprocess
            out = subprocess.run(
                [rt, "ps", "-a", "--filter", f"name=^{CONTAINER_NAME}$",
                 "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=6)
            return CONTAINER_NAME in out.stdout.split()
        except Exception:
            return False

    @staticmethod
    def _list_models() -> List[Dict[str, Any]]:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/api/tags", timeout=3) as r:
                import json
                data = json.loads(r.read().decode())
            return [{"name": m.get("name"),
                     "size_gb": round((m.get("size") or 0) / (1024 ** 3), 1)}
                    for m in data.get("models", [])]
        except Exception:
            return []
