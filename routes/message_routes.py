"""
Messages API — strictly participant-only.

Every endpoint resolves "me" from the authenticated principal and returns only
threads that principal is part of; there is deliberately no admin
read-everything view. from_user is always the authenticated username.
See docs/notifications.md.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request as HttpRequest
from pydantic import BaseModel, Field

from modules.auth_middleware import Principal, require_authenticated

logger = logging.getLogger("routes.messages")


class SendBody(BaseModel):
    to_user: str = Field(..., min_length=1, max_length=64)
    body: str = Field(..., min_length=1, max_length=1000)


def register_message_routes(app: FastAPI, store_getter: Callable):

    def _store():
        s = store_getter()
        if not s:
            raise HTTPException(503, "Message store not initialised")
        return s

    def _me(request: HttpRequest) -> str:
        p: Optional[Principal] = getattr(request.state, "principal", None)
        if p is None or not getattr(p, "user", None):
            raise HTTPException(401, "Authentication required")
        return p.user.username

    @app.get("/api/messages/threads")
    async def threads(request: HttpRequest, _=Depends(require_authenticated)):
        me = _me(request)
        return {"user": me, "threads": await _store().threads_for(me)}

    @app.get("/api/messages/unread")
    async def unread(request: HttpRequest, _=Depends(require_authenticated)):
        me = _me(request)
        return {"user": me, "unread": await _store().unread_total(me)}

    @app.get("/api/messages/with/{peer}")
    async def conversation(
            peer: str,
            request: HttpRequest,
            limit: int = Query(50, ge=1, le=200),
            before: Optional[float] = Query(None),
            _=Depends(require_authenticated),
    ):
        me = _me(request)
        if peer.lower() == me.lower():
            raise HTTPException(400, "No self-conversation")
        msgs = await _store().conversation(me, peer, limit=limit, before=before)
        return {"user": me, "peer": peer, "messages": msgs}

    @app.post("/api/messages")
    async def send(payload: SendBody, request: HttpRequest,
                   _=Depends(require_authenticated)):
        me = _me(request)
        result = await _store().send(
            from_user=me, to_user=payload.to_user, body=payload.body)
        if not result.get("success"):
            raise HTTPException(400, result.get("error"))
        return result

    @app.post("/api/messages/with/{peer}/read")
    async def mark_read(peer: str, request: HttpRequest,
                        _=Depends(require_authenticated)):
        me = _me(request)
        return {"success": True, "marked": await _store().mark_read(me, peer)}

    logger.info("Message routes registered (participant-only)")
