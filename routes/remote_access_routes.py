"""
Remote access (managed Cloudflare Tunnel) API — status at system:read;
settings, start and stop at admin.

Everything that can reconfigure exposure is admin-only: this feature publishes
the gateway to the internet. See docs/remote_access.md.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from modules.auth_middleware import require_scope
from modules.remote_access import get_remote_access_manager, RemoteAccessManager

logger = logging.getLogger("routes.remote_access")


class RemoteAccessSettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    mode: Optional[str] = Field(None, pattern="^(token|quick)$")
    # None = keep existing token, "" = clear it
    tunnel_token: Optional[str] = Field(None, max_length=2048)
    hostname: Optional[str] = Field(None, max_length=253)
    cloudflared_path: Optional[str] = Field(None, max_length=512)


def register_remote_access_routes(app: FastAPI):

    def _mgr() -> RemoteAccessManager:
        m = get_remote_access_manager()
        if not m:
            raise HTTPException(503, "Remote access manager not initialised")
        return m

    @app.get("/api/remote-access/status")
    async def remote_access_status(_=Depends(require_scope("system:read"))):
        mgr = _mgr()
        status = mgr.get_status()
        status["binary_version"] = await mgr.cloudflared_version()
        return status

    @app.get("/api/remote-access/settings")
    async def remote_access_settings(_=Depends(require_scope("admin"))):
        return _mgr().settings.public_view()

    @app.put("/api/remote-access/settings")
    async def update_remote_access_settings(
            req: RemoteAccessSettingsRequest,
            principal=Depends(require_scope("admin")),
    ):
        mgr = _mgr()
        settings = await mgr.apply_settings(**req.model_dump())
        logger.info(
            f"Remote access settings updated by {principal.user.username} "
            f"(enabled={settings.enabled}, mode={settings.mode})"
        )
        return {"success": True, "status": mgr.get_status()}

    @app.post("/api/remote-access/start")
    async def start_remote_access(
            principal=Depends(require_scope("admin")),
    ):
        mgr = _mgr()
        ok = await mgr.start()
        if not ok:
            raise HTTPException(500, mgr.get_status().get("last_error")
                                or "Failed to start tunnel")
        logger.info(f"Tunnel started by {principal.user.username}")
        return {"success": True, "status": mgr.get_status()}

    @app.post("/api/remote-access/stop")
    async def stop_remote_access(
            principal=Depends(require_scope("admin")),
    ):
        mgr = _mgr()
        await mgr.stop()
        logger.info(f"Tunnel stopped by {principal.user.username}")
        return {"success": True, "status": mgr.get_status()}

    logger.info("Remote access routes registered")
