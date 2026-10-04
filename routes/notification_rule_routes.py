"""
Notification rule API. Rules belong to the signed-in user, who is also who they
notify, so every route works on the caller's own rules only.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request as HttpRequest
from pydantic import BaseModel

from modules.auth_middleware import Principal, require_authenticated
from modules.notification_rules import get_rule_engine

logger = logging.getLogger("routes.notification_rules")


class ImportBody(BaseModel):
    rules: List[Dict[str, Any]]


def register_notification_rule_routes(app: FastAPI) -> None:

    def _store():
        engine = get_rule_engine()
        if not engine:
            raise HTTPException(503, "Notification rules not initialised")
        return engine.store

    def _owner(request: HttpRequest) -> str:
        p: Optional[Principal] = getattr(request.state, "principal", None)
        if p is None:
            raise HTTPException(401, "Authentication required")
        return p.user.username

    @app.get("/api/notification-rules")
    async def list_rules(request: HttpRequest, _=Depends(require_authenticated)):
        engine = get_rule_engine()
        rules = _store().for_owner(_owner(request))
        return {"rules": [{**r, "last_fired": engine.last_fired(r["id"])} for r in rules]}

    @app.post("/api/notification-rules")
    async def create_rule(body: Dict[str, Any], request: HttpRequest,
                          _=Depends(require_authenticated)):
        try:
            return _store().create(_owner(request), body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # Before /{rule_id}, which would otherwise take "bell" as a rule id.
    @app.get("/api/notification-rules/bell")
    async def get_bell(request: HttpRequest, _=Depends(require_authenticated)):
        """The navbar bell's switches for the caller."""
        return _store().bell_settings(_owner(request))

    @app.put("/api/notification-rules/bell")
    async def put_bell(body: Dict[str, Any], request: HttpRequest,
                       _=Depends(require_authenticated)):
        try:
            return _store().set_bell(_owner(request), body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.put("/api/notification-rules/{rule_id}")
    async def update_rule(rule_id: str, body: Dict[str, Any], request: HttpRequest,
                          _=Depends(require_authenticated)):
        try:
            rule = _store().update(_owner(request), rule_id, body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if rule is None:
            raise HTTPException(404, "No such rule")
        return rule

    @app.delete("/api/notification-rules/{rule_id}")
    async def delete_rule(rule_id: str, request: HttpRequest,
                          _=Depends(require_authenticated)):
        if not _store().delete(_owner(request), rule_id):
            raise HTTPException(404, "No such rule")
        return {"success": True}

    @app.post("/api/notification-rules/{rule_id}/test")
    async def test_rule(rule_id: str, request: HttpRequest, _=Depends(require_authenticated)):
        """Send this rule's notification now, to prove delivery without waiting for the event."""
        rule = _store().rules.get(rule_id)
        if not rule or rule["owner"] != _owner(request):
            raise HTTPException(404, "No such rule")
        return await get_rule_engine().send_test(rule)

    @app.post("/api/notification-rules/import")
    async def import_rules(body: ImportBody, request: HttpRequest,
                           _=Depends(require_authenticated)):
        """Adopt the rules a browser kept locally before rules moved to the hub."""
        imported, errors = _store().import_rules(_owner(request), body.rules)
        return {"imported": imported, "errors": errors}

    logger.info("Notification rule routes registered")
