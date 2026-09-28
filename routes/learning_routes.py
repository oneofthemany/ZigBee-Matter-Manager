"""
Device learning routes (docs/plans/device-learning.md): the wizard's session,
review and save, profile history and rollback, export and import.
Registered from register_device_routes, under /api/device/{ieee}/...
"""
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from pydantic import BaseModel


class StepBegin(BaseModel):
    inputs: Dict[str, Any] = {}


class StepDecide(BaseModel):
    accept: List[int] = []


class TryWrite(BaseModel):
    candidate: Dict[str, Any]
    inputs: Dict[str, Any] = {}


class TryAnswer(BaseModel):
    changed: bool


class EntrySave(BaseModel):
    entry: dict


class EntryImport(BaseModel):
    payload: dict
    apply: bool = False


def register_learning_routes(app: FastAPI, get_zigbee_service):
    from modules import device_learning as learning

    def _device(ieee: str):
        svc = get_zigbee_service()
        return svc, (svc.devices.get(ieee) if svc is not None else None)

    missing = {"success": False, "error": "Device not found"}

    @app.get("/api/device/{ieee}/learn")
    async def learn_state(ieee: str):
        _, dev = _device(ieee)
        return learning.state(dev) if dev else missing

    @app.post("/api/device/{ieee}/learn/start")
    async def learn_start(ieee: str):
        _, dev = _device(ieee)
        return learning.start(dev) if dev else missing

    @app.post("/api/device/{ieee}/learn/end")
    async def learn_end(ieee: str):
        _, dev = _device(ieee)
        return await learning.end(dev) if dev else missing

    @app.post("/api/device/{ieee}/learn/step/{key}/begin")
    async def learn_begin(ieee: str, key: str, request: StepBegin):
        _, dev = _device(ieee)
        return await learning.begin(dev, key, request.inputs) if dev else missing

    @app.post("/api/device/{ieee}/learn/step/{key}/finish")
    async def learn_finish(ieee: str, key: str):
        _, dev = _device(ieee)
        return await learning.finish(dev, key) if dev else missing

    @app.post("/api/device/{ieee}/learn/step/{key}/decide")
    async def learn_decide(ieee: str, key: str, request: StepDecide):
        _, dev = _device(ieee)
        return learning.decide(dev, key, request.accept) if dev else missing

    @app.get("/api/device/{ieee}/learn/step/{key}/candidates")
    async def learn_candidates(ieee: str, key: str):
        _, dev = _device(ieee)
        return learning.candidates(dev, key) if dev else missing

    @app.post("/api/device/{ieee}/learn/step/{key}/try")
    async def learn_try(ieee: str, key: str, request: TryWrite):
        """Flip one setting while the user watches; it is put back on answer or timeout."""
        _, dev = _device(ieee)
        return await learning.try_write(dev, key, request.candidate, request.inputs) if dev else missing

    @app.post("/api/device/{ieee}/learn/step/{key}/answer")
    async def learn_answer(ieee: str, key: str, request: TryAnswer):
        _, dev = _device(ieee)
        return await learning.answer(dev, key, request.changed) if dev else missing

    @app.get("/api/device/{ieee}/learn/review")
    async def learn_review(ieee: str):
        _, dev = _device(ieee)
        return learning.review(dev) if dev else missing

    @app.post("/api/device/{ieee}/learn/save")
    async def learn_save(ieee: str, request: EntrySave):
        svc, dev = _device(ieee)
        if not dev:
            return missing
        out = learning.save(dev, request.entry)
        if out.get("success"):
            await svc.announce_device(ieee)
        return out

    @app.post("/api/device/{ieee}/profile/preview")
    async def profile_preview(ieee: str, request: EntrySave):
        _, dev = _device(ieee)
        return learning.preview_entry(dev, request.entry) if dev else missing

    @app.get("/api/device/{ieee}/profile/history")
    async def profile_history(ieee: str):
        from modules.device_profiles import get_profile_store, profile_for_device
        _, dev = _device(ieee)
        if not dev:
            return missing
        p = profile_for_device(dev)
        pid = p["id"] if p else None
        return {"success": True, "profile": pid,
                "source": (p.get("meta") or {}).get("source") if p else None,
                "history": get_profile_store().history(pid) if pid else []}

    @app.post("/api/device/{ieee}/profile/rollback")
    async def profile_rollback(ieee: str):
        from modules.device_profiles import get_profile_store, profile_for_device
        svc, dev = _device(ieee)
        if not dev:
            return missing
        p = profile_for_device(dev)
        if not p or (p.get("meta") or {}).get("source") != "user":
            return {"success": False, "error": "no profile of yours to roll back"}
        restored = get_profile_store().rollback(p["id"])
        learning.refresh(dev)
        await svc.announce_device(ieee)
        return {"success": True, "restored": bool(restored),
                "now": (learning.review(dev).get("based_on") or {})}

    @app.get("/api/device/{ieee}/profile/export")
    async def profile_export(ieee: str):
        _, dev = _device(ieee)
        return {"success": True, **learning.export(dev)} if dev else missing

    @app.post("/api/device/{ieee}/profile/import")
    async def profile_import(ieee: str, request: EntryImport):
        svc, dev = _device(ieee)
        if not dev:
            return missing
        out = learning.import_entry(dev, request.payload, request.apply)
        if out.get("success") and request.apply:
            await svc.announce_device(ieee)
        return out
