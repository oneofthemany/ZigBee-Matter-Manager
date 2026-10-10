"""
Notification channel API (ntfy, Telegram, Signal, Pushover, email). Each user manages
their own destinations; the hub's servers and tokens are admin-only and their
secrets are write-only. See docs/notifications.md §Other channels.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request as HttpRequest

from modules.auth_middleware import Principal, require_authenticated, require_scope
from modules.notify_channels import available, get_channel_manager, hub_public_view

logger = logging.getLogger("routes.notify_channels")


def register_notify_channel_routes(app: FastAPI) -> None:

    def _mgr():
        m = get_channel_manager()
        if not m:
            raise HTTPException(503, "Notification channels not initialised")
        return m

    def _me(request: HttpRequest) -> str:
        p: Optional[Principal] = getattr(request.state, "principal", None)
        if p is None:
            raise HTTPException(401, "Authentication required")
        return p.user.username

    def _view(m, user: str) -> Dict[str, Any]:
        return {"available": available(m.hub), "ntfy_server": m.hub.get("ntfy_server") or "",
                "settings": m.settings(user), "warnings": m.warnings(user)}

    @app.get("/api/notify-channels")
    async def get_mine(request: HttpRequest, _=Depends(require_authenticated)):
        return _view(_mgr(), _me(request))

    @app.put("/api/notify-channels")
    async def put_mine(body: Dict[str, Any], request: HttpRequest,
                       _=Depends(require_authenticated)):
        m, user = _mgr(), _me(request)
        try:
            m.update(user, body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return _view(m, user)

    @app.post("/api/notify-channels/test")
    async def test_mine(request: HttpRequest, _=Depends(require_authenticated)):
        results = await _mgr().send_to_user(_me(request), {
            "title": "ZMM test", "body": "This channel reaches you."})
        return {"channels": results}

    @app.post("/api/notify-channels/telegram/link")
    async def telegram_link(request: HttpRequest, _=Depends(require_authenticated)):
        try:
            return await _mgr().telegram_link_start(_me(request))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/notify-channels/telegram/check")
    async def telegram_check(request: HttpRequest, _=Depends(require_authenticated)):
        try:
            return await _mgr().telegram_link_check(_me(request))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/notify-channels/signal/verify")
    async def signal_verify(body: Dict[str, Any], request: HttpRequest,
                            _=Depends(require_authenticated)):
        try:
            return await _mgr().signal_verify_start(_me(request), body.get("number"))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/notify-channels/signal/confirm")
    async def signal_confirm(body: Dict[str, Any], request: HttpRequest,
                             _=Depends(require_authenticated)):
        try:
            return _mgr().signal_verify_confirm(_me(request), body.get("code"))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/notify-channels/hub")
    async def get_hub(_=Depends(require_scope("admin"))):
        return hub_public_view(_mgr().hub)

    @app.put("/api/notify-channels/hub")
    async def put_hub(body: Dict[str, Any], _=Depends(require_scope("admin"))):
        try:
            hub = _mgr().save_hub(body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except OSError as e:
            raise HTTPException(500, f"Could not write the secrets file: {e}")
        return hub_public_view(hub)
