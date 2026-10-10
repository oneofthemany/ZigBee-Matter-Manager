"""
go2rtc config and client (modules/go2rtc.py): the generated config shuts every
server but the authenticated API, and the client talks to it as documented.
"""

from __future__ import annotations

import asyncio
import stat
import tempfile
from pathlib import Path

import yaml
from harness import Checker

from modules import go2rtc as G


class FakeApi:
    def __init__(self):
        self.calls = []
        self.status = 200
        self.image = b"\xff\xd8\xff\xe0jpeg"

    async def __call__(self, method, url, params=None, auth=None, raw=False):
        self.calls.append((method, url, params, auth))
        if self.status != 200:
            return self.status, {}
        if url.endswith("/api/frame.jpeg"):
            return 200, self.image
        if url.endswith("/api/streams") and method == "GET":
            return 200, {"zmm_front": {}}
        return 200, {}


def run() -> Checker:
    c = Checker("go2rtc")
    with tempfile.TemporaryDirectory() as tmp:
        saved = G.SECRETS_FILE, G.CONFIG_DIR
        G.SECRETS_FILE, G.CONFIG_DIR = str(Path(tmp) / "secrets.yaml"), Path(tmp) / "go2rtc"
        Path(G.SECRETS_FILE).write_text("fuel_finder:\n  client_id: keep\n")
        try:
            c.section("config")
            s = G.ensure_credentials()
            c.check("API credentials are generated once and kept",
                    s["username"] == "zmm" and len(s["password"]) >= 24
                    and G.ensure_credentials()["password"] == s["password"])
            c.check("they go in the secrets file beside what was there",
                    "keep" in Path(G.SECRETS_FILE).read_text())
            path = G.write_config()
            cfg = yaml.safe_load(path.read_text())
            c.check("the API needs the credentials, from localhost too",
                    cfg["api"]["password"] == s["password"] and cfg["api"]["local_auth"] is True, cfg["api"])
            c.check("the API listens on loopback by default", cfg["api"]["listen"] == "127.0.0.1:1984")
            c.check("go2rtc's RTSP, WebRTC and HomeKit servers are switched off",
                    cfg["rtsp"]["listen"] == "" and cfg["webrtc"]["listen"] == "" and cfg["srtp"]["listen"] == "")
            c.check("the config (with the password) is 0600", stat.S_IMODE(path.stat().st_mode) == 0o600)
            c.check("there is no inline `streams: {}` for go2rtc's config patcher to choke on",
                    "streams" not in cfg and "{}" not in path.read_text(), path.read_text())
            for bad in ({"url": "file:///etc/passwd"}, {"listen": "1984; rm -rf"}, {"listen": ""}):
                try:
                    G.save_settings(bad)
                    c.check(f"bad setting {bad} refused", False)
                except ValueError:
                    c.check(f"bad setting {bad} refused", True)

            c.section("client")
            api = FakeApi()
            g = G.Go2rtc(G.load_settings(), http=api)

            async def scenario():
                await g.put_stream("zmm_front", "rtsp://u:p@cam/1")
                await g.delete_stream("zmm_old")
                img = await g.snapshot("zmm_front", width=640)
                return img, await g.streams(), await g.healthy()
            img, streams, healthy = asyncio.run(scenario())
            c.check("a stream is put by name and source",
                    api.calls[0][:3] == ("PUT", "http://127.0.0.1:1984/api/streams",
                                         {"name": "zmm_front", "src": "rtsp://u:p@cam/1"}), api.calls[0])
            c.check("a stream is deleted by name", api.calls[1][2] == {"src": "zmm_old"})
            c.check("every call carries the credentials", all(call[3] == ("zmm", s["password"]) for call in api.calls))
            c.check("snapshots come back as JPEG", img.startswith(b"\xff\xd8") and api.calls[2][2]["width"] == 640)
            c.check("streams and health are read", streams == {"zmm_front": {}} and healthy)
            api.image = b"<html>error</html>"
            try:
                asyncio.run(g.snapshot("zmm_front"))
                c.check("a non-image answer isn't passed off as a snapshot", False)
            except G.Go2rtcError:
                c.check("a non-image answer isn't passed off as a snapshot", True)
            api.status = 401
            try:
                asyncio.run(g.streams())
                c.check("refused credentials say so", False)
            except G.Go2rtcError as e:
                c.check("refused credentials say so", "credentials" in str(e), str(e))

            c.section("go2rtc can't save a stream to its config")

            class SaveFails(FakeApi):
                """go2rtc's PUT/DELETE: acts in memory, then 400s on the config save."""
                def __init__(self):
                    super().__init__()
                    self.live = {}

                async def __call__(self, method, url, params=None, auth=None, raw=False):
                    self.calls.append((method, url, params, auth))
                    if url.endswith("/api/streams") and method == "GET":
                        return 200, dict(self.live)
                    if method == "PUT":
                        if params["src"].startswith("exec:"):
                            return 400, "streams: source from insecure producer: rtsp://admin:hunter2@cam/1"
                        self.live[params["name"]] = {}
                        return 400, "yaml: line 12: mapping values are not allowed in this context"
                    if method == "DELETE":
                        self.live.pop(params["src"], None)
                        return 400, "yaml: path not exist"
                    return 200, {}
            sf = SaveFails()
            g2 = G.Go2rtc(G.load_settings(), http=sf)
            asyncio.run(g2.put_stream("zmm_front", "rtsp://cam/1"))
            c.check("a stream that is live despite the 400 counts as added", "zmm_front" in sf.live)
            asyncio.run(g2.delete_stream("zmm_front"))
            c.check("and one that is gone despite the 400 counts as removed", "zmm_front" not in sf.live)
            try:
                asyncio.run(g2.put_stream("zmm_bad", "exec:rm"))
                c.check("a stream go2rtc really refused is still an error", False)
            except G.Go2rtcError as e:
                c.check("a stream go2rtc really refused is still an error", e.status == 400, str(e))
                c.check("the error carries go2rtc's reason with credentials masked",
                        "insecure producer" in str(e) and "hunter2" not in str(e) and "***@" in str(e), str(e))

            url, headers = g.ws_target("zmm_front door")
            c.check("the stream websocket carries Basic auth and an encoded name",
                    url == "ws://127.0.0.1:1984/api/ws?src=zmm_front%20door"
                    and headers["Authorization"].startswith("Basic "), (url, headers))
            shared = g.stream_url("zmm_front door")
            c.check("another local reader is given the authenticated API stream, not RTSP",
                    shared.startswith("http://") and "@127.0.0.1:1984/api/stream.mp4?src=zmm_front%20door" in shared
                    and g.settings["password"] in shared, shared)
        finally:
            G.SECRETS_FILE, G.CONFIG_DIR = saved
    return c
