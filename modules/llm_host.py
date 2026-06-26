"""
LLM Host Capability Assessor
============================
llmfit-style check: inspects the host's CPU / RAM / GPU and decides whether a
local LLM is viable here at all, which model sizes/quantisations actually fit,
and which serving backend makes sense. Read-only — it never installs or runs
anything; it only measures and recommends.

It exists so the rest of the local-AI stack (Ollama install, SGLang) is only
ever offered when the hardware can back it up. On a small SBC the honest answer
is usually "stick with the deterministic local parser", and this module says so.

No hard dependencies: psutil is used when present, with /proc fallbacks; GPU is
probed via nvidia-smi (NVIDIA) with an lspci fallback for mere presence.
"""

import logging
import os
import platform
import re
import shutil
import socket
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger("modules.llm_host")

try:
    import psutil  # noqa
    _HAS_PSUTIL = True
except Exception:  # pragma: no cover
    _HAS_PSUTIL = False

GB = 1024 ** 3

# Approx. on-disk/in-memory weight cost per 1B params, by quantisation.
# Calibrated against real Ollama tags (7B Q4_K_M ≈ 4.4 GB → ~0.63 GB/B).
_BYTES_PER_B = {
    "Q4": 0.63,   # Q4_K_M — the Ollama default, best size/quality trade-off
    "Q8": 1.10,
    "F16": 2.10,
}
# Fixed runtime overhead (server + base KV cache + activations), GB.
_RUNTIME_OVERHEAD_GB = 1.3
# Per-1B extra for KV cache at a working context (~8k tokens).
_KV_PER_B_GB = 0.18

# Curated model catalogue (Ollama tags) spanning SBC → workstation.
_CATALOG = [
    {"name": "qwen2.5:0.5b",        "params_b": 0.5,  "role": "tiny"},
    {"name": "llama3.2:1b",         "params_b": 1.0,  "role": "tiny"},
    {"name": "qwen2.5:1.5b",        "params_b": 1.5,  "role": "small"},
    {"name": "llama3.2:3b",         "params_b": 3.0,  "role": "small"},
    {"name": "qwen2.5-coder:7b",    "params_b": 7.0,  "role": "capable"},
    {"name": "llama3.1:8b",         "params_b": 8.0,  "role": "capable"},
    {"name": "qwen2.5:14b",         "params_b": 14.0, "role": "large"},
    {"name": "gemma2:27b",          "params_b": 27.0, "role": "large"},
]


class HostCapabilityAssessor:

    def assess(self) -> Dict[str, Any]:
        cpu = self._cpu()
        ram = self._ram()
        gpu = self._gpu()
        backends = self._backends(gpu)

        # Budget: a GPU path is bounded by free VRAM; CPU path by available RAM
        # (keep ~2 GB headroom for the OS / the rest of this app).
        if gpu.get("present") and gpu.get("vram_total_gb"):
            budget = round(max(gpu.get("vram_free_gb") or gpu["vram_total_gb"], 0), 1)
            target = "gpu"
        else:
            budget = round(max((ram.get("available_gb") or ram["total_gb"]) - 2.0, 0), 1)
            target = "cpu"

        recs = self._recommendations(budget, target)
        verdict = self._verdict(cpu, ram, gpu, target, budget, recs)

        return {
            "cpu": cpu,
            "ram": ram,
            "gpu": gpu,
            "backends": backends,
            "budget_gb": budget,
            "target": target,
            "recommendations": recs,
            "verdict": verdict,
        }

    # ── CPU ──────────────────────────────────────────────────────────────────

    def _cpu(self) -> Dict[str, Any]:
        logical = os.cpu_count() or 1
        physical = logical
        if _HAS_PSUTIL:
            try:
                physical = psutil.cpu_count(logical=False) or logical
            except Exception:
                pass
        model = platform.processor() or platform.machine()
        # /proc/cpuinfo gives a friendlier model name on Linux.
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.lower().startswith("model name"):
                        model = line.split(":", 1)[1].strip()
                        break
                    if line.lower().startswith("hardware"):  # ARM SBCs
                        model = line.split(":", 1)[1].strip()
        except Exception:
            pass
        return {"model": model, "arch": platform.machine(),
                "cores_physical": physical, "cores_logical": logical}

    # ── RAM ──────────────────────────────────────────────────────────────────

    def _ram(self) -> Dict[str, Any]:
        if _HAS_PSUTIL:
            try:
                vm = psutil.virtual_memory()
                return {"total_gb": round(vm.total / GB, 1),
                        "available_gb": round(vm.available / GB, 1)}
            except Exception:
                pass
        total = avail = None
        try:
            with open("/proc/meminfo") as f:
                info = {}
                for line in f:
                    k, _, v = line.partition(":")
                    info[k.strip()] = v.strip()
            total = int(info["MemTotal"].split()[0]) * 1024 / GB
            a = info.get("MemAvailable") or info.get("MemFree")
            avail = int(a.split()[0]) * 1024 / GB
        except Exception:
            pass
        return {"total_gb": round(total, 1) if total else 0.0,
                "available_gb": round(avail, 1) if avail else 0.0}

    # ── GPU ──────────────────────────────────────────────────────────────────

    def _gpu(self) -> Dict[str, Any]:
        smi = shutil.which("nvidia-smi")
        if smi:
            try:
                out = subprocess.run(
                    [smi, "--query-gpu=name,memory.total,memory.free,driver_version",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=6)
                if out.returncode == 0 and out.stdout.strip():
                    line = out.stdout.strip().splitlines()[0]
                    parts = [p.strip() for p in line.split(",")]
                    name = parts[0]
                    vram_total = float(parts[1]) / 1024 if len(parts) > 1 else None
                    vram_free = float(parts[2]) / 1024 if len(parts) > 2 else None
                    driver = parts[3] if len(parts) > 3 else None
                    cuda = self._cuda_version()
                    return {"present": True, "vendor": "NVIDIA", "name": name,
                            "vram_total_gb": round(vram_total, 1) if vram_total else None,
                            "vram_free_gb": round(vram_free, 1) if vram_free else None,
                            "driver": driver, "cuda": cuda}
            except Exception as e:
                logger.debug(f"nvidia-smi probe failed: {e}")
        # Presence-only fallback via lspci (no VRAM figures).
        lspci = shutil.which("lspci")
        if lspci:
            try:
                out = subprocess.run([lspci], capture_output=True, text=True, timeout=6)
                for line in out.stdout.splitlines():
                    if re.search(r"\b(VGA|3D|Display)\b", line):
                        vendor = ("NVIDIA" if "nvidia" in line.lower() else
                                  "AMD" if re.search(r"amd|radeon", line, re.I) else
                                  "Intel" if "intel" in line.lower() else "Unknown")
                        name = line.split(":", 2)[-1].strip()
                        return {"present": True, "vendor": vendor, "name": name,
                                "vram_total_gb": None, "vram_free_gb": None,
                                "cuda": None,
                                "note": "Detected via lspci — no VRAM/driver query "
                                        "available; treated as CPU-class for fit."}
            except Exception:
                pass
        return {"present": False}

    @staticmethod
    def _cuda_version() -> Optional[str]:
        smi = shutil.which("nvidia-smi")
        if not smi:
            return None
        try:
            out = subprocess.run([smi], capture_output=True, text=True, timeout=6)
            m = re.search(r"CUDA Version:\s*([\d.]+)", out.stdout)
            return m.group(1) if m else None
        except Exception:
            return None

    # ── Backends ─────────────────────────────────────────────────────────────

    def _backends(self, gpu: Dict) -> Dict[str, Any]:
        ollama_bin = shutil.which("ollama")
        ollama_up = self._port_open("127.0.0.1", 11434)
        container = self._container_runtime()
        usable_vram = (gpu.get("vram_total_gb") or 0) if gpu.get("present") else 0
        return {
            "ollama": {
                "installed": bool(ollama_bin) or ollama_up,
                "running": ollama_up,
                "binary": ollama_bin,
                "installable": bool(container) or self._is_linux(),
            },
            "sglang": {
                # SGLang realistically needs a CUDA GPU with real VRAM headroom.
                "viable": bool(gpu.get("present") and gpu.get("cuda")
                               and usable_vram >= 12),
                "reason": ("CUDA GPU with >=12 GB VRAM"
                           if gpu.get("present") and usable_vram >= 12
                           else "Needs an NVIDIA CUDA GPU with >=12 GB VRAM"),
                "container": container,
            },
            "container_runtime": container,
        }

    @staticmethod
    def _container_runtime() -> Optional[str]:
        for rt in ("podman", "docker"):
            if shutil.which(rt):
                return rt
        return None

    @staticmethod
    def _is_linux() -> bool:
        return platform.system() == "Linux"

    @staticmethod
    def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            return False

    # ── Fit math ─────────────────────────────────────────────────────────────

    @staticmethod
    def _estimate_gb(params_b: float, quant: str) -> float:
        weights = params_b * _BYTES_PER_B.get(quant, _BYTES_PER_B["Q4"])
        kv = params_b * _KV_PER_B_GB
        return round(weights + kv + _RUNTIME_OVERHEAD_GB, 1)

    def _recommendations(self, budget_gb: float, target: str) -> List[Dict[str, Any]]:
        out = []
        for m in _CATALOG:
            est = self._estimate_gb(m["params_b"], "Q4")
            fits = est <= budget_gb if budget_gb > 0 else False
            out.append({
                "name": m["name"],
                "params_b": m["params_b"],
                "quant": "Q4_K_M",
                "role": m["role"],
                "est_gb": est,
                "fits": fits,
                "where": "VRAM" if target == "gpu" else "RAM",
            })
        return out

    def _verdict(self, cpu: Dict, ram: Dict, gpu: Dict, target: str,
                 budget_gb: float, recs: List[Dict]) -> Dict[str, Any]:
        fitting = [r for r in recs if r["fits"]]
        best = fitting[-1] if fitting else None

        if not fitting:
            return {
                "llm_viable": False,
                "tier": "none",
                "headline": "No local LLM is viable on this host",
                "detail": (f"Only {budget_gb} GB is free for a model "
                           f"({target.upper()} path). Even the smallest catalogue "
                           f"entry needs ~{recs[0]['est_gb']} GB. Stick with the "
                           f"deterministic local parser; point at a remote model "
                           f"if you want LLM fallback."),
                "recommended_model": None,
            }

        if target == "gpu":
            tier = "gpu"
            headline = f"GPU-accelerated LLM is viable ({gpu.get('name', 'GPU')})"
            detail = (f"~{budget_gb} GB VRAM free. Largest comfortable fit: "
                      f"{best['name']} (~{best['est_gb']} GB). "
                      "Ollama is the lightest backend; SGLang only if you want "
                      "high-throughput serving and have the VRAM for it.")
        else:
            tier = "cpu" if best["params_b"] >= 7 else "cpu-small"
            headline = "CPU-only LLM is possible but slow"
            detail = (f"No usable GPU detected; running on CPU "
                      f"({cpu['cores_logical']} threads, {budget_gb} GB usable). "
                      f"Largest sensible fit: {best['name']} (~{best['est_gb']} GB) "
                      "— expect multi-second responses. The local parser stays the "
                      "fast default; treat the LLM as occasional fallback.")
        return {
            "llm_viable": True,
            "tier": tier,
            "headline": headline,
            "detail": detail,
            "recommended_model": best["name"],
            "recommended_est_gb": best["est_gb"],
        }
