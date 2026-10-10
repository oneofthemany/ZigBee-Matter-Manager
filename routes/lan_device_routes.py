"""
Setup API for Wi-Fi devices on a local API — /api/shelly/*, /api/esphome/*,
admin-only like every integration's setup. Using the devices goes through the
ordinary /api/devices and /api/device/command (device:read / device:write).
See docs/wifi-devices.md.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict

from fastapi import Depends, FastAPI, HTTPException

from modules.auth_middleware import require_scope

logger = logging.getLogger("routes.lan_devices")


def register_lan_device_routes(app: FastAPI, kind: str, get_hub: Callable[[], Any]) -> None:
    base = f"/api/{kind}"

    def _hub():
        h = get_hub()
        if h is None:
            raise HTTPException(503, f"{kind} not initialised")
        return h

    async def list_devices(_=Depends(require_scope("admin"))):
        return {"devices": _hub().list()}

    async def discover(_=Depends(require_scope("admin"))):
        return {"devices": await _hub().discover()}

    async def add(body: Dict[str, Any], _=Depends(require_scope("admin"))):
        try:
            return await _hub().add(body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    async def update(dev_id: str, body: Dict[str, Any], _=Depends(require_scope("admin"))):
        try:
            return await _hub().update(dev_id, body)
        except KeyError:
            raise HTTPException(404, "No such device")
        except ValueError as e:
            raise HTTPException(400, str(e))

    async def delete(dev_id: str, _=Depends(require_scope("admin"))):
        if not await _hub().delete(dev_id):
            raise HTTPException(404, "No such device")
        return {"success": True}

    # Fixed paths before /{dev_id}.
    app.get(base)(list_devices)
    app.post(f"{base}/discover")(discover)
    app.post(base)(add)
    app.put(f"{base}/{{dev_id}}")(update)
    app.delete(f"{base}/{{dev_id}}")(delete)
