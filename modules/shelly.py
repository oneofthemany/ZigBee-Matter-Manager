"""
Shelly devices over their local API — no cloud. See docs/wifi-devices.md §Shelly.

  Gen1   REST: GET /status, /relay/N?turn=, /light/N, /roller/N; polled.
  Gen2+  JSON-RPC (Plus, Pro, Gen3/4): Shelly.GetStatus, Switch.Set, Light.Set,
         Cover.*; pushed over the /rpc websocket (NotifyStatus, NotifyEvent for
         button presses), with an HTTP poll as the fallback.

Hand-written rather than aioshelly, which brings a Bluetooth stack ZMM doesn't
need. Battery Shellys (H&T, Door/Window) sleep and only wake to push to a
server; they aren't supported.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from typing import Any, Callable, Dict, List, Optional, Tuple

from modules.lan_devices import LanDevice, LanDeviceHub

logger = logging.getLogger("shelly")

TIMEOUT_S = 8
GEN1_POLL_S = 5
GEN2_POLL_S = 30            # only while the websocket is down
GEN2_REFRESH_S = 300        # full status even with push, to catch anything missed
ACTUATORS = ("switch", "light", "cover")
# Gen2 input events -> the `action` values Zigbee buttons report.
EVENTS = {"single_push": "single", "double_push": "double", "triple_push": "triple",
          "long_push": "hold", "btn_down": "press", "btn_up": "release"}


# Normalisation (pure — tested against real payloads)

def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _onoff(on: Any) -> Dict[str, Any]:
    return {"state": "ON" if on else "OFF", "on": bool(on)}


def _suffix(state: Dict[str, Any], ep: Optional[int]) -> Dict[str, Any]:
    return state if ep is None else {f"{k}_{ep}": v for k, v in state.items()}


def _controls(kind: str, ep: Optional[int], label: str) -> List[Dict[str, Any]]:
    e = ep or 1
    p = f"{label} " if label else ""
    if kind == "cover":
        return [{"command": "open", "label": f"{p}Open", "endpoint_id": e},
                {"command": "close", "label": f"{p}Close", "endpoint_id": e},
                {"command": "stop", "label": f"{p}Stop", "endpoint_id": e},
                {"command": "position", "label": f"{p}Position", "type": "slider",
                 "min": 0, "max": 100, "endpoint_id": e}]
    out = [{"command": c, "label": f"{p}{c.title()}", "endpoint_id": e} for c in ("on", "off", "toggle")]
    if kind == "light":
        out.append({"command": "brightness", "label": f"{p}Brightness", "type": "slider",
                    "min": 0, "max": 100, "endpoint_id": e})
    return out


def _caps_for(kinds: List[str], sensors: Dict[str, Any]) -> List[str]:
    caps = set()
    for k in kinds:
        caps.add({"switch": "switch", "light": "light", "cover": "cover"}[k])
        if k == "light":
            caps.update({"switch", "level_control"})
    if len(kinds) > 1:
        caps.add("multi_endpoint")
    for key, cap in (("power", "power_monitoring"), ("temperature", "temperature_sensor"),
                     ("humidity", "humidity_sensor"), ("illuminance", "illuminance_sensor"),
                     ("contact", "contact_sensor"), ("occupancy", "motion_sensor"),
                     ("battery", "battery")):
        if any(k == key or k.startswith(key + "_") for k in sensors):
            caps.add(cap)
    return sorted(caps)


def gen2_normalise(status: Dict[str, Any], names: Optional[Dict[str, str]] = None
                   ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str], Dict[str, int]]:
    """(state, controls, caps, component -> endpoint) from Shelly.GetStatus."""
    names = names or {}
    comps = sorted((k for k in status if k.split(":")[0] in ACTUATORS),
                   key=lambda k: (ACTUATORS.index(k.split(":")[0]), int(k.split(":")[1])))
    multi = len(comps) > 1
    state: Dict[str, Any] = {}
    controls: List[Dict[str, Any]] = []
    endpoints: Dict[str, int] = {}
    for i, comp in enumerate(comps, start=1):
        kind, s = comp.split(":")[0], status[comp] or {}
        ep = i if multi else None
        endpoints[comp] = i
        ch: Dict[str, Any] = {}
        if kind == "cover":
            pos = _num(s.get("current_pos"))
            if pos is not None:
                ch["position"] = int(pos)
            ch["cover_state"] = s.get("state")
        else:
            ch.update(_onoff(s.get("output")))
            if kind == "light" and _num(s.get("brightness")) is not None:
                ch["brightness"] = round(s["brightness"] * 2.54)
                ch["level"] = int(s["brightness"])
        for src, dst in (("apower", "power"), ("voltage", "voltage"), ("current", "current")):
            if _num(s.get(src)) is not None:
                ch[dst] = s[src]
        total = (s.get("aenergy") or {}).get("total")
        if _num(total) is not None:
            ch["energy"] = round(total / 1000, 3)            # Wh -> kWh
        state.update(_suffix(ch, ep))
        if i == 1 and _num((s.get("temperature") or {}).get("tC")) is not None:
            state["device_temperature"] = s["temperature"]["tC"]
        controls += _controls(kind, ep, names.get(comp, "") if multi else "")

    # Plus PM Mini / Pro EM style meters with no actuator
    for comp in sorted(k for k in status if k.split(":")[0] in ("pm1", "em1")):
        s = status[comp] or {}
        if _num(s.get("apower")) is not None and "power" not in state:
            state["power"] = s["apower"]
    sensors: Dict[str, Any] = {}
    for comp, s in status.items():
        kind = comp.split(":")[0]
        s = s or {}
        if kind == "temperature" and _num(s.get("tC")) is not None:
            sensors.setdefault("temperature", s["tC"])
        elif kind == "humidity" and _num(s.get("rh")) is not None:
            sensors.setdefault("humidity", s["rh"])
        elif kind == "illuminance" and _num(s.get("lux")) is not None:
            sensors.setdefault("illuminance", s["lux"])
        elif kind == "devicepower" and _num((s.get("battery") or {}).get("percent")) is not None:
            sensors.setdefault("battery", s["battery"]["percent"])
        elif kind == "input" and isinstance(s.get("state"), bool):
            sensors[f"input_{int(comp.split(':')[1]) + 1}"] = s["state"]
    state.update(sensors)
    caps = _caps_for([c.split(":")[0] for c in comps], {**state})
    if any(k.startswith("input") for k in status):
        caps.append("button")
    return state, controls, sorted(set(caps)), endpoints


def gen2_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A NotifyEvent entry -> {action, action_endpoint} for button presses."""
    comp, ev = str(event.get("component") or ""), EVENTS.get(str(event.get("event") or ""))
    if not comp.startswith("input:") or ev is None:
        return None
    return {"action": ev, "action_endpoint": int(comp.split(":")[1]) + 1}


def gen1_normalise(status: Dict[str, Any], mode: str = "relay"
                   ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    """(state, controls, caps) from a Gen1 /status."""
    if mode == "roller" and status.get("rollers"):
        chans = [("cover", r) for r in status["rollers"]]
    elif status.get("lights"):
        chans = [("light", x) for x in status["lights"]]
    else:
        chans = [("switch", x) for x in status.get("relays") or []]
    meters = status.get("meters") or status.get("emeters") or []
    multi = len(chans) > 1
    state: Dict[str, Any] = {}
    controls: List[Dict[str, Any]] = []
    for i, (kind, s) in enumerate(chans, start=1):
        ep = i if multi else None
        ch: Dict[str, Any] = {}
        if kind == "cover":
            if _num(s.get("current_pos")) is not None:
                ch["position"] = int(s["current_pos"])
            ch["cover_state"] = s.get("state")
            if _num(s.get("power")) is not None:
                ch["power"] = s["power"]
        else:
            ch.update(_onoff(s.get("ison")))
            if kind == "light" and _num(s.get("brightness")) is not None:
                ch["brightness"] = round(s["brightness"] * 2.54)
                ch["level"] = int(s["brightness"])
            m = meters[i - 1] if i - 1 < len(meters) else {}
            if _num(m.get("power")) is not None:
                ch["power"] = m["power"]
            if _num(m.get("total")) is not None:
                # meters count watt-minutes, emeters watt-hours
                ch["energy"] = round(m["total"] / (60000 if "meters" in status else 1000), 3)
            if _num(m.get("voltage")) is not None:
                ch["voltage"] = m["voltage"]
        state.update(_suffix(ch, ep))
        controls += _controls(kind, ep, f"Channel {i}" if multi else "")
    if not chans and meters and _num(meters[0].get("power")) is not None:
        state["power"] = meters[0]["power"]
    if _num((status.get("tmp") or {}).get("tC")) is not None:
        key = "device_temperature" if chans else "temperature"
        state[key] = status["tmp"]["tC"]
    if _num((status.get("hum") or {}).get("value")) is not None:
        state["humidity"] = status["hum"]["value"]
    if _num((status.get("lux") or {}).get("value")) is not None:
        state["illuminance"] = status["lux"]["value"]
    if _num((status.get("bat") or {}).get("value")) is not None:
        state["battery"] = status["bat"]["value"]
    sensor = (status.get("sensor") or {}).get("state")
    if sensor in ("open", "close"):
        state["contact"] = sensor == "close"                 # ZCL: True = closed
    for i, inp in enumerate(status.get("inputs") or [], start=1):
        state[f"input_{i}"] = bool(inp.get("input"))
    caps = _caps_for([k for k, _ in chans], state)
    return state, controls, caps


# Gen2 digest auth (RPC "auth" object; docs: shelly-api-docs.shelly.cloud §Authentication)

def rpc_auth(challenge: Dict[str, Any], password: str) -> Dict[str, Any]:
    realm, nonce = challenge["realm"], challenge["nonce"]
    nc = challenge.get("nc", 1)
    cnonce = secrets.randbelow(10**9)
    h = lambda s: hashlib.sha256(s.encode()).hexdigest()   # noqa: E731
    ha1 = h(f"admin:{realm}:{password}")
    ha2 = h("dummy_method:dummy_uri")
    return {"realm": realm, "username": "admin", "nonce": nonce, "cnonce": cnonce,
            "response": h(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}"), "algorithm": "SHA-256"}


class ShellyError(ValueError):
    pass


# HTTP client

HttpFn = Callable[..., Any]


async def _httpx(method: str, url: str, json_body=None, params=None, auth=None) -> Tuple[int, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False) as cx:
        r = await cx.request(method, url, json=json_body, params=params, auth=auth)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:300]


class ShellyClient:
    def __init__(self, host: str, port: int = 80, username: str = "", password: str = "",
                 gen: Optional[int] = None, http: Optional[HttpFn] = None):
        self.base = f"http://{host}:{port}" if port != 80 else f"http://{host}"
        self.username, self.password, self.gen = username or "admin", password, gen
        self._http = http or _httpx

    def _auth(self):
        if not self.password:
            return None
        import httpx
        if (self.gen or 1) >= 2:
            return httpx.DigestAuth("admin", self.password)
        return httpx.BasicAuth(self.username, self.password)

    async def _get(self, path: str, params=None) -> Any:
        try:
            status, body = await self._http("GET", self.base + path, params=params, auth=self._auth())
        except Exception as e:                            # noqa: BLE001
            raise ShellyError(f"no answer from {self.base} ({type(e).__name__})") from e
        if status == 401:
            raise ShellyError("the Shelly refused the password" if self.password else
                              "the Shelly has a password set — enter it")
        if status >= 400:
            raise ShellyError(f"the Shelly answered {status}")
        return body

    async def identify(self) -> Dict[str, Any]:
        """GET /shelly needs no auth on any generation."""
        info = await self._get("/shelly")
        if not isinstance(info, dict) or not (info.get("mac") or info.get("id")):
            raise ShellyError("that address isn't a Shelly")
        self.gen = int(info.get("gen") or 1)
        return info

    async def rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            status, body = await self._http("POST", self.base + "/rpc",
                                            json_body={"id": 1, "method": method, "params": params or {}},
                                            auth=self._auth())
        except Exception as e:                            # noqa: BLE001
            raise ShellyError(f"no answer from {self.base} ({type(e).__name__})") from e
        if status == 401:
            raise ShellyError("the Shelly refused the password" if self.password else
                              "the Shelly has a password set — enter it")
        if isinstance(body, dict) and body.get("error"):
            raise ShellyError(f"{method}: {body['error'].get('message')}")
        if status >= 400:
            raise ShellyError(f"{method}: the Shelly answered {status}")
        return body.get("result") if isinstance(body, dict) else None

    async def gen1(self, path: str, params=None) -> Any:
        return await self._get(path, params)


# Session: keeps one device current

class ShellySession:
    def __init__(self, hub: "ShellyHub", dev: LanDevice, client: Optional[ShellyClient] = None):
        self.hub, self.dev = hub, dev
        c = hub.creds(dev.cfg["id"])
        self.client = client or ShellyClient(dev.cfg["host"], dev.cfg["port"], c.get("username", ""),
                                             c.get("password", ""), dev.cfg.get("gen"))
        self.status: Dict[str, Any] = {}
        self.names: Dict[str, str] = {}
        self.endpoints: Dict[str, int] = {}
        self.mode = "relay"
        self._closed = False
        self._ws = None

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            await self._ws.close()

    # State
    async def _publish_gen2(self, extra: Optional[Dict[str, Any]] = None) -> None:
        state, controls, caps, self.endpoints = gen2_normalise(self.status, self.names)
        await self.hub.publish(self.dev, {**state, **(extra or {})}, controls, caps)

    async def refresh(self) -> None:
        if (self.client.gen or 1) >= 2:
            self.status = await self.client.rpc("Shelly.GetStatus") or {}
            await self._publish_gen2()
        else:
            state, controls, caps = gen1_normalise(await self.client.gen1("/status") or {}, self.mode)
            await self.hub.publish(self.dev, state, controls, caps)

    async def _setup(self) -> None:
        if self.client.gen is None:
            await self.client.identify()
        if self.client.gen >= 2:
            try:
                cfg = await self.client.rpc("Shelly.GetConfig") or {}
                self.names = {k: v.get("name") for k, v in cfg.items()
                              if isinstance(v, dict) and v.get("name") and k.split(":")[0] in ACTUATORS}
            except ShellyError:
                self.names = {}
        else:
            settings = await self.client.gen1("/settings") or {}
            self.mode = settings.get("mode") or "relay"

    async def run(self) -> None:
        backoff = 5
        while not self._closed:
            try:
                await self._setup()
                await self.refresh()
                backoff = 5
                if self.client.gen >= 2:
                    await self._push_loop()
                else:
                    while not self._closed:
                        await asyncio.sleep(GEN1_POLL_S)
                        await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:                        # noqa: BLE001
                await self.hub.offline(self.dev, str(e))
                logger.info("[shelly] %s: %s (retry in %ss)", self.dev.cfg["id"], e, backoff)
            if not self._closed:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _push_loop(self) -> None:
        """Gen2+ websocket; falls back to polling when it can't be held open."""
        import aiohttp
        url = self.client.base.replace("http://", "ws://") + "/rpc"
        src = f"zmm-{secrets.token_hex(4)}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, heartbeat=30, timeout=TIMEOUT_S) as ws:
                    self._ws = ws
                    req = {"id": 1, "src": src, "method": "Shelly.GetStatus"}
                    await ws.send_str(json.dumps(req))
                    last_full = asyncio.get_running_loop().time()
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            break
                        data = json.loads(msg.data)
                        err = data.get("error") or {}
                        if err.get("code") == 401 and self.client.password and "auth" not in req:
                            req["auth"] = rpc_auth(json.loads(err["message"]), self.client.password)
                            await ws.send_str(json.dumps(req))
                            continue
                        if err:
                            raise ShellyError(err.get("message") or "websocket refused")
                        await self._on_message(data)
                        if asyncio.get_running_loop().time() - last_full > GEN2_REFRESH_S:
                            last_full = asyncio.get_running_loop().time()
                            await self.refresh()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.info("[shelly] %s websocket unavailable (%s); polling", self.dev.cfg["id"], e)
        finally:
            self._ws = None
        while not self._closed:
            await asyncio.sleep(GEN2_POLL_S)
            await self.refresh()

    async def _on_message(self, data: Dict[str, Any]) -> None:
        if "result" in data and isinstance(data["result"], dict):
            self.status = data["result"]
            await self._publish_gen2()
            return
        method, params = data.get("method"), data.get("params") or {}
        if method in ("NotifyStatus", "NotifyFullStatus"):
            for comp, v in params.items():
                if isinstance(v, dict) and isinstance(self.status.get(comp), dict):
                    _deep_merge(self.status[comp], v)
                elif isinstance(v, dict):
                    self.status[comp] = v
            await self._publish_gen2()
        elif method == "NotifyEvent":
            for ev in params.get("events") or []:
                action = gen2_event(ev)
                if action:
                    await self._publish_gen2(action)

    # Commands
    async def command(self, command: str, value: Any, endpoint: Optional[int]) -> None:
        if (self.client.gen or 1) >= 2:
            await self._command_gen2(command, value, endpoint)
        else:
            await self._command_gen1(command, value, endpoint)
        await self.refresh()

    def _component(self, endpoint: Optional[int], want: Tuple[str, ...]) -> Tuple[str, int]:
        ep = endpoint or 1
        for comp, e in self.endpoints.items():
            if e == ep and comp.split(":")[0] in want:
                return comp.split(":")[0], int(comp.split(":")[1])
        raise ValueError(f"No {'/'.join(want)} on channel {ep}")

    async def _command_gen2(self, command: str, value: Any, endpoint: Optional[int]) -> None:
        if command in ("open", "close", "stop", "position"):
            _, cid = self._component(endpoint, ("cover",))
            if command == "position":
                await self.client.rpc("Cover.GoToPosition", {"id": cid, "pos": _pct(value)})
            else:
                await self.client.rpc(f"Cover.{command.title()}", {"id": cid})
            return
        kind, cid = self._component(endpoint, ("switch", "light"))
        api = "Switch" if kind == "switch" else "Light"
        if command in ("on", "off"):
            await self.client.rpc(f"{api}.Set", {"id": cid, "on": command == "on"})
        elif command == "toggle":
            await self.client.rpc(f"{api}.Toggle", {"id": cid})
        elif command == "brightness" and kind == "light":
            await self.client.rpc("Light.Set", {"id": cid, "on": _pct(value) > 0, "brightness": _pct(value)})
        else:
            raise ValueError(f"{self.dev.friendly_name} has no '{command}'")

    async def _command_gen1(self, command: str, value: Any, endpoint: Optional[int]) -> None:
        n = (endpoint or 1) - 1
        if command in ("open", "close", "stop"):
            await self.client.gen1(f"/roller/{n}", {"go": command})
        elif command == "position":
            await self.client.gen1(f"/roller/{n}", {"go": "to_pos", "roller_pos": _pct(value)})
        elif command in ("on", "off", "toggle"):
            path = "light" if any(c.get("command") == "brightness" for c in self.dev.controls) else "relay"
            await self.client.gen1(f"/{path}/{n}", {"turn": command})
        elif command == "brightness":
            await self.client.gen1(f"/light/{n}", {"turn": "on" if _pct(value) else "off",
                                                    "brightness": _pct(value)})
        else:
            raise ValueError(f"{self.dev.friendly_name} has no '{command}'")


def _pct(value: Any) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        raise ValueError("Expected a number 0-100") from None


def _deep_merge(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


class ShellyHub(LanDeviceHub):
    kind = "shelly"
    manufacturer = "Shelly"
    secret_fields = ("password",)
    mdns_types = ("_shelly._tcp.local.", "_http._tcp.local.")

    def __init__(self, *a, client_factory: Optional[Callable[..., ShellyClient]] = None, **kw):
        self._client_factory = client_factory
        super().__init__(*a, **kw)

    async def probe(self, host: str, port: int, creds: Dict[str, str]) -> Dict[str, Any]:
        make = self._client_factory or ShellyClient
        c = make(host, port, creds.get("username", ""), creds.get("password", ""))
        info = await c.identify()
        # Prove the password too: a wrong one would only show up later as "offline".
        if c.gen >= 2:
            await c.rpc("Shelly.GetStatus")
        else:
            await c.gen1("/status")
        dev_id = info.get("id") or f"shelly-{str(info.get('mac', '')).lower()}"
        return {"id": dev_id, "name": info.get("name") or info.get("app") or info.get("type") or dev_id,
                "model": info.get("model") or info.get("type") or "", "gen": c.gen,
                "mac": info.get("mac")}

    def make_session(self, dev: LanDevice):
        c = None
        if self._client_factory:
            cr = self.creds(dev.cfg["id"])
            c = self._client_factory(dev.cfg["host"], dev.cfg["port"], cr.get("username", ""),
                                     cr.get("password", ""), dev.cfg.get("gen"))
        return ShellySession(self, dev, c)

    def mdns_entry(self, name: str, host: str, port: int, props: Dict[str, str]):
        # _http._tcp carries everything on the LAN; only Shelly names count.
        if not name.lower().startswith("shelly"):
            return None
        return {"name": name, "host": host, "port": port, "gen": int(props.get("gen") or 1)}


_hub: Optional[ShellyHub] = None


def get_shelly_hub() -> Optional[ShellyHub]:
    return _hub


def set_shelly_hub(h: Optional[ShellyHub]) -> None:
    global _hub
    _hub = h
