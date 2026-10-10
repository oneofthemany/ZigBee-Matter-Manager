"""
HomeKit accessories over the LAN, with this hub acting as the HomeKit
controller (the role an iPhone plays) through aiohomekit — the library behind
Home Assistant's HomeKit Controller integration.

Scope is televisions (HAP category 31, e.g. Sky Glass): power, input, remote
keys, volume and mute. Everything is local HAP over IP; no Apple account.

An accessory pairs with one controller set. Pairing here needs the accessory
unpaired (`sf=1` in its _hap._tcp advert) and the 8-digit code it shows on
screen. The resulting long-term keys are credentials: they live in a 0600 file
under data/ that is gitignored, never in config.yaml.

Pairing, discovery and the API: docs/external-apis.md §HomeKit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("zbm.homekit")

PAIRINGS_FILE = os.environ.get("ZMM_HOMEKIT_PAIRINGS", "./data/homekit_pairings.json")

CATEGORY_TELEVISION = 31
# HAP is local and cheap, but a TV in standby answers slowly; reads are cached.
STATUS_MAX_AGE = 15.0
# Long enough to walk to the TV and read the code, short enough that an
# abandoned pair-setup does not hold the accessory's pairing slot.
PENDING_PAIRING_SECONDS = 300.0
DISCOVERY_SETTLE_SECONDS = 4.0
# What aiohomekit's IP and CoAP transports look for a browser of.
HAP_TYPES = ("_hap._tcp.local.", "_hap._udp.local.")


def _u(short: str) -> str:
    return f"{short.upper().zfill(8)}-0000-1000-8000-0026BB765291"


S_ACCESSORY_INFO = _u("3E")
S_TELEVISION = _u("D8")
S_INPUT_SOURCE = _u("D9")
S_SPEAKER = _u("113")

C_NAME = _u("23")
C_MANUFACTURER = _u("20")
C_MODEL = _u("21")
C_SERIAL = _u("30")
C_FIRMWARE = _u("52")
C_ACTIVE = _u("B0")
C_ACTIVE_IDENTIFIER = _u("E7")
C_CONFIGURED_NAME = _u("E3")
C_REMOTE_KEY = _u("E1")
C_IDENTIFIER = _u("E6")
C_IS_CONFIGURED = _u("D6")
C_VISIBILITY = _u("135")
C_INPUT_SOURCE_TYPE = _u("DB")
C_MUTE = _u("11A")
C_VOLUME = _u("119")
C_VOLUME_SELECTOR = _u("EA")

REMOTE_KEYS = {
    "rewind": 0, "fast_forward": 1, "next": 2, "previous": 3,
    "up": 4, "down": 5, "left": 6, "right": 7, "select": 8,
    "back": 9, "exit": 10, "play_pause": 11, "info": 15,
}
INPUT_TYPES = {0: "other", 1: "home_screen", 2: "tuner", 3: "hdmi", 4: "composite",
               5: "s_video", 6: "component", 7: "dvi", 8: "airplay", 9: "usb", 10: "application"}


class HomeKitError(Exception):
    """User-visible HomeKit failure (no library, not paired, unreachable)."""


# Accessory database → TV map

def _norm(t: str) -> str:
    t = str(t).upper()
    return _u(t) if len(t) <= 8 else t


def _chars(service: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {_norm(c["type"]): c for c in service.get("characteristics", [])}


def map_television(accessories: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Locate the TV's characteristics in a HAP /accessories database.

    Returns iids for writing and current values for reading, or None when no
    Television service exists. Input sources are linked services in the spec,
    but some accessories omit the links, so every InputSource on the
    accessory counts.
    """
    for acc in accessories:
        aid = acc["aid"]
        services = acc.get("services", [])
        tv = next((s for s in services if _norm(s["type"]) == S_TELEVISION), None)
        if tv is None:
            continue

        def iid_val(chars, ctype):
            c = chars.get(ctype)
            return (c["iid"], c.get("value")) if c else (None, None)

        info = next((_chars(s) for s in services if _norm(s["type"]) == S_ACCESSORY_INFO), {})
        tvc = _chars(tv)
        out: Dict[str, Any] = {"aid": aid, "iids": {}, "values": {}}

        def take(key, chars, ctype):
            iid, val = iid_val(chars, ctype)
            if iid is not None:
                out["iids"][key] = iid
                out["values"][key] = val

        for key, ctype in (("name", C_NAME), ("manufacturer", C_MANUFACTURER),
                           ("model", C_MODEL), ("serial", C_SERIAL), ("firmware", C_FIRMWARE)):
            take(key, info, ctype)
        for key, ctype in (("active", C_ACTIVE), ("active_identifier", C_ACTIVE_IDENTIFIER),
                           ("configured_name", C_CONFIGURED_NAME), ("remote_key", C_REMOTE_KEY)):
            take(key, tvc, ctype)

        speaker = next((s for s in services if _norm(s["type"]) == S_SPEAKER), None)
        if speaker is not None:
            spc = _chars(speaker)
            for key, ctype in (("mute", C_MUTE), ("volume", C_VOLUME),
                               ("volume_selector", C_VOLUME_SELECTOR)):
                take(key, spc, ctype)

        inputs = []
        for s in services:
            if _norm(s["type"]) != S_INPUT_SOURCE:
                continue
            c = _chars(s)
            ident = (c.get(C_IDENTIFIER) or {}).get("value")
            if ident is None:
                continue
            # HAP: IsConfigured 1 = configured, CurrentVisibilityState 0 = shown.
            if (c.get(C_IS_CONFIGURED) or {}).get("value", 1) != 1:
                continue
            if (c.get(C_VISIBILITY) or {}).get("value", 0) != 0:
                continue
            name = ((c.get(C_CONFIGURED_NAME) or {}).get("value")
                    or (c.get(C_NAME) or {}).get("value") or f"Input {ident}")
            kind = INPUT_TYPES.get((c.get(C_INPUT_SOURCE_TYPE) or {}).get("value"), "other")
            inputs.append({"id": int(ident), "name": str(name), "type": kind})
        out["inputs"] = sorted(inputs, key=lambda i: i["id"])
        return out
    return None


def normalise_status(device_id: str, tv: Dict[str, Any]) -> Dict[str, Any]:
    v, iids = tv["values"], tv["iids"]
    active_input = v.get("active_identifier")
    input_name = next((i["name"] for i in tv["inputs"] if i["id"] == active_input), None)
    return {
        "id": device_id,
        "name": v.get("configured_name") or v.get("name") or device_id,
        "manufacturer": v.get("manufacturer") or "HomeKit",
        "model": v.get("model") or "Television",
        "firmware": v.get("firmware"),
        "online": True,
        "power": None if v.get("active") is None else bool(v.get("active")),
        "input": active_input,
        "input_name": input_name,
        "inputs": tv["inputs"],
        "mute": None if v.get("mute") is None else bool(v.get("mute")),
        "volume": v.get("volume"),
        "capabilities": {
            "power": "active" in iids,
            "input": "active_identifier" in iids and bool(tv["inputs"]),
            "remote": "remote_key" in iids,
            "volume_step": "volume_selector" in iids,
            "volume_level": "volume" in iids,
            "mute": "mute" in iids,
        },
    }


def format_pin(pin: str) -> str:
    """HAP wants XXX-XX-XXX; TVs show it with spaces, dashes or neither."""
    digits = re.sub(r"\D", "", str(pin or ""))
    if len(digits) != 8:
        raise HomeKitError("The setup code is 8 digits, as shown on the TV")
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"


# Pairing store

def load_pairings(path: str = PAIRINGS_FILE) -> Dict[str, Dict[str, Any]]:
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"Could not read {path}: {e}")
        return {}


def save_pairings(pairings: Dict[str, Dict[str, Any]], path: str = PAIRINGS_FILE) -> None:
    """Atomic, and 0600 from creation: the file holds the controller's private key."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(pairings, fh, indent=2)
    os.replace(tmp, p)


# Controller

class HomeKitController:
    """aiohomekit session, pairings and a short status cache.

    aiohomekit is native asyncio; only the pairing file is written off-loop.
    One lock serialises session setup and writes so a burst of UI requests
    does not open parallel pair-verify handshakes to the same accessory.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 pairings_file: str = PAIRINGS_FILE):
        self._pairings_file = pairings_file
        self._ctl: Any = None
        self._zc: Any = None
        self._browser: Any = None
        self._started_at = 0.0
        self._pairings: Dict[str, Dict[str, Any]] = {}
        self._maps: Dict[str, Dict[str, Any]] = {}
        self._status: Dict[str, tuple[float, Dict[str, Any]]] = {}
        self._pending: Dict[str, tuple[float, Callable[[str], Awaitable[Any]]]] = {}
        self._lock = asyncio.Lock()
        self.last_error: Optional[str] = None
        self.reload(config or {})

    def reload(self, config: Dict[str, Any]) -> None:
        self.enabled = bool(config.get("enabled", False))

    # Session
    async def _ensure_started(self) -> None:
        if not self.enabled:
            raise HomeKitError("HomeKit integration is disabled")
        if self._ctl is not None:
            return
        try:
            from aiohomekit import Controller
            from zeroconf.asyncio import AsyncZeroconf
        except ImportError as e:
            raise HomeKitError("aiohomekit library not installed — "
                               "add 'aiohomekit' to requirements") from e
        self._zc = AsyncZeroconf()
        # aiohomekit doesn't browse for itself: its transports attach to a
        # browser already running on this instance, and refuse to start
        # ("no zeroconf browser for _hap._tcp.local.") without one.
        self._browser = self._make_browser(self._zc)
        ctl = Controller(async_zeroconf_instance=self._zc)
        await ctl.async_start()
        self._pairings = await asyncio.to_thread(load_pairings, self._pairings_file)
        for alias, data in self._pairings.items():
            try:
                ctl.load_pairing(alias, dict(data))
            except Exception as e:                        # noqa: BLE001
                logger.warning(f"HomeKit pairing {alias} not loaded: {e}")
        self._ctl = ctl
        self._started_at = time.monotonic()
        logger.info(f"HomeKit controller started with {len(self._pairings)} pairing(s)")

    @staticmethod
    def _make_browser(zc: Any) -> Any:
        from aiohomekit.zeroconf import ZeroconfServiceListener
        from zeroconf.asyncio import AsyncServiceBrowser
        return AsyncServiceBrowser(zc.zeroconf, list(HAP_TYPES), listener=ZeroconfServiceListener())

    async def stop(self) -> None:
        async with self._lock:
            ctl, zc = self._ctl, self._zc
            browser, self._browser = self._browser, None
            self._ctl = self._zc = None
            self._pending.clear()
            self._maps.clear()
            self._status.clear()
            if ctl is not None:
                for p in list(getattr(ctl, "aliases", {}).values()):
                    try:
                        await p.close()
                    except Exception as e:                # noqa: BLE001
                        logger.debug(f"HomeKit pairing close: {e}")
                try:
                    await ctl.async_stop()
                except Exception as e:                    # noqa: BLE001
                    logger.debug(f"HomeKit controller stop: {e}")
            if browser is not None:
                try:
                    await browser.async_cancel()
                except Exception as e:                    # noqa: BLE001
                    logger.debug(f"HomeKit browser cancel: {e}")
            if zc is not None:
                await zc.async_close()

    async def _settle(self) -> None:
        """The zeroconf browser needs a moment after start to hear adverts."""
        left = DISCOVERY_SETTLE_SECONDS - (time.monotonic() - self._started_at)
        if left > 0:
            await asyncio.sleep(left)

    def _pairing(self, device_id: str) -> Any:
        p = getattr(self._ctl, "aliases", {}).get(device_id.lower())
        if p is None:
            raise HomeKitError(f"HomeKit accessory {device_id} is not paired")
        return p

    # Discovery and pairing
    async def discover(self) -> List[Dict[str, Any]]:
        async with self._lock:
            await self._ensure_started()
        await self._settle()
        found = []
        async for d in self._ctl.async_discover():
            desc = d.description
            did = str(desc.id).lower()
            found.append({
                "id": did,
                "name": desc.name or did,
                "model": desc.model,
                "category": int(desc.category),
                "television": int(desc.category) == CATEGORY_TELEVISION,
                "address": getattr(desc, "address", None),
                "port": getattr(desc, "port", None),
                # sf bit 0: the accessory has no pairings and will accept one.
                "available": not d.paired,
                "paired_here": did in self._pairings,
            })
        return sorted(found, key=lambda x: (not x["television"], x["name"].lower()))

    async def start_pairing(self, device_id: str) -> None:
        """Begin pair-setup; the accessory now shows its code on screen."""
        device_id = device_id.lower()
        async with self._lock:
            await self._ensure_started()
            if device_id in self._pairings:
                raise HomeKitError("Already paired with this hub")
            try:
                discovery = await self._ctl.async_find(device_id, timeout=10)
            except Exception as e:                        # noqa: BLE001
                raise HomeKitError(f"Accessory {device_id} not found on the network") from e
            if discovery.paired:
                raise HomeKitError("This accessory is already paired to another home "
                                   "(e.g. Apple Home) — remove it there, or reset its "
                                   "HomeKit pairing, then try again")
            try:
                finish = await discovery.async_start_pairing(device_id)
            except Exception as e:                        # noqa: BLE001
                raise HomeKitError(f"Pairing could not start: {e}") from e
            self._pending[device_id] = (time.monotonic(), finish)

    async def finish_pairing(self, device_id: str, pin: str) -> Dict[str, Any]:
        device_id = device_id.lower()
        code = format_pin(pin)
        async with self._lock:
            entry = self._pending.pop(device_id, None)
            if entry is None or time.monotonic() - entry[0] > PENDING_PAIRING_SECONDS:
                raise HomeKitError("No pairing in progress for this accessory — start again")
            try:
                pairing = await entry[1](code)
            except Exception as e:                        # noqa: BLE001
                raise HomeKitError(f"Pairing failed: {e} — check the code on the TV "
                                   f"and start again") from e
            self._pairings[device_id] = dict(pairing.pairing_data)
            self._ctl.aliases[device_id] = pairing
            await asyncio.to_thread(save_pairings, self._pairings, self._pairings_file)
            logger.info(f"HomeKit: paired with {device_id}")
        return await self.status(device_id, max_age=0)

    async def unpair(self, device_id: str) -> bool:
        """Remove the pairing on the accessory too, so it can be paired again.
        Returns whether the accessory confirmed; the local keys go either way."""
        device_id = device_id.lower()
        confirmed = False
        async with self._lock:
            await self._ensure_started()
            data = self._pairings.get(device_id)
            if data is None:
                raise HomeKitError(f"HomeKit accessory {device_id} is not paired")
            pairing = getattr(self._ctl, "aliases", {}).get(device_id)
            if pairing is not None:
                try:
                    confirmed = bool(await pairing.remove_pairing(data.get("iOSPairingId", "")))
                except Exception as e:                    # noqa: BLE001
                    logger.warning(f"HomeKit: accessory {device_id} did not confirm unpair: {e}")
                try:
                    await pairing.close()
                except Exception:                         # noqa: BLE001
                    pass
            self._ctl.aliases.pop(device_id, None)
            self._ctl.pairings.pop(str(data.get("AccessoryPairingID", "")).lower(), None)
            self._pairings.pop(device_id, None)
            self._maps.pop(device_id, None)
            self._status.pop(device_id, None)
            await asyncio.to_thread(save_pairings, self._pairings, self._pairings_file)
        return confirmed

    # Devices
    def device_ids(self) -> List[str]:
        """Empty until the session starts; the pairing file is not read on the loop."""
        return list(self._pairings)

    def cached_status(self, device_id: str) -> Optional[tuple[float, Dict[str, Any]]]:
        entry = self._status.get(device_id.lower())
        if entry is None:
            return None
        return time.monotonic() - entry[0], entry[1]

    async def list_devices(self, max_age: float = STATUS_MAX_AGE) -> List[Dict[str, Any]]:
        async with self._lock:
            await self._ensure_started()
            ids = list(self._pairings)
        out = []
        for i in ids:
            try:
                out.append(await self.status(i, max_age=max_age))
            except HomeKitError as e:
                out.append({"id": i, "name": i, "online": False, "error": str(e)})
        return out

    async def status(self, device_id: str, max_age: float = STATUS_MAX_AGE) -> Dict[str, Any]:
        device_id = device_id.lower()
        cached = self.cached_status(device_id)
        if cached is not None and cached[0] <= max_age:
            return cached[1]
        async with self._lock:
            cached = self.cached_status(device_id)
            if cached is not None and cached[0] <= max_age:
                return cached[1]
            await self._ensure_started()
            pairing = self._pairing(device_id)
            try:
                accessories = await asyncio.wait_for(
                    pairing.list_accessories_and_characteristics(), timeout=15)
            except Exception as e:                        # noqa: BLE001
                msg = str(e) or type(e).__name__
                self.last_error = msg
                if cached is not None:
                    # A TV in deep standby drops off the network; keep the last state.
                    return {**cached[1], "online": False, "stale": True, "error": msg}
                raise HomeKitError(f"HomeKit accessory unreachable: {msg}") from e
            tv = map_television(accessories)
            if tv is None:
                raise HomeKitError("This accessory has no Television service")
            self._maps[device_id] = tv
            status = normalise_status(device_id, tv)
            self._status[device_id] = (time.monotonic(), status)
            self.last_error = None
            return status

    async def control(self, device_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        device_id = device_id.lower()
        if device_id not in self._maps:
            await self.status(device_id, max_age=0)
        async with self._lock:
            pairing = self._pairing(device_id)
            tv = self._maps[device_id]
            writes = build_writes(tv, changes)
            try:
                result = await asyncio.wait_for(pairing.put_characteristics(writes), timeout=10)
            except Exception as e:                        # noqa: BLE001
                raise HomeKitError(f"HomeKit control failed: {e}") from e
            failed = [v.get("description") for v in (result or {}).values()
                      if v.get("status", 0) != 0]
            if failed:
                raise HomeKitError(f"The TV refused the change: {', '.join(map(str, failed))}")
            self._status.pop(device_id, None)
        return await self.status(device_id, max_age=0)


def build_writes(tv: Dict[str, Any], changes: Dict[str, Any]) -> List[tuple]:
    """Translate a control request into (aid, iid, value) writes."""
    aid, iids = tv["aid"], tv["iids"]
    writes: List[tuple] = []

    def need(key: str, what: str) -> int:
        if key not in iids:
            raise HomeKitError(f"This TV does not support {what}")
        return iids[key]

    if "power" in changes:
        writes.append((aid, need("active", "power"), 1 if changes["power"] else 0))
    if "input" in changes:
        ident = int(changes["input"])
        if ident not in {i["id"] for i in tv["inputs"]}:
            raise HomeKitError(f"Unknown input {ident}")
        writes.append((aid, need("active_identifier", "input switching"), ident))
    if "key" in changes:
        key = str(changes["key"]).lower()
        if key not in REMOTE_KEYS:
            raise HomeKitError(f"Unknown remote key {key!r}")
        writes.append((aid, need("remote_key", "remote keys"), REMOTE_KEYS[key]))
    if "volume_step" in changes:
        # VolumeSelector: 0 increments, 1 decrements.
        up = str(changes["volume_step"]).lower() in ("up", "+", "1", "true")
        writes.append((aid, need("volume_selector", "volume buttons"), 0 if up else 1))
    if "volume" in changes:
        writes.append((aid, need("volume", "volume level"),
                       max(0, min(100, int(changes["volume"])))))
    if "mute" in changes:
        writes.append((aid, need("mute", "mute"), bool(changes["mute"])))
    if not writes:
        raise HomeKitError("Nothing to change")
    return writes
