"""
security_routes.py
==================
API for physical-security providers (smart locks). Providers are described
by a registry so the frontend can build its Security tab dynamically —
adding Yale (or anything else) later means a new entry in PROVIDERS plus
its own endpoints, no frontend structural changes.

First provider: Nuki, over two channels:
  * bridge — Nuki Bridge HTTP API on the LAN (modules/nuki_controller.py)
  * matter — bridge-less locks (Smart Lock 3.0 Pro / 4th gen) commissioned
    through the app's embedded Matter server; they surface here filtered
    from the Matter device list (type "Lock").

Endpoints
---------
GET  /api/security/providers                    — registry + per-channel state
GET  /api/security/nuki/status                  — bridge /info + matter summary
GET  /api/security/nuki/locks                   — unified lock list (both channels)
POST /api/security/nuki/locks/{lock_id}/action  — {action: lock|unlock|unlatch|
                                                   lock_n_go|lock_n_go_unlatch}
POST /api/security/nuki/bridge/discover         — find bridges via Nuki cloud
POST /api/security/nuki/bridge/auth             — fetch token (button pressed
                                                   on the bridge within 30 s)

Config is read from config.yaml per request (like ac_routes) so Settings
edits apply without a restart. Lock ids are channel-namespaced:
"bridge:<nukiId>" / "matter:<node_id>".
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import yaml
from fastapi import FastAPI

from modules.nuki_controller import (
    LOCK_ACTIONS, NukiBridgeClient, NukiError, discover_bridges,
)

logger = logging.getLogger("zbm.security")

CONFIG_PATH = "./config/config.yaml"


def _load_nuki_cfg() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        cfg = yaml.safe_load(f) or {}
    return ((cfg.get("security") or {}).get("nuki")) or {}


def register_security_routes(app: FastAPI, get_matter_bridge=None):

    def _bridge_client() -> NukiBridgeClient:
        return NukiBridgeClient(_load_nuki_cfg().get("bridge") or {})

    def _channel_enabled(channel: str) -> bool:
        cfg = _load_nuki_cfg()
        return bool(cfg.get("enabled")) and \
            bool((cfg.get(channel) or {}).get("enabled", True))

    def _matter_locks() -> List[Dict[str, Any]]:
        """Matter devices whose parser classified them as locks, in the
        same unified shape as bridge locks."""
        mb = get_matter_bridge() if get_matter_bridge else None
        if not mb:
            return []
        locks = []
        for dev in getattr(mb, "devices", {}).values():
            try:
                if dev.get_type() != "Lock" and "locked" not in dev.state:
                    continue
                locked = dev.state.get("locked")
                locks.append({
                    "id": f"matter:{dev.node_id}",
                    "node_id": dev.node_id,
                    "name": dev.friendly_name,
                    "via": "matter",
                    "manufacturer": dev.manufacturer,
                    "model": dev.model,
                    "state_name": ("locked" if locked
                                   else "unlocked" if locked is not None
                                   else "unknown"),
                    "battery": dev.state.get("battery"),
                    "available": dev.is_available(),
                })
            except Exception as e:
                logger.debug(f"Security: skipping matter node: {e}")
        return locks

    # ── Provider registry ───────────────────────────────────────────────

    @app.get("/api/security/providers")
    async def security_providers():
        """Registry the frontend builds its Security sub-tabs from.
        Future providers (yale, ...) append entries here."""
        nuki = _load_nuki_cfg()
        bridge = nuki.get("bridge") or {}
        return {"success": True, "providers": [
            {
                "id": "nuki",
                "name": "Nuki",
                "icon": "fa-lock",
                "enabled": bool(nuki.get("enabled")),
                "channels": {
                    "bridge": {
                        "enabled": bool(bridge.get("enabled", True)),
                        "configured": bool(bridge.get("host") and bridge.get("token")),
                    },
                    "matter": {
                        "enabled": bool((nuki.get("matter") or {}).get("enabled", True)),
                        "available": bool(get_matter_bridge and get_matter_bridge()),
                    },
                },
            },
        ]}

    # ── Nuki: status / locks / actions ──────────────────────────────────

    @app.get("/api/security/nuki/status")
    async def nuki_status():
        out: Dict[str, Any] = {"success": True, "bridge": None, "matter": None}
        if _channel_enabled("bridge"):
            client = _bridge_client()
            if client.configured:
                try:
                    info = await client.info()
                    out["bridge"] = {
                        "ok": True,
                        "firmware": (info.get("versions") or {}).get("firmwareVersion"),
                        "server_connected": info.get("serverConnected"),
                        "uptime": info.get("uptime"),
                    }
                except Exception as e:
                    out["bridge"] = {"ok": False, "error": str(e) or type(e).__name__}
            else:
                out["bridge"] = {"ok": False, "error": "not configured"}
        if _channel_enabled("matter"):
            mb = get_matter_bridge() if get_matter_bridge else None
            out["matter"] = {
                "ok": bool(mb and mb.is_connected()),
                "locks": len(_matter_locks()),
            }
        return out

    @app.get("/api/security/nuki/locks")
    async def nuki_locks():
        if not _load_nuki_cfg().get("enabled"):
            return {"success": True, "enabled": False, "locks": []}
        locks: List[Dict[str, Any]] = []
        errors: List[str] = []
        if _channel_enabled("bridge"):
            client = _bridge_client()
            if client.configured:
                try:
                    devices = await client.list_devices()
                    locks.extend(client.normalize_device(d) for d in devices)
                except Exception as e:
                    errors.append(f"bridge: {str(e) or type(e).__name__}")
        if _channel_enabled("matter"):
            locks.extend(_matter_locks())
        return {"success": True, "enabled": True, "locks": locks, "errors": errors}

    @app.post("/api/security/nuki/locks/{lock_id}/action")
    async def nuki_lock_action(lock_id: str, data: dict):
        action = str((data or {}).get("action") or "").strip()
        if action not in LOCK_ACTIONS:
            return {"success": False,
                    "error": f"action must be one of {', '.join(LOCK_ACTIONS)}"}
        try:
            channel, _, raw_id = lock_id.partition(":")
            if channel == "bridge":
                client = _bridge_client()
                # deviceType is needed alongside nukiId; look it up from /list
                devices = await client.list_devices()
                dev = next((d for d in devices
                            if str(d.get("nukiId")) == raw_id), None)
                if not dev:
                    return {"success": False, "error": f"Unknown lock {lock_id}"}
                res = await client.lock_action(
                    dev["nukiId"], action, dev.get("deviceType", 0))
                return {"success": bool(res.get("success")),
                        "battery_critical": res.get("batteryCritical")}
            if channel == "matter":
                mb = get_matter_bridge() if get_matter_bridge else None
                if not mb:
                    return {"success": False, "error": "Matter bridge not running"}
                # Matter has no lock'n'go; degrade to the closest verb
                cmd = {"lock_n_go": "lock",
                       "lock_n_go_unlatch": "unlatch"}.get(action, action)
                return await mb.send_command(int(raw_id), cmd)
            return {"success": False,
                    "error": f"Unknown channel in lock id '{lock_id}'"}
        except Exception as e:
            logger.warning(f"Nuki action '{action}' on {lock_id} failed: {e}")
            return {"success": False, "error": str(e) or type(e).__name__}

    # ── Nuki: bridge onboarding helpers ─────────────────────────────────

    @app.post("/api/security/nuki/bridge/discover")
    async def nuki_bridge_discover():
        try:
            bridges = await discover_bridges()
            return {"success": True, "bridges": bridges}
        except Exception as e:
            return {"success": False, "error": str(e) or type(e).__name__}

    @app.post("/api/security/nuki/bridge/auth")
    async def nuki_bridge_auth(data: dict = None):
        """Fetch a token from the bridge's /auth endpoint. The user must
        press the bridge button no more than 30 s before calling this.
        Accepts an optional {host, port} so it works pre-save."""
        cfg = dict(_load_nuki_cfg().get("bridge") or {})
        for key in ("host", "port"):
            if data and data.get(key):
                cfg[key] = data[key]
        try:
            res = await NukiBridgeClient(cfg).fetch_auth_token()
            if not res.get("success"):
                return {"success": False,
                        "error": "Bridge refused — press its button and retry "
                                 "within 30 seconds"}
            return {"success": True, "token": res.get("token")}
        except Exception as e:
            logger.warning(f"Nuki bridge /auth failed: {e}")
            return {"success": False, "error": str(e) or type(e).__name__}

    logger.info("Security routes registered (providers: nuki)")
