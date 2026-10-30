"""
Speaker serial panel API — /api/esp-serial/*, admin-only: it can restart and
reflash hardware, and backups hold Wi-Fi credentials. docs/esp-serial.md.
"""

from __future__ import annotations

from typing import Any, Callable, Dict

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from modules.auth_middleware import require_scope
from modules.esp_serial import MAX_IMAGE, BusyError, CooldownError, EspSerialManager, ImprovError
from modules.esp_esptool import EsptoolError

def _http(e: Exception) -> HTTPException:
    if isinstance(e, LookupError):
        return HTTPException(404, str(e))
    if isinstance(e, CooldownError):
        return HTTPException(429, str(e), headers={"Retry-After": str(e.remaining)})
    if isinstance(e, BusyError):
        return HTTPException(409, str(e))
    if isinstance(e, (ImprovError, EsptoolError)):
        return HTTPException(502, str(e))
    if isinstance(e, ValueError):
        return HTTPException(400, str(e))
    if isinstance(e, OSError):
        return HTTPException(503, f"The port could not be opened: {e.strerror or e}")
    raise e


def register_esp_serial_routes(app: FastAPI, get_manager: Callable[[], EspSerialManager]) -> None:
    admin = Depends(require_scope("admin"))

    def mgr() -> EspSerialManager:
        m = get_manager()
        if m is None:
            raise HTTPException(503, "Speaker serial not initialised")
        return m

    def flags(body: Dict[str, Any]) -> Dict[str, Any]:
        return {"acknowledge_restart": body.get("acknowledge_restart"),
                "override_cooldown": body.get("override_cooldown") is True}

    @app.get("/api/esp-serial/ports")
    async def ports(_=admin):
        return mgr().ports()

    @app.get("/api/esp-serial/library")
    async def library(_=admin):
        return mgr().library()

    @app.post("/api/esp-serial/images")
    async def upload(request: Request, name: str = "image.bin", _=admin):
        body = await request.body()
        if len(body) > MAX_IMAGE:
            raise HTTPException(413, "Image too large")
        try:
            return mgr().save_image(body, name)
        except ValueError as e:
            raise _http(e)

    @app.delete("/api/esp-serial/images/{iid}")
    async def delete_image(iid: str, _=admin):
        if not mgr().delete_image(iid):
            raise HTTPException(404, "No such image")
        return {"success": True}

    @app.get("/api/esp-serial/backups/{bid}/download")
    async def download(bid: str, _=admin):
        try:
            path, meta = mgr().source(f"backup:{bid}")
        except LookupError as e:
            raise _http(e)
        return FileResponse(path, media_type="application/octet-stream", filename=f"{meta['id']}.bin")

    @app.get("/api/esp-serial/jobs/{jid}")
    async def job(jid: str, _=admin):
        j = mgr().jobs.get(jid)
        if j is None:
            raise HTTPException(404, "No such job")
        return j

    @app.get("/api/esp-serial/ports/{name}/log")
    async def log(name: str, after: int = 0, _=admin):
        try:
            return mgr().log(name, after)
        except Exception as e:                  # noqa: BLE001
            raise _http(e)

    @app.post("/api/esp-serial/ports/{name}/log/stop")
    async def stop_log(name: str, _=admin):
        await mgr().stop_log(name)
        return {"success": True}

    @app.post("/api/esp-serial/ports/{name}/improv/status")
    async def improv_status(name: str, _=admin):
        try:
            return await mgr().improv_status(name)
        except Exception as e:                  # noqa: BLE001
            raise _http(e)

    @app.post("/api/esp-serial/ports/{name}/improv/wifi")
    async def improv_wifi(name: str, body: Dict[str, Any], _=admin):
        try:
            return await mgr().improv_wifi(name, str(body.get("ssid") or ""), str(body.get("password") or ""))
        except Exception as e:                  # noqa: BLE001
            raise _http(e)

    @app.post("/api/esp-serial/ports/{name}/info")
    async def info(name: str, body: Dict[str, Any], _=admin):
        m, f = mgr(), flags(body)
        try:
            m.precheck(name, **f)
        except Exception as e:                  # noqa: BLE001
            raise _http(e)
        return m.start_job("info", name, lambda: m.info(name, f["override_cooldown"]))

    @app.post("/api/esp-serial/ports/{name}/backup")
    async def backup(name: str, body: Dict[str, Any], _=admin):
        m, f = mgr(), flags(body)
        try:
            m.precheck(name, **f)
        except Exception as e:                  # noqa: BLE001
            raise _http(e)
        return m.start_job("backup", name, lambda: m.backup(name, f["override_cooldown"]))

    @app.post("/api/esp-serial/ports/{name}/flash")
    async def flash(name: str, body: Dict[str, Any], _=admin):
        m, f = mgr(), flags(body)
        ref = str(body.get("source") or "")
        try:
            m.flash_precheck(name, ref, str(body.get("confirm") or ""), **f)
        except Exception as e:                  # noqa: BLE001
            raise _http(e)
        return m.start_job("flash", name, lambda: m.flash(name, ref, f["override_cooldown"]))
