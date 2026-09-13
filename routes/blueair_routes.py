"""
API for Blueair air purifiers / humidifiers — a thin HTTP layer over
modules/blueair_controller.py.

The account password is write-only: it is accepted by the config endpoint and
stored in the gitignored secrets file, never returned. Devices appear in
/api/devices like AC units; that hook serves cached status only and refreshes in
the background, because every live read is a cloud round trip.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

import yaml
from fastapi import Body, Depends, FastAPI, HTTPException

from modules.auth_middleware import require_scope

logger = logging.getLogger("zbm.blueair")

CONFIG_PATH = "./config/config.yaml"


def _load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def register_blueair_routes(app: FastAPI):
    from modules.blueair_controller import (
        REGIONS, BlueairController, BlueairError,
        credentials_status, resolve_credentials, save_credentials,
    )

    state: Dict[str, Any] = {"controller": None, "probe": None}

    def _controller() -> BlueairController:
        cfg = _load_config().get("blueair") or {}
        ctl = state["controller"]
        if ctl is None:
            ctl = state["controller"] = BlueairController(cfg)
        else:
            ctl.reload(cfg)
        return ctl

    def _spawn_probe(ctl: BlueairController) -> None:
        """One background refresh at a time; the strong ref keeps the task
        alive until it finishes."""
        t = state.get("probe")
        if t is not None and not t.done():
            return

        async def _probe():
            try:
                await ctl.list_devices()
            except Exception as e:
                logger.debug(f"Blueair background refresh failed: {e}")

        state["probe"] = asyncio.create_task(_probe())

    async def _device_list_entries() -> list:
        try:
            ctl = _controller()
            if not ctl.enabled or not credentials_status()["configured"]:
                return []
        except Exception as e:
            logger.warning(f"Blueair device-list entries failed: {e}")
            return []

        ids = ctl.device_ids()
        if not ids or any((c := ctl.cached_status(i)) is None or c[0] >= ctl.poll_seconds
                          for i in ids):
            _spawn_probe(ctl)

        entries = []
        for device_id in ids:
            cached = ctl.cached_status(device_id)
            if cached is None:
                continue
            s = cached[1]
            entries.append({
                "ieee": f"blueair_{device_id}",
                "blueair_device_id": device_id,
                "friendly_name": s.get("name") or device_id,
                "type": "AirPurifier",
                "protocol": "wifi",
                "manufacturer": "Blueair",
                "model": s.get("model") or "Blueair",
                "available": bool(s.get("online")),
                "state": {k: s.get(k) for k in
                          ("power", "fan_speed_pct", "auto", "pm2_5", "filter_usage_pct")
                          if s.get(k) is not None},
            })
        return entries

    app.state.blueair_device_entries = _device_list_entries

    # Account / config

    @app.get("/api/blueair/config")
    async def get_config(_=Depends(require_scope("admin"))):
        ctl = _controller()
        return {
            "success": True,
            **credentials_status(),
            "enabled": ctl.enabled,
            "region": ctl.region,
            "regions": list(REGIONS),
            "poll_interval_seconds": ctl.poll_seconds,
            "last_error": ctl.last_error,
        }

    @app.post("/api/blueair/config")
    async def save_config(body: dict = Body(...), _=Depends(require_scope("admin"))):
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "").strip()
        # Blank password keeps the stored one; a new username needs a password.
        if password:
            if credentials_status()["source"] == "environment":
                raise HTTPException(409, "Credentials come from the environment "
                                         "(ZMM_BLUEAIR_USERNAME/PASSWORD); unset those to edit them here.")
            try:
                save_credentials(username or resolve_credentials()[0], password)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            except OSError as e:
                raise HTTPException(500, f"Could not write the secrets file: {e}") from e
        elif username and username != resolve_credentials()[0]:
            raise HTTPException(400, "Enter the password for the new account")

        cfg = _load_config()
        section = cfg.setdefault("blueair", {})
        if "enabled" in body:
            section["enabled"] = bool(body["enabled"])
        if body.get("region"):
            region = str(body["region"]).lower()
            if region not in REGIONS:
                raise HTTPException(400, f"region must be one of {list(REGIONS)}")
            section["region"] = region
        if body.get("poll_interval_seconds"):
            section["poll_interval_seconds"] = int(body["poll_interval_seconds"])
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

        ctl = _controller()
        await ctl.reset()
        return {"success": True, **credentials_status(), "enabled": ctl.enabled,
                "region": ctl.region}

    @app.post("/api/blueair/test")
    async def test_login(body: dict = Body(default={}), _=Depends(require_scope("admin"))):
        """Log in and list devices. Blank fields fall back to what is stored."""
        stored_user, stored_pass = resolve_credentials()
        username = str(body.get("username") or "").strip() or stored_user
        password = str(body.get("password") or "").strip() or stored_pass
        region = str(body.get("region") or _controller().region).lower()
        if not (username and password):
            return {"success": False, "error": "Enter the Blueair account email and password"}
        if region not in REGIONS:
            return {"success": False, "error": f"region must be one of {list(REGIONS)}"}
        try:
            devices = await _controller().test_login(username, password, region)
            return {"success": True, "devices": devices}
        except BlueairError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"Blueair test failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # Devices

    @app.get("/api/blueair/devices")
    async def list_devices(max_age: float = None):
        try:
            return {"success": True, "devices": await _controller().list_devices(max_age)}
        except BlueairError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"Blueair list failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.get("/api/blueair/devices/{device_id}/status")
    async def device_status(device_id: str, max_age: float = None):
        try:
            return {"success": True, "status": await _controller().status(device_id, max_age)}
        except BlueairError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"Blueair status failed for {device_id}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.post("/api/blueair/devices/{device_id}/control")
    async def device_control(device_id: str, body: dict = Body(...)):
        try:
            return {"success": True, "status": await _controller().control(device_id, body)}
        except BlueairError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"Blueair control failed for {device_id}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
