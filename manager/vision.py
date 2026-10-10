"""
Object-detection sidecar lifecycle (manager/app_sidecar.py): runs from the
app's image as ``python -m vision``, on the Coral when one is ready, otherwise
the CPU. The app pushes the camera list and reads results over loopback. See
docs/vision.md §Sidecar.

Standalone by design: the manager never imports from modules/.
"""
from typing import Any, Dict, List

from manager import accelerators
from manager.app_sidecar import AppSidecar

BACKEND_ENV = "ZMM_VISION_BACKEND"


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


_sidecar = AppSidecar("vision", "Object detection", variant_env=BACKEND_ENV,
                      variant=lambda: {**hardware(), "name": hardware()["backend"]})

CONTAINER = _sidecar.container
_SVC_TRIGGER, _SVC_STATUS = _sidecar.svc_trigger, _sidecar.svc_status
enabled, service_status, request_service = _sidecar.enabled, _sidecar.service_status, _sidecar.request_service
status, enable, disable, restart, ensure = (_sidecar.status, _sidecar.enable, _sidecar.disable,
                                            _sidecar.restart, _sidecar.ensure)
