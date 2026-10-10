"""
ESPHome (modules/esphome.py): entities to state keys and controls, state
updates, commands, reconnects and adding a device. Uses aioesphomeapi's own
entity and state classes where it's installed, look-alikes otherwise; the
connection is a recording fake.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from harness import Checker

from modules import esphome as E
from modules import lan_devices as L

try:
    from aioesphomeapi.model import (BinarySensorInfo, BinarySensorState, ButtonInfo, CoverInfo, CoverState,
                                     LightInfo, LightState, SensorInfo, SensorState, SwitchInfo, SwitchState)
    REAL = True
except ImportError:                     # a bare dev box
    REAL = False

    @dataclass
    class _Info:
        object_id: str = ""
        key: int = 0
        name: str = ""
        device_class: str = ""
        disabled_by_default: bool = False
        entity_category: int = 0
        supports_stop: bool = False
        supports_position: bool = False
        min_mireds: float = 0.0
        max_mireds: float = 0.0
        supported_color_modes: list = field(default_factory=list)

    @dataclass
    class _State:
        key: int = 0
        state: object = None
        missing_state: bool = False
        brightness: float = None
        color_temperature: float = 0.0
        position: float = None

    SwitchInfo = type("SwitchInfo", (_Info,), {})
    LightInfo = type("LightInfo", (_Info,), {})
    SensorInfo = type("SensorInfo", (_Info,), {})
    BinarySensorInfo = type("BinarySensorInfo", (_Info,), {})
    CoverInfo = type("CoverInfo", (_Info,), {})
    ButtonInfo = type("ButtonInfo", (_Info,), {})
    SwitchState = LightState = SensorState = BinarySensorState = CoverState = _State


def plug():
    return [SwitchInfo(object_id="relay", key=1, name="Relay"),
            SensorInfo(object_id="power", key=2, name="Power", device_class="power"),
            SensorInfo(object_id="uptime", key=3, name="Uptime", device_class="duration"),
            BinarySensorInfo(object_id="front_door", key=4, name="Front door", device_class="door"),
            BinarySensorInfo(object_id="hall_pir", key=5, name="Hall PIR", device_class="motion"),
            SwitchInfo(object_id="restart", key=6, name="Restart", entity_category=1),
            ButtonInfo(object_id="identify", key=7, name="Identify")]


def two_gang():
    return [LightInfo(object_id="lamp", key=10, name="Lamp", min_mireds=153, max_mireds=500),
            SwitchInfo(object_id="fan", key=11, name="Fan"),
            CoverInfo(object_id="blind", key=12, name="Blind", supports_stop=True, supports_position=True)]


class FakeClient:
    instances = []

    def __init__(self, host, port, password, key, entities=None, fail=None):
        self.host, self.port, self.password, self.key = host, port, password, key
        self.entities = entities if entities is not None else plug()
        self.fail = fail
        self.sent = []
        self.on_state = None
        self.on_stop = None
        FakeClient.instances.append(self)

    async def connect(self, on_stop=None, login=False):
        if self.fail:
            raise self.fail
        self.on_stop = on_stop

    async def device_info(self):
        class Info:
            name, friendly_name, model, project_name, mac_address = "kitchen-plug", "Kitchen plug", "esp32", "", "AA:BB"
        return Info()

    async def list_entities_services(self):
        return self.entities, []

    def subscribe_states(self, cb):
        self.on_state = cb

    async def disconnect(self, force=False):
        pass

    def switch_command(self, key, state):
        self.sent.append(("switch", key, state))

    def light_command(self, key, **kw):
        self.sent.append(("light", key, kw))

    def cover_command(self, key, **kw):
        self.sent.append(("cover", key, kw))

    def button_command(self, key):
        self.sent.append(("button", key))


class InvalidEncryptionKeyAPIError(Exception):
    pass


def run() -> Checker:
    c = Checker("esphome" + ("" if REAL else " (look-alike entities)"))

    c.section("entities")
    m = E.EntityMap(plug())
    c.check("one relay is the device's channel, unsuffixed; config entities aren't channels",
            m.channel == {1: 1} and not m.multi, m.channel)
    c.check("on/off/toggle and the button are offered",
            [x["command"] for x in m.controls] == ["on", "off", "toggle", "press"], m.controls)
    c.check("capabilities follow the device classes",
            {"switch", "power_monitoring", "contact_sensor", "motion_sensor"} <= set(m.caps), m.caps)
    c.check("a relay turning on reports state and on",
            m.apply(SwitchState(key=1, state=True)) == {"state": "ON", "on": True})
    c.check("a power sensor reports under its own name and the common key",
            m.apply(SensorState(key=2, state=12.5)) == {"power": 12.5})
    c.check("a sensor with no common key keeps its own",
            m.apply(SensorState(key=3, state=300.0)) == {"uptime": 300.0})
    c.check("a door opening is contact False, as ZCL has it",
            m.apply(BinarySensorState(key=4, state=True)) == {"front_door": True, "contact": False})
    c.check("motion is occupancy", m.apply(BinarySensorState(key=5, state=True))["occupancy"] is True)
    c.check("a reading the device hasn't got yet is skipped",
            m.apply(SensorState(key=2, state=float("nan"))) == {}
            and m.apply(SensorState(key=2, state=1.0, missing_state=True)) == {})

    m2 = E.EntityMap(two_gang())
    c.check("several channels are numbered: switch, light, cover order",
            m2.multi and m2.channel == {11: 1, 10: 2, 12: 3}, m2.channel)
    c.check("a light reports brightness 0-254, level %, colour temperature in mireds",
            m2.apply(LightState(key=10, state=True, brightness=0.5, color_temperature=370))
            == {"state_2": "ON", "on_2": True, "brightness_2": 127, "level_2": 50, "color_temp_2": 370})
    c.check("a light's colour slider runs in Kelvin, from its mired range",
            next(x for x in m2.controls if x["command"] == "color_temp")["min"] == 2000)
    c.check("a cover reports position %", m2.apply(CoverState(key=12, position=0.25)) == {"position_3": 25})

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            saved = L.SECRETS_FILE
            L.SECRETS_FILE = str(Path(tmp) / "secrets.yaml")
            evals = []

            async def evaluate(ieee, changed):
                evals.append((ieee, dict(changed)))
            entities = {"current": plug()}

            def factory(host, port, password, key):
                fail = InvalidEncryptionKeyAPIError("x") if key == "wrong" else None
                return FakeClient(host, port, password, key, entities["current"], fail)
            hub = E.ESPHomeHub(path=Path(tmp) / "esphome.json", evaluate=evaluate, client_factory=factory)
            good_key = "px7tsbK3C7bpXHr2OevEV2ZMg/FrNZw2+O2pNPbedtA="
            try:
                c.section("adding")
                try:
                    await hub.add({"host": "10.0.0.5", "encryption_key": "not-a-key"})
                    c.check("a malformed key is refused before connecting", False)
                except ValueError as e:
                    c.check("a malformed key is refused before connecting", "44-character" in str(e), str(e))
                hub._client_factory = lambda h, p, pw, k: FakeClient(h, p, pw, k, plug(),
                                                                     InvalidEncryptionKeyAPIError("x"))
                try:
                    await hub.add({"host": "10.0.0.5", "encryption_key": good_key})
                    c.check("a key the device rejects says so", False)
                except ValueError as e:
                    c.check("a key the device rejects says so", "encryption key doesn't match" in str(e), str(e))
                hub._client_factory = factory
                dev = await hub.add({"host": "10.0.0.5", "encryption_key": good_key})
                c.check("added under the device's name, on the API port",
                        dev["id"] == "kitchen-plug" and dev["name"] == "Kitchen plug" and dev["port"] == 6053, dev)
                c.check("the key lives in the secrets file only",
                        good_key not in (Path(tmp) / "esphome.json").read_text()
                        and good_key in Path(L.SECRETS_FILE).read_text())

                c.section("session")
                d = hub.devices["kitchen-plug"]
                FakeClient.instances.clear()
                hub._started = True
                hub._start(d)
                await asyncio.sleep(0.05)
                client = FakeClient.instances[-1]
                c.check("connects with the stored key and publishes controls",
                        client.key == good_key and d.controls and d.online, (client.key, d.controls))
                client.on_state(SwitchState(key=1, state=True))
                client.on_state(BinarySensorState(key=5, state=True))
                await asyncio.sleep(0.05)
                c.check("pushed states reach the device and the rule engine",
                        d.state["state"] == "ON" and d.state["occupancy"] is True
                        and ("esphome::kitchen-plug", {"hall_pir": True, "occupancy": True}) in evals, evals)

                c.section("commands")
                await d.send_command("off")
                await d.send_command("toggle")
                await d.send_command("press", "identify")
                c.check("off, toggle from on, and a button press reach the device",
                        client.sent == [("switch", 1, False), ("switch", 1, False), ("button", 7)], client.sent)
                r = await d.send_command("brightness", 50)
                c.check("a relay has no brightness, and says so", not r["success"], r)
                r = await d.send_command("press", "self_destruct")
                c.check("an unknown button is refused", not r["success"], r)

                c.section("reconnecting")
                await client.on_stop(False)
                await asyncio.sleep(0.05)
                c.check("a dropped connection marks it offline", d.online is False and not d.is_available())
                d.session._stopped.set()
                await hub.stop()
            finally:
                await hub.stop()
                L.SECRETS_FILE = saved

        c.section("multi-channel commands")
        with tempfile.TemporaryDirectory() as tmp:
            saved = L.SECRETS_FILE
            L.SECRETS_FILE = str(Path(tmp) / "secrets.yaml")
            hub = E.ESPHomeHub(path=Path(tmp) / "e.json",
                               client_factory=lambda h, p, pw, k: FakeClient(h, p, pw, k, two_gang()))
            try:
                await hub.add({"host": "10.0.0.6"})
                d = next(iter(hub.devices.values()))
                s = hub.make_session(d)
                d.session = s
                await s._connect_once()
                await d.send_command("brightness", 40, 2)
                await d.send_command("color_temp", 2700, 2)
                await d.send_command("position", 30, 3)
                await d.send_command("stop", None, 3)
                await d.send_command("on", None, 1)
                sent = s.client.sent
                c.check("channel 2 is the light: brightness 0-1, colour temperature in mireds",
                        sent[0] == ("light", 10, {"state": True, "brightness": 0.4})
                        and sent[1][0] == "light" and abs(sent[1][2]["color_temperature"] - 370.37) < 0.1, sent[:2])
                c.check("channel 3 is the cover: position 0-1 and stop",
                        sent[2] == ("cover", 12, {"position": 0.3}) and sent[3] == ("cover", 12, {"stop": True}), sent[2:4])
                c.check("channel 1 is the switch", sent[4] == ("switch", 11, True), sent[4])
                r = await d.send_command("on", None, 9)
                c.check("a channel that doesn't exist is refused", not r["success"], r)
            finally:
                await hub.stop()
                L.SECRETS_FILE = saved

    asyncio.run(scenario())
    return c
