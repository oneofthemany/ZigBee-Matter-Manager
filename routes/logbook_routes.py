"""
Logbook API: the live log's history, and the trace behind one line.
See docs/logbook.md.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException

from modules.auth_middleware import require_scope
from modules.logbook import RETENTION_DAYS, get_logbook

logger = logging.getLogger("routes.logbook")


def register_logbook_routes(app: FastAPI) -> None:

    def _lb():
        lb = get_logbook()
        if lb is None:
            raise HTTPException(503, "Logbook not initialised")
        return lb

    @app.get("/api/logbook/events")
    async def events(before: Optional[float] = None, limit: int = 200,
                     ieee: Optional[str] = None, q: Optional[str] = None,
                     _=Depends(require_scope("system:read"))):
        """Log lines, newest first; `before` (a ts) pages back."""
        rows = await _lb().events(before=before, limit=limit, ieee=ieee, q=(q or "")[:100] or None)
        return {"events": rows, "retention_days": RETENTION_DAYS, "dropped": _lb().dropped}

    @app.get("/api/logbook/trace/{event_id}")
    async def trace(event_id: str, _=Depends(require_scope("system:read"))):
        if not event_id.isalnum() or len(event_id) > 32:
            raise HTTPException(400, "Bad event id")
        t = await _lb().trace(event_id)
        if t is None:
            raise HTTPException(404, f"That line is older than the {RETENTION_DAYS} days kept, or was never stored")
        return t
