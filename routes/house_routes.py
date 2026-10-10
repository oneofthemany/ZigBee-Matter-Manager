"""
House mode and alarm API. Arming and disarming are security:write like locks;
disarming also needs the caller's own PIN. Setup is admin-only.
See docs/house-mode-and-alarm.md.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request as HttpRequest

from modules.alarm import get_alarm
from modules.auth_middleware import Principal, require_scope
from modules.house_mode import get_house_mode

logger = logging.getLogger("routes.house")


def register_house_routes(app: FastAPI) -> None:

    def _house():
        h = get_house_mode()
        if not h:
            raise HTTPException(503, "House mode not initialised")
        return h

    def _alarm():
        a = get_alarm()
        if not a:
            raise HTTPException(503, "Alarm not initialised")
        return a

    def _me(request: HttpRequest) -> str:
        p: Optional[Principal] = getattr(request.state, "principal", None)
        if p is None:
            raise HTTPException(401, "Authentication required")
        return p.user.username

    def _result(r: Dict[str, Any]) -> Dict[str, Any]:
        if r.get("success"):
            return r
        # 409 carries the open sensors, so the UI can offer "arm anyway".
        raise HTTPException(409 if r.get("open") else 400,
                            detail=r if r.get("open") else r.get("error"))

    # House mode
    @app.get("/api/house/mode")
    async def get_mode(_=Depends(require_scope("device:read"))):
        return _house().status()

    @app.post("/api/house/mode")
    async def set_mode(body: Dict[str, Any], request: HttpRequest,
                       _=Depends(require_scope("device:write"))):
        try:
            return await _house().set(str(body.get("mode") or ""), source=_me(request))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.put("/api/house/mode/config")
    async def put_mode_config(body: Dict[str, Any], _=Depends(require_scope("admin"))):
        try:
            _house().update_config(body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        await _house().check_presence()
        return _house().status()

    @app.post("/api/house/mode/config/create-worker")
    async def create_mode_worker(_=Depends(require_scope("admin"))):
        try:
            return _house().ensure_worker()
        except ValueError as e:
            raise HTTPException(400, str(e))

    # Alarm
    @app.get("/api/alarm")
    async def alarm_status(request: HttpRequest, _=Depends(require_scope("security:read"))):
        a = _alarm()
        return {**a.status(), "have_pin": a.has_pin(_me(request))}

    @app.post("/api/alarm/arm")
    async def alarm_arm(body: Dict[str, Any], request: HttpRequest,
                        _=Depends(require_scope("security:write"))):
        return _result(await _alarm().arm(str(body.get("mode") or ""), _me(request),
                                          body.get("pin"), force=bool(body.get("force"))))

    @app.post("/api/alarm/disarm")
    async def alarm_disarm(body: Dict[str, Any], request: HttpRequest,
                           _=Depends(require_scope("security:write"))):
        return _result(await _alarm().disarm(_me(request), body.get("pin")))

    @app.post("/api/alarm/pin")
    async def alarm_set_pin(body: Dict[str, Any], request: HttpRequest,
                            _=Depends(require_scope("security:write"))):
        try:
            await _alarm().set_pin(_me(request), str(body.get("pin") or ""), body.get("current"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        except PermissionError as e:
            raise HTTPException(403, str(e))
        return {"success": True}

    @app.delete("/api/alarm/pin/{username}")
    async def alarm_clear_pin(username: str, _=Depends(require_scope("admin"))):
        """For a forgotten PIN; the user then sets a new one."""
        return {"success": await _alarm().clear_pin(username)}

    @app.get("/api/alarm/config")
    async def alarm_get_config(_=Depends(require_scope("admin"))):
        a = _alarm()
        return {**a.config, "pins_set": sorted(a.pins)}

    @app.put("/api/alarm/config")
    async def alarm_put_config(body: Dict[str, Any], _=Depends(require_scope("admin"))):
        try:
            cfg = _alarm().update_config(body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {**cfg, "pins_set": sorted(_alarm().pins)}
