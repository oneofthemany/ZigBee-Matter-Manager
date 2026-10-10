"""
Shared plumbing for Wi-Fi devices on a local API (Shelly, ESPHome): the device
object the engine and device list see, the registry, credentials, publishing
state changes, and mDNS discovery. Each kind supplies a Session that keeps one
device's state current. See docs/wifi-devices.md.

State uses the keys Zigbee and Matter devices use (state, brightness, power,
temperature, occupancy, contact, ...), with a numeric suffix per channel on
multi-channel devices (state_1, state_2) — the swarm reads that suffix as an
endpoint. So rules, notification rules, alarm zones and the swarm treat these
devices like any other.
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

logger = logging.getLogger("lan_devices")

SECRETS_FILE = os.environ.get("ZMM_SECRETS_FILE", "./config/secrets.yaml")
MAX_DEVICES = 200
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:64] or "device"


def check_host(host: str) -> str:
    host = (host or "").strip()
    if not _HOST_RE.match(host) or host.startswith("-"):
        raise ValueError("Host must be an IP address or hostname")
    return host


# Credentials (config/secrets.yaml, one section per kind)

def _read_secrets() -> Dict[str, Any]:
    try:
        import yaml
        with open(SECRETS_FILE, "r") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"Could not read {SECRETS_FILE}: {e}")
        return {}


def _write_section(name: str, value: Dict[str, Any]) -> None:
    import yaml
    path = Path(SECRETS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_secrets()
    existing[name] = value
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        yaml.dump(existing, fh, default_flow_style=False, sort_keys=False)
    os.chmod(path, 0o600)


# Devices

class _Caps:
    def __init__(self, caps: List[str]):
        self._caps = set(caps)

    def has_capability(self, cap: str) -> bool:
        return cap in self._caps

    def get_capabilities(self):
        return set(self._caps)


class LanDevice:
    """One device as the engine, the device list and the modal see it."""

    def __init__(self, hub: "LanDeviceHub", cfg: Dict[str, Any]):
        self.hub = hub
        self.cfg = cfg
        self.ieee = f"{hub.kind}::{cfg['id']}"
        self.friendly_name = cfg.get("name") or cfg["id"]
        self.manufacturer = hub.manufacturer
        self.model = cfg.get("model") or hub.manufacturer
        self.state: Dict[str, Any] = {}
        self.controls: List[Dict[str, Any]] = []
        self.caps: List[str] = []
        self.online: Optional[bool] = None
        self.error: Optional[str] = None
        self.last_seen = 0.0
        self.session: Any = None
        self.task: Optional[asyncio.Task] = None

    @property
    def capabilities(self) -> _Caps:
        return _Caps(self.caps)

    def is_available(self) -> bool:
        return self.online is not False

    def get_control_commands(self) -> List[Dict[str, Any]]:
        return list(self.controls)

    def value_options(self, attribute: str) -> Optional[List[str]]:
        if attribute == "state" or re.match(r"^state_\d+$", attribute):
            return ["ON", "OFF"]
        return None

    async def send_command(self, command: str, value: Any = None, endpoint_id: Optional[int] = None):
        return await self.hub.command(self, command, value, endpoint_id)

    def to_device_list_entry(self) -> Dict[str, Any]:
        return {
            "ieee": self.ieee, "lan_kind": self.hub.kind, "lan_id": self.cfg["id"],
            "friendly_name": self.friendly_name, "manufacturer": self.manufacturer,
            "model": self.model, "type": self.hub.device_type(self), "protocol": "wifi",
            "available": self.online, "state": dict(self.state),
            "capabilities": list(self.caps), "controls": list(self.controls),
            "ip_addresses": [self.cfg["host"]],
            "last_seen_ts": int(self.last_seen * 1000) if self.last_seen else None,
        }


class LanDeviceHub:
    """Registry + lifecycle for one kind. Subclasses set kind, manufacturer,
    and implement probe() and make_session()."""

    kind = "lan"
    manufacturer = "Wi-Fi"
    default_port = 80
    secret_fields: tuple = ("password",)
    mdns_types: tuple = ()

    def __init__(self, path: Optional[Path] = None,
                 broadcast: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
                 evaluate: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None):
        self.path = path or Path(f"./data/{self.kind}_devices.json")
        self._broadcast = broadcast
        self._evaluate = evaluate
        self.devices: Dict[str, LanDevice] = {}
        self._started = False
        self.load()

    # Subclass hooks
    async def probe(self, host: str, port: int, creds: Dict[str, str]) -> Dict[str, Any]:
        """Reach the device; return {id, name, model, ...}. Raise ValueError to refuse."""
        raise NotImplementedError

    def make_session(self, dev: LanDevice):
        raise NotImplementedError

    def device_type(self, dev: LanDevice) -> str:
        caps = set(dev.caps)
        for cap, t in (("light", "Light"), ("cover", "Cover"), ("switch", "Switch"),
                       ("motion_sensor", "Sensor"), ("contact_sensor", "Sensor"),
                       ("temperature_sensor", "Sensor")):
            if cap in caps:
                return t
        # Never "Router": the UI offers Routers as Zigbee pairing parents.
        return "WiFi"

    # Storage
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.error("[%s] unreadable %s: %s", self.kind, self.path, e)
            return
        for cfg in raw.get("devices") or []:
            if isinstance(cfg, dict) and _ID_RE.match(str(cfg.get("id") or "")):
                self.devices[cfg["id"]] = LanDevice(self, cfg)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"devices": [d.cfg for d in self.devices.values()]}, indent=1))
        os.replace(tmp, self.path)

    def creds(self, dev_id: str) -> Dict[str, str]:
        return dict((_read_secrets().get(self.kind) or {}).get(dev_id) or {})

    def _set_creds(self, dev_id: str, data: Dict[str, Any], keep: bool = True) -> None:
        all_creds = dict(_read_secrets().get(self.kind) or {})
        cur = dict(all_creds.get(dev_id) or {})
        for f in self.secret_fields + ("username",):
            if f in data:
                v = str(data.get(f) or "")
                if "\r" in v or "\n" in v:
                    raise ValueError("Credentials must be a single line")
                if v or not keep:
                    cur[f] = v
        if data.get("clear_credentials"):
            cur = {}
        cur = {k: v for k, v in cur.items() if v}
        if cur:
            all_creds[dev_id] = cur
        else:
            all_creds.pop(dev_id, None)
        _write_section(self.kind, all_creds)

    def public(self, dev: LanDevice) -> Dict[str, Any]:
        c = self.creds(dev.cfg["id"])
        return {**dev.cfg, "ieee": dev.ieee, "online": dev.online, "error": dev.error,
                "has_credentials": any(c.get(f) for f in self.secret_fields),
                "username": c.get("username", ""), "state": dict(dev.state)}

    def list(self) -> List[Dict[str, Any]]:
        return [self.public(d) for d in sorted(self.devices.values(), key=lambda d: d.friendly_name.lower())]

    # Editing (admin)
    async def add(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if len(self.devices) >= MAX_DEVICES:
            raise ValueError(f"At most {MAX_DEVICES} devices")
        host = check_host(str(data.get("host") or ""))
        port = int(data.get("port") or self.default_port)
        if not 1 <= port <= 65535:
            raise ValueError("Port must be 1-65535")
        creds = {f: str(data.get(f) or "") for f in self.secret_fields + ("username",) if data.get(f)}
        info = await self.probe(host, port, creds)            # proves reachability and credentials
        dev_id = slug(info.get("id") or host)
        if dev_id in self.devices:
            raise ValueError(f"That device is already added as '{self.devices[dev_id].friendly_name}'")
        name = str(data.get("name") or info.get("name") or dev_id).strip()[:60]
        cfg = {"id": dev_id, "name": name, "host": host, "port": port,
               "model": str(info.get("model") or "")[:60],
               **{k: v for k, v in info.items() if k in ("gen", "mac")}}
        self._set_creds(dev_id, data)
        dev = LanDevice(self, cfg)
        self.devices[dev_id] = dev
        self._save()
        if self._started:
            self._start(dev)
        return self.public(dev)

    async def update(self, dev_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        dev = self.devices.get(dev_id)
        if dev is None:
            raise KeyError(dev_id)
        cfg = dict(dev.cfg)
        if "name" in data:
            name = str(data["name"] or "").strip()
            if not name or len(name) > 60:
                raise ValueError("A device needs a name (up to 60 characters)")
            cfg["name"] = name
        if "host" in data:
            cfg["host"] = check_host(str(data["host"]))
        if "port" in data:
            cfg["port"] = int(data["port"] or self.default_port)
        self._set_creds(dev_id, data)
        dev.cfg, dev.friendly_name = cfg, cfg["name"]
        self._save()
        if self._started and any(k in data for k in ("host", "port", *self.secret_fields, "clear_credentials")):
            await self._stop(dev)
            self._start(dev)
        return self.public(dev)

    async def delete(self, dev_id: str) -> bool:
        dev = self.devices.pop(dev_id, None)
        if dev is None:
            return False
        await self._stop(dev)
        self._set_creds(dev_id, {"clear_credentials": True})
        self._save()
        return True

    # Engine, device list, commands
    def automation_devices(self) -> Dict[str, LanDevice]:
        return {d.ieee: d for d in self.devices.values()}

    def device_entries(self) -> List[Dict[str, Any]]:
        return [d.to_device_list_entry() for d in self.devices.values()]

    def by_ieee(self, ieee: str) -> Optional[LanDevice]:
        prefix = f"{self.kind}::"
        return self.devices.get(ieee[len(prefix):]) if ieee.startswith(prefix) else None

    async def command(self, dev: LanDevice, command: str, value: Any, endpoint: Optional[int]) -> Dict[str, Any]:
        if dev.session is None:
            return {"success": False, "error": f"{dev.friendly_name} isn't connected"}
        try:
            await dev.session.command(command, value, endpoint)
            return {"success": True}
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:                            # noqa: BLE001
            logger.warning("[%s] %s %s failed: %s", self.kind, dev.cfg["id"], command, e)
            return {"success": False, "error": f"{dev.friendly_name}: {type(e).__name__}: {e}"}

    async def publish(self, dev: LanDevice, state: Dict[str, Any], controls=None, caps=None,
                      online: bool = True) -> None:
        """Merge a state report; tell the browser and the engine what changed."""
        if controls is not None:
            dev.controls = controls
        if caps is not None:
            dev.caps = caps
        was_online = dev.online
        dev.online, dev.error = online, None if online else dev.error
        changed = {k: v for k, v in state.items() if dev.state.get(k) != v
                   or k in ("action",)}           # a button press repeats; each is an event
        if online:
            dev.last_seen = time.time()
        dev.state.update(state)
        dev.state["available"] = online
        if was_online != online:
            changed["available"] = online
        if not changed:
            return
        if self._broadcast:
            try:
                await self._broadcast("device_updated", {"ieee": dev.ieee, "data": dict(dev.state)})
            except Exception as e:                        # noqa: BLE001
                logger.debug("[%s] broadcast failed: %s", self.kind, e)
        if self._evaluate:
            try:
                await self._evaluate(dev.ieee, changed)
            except Exception as e:                        # noqa: BLE001
                logger.warning("[%s] evaluating %s failed: %s", self.kind, dev.ieee, e)
        if "action" in state:
            dev.state.pop("action", None)               # an event, not a standing state

    async def offline(self, dev: LanDevice, error: str) -> None:
        dev.error = error
        if dev.online is not False:
            await self.publish(dev, {}, online=False)

    # Lifecycle
    async def start(self) -> None:
        self._started = True
        for dev in self.devices.values():
            self._start(dev)

    async def stop(self) -> None:
        self._started = False
        for dev in list(self.devices.values()):
            await self._stop(dev)

    def _start(self, dev: LanDevice) -> None:
        dev.session = self.make_session(dev)
        dev.task = asyncio.get_running_loop().create_task(self._run(dev))

    async def _run(self, dev: LanDevice) -> None:
        try:
            await dev.session.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:                            # noqa: BLE001
            logger.error("[%s] session for %s ended: %s", self.kind, dev.cfg["id"], e)

    async def _stop(self, dev: LanDevice) -> None:
        t, dev.task = dev.task, None
        if dev.session is not None:
            try:
                await dev.session.close()
            except Exception:                             # noqa: BLE001
                pass
        dev.session = None
        if t and not t.done():
            t.cancel()

    # Discovery
    async def discover(self, seconds: float = 3.0) -> List[Dict[str, Any]]:
        """Devices of this kind announcing themselves over mDNS on the hub's LAN."""
        try:
            from zeroconf import ServiceStateChange
            from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
        except ImportError:
            return []
        found: Dict[str, Dict[str, Any]] = {}
        names: List[tuple] = []

        def on_change(zeroconf, service_type, name, state_change):
            if state_change is ServiceStateChange.Added:
                names.append((service_type, name))
        azc = AsyncZeroconf()
        try:
            browser = AsyncServiceBrowser(azc.zeroconf, list(self.mdns_types), handlers=[on_change])
            await asyncio.sleep(seconds)
            await browser.async_cancel()
            for stype, name in names:
                info = AsyncServiceInfo(stype, name)
                if not await info.async_request(azc.zeroconf, 1500):
                    continue
                addrs = info.parsed_addresses()
                if not addrs:
                    continue
                entry = self.mdns_entry(name[: -len(stype) - 1], addrs[0], info.port or self.default_port,
                                        {k.decode(errors="replace"): (v or b"").decode(errors="replace")
                                         for k, v in (info.properties or {}).items()})
                if entry:
                    found[entry["host"]] = entry
        finally:
            await azc.async_close()
        known = {d.cfg["host"] for d in self.devices.values()}
        return sorted(({**e, "added": e["host"] in known} for e in found.values()), key=lambda e: e["name"])

    def mdns_entry(self, name: str, host: str, port: int, props: Dict[str, str]) -> Optional[Dict[str, Any]]:
        return {"name": name, "host": host, "port": port}
