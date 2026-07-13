"""
nuki_controller.py
==================
Async client for the Nuki Bridge HTTP API (v1.13).

Covers the "with bridge" half of the Nuki integration; bridge-less locks
(Smart Lock 3.0 Pro / 4th gen) are commissioned through the app's existing
Matter server instead and handled in routes/security_routes.py.

Bridge API notes
----------------
* All endpoints are plain HTTP GET on the bridge (default port 8080).
* Auth is a token, sent either as `token=` (plain) or as the hashed triple
  `ts`/`rnr`/`hash` where hash = sha256("<ts>,<rnr>,<token>"). Hashed is the
  default here — the plain form leaks the token to anything that can see
  LAN traffic. The bridge must have "hashed token only" left on (factory
  default) for hashed to work; plain is kept as an opt-out for old firmware.
* /auth returns a fresh token but only while the bridge's button has been
  pressed within the last 30 s, and only if auth-enable is on.
* Bridge discovery is a Nuki cloud call (api.nuki.io) — the bridge phones
  home its LAN ip/port; no credentials needed.

Config lives in config.yaml under security.nuki.bridge:
    {enabled, host, port, token, hashed_token}
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("zbm.nuki")

DISCOVER_URL = "https://api.nuki.io/discover/bridges"

# Smart Lock lock actions (deviceType 0/3/4). Openers use a different table —
# out of scope until someone owns one.
LOCK_ACTIONS = {
    "unlock": 1,
    "lock": 2,
    "unlatch": 3,
    "lock_n_go": 4,
    "lock_n_go_unlatch": 5,
}

LOCK_STATES = {
    0: "uncalibrated",
    1: "locked",
    2: "unlocking",
    3: "unlocked",
    4: "locking",
    5: "unlatched",
    6: "unlocked (lock 'n' go)",
    7: "unlatching",
    253: "boot run",
    254: "motor blocked",
    255: "undefined",
}

DEVICE_TYPES = {
    0: "Smart Lock 1/2",
    2: "Opener",
    3: "Smart Door",
    4: "Smart Lock 3/4",
}


class NukiError(Exception):
    pass


class NukiBridgeClient:
    """One bridge, as configured in security.nuki.bridge."""

    def __init__(self, cfg: Dict[str, Any]):
        self.host = str(cfg.get("host") or "").strip()
        self.port = int(cfg.get("port") or 8080)
        self.token = str(cfg.get("token") or "").strip()
        self.hashed = bool(cfg.get("hashed_token", True))

    @property
    def configured(self) -> bool:
        return bool(self.host and self.token)

    def _auth_params(self) -> Dict[str, str]:
        if not self.hashed:
            return {"token": self.token}
        # ISO 8601 UTC with trailing Z, seconds precision — the bridge
        # rejects timestamps drifted by more than a few minutes.
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rnr = random.randint(0, 65535)
        digest = hashlib.sha256(f"{ts},{rnr},{self.token}".encode()).hexdigest()
        return {"ts": ts, "rnr": str(rnr), "hash": digest}

    async def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None,
                   auth: bool = True, timeout: float = 15.0) -> Any:
        if auth and not self.configured:
            raise NukiError("Bridge host/token not configured")
        url = f"http://{self.host}:{self.port}/{endpoint.lstrip('/')}"
        q: Dict[str, Any] = dict(params or {})
        if auth:
            q.update(self._auth_params())
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                async with session.get(url, params=q) as resp:
                    if resp.status == 401:
                        raise NukiError("Bridge rejected the token (401)")
                    if resp.status == 403:
                        raise NukiError(
                            "Bridge auth disabled (403) — enable the HTTP API "
                            "in the Nuki app or via /auth")
                    if resp.status >= 400:
                        raise NukiError(f"Bridge HTTP {resp.status}")
                    return await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise NukiError(f"Bridge unreachable at {self.host}:{self.port} ({e})")

    # ── API surface ─────────────────────────────────────────────────────

    async def info(self) -> Dict[str, Any]:
        return await self._get("info")

    async def list_devices(self) -> List[Dict[str, Any]]:
        data = await self._get("list")
        return data if isinstance(data, list) else []

    async def lock_state(self, nuki_id: int, device_type: int = 0) -> Dict[str, Any]:
        return await self._get("lockState", {
            "nukiId": nuki_id, "deviceType": device_type,
        }, timeout=25.0)

    async def lock_action(self, nuki_id: int, action: str,
                          device_type: int = 0) -> Dict[str, Any]:
        code = LOCK_ACTIONS.get(action)
        if code is None:
            raise NukiError(f"Unknown lock action '{action}' "
                            f"(expected one of {', '.join(LOCK_ACTIONS)})")
        # nowait=0: block until the lock reports the result, up to ~20 s for
        # a full lock'n'go — hence the generous timeout.
        return await self._get("lockAction", {
            "nukiId": nuki_id, "deviceType": device_type,
            "action": code, "nowait": 0,
        }, timeout=45.0)

    async def fetch_auth_token(self) -> Dict[str, Any]:
        """Grab a token from /auth — the bridge button must have been
        pressed within the last 30 s. Needs host only, not a token."""
        if not self.host:
            raise NukiError("Bridge host not configured")
        return await self._get("auth", auth=False, timeout=35.0)

    def normalize_device(self, dev: Dict[str, Any]) -> Dict[str, Any]:
        """Bridge /list entry → the unified lock shape the frontend renders."""
        st = dev.get("lastKnownState") or {}
        state_code = st.get("state")
        return {
            "id": f"bridge:{dev.get('nukiId')}",
            "nuki_id": dev.get("nukiId"),
            "name": dev.get("name") or f"Nuki {dev.get('nukiId')}",
            "via": "bridge",
            "device_type": dev.get("deviceType", 0),
            "device_type_name": DEVICE_TYPES.get(dev.get("deviceType", 0), "unknown"),
            "state": state_code,
            "state_name": st.get("stateName")
                or LOCK_STATES.get(state_code, "unknown"),
            "battery_critical": st.get("batteryCritical"),
            "battery_charge": st.get("batteryChargeState"),
            "door_state": st.get("doorsensorStateName"),
            "firmware": dev.get("firmwareVersion"),
            "last_updated": st.get("timestamp"),
            "available": state_code is not None,
        }


async def discover_bridges(timeout: float = 10.0) -> List[Dict[str, Any]]:
    """Ask the Nuki cloud which bridges have phoned home from this network."""
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(DISCOVER_URL) as resp:
                data = await resp.json(content_type=None)
    except aiohttp.ClientError as e:
        raise NukiError(f"Bridge discovery failed ({e})")
    if data.get("errorCode") not in (0, None):
        raise NukiError(f"Bridge discovery error code {data.get('errorCode')}")
    return data.get("bridges") or []
