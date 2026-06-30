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


@app.get("/", response_class=HTMLResponse)
async def index():
    return _DASHBOARD_HTML


_DASHBOARD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZMM Manager</title><style>
body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:1rem}
h1{font-size:1.1rem}.card{background:#1e293b;border-radius:10px;padding:1rem;margin:.6rem 0}
.badge{display:inline-block;padding:.15rem .5rem;border-radius:6px;font-size:.8rem;font-weight:600}
.b-ok{background:#16a34a}.b-bad{background:#dc2626}.b-mut{background:#475569}
.muted{color:#94a3b8;font-size:.85rem}code{background:#020617;padding:.05rem .35rem;border-radius:4px}
</style></head><body>
<h1>&#128296; ZMM Manager <span id="conn" class="badge b-mut">&hellip;</span></h1>
<div class="card"><div class="muted">App (:8000)</div><div id="app">checking&hellip;</div></div>
<div class="card"><div class="muted">Watchdog</div><div id="wd">&hellip;</div></div>
<div class="card"><div class="muted">Containers</div><div id="cont">&hellip;</div></div>
<script>
const $=id=>document.getElementById(id);
function badge(s){s=(s||'').toLowerCase();
 if(s==='running'||s===true||s==='ok')return'b-ok';
 if(s==='exited'||s==='dead'||s===false)return'b-bad';return'b-mut';}
function wbadge(s){s=(s||'').toLowerCase();
 if(s==='ok')return'b-ok';
 if(s==='unhealthy'||s==='restarted'||s==='exhausted')return'b-bad';return'b-mut';}
async function refresh(){
 try{const r=await fetch('/status');if(!r.ok)throw 0;const d=await r.json();
  $('conn').textContent='live';$('conn').className='badge b-ok';
  const a=d.app||{};
  const av=(a.body&&a.body.version)?(' v'+a.body.version):'';
  $('app').innerHTML='<span class="badge '+badge(a.ok)+'">'+(a.ok?'healthy':'unhealthy')+'</span>'+av+
   (a.error?(' <span class="muted">'+a.error+'</span>'):'');
  const w=d.watchdog||{};
  $('wd').innerHTML='<span class="badge '+wbadge(w.status)+'">'+(w.status||'?')+'</span>'+
   (w.restarts?(' <span class="muted">'+w.restarts+' restart(s)</span>'):'')+
   (w.last_action?(' <span class="muted">'+w.last_action+'</span>'):'');
  const c=d.containers||{};
  if(!c.available){$('cont').innerHTML='<span class="muted">'+(c.error||'unavailable')+'</span>';}
  else{$('cont').innerHTML=(c.containers||[]).map(x=>
   '<div><code>'+x.name+'</code> <span class="badge '+badge(x.state)+'">'+(x.state||'?')+'</span> '+
   '<span class="muted">'+(x.status||'')+'</span></div>').join('')||'<span class="muted">none</span>';}
 }catch(e){$('conn').textContent='offline';$('conn').className='badge b-bad';}
}
refresh();setInterval(refresh,2000);
</script></body></html>"""
