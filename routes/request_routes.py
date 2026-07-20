"""
Requests API — asks that need an answer.

Authorisation is by identity, not by scope: you may answer what is addressed to
you, and see what you sent or received. Admin sees everything, because someone
has to be able to explain why a request never arrived.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request as HttpRequest
from pydantic import BaseModel, Field

from modules.auth_middleware import Principal, require_authenticated, require_scope
from modules.requests_store import (
    DEFAULT_TIMEOUT_S, MAX_MESSAGE_LEN, get_request_store,
)

logger = logging.getLogger("routes.requests")


class CreateRequest(BaseModel):
    to_user: str = Field(..., min_length=1, max_length=64)
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LEN)
    timeout_s: float = Field(DEFAULT_TIMEOUT_S, gt=0, le=7 * 24 * 3600)
    from_user: Optional[str] = None       # admin may send on someone's behalf


def register_request_routes(app: FastAPI) -> None:

    def _store():
        s = get_request_store()
        if not s:
            raise HTTPException(503, "Request store not initialised")
        return s

    def _me(request: HttpRequest) -> Principal:
        p: Optional[Principal] = getattr(request.state, "principal", None)
        if p is None:
            raise HTTPException(401, "Authentication required")
        return p

    @app.get("/api/requests")
    async def list_requests(
            request: HttpRequest,
            include_settled: bool = False,
            _=Depends(require_authenticated),
    ):
        p = _me(request)
        from modules.auth import scope_matches
        if scope_matches("admin", p.scopes):
            return {"requests": _store().all(include_settled)}
        return {"requests": _store().for_user(p.user.username, include_settled)}

    @app.post("/api/requests")
    async def create_request(
            payload: CreateRequest,
            request: HttpRequest,
            _=Depends(require_authenticated),
    ):
        p = _me(request)
        from modules.auth import scope_matches
        is_admin = scope_matches("admin", p.scopes)

        # Impersonation is an admin capability. Without this check anyone could
        # send an ask that appears to come from someone else — and the whole
        # value of the feature is that the recipient knows who is asking.
        sender = payload.from_user or p.user.username
        if sender != p.user.username and not is_admin:
            raise HTTPException(403, "Cannot send requests as another user")

        result = await _store().create(
            from_user=sender,
            to_user=payload.to_user,
            message=payload.message,
            timeout_s=payload.timeout_s,
            source="manual",
        )
        if not result.get("success"):
            raise HTTPException(400, result.get("error"))
        return result

    @app.post("/api/requests/{request_id}/accept")
    async def accept(request_id: str, request: HttpRequest,
                     _=Depends(require_authenticated)):
        return await _answer(request_id, request, True)

    @app.post("/api/requests/{request_id}/decline")
    async def decline(request_id: str, request: HttpRequest,
                      _=Depends(require_authenticated)):
        return await _answer(request_id, request, False)

    async def _answer(request_id: str, request: HttpRequest, accept: bool):
        p = _me(request)
        result = await _store().answer(request_id, p.user.username, accept)
        if not result.get("success"):
            # 409, not 400: answering something already settled is a race
            # (the sweep ran while the notification sat on a lock screen),
            # and the client needs the settled state to show what happened.
            code = 409 if result.get("request") else 404
            raise HTTPException(code, result.get("error"))
        return result

    @app.post("/api/requests/sweep")
    async def sweep_now(_=Depends(require_scope("admin"))):
        """Force an expiry pass. Useful for testing the escalation path."""
        n = await _store().sweep()
        return {"success": True, "expired": n}

    logger.info("Request routes registered")
