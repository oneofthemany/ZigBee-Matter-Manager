"""
Shelly (modules/shelly.py): status payloads shaped like real Gen1 and Gen2
devices' into ZMM state, digest auth, push updates, button events, commands,
and adding a device. The network is a recording fake; everything else is real.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path

from harness import Checker

from modules import lan_devices as L
from modules import shelly as S

PLUS_1PM = {"switch:0": {"id": 0, "source": "init", "output": True, "apower": 42.5, "voltage": 231.4,
                         "current": 0.21, "aenergy": {"total": 12345.6}, "temperature": {"tC": 41.2}},
            "input:0": {"id": 0, "state": False}, "sys": {"mac": "A8032AB12345"}, "wifi": {"rssi": -60}}
PLUS_2PM = {"switch:0": {"id": 0, "output": True, "apower": 10.0, "aenergy": {"total": 1000}},
            "switch:1": {"id": 1, "output": False, "apower": 0.0, "aenergy": {"total": 2000}},
            "input:0": {"id": 0, "state": False}, "input:1": {"id": 1, "state": True}}
PLUS_2PM_COVER = {"cover:0": {"id": 0, "state": "stopped", "current_pos": 63, "apower": 0.0}}
DIMMER2 = {"light:0": {"id": 0, "output": True, "brightness": 50}}
PLUS_HT = {"temperature:0": {"id": 0, "tC": 21.4}, "humidity:0": {"id": 0, "rh": 48.2},
           "devicepower:0": {"id": 0, "battery": {"V": 2.9, "percent": 88}}}

G1_1PM = {"relays": [{"ison": True}], "meters": [{"power": 60.0, "total": 600000}], "tmp": {"tC": 40.0},
          "inputs": [{"input": 1}]}
G1_25_ROLLER = {"rollers": [{"state": "open", "current_pos": 100, "power": 0.0}],
                "relays": [{"ison": False}, {"ison": False}], "meters": [{"power": 0}, {"power": 0}]}
G1_DW2 = {"sensor": {"state": "open", "is_valid": True}, "lux": {"value": 120}, "bat": {"value": 97},
          "tmp": {"tC": 19.5}}
G1_EM = {"relays": [{"ison": True}], "emeters": [{"power": 1500.0, "total": 5000.0, "voltage": 240.1}]}


class FakeClient:
    def __init__(self, host="h", port=80, username="", password="", gen=None, info=None,
                 status=None, gen1_status=None, settings=None, require_password=None):
        self.host, self.password, self.gen = host, password, gen
        self.info = info or {"id": "shellyplus1pm-a8032ab12345", "mac": "A8032AB12345", "gen": 2,
                             "model": "SNSW-001P16EU", "app": "Plus1PM", "name": "Hall light"}
        self.status = status if status is not None else dict(PLUS_1PM)
        self.gen1_status = gen1_status or {}
        self.settings = settings or {"mode": "relay"}
        self.require_password = require_password
        self.calls = []
        self.base = f"http://{host}"

    def _check(self):
        if self.require_password is not None and self.password != self.require_password:
            raise S.ShellyError("the Shelly refused the password")

    async def identify(self):
        self.gen = int(self.info.get("gen") or 1)
        return self.info

    async def rpc(self, method, params=None):
        self._check()
        self.calls.append((method, params))
        if method == "Shelly.GetStatus":
            return self.status
        if method == "Shelly.GetConfig":
            return {"switch:0": {"name": "Hall"}}
        return {}

    async def gen1(self, path, params=None):
        self._check()
        self.calls.append((path, params))
        if path == "/status":
            return self.gen1_status
        if path == "/settings":
            return self.settings
        return {}


def run() -> Checker:
    c = Checker("shelly")

    c.section("Gen2 status")
    st, ctl, caps, eps = S.gen2_normalise(PLUS_1PM)
    c.check("a single relay reports state, power, energy in kWh, voltage, current",
            st["state"] == "ON" and st["on"] is True and st["power"] == 42.5 and st["energy"] == 12.346
            and st["voltage"] == 231.4 and st["current"] == 0.21, st)
    c.check("the chip's temperature isn't passed off as the room's",
            st.get("device_temperature") == 41.2 and "temperature" not in st, st)
    c.check("one relay: on/off/toggle, no channel suffix",
            [x["command"] for x in ctl] == ["on", "off", "toggle"] and "state_1" not in st, ctl)
    c.check("capabilities name a switch with metering", {"switch", "power_monitoring"} <= set(caps), caps)

    st, ctl, caps, eps = S.gen2_normalise(PLUS_2PM, {"switch:0": "Lamp", "switch:1": "Fan"})
    c.check("two relays become channels 1 and 2, as the swarm reads endpoints",
            st["state_1"] == "ON" and st["state_2"] == "OFF" and st["power_1"] == 10.0 and "state" not in st, st)
    c.check("…with controls per channel, labelled with the names set on the device",
            [(x["endpoint_id"], x["label"]) for x in ctl if x["command"] == "on"] == [(1, "Lamp On"), (2, "Fan On")], ctl)
    c.check("…and multi_endpoint", "multi_endpoint" in caps)
    c.check("inputs are reported", st["input_2"] is True and "button" in caps, st)

    st, ctl, caps, _ = S.gen2_normalise(PLUS_2PM_COVER)
    c.check("a 2PM in cover mode is a cover with a position",
            st["position"] == 63 and st["cover_state"] == "stopped" and "cover" in caps
            and {x["command"] for x in ctl} == {"open", "close", "stop", "position"}, (st, ctl))

    st, ctl, caps, _ = S.gen2_normalise(DIMMER2)
    c.check("a dimmer reports brightness on the 0-254 scale and level in %",
            st["brightness"] == 127 and st["level"] == 50 and "light" in caps
            and any(x["command"] == "brightness" for x in ctl), st)

    st, ctl, caps, _ = S.gen2_normalise(PLUS_HT)
    c.check("an H&T reports temperature, humidity and battery, and nothing to switch",
            st == {"temperature": 21.4, "humidity": 48.2, "battery": 88} and ctl == []
            and {"temperature_sensor", "humidity_sensor"} <= set(caps), (st, caps))

    c.check("a button press becomes the action Zigbee buttons report, with its channel",
            S.gen2_event({"component": "input:1", "event": "double_push"}) == {"action": "double", "action_endpoint": 2})
    c.check("other events are ignored", S.gen2_event({"component": "switch:0", "event": "overpower"}) is None)

    c.section("Gen1 status")
    st, ctl, caps = S.gen1_normalise(G1_1PM)
    c.check("a 1PM's watt-minutes become kWh", st["state"] == "ON" and st["power"] == 60.0 and st["energy"] == 10.0, st)
    st, ctl, caps = S.gen1_normalise(G1_25_ROLLER, "roller")
    c.check("a 2.5 in roller mode is one cover, not two relays",
            st.get("position") == 100 and "state_1" not in st and "cover" in caps, st)
    st, ctl, caps = S.gen1_normalise(G1_DW2)
    c.check("a door/window sensor's 'open' is contact False, as ZCL has it",
            st["contact"] is False and st["temperature"] == 19.5 and "contact_sensor" in caps, st)
    st, ctl, caps = S.gen1_normalise(G1_EM)
    c.check("an EM's watt-hours become kWh", st["power"] == 1500.0 and st["energy"] == 5.0 and st["voltage"] == 240.1, st)

    c.section("digest auth")
    ch = {"realm": "shellyplus1-a8032ab12345", "nonce": 1625038762, "nc": 1, "algorithm": "SHA-256"}
    a = S.rpc_auth(ch, "secret")
    h = lambda s: hashlib.sha256(s.encode()).hexdigest()   # noqa: E731
    want = h(f"{h('admin:shellyplus1-a8032ab12345:secret')}:1625038762:1:{a['cnonce']}:auth:{h('dummy_method:dummy_uri')}")
    c.check("the RPC auth object is Shelly's SHA-256 digest", a["response"] == want and a["username"] == "admin", a)

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            saved = L.SECRETS_FILE
            L.SECRETS_FILE = str(Path(tmp) / "secrets.yaml")
            pushed, evals = [], []

            async def broadcast(t, p):
                pushed.append((t, p))

            async def evaluate(ieee, changed):
                evals.append((ieee, dict(changed)))
            fakes = {}

            def factory(host, port=80, username="", password="", gen=None):
                f = FakeClient(host, port, username, password, gen, require_password="pw" if host == "locked" else None)
                fakes.setdefault(host, []).append(f)
                return f
            hub = S.ShellyHub(path=Path(tmp) / "shelly.json", broadcast=broadcast, evaluate=evaluate,
                              client_factory=factory)
            try:
                c.section("adding")
                try:
                    await hub.add({"host": "locked"})
                    c.check("a passworded Shelly without its password is refused at add", False)
                except ValueError as e:
                    c.check("a passworded Shelly without its password is refused at add", "password" in str(e), str(e))
                dev = await hub.add({"host": "locked", "password": "pw"})
                c.check("added under the Shelly's own id and name",
                        dev["id"] == "shellyplus1pm-a8032ab12345" and dev["name"] == "Hall light" and dev["gen"] == 2, dev)
                c.check("the password lives in the secrets file, not the registry",
                        "pw" not in (Path(tmp) / "shelly.json").read_text()
                        and "password: pw" in Path(L.SECRETS_FILE).read_text())
                try:
                    await hub.add({"host": "locked", "password": "pw"})
                    c.check("the same Shelly can't be added twice", False)
                except ValueError:
                    c.check("the same Shelly can't be added twice", True)
                for bad in ("a b", "http://x/", "-oProxyCommand"):
                    try:
                        await hub.add({"host": bad})
                        c.check(f"host '{bad}' is refused", False)
                    except ValueError:
                        c.check(f"host '{bad}' is refused", True)

                c.section("session")
                d = hub.devices["shellyplus1pm-a8032ab12345"]
                session = hub.make_session(d)
                d.session = session
                await session._setup()
                await session.refresh()
                c.check("a refresh publishes state, controls and capabilities",
                        d.state["power"] == 42.5 and d.controls and "switch" in d.caps and d.online, d.state)
                c.check("…to the browser and the rule engine",
                        pushed[-1][0] == "device_updated" and evals[-1][0] == "shelly::shellyplus1pm-a8032ab12345")
                n = len(evals)
                await session.refresh()
                c.check("an unchanged refresh doesn't re-trigger rules", len(evals) == n)
                await session._on_message({"method": "NotifyStatus", "params": {"switch:0": {"output": False, "apower": 0}}})
                c.check("a pushed change merges into the last status and reaches rules as what changed",
                        d.state["state"] == "OFF" and d.state["voltage"] == 231.4
                        and evals[-1][1] == {"state": "OFF", "on": False, "power": 0}, evals[-1])
                await session._on_message({"method": "NotifyEvent", "params": {"events": [
                    {"component": "input:0", "event": "single_push"}]}})
                await session._on_message({"method": "NotifyEvent", "params": {"events": [
                    {"component": "input:0", "event": "single_push"}]}})
                presses = [e for e in evals if e[1].get("action") == "single"]
                c.check("each button press is its own event, even the same one twice", len(presses) == 2, presses)
                c.check("…and doesn't linger in state as if still pressed", "action" not in d.state)

                c.section("commands")
                client = session.client
                client.calls.clear()
                r = await d.send_command("on")
                c.check("on is Switch.Set on the right switch", r["success"] and client.calls[0] == ("Switch.Set", {"id": 0, "on": True}), client.calls)
                r = await d.send_command("position", 50)
                c.check("a command the device can't do is refused with a reason", not r["success"] and "cover" in r["error"], r)
                client.status = dict(PLUS_2PM_COVER)
                await session.refresh()
                client.calls.clear()
                await d.send_command("position", 30)
                await d.send_command("stop")
                c.check("cover commands go to Cover.*", client.calls[0] == ("Cover.GoToPosition", {"id": 0, "pos": 30})
                        and ("Cover.Stop", {"id": 0}) in client.calls, client.calls)
                r = await d.send_command("position", "lots")
                c.check("a bad value is refused", not r["success"], r)

                g1 = FakeClient("g1", info={"type": "SHSW-25", "mac": "AABBCC", "auth": False},
                                gen1_status=G1_25_ROLLER, settings={"mode": "roller"})
                g1.gen = 1
                s1 = S.ShellySession(hub, d, g1)
                await s1._setup()
                await s1.refresh()
                g1.calls.clear()
                await s1.command("position", 40, None)
                c.check("Gen1 roller commands use /roller/N?go=to_pos",
                        g1.calls[0] == ("/roller/0", {"go": "to_pos", "roller_pos": 40}), g1.calls)

                c.section("going offline")
                await hub.offline(d, "no answer")
                c.check("a lost device is marked unavailable for rules and the list",
                        d.online is False and evals[-1][1].get("available") is False and not d.is_available())
                entry = d.to_device_list_entry()
                c.check("the device list entry carries controls and no credentials",
                        entry["lan_kind"] == "shelly" and entry["controls"] and "pw" not in repr(entry)
                        and entry["type"] != "Router", entry)

                c.section("removing")
                await hub.delete("shellyplus1pm-a8032ab12345")
                c.check("removing drops its credentials", "pw" not in Path(L.SECRETS_FILE).read_text())
            finally:
                await hub.stop()
                L.SECRETS_FILE = saved

    asyncio.run(scenario())
    return c
