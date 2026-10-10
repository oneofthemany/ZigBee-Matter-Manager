"""
ESPHome devices over the native API (aioesphomeapi), the same protocol Home
Assistant uses: encrypted with the device's API key, pushed, no polling. See
docs/wifi-devices.md §ESPHome.

Entities map onto the keys every other device uses: switches and lights become
channels (state, state_1…), sensors and binary sensors map by device_class
(temperature, occupancy, contact…), and every entity is also kept under its own
object_id so nothing a device reports is lost.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from modules.lan_devices import LanDevice, LanDeviceHub

logger = logging.getLogger("esphome")

DEFAULT_PORT = 6053

# device_class -> the key Zigbee/Matter devices report it under.
SENSOR_KEYS = {"temperature": "temperature", "humidity": "humidity", "power": "power",
               "energy": "energy", "voltage": "voltage", "current": "current",
               "illuminance": "illuminance", "battery": "battery", "carbon_dioxide": "co2",
               "pm25": "pm25", "pressure": "pressure"}
# binary_sensor device_class -> (key, invert). Openings invert: ZCL contact is True when closed.
BINARY_KEYS = {"motion": ("occupancy", False), "occupancy": ("occupancy", False),
               "presence": ("occupancy", False), "door": ("contact", True),
               "window": ("contact", True), "opening": ("contact", True),
               "garage_door": ("contact", True), "moisture": ("water_leak", False),
               "smoke": ("smoke", False), "vibration": ("vibration", False),
               "tamper": ("tamper", False)}


class EntityMap:
    """How one device's entities become state keys and controls. Pure: built
    from entity infos, then applied to each state update."""

    def __init__(self, entities: List[Any]):
        self.by_key: Dict[int, Tuple[str, Any]] = {}
        chans = [e for e in entities if _kind(e) in ("switch", "light", "cover")
                 and not getattr(e, "disabled_by_default", False)
                 and not getattr(e, "entity_category", 0)]
        chans.sort(key=lambda e: (("switch", "light", "cover").index(_kind(e)), e.object_id))
        self.multi = len(chans) > 1
        self.channel: Dict[int, int] = {e.key: i for i, e in enumerate(chans, start=1)}
        self.kind: Dict[int, str] = {e.key: _kind(e) for e in entities}
        self.object_id: Dict[int, str] = {e.key: e.object_id for e in entities}
        self.alias: Dict[int, Tuple[str, bool]] = {}
        used = set()
        for e in entities:
            dc = (getattr(e, "device_class", "") or "").lower()
            k = _kind(e)
            alias = None
            if k == "sensor" and dc in SENSOR_KEYS:
                alias = (SENSOR_KEYS[dc], False)
            elif k == "binary_sensor" and dc in BINARY_KEYS:
                alias = BINARY_KEYS[dc]
            if alias and alias[0] not in used:            # first of each kind takes the common key
                used.add(alias[0])
                self.alias[e.key] = alias
        self.controls: List[Dict[str, Any]] = []
        for e in chans:
            ep = self.channel[e.key]
            p = f"{e.name} " if self.multi else ""
            if _kind(e) == "cover":
                self.controls += [{"command": c, "label": f"{p}{c.title()}", "endpoint_id": ep}
                                  for c in ("open", "close")]
                if getattr(e, "supports_stop", False):
                    self.controls.append({"command": "stop", "label": f"{p}Stop", "endpoint_id": ep})
                if getattr(e, "supports_position", False):
                    self.controls.append({"command": "position", "label": f"{p}Position", "type": "slider",
                                          "min": 0, "max": 100, "endpoint_id": ep})
                continue
            self.controls += [{"command": c, "label": f"{p}{c.title()}", "endpoint_id": ep}
                              for c in ("on", "off", "toggle")]
            if _kind(e) == "light":
                self.controls.append({"command": "brightness", "label": f"{p}Brightness",
                                      "type": "slider", "min": 0, "max": 100, "endpoint_id": ep})
                if getattr(e, "min_mireds", 0) and getattr(e, "max_mireds", 0):
                    self.controls.append({"command": "color_temp", "label": f"{p}Colour temperature",
                                          "type": "slider", "endpoint_id": ep,
                                          "min": int(1e6 / e.max_mireds), "max": int(1e6 / e.min_mireds)})
        self.buttons = {e.object_id: e.key for e in entities if _kind(e) == "button"}
        for name, key in self.buttons.items():
            self.controls.append({"command": "press", "label": f"Press {name}", "value": name})
        caps = set()
        for e in chans:
            caps.add({"switch": "switch", "light": "light", "cover": "cover"}[_kind(e)])
            if _kind(e) == "light":
                caps.update({"switch", "level_control"})
        if self.multi:
            caps.add("multi_endpoint")
        for key, _ in self.alias.values():
            caps.add({"temperature": "temperature_sensor", "humidity": "humidity_sensor",
                      "power": "power_monitoring", "illuminance": "illuminance_sensor",
                      "occupancy": "motion_sensor", "contact": "contact_sensor",
                      "battery": "battery"}.get(key, key))
        self.caps = sorted(caps)

    def apply(self, st: Any) -> Dict[str, Any]:
        key = getattr(st, "key", None)
        if key not in self.kind or getattr(st, "missing_state", False):
            return {}
        k = self.kind[key]
        out: Dict[str, Any] = {}
        sfx = f"_{self.channel[key]}" if self.multi and key in self.channel else ""
        if k in ("switch", "light"):
            on = bool(st.state)
            out[f"state{sfx}"], out[f"on{sfx}"] = ("ON" if on else "OFF"), on
            if k == "light" and getattr(st, "brightness", None) is not None:
                out[f"brightness{sfx}"] = round(st.brightness * 254)
                out[f"level{sfx}"] = round(st.brightness * 100)
            if k == "light" and getattr(st, "color_temperature", 0):
                out[f"color_temp{sfx}"] = round(st.color_temperature)
        elif k == "cover":
            if getattr(st, "position", None) is not None:
                out[f"position{sfx}"] = round(st.position * 100)
        elif k in ("sensor", "binary_sensor", "text_sensor"):
            v = st.state
            if isinstance(v, float) and v != v:            # NaN: no reading yet
                return {}
            out[self.object_id[key]] = v
            if key in self.alias:
                name, invert = self.alias[key]
                out[name] = (not v) if invert else v
        return out


def _kind(entity: Any) -> str:
    return {"SwitchInfo": "switch", "LightInfo": "light", "CoverInfo": "cover",
            "SensorInfo": "sensor", "BinarySensorInfo": "binary_sensor",
            "TextSensorInfo": "text_sensor", "ButtonInfo": "button"}.get(type(entity).__name__, "other")


class ESPHomeSession:
    def __init__(self, hub: "ESPHomeHub", dev: LanDevice, client_factory: Optional[Callable] = None):
        self.hub, self.dev = hub, dev
        self._factory = client_factory or _api_client
        self.client = None
        self.map: Optional[EntityMap] = None
        self._closed = False
        self._stopped = asyncio.Event()

    async def close(self) -> None:
        self._closed = True
        self._stopped.set()
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:                             # noqa: BLE001
                pass

    async def run(self) -> None:
        backoff = 5
        while not self._closed:
            try:
                await self._connect_once()
                backoff = 5
                await self._stopped.wait()                # until the device drops us
                self._stopped = asyncio.Event()
                if not self._closed:
                    await self.hub.offline(self.dev, "disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as e:                        # noqa: BLE001
                await self.hub.offline(self.dev, _friendly(e))
                logger.info("[esphome] %s: %s (retry in %ss)", self.dev.cfg["id"], _friendly(e), backoff)
            if not self._closed:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _connect_once(self) -> None:
        c = self.hub.creds(self.dev.cfg["id"])
        self.client = self._factory(self.dev.cfg["host"], self.dev.cfg["port"],
                                    c.get("password", ""), c.get("encryption_key", ""))

        async def on_stop(expected: bool) -> None:
            self._stopped.set()
        await self.client.connect(on_stop=on_stop, login=True)
        entities, _ = await self.client.list_entities_services()
        self.map = EntityMap(entities)
        await self.hub.publish(self.dev, {}, self.map.controls, self.map.caps)

        def on_state(st) -> None:
            update = self.map.apply(st) if self.map else {}
            if update:
                asyncio.get_running_loop().create_task(self.hub.publish(self.dev, update))
        self.client.subscribe_states(on_state)

    async def command(self, command: str, value: Any, endpoint: Optional[int]) -> None:
        if self.client is None or self.map is None:
            raise ValueError(f"{self.dev.friendly_name} isn't connected")
        if command == "press":
            key = self.map.buttons.get(str(value or ""))
            if key is None:
                raise ValueError(f"No button '{value}'")
            self.client.button_command(key)
            return
        ep = endpoint or 1
        key = next((k for k, e in self.map.channel.items() if e == ep), None)
        if key is None:
            raise ValueError(f"No channel {ep} on {self.dev.friendly_name}")
        kind = self.map.kind[key]
        sfx = f"_{ep}" if self.map.multi else ""
        if kind == "cover":
            if command == "open":
                self.client.cover_command(key, position=1.0)
            elif command == "close":
                self.client.cover_command(key, position=0.0)
            elif command == "stop":
                self.client.cover_command(key, stop=True)
            elif command == "position":
                self.client.cover_command(key, position=_pct(value) / 100)
            else:
                raise ValueError(f"A cover takes open/close/stop/position, not '{command}'")
            return
        if command == "toggle":
            command = "off" if self.dev.state.get(f"on{sfx}") else "on"
        if kind == "switch" and command in ("on", "off"):
            self.client.switch_command(key, command == "on")
        elif kind == "light" and command in ("on", "off"):
            self.client.light_command(key, state=command == "on")
        elif kind == "light" and command == "brightness":
            pct = _pct(value)
            self.client.light_command(key, state=pct > 0, brightness=pct / 100 if pct else None)
        elif kind == "light" and command == "color_temp":
            kelvin = max(1000, int(value or 4000))
            self.client.light_command(key, state=True, color_temperature=1e6 / kelvin)
        else:
            raise ValueError(f"{self.dev.friendly_name} has no '{command}'")


def _pct(value: Any) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        raise ValueError("Expected a number 0-100") from None


def _api_client(host: str, port: int, password: str, key: str):
    from aioesphomeapi import APIClient
    return APIClient(host, port, password or None, noise_psk=key or None, client_info="ZMM")


def _friendly(e: Exception) -> str:
    n = type(e).__name__
    if n == "InvalidEncryptionKeyAPIError":
        return "the encryption key doesn't match the device's api: key"
    if n == "RequiresEncryptionAPIError":
        return "the device requires its API encryption key"
    if n == "InvalidAuthAPIError":
        return "the device refused the API password"
    return f"{n}: {e}" if str(e) else n


class ESPHomeHub(LanDeviceHub):
    kind = "esphome"
    manufacturer = "ESPHome"
    default_port = DEFAULT_PORT
    secret_fields = ("encryption_key", "password")
    mdns_types = ("_esphomelib._tcp.local.",)

    def __init__(self, *a, client_factory: Optional[Callable] = None, **kw):
        self._client_factory = client_factory
        super().__init__(*a, **kw)

    async def probe(self, host: str, port: int, creds: Dict[str, str]) -> Dict[str, Any]:
        key = creds.get("encryption_key", "")
        if key:
            import base64
            import binascii
            try:
                if len(base64.b64decode(key, validate=True)) != 32:
                    raise ValueError
            except (binascii.Error, ValueError):
                raise ValueError("The encryption key is the 44-character base64 value from the "
                                 "device's api: encryption: key") from None
        client = (self._client_factory or _api_client)(host, port, creds.get("password", ""), key)
        try:
            await client.connect(login=True)
            info = await client.device_info()
        except Exception as e:                            # noqa: BLE001
            raise ValueError(_friendly(e)) from e
        finally:
            try:
                await client.disconnect()
            except Exception:                             # noqa: BLE001
                pass
        return {"id": info.name, "name": info.friendly_name or info.name,
                "model": info.model or info.project_name or "ESPHome",
                "mac": info.mac_address}

    def make_session(self, dev: LanDevice):
        return ESPHomeSession(self, dev, self._client_factory)

    def mdns_entry(self, name: str, host: str, port: int, props: Dict[str, str]):
        return {"name": props.get("friendly_name") or name, "host": host, "port": port or DEFAULT_PORT,
                "encrypted": bool(props.get("api_encryption"))}


_hub: Optional[ESPHomeHub] = None


def get_esphome_hub() -> Optional[ESPHomeHub]:
    return _hub


def set_esphome_hub(h: Optional[ESPHomeHub]) -> None:
    global _hub
    _hub = h
