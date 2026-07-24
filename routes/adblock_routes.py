"""API routes for Beekeeper — the DNS-sinkhole ad/tracker blocker.

Thin proxy in front of the Beekeeper sidecar's loopback control API (see
modules/adblock.py). All paths sit under ``/api/adblock`` and are therefore
gated by the app's auth middleware like every other ``/api/*`` route. When the
sidecar is unreachable each endpoint returns ``{"available": false, ...}`` so
the UI can show an offline state rather than a 500.
"""
import logging

from fastapi import Body, FastAPI, Query

from modules.adblock import get_client

logger = logging.getLogger("routes.adblock")


def register_adblock_routes(app: FastAPI):
    client = get_client()

    # ── status / config ──────────────────────────────────────────────────────
    @app.get("/api/adblock/status")
    async def adblock_status():
        return await client.status()

    @app.get("/api/adblock/config")
    async def adblock_config():
        return await client.config()

    # ── master switch (bind/unbind :53) + blocking toggles ───────────────────
    @app.post("/api/adblock/service")
    async def adblock_service(payload: dict = Body(default={})):
        return await client.service(str(payload.get("action", "")))

    @app.post("/api/adblock/enable")
    async def adblock_enable(payload: dict = Body(default={})):
        return await client.set_enabled(bool(payload.get("enabled", True)))

    @app.post("/api/adblock/pause")
    async def adblock_pause(payload: dict = Body(default={})):
        return await client.pause(float(payload.get("minutes", 5)))

    @app.post("/api/adblock/resume")
    async def adblock_resume():
        return await client.resume()

    # ── blocklists ────────────────────────────────────────────────────────────
    @app.post("/api/adblock/refresh")
    async def adblock_refresh():
        return await client.refresh()

    @app.get("/api/adblock/lists")
    async def adblock_lists():
        return await client.lists()

    @app.post("/api/adblock/lists/add")
    async def adblock_lists_add(payload: dict = Body(...)):
        return await client.add_list(str(payload.get("name", "")),
                                     str(payload.get("url", "")))

    @app.post("/api/adblock/lists/remove")
    async def adblock_lists_remove(payload: dict = Body(...)):
        return await client.remove_list(str(payload.get("key", "")))

    @app.post("/api/adblock/lists/toggle")
    async def adblock_lists_toggle(payload: dict = Body(...)):
        return await client.toggle_list(str(payload.get("key", "")),
                                        bool(payload.get("enabled", True)))

    # ── allow / deny rules ────────────────────────────────────────────────────
    @app.get("/api/adblock/rules")
    async def adblock_rules():
        return await client.rules()

    @app.post("/api/adblock/rules")
    async def adblock_add_rule(payload: dict = Body(...)):
        return await client.add_rule(str(payload.get("kind", "")),
                                     str(payload.get("domain", "")))

    @app.post("/api/adblock/rules/remove")
    async def adblock_remove_rule(payload: dict = Body(...)):
        return await client.remove_rule(str(payload.get("kind", "")),
                                        str(payload.get("domain", "")))

    @app.get("/api/adblock/check")
    async def adblock_check(domain: str = Query(...)):
        return await client.check(domain)

    @app.get("/api/adblock/dig")
    async def adblock_dig(domain: str = Query(...), type: int = 1):
        return await client.dig(domain, type)

    # ── stats ─────────────────────────────────────────────────────────────────
    @app.get("/api/adblock/stats/summary")
    async def adblock_summary(hours: float = 24.0):
        return await client.summary(hours)

    @app.get("/api/adblock/stats/top-blocked")
    async def adblock_top_blocked(limit: int = 20, hours: float = 24.0):
        return await client.top_blocked(limit, hours)

    @app.get("/api/adblock/stats/top-clients")
    async def adblock_top_clients(limit: int = 20, hours: float = 24.0):
        return await client.top_clients(limit, hours)

    @app.get("/api/adblock/stats/recent")
    async def adblock_recent(limit: int = 100):
        return await client.recent(limit)

    @app.get("/api/adblock/stats/series")
    async def adblock_series(hours: float = 24.0, buckets: int = 24):
        return await client.series(hours, buckets)

    @app.post("/api/adblock/cache/flush")
    async def adblock_flush_cache():
        return await client.flush_cache()
