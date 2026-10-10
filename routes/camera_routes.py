"""
Camera API: viewing is camera:read, setup is admin. The stream is a websocket
proxied to go2rtc, so cameras and go2rtc are never exposed to the browser.
See docs/cameras.md.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, WebSocket
from fastapi.responses import Response

from modules.auth import LAN_ONLY_SCOPE, get_auth_manager, scope_matches
from modules.auth_middleware import _derive_session_secret, _verify_session, require_scope
from modules.auth_network import get_network_resolver
from modules.cameras import STREAM_PREFIX, get_camera_manager
from modules.go2rtc import Go2rtcError

logger = logging.getLogger("routes.cameras")


def ws_principal(ws: WebSocket) -> Optional[str]:
    """Who a stream websocket belongs to, with camera:read — or None.

    HTTP middleware never sees websockets, so this repeats its checks: a
    session cookie (or ?token=), LAN-only accounts staying on the LAN, and a
    cookie handshake the browser marks cross-site refused — a hostile page
    riding the cookie. Sec-Fetch-Site, not Origin vs Host, for the reason in
    auth_middleware._cross_site_write: a tunnel rewrites Host."""
    auth = get_auth_manager()
    if auth is None:
        return None
    username = None
    cookie = ws.cookies.get("zmm_session")
    site = ws.headers.get("sec-fetch-site")
    if cookie and site is not None and site not in ("same-origin", "none"):
        logger.warning("[cameras] stream refused: cross-site handshake (%s)", site)
        return None
    if cookie:
        u = _verify_session(cookie, _derive_session_secret(str(auth.config_path)), auth=auth)
        if u and u in auth.users and not auth.users[u].disabled:
            username = u
            scopes = auth.resolve_user_scopes(u)
    if username is None:
        token = ws.query_params.get("token")
        verified = auth.verify_token(token) if token else None
        if not verified:
            return None
        username, scopes = verified[0].username, set(verified[2])
    if not scope_matches("camera:read", scopes):
        return None
    resolver = get_network_resolver()
    if resolver is not None and LAN_ONLY_SCOPE in auth.resolve_user_scopes(username) \
            and not resolver.is_lan(resolver.resolve(ws)):
        return None
    return username


def register_camera_routes(app: FastAPI) -> None:

    def _mgr():
        m = get_camera_manager()
        if not m:
            raise HTTPException(503, "Cameras not initialised")
        return m

    @app.get("/api/cameras")
    async def list_cameras(_=Depends(require_scope("camera:read"))):
        m = _mgr()
        return {"cameras": m.list(), "error": m.last_error}

    @app.get("/api/cameras/{cid}/snapshot")
    async def snapshot(cid: str, _=Depends(require_scope("camera:read"))):
        try:
            img = await _mgr().snapshot(cid)
        except KeyError:
            raise HTTPException(404, "No such camera")
        except Go2rtcError as e:
            raise HTTPException(502, str(e))
        return Response(img, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/api/cameras/{cid}/detection")
    async def detection_snapshot(cid: str, _=Depends(require_scope("camera:read"))):
        """The frame of the camera's latest detection, boxes drawn."""
        from modules.vision import VisionError, get_vision_bridge
        b = get_vision_bridge()
        if cid not in _mgr().cameras:
            raise HTTPException(404, "No such camera")
        if b is None:
            raise HTTPException(503, "Detection not initialised")
        try:
            img = await b.client.snapshot(cid)
        except VisionError as e:
            raise HTTPException(404, str(e))
        return Response(img, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.websocket("/api/cameras/{cid}/stream")
    async def stream(ws: WebSocket, cid: str):
        m = get_camera_manager()
        user = ws_principal(ws)
        if m is None or user is None:
            await ws.close(code=1008)
            return
        if cid not in m.cameras or not m.cameras[cid].get("enabled", True):
            await ws.close(code=1008)
            return
        await ws.accept()
        url, headers = m.go2rtc.ws_target(STREAM_PREFIX + cid)
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, headers=headers, heartbeat=20,
                                              max_msg_size=8 * 1024 * 1024) as up:
                    await _pump(ws, up)
        except Exception as e:                            # noqa: BLE001
            logger.info("[cameras] stream %s for %s ended: %s", cid, user, type(e).__name__)
        finally:
            try:
                await ws.close()
            except Exception:                             # noqa: BLE001
                pass

    # Setup (admin). Fixed paths first: /api/cameras/{cid} would take them.
    @app.post("/api/cameras/discover")
    async def discover(_=Depends(require_scope("admin"))):
        from modules.onvif import discover as onvif_discover
        found = await onvif_discover()
        known = {(c.get("onvif") or {}).get("host") for c in _mgr().cameras.values()}
        return {"cameras": [{**f, "added": f["host"] in known} for f in found]}

    @app.post("/api/cameras/probe")
    async def probe(body: Dict[str, Any], _=Depends(require_scope("admin"))):
        """An ONVIF camera's profiles and stream URIs, for the add form."""
        from modules.onvif import OnvifCamera, OnvifError
        host = str(body.get("host") or "").strip()
        if not host or any(c in host for c in "/@ \r\n"):
            raise HTTPException(400, "Enter the camera's IP or hostname")
        cam = OnvifCamera(host, int(body.get("port") or 80),
                          str(body.get("username") or ""), str(body.get("password") or ""))
        try:
            profiles = await cam.profiles()
            events = "events" in await cam.services()
        except OnvifError as e:
            raise HTTPException(502, str(e))
        return {"profiles": profiles, "events": events}

    @app.get("/api/cameras/vision")
    async def vision_status(_=Depends(require_scope("admin"))):
        """Whether ZMM reaches the detection sidecar and what it runs on.
        Enabling it is the ZMM Manager's job."""
        from modules.vision import get_vision_bridge
        b = get_vision_bridge()
        if b is None:
            raise HTTPException(503, "Detection not initialised")
        return b.public()

    @app.get("/api/cameras/go2rtc")
    async def go2rtc_status(_=Depends(require_scope("admin"))):
        """Whether ZMM reaches go2rtc. Enabling it is the ZMM Manager's job."""
        m = _mgr()
        s = m.go2rtc.settings
        return {"url": s["url"], "listen": s["listen"], "credentials": bool(s.get("password")),
                "healthy": await m.go2rtc.healthy(), "error": m.last_error}

    @app.put("/api/cameras/go2rtc")
    async def go2rtc_settings(body: Dict[str, Any], _=Depends(require_scope("admin"))):
        """Where go2rtc is and what it listens on, for a docker host or one ZMM
        didn't set up. The config is rewritten and go2rtc told to reload it."""
        from modules.go2rtc import Go2rtcError as E, save_settings, write_config
        m = _mgr()
        try:
            s = save_settings({k: body[k] for k in ("url", "listen", "username", "password") if k in body})
        except ValueError as e:
            raise HTTPException(400, str(e))
        write_config(s)
        try:
            await m.go2rtc.restart()          # still at the old address and credentials
        except E as e:
            logger.info("[cameras] go2rtc restart after settings change: %s", e)
        m.go2rtc.settings = s
        await asyncio.sleep(2)
        await m.reconcile()
        return {"url": s["url"], "listen": s["listen"], "healthy": await m.go2rtc.healthy()}

    @app.post("/api/cameras")
    async def add_camera(body: Dict[str, Any], _=Depends(require_scope("admin"))):
        try:
            return await _mgr().add(body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.put("/api/cameras/{cid}")
    async def update_camera(cid: str, body: Dict[str, Any], _=Depends(require_scope("admin"))):
        try:
            return await _mgr().update(cid, body)
        except KeyError:
            raise HTTPException(404, "No such camera")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/cameras/{cid}")
    async def delete_camera(cid: str, _=Depends(require_scope("admin"))):
        if not await _mgr().delete(cid):
            raise HTTPException(404, "No such camera")
        return {"success": True}

async def _pump(ws: WebSocket, up) -> None:
    """Both directions until either side closes: the browser's mse request up,
    go2rtc's codec reply and fMP4 fragments down."""
    import aiohttp
    from starlette.websockets import WebSocketDisconnect

    async def down():
        async for msg in up:
            if msg.type == aiohttp.WSMsgType.BINARY:
                await ws.send_bytes(msg.data)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                await ws.send_text(msg.data)
            else:
                break

    async def upward():
        try:
            while True:
                text = await ws.receive_text()
                if len(text) > 4096:
                    break                   # an mse request is a codec list, not a payload
                await up.send_str(text)
        except WebSocketDisconnect:
            pass

    tasks = [asyncio.create_task(down()), asyncio.create_task(upward())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
