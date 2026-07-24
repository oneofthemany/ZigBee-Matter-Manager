"""Loopback control API for the Beekeeper sidecar.

A small FastAPI app bound to 127.0.0.1 only. The main ZMM app proxies to it from
``routes/adblock_routes.py`` behind its own auth middleware, so this API is
intentionally unauthenticated — it is not reachable off the host. Reads run the
SQLite queries in a thread so they never block the DNS event loop.
"""
from __future__ import annotations

import logging

from fastapi import Body, FastAPI, Query
from fastapi.responses import JSONResponse

from .server import BeekeeperServer

logger = logging.getLogger("beekeeper.control")


def build_app(server: BeekeeperServer) -> FastAPI:
    import asyncio

    app = FastAPI(title="Beekeeper Control", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.get("/healthz")
    async def healthz():
        return {"beekeeper": "ok", "running": server.running}

    @app.get("/status")
    async def status():
        return server.status()

    @app.get("/config")
    async def config():
        return server.cfg.to_public_dict()

    # ── service (bind/unbind :53) + blocking toggle ──────────────────────────
    @app.post("/service/start")
    async def service_start():
        try:
            await server.start()
        except OSError as e:
            # Almost always :53 already in use (systemd-resolved). Make it legible.
            return JSONResponse({"ok": False, "error": f"could not bind DNS port: {e}"},
                                status_code=409)
        server.set_service_enabled(True)   # persist so it re-binds after a restart
        return {"ok": True, "status": server.status()}

    @app.post("/service/stop")
    async def service_stop():
        await server.stop(keep_stats=True)
        server.set_service_enabled(False)
        return {"ok": True, "status": server.status()}

    @app.post("/enable")
    async def enable(payload: dict = Body(default={})):
        server.set_enabled(bool(payload.get("enabled", True)))
        return {"ok": True, "runtime": server.state.status()}

    @app.post("/pause")
    async def pause(payload: dict = Body(default={})):
        minutes = float(payload.get("minutes", 5))
        until = server.pause(minutes)
        return {"ok": True, "paused_until": until, "runtime": server.state.status()}

    @app.post("/resume")
    async def resume():
        server.resume()
        return {"ok": True, "runtime": server.state.status()}

    # ── blocklists ────────────────────────────────────────────────────────────
    @app.post("/refresh")
    async def refresh():
        return await server.refresh_now()

    @app.get("/lists")
    async def lists():
        from . import blocklists
        return {"lists": blocklists.read_meta(server.cfg.lists_dir),
                "sources": server.sources()}

    @app.post("/lists/add")
    async def lists_add(payload: dict = Body(...)):
        return await server.add_source(str(payload.get("name", "")),
                                       str(payload.get("url", "")))

    @app.post("/lists/remove")
    async def lists_remove(payload: dict = Body(...)):
        return await server.remove_source(str(payload.get("key", "")))

    @app.post("/lists/toggle")
    async def lists_toggle(payload: dict = Body(...)):
        return await server.set_source_enabled(str(payload.get("key", "")),
                                               bool(payload.get("enabled", True)))

    # ── allow / deny rules ────────────────────────────────────────────────────
    @app.get("/rules")
    async def get_rules():
        return {"allow": server.list_rules("allow"),
                "deny": server.list_rules("deny")}

    @app.post("/rules")
    async def add_rule(payload: dict = Body(...)):
        kind = payload.get("kind")
        if kind not in ("allow", "deny"):
            return JSONResponse({"ok": False, "error": "kind must be allow|deny"},
                                status_code=400)
        ok = await server.add_rule(kind, str(payload.get("domain", "")))
        return {"ok": ok, "rules": {"allow": server.list_rules("allow"),
                                    "deny": server.list_rules("deny")}}

    @app.post("/rules/remove")
    async def remove_rule(payload: dict = Body(...)):
        kind = payload.get("kind")
        if kind not in ("allow", "deny"):
            return JSONResponse({"ok": False, "error": "kind must be allow|deny"},
                                status_code=400)
        ok = await server.remove_rule(kind, str(payload.get("domain", "")))
        return {"ok": ok, "rules": {"allow": server.list_rules("allow"),
                                    "deny": server.list_rules("deny")}}

    @app.get("/check")
    async def check(domain: str = Query(...)):
        return server.check_domain(domain)

    @app.get("/dig")
    async def dig(domain: str = Query(...), type: int = 1):
        return await server.dig(domain, type)

    # ── stats ─────────────────────────────────────────────────────────────────
    @app.get("/stats/summary")
    async def stats_summary(hours: float = 24.0):
        return await asyncio.to_thread(server.stats.summary, hours)

    @app.get("/stats/top-blocked")
    async def stats_top_blocked(limit: int = 20, hours: float = 24.0):
        return {"items": await asyncio.to_thread(server.stats.top_blocked, limit, hours)}

    @app.get("/stats/top-clients")
    async def stats_top_clients(limit: int = 20, hours: float = 24.0):
        return {"items": await asyncio.to_thread(server.stats.top_clients, limit, hours)}

    @app.get("/stats/recent")
    async def stats_recent(limit: int = 100):
        return {"items": await asyncio.to_thread(server.stats.recent, limit)}

    @app.get("/stats/series")
    async def stats_series(hours: float = 24.0, buckets: int = 24):
        return {"series": await asyncio.to_thread(server.stats.series, hours, buckets)}

    @app.post("/cache/flush")
    async def cache_flush():
        return {"ok": True, "cleared": server.resolver.clear_cache()}

    return app
