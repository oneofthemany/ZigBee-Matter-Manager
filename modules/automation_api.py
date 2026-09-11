"""
Automation API - FastAPI routes for state-machine automation rules.
Steps are recursive (if_then_else, parallel) so we accept raw dicts and
delegate validation to the engine.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Union

from fastapi import Body, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class ConditionItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    type: str = "attribute"
    # The device this condition reads. Absent means the rule's source_ieee, so
    # one rule can trigger on several devices joined by condition_logic.
    ieee: Optional[str] = None
    # group fields — a nested block with its own AND/OR, joined to its siblings
    # by the rule's condition_logic ("(A and B) or C"). One level deep.
    condition_logic: Optional[str] = None
    conditions: Optional[List["ConditionItem"]] = None
    attribute: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[Any] = None
    sustain: Optional[int] = None
    # rose_by / fell_by window, seconds
    within: Optional[float] = None
    # offline condition: minutes without a report (absent = the hub's verdict)
    minutes: Optional[float] = None
    # webhook condition: the id in /api/automations/webhook/<hook> (made if absent)
    hook: Optional[str] = None
    negate: bool = False
    time_from: Optional[str] = None
    time_to: Optional[str] = None
    days: Optional[List[int]] = None
    # time (alarm) field
    at: Optional[str] = None
    # zone fields — a person entering or leaving a named place. `place` is one
    # place id, "home", "any", or a list of ids forming a single zone.
    event: Optional[str] = None
    place: Optional[Any] = None
    # sun fields — wire names are "from"/"to" ("from" is a Python keyword)
    sun_from: Optional[str] = Field(default=None, alias="from")
    sun_to: Optional[str] = Field(default=None, alias="to")
    offset_from: Optional[float] = None
    offset_to: Optional[float] = None


ConditionItem.model_rebuild()          # resolve the self-reference for groups

class PrerequisiteItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    type: str = "device"
    # device fields
    ieee: Optional[str] = None
    attribute: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[Any] = None
    negate: bool = False
    # time_window fields
    time_from: Optional[str] = None
    time_to: Optional[str] = None
    days: Optional[List[int]] = None
    # sun fields — wire names are "from"/"to"
    sun_from: Optional[str] = Field(default=None, alias="from")
    sun_to: Optional[str] = Field(default=None, alias="to")
    offset_from: Optional[float] = None
    offset_to: Optional[float] = None

class AutomationCreateRequest(BaseModel):
    name: Optional[str] = ""
    source_ieee: str
    conditions: Optional[List[ConditionItem]] = None
    # How the trigger conditions are joined: "and" (all) or "or" (any).
    condition_logic: str = "and"
    # What firing again while still running does: restart | single | queued | parallel.
    run_mode: str = "restart"
    attribute: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[Any] = None
    prerequisites: Optional[List[PrerequisiteItem]] = Field(default_factory=list)
    then_sequence: List[Dict[str, Any]] = Field(default_factory=list)
    else_sequence: List[Dict[str, Any]] = Field(default_factory=list)
    cooldown: int = 5
    enabled: bool = True

class AutomationUpdateRequest(BaseModel):
    name: Optional[str] = None
    conditions: Optional[List[ConditionItem]] = None
    condition_logic: Optional[str] = None
    run_mode: Optional[str] = None
    prerequisites: Optional[List[PrerequisiteItem]] = None
    then_sequence: Optional[List[Dict[str, Any]]] = None
    else_sequence: Optional[List[Dict[str, Any]]] = None
    cooldown: Optional[int] = None
    enabled: Optional[bool] = None


def _sun_dict(item):
    d = {"type": "sun", "from": item.sun_from, "to": item.sun_to, "negate": item.negate}
    if item.days is not None: d["days"] = item.days
    if item.offset_from is not None: d["offset_from"] = item.offset_from
    if item.offset_to is not None: d["offset_to"] = item.offset_to
    return d

def _conds_to_dicts(items):
    if not items: return []
    r = []
    for c in items:
        if c.type == "group":
            r.append({"type": "group", "condition_logic": c.condition_logic or "and",
                      "conditions": _conds_to_dicts(c.conditions)})
        elif c.type == "time_window":
            r.append({"type": "time_window", "time_from": c.time_from, "time_to": c.time_to,
                      "days": c.days if c.days is not None else list(range(7)), "negate": c.negate})
        elif c.type == "time":
            r.append({"type": "time", "at": c.at,
                      "days": c.days if c.days is not None else list(range(7))})
        elif c.type == "zone":
            d = {"type": "zone", "event": c.event, "place": c.place}
            if c.ieee: d["ieee"] = c.ieee
            r.append(d)
        elif c.type == "sun":
            r.append(_sun_dict(c))
        elif c.type == "date":
            r.append({"type": "date", "from": c.sun_from, "to": c.sun_to, "negate": c.negate})
        elif c.type == "webhook":
            r.append({"type": "webhook", "hook": c.hook})
        elif c.type == "startup":
            r.append({"type": "startup"})
        elif c.type == "offline":
            d = {"type": "offline"}
            if c.minutes: d["minutes"] = c.minutes
            if c.negate: d["negate"] = True
            if c.ieee: d["ieee"] = c.ieee
            r.append(d)
        else:
            d = {"type": "attribute", "attribute": c.attribute, "operator": c.operator, "value": c.value}
            if c.ieee: d["ieee"] = c.ieee
            if c.sustain and c.sustain > 0: d["sustain"] = c.sustain
            if c.within: d["within"] = c.within
            r.append(d)
    return r

def _prereqs_to_dicts(items):
    if not items: return []
    result = []
    for p in items:
        if p.type == "time_window":
            result.append({
                "type": "time_window",
                "time_from": p.time_from,
                "time_to": p.time_to,
                "days": p.days if p.days is not None else list(range(7)),
                "negate": p.negate,
            })
        elif p.type == "date":
            result.append({"type": "date", "from": p.sun_from, "to": p.sun_to,
                           "negate": p.negate})
        elif p.type == "sun":
            result.append(_sun_dict(p))
        else:
            result.append({
                "type": "device",
                "ieee": p.ieee,
                "attribute": p.attribute,
                "operator": p.operator,
                "value": p.value,
                "negate": p.negate,
            })
    return result


def register_automation_routes(app: FastAPI,
                               automation_getter: Union[Any, Callable[[], Any]]):
    def ge():
        return automation_getter() if callable(automation_getter) else automation_getter

    @app.get("/api/automations", tags=["automations"])
    async def list_rules(source_ieee: Optional[str] = None):
        e = ge(); return e.get_rules(source_ieee=source_ieee) if e else []

    @app.get("/api/automations/stats", tags=["automations"])
    async def stats():
        e = ge(); return e.get_stats() if e else {"total_rules":0}

    @app.get("/api/automations/trace", tags=["automations"])
    async def trace(rule_id: Optional[str] = None):
        e = ge()
        if not e: return []
        # Rule-filtered requests come from the per-rule ring buffer, which
        # keeps a rule's own history even after the shared log has churned.
        return e.get_trace_log(rule_id)

    @app.get("/api/automations/rule/{rule_id}", tags=["automations"])
    async def get_rule(rule_id: str):
        e = ge()
        if not e: raise HTTPException(503)
        r = e.get_rule(rule_id)
        if not r: raise HTTPException(404, f"Not found: {rule_id}")
        return r

    @app.post("/api/automations", tags=["automations"])
    async def create(request: AutomationCreateRequest):
        e = ge()
        if not e: raise HTTPException(503)
        data = request.model_dump()
        if data.get("conditions"): data["conditions"] = _conds_to_dicts(request.conditions)
        if data.get("prerequisites"): data["prerequisites"] = _prereqs_to_dicts(request.prerequisites)
        # then_sequence and else_sequence are already raw dicts
        result = e.add_rule(data)
        if not result.get("success"):
            raise HTTPException(400, result.get("error"))
        return result

    @app.put("/api/automations/{rule_id}", tags=["automations"])
    async def update(rule_id: str, request: AutomationUpdateRequest):
        e = ge()
        if not e: raise HTTPException(503)
        updates = {k:v for k,v in request.model_dump().items() if v is not None}
        if not updates: raise HTTPException(400, "Nothing to update")
        if "conditions" in updates and request.conditions:
            updates["conditions"] = _conds_to_dicts(request.conditions)
        if "prerequisites" in updates and request.prerequisites:
            updates["prerequisites"] = _prereqs_to_dicts(request.prerequisites)
        result = e.update_rule(rule_id, updates)
        if not result.get("success"):
            code = 404 if "not found" in result.get("error","").lower() else 400
            raise HTTPException(code, result.get("error"))
        return result

    @app.patch("/api/automations/{rule_id}/toggle", tags=["automations"])
    async def toggle(rule_id: str):
        e = ge()
        if not e: raise HTTPException(503)
        r = e.get_rule(rule_id)
        if not r: raise HTTPException(404)
        return e.update_rule(rule_id, {"enabled": not r.get("enabled",True)})

    @app.delete("/api/automations/{rule_id}", tags=["automations"])
    async def delete(rule_id: str):
        e = ge()
        if not e: raise HTTPException(503)
        result = e.delete_rule(rule_id)
        if not result.get("success"): raise HTTPException(404, result.get("error"))
        return result

    # Import, webhooks, run by hand

    @app.post("/api/automations/import", tags=["automations"])
    async def import_rules(payload: Any = Body(...)):
        """Add rules from JSON as Download produces it — one rule, a list, or
        {"rules": [...]}. Each gets a new id; any that fail validation are
        reported and skipped."""
        e = ge()
        if not e:
            raise HTTPException(503)
        result = e.import_rules(payload)
        if not result.get("imported"):
            errors = [r.get("error") for r in result.get("results", []) if r.get("error")]
            raise HTTPException(400, result.get("error") or "; ".join(errors) or "Nothing to import")
        return result

    @app.post("/api/automations/webhook/{hook}", tags=["automations"])
    async def webhook(hook: str, request: Request):
        """Fire every enabled rule with a webhook condition on this id.

        Authenticated like every other /api call (an API token as
        Authorization: Bearer …) — a webhook can unlock a door, so it is not
        public. A JSON object body is readable in message text as {webhook.key}.
        """
        e = ge()
        if not e:
            raise HTTPException(503)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        result = e.fire_webhook(hook, payload if isinstance(payload, dict) else {})
        if not result.get("success"):
            raise HTTPException(404, result.get("error"))
        return result

    @app.post("/api/automations/{rule_id}/run", tags=["automations"])
    async def run_now(rule_id: str, path: str = "then"):
        """Run a rule's THEN (or ELSE, ?path=else) steps now, as a test. The
        rule's matched state is left alone."""
        e = ge()
        if not e:
            raise HTTPException(503)
        result = e.run_now(rule_id, path)
        if not result.get("success"):
            err = result.get("error", "")
            raise HTTPException(404 if "not found" in err.lower() else 409, err)
        return result

    # Offers — a message that can act, awaiting an answer.

    @app.get("/api/automations/offers", tags=["automations"])
    async def list_offers(to_user: Optional[str] = None):
        """Offers still awaiting an answer. Expired ones are dropped on read."""
        e = ge()
        return {"offers": e.get_offers(to_user) if e else []}

    @app.post("/api/automations/offers/{token}/accept", tags=["automations"])
    async def accept_offer(token: str, as_user: Optional[str] = None):
        """Say yes: run the sequence the rule stored against this offer."""
        e = ge()
        if not e:
            raise HTTPException(503)
        result = await e.accept_offer(token, as_user=as_user)
        if not result.get("success"):
            raise HTTPException(404, result.get("error"))
        return result

    @app.post("/api/automations/offers/{token}/decline", tags=["automations"])
    async def decline_offer(token: str, as_user: Optional[str] = None):
        """Say no. Nothing runs and the offer goes away."""
        e = ge()
        if not e:
            raise HTTPException(503)
        result = e.decline_offer(token, as_user=as_user)
        if not result.get("success"):
            raise HTTPException(404, result.get("error"))
        return result

    @app.get("/api/automations/device/{ieee}/attributes", tags=["automations"])
    async def attrs(ieee: str):
        e = ge(); return e.get_source_attributes(ieee) if e else []

    @app.get("/api/automations/device/{ieee}/state", tags=["automations"])
    async def dev_state(ieee: str):
        e = ge(); return e.get_device_state(ieee) if e else {}

    @app.get("/api/automations/device/{ieee}/actions", tags=["automations"])
    async def actions(ieee: str):
        e = ge(); return e.get_target_actions(ieee) if e else []

    @app.get("/api/automations/actuators", tags=["automations"])
    async def actuators():
        e = ge(); return e.get_actuator_devices() if e else []

    @app.get("/api/automations/devices", tags=["automations"])
    async def all_devs():
        e = ge(); return e.get_all_devices_summary() if e else []

    logger.info("Automation API routes registered")