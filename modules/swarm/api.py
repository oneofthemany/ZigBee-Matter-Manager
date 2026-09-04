"""
Swarm Intelligence — HTTP surface.

Read-only. Every route describes what the swarm *could* do; nothing here
creates a rule or sends a command. Rule creation stays on the existing
/api/automations endpoints, which these responses are shaped to feed.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Union

from fastapi import FastAPI, HTTPException

from modules.swarm.capabilities import CAPABILITIES, PARAMS
from modules.swarm.network import describe_network, load_rooms, pairings, room_assignments
from modules.swarm.resolver import describe_device

logger = logging.getLogger("modules.swarm.api")


def register_swarm_routes(app: FastAPI,
                          automation_getter: Union[Any, Callable[[], Any]],
                          service_getter: Optional[Callable[[], Any]] = None):
    """Mount the swarm routes.

    `automation_getter` supplies the engine, whose merged registry is the only
    complete view of the network — Zigbee, Matter, Nuki and presence users all
    reach automations through it, so describing that view describes everything
    an automation can actually address.
    """

    def engine():
        return automation_getter() if callable(automation_getter) else automation_getter

    def _devices_and_names():
        e = engine()
        if not e:
            raise HTTPException(503, "Automation engine unavailable")
        return e._get_all_devices(), e._get_all_names()

    def _settings():
        if not service_getter:
            return {}
        try:
            return getattr(service_getter(), "device_settings", {}) or {}
        except Exception:
            return {}

    @app.get("/api/swarm/vocabulary", tags=["swarm"])
    async def vocabulary():
        """The capability table and tunable parameters, for rendering the UI.

        Static for the life of the process — the frontend can cache it and only
        re-fetch the per-device offers.
        """
        return {"capabilities": CAPABILITIES, "params": PARAMS}

    @app.get("/api/swarm/capabilities", tags=["swarm"])
    async def network_capabilities():
        """Every device's offers, grouped by room, with coverage counts."""
        devices, names = _devices_and_names()
        return describe_network(devices, names, _settings())

    @app.get("/api/swarm/capabilities/{ieee}", tags=["swarm"])
    async def device_offers(ieee: str):
        """One device's offers."""
        devices, names = _devices_and_names()
        dev = devices.get(ieee)
        if not dev:
            raise HTTPException(404, f"Not found: {ieee}")
        rooms = load_rooms()
        room = room_assignments(_settings()).get(ieee)
        return describe_device(ieee, dev, names.get(ieee), room,
                               rooms.get(room) if room else None)

    @app.get("/api/swarm/pairings/{ieee}", tags=["swarm"])
    async def device_pairings(ieee: str, min_confidence: str = "low",
                              limit: int = 200):
        """Every wiring this device participates in, ranked, both directions."""
        devices, names = _devices_and_names()
        if ieee not in devices:
            raise HTTPException(404, f"Not found: {ieee}")
        described = describe_network(devices, names, _settings())["devices"]
        return pairings(ieee, described, min_confidence=min_confidence, limit=limit)

    @app.get("/api/swarm/rooms", tags=["swarm"])
    async def rooms():
        """Chamber registry plus which devices sit in each."""
        devices, _ = _devices_and_names()
        registry = load_rooms()
        assigned = room_assignments(_settings())
        out = {rid: {"id": rid, "name": label, "devices": []}
               for rid, label in registry.items()}
        out["__unassigned__"] = {"id": None, "name": "Unassigned", "devices": []}
        for ieee in devices:
            key = assigned.get(ieee) or "__unassigned__"
            out.setdefault(key, {"id": key, "name": key, "devices": []})
            out[key]["devices"].append(ieee)
        return {"rooms": list(out.values())}

    logger.info("Swarm Intelligence routes registered")
