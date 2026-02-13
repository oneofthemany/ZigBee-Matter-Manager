"""
Automation API - FastAPI routes for threshold-based automation rules.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class ConditionItem(BaseModel):
    attribute: str
    operator: str
    value: Any
    sustain: Optional[int] = Field(None, description="Optional: seconds condition must hold")


class PrerequisiteItem(BaseModel):
    ieee: str
    attribute: str
    operator: str
    value: Any


class AutomationCreateRequest(BaseModel):
    name: Optional[str] = Field("", description="Human-friendly rule name")
    source_ieee: str
    conditions: Optional[List[ConditionItem]] = None
    # Single condition shorthand
    attribute: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[Any] = None
    prerequisites: Optional[List[PrerequisiteItem]] = Field(default_factory=list)
    target_ieee: str
    command: str
    command_value: Optional[Any] = None
    endpoint_id: Optional[int] = None
    delay: Optional[int] = Field(None, description="Optional delay in seconds before executing")
    cooldown: int = 5
    enabled: bool = True


class AutomationUpdateRequest(BaseModel):
    name: Optional[str] = None
    conditions: Optional[List[ConditionItem]] = None
    prerequisites: Optional[List[PrerequisiteItem]] = None
    target_ieee: Optional[str] = None
    command: Optional[str] = None
    command_value: Optional[Any] = None
    endpoint_id: Optional[int] = None
    delay: Optional[int] = None
    cooldown: Optional[int] = None
    enabled: Optional[bool] = None


# ============================================================================
# ROUTE REGISTRATION
# ============================================================================

def register_automation_routes(
        app: FastAPI,
        automation_getter: Union[Any, Callable[[], Any]],
):
    def get_engine():
        if callable(automation_getter):
            return automation_getter()
        return automation_getter

    def _conditions_to_dicts(conditions):
        if not conditions:
            return []
        result = []
        for c in conditions:
            d = {"attribute": c.attribute, "operator": c.operator, "value": c.value}
            if c.sustain is not None and c.sustain > 0:
                d["sustain"] = c.sustain
            result.append(d)
        return result

    def _prereqs_to_dicts(prereqs):
        if not prereqs:
            return []
        return [{"ieee": p.ieee, "attribute": p.attribute,
                 "operator": p.operator, "value": p.value}
                for p in prereqs]

    # -----------------------------------------------------------------

    @app.get("/api/automations", tags=["automations"])
    async def list_automations(source_ieee: Optional[str] = None):
        engine = get_engine()
        if not engine:
            return []
        return engine.get_rules(source_ieee=source_ieee)

    @app.get("/api/automations/stats", tags=["automations"])
    async def get_automation_stats():
        engine = get_engine()
        if not engine:
            return {"total_rules": 0}
        return engine.get_stats()

    @app.get("/api/automations/trace", tags=["automations"])
    async def get_automation_trace(rule_id: Optional[str] = None):
        engine = get_engine()
        if not engine:
            return []
        entries = engine.get_trace_log()
        if rule_id:
            entries = [e for e in entries if e.get("rule_id") == rule_id]
        return entries

    @app.get("/api/automations/rule/{rule_id}", tags=["automations"])
    async def get_automation_rule(rule_id: str):
        engine = get_engine()
        if not engine:
            raise HTTPException(status_code=503, detail="Engine not initialised")
        rule = engine.get_rule(rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail=f"Rule not found: {rule_id}")
        return rule

    @app.post("/api/automations", tags=["automations"])
    async def create_automation(request: AutomationCreateRequest):
        engine = get_engine()
        if not engine:
            raise HTTPException(status_code=503, detail="Engine not initialised")
        data = request.model_dump()
        if data.get("conditions"):
            data["conditions"] = _conditions_to_dicts(request.conditions)
        if data.get("prerequisites"):
            data["prerequisites"] = _prereqs_to_dicts(request.prerequisites)
        result = engine.add_rule(data)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Unknown"))
        return result

    @app.put("/api/automations/{rule_id}", tags=["automations"])
    async def update_automation(rule_id: str, request: AutomationUpdateRequest):
        engine = get_engine()
        if not engine:
            raise HTTPException(status_code=503, detail="Engine not initialised")
        updates = {k: v for k, v in request.model_dump().items() if v is not None}
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")
        if "conditions" in updates and request.conditions:
            updates["conditions"] = _conditions_to_dicts(request.conditions)
        if "prerequisites" in updates and request.prerequisites:
            updates["prerequisites"] = _prereqs_to_dicts(request.prerequisites)
        result = engine.update_rule(rule_id, updates)
        if not result.get("success"):
            code = 404 if "not found" in result.get("error", "").lower() else 400
            raise HTTPException(status_code=code, detail=result.get("error"))
        return result

    @app.patch("/api/automations/{rule_id}/toggle", tags=["automations"])
    async def toggle_automation(rule_id: str):
        engine = get_engine()
        if not engine:
            raise HTTPException(status_code=503, detail="Engine not initialised")
        rule = engine.get_rule(rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail=f"Rule not found: {rule_id}")
        return engine.update_rule(rule_id, {"enabled": not rule.get("enabled", True)})

    @app.delete("/api/automations/{rule_id}", tags=["automations"])
    async def delete_automation(rule_id: str):
        engine = get_engine()
        if not engine:
            raise HTTPException(status_code=503, detail="Engine not initialised")
        result = engine.delete_rule(rule_id)
        if not result.get("success"):
            code = 404 if "not found" in result.get("error", "").lower() else 400
            raise HTTPException(status_code=code, detail=result.get("error"))
        return result

    # -----------------------------------------------------------------
    # HELPER ENDPOINTS
    # -----------------------------------------------------------------

    @app.get("/api/automations/device/{ieee}/attributes", tags=["automations"])
    async def get_device_attributes(ieee: str):
        engine = get_engine()
        if not engine:
            return []
        return engine.get_source_attributes(ieee)

    @app.get("/api/automations/device/{ieee}/state", tags=["automations"])
    async def get_device_state(ieee: str):
        engine = get_engine()
        if not engine:
            return {}
        return engine.get_device_state(ieee)

    @app.get("/api/automations/device/{ieee}/actions", tags=["automations"])
    async def get_device_actions(ieee: str):
        engine = get_engine()
        if not engine:
            return []
        return engine.get_target_actions(ieee)

    @app.get("/api/automations/actuators", tags=["automations"])
    async def get_actuator_devices():
        engine = get_engine()
        if not engine:
            return []
        return engine.get_actuator_devices()

    @app.get("/api/automations/devices", tags=["automations"])
    async def get_all_devices():
        engine = get_engine()
        if not engine:
            return []
        return engine.get_all_devices_summary()

    logger.info("Automation API routes registered")