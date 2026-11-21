"""
/static text assets ship gzipped; everything else under /static, and every
Range request, ships byte-for-byte so media seeking and pre-compressed
formats are untouched.
"""

from __future__ import annotations

import gzip
import shutil
import tempfile
from pathlib import Path

from harness import Checker

from starlette.applications import Starlette
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient

from modules.static_compression import StaticCompression

REPO = Path(__file__).resolve().parents[2]


def run() -> Checker:
    c = Checker("static compression")
    tmp = Path(tempfile.mkdtemp(prefix="zmm_gz_"))
    try:
        js = ("var x = 1;\n" * 2000).encode()
        (tmp / "app.js").write_bytes(js)
        (tmp / "app.css").write_text("body{margin:0}\n" * 500)
        (tmp / "tiny.js").write_text("1")
        png = bytes(range(256)) * 40
        (tmp / "logo.png").write_bytes(png)

        app = Starlette()
        app.mount("/static", StaticCompression(StaticFiles(directory=str(tmp))), name="static")
        client = TestClient(app)
        gz = {"Accept-Encoding": "gzip"}

        r = client.get("/static/app.js", headers=gz)
        c.check("a script is sent gzipped", r.headers.get("content-encoding") == "gzip", dict(r.headers))
        c.check("the gzipped script decodes to the file", r.content == js)
        c.check("a gzipped body is smaller on the wire",
                int(r.headers.get("content-length", len(js))) < len(js) // 4, r.headers.get("content-length"))
        c.check("responses vary on Accept-Encoding", "accept-encoding" in r.headers.get("vary", "").lower())

        r = client.get("/static/app.css", headers=gz)
        c.check("a stylesheet is sent gzipped", r.headers.get("content-encoding") == "gzip")

        r = client.get("/static/app.js", headers={"Accept-Encoding": "identity"})
        c.check("a client without gzip gets the plain file",
                "content-encoding" not in r.headers and r.content == js)

        r = client.get("/static/tiny.js", headers=gz)
        c.check("a file under the size floor is sent plain", "content-encoding" not in r.headers)

        r = client.get("/static/logo.png", headers=gz)
        c.check("an image is sent byte-for-byte",
                "content-encoding" not in r.headers and r.content == png, dict(r.headers))

        r = client.get("/static/app.js", headers={**gz, "Range": "bytes=0-9"})
        c.check("a Range request gets the raw byte range",
                r.status_code == 206 and "content-encoding" not in r.headers and r.content == js[:10],
                (r.status_code, dict(r.headers)))

        r = client.get("/static/missing.js", headers=gz)
        c.check("a missing file is still a 404", r.status_code == 404)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    route = (REPO / "main.py").read_text()
    c.check("main.py mounts /static through StaticCompression",
            'app.mount("/static", StaticCompression(StaticFiles(' in route)
    return c


if __name__ == "__main__":
    run()
