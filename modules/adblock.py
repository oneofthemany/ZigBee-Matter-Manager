"""Client for the Beekeeper DNS-sinkhole sidecar's loopback control API.

The ad-block engine runs as a separate always-on process (``python -m
beekeeper``) so a restart/upgrade of this app never drops household DNS. This
module is the thin bridge the main app uses to talk to it: it reads the
control host/port from ``config.yaml`` and proxies requests, degrading
gracefully to ``{"available": False}`` when the sidecar isn't reachable (not
installed yet, disabled, or restarting) so the UI can render an "offline" state
instead of erroring.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import yaml

logger = logging.getLogger("modules.adblock")

_CONFIG_PATH = Path("./config/config.yaml")
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8053


def _control_base() -> str:
    host, port = _DEFAULT_HOST, _DEFAULT_PORT
    try:
        with open(_CONFIG_PATH) as f:
            bk = (yaml.safe_load(f) or {}).get("beekeeper") or {}
        control = bk.get("control") or {}
        host = str(control.get("host", host) or host)
        port = int(control.get("port", port))
    except (OSError, ValueError, TypeError):
        pass  # sidecar defaults are fine
    return f"http://{host}:{port}"


class AdBlockClient:
    def __init__(self, timeout: float = 5.0):
        self._timeout = timeout
        self._base = _control_base()

    async def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        url = self._base + path
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as cx:
                r = await cx.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            logger.debug("Beekeeper control unreachable at %s: %s", url, e)
            return {"available": False,
                    "error": "Beekeeper sidecar is not reachable. Is it installed "
                             "and enabled? See docs/beekeeper.md."}
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text}
        if isinstance(data, dict):
            data.setdefault("available", True)
            data.setdefault("_status", r.status_code)
            return data
        return {"available": True, "_status": r.status_code, "items": data}

    # ── status / config ──────────────────────────────────────────────────────
    async def status(self) -> Dict[str, Any]:
        return await self._request("GET", "/status")

    async def config(self) -> Dict[str, Any]:
        return await self._request("GET", "/config")

    async def healthz(self) -> Dict[str, Any]:
        return await self._request("GET", "/healthz")

    # ── service + toggles ────────────────────────────────────────────────────
    async def service(self, action: str) -> Dict[str, Any]:
        if action not in ("start", "stop"):
            return {"available": True, "ok": False, "error": "action must be start|stop"}
        return await self._request("POST", f"/service/{action}")

    async def set_enabled(self, enabled: bool) -> Dict[str, Any]:
        return await self._request("POST", "/enable", json={"enabled": enabled})

    async def pause(self, minutes: float) -> Dict[str, Any]:
        return await self._request("POST", "/pause", json={"minutes": minutes})

    async def resume(self) -> Dict[str, Any]:
        return await self._request("POST", "/resume")

    # ── blocklists ────────────────────────────────────────────────────────────
    async def refresh(self) -> Dict[str, Any]:
        return await self._request("POST", "/refresh")

    async def lists(self) -> Dict[str, Any]:
        return await self._request("GET", "/lists")

    # ── rules ─────────────────────────────────────────────────────────────────
    async def rules(self) -> Dict[str, Any]:
        return await self._request("GET", "/rules")

    async def add_rule(self, kind: str, domain: str) -> Dict[str, Any]:
        return await self._request("POST", "/rules", json={"kind": kind, "domain": domain})

    async def remove_rule(self, kind: str, domain: str) -> Dict[str, Any]:
        return await self._request("POST", "/rules/remove",
                                   json={"kind": kind, "domain": domain})

    async def check(self, domain: str) -> Dict[str, Any]:
        return await self._request("GET", "/check", params={"domain": domain})

    # ── stats ─────────────────────────────────────────────────────────────────
    async def summary(self, hours: float = 24.0) -> Dict[str, Any]:
        return await self._request("GET", "/stats/summary", params={"hours": hours})

    async def top_blocked(self, limit: int = 20, hours: float = 24.0) -> Dict[str, Any]:
        return await self._request("GET", "/stats/top-blocked",
                                   params={"limit": limit, "hours": hours})

    async def top_clients(self, limit: int = 20, hours: float = 24.0) -> Dict[str, Any]:
        return await self._request("GET", "/stats/top-clients",
                                   params={"limit": limit, "hours": hours})

    async def recent(self, limit: int = 100) -> Dict[str, Any]:
        return await self._request("GET", "/stats/recent", params={"limit": limit})

    async def series(self, hours: float = 24.0, buckets: int = 24) -> Dict[str, Any]:
        return await self._request("GET", "/stats/series",
                                   params={"hours": hours, "buckets": buckets})

    async def flush_cache(self) -> Dict[str, Any]:
        return await self._request("POST", "/cache/flush")


_client: Optional[AdBlockClient] = None


def get_client() -> AdBlockClient:
    global _client
    if _client is None:
        _client = AdBlockClient()
    return _client
