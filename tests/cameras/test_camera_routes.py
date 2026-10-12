"""
Camera API over real FastAPI, and the stream websocket proxied to a real
websocket server standing in for go2rtc. Skipped without FastAPI.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
from pathlib import Path

from harness import Checker

import test_cameras as T


def _fake_go2rtc():
    """A websocket server on a free port answering like go2rtc's /api/ws."""
    from aiohttp import web
    seen = {}

    async def ws_handler(request):
        seen["auth"] = request.headers.get("Authorization", "")
        seen["src"] = request.query.get("src")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            seen["sent"] = msg.data
            await ws.send_str('{"type":"mse","value":"video/mp4; codecs=\\"avc1.640029\\""}')
            await ws.send_bytes(b"\x00\x00\x00\x18ftypfragment")
        return ws

    app = web.Application()
    app.router.add_get("/api/ws", ws_handler)
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", 0)
    loop.run_until_complete(site.start())
    port = site._server.sockets[0].getsockname()[1]
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return port, seen, lambda: loop.call_soon_threadsafe(loop.stop)


def run() -> Checker:
    c = Checker("camera_routes")
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect
    except ImportError:
        print("\n  skipped (fastapi not installed)")
        return c

    from modules import auth as Auth
    from modules import cameras as C
    from modules import vision as V
    from modules.auth_middleware import AuthMiddleware, _derive_session_secret, issue_session_cookie
    from modules.go2rtc import Go2rtc
    from routes.camera_routes import register_camera_routes

    port, seen, stop_server = _fake_go2rtc()
    with tempfile.TemporaryDirectory() as tmp:
        saved = C.SECRETS_FILE
        C.SECRETS_FILE = str(Path(tmp) / "secrets.yaml")
        auth = Auth.AuthManager(config_path=Path(tmp) / "auth.yaml")
        auth.load()
        for name, scopes in Auth.DEFAULT_GROUPS.items():
            auth.groups[name] = Auth.Group(name=name, scopes=list(scopes))

        async def users():
            await auth.create_user("root", "correct-horse-1", groups=["admins"])
            await auth.create_user("resident", "correct-horse-2", groups=["users"])
            await auth.create_user("guest", "correct-horse-3", groups=["viewers"])
        asyncio.run(users())
        Auth.set_auth_manager(auth)
        secret = _derive_session_secret(str(auth.config_path))
        cookie = {u: issue_session_cookie(u, secret) for u in ("root", "resident", "guest")}

        g = Go2rtc({"url": f"http://127.0.0.1:{port}", "listen": "", "username": "zmm", "password": "pw"})
        fake = T.FakeGo2rtc()
        m = C.CameraManager(path=Path(tmp) / "cameras.json", go2rtc=g)
        m.go2rtc = fake
        asyncio.run(m.add({"name": "Front", "url": "rtsp://admin:secret@cam/1"}))
        m.go2rtc = g                       # the stream goes to the real fake server
        m.snapshot = lambda cid: T.FakeGo2rtc().snapshot(cid) if cid in m.cameras else (_ for _ in ()).throw(KeyError(cid))
        C.set_camera_manager(m)
        try:
            app = FastAPI()
            app.add_middleware(AuthMiddleware, auth_manager=auth, enforce=True)
            register_camera_routes(app)
            api = TestClient(app)

            def as_(user):
                api.cookies.clear()
                api.cookies.set("zmm_session", cookie[user])

            c.section("viewing")
            as_("resident")
            r = api.get("/api/cameras")
            c.check("a household member lists cameras, without credentials",
                    r.status_code == 200 and r.json()["cameras"][0]["id"] == "front" and "secret" not in r.text, r.text)
            r = api.get("/api/cameras/front/snapshot")
            c.check("and gets an uncached JPEG snapshot",
                    r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
                    and r.headers["cache-control"] == "no-store", r.headers)
            c.check("an unknown camera is a 404", api.get("/api/cameras/nope/snapshot").status_code == 404)
            as_("guest")
            c.check("a viewer can't see cameras", api.get("/api/cameras").status_code == 403)
            c.check("or snapshots", api.get("/api/cameras/front/snapshot").status_code == 403)

            c.section("setup is admin")
            as_("resident")
            c.check("a household member can't add cameras",
                    api.post("/api/cameras", json={"name": "X", "url": "rtsp://x/1"}).status_code == 403)
            c.check("or reach go2rtc settings", api.get("/api/cameras/go2rtc").status_code == 403)
            c.check("or run discovery", api.post("/api/cameras/discover").status_code == 403)

            c.section("the stream websocket")

            def refused(user=None, headers=None, cid="front"):
                api.cookies.clear()
                if user:
                    api.cookies.set("zmm_session", cookie[user])
                try:
                    # A refusal closes before accepting, which raises here;
                    # getting in at all means it wasn't refused.
                    with api.websocket_connect(f"/api/cameras/{cid}/stream", headers=headers or {}):
                        return False
                except WebSocketDisconnect as e:
                    return e.code == 1008
            c.check("no session: refused", refused())
            c.check("a viewer without camera:read: refused", refused("guest"))
            c.check("a cross-site handshake riding the cookie: refused",
                    refused("resident", {"sec-fetch-site": "cross-site"}))
            c.check("an unknown camera: refused", refused("resident", cid="nope"))

            as_("resident")
            with api.websocket_connect("/api/cameras/front/stream",
                                       headers={"origin": "https://hub.example.com"}) as ws:
                ws.send_text('{"type":"mse","value":"avc1.640029"}')
                reply = ws.receive_text()
                frag = ws.receive_bytes()
            c.check("a household member's stream is proxied both ways, even behind a tunnel",
                    '"mse"' in reply and frag.startswith(b"\x00\x00\x00\x18ftyp")
                    and seen.get("sent") == '{"type":"mse","value":"avc1.640029"}', (reply, frag, seen))
            c.check("go2rtc is asked for ZMM's stream, with ZMM's credentials",
                    seen.get("src") == "zmm_front" and seen.get("auth", "").startswith("Basic "), seen)

            c.section("faces")
            from routes.camera_routes import register_face_routes
            asyncio.run(auth.create_user("keeper", "correct-horse-4", groups=["users"], extra_scopes=["vision:enrol"]))
            cookie["keeper"] = issue_session_cookie("keeper", secret)
            calls, answer = [], {"r": (200, {"people": []})}

            async def sidecar(method, url, token, **kw):
                calls.append((method, url.split("8556", 1)[-1], kw))
                if isinstance(answer["r"], Exception):
                    raise answer["r"]
                return answer["r"]
            V.set_vision_bridge(V.VisionBridge(m, V.VisionClient(token=lambda: "t", http=sidecar)))
            register_face_routes(app)
            m.go2rtc = fake                    # the still for "look now"
            as_("resident")
            c.check("a household member without the grant can't list faces", api.get("/api/vision/faces").status_code == 403)
            c.check("…or enrol one", api.put("/api/vision/faces/sean/slot/front", content=b"jpg").status_code == 403)
            c.check("and none of that reached the sidecar", calls == [], calls)
            as_("keeper")
            c.check("with vision:enrol the list comes through", api.get("/api/vision/faces").json() == {"people": []})
            answer["r"] = (200, {"id": "ab12cd34", "slot": "front"})
            r = api.put("/api/vision/faces/sean/slot/front", content=b"\xff\xd8photo")
            c.check("a photo is passed to the sidecar byte for byte",
                    r.status_code == 200 and calls[-1][:2] == ("PUT", "/faces/sean/slot/front")
                    and calls[-1][2]["content"] == b"\xff\xd8photo", calls[-1])
            n = len(calls)
            c.check("an id that isn't one never reaches the sidecar's URL",
                    api.put("/api/vision/faces/Sean%3Fx/slot/front", content=b"j").status_code == 404
                    and api.delete("/api/vision/faces/sean/samples/not-a-hex").status_code == 404
                    and api.put("/api/vision/faces/sean/slot/up", content=b"j").status_code == 404 and len(calls) == n, calls[n:])
            c.check("an empty upload is refused here", api.put("/api/vision/faces/sean/slot/front", content=b"").status_code == 400)
            answer["r"] = (422, {"error": "No face found — face the camera, in good light"})
            r = api.put("/api/vision/faces/sean/slot/front", content=b"jpg")
            c.check("the sidecar's reason for refusing a photo reaches the phone as it was written",
                    r.status_code == 422 and r.json()["detail"] == "No face found — face the camera, in good light", r.text)
            answer["r"] = (200, {"token": "tok", "faces": []})
            r = api.post("/api/vision/faces/detect/camera/front")
            c.check("looking through a camera sends its still, and says which camera",
                    r.status_code == 200 and calls[-1][1] == "/faces/detect" and calls[-1][2]["params"] == {"camera": "front"}
                    and calls[-1][2]["content"], calls[-1])
            c.check("an unknown camera is a 404", api.post("/api/vision/faces/detect/camera/nope").status_code == 404)
            answer["r"] = (200, b"\xff\xd8crop")
            r = api.get("/api/vision/faces/sean/samples/ab12cd34")
            c.check("a face crop is served as a private JPEG",
                    r.status_code == 200 and r.content == b"\xff\xd8crop" and r.headers["content-type"] == "image/jpeg"
                    and r.headers["cache-control"].startswith("private"), r.headers)
            answer["r"] = ConnectionError("down")
            c.check("with detection not running it says so, as a 503", api.get("/api/vision/faces").status_code == 503)
        finally:
            V.set_vision_bridge(None)
            C.set_camera_manager(None)
            Auth.set_auth_manager(None)
            C.SECRETS_FILE = saved
            stop_server()
    return c
