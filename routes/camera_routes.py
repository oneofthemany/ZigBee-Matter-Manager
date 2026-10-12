"""
Camera API: viewing is camera:read, setup is admin. The stream is a websocket
proxied to go2rtc, so cameras and go2rtc are never exposed to the browser.
See docs/cameras.md.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
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
    async def detection_snapshot(cid: str, view: str = "", _=Depends(require_scope("camera:read"))):
        """The frame of the camera's latest detection, boxes drawn; or with
        ?view=frame what the detector sees now, zones outlined."""
        from modules.vision import VisionError, get_vision_bridge
        b = get_vision_bridge()
        if cid not in _mgr().cameras:
            raise HTTPException(404, "No such camera")
        if b is None:
            raise HTTPException(503, "Detection not initialised")
        try:
            img = await b.client.snapshot(cid, "frame" if view == "frame" else "snapshot")
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


MAX_FACE_IMAGE = 12 * 1024 * 1024


def register_face_routes(app: FastAPI) -> None:
    """Enrolling people for face recognition: a pass-through to the vision
    sidecar, which owns the gallery. vision:enrol sees and changes faces;
    camera:read alone does neither. See docs/vision.md §Faces."""
    from modules.vision import VisionError, get_vision_bridge

    async def _faces(method: str, path: str = "", **kw) -> Any:
        b = get_vision_bridge()
        if b is None:
            raise HTTPException(503, "Detection not initialised")
        try:
            return await b.client.faces(method, path, **kw)
        except VisionError as e:
            if e.status and 400 <= e.status < 500:
                raise HTTPException(e.status, e.detail or str(e))
            raise HTTPException(503, "Object detection isn't running — enable it in the ZMM Manager")

    def _ids(pid: str, sid: str = "00000000", slot: str = "front") -> None:
        # These become part of the sidecar's URL.
        if not (re.fullmatch(r"[a-z0-9][a-z0-9_]{0,31}", pid) and re.fullmatch(r"[0-9a-f]{8}", sid)
                and slot in ("front", "left", "right", "down")):
            raise HTTPException(404, "No such person or picture")

    async def _image(request: Request) -> bytes:
        if int(request.headers.get("content-length") or 0) > MAX_FACE_IMAGE:
            raise HTTPException(413, "That picture is too large")
        data = await request.body()
        if not data or len(data) > MAX_FACE_IMAGE:
            raise HTTPException(400, "Send a picture")
        return data

    @app.get("/api/vision/faces")
    async def faces_list(_=Depends(require_scope("vision:enrol"))):
        return await _faces("GET")

    @app.post("/api/vision/faces")
    async def faces_add(body: Dict[str, Any], _=Depends(require_scope("vision:enrol"))):
        return await _faces("POST", body={"name": body.get("name")})

    # Fixed paths first: /api/vision/faces/{pid} would take them.
    @app.post("/api/vision/faces/detect")
    async def faces_detect(request: Request, _=Depends(require_scope("vision:enrol"))):
        """The faces in an uploaded picture, each with who it looks like."""
        return await _faces("POST", "/detect", content=await _image(request))

    @app.post("/api/vision/faces/detect/camera/{cid}")
    async def faces_detect_camera(cid: str, _=Depends(require_scope("vision:enrol")),
                                  __=Depends(require_scope("camera:read"))):
        """The faces a camera sees now, from its full-resolution still."""
        m = get_camera_manager()
        if m is None or cid not in m.cameras:
            raise HTTPException(404, "No such camera")
        try:
            img = await m.go2rtc.snapshot(STREAM_PREFIX + cid)   # not the cached one: this is the moment asked for
        except Go2rtcError as e:
            raise HTTPException(502, str(e))
        return await _faces("POST", "/detect", params={"camera": cid}, content=img)

    @app.patch("/api/vision/faces/{pid}")
    async def faces_rename(pid: str, body: Dict[str, Any], _=Depends(require_scope("vision:enrol"))):
        _ids(pid)
        return await _faces("PATCH", f"/{pid}", body={"name": body.get("name")})

    @app.delete("/api/vision/faces/{pid}")
    async def faces_delete(pid: str, _=Depends(require_scope("vision:enrol"))):
        _ids(pid)
        return await _faces("DELETE", f"/{pid}")

    @app.put("/api/vision/faces/{pid}/slot/{slot}")
    async def faces_slot(pid: str, slot: str, request: Request, _=Depends(require_scope("vision:enrol"))):
        _ids(pid, slot=slot)
        return await _faces("PUT", f"/{pid}/slot/{slot}", content=await _image(request))

    @app.post("/api/vision/faces/{pid}/samples")
    async def faces_add_sample(pid: str, body: Dict[str, Any], _=Depends(require_scope("vision:enrol"))):
        _ids(pid)
        return await _faces("POST", f"/{pid}/samples", body={"token": body.get("token"), "face": body.get("face")})

    @app.delete("/api/vision/faces/{pid}/samples/{sid}")
    async def faces_delete_sample(pid: str, sid: str, _=Depends(require_scope("vision:enrol"))):
        _ids(pid, sid)
        return await _faces("DELETE", f"/{pid}/samples/{sid}")

    @app.get("/api/vision/faces/{pid}/samples/{sid}")
    async def faces_sample(pid: str, sid: str, _=Depends(require_scope("vision:enrol"))):
        _ids(pid, sid)
        img = await _faces("GET", f"/{pid}/{sid}.jpg", raw=True)
        return Response(img, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})
