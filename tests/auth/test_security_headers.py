"""
The headers reach the response, auth's own 401/403 included — a clickjacked
login page is still clickjacked. The ordering that makes that true is easy to
get backwards, so it is asserted rather than assumed.

Needs FastAPI, so run_all skips it on a bare host (AGENTS.md §The dev box).
"""

from __future__ import annotations

from pathlib import Path

from harness import Checker

from modules.auth_scopes import scope_for_path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from modules.security_headers import (
    CSP_REPORT_PATH,
    SecurityHeadersMiddleware,
    _ReportLimiter,
    register_csp_report_route,
)


def _client(limiter=None) -> TestClient:
    app = FastAPI()

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "nope"}, status_code=403)

    register_csp_report_route(app, limiter=limiter)
    mw = SecurityHeadersMiddleware()
    app.add_middleware(BaseHTTPMiddleware, dispatch=mw.dispatch)
    return TestClient(app, raise_server_exceptions=False)


def run() -> Checker:
    c = Checker("security_headers")
    client = _client()

    c.section("the baseline headers are present")
    h = client.get("/ok").headers
    c.check("X-Content-Type-Options is nosniff",
            h.get("x-content-type-options") == "nosniff", h.get("x-content-type-options"))
    c.check("Referrer-Policy is same-origin",
            h.get("referrer-policy") == "same-origin", h.get("referrer-policy"))
    c.check("X-Frame-Options is SAMEORIGIN",
            h.get("x-frame-options") == "SAMEORIGIN", h.get("x-frame-options"))

    c.section("HSTS is deliberately absent")
    c.check("no Strict-Transport-Security against the self-signed cert",
            "strict-transport-security" not in h, dict(h))

    c.section("both CSP headers ship, and only the safe one is enforced")
    enforced = h.get("content-security-policy") or ""
    report = h.get("content-security-policy-report-only") or ""
    c.check("an enforced policy is sent", bool(enforced), enforced)
    c.check("a report-only policy is sent", bool(report), report)
    c.check("enforced stops framing", "frame-ancestors 'self'" in enforced, enforced)
    c.check("enforced pins base-uri", "base-uri 'self'" in enforced, enforced)
    c.check("enforced blocks plugins", "object-src 'none'" in enforced, enforced)
    # Enforcing script-src would break the SPA's 55 inline handlers.
    c.check("enforced does NOT restrict script-src",
            "script-src" not in enforced, enforced)
    c.check("report-only DOES restrict script-src",
            "script-src 'self'" in report, report)
    c.check("report-only names a report-uri",
            f"report-uri {CSP_REPORT_PATH}" in report, report)
    c.check("report-only allows the MFA QR data: URI",
            "data:" in report, report)

    c.section("headers ride on auth's own refusals")
    hb = client.get("/boom").headers
    c.check("a 403 still carries the frame guard",
            "frame-ancestors 'self'" in (hb.get("content-security-policy") or ""),
            hb.get("content-security-policy"))
    c.check("a 403 still carries nosniff",
            hb.get("x-content-type-options") == "nosniff")

    c.section("there is no switch that can enforce the strict policy")
    # Promotion is a code change once static/ is clean, never a toggle:
    # enforcing early breaks the settings page that would undo it.
    import inspect
    src = inspect.getsource(SecurityHeadersMiddleware)
    c.check("the middleware takes no enforcement flag",
            "csp_enforce" not in src, src[:200])
    c.check("both headers always ship together",
            bool(enforced) and bool(report))

    c.section("the report sink accepts and is capped")
    r = client.post(CSP_REPORT_PATH, json={
        "csp-report": {"violated-directive": "script-src",
                       "blocked-uri": "inline",
                       "document-uri": "https://hub/index.html"}})
    c.check("a well-formed report is accepted", r.status_code == 204, r.status_code)
    c.check("junk does not 500",
            client.post(CSP_REPORT_PATH, content=b"not json").status_code == 204)
    c.check("an oversized body is dropped, not parsed",
            client.post(CSP_REPORT_PATH,
                        content=b"x" * 9000).status_code == 204)
    # A body on a 204 is what uvicorn refuses to send: it raises mid-send and
    # kills the HTTP/1.1 connection, taking the browser's queued requests
    # (a <script> tag, an API call) with it. Every path must answer empty.
    c.check("the ack carries no body", r.content == b"", r.content)
    c.check("nor does the junk path",
            client.post(CSP_REPORT_PATH, content=b"not json").content == b"")
    c.check("nor the oversized path",
            client.post(CSP_REPORT_PATH, content=b"x" * 9000).content == b"")
    c.check("and no content-length is declared",
            "content-length" not in {k.lower() for k in r.headers},
            dict(r.headers))

    c.section("the constant and the decorator agree")
    # The route is a literal so the scanner sees it; this pins the pair.
    src = (Path(__file__).resolve().parents[2]
           / "modules" / "security_headers.py").read_text()
    c.check("the decorator uses the same path as CSP_REPORT_PATH",
            f'@app.post("{CSP_REPORT_PATH}")' in src, CSP_REPORT_PATH)
    c.check("and the scope table maps it",
            scope_for_path(CSP_REPORT_PATH, "POST") is not None)

    c.section("report flooding is rate limited")
    lim = _ReportLimiter(burst=3, window=60.0)
    c.check("the limiter allows its burst",
            all(lim.allow() for _ in range(3)))
    c.check("and refuses past it", not lim.allow())
    c.check("and keeps refusing within the window", not lim.allow())

    return c


if __name__ == "__main__":
    run()
