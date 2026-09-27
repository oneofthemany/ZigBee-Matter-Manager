"""
The coordinator is the radio, not a device; and a device's quirk is read from
the live zigpy object.

The coordinator carries On/Off and IAS clusters of its own, so it was handled
like a device: a switch in the Control tab, IAS keys in its state, and an HA
announcement. The API's quirk field read an attribute zigpy no longer sets and
reported "NoneType" even for quirked devices.
"""

from __future__ import annotations

import asyncio

from harness import Checker

from zigpy.quirks import CustomDevice
from zigpy.quirks.v2 import CustomDeviceV2

from device.core import quirk_name_of
from device.handlers import DeviceHandlerManagerMixin
from modules.device_capabilities import DeviceCapabilities
from mqtt import MQTTService


class _Stub:
    def __init__(self, cid):
        self.cluster_id = cid
        self._listeners = {}

    def add_listener(self, _l):
        pass


class _Ep:
    def __init__(self, ep_id, ins, outs=()):
        self.endpoint_id = ep_id
        self.in_clusters = {c: _Stub(c) for c in ins}
        self.out_clusters = {c: _Stub(c) for c in outs}
        for cl in (*self.in_clusters.values(), *self.out_clusters.values()):
            cl.endpoint = self
        self.profile_id = 0x0104
        self.device_type = 0x0400


class _Zdev:
    ieee = "e4:56:ac:ff:fe:4c:d0:77"
    manufacturer = "Silicon Labs"
    model = "EZSP"

    def __init__(self):
        self.endpoints = {0: None, 1: _Ep(1, [0x0000, 0x0006, 0x000A, 0x0019, 0x0501],
                                          [0x0001, 0x0020, 0x0500, 0x0502])}


class _Device(DeviceHandlerManagerMixin):
    def __init__(self, coordinator: bool):
        self.ieee = _Zdev.ieee
        self.zigpy_dev = _Zdev()
        self.handlers = {}
        self.state = {}
        self.is_coordinator = coordinator

    def get_binding_preferences(self):
        return {}

    def _is_battery_powered(self):
        return False


class _Client:
    """Replays retained topics into the service's collector on subscribe."""

    def __init__(self, svc, retained):
        self.svc, self.retained, self.sent = svc, retained, []

    async def subscribe(self, pattern, qos=0):
        self.sent.append(("sub", pattern))
        for t in self.retained:
            if self.svc._discovery_collector is not None:
                self.svc._discovery_collector.add(t)

    async def unsubscribe(self, pattern):
        self.sent.append(("unsub", pattern))

    async def publish(self, topic, payload, retain=False, qos=0):
        self.sent.append(("pub", topic, payload, retain))


def run() -> Checker:
    c = Checker("coordinator_quirk")

    c.section("the quirk is read from the live zigpy object")

    class LumiLightAcn003(CustomDevice):
        pass

    v1 = object.__new__(LumiLightAcn003)
    c.check("a v1 quirk is named by its class", quirk_name_of(v1) == "LumiLightAcn003",
            quirk_name_of(v1))
    v2 = object.__new__(CustomDeviceV2)
    v2.quirk_metadata = type("M", (), {"quirk_file": "/q/zhaquirks/tuya/ts0601_cover.py",
                                       "quirk_file_line": 42})()
    c.check("a v2 quirk is named by its source file",
            quirk_name_of(v2) == "ts0601_cover:42", quirk_name_of(v2))
    c.check("an unquirked device has none", quirk_name_of(_Zdev()) is None)

    c.section("the coordinator is not handled as a device")
    coord = _Device(coordinator=True)
    coord._identify_handlers()
    c.check("it gets no cluster handlers", coord.handlers == {}, coord.handlers)
    caps = DeviceCapabilities(coord).get_capabilities()
    c.check("it gets no capabilities (no switch, no IAS, no motion)", not caps, caps)
    other = _Device(coordinator=False)
    other._identify_handlers()
    c.check("the same clusters on a device still get handlers", bool(other.handlers))

    c.section("its old HA entities are retracted")
    svc = MQTTService.__new__(MQTTService)
    svc.ha_discovery, svc._connected, svc._discovery_collector = True, True, None
    node = _Zdev.ieee.replace(":", "")
    retained = [f"homeassistant/switch/{node}/switch_1/config",
                f"homeassistant/binary_sensor/{node}/contact_1/config"]
    svc.client = _Client(svc, retained)
    n = asyncio.run(svc.purge_discovery(_Zdev.ieee, wait=0))
    cleared = sorted(s[1] for s in svc.client.sent if s[0] == "pub" and s[2] == "" and s[3])
    c.check("every retained config under its node is cleared", cleared == sorted(retained),
            svc.client.sent)
    c.check("it reports how many", n == 2, n)
    order = [s[0] for s in svc.client.sent]
    c.check("it unsubscribes before clearing, so its own clears are not collected",
            order.index("unsub") < order.index("pub"), order)
    c.check("the collector is off afterwards", svc._discovery_collector is None)
    return c


if __name__ == "__main__":
    run()
