"""
Signal Inspector routes.
========================

A live, device-agnostic view of every raw signal a device emits — ZCL
attribute reports, cluster commands, Tuya datapoints, Matter attributes and
any derived state key. Backed by :mod:`modules.signal_inspector`.

Endpoints
---------
* ``GET  /api/signals/{ieee}``         — current snapshot of all signals.
* ``POST /api/signals/{ieee}/start``   — begin live streaming for this device
  (emits ``signal_inspector_update`` over the WebSocket). Returns a snapshot.
* ``POST /api/signals/{ieee}/stop``    — stop live streaming.
* ``POST /api/signals/{ieee}/clear``   — drop recorded signals (fresh baseline,
  e.g. before a learn-by-demonstration capture).
"""
import logging
from typing import Any, Dict

from fastapi import FastAPI

from modules.signal_inspector import get_signal_inspector

logger = logging.getLogger("routes.signals")


def register_signal_routes(app: FastAPI, get_zigbee_service):
    """Register signal-inspector routes."""

    def _exists(ieee: str) -> bool:
        # Matter devices flow through update_state too, so accept any device
        # the service knows about; fall back to "always allow" if the service
        # can't be queried (the inspector simply returns an empty snapshot).
        try:
            svc = get_zigbee_service()
            if svc is not None and hasattr(svc, "devices"):
                return ieee in svc.devices
        except Exception:
            pass
        return True

    @app.get("/api/signals/{ieee}")
    async def get_signals(ieee: str):
        inspector = get_signal_inspector()
        if ieee == "all":
            return {
                "success": True,
                "ieee": "all",
                "active": inspector.is_watching_all(),
                "signals": inspector.snapshot_all(),
            }
        return {
            "success": True,
            "ieee": ieee,
            "active": inspector.is_active(ieee),
            "signals": inspector.snapshot(ieee),
        }

    @app.post("/api/signals/{ieee}/start")
    async def start_signals(ieee: str):
        inspector = get_signal_inspector()
        if ieee == "all":
            inspector.start_all()
            logger.info("Signal inspection started (all devices)")
            return {
                "success": True, "ieee": "all", "active": True,
                "signals": inspector.snapshot_all(),
            }
        if not _exists(ieee):
            return {"success": False, "error": "Device not found"}
        inspector.start(ieee)
        logger.info(f"[{ieee}] Signal inspection started")
        return {
            "success": True,
            "ieee": ieee,
            "active": True,
            "signals": inspector.snapshot(ieee),
        }

    @app.post("/api/signals/{ieee}/stop")
    async def stop_signals(ieee: str):
        inspector = get_signal_inspector()
        if ieee == "all":
            inspector.stop_all()
            logger.info("Signal inspection stopped (all devices)")
            return {"success": True, "ieee": "all", "active": False}
        inspector.stop(ieee)
        logger.info(f"[{ieee}] Signal inspection stopped")
        return {"success": True, "ieee": ieee, "active": False}

    @app.post("/api/signals/{ieee}/clear")
    async def clear_signals(ieee: str):
        inspector = get_signal_inspector()
        if ieee == "all":
            # Clear every device's recorded signals.
            for _ieee in [s.get("ieee") for s in inspector.snapshot_all(limit=100000)]:
                if _ieee:
                    inspector.clear(_ieee)
            return {"success": True, "ieee": "all", "signals": []}
        inspector.clear(ieee)
        return {"success": True, "ieee": ieee, "signals": []}

    # ---- learn-by-demonstration ---------------------------------------

    def _device(ieee: str):
        try:
            svc = get_zigbee_service()
            if svc is not None and hasattr(svc, "devices"):
                return svc.devices.get(ieee)
        except Exception:
            pass
        return None

    @app.post("/api/signals/{ieee}/learn/baseline")
    async def learn_baseline(ieee: str):
        """Snapshot current signal values, then the user operates the device."""
        if ieee == "all":
            return {"success": False, "error": "Pick a specific device to learn"}
        if not _exists(ieee):
            return {"success": False, "error": "Device not found"}
        inspector = get_signal_inspector()
        inspector.start(ieee)                 # ensure we're capturing
        n = inspector.mark_baseline(ieee)
        logger.info(f"[{ieee}] Learn baseline set ({n} signals)")
        return {"success": True, "ieee": ieee, "baseline_signals": n}

    @app.get("/api/signals/{ieee}/learn/diff")
    async def learn_diff(ieee: str):
        """Return the signals that moved since the baseline, ranked."""
        inspector = get_signal_inspector()
        if not inspector.has_baseline(ieee):
            return {"success": False, "error": "No baseline — start learning first"}
        return {"success": True, "ieee": ieee, "changes": inspector.diff(ieee)}

    @app.post("/api/signals/{ieee}/learn/map")
    async def learn_map(ieee: str, data: Dict[str, Any] = None):
        """Persist a friendly mapping for a demonstrated signal.

        Body: {state_key, friendly_name, scale?, unit?, device_class?, invert?}
        Mappings are keyed by the literal state key (``state:<key>``), which
        uniformly covers ZCL attributes, Tuya datapoints and derived keys.
        """
        data = data or {}
        friendly = (data.get("friendly_name") or "").strip()

        from modules.device_profiles import get_profile_store, _to_int

        # --- command -> action mapping ---------------------------------
        # A demonstrated button command becomes a named `action` (the z2m/ZHA
        # convention automations trigger on). Keyed by ep/cluster/command.
        if data.get("command"):
            if not friendly:
                return {"success": False, "error": "action name required"}
            ep = _to_int(data.get("endpoint"))
            cl = _to_int(data.get("cluster"))
            cmd = _to_int(data.get("item"))
            if ep is None or cl is None or cmd is None:
                return {"success": False, "error": "endpoint, cluster and item required for a command"}
            raw_key = f"cmd:{ep}/{cl}/{cmd}"
            get_profile_store().set_ieee_mapping(ieee, raw_key, friendly)
            logger.info(f"[{ieee}] Learned action {raw_key} -> {friendly!r}")
            return {"success": True, "ieee": ieee, "raw_key": raw_key,
                    "friendly_name": friendly, "kind": "action"}

        state_key = (data.get("state_key") or "").strip()
        if not state_key:
            return {"success": False, "error": "state_key required (map a value signal, not a command)"}
        if not friendly:
            return {"success": False, "error": "friendly_name required"}

        try:
            scale = float(data.get("scale") or 1)
        except (TypeError, ValueError):
            scale = 1.0
        unit = str(data.get("unit") or "")
        device_class = str(data.get("device_class") or "")
        invert = bool(data.get("invert", False))

        raw_key = f"state:{state_key}"
        get_profile_store().set_ieee_mapping(
            ieee, raw_key, friendly,
            scale=scale, unit=unit, device_class=device_class, invert=invert,
        )
        logger.info(f"[{ieee}] Learned mapping {state_key!r} -> {friendly!r}")

        # Surface the friendly key immediately by re-transforming current state.
        dev = _device(ieee)
        applied = None
        if dev is not None:
            try:
                from modules.device_profiles_apply import transform_state_with_profile
                new = transform_state_with_profile(dev, dev.state)
                added = {k: v for k, v in new.items() if k not in dev.state}
                if added and hasattr(dev, "update_state"):
                    dev.update_state(added)
                applied = new.get(friendly)
            except Exception as e:
                logger.debug(f"[{ieee}] immediate re-transform failed: {e}")

        return {
            "success": True, "ieee": ieee,
            "raw_key": raw_key, "friendly_name": friendly, "value": applied,
        }

    @app.post("/api/signals/{ieee}/learn/unmap")
    async def learn_unmap(ieee: str, data: Dict[str, Any] = None):
        data = data or {}
        from modules.device_profiles import get_profile_store
        raw_key = (data.get("raw_key") or "").strip()
        if not raw_key:
            state_key = (data.get("state_key") or "").strip()
            if not state_key:
                return {"success": False, "error": "raw_key or state_key required"}
            raw_key = f"state:{state_key}"
        ok = get_profile_store().remove_ieee_mapping(ieee, raw_key)
        return {"success": ok, "ieee": ieee, "raw_key": raw_key}
