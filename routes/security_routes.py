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

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List

import yaml
from fastapi import FastAPI

from modules.nuki_controller import (
    LOCK_ACTIONS, NukiBridgeClient, NukiError, NukiLockDevice, discover_bridges,
)

logger = logging.getLogger("zbm.security")

CONFIG_PATH = "./config/config.yaml"
# Last bridge /list snapshot persisted across restarts (mirrors the AC
# status store) so the lock modal/device list render instantly on boot.
LOCKS_STORE_PATH = "./data/nuki_locks_cache.json"
LOCKS_STORE_MIN_WRITE_SEC = 30.0

# Which Matter-commissioned locks belong to which provider tab, matched
# case-insensitively against the device's manufacturer string. Locks that
# match no provider still appear in the main device list — they just don't
# claim a Security sub-tab.
MATTER_MFR_FILTERS = {
    "nuki": ("nuki",),
    "yale": ("yale", "august", "assa abloy"),
}


def _load_security_cfg() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("security") or {}


def _load_nuki_cfg() -> Dict[str, Any]:
    return _load_security_cfg().get("nuki") or {}


def _load_yale_cfg() -> Dict[str, Any]:
    return _load_security_cfg().get("yale") or {}


def register_security_routes(app: FastAPI, get_matter_bridge=None,
                             get_zigbee_service=None):

    # Last bridge /list result — serves /api/devices without blocking on a
    # slow/offline bridge (same pattern as ac_routes' status cache) — plus
    # the NukiLockDevice registry the automation engine reads.
    state: Dict[str, Any] = {"bridge_cache": None, "probe": None,
                             "poll_task": None}
    lock_devices: Dict[str, NukiLockDevice] = {}
    BRIDGE_LIST_MAX_AGE = 20.0
    POLL_DEFAULT_SEC = 30.0

    def _bridge_client() -> NukiBridgeClient:
        return NukiBridgeClient(_load_nuki_cfg().get("bridge") or {})

    def _bridge_channel_on(cfg: Dict[str, Any]) -> bool:
        return bool(cfg.get("enabled")) and \
            bool((cfg.get("bridge") or {}).get("enabled", True))

    async def _send_bridge_action(nuki_id, action: str, device_type) -> dict:
        res = await _bridge_client().lock_action(nuki_id, action,
                                                 device_type or 0)
        _spawn_bridge_probe()   # pick up the real post-action state
        return {"success": bool(res.get("success")),
                "battery_critical": res.get("batteryCritical")}

    def _sync_lock_registry(locks: List[Dict[str, Any]]) -> Dict[str, Dict]:
        """Upsert NukiLockDevice objects from a fresh /list snapshot;
        returns {ieee: changed_state} for devices whose state moved."""
        changes: Dict[str, Dict] = {}
        seen = set()
        for lock in locks:
            ieee = f"nuki_{lock.get('nuki_id')}"
            seen.add(ieee)
            dev = lock_devices.get(ieee)
            if dev is None:
                lock_devices[ieee] = NukiLockDevice(lock, _send_bridge_action)
            else:
                changed = dev.update_from_lock(lock)
                if changed:
                    changes[ieee] = changed
        for ieee in [i for i in lock_devices if i not in seen]:
            del lock_devices[ieee]
        return changes

    def _emit_changes(changes: Dict[str, Dict]) -> None:
        """Feed lock state changes to the automation engine (lock/unlock
        triggers, door-sensor conditions, battery alerts...)."""
        if not changes:
            return
        svc = get_zigbee_service() if get_zigbee_service else None
        engine = getattr(svc, "automation", None) if svc else None
        if not engine:
            return
        for ieee, changed in changes.items():
            logger.info(f"Nuki: {ieee} changed {changed}")
            asyncio.create_task(engine.evaluate(ieee, changed))

    async def _fetch_bridge_locks() -> List[Dict[str, Any]]:
        client = _bridge_client()
        if not client.configured:
            return []
        devices = await client.list_devices()
        locks = [client.normalize_device(d) for d in devices]
        state["bridge_cache"] = (time.monotonic(), locks)
        _emit_changes(_sync_lock_registry(locks))
        _persist_locks(locks)
        return locks

    def _persist_locks(locks: List[Dict[str, Any]]) -> None:
        now = time.monotonic()
        if now - state.get("last_persist", 0.0) < LOCKS_STORE_MIN_WRITE_SEC:
            return
        state["last_persist"] = now
        try:
            os.makedirs(os.path.dirname(LOCKS_STORE_PATH), exist_ok=True)
            tmp = LOCKS_STORE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"ts": time.time(),
                           "locks": [{k: v for k, v in l.items()
                                      if k != "cached"} for l in locks]}, f)
            os.replace(tmp, LOCKS_STORE_PATH)
        except OSError as e:
            logger.debug(f"Nuki locks store write failed: {e}")

    def _load_persisted_locks() -> None:
        try:
            with open(LOCKS_STORE_PATH, "r") as f:
                raw = json.load(f) or {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        locks = raw.get("locks") or []
        if not locks:
            return
        age = max(0.0, time.time() - float(raw.get("ts") or 0))
        for lock in locks:
            lock["cached"] = True   # tell the UI this may be stale
        state["bridge_cache"] = (time.monotonic() - age, locks)
        _sync_lock_registry(locks)   # pre-populate the device registry
        logger.info(f"Nuki: restored {len(locks)} lock(s) from cache "
                    f"({age:.0f}s old)")

    _load_persisted_locks()

    def _cached_bridge_locks():
        c = state["bridge_cache"]
        return (time.monotonic() - c[0], c[1]) if c else None

    def _spawn_bridge_probe() -> None:
        t = state.get("probe")
        if t is not None and not t.done():
            return

        async def _probe():
            try:
                await _fetch_bridge_locks()
            except Exception as e:
                logger.debug(f"Nuki background list probe failed: {e}")

        state["probe"] = asyncio.create_task(_probe())

    # ── device-list + automation integration ───────────────────────────
    # Locks (never the bridge itself) surface in /api/devices like AC units
    # do, and the poller below keeps their state fresh so automations can
    # trigger on lock/unlock even with no UI open.

    async def _device_list_entries() -> list:
        cfg = _load_nuki_cfg()
        if not _bridge_channel_on(cfg) or not _bridge_client().configured:
            return []
        cached = _cached_bridge_locks()
        if cached is None or cached[0] >= BRIDGE_LIST_MAX_AGE:
            _spawn_bridge_probe()
        return [dev.to_device_list_entry() for dev in lock_devices.values()]

    app.state.nuki_device_entries = _device_list_entries

    async def _nuki_send_command(ieee: str, command: str) -> dict:
        """Hook for /api/device/command so the standard device modal and
        automation engine reach bridge locks by pseudo-ieee."""
        dev = lock_devices.get(ieee)
        if dev is None:
            return {"success": False, "error": f"Unknown Nuki lock {ieee}"}
        return await dev.send_command(command)

    app.state.nuki_send_command = _nuki_send_command

    def _register_device_getter() -> bool:
        """Expose the lock registry to the automation engine's merged
        device view (idempotent). Returns False until the service is up."""
        if state.get("engine_registered"):
            return True
        try:
            svc = get_zigbee_service() if get_zigbee_service else None
            engine = getattr(svc, "automation", None) if svc else None
            if engine and hasattr(engine, "add_device_getter"):
                engine.add_device_getter(lambda: dict(lock_devices))
                state["engine_registered"] = True
                logger.info("Nuki locks wired into automation engine")
                return True
        except Exception as e:
            logger.debug(f"Nuki automation wiring not ready: {e}")
        return False

    # Wire into the automation engine right away — the engine exists before
    # routes register, and rules must see locks even if the poller never
    # runs (e.g. bridge not configured yet, matter-only setups).
    _register_device_getter()

    async def _poll_loop():
        while True:
            interval = POLL_DEFAULT_SEC
            try:
                cfg = _load_nuki_cfg()
                interval = float(cfg.get("poll_interval_seconds")
                                 or POLL_DEFAULT_SEC)
                if _bridge_channel_on(cfg) and \
                        NukiBridgeClient(cfg.get("bridge") or {}).configured:
                    _register_device_getter()
                    await _fetch_bridge_locks()
            except Exception as e:
                logger.debug(f"Nuki poll failed: {e}")
            await asyncio.sleep(max(10.0, interval))

    def _start_poller():
        """Called from main.py's lifespan once the loop is running."""
        t = state.get("poll_task")
        if t is None or t.done():
            state["poll_task"] = asyncio.create_task(_poll_loop())

    app.state.nuki_poll_start = _start_poller

    def _channel_enabled(channel: str) -> bool:
        cfg = _load_nuki_cfg()
        return bool(cfg.get("enabled")) and \
            bool((cfg.get(channel) or {}).get("enabled", True))

    def _matter_locks(provider: str = None) -> List[Dict[str, Any]]:
        """Matter devices whose parser classified them as locks, in the
        same unified shape as bridge locks. With a provider id, only locks
        whose manufacturer matches that provider's filter are returned."""
        mfr_filter = MATTER_MFR_FILTERS.get(provider) if provider else None
        mb = get_matter_bridge() if get_matter_bridge else None
        if not mb:
            return []
        locks = []
        for dev in getattr(mb, "devices", {}).values():
            try:
                if dev.get_type() != "Lock" and "locked" not in dev.state:
                    continue
                if mfr_filter:
                    mfr = (dev.manufacturer or "").lower()
                    if not any(f in mfr for f in mfr_filter):
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

    async def _matter_lock_action(node_id: int, action: str) -> dict:
        """Send a lock verb to a Matter-commissioned lock. Matter has no
        lock'n'go, so those degrade to the closest single verb."""
        mb = get_matter_bridge() if get_matter_bridge else None
        if not mb:
            return {"success": False, "error": "Matter bridge not running"}
        cmd = {"lock_n_go": "lock",
               "lock_n_go_unlatch": "unlatch"}.get(action, action)
        return await mb.send_command(node_id, cmd)

    # ── Provider registry ───────────────────────────────────────────────

    @app.get("/api/security/providers")
    async def security_providers():
        """Registry the frontend builds its Security sub-tabs from.
        Future providers append entries here."""
        nuki = _load_nuki_cfg()
        yale = _load_yale_cfg()
        bridge = nuki.get("bridge") or {}
        matter_up = bool(get_matter_bridge and get_matter_bridge())
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
                        "available": matter_up,
                    },
                },
            },
            {
                "id": "yale",
                "name": "Yale",
                "icon": "fa-key",
                "enabled": bool(yale.get("enabled")),
                "channels": {
                    "matter": {
                        "enabled": bool((yale.get("matter") or {}).get("enabled", True)),
                        "available": matter_up,
                    },
                    # Yale/August cloud (yalexs) is deliberately absent: both
                    # backends now reject the community API key — August needs
                    # an official partner key, Yale Home OAuths only via Home
                    # Assistant. Matter is the local, credential-free path.
                    "cloud": {"enabled": False, "available": False},
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
                "locks": len(_matter_locks("nuki")),
            }
        return out

    @app.get("/api/security/nuki/locks")
    async def nuki_locks(max_age: float = 0.0):
        """max_age > 0 serves the app-side bridge cache (persisted across
        restarts) when younger than that many seconds — the lock modal uses
        it to render instantly, then refreshes live. Matter locks are always
        served from the matter bridge's in-memory registry (no probe cost)."""
        if not _load_nuki_cfg().get("enabled"):
            return {"success": True, "enabled": False, "locks": []}
        locks: List[Dict[str, Any]] = []
        errors: List[str] = []
        age_sec = 0
        if _channel_enabled("bridge"):
            try:
                cached = _cached_bridge_locks()
                if max_age > 0 and cached and cached[0] < max_age:
                    locks.extend(cached[1])
                    age_sec = round(cached[0], 1)
                    if cached[0] >= BRIDGE_LIST_MAX_AGE:
                        _spawn_bridge_probe()
                else:
                    locks.extend(await _fetch_bridge_locks())
            except Exception as e:
                errors.append(f"bridge: {str(e) or type(e).__name__}")
        if _channel_enabled("matter"):
            locks.extend(_matter_locks("nuki"))
        return {"success": True, "enabled": True, "locks": locks,
                "errors": errors, "age_sec": age_sec}

    @app.post("/api/security/nuki/locks/{lock_id}/action")
    async def nuki_lock_action(lock_id: str, data: dict):
        action = str((data or {}).get("action") or "").strip()
        if action not in LOCK_ACTIONS:
            return {"success": False,
                    "error": f"action must be one of {', '.join(LOCK_ACTIONS)}"}
        try:
            channel, _, raw_id = lock_id.partition(":")
            if channel == "bridge":
                dev = lock_devices.get(f"nuki_{raw_id}")
                if dev is not None:
                    return await dev.send_command(action)
                # Registry cold (e.g. right after startup) — resolve the
                # deviceType from a live /list and fire directly.
                for lock in await _fetch_bridge_locks():
                    if str(lock.get("nuki_id")) == raw_id:
                        return await _send_bridge_action(
                            lock["nuki_id"], action,
                            lock.get("device_type", 0))
                return {"success": False, "error": f"Unknown lock {lock_id}"}
            if channel == "matter":
                return await _matter_lock_action(int(raw_id), action)
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

    # ── Yale: status / locks / actions (Matter channel) ─────────────────
    # Cloud control via yalexs is intentionally not wired: both August and
    # Yale backends now reject the community API key (August requires an
    # official partner key; Yale Home OAuths only through Home Assistant),
    # so a standalone app cannot authenticate. Yale Assure Lock 2 / Linus
    # L2 commission over Matter instead — local and credential-free. If a
    # partner key is ever obtained, add a yale_controller module and a
    # "cloud" channel here mirroring the Nuki bridge pattern.

    def _yale_enabled() -> bool:
        cfg = _load_yale_cfg()
        return bool(cfg.get("enabled")) and \
            bool((cfg.get("matter") or {}).get("enabled", True))

    @app.get("/api/security/yale/status")
    async def yale_status():
        out: Dict[str, Any] = {"success": True, "matter": None}
        if _yale_enabled():
            mb = get_matter_bridge() if get_matter_bridge else None
            out["matter"] = {
                "ok": bool(mb and mb.is_connected()),
                "locks": len(_matter_locks("yale")),
            }
        return out

    @app.get("/api/security/yale/locks")
    async def yale_locks():
        if not _load_yale_cfg().get("enabled"):
            return {"success": True, "enabled": False, "locks": []}
        locks = _matter_locks("yale") if _yale_enabled() else []
        return {"success": True, "enabled": True, "locks": locks,
                "errors": [], "age_sec": 0}

    @app.post("/api/security/yale/locks/{lock_id}/action")
    async def yale_lock_action(lock_id: str, data: dict):
        action = str((data or {}).get("action") or "").strip()
        if action not in ("lock", "unlock", "unlatch"):
            return {"success": False,
                    "error": "action must be one of lock, unlock, unlatch"}
        try:
            channel, _, raw_id = lock_id.partition(":")
            if channel != "matter":
                return {"success": False,
                        "error": f"Unknown channel in lock id '{lock_id}'"}
            return await _matter_lock_action(int(raw_id), action)
        except Exception as e:
            logger.warning(f"Yale action '{action}' on {lock_id} failed: {e}")
            return {"success": False, "error": str(e) or type(e).__name__}

    logger.info("Security routes registered (providers: nuki, yale)")
