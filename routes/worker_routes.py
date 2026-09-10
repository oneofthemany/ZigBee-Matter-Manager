"""
Workers API — household state a person or a rule sets.

Configuration is `automation:write`: creating a worker creates something rules
depend on, and deleting one changes what every rule reading it decides. Setting
a worker's value is `device:write` instead, because that is the same act as
pressing a switch — the whole point of a worker is that the household can flip
it. Reading is `automation:read`. See docs/workers.md.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from modules.auth_middleware import require_scope
from modules.workers import (
    MAX_WORKERS, WORKER_TYPES, get_worker_manager,
)

logger = logging.getLogger("routes.workers")


class WorkerCreate(BaseModel):
    id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=48)
    type: str
    icon: Optional[str] = None
    description: Optional[str] = ""
    enabled: bool = True
    restore: bool = True
    # Type-specific. Every field is optional here and defaulted by the manager,
    # so one payload shape serves all six types rather than six near-identical
    # models that would drift apart the first time a type gains a field.
    initial: Optional[Any] = None
    options: Optional[List[str]] = None
    default_seconds: Optional[int] = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    unit: Optional[str] = None
    reset_at: Optional[str] = None


class WorkerUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=48)
    icon: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    restore: Optional[bool] = None
    initial: Optional[Any] = None
    options: Optional[List[str]] = None
    default_seconds: Optional[int] = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    unit: Optional[str] = None
    reset_at: Optional[str] = None


class WorkerCommand(BaseModel):
    command: str
    value: Optional[Any] = None


def register_worker_routes(app: FastAPI,
                           automation_getter: Optional[Callable[[], Any]] = None
                           ) -> None:

    def _mgr():
        m = get_worker_manager()
        if not m:
            raise HTTPException(503, "Worker manager not initialised")
        return m

    def _types_payload() -> Dict[str, Any]:
        return {k: dict(v) for k, v in WORKER_TYPES.items()}

    @app.get("/api/workers")
    async def list_workers(_=Depends(require_scope("automation:read"))):
        return {"workers": _mgr().list(), "types": _types_payload(),
                "max": MAX_WORKERS}

    @app.get("/api/workers/types")
    async def worker_types(_=Depends(require_scope("automation:read"))):
        """Type catalogue, so the page builds its form from the server."""
        return {"types": _types_payload()}

    @app.get("/api/workers/usage")
    async def worker_usage(_=Depends(require_scope("automation:read"))):
        """Which rules trigger on, or command, each worker."""
        engine = automation_getter() if automation_getter else None
        rules = engine.get_rules() if engine else []
        return {"usage": _mgr().usage(rules)}

    @app.get("/api/workers/{worker_id}")
    async def get_worker(worker_id: str,
                         _=Depends(require_scope("automation:read"))):
        worker = _mgr().get(worker_id)
        if not worker:
            raise HTTPException(404, f"No worker '{worker_id}'")
        return worker.describe()

    @app.post("/api/workers")
    async def create_worker(payload: WorkerCreate,
                            _=Depends(require_scope("automation:write"))):
        result = _mgr().create(payload.dict(exclude_none=True))
        if not result.get("success"):
            raise HTTPException(400, result.get("error"))
        return result

    @app.put("/api/workers/{worker_id}")
    async def update_worker(worker_id: str, payload: WorkerUpdate,
                            _=Depends(require_scope("automation:write"))):
        result = _mgr().update(worker_id, payload.dict(exclude_none=True))
        if not result.get("success"):
            raise HTTPException(400, result.get("error"))
        return result

    @app.delete("/api/workers/{worker_id}")
    async def delete_worker(worker_id: str,
                            _=Depends(require_scope("automation:write"))):
        result = _mgr().delete(worker_id)
        if not result.get("success"):
            raise HTTPException(404, result.get("error"))
        return result

    @app.post("/api/workers/{worker_id}/command")
    async def command_worker(worker_id: str, payload: WorkerCommand,
                             _=Depends(require_scope("device:write"))):
        result = await _mgr().command(worker_id, payload.command, payload.value)
        if not result.get("success"):
            raise HTTPException(400, result.get("error"))
        return result

    logger.info("Worker routes registered")
