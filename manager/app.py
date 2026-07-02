"""Manager sidecar FastAPI app (:8001).

CP2a: a minimal, always-on status page + JSON endpoint. It health-checks the app
over the pod-shared loopback and lists the deployment's containers via the runtime
socket. No auth yet (CP2a is for proving the sidecar works); auth + recovery
actions come in CP2b.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from manager import containers, watchdog

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger("manager.app")

# The app serves HTTPS on the pod-shared loopback. verify=False: its cert is
# self-signed and we're talking to 127.0.0.1 inside the same netns.
APP_HEALTH_URL = os.environ.get("ZMM_APP_HEALTH_URL",
                                "https://127.0.0.1:8000/api/system/health")

@asynccontextmanager
async def lifespan(app: FastAPI):
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
    return {"app": await _app_health(),
            "containers": await containers.list_containers(),
            "watchdog": watchdog.get_state()}


@app.get("/healthz")
async def healthz():
    # Liveness for the manager itself (unauthenticated, cheap).
    return JSONResponse({"manager": "ok"})


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
