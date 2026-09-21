"""
Browser security headers and the CSP report sink. See auth.md §Headers.

Two CSP headers ship together: an enforced one carrying only directives that
cannot break the SPA, and a Report-Only one carrying the strict target.
Promotion is a code change, never a setting — enforcing early breaks the SPA
including the settings page, leaving no way back.

No HSTS: the first-boot cert is self-signed, so it would only remove the
browser's click-through and strand the user on their own hub.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger("modules.security_headers")

#: Anonymous by necessity — a report can outlive the session cookie — so it
#: is rate-limited and size-capped below.
CSP_REPORT_PATH = "/api/csp/report"

#: Directives safe to enforce against the SPA as it stands.
_ENFORCED: Tuple[Tuple[str, str], ...] = (
    ("frame-ancestors", "'self'"),
    ("base-uri", "'self'"),
    ("form-action", "'self'"),
    ("object-src", "'none'"),
)

#: Target policy. `data:` covers the MFA QR, `blob:` the client-built audio.
_TARGET: Tuple[Tuple[str, str], ...] = (
    ("default-src", "'self'"),
    ("script-src", "'self'"),
    ("style-src", "'self'"),
    ("img-src", "'self' data: blob:"),
    ("media-src", "'self' blob:"),
    ("font-src", "'self'"),
    ("connect-src", "'self'"),
    ("frame-ancestors", "'self'"),
    ("base-uri", "'self'"),
    ("form-action", "'self'"),
    ("object-src", "'none'"),
)


def _policy(directives: Tuple[Tuple[str, str], ...],
            report_uri: Optional[str] = None) -> str:
    out = "; ".join(f"{k} {v}" for k, v in directives)
    if report_uri:
        out += f"; report-uri {report_uri}"
    return out


class _ReportLimiter:
    """Caps report logging at `burst` per `window`: an open endpoint is a
    log-flooding primitive. Overflow is counted, then summarised."""

    def __init__(self, burst: int = 20, window: float = 60.0):
        self.burst = burst
        self.window = window
        self._start = 0.0
        self._seen = 0
        self._dropped = 0

    def allow(self) -> bool:
        now = time.monotonic()
        if now - self._start > self.window:
            if self._dropped:
                logger.warning("[csp] %d further violation report(s) suppressed",
                               self._dropped)
            self._start, self._seen, self._dropped = now, 0, 0
        if self._seen < self.burst:
            self._seen += 1
            return True
        self._dropped += 1
        return False


class SecurityHeadersMiddleware:
    """Headers on every response. Registered last so it sits outermost and
    covers auth's own 401/403."""

    def __init__(self):
        self._enforced = _policy(_ENFORCED)
        self._target = _policy(_TARGET, report_uri=CSP_REPORT_PATH)

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "same-origin")
        # Alongside frame-ancestors, for browsers honouring only this.
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("Content-Security-Policy", self._enforced)
        h.setdefault("Content-Security-Policy-Report-Only", self._target)
        return response


def register_csp_report_route(app, limiter: Optional[_ReportLimiter] = None):
    """Mount the violation sink. Anonymous: see CSP_REPORT_PATH."""
    lim = limiter or _ReportLimiter()

    # Literal, not CSP_REPORT_PATH: harness.py finds routes by reading
    # decorators and cannot see a constant. A test pins the two together.
    @app.post("/api/csp/report")
    async def csp_report(request: Request):
        # Cap the body: this endpoint is unauthenticated by necessity.
        raw = await request.body()
        if len(raw) > 8192:
            return JSONResponse({"ok": True}, status_code=204)
        if not lim.allow():
            return JSONResponse({"ok": True}, status_code=204)
        try:
            report = (json.loads(raw) or {}).get("csp-report", {})
            logger.warning(
                "[csp] %s blocked %s on %s",
                report.get("violated-directive") or "?",
                report.get("blocked-uri") or "?",
                report.get("document-uri") or "?",
            )
        except (ValueError, AttributeError):
            logger.debug("[csp] unparseable report (%d bytes)", len(raw))
        return JSONResponse({"ok": True}, status_code=204)

    return csp_report
