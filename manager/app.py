"""Manager sidecar FastAPI app (:8001).

CP2a: a minimal, always-on status page + JSON endpoint. It health-checks the app
over the pod-shared loopback and lists the deployment's containers via the runtime
socket. No auth yet (CP2a is for proving the sidecar works); auth + recovery
actions come in CP2b.
"""
import asyncio
import base64
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, Header
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from manager import containers, logs, recovery, upgrade, watchdog

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger("manager.app")

# The app serves HTTPS on the pod-shared loopback. verify=False: its cert is
# self-signed and we're talking to 127.0.0.1 inside the same netns.
APP_HEALTH_URL = os.environ.get("ZMM_APP_HEALTH_URL",
                                "https://127.0.0.1:8000/api/system/health")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the action token exists from first boot (its path is what the
    # dashboard tells users to look up when prompted).
    upgrade.get_token()
    # Run the auto-recovery watchdog for the lifetime of the manager.
    task = asyncio.create_task(watchdog.run_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="ZMM Manager", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


async def _app_health() -> dict:
    try:
        async with httpx.AsyncClient(verify=False, timeout=5.0) as cx:
            r = await cx.get(APP_HEALTH_URL)
            body = None
            if r.headers.get("content-type", "").startswith("application/json"):
                try:
                    body = r.json()
                except Exception:
                    body = None
            return {"ok": r.status_code == 200, "status_code": r.status_code, "body": body}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/status")
async def status():
    app_health = await _app_health()
    return {"app": app_health,
            "containers": await containers.list_containers(),
            "watchdog": watchdog.get_state(),
            "recovery": recovery.state(app_ok=app_health.get("ok"))}


@app.get("/healthz")
async def healthz():
    # Liveness for the manager itself (unauthenticated, cheap).
    return JSONResponse({"manager": "ok"})


# ── Live logs (CP2b, read-only, on request) ──────────────────────────────────
# SSE so the dashboard can use a plain EventSource; the stream ends when the
# client closes the connection, so nothing runs unless a user is watching.

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@app.get("/logs")
async def log_sources():
    """What can be streamed: this deployment's containers + DATA_DIR log files."""
    c = await containers.list_containers()
    return {"containers": [x["name"] for x in c.get("containers", [])],
            "files": logs.list_log_files()}


@app.get("/logs/file/{name}")
async def log_file(name: str, tail: int = 200):
    path = logs.resolve_log_file(name)
    if path is None:
        return JSONResponse({"error": "unknown log file"}, status_code=404)
    return StreamingResponse(logs.stream_file(path, tail=max(1, min(tail, 2000))),
                             media_type="text/event-stream", headers=_SSE_HEADERS)


@app.get("/logs/container/{name}")
async def log_container(name: str, tail: int = 200):
    if not logs.allowed_container(name):
        return JSONResponse({"error": "unknown container"}, status_code=404)
    return StreamingResponse(logs.stream_container(name, tail=max(1, min(tail, 2000))),
                             media_type="text/event-stream", headers=_SSE_HEADERS)


# ── Upgrade: rollback + image retention (CP2b) ───────────────────────────────
# Reads are open like the rest of the manager; ACTIONS require the bearer
# token from data/state/manager_token (shown in the app's Upgrade tab).

def _unauthorized() -> JSONResponse:
    return JSONResponse({"success": False, "error": "valid bearer token required "
                        "(data/state/manager_token on the host)"}, status_code=401)


@app.get("/upgrade")
async def upgrade_info():
    state = upgrade.version_state()
    return {
        "status": upgrade.read_status(),
        "current_version": state.get("current_version"),
        "previous_version": state.get("previous_version"),
        "retention_count": state.get("retention_count") or 2,
        "images": await upgrade.list_images(),
    }


@app.post("/upgrade/rollback")
async def upgrade_rollback(data: dict = Body(...),
                           authorization: str = Header(default="")):
    if not upgrade.check_token(authorization):
        return _unauthorized()
    version = str(data.get("version") or "").strip()
    if not version:
        return JSONResponse({"success": False, "error": "version required"},
                            status_code=400)
    ok, msg = await upgrade.rollback_to(version)
    return JSONResponse({"success": ok, "message": msg},
                        status_code=200 if ok else 409)


@app.post("/upgrade/gc")
async def upgrade_gc(authorization: str = Header(default="")):
    if not upgrade.check_token(authorization):
        return _unauthorized()
    ok, msg = upgrade.run_gc()
    return JSONResponse({"success": ok, "message": msg},
                        status_code=200 if ok else 409)


# ── Disaster recovery (CP2b) — replaces the in-app recovery_server ───────────
# Reads of the crash summary/backup list are open (same as /status); anything
# that reads code content or writes into the app container needs the token.

@app.get("/recovery")
async def recovery_state(path: str = ""):
    app_health = await _app_health()
    return {**recovery.state(app_ok=app_health.get("ok")),
            "backups": recovery.list_backups(path or None)}


@app.get("/recovery/backup-content")
async def recovery_backup_content(name: str,
                                  authorization: str = Header(default="")):
    if not upgrade.check_token(authorization):
        return _unauthorized()
    ok, content = recovery.backup_content(name)
    if not ok:
        return JSONResponse({"success": False, "error": content}, status_code=404)
    return {"success": True, "name": name, "content": content}


@app.get("/recovery/file-content")
async def recovery_file_content(path: str,
                                authorization: str = Header(default="")):
    if not upgrade.check_token(authorization):
        return _unauthorized()
    ok, content = await recovery.read_app_file(path)
    if not ok:
        return JSONResponse({"success": False, "error": content}, status_code=404)
    return {"success": True, "path": path, "content": content}


@app.post("/recovery/upload")
async def recovery_upload(data: dict = Body(...),
                          authorization: str = Header(default="")):
    if not upgrade.check_token(authorization):
        return _unauthorized()
    rel = data.get("path")
    content = data.get("content")
    content_b64 = data.get("content_base64")
    if not rel or (content is None and content_b64 is None):
        return JSONResponse({"success": False, "error": "path and content (or "
                            "content_base64) required"}, status_code=400)
    try:
        raw = (base64.b64decode(content_b64)
               if content_b64 is not None else str(content).encode("utf-8"))
    except Exception:
        return JSONResponse({"success": False, "error": "invalid base64"},
                            status_code=400)
    ok, msg = await recovery.write_app_file(rel, raw)
    return JSONResponse({"success": ok, "message" if ok else "error": msg},
                        status_code=200 if ok else 400)


@app.post("/recovery/restore")
async def recovery_restore(data: dict = Body(...),
                           authorization: str = Header(default="")):
    if not upgrade.check_token(authorization):
        return _unauthorized()
    name, rel = data.get("backup"), data.get("path")
    if not name or not rel:
        return JSONResponse({"success": False, "error": "backup and path required"},
                            status_code=400)
    ok, msg = await recovery.restore_backup(name, rel)
    return JSONResponse({"success": ok, "message" if ok else "error": msg},
                        status_code=200 if ok else 400)


@app.post("/recovery/clear-pending")
async def recovery_clear_pending(authorization: str = Header(default="")):
    if not upgrade.check_token(authorization):
        return _unauthorized()
    return {"success": True, "removed": recovery.clear_pending()}


@app.post("/recovery/resume")
async def recovery_resume(authorization: str = Header(default="")):
    if not upgrade.check_token(authorization):
        return _unauthorized()
    # If the app container is dead, a resume marker helps nobody — restart it.
    info = await containers.inspect_container(containers.APP_CONTAINER)
    running = bool(((info or {}).get("State") or {}).get("Running"))
    if not running:
        ok = await containers.restart_container(containers.APP_CONTAINER)
        return JSONResponse(
            {"success": ok, ("message" if ok else "error"):
             "App container was stopped — restarted it" if ok
             else "App container is stopped and restart failed"},
            status_code=200 if ok else 500)
    ok, msg = recovery.request_resume()
    return JSONResponse({"success": ok, "message" if ok else "error": msg},
                        status_code=200 if ok else 500)


@app.post("/upgrade/retention")
async def upgrade_retention(data: dict = Body(...),
                            authorization: str = Header(default="")):
    if not upgrade.check_token(authorization):
        return _unauthorized()
    try:
        count = int(data.get("retention_count"))
    except (TypeError, ValueError):
        return JSONResponse({"success": False, "error": "retention_count must be "
                            "an integer"}, status_code=400)
    ok, msg = upgrade.set_retention(count)
    return JSONResponse({"success": ok, "message": msg},
                        status_code=200 if ok else 400)


# The dashboard is a static asset shipped alongside this module (manager/
# dashboard.html) — kept out of Python so its inline JS can't be mangled by
# string escaping and can be linted/edited as real HTML.
_DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")


@app.get("/", response_class=HTMLResponse)
async def index():
    try:
        return _DASHBOARD_PATH.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("dashboard.html unreadable: %s", e)
        return HTMLResponse("<h1>ZMM Manager</h1><p>dashboard asset missing</p>",
                            status_code=500)
