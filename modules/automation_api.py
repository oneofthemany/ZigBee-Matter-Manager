"""
Automation API - FastAPI routes for state-machine automation rules.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ============================================================================
# MODELS
# ============================================================================

class ConditionItem(BaseModel):
    attribute: str
    operator: str
    value: Any
    sustain: Optional[int] = None


class PrerequisiteItem(BaseModel):
    ieee: str
    attribute: str
    operator: str
    value: Any


class StepItem(BaseModel):
    type: str  # "command", "delay", "wait_for", "condition"
    # command
    target_ieee: Optional[str] = None
    command: Optional[str] = None
    value: Optional[Any] = None
    endpoint_id: Optional[int] = None
    # delay
    seconds: Optional[int] = None
    # wait_for / condition
    ieee: Optional[str] = None
    attribute: Optional[str] = None
    operator: Optional[str] = None
    # wait_for value uses 'value' field
    timeout: Optional[int] = None


class AutomationCreateRequest(BaseModel):
    name: Optional[str] = ""
    source_ieee: str
    conditions: Optional[List[ConditionItem]] = None
    attribute: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[Any] = None
    prerequisites: Optional[List[PrerequisiteItem]] = Field(default_factory=list)
    then_sequence: List[StepItem] = Field(default_factory=list)
    else_sequence: List[StepItem] = Field(default_factory=list)
    cooldown: int = 5
    enabled: bool = True


class AutomationUpdateRequest(BaseModel):
    name: Optional[str] = None
    conditions: Optional[List[ConditionItem]] = None
    prerequisites: Optional[List[PrerequisiteItem]] = None
    then_sequence: Optional[List[StepItem]] = None
    else_sequence: Optional[List[StepItem]] = None
    cooldown: Optional[int] = None
    enabled: Optional[bool] = None


# ============================================================================
# HELPERS
# ============================================================================

def _conditions_to_dicts(items):
    if not items:
        return []
    result = []
    for c in items:
        d = {"attribute": c.attribute, "operator": c.operator, "value": c.value}
        if c.sustain is not None and c.sustain > 0:
            d["sustain"] = c.sustain
        result.append(d)
    return result


def _prereqs_to_dicts(items):
    if not items:
        return []
    return [{"ieee": p.ieee, "attribute": p.attribute,
             "operator": p.operator, "value": p.value} for p in items]


def _steps_to_dicts(items):
    if not items:
        return []
    result = []
    for s in items:
        d = {"type": s.type}
        if s.type == "command":
            d.update({"target_ieee": s.target_ieee, "command": s.command})
            if s.value is not None:
                d["value"] = s.value
            if s.endpoint_id is not None:
                d["endpoint_id"] = s.endpoint_id
        elif s.type == "delay":
            d["seconds"] = s.seconds or 0
        elif s.type in ("wait_for", "condition"):
            d.update({"ieee": s.ieee, "attribute": s.attribute,
                      "operator": s.operator, "value": s.value})
            if s.type == "wait_for" and s.timeout:
                d["timeout"] = s.timeout
        result.append(d)
    return result


# ============================================================================
# REGISTRATION
# ============================================================================

def register_automation_routes(app: FastAPI,
                               automation_getter: Union[Any, Callable[[], Any]]):
    def get_engine():
        return automation_getter() if callable(automation_getter) else automation_getter

    @app.get("/api/automations", tags=["automations"])
    async def list_automations(source_ieee: Optional[str] = None):
        e = get_engine()
        return e.get_rules(source_ieee=source_ieee) if e else []

    @app.get("/api/automations/stats", tags=["automations"])
    async def get_stats():
        e = get_engine()
        return e.get_stats() if e else {"total_rules": 0}

    @app.get("/api/automations/trace", tags=["automations"])
    async def get_trace(rule_id: Optional[str] = None):
        e = get_engine()
        if not e:
            return []
        entries = e.get_trace_log()
        if rule_id:
            entries = [x for x in entries if x.get("rule_id") == rule_id]
        return entries

    @app.get("/api/automations/rule/{rule_id}", tags=["automations"])
    async def get_rule(rule_id: str):
        e = get_engine()
        if not e:
            raise HTTPException(503, "Engine not initialised")
        r = e.get_rule(rule_id)
        if not r:
            raise HTTPException(404, f"Rule not found: {rule_id}")
        return r

    @app.post("/api/automations", tags=["automations"])
    async def create(request: AutomationCreateRequest):
        e = get_engine()
        if not e:
            raise HTTPException(503, "Engine not initialised")
        data = request.model_dump()
        if data.get("conditions"):
            data["conditions"] = _conditions_to_dicts(request.conditions)
        if data.get("prerequisites"):
            data["prerequisites"] = _prereqs_to_dicts(request.prerequisites)
        data["then_sequence"] = _steps_to_dicts(request.then_sequence)
        data["else_sequence"] = _steps_to_dicts(request.else_sequence)
        result = e.add_rule(data)
        if not result.get("success"):
            raise HTTPException(400, result.get("error", "Unknown"))
        return result

    @app.put("/api/automations/{rule_id}", tags=["automations"])
    async def update(rule_id: str, request: AutomationUpdateRequest):
        e = get_engine()
        if not e:
            raise HTTPException(503, "Engine not initialised")
        updates = {k: v for k, v in request.model_dump().items() if v is not None}
        if not updates:
            raise HTTPException(400, "No fields to update")
        if "conditions" in updates and request.conditions:
            updates["conditions"] = _conditions_to_dicts(request.conditions)
        if "prerequisites" in updates and request.prerequisites:
            updates["prerequisites"] = _prereqs_to_dicts(request.prerequisites)
        if "then_sequence" in updates and request.then_sequence:
            updates["then_sequence"] = _steps_to_dicts(request.then_sequence)
        if "else_sequence" in updates and request.else_sequence:
            updates["else_sequence"] = _steps_to_dicts(request.else_sequence)
        result = e.update_rule(rule_id, updates)
        if not result.get("success"):
            code = 404 if "not found" in result.get("error", "").lower() else 400
            raise HTTPException(code, result.get("error"))
        return result

    @app.patch("/api/automations/{rule_id}/toggle", tags=["automations"])
    async def toggle(rule_id: str):
        e = get_engine()
        if not e:
            raise HTTPException(503, "Engine not initialised")
        r = e.get_rule(rule_id)
        if not r:
            raise HTTPException(404, f"Rule not found: {rule_id}")
        return e.update_rule(rule_id, {"enabled": not r.get("enabled", True)})

    @app.delete("/api/automations/{rule_id}", tags=["automations"])
    async def delete(rule_id: str):
        e = get_engine()
        if not e:
            raise HTTPException(503, "Engine not initialised")
        result = e.delete_rule(rule_id)
        if not result.get("success"):
            raise HTTPException(404, result.get("error"))
        return result

    # Helpers
    @app.get("/api/automations/device/{ieee}/attributes", tags=["automations"])
    async def attrs(ieee: str):
        e = get_engine()
        return e.get_source_attributes(ieee) if e else []

    @app.get("/api/automations/device/{ieee}/state", tags=["automations"])
    async def dev_state(ieee: str):
        e = get_engine()
        return e.get_device_state(ieee) if e else {}

    @app.get("/api/automations/device/{ieee}/actions", tags=["automations"])
    async def actions(ieee: str):
        e = get_engine()
        return e.get_target_actions(ieee) if e else []

    @app.get("/api/automations/actuators", tags=["automations"])
    async def actuators():
        e = get_engine()
        return e.get_actuator_devices() if e else []

    @app.get("/api/automations/devices", tags=["automations"])
    async def all_devices():
        e = get_engine()
        return e.get_all_devices_summary() if e else []

    logger.info("Automation API routes registered")