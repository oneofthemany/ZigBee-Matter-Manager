"""
API for HomeKit accessories (televisions) — a thin HTTP layer over
modules/homekit_controller.py.

Pairing is admin work: it mints long-term keys for the accessory. Paired TVs
appear in /api/devices; that hook serves cached status and refreshes in the
background so the device list never waits on a TV waking from standby.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

import yaml
from fastapi import Body, Depends, FastAPI

from modules.auth_middleware import require_scope

logger = logging.getLogger("zbm.homekit")

CONFIG_PATH = "./config/config.yaml"
# The device list polls often; a TV's state is re-read at most this often.
LIST_REFRESH_SECONDS = 30.0


def _load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def register_homekit_routes(app: FastAPI):
    from modules.homekit_controller import HomeKitController, HomeKitError

    state: Dict[str, Any] = {"controller": None, "probe": None}

    def _controller() -> HomeKitController:
        cfg = _load_config().get("homekit") or {}
        ctl = state["controller"]
        if ctl is None:
            ctl = state["controller"] = HomeKitController(cfg)
        else:
            ctl.reload(cfg)
        return ctl

    def _spawn_probe(ctl: HomeKitController) -> None:
        t = state.get("probe")
        if t is not None and not t.done():
            return

        async def _probe():
            before = {i for i in ctl.device_ids() if ctl.cached_status(i) is not None}
            try:
                await ctl.list_devices(max_age=LIST_REFRESH_SECONDS)
            except Exception as e:
                logger.warning(f"HomeKit background refresh failed: {e}")
            after = {i for i in ctl.device_ids() if ctl.cached_status(i) is not None}
            # The device table fetches once on load, before the first refresh
            # lands, so tell it when rows become available.
            if after != before:
                from routes.websocket_routes import broadcast_event
                await broadcast_event("devices_changed", {"source": "homekit"})

        state["probe"] = asyncio.create_task(_probe())

    async def _device_list_entries() -> list:
        try:
            ctl = _controller()
            if not ctl.enabled:
                return []
        except Exception as e:
            logger.warning(f"HomeKit device-list entries failed: {e}")
            return []

        ids = ctl.device_ids()
        if not ids or any((c := ctl.cached_status(i)) is None or c[0] >= LIST_REFRESH_SECONDS
                          for i in ids):
            _spawn_probe(ctl)

        entries = []
        for device_id in ids:
            cached = ctl.cached_status(device_id)
            if cached is None:
                continue
            s = cached[1]
            entries.append({
                "ieee": f"homekit_{device_id}",
                "homekit_device_id": device_id,
                "friendly_name": s.get("name") or device_id,
                "type": "Television",
                "protocol": "wifi",
                "manufacturer": s.get("manufacturer") or "HomeKit",
                "model": s.get("model") or "Television",
                "available": bool(s.get("online")),
                "state": {k: s.get(k) for k in ("power", "input_name", "mute")
                          if s.get(k) is not None},
            })
        return entries

    async def _stop():
        ctl = state["controller"]
        if ctl is not None:
            await ctl.stop()

    app.state.homekit_device_entries = _device_list_entries
    app.state.homekit_stop = _stop

    def _fail(e: Exception, what: str) -> dict:
        if not isinstance(e, HomeKitError):
            logger.error(f"HomeKit {what} failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}

    # Config

    @app.get("/api/homekit/config")
    async def get_config(_=Depends(require_scope("admin"))):
        ctl = _controller()
        return {"success": True, "enabled": ctl.enabled, "last_error": ctl.last_error}

    @app.post("/api/homekit/config")
    async def save_config(body: dict = Body(...), _=Depends(require_scope("admin"))):
        cfg = _load_config()
        section = cfg.setdefault("homekit", {})
        if "enabled" in body:
            section["enabled"] = bool(body["enabled"])
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
        ctl = _controller()
        if not ctl.enabled:
            await ctl.stop()
        return {"success": True, "enabled": ctl.enabled}

    # Discovery and pairing

    @app.get("/api/homekit/discover")
    async def discover(_=Depends(require_scope("admin"))):
        try:
            return {"success": True, "accessories": await _controller().discover()}
        except Exception as e:
            return _fail(e, "discovery")

    @app.post("/api/homekit/pair/start")
    async def pair_start(body: dict = Body(...), _=Depends(require_scope("admin"))):
        try:
            await _controller().start_pairing(str(body.get("id") or ""))
            return {"success": True}
        except Exception as e:
            return _fail(e, "pair start")

    @app.post("/api/homekit/pair/finish")
    async def pair_finish(body: dict = Body(...), _=Depends(require_scope("admin"))):
        try:
            status = await _controller().finish_pairing(
                str(body.get("id") or ""), str(body.get("pin") or ""))
            return {"success": True, "status": status}
        except Exception as e:
            return _fail(e, "pair finish")

    @app.delete("/api/homekit/devices/{device_id}")
    async def unpair(device_id: str, _=Depends(require_scope("admin"))):
        try:
            confirmed = await _controller().unpair(device_id)
            return {"success": True, "accessory_confirmed": confirmed}
        except Exception as e:
            return _fail(e, "unpair")

    # Devices

    @app.get("/api/homekit/devices")
    async def list_devices(max_age: float = LIST_REFRESH_SECONDS):
        try:
            return {"success": True, "devices": await _controller().list_devices(max_age)}
        except Exception as e:
            return _fail(e, "list")

    @app.get("/api/homekit/devices/{device_id}/status")
    async def device_status(device_id: str, max_age: float = 15.0):
        try:
            return {"success": True, "status": await _controller().status(device_id, max_age)}
        except Exception as e:
            return _fail(e, f"status for {device_id}")

    @app.post("/api/homekit/devices/{device_id}/control")
    async def device_control(device_id: str, body: dict = Body(...)):
        try:
            return {"success": True, "status": await _controller().control(device_id, body)}
        except Exception as e:
            return _fail(e, f"control for {device_id}")
