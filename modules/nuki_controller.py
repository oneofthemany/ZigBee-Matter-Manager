"""
Async client for the Nuki Bridge HTTP API (v1.13) — the "with bridge" half of
the integration; bridge-less locks go through Matter instead.

Auth defaults to the hashed token triple, since the plain form leaks the token
to anything watching LAN traffic. Config: security.nuki.bridge in config.yaml.
See docs/security.md.
"""

from __future__ import annotations

import asyncio
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
        # TimeoutError is NOT a ClientError — catch both, plus OS-level
        # socket errors, so callers only ever see NukiError.
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as e:
            reason = str(e) or type(e).__name__
            raise NukiError(f"Bridge unreachable at {self.host}:{self.port} ({reason})")

    # API surface

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


class _LockCapabilities:
    """Duck-typed capabilities object (has_capability) the automation
    engine's actuator filter expects on non-Matter devices."""

    def has_capability(self, cap: str) -> bool:
        return cap in ("lock", "door_lock")

    def get_capabilities(self) -> List[str]:
        return ["lock"]


_LOCK_CAPS = _LockCapabilities()


class NukiLockDevice:
    """
    Bridge-paired lock exposed to the device list and the automation engine.
    Mirrors the surface MatterDevice presents (.state, .friendly_name,
    async send_command, get_control_commands) so both treat it like any
    other device. Pseudo-ieee: nuki_<nukiId>.
    """

    def __init__(self, lock: Dict[str, Any], action_sender):
        # action_sender: async (nuki_id, action, device_type) -> result dict
        self._send_action = action_sender
        self.nuki_id = lock.get("nuki_id")
        self.ieee = f"nuki_{self.nuki_id}"
        self.manufacturer = "Nuki"
        self.state: Dict[str, Any] = {}
        self.last_seen = time.time()
        self._available = False
        self._lock: Dict[str, Any] = {}
        self.update_from_lock(lock)

    @property
    def friendly_name(self) -> str:
        return self._lock.get("name") or self.ieee

    @property
    def model(self) -> str:
        return self._lock.get("device_type_name") or "Smart Lock"

    @property
    def capabilities(self) -> _LockCapabilities:
        return _LOCK_CAPS

    def update_from_lock(self, lock: Dict[str, Any]) -> Dict[str, Any]:
        """Refresh from a normalized /list entry. Returns just the state keys
        that changed — the poller feeds those to automation.evaluate()."""
        self._lock = lock
        self._available = bool(lock.get("available"))
        if self._available:
            self.last_seen = time.time()
        raw_state = lock.get("state")
        new_state = {k: v for k, v in {
            "locked": (raw_state == 1) if raw_state is not None else None,
            "lock_state": lock.get("state_name"),
            "battery_critical": lock.get("battery_critical"),
            "door_state": lock.get("door_state"),
        }.items() if v is not None}
        changed = {k: v for k, v in new_state.items()
                   if self.state.get(k) != v}
        self.state = new_state
        return changed

    def is_available(self) -> bool:
        return self._available

    def get_role(self) -> str:
        return "Nuki"

    def get_type(self) -> str:
        return "Lock"

    def get_control_commands(self) -> List[Dict[str, Any]]:
        return [
            {"command": "lock", "label": "Lock", "endpoint_id": None},
            {"command": "unlock", "label": "Unlock", "endpoint_id": None},
            {"command": "unlatch", "label": "Unlatch", "endpoint_id": None},
            {"command": "lock_n_go", "label": "Lock 'n' Go", "endpoint_id": None},
        ]

    async def send_command(self, command, value=None, endpoint_id=None, **_):
        if command not in LOCK_ACTIONS:
            return {"success": False,
                    "error": f"Unknown lock command '{command}'"}
        res = await self._send_action(self.nuki_id, command,
                                      self._lock.get("device_type", 0))
        if res.get("success"):
            locked = command in ("lock", "lock_n_go")
            self.state["locked"] = locked
            self.state["lock_state"] = ("locked" if locked else
                                        "unlatched" if "unlatch" in command
                                        else "unlocked")
        return res

    def to_device_list_entry(self) -> dict:
        """Dict matching ZigbeeService.get_device_list() format — merged
        into /api/devices so locks appear alongside zigbee/matter/AC."""
        return {
            "ieee": self.ieee,
            "nuki_lock_id": self._lock.get("id"),
            "friendly_name": self.friendly_name,
            "type": "Lock",
            "protocol": "wifi",
            "manufacturer": self.manufacturer,
            "model": self.model,
            "available": self._available,
            "ip_addresses": [],
            "last_seen_ts": int(self.last_seen * 1000) if self._available else None,
            "state": dict(self.state),
            "capabilities": ["lock"],
        }


async def discover_bridges(timeout: float = 10.0) -> List[Dict[str, Any]]:
    """Ask the Nuki cloud which bridges have phoned home from this network."""
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(DISCOVER_URL) as resp:
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as e:
        raise NukiError(f"Bridge discovery failed ({str(e) or type(e).__name__})")
    if data.get("errorCode") not in (0, None):
        raise NukiError(f"Bridge discovery error code {data.get('errorCode')}")
    return data.get("bridges") or []
