"""
Swarm Intelligence — HTTP surface.

Read-only. Every route describes what the swarm *could* do; nothing here
creates a rule or sends a command. Rule creation stays on the existing
/api/automations endpoints, which these responses are shaped to feed.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Union

from fastapi import Body, FastAPI, HTTPException

from modules.swarm import diagnostics as dx
from modules.swarm import suggestions as sg
from modules.swarm.capabilities import CAPABILITIES, PARAMS
from modules.swarm.compiler import CompileError
from modules.swarm.network import describe_network, load_rooms, pairings, room_assignments
from modules.swarm.resolver import describe_device
from modules.swarm.stigmergy import get_stigmergy_store, validate

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

    # Suggestions
    #
    # build() walks every pattern against every room on each call. That is
    # cheap next to a device poll and always current, which matters more here
    # than caching would: a suggestion computed against a stale registry can
    # name a device that has since moved room.

    def _described():
        devices, names = _devices_and_names()
        return describe_network(devices, names, _settings())["devices"], names

    def _rules():
        e = engine()
        try:
            return e.get_rules() if e else []
        except Exception as exc:
            logger.warning(f"Could not read rules for deduplication: {exc}")
            return []

    def _build():
        described, names = _described()
        return described, sg.build(described, rules=_rules(), rooms=load_rooms(),
                                   names=names, validator=engine())

    @app.get("/api/swarm/stigmergy", tags=["swarm"])
    async def list_patterns():
        """Every stigmergy pattern loaded, plus any that failed to load."""
        store = get_stigmergy_store()
        return {"patterns": store.all(include_disabled=True), "errors": store.errors}

    @app.get("/api/swarm/stigmergy/{pattern_id}", tags=["swarm"])
    async def get_pattern(pattern_id: str):
        pattern = get_stigmergy_store().get(pattern_id)
        if not pattern:
            raise HTTPException(404, f"Unknown pattern: {pattern_id}")
        return pattern

    @app.post("/api/swarm/stigmergy/reload", tags=["swarm"])
    async def reload_patterns():
        """Re-read the pattern directories. For editing patterns without a restart."""
        store = get_stigmergy_store()
        store.reload()
        return {"success": True, "loaded": len(store.all()), "errors": store.errors}

    @app.get("/api/swarm/suggestions", tags=["swarm"])
    async def list_suggestions(room: Optional[str] = None,
                               category: Optional[str] = None,
                               status: Optional[str] = None,
                               device: Optional[str] = None,
                               include_trace: bool = False):
        """Suggestions grouped by room, with coverage and what was withheld.

        `device` narrows to suggestions this device takes part in, whether as
        the trigger or as something driven — which is what the rule builder
        asks for once a source device has been chosen.
        """
        _, built = _build()
        items = built["suggestions"]
        if device:
            items = [s for s in items
                     if any(d["ieee"] == device for d in s["devices"])]
        if room:
            items = [s for s in items if s.get("room") == room]
        if category:
            items = [s for s in items if s.get("category") == category]
        if status:
            items = [s for s in items if s["status"] == status]
        out = {"suggestions": items, "coverage": built["coverage"],
               "summary": built["summary"], "rejected": built["rejected"]}
        if include_trace:
            out["trace"] = built["trace"]
        return out

    @app.get("/api/swarm/suggestions/{suggestion_id}", tags=["swarm"])
    async def get_suggestion(suggestion_id: str):
        """One suggestion, including the rule it would create."""
        _, built = _build()
        found = sg.find(built, suggestion_id)
        if not found:
            raise HTTPException(404, f"Unknown suggestion: {suggestion_id}")
        return found

    @app.post("/api/swarm/suggestions/{suggestion_id}/apply", tags=["swarm"])
    async def apply_suggestion(suggestion_id: str,
                               body: Dict[str, Any] = Body(default={})):
        """Create the rule a suggestion describes.

        The pattern is re-matched and recompiled from the live network rather
        than trusting a rule posted back by the client — the network may have
        changed since the suggestion was offered, and a client-supplied rule is
        a client-supplied rule.
        """
        e = engine()
        if not e:
            raise HTTPException(503, "Automation engine unavailable")

        described, built = _build()
        found = sg.find(built, suggestion_id)
        if not found:
            raise HTTPException(404, f"Unknown suggestion: {suggestion_id}")
        if found["status"] == "active":
            raise HTTPException(409, f"Already built as rule {found.get('rule_id')}")

        pattern = get_stigmergy_store().get(found["pattern_id"])
        if not pattern:
            raise HTTPException(404, f"Unknown pattern: {found['pattern_id']}")

        try:
            rule = sg.recompile(pattern, found, described,
                                overrides=body.get("params"), rooms=load_rooms())
        except CompileError as exc:
            raise HTTPException(409, str(exc))

        if body.get("name"):
            rule["name"] = str(body["name"])[:100]

        result = e.add_rule(rule)
        if not result.get("success"):
            # The engine rejected a rule this layer validated, which means the
            # two disagree — worth surfacing loudly rather than as a bare 400.
            logger.error(f"Suggestion {suggestion_id} passed swarm validation but "
                         f"the engine rejected it: {result.get('error')}")
            raise HTTPException(400, result.get("error"))
        return {"success": True, "rule": result["rule"],
                "suggestion_id": suggestion_id}

    @app.get("/api/swarm/coverage", tags=["swarm"])
    async def coverage_report():
        """Which devices take part in at least one rule, and which do not."""
        _, built = _build()
        return {"coverage": built["coverage"], "summary": built["summary"]}

    # Diagnostics

    @app.get("/api/swarm/diagnostics", tags=["swarm"])
    async def diagnostics():
        """Triage report: what is broken, what is blind, and why.

        Safe on a live system — it reads state and changes nothing.
        """
        described, built = _build()
        return dx.diagnose(described, built=built, rules=_rules(),
                           rooms=load_rooms())

    @app.get("/api/swarm/explain/{pattern_id}", tags=["swarm"])
    async def explain_pattern(pattern_id: str):
        """Why one pattern matched, or did not, in every scope it was tried."""
        described, _ = _described()
        result = dx.explain(pattern_id, described, load_rooms())
        if "error" in result:
            raise HTTPException(404, result["error"])
        return result

    @app.get("/api/swarm/explain/{pattern_id}/slot/{slot}", tags=["swarm"])
    async def explain_slot(pattern_id: str, slot: str):
        """Every device that could fill one slot, ignoring room and device class.

        Answers the first question when a slot will not fill: does anything on
        this network make that offer at all?
        """
        pattern = get_stigmergy_store().get(pattern_id)
        if not pattern:
            raise HTTPException(404, f"Unknown pattern: {pattern_id}")
        spec = (pattern.get("slots") or {}).get(slot)
        if not spec:
            raise HTTPException(404, f"Pattern {pattern_id} has no slot {slot!r}")
        described, _ = _described()
        return {"pattern_id": pattern_id, "slot": slot, "spec": spec,
                "offered_by": dx.offers_for_slot(spec, described)}

    @app.post("/api/swarm/validate", tags=["swarm"])
    async def validate_pattern(pattern: Dict[str, Any] = Body(...)):
        """Check a pattern without saving it. For authoring."""
        errors = validate(pattern)
        return {"valid": not errors, "errors": errors}

    logger.info("Swarm Intelligence routes registered")
