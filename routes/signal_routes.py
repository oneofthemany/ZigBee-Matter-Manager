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
