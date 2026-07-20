"""
Web Push subscription API.

Subscriptions belong to the logged-in account, so a phone that subscribes is
registered against whoever is signed in on it. That is what lets an automation
address a person rather than a device.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request as HttpRequest
from pydantic import BaseModel, Field

from modules.auth_middleware import Principal, require_authenticated, require_scope
from modules.webpush import get_push_manager

logger = logging.getLogger("routes.push")


class SubscribeBody(BaseModel):
    endpoint: str = Field(..., min_length=1, max_length=2000)
    p256dh: str = Field(..., min_length=1, max_length=256)
    auth: str = Field(..., min_length=1, max_length=64)
    label: str = Field("", max_length=64)


def register_push_routes(app: FastAPI) -> None:

    def _mgr():
        m = get_push_manager()
        if not m:
            raise HTTPException(503, "Push not initialised")
        return m

    def _me(request: HttpRequest) -> Principal:
        p: Optional[Principal] = getattr(request.state, "principal", None)
        if p is None:
            raise HTTPException(401, "Authentication required")
        return p

    @app.get("/api/push/key")
    async def public_key(_=Depends(require_authenticated)):
        """
        The applicationServerKey a browser needs to subscribe.

        Public by design — it is the key push services use to verify our
        signature, and a subscriber must embed it.
        """
        return {"key": _mgr().keys.public_b64}

    @app.post("/api/push/subscribe")
    async def subscribe(body: SubscribeBody, request: HttpRequest,
                        _=Depends(require_authenticated)):
        p = _me(request)
        result = _mgr().subscribe(
            user=p.user.username, endpoint=body.endpoint,
            p256dh=body.p256dh, auth=body.auth, label=body.label,
        )
        if not result.get("success"):
            raise HTTPException(400, result.get("error"))
        return result

    @app.get("/api/push/subscriptions")
    async def list_subs(request: HttpRequest, _=Depends(require_authenticated)):
        p = _me(request)
        from modules.auth import scope_matches
        m = _mgr()
        subs = (m.subs.values() if scope_matches("admin", p.scopes)
                else m.for_user(p.user.username))
        # public_view strips the keys — they are all an attacker needs to read
        # everything sent to that device.
        return {"subscriptions": [s.public_view() for s in subs]}

    @app.delete("/api/push/subscriptions/{sub_id}")
    async def unsubscribe(sub_id: str, request: HttpRequest,
                          _=Depends(require_authenticated)):
        p = _me(request)
        from modules.auth import scope_matches
        owner = None if scope_matches("admin", p.scopes) else p.user.username
        result = _mgr().unsubscribe(sub_id, owner)
        if not result.get("success"):
            raise HTTPException(404, result.get("error"))
        return result

    @app.post("/api/push/test")
    async def test_push(request: HttpRequest, _=Depends(require_authenticated)):
        """Send yourself a push, to prove the whole chain end to end."""
        p = _me(request)
        result = await _mgr().send_to_user(p.user.username, {
            "title": "ZMM test",
            "body": "Push notifications are working.",
            "tag": "zmm-test",
        })
        if result.get("no_subscriptions"):
            raise HTTPException(
                400,
                "No push subscriptions for this account. Enable notifications "
                "in this browser first — and note that requires a trusted "
                "HTTPS address, so the LAN self-signed URL will not work."
            )
        return result

    logger.info("Push routes registered")
