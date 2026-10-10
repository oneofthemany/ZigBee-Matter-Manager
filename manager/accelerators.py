"""
What this host can run object detection on: a Coral Edge TPU, a GPU, or just
the CPU. Read from sysfs, which a container sees read-only, so the probe needs
no privileges and changes nothing. See docs/vision.md §Hardware.

Seeing a device on the bus is not the same as being able to use it: an M.2
Coral needs the host's gasket/apex driver bound before /dev/apex_0 exists, so
each device reports its driver as well as its presence.

Standalone by design: the manager never imports from modules/.
"""
import glob
import json
import os
from typing import Any, Dict, List, Optional

SYS = os.environ.get("ZMM_SYSFS_ROOT", "/sys")
DEV = os.environ.get("ZMM_DEV_ROOT", "/dev")
PROC = os.environ.get("ZMM_PROC_ROOT", "/proc")

# (vendor, device) -> name. Global Unichip makes the Edge TPU's PCIe ASIC.
CORAL_PCI = {("0x1ac1", "0x089a"): "Coral Edge TPU (M.2 / Mini PCIe)"}
# The USB stick enumerates as 1a6e:089a until its firmware loads, then 18d1:9302.
CORAL_USB = {("1a6e", "089a"), ("18d1", "9302")}
GPU_VENDORS = {"0x10de": "nvidia", "0x8086": "intel", "0x1002": "amd"}
HAILO_VENDOR = "0x1e60"

_DATA_DIR = os.environ.get("ZMM_DATA_DIR") or os.environ.get("DATA_DIR") \
    or "/opt/.zigbee-matter-manager"
_CORAL_DIR = os.path.join(_DATA_DIR, "data", "coral")
# The M.2 driver is built and loaded on the host by scripts/coral_driver.sh.
_DRV_TRIGGER = os.path.join(_CORAL_DIR, "driver_action")
_DRV_STATUS = os.path.join(_CORAL_DIR, "driver_status.json")
_THERMAL = os.path.join(_CORAL_DIR, "thermal")
THROTTLE_RANGE = (50, 85)                    # °C; 85 is the driver's own default


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _driver(dev_dir: str) -> Optional[str]:
    try:
        return os.path.basename(os.readlink(os.path.join(dev_dir, "driver")))
    except OSError:
        return None


def _millic(path: str) -> Optional[float]:
    try:
        return round(int(_read(path)) / 1000, 1)
    except ValueError:
        return None


def _coral_thermal() -> Optional[Dict[str, Any]]:
    """Chip temperature and the points where the driver slows its clock."""
    d = os.path.join(SYS, "class/apex/apex_0")
    temp = _millic(f"{d}/temp")
    if temp is None:
        return None
    trips = [_millic(f"{d}/trip_point{i}_temp") for i in range(3)]
    return {"temp_c": temp, "throttle_at_c": trips, "shutdown_at_c": _millic(f"{d}/hw_temp_warn2"),
            # 0 = full speed; each trip point passed halves the clock again.
            "throttle_step": sum(1 for t in trips if t is not None and temp >= t)}


def driver_status() -> Dict[str, Any]:
    """The host helper's last report on the Coral driver (never raises)."""
    try:
        with open(_DRV_STATUS) as f:
            data = json.load(f)
        return {"known": True, **data, "pending": os.path.isfile(_DRV_TRIGGER)}
    except (OSError, ValueError):
        return {"known": False, "installed": False, "pending": os.path.isfile(_DRV_TRIGGER),
                "detail": "not checked yet — needs the host helper (install_watcher.sh)"}


def request_driver(action: str) -> Dict[str, Any]:
    """Ask the host helper to install, remove or re-check the driver."""
    if action not in ("install", "remove", "check"):
        return {"success": False, "error": "action must be install|remove|check"}
    try:
        os.makedirs(_CORAL_DIR, exist_ok=True)
        with open(_DRV_TRIGGER, "w") as f:
            f.write(action)
        return {"success": True, "message": f"Coral driver {action} requested"}
    except OSError as e:
        return {"success": False, "error": str(e)}


def set_throttle(celsius: Any) -> Dict[str, Any]:
    """Where the chip starts slowing down; the host helper applies it."""
    try:
        c = int(celsius)
    except (TypeError, ValueError):
        return {"success": False, "error": "temperature must be a whole number of °C"}
    lo, hi = THROTTLE_RANGE
    if not lo <= c <= hi:
        return {"success": False, "error": f"temperature must be {lo}–{hi} °C"}
    try:
        os.makedirs(_CORAL_DIR, exist_ok=True)
        with open(_THERMAL, "w") as f:
            f.write(str(c))
    except OSError as e:
        return {"success": False, "error": str(e)}
    return request_driver("check")


def _pci() -> List[Dict[str, Any]]:
    out = []
    for d in sorted(glob.glob(os.path.join(SYS, "bus/pci/devices/*"))):
        vendor, device, cls = _read(f"{d}/vendor"), _read(f"{d}/device"), _read(f"{d}/class")
        addr, driver = os.path.basename(d), _driver(d)
        if (vendor, device) in CORAL_PCI:
            nodes = sorted(glob.glob(os.path.join(DEV, "apex_*")))
            out.append({"kind": "coral", "bus": "pci", "address": addr, "name": CORAL_PCI[(vendor, device)],
                        "driver": driver, "ready": driver == "apex",
                        "device_nodes": [os.path.basename(n) for n in nodes],
                        "thermal": _coral_thermal(), "host_driver": driver_status(),
                        "note": None if driver == "apex" else
                        "The card is on the bus but the host's gasket/apex driver isn't loaded, so "
                        "nothing can use it yet."})
        elif vendor == HAILO_VENDOR:
            out.append({"kind": "hailo", "bus": "pci", "address": addr, "name": "Hailo AI accelerator",
                        "driver": driver, "ready": bool(driver), "note": None})
        elif vendor in GPU_VENDORS and cls.startswith("0x03"):          # display controller
            kind = GPU_VENDORS[vendor]
            cdi = os.path.exists("/etc/cdi/nvidia.yaml") if kind == "nvidia" else None
            out.append({"kind": f"{kind}_gpu", "bus": "pci", "address": addr,
                        "name": {"nvidia": "NVIDIA GPU", "intel": "Intel graphics", "amd": "AMD graphics"}[kind],
                        "driver": driver, "ready": bool(driver) and cdi is not False, "cdi": cdi,
                        "note": "No NVIDIA container toolkit (CDI) spec found, so containers can't be "
                                "given the GPU." if cdi is False else None})
    return out


def _usb() -> List[Dict[str, Any]]:
    out = []
    for d in sorted(glob.glob(os.path.join(SYS, "bus/usb/devices/*"))):
        ids = (_read(f"{d}/idVendor"), _read(f"{d}/idProduct"))
        if ids in CORAL_USB:
            out.append({"kind": "coral", "bus": "usb", "address": os.path.basename(d),
                        "name": "Coral USB Accelerator", "driver": None, "ready": True,
                        # USB 2 works but is several times slower per inference.
                        "note": None if _read(f"{d}/speed") in ("5000", "10000") else
                        "On a USB 2 port; a USB 3 port is much faster."})
    return out


def _cpu() -> Dict[str, Any]:
    info = _read(os.path.join(PROC, "cpuinfo"))
    flags = next((ln.split(":", 1)[1].split() for ln in info.splitlines()
                  if ln.lower().startswith(("flags", "features"))), [])
    model = next((ln.split(":", 1)[1].strip() for ln in info.splitlines()
                  if ln.lower().startswith("model name")), "")
    return {"cores": os.cpu_count() or 1, "model": model,
            "avx2": "avx2" in flags, "neon": "asimd" in flags or "neon" in flags}


# Best first. A device that is present but not ready is never chosen.
PREFERENCE = ("coral", "hailo", "nvidia_gpu", "intel_gpu", "amd_gpu")


def probe() -> Dict[str, Any]:
    """Everything found, and which backend detection should use. Never raises."""
    try:
        devices = _pci() + _usb()
    except Exception as e:                                # noqa: BLE001
        return {"devices": [], "cpu": _cpu(), "recommended": "cpu", "error": str(e)}
    ready = {d["kind"] for d in devices if d["ready"]}
    recommended = next((k for k in PREFERENCE if k in ready), "cpu")
    blocked = [d for d in devices if not d["ready"]]
    return {"devices": devices, "cpu": _cpu(), "recommended": recommended,
            # What would be better than the choice, if it were set up.
            "could_use": next((d["kind"] for d in blocked
                               if PREFERENCE.index(d["kind"]) < (PREFERENCE.index(recommended)
                                                                 if recommended in PREFERENCE else len(PREFERENCE))),
                              None)}
