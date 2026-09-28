"""
The Aqara handler uses only the 0xFCC0 attributes an endpoint lists.

Each Aqara EP carries its own attribute set (the aeu002 probe: ~45 on EP1,
none of 0x0200/0x0201), and zigpy's generic 0xFCC0 knows none of them, so the
poll raised KeyError(513) before sending anything. The 0xF7 struct's tag
layout also differs per model: aeu002 puts a float where the global map
expects the indicator mode.
"""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace as NS

from harness import Checker

from pathlib import Path

import modules.device_profiles as device_profiles
from handlers.aqara import AqaraManufacturerCluster, MultistateInputHandler
from modules.device_profiles import ProfileStore

REPO = Path(__file__).resolve().parents[2]
# The 0xF7 blob lumi.plug.aeu002 reported (observed in the live zigbee cache)
AEU002_F7 = bytes.fromhex(
    "032800052101000921000b0a2100000d23060000006410006510016610006820006920006a20006b20"
    "0095394c37093d9839000000009739f56a6a43")

# EP1 of lumi.plug.aeu002, from data/probes/54ef4410015a14eb_20260927_131518.json
AEU002_EP1 = {0x0001, 0x0002, 0x0003, 0x0005, 0x0006, 0x0007, 0x0008, 0x0009, 0x000B,
              0x000C, 0x000E, 0x0012, 0x0013, 0x0014, 0x0016, 0x0020, 0x0021, 0x0080,
              0x0081, 0x00DA, 0x00DD, 0x00DE, 0x00DF, 0x00E5, 0x00E6, 0x00E8, 0x00EE,
              0x00F3, 0x00F5, 0x00F6, 0x00F7, 0x00FA, 0x00FC, 0x00FE, 0x00FF, 0x0700,
              0x0701, 0x0800, 0x0801, 0x0802, 0xFFF2}


class _Cluster:
    cluster_id = 0xFCC0

    def __init__(self, listed, values=None, answers_discovery=True):
        self.endpoint = NS(endpoint_id=1, in_clusters={})
        self.listed = sorted(listed)
        self.values = values or {}
        self.answers_discovery = answers_discovery
        self.sent: list = []

    def add_listener(self, _l):
        pass

    async def discover_attributes(self, start, count, manufacturer=None):
        self.sent.append(("discover", start, manufacturer))
        if not self.answers_discovery:
            raise asyncio.TimeoutError
        page = [a for a in self.listed if a >= start][:count]
        done = len([a for a in self.listed if a >= start]) <= count
        return NS(attribute_info=[NS(attrid=a, datatype=0x20) for a in page], discovery_complete=done)

    async def read_attributes_raw(self, attrs, manufacturer=None):
        self.sent.append(("read", list(attrs), manufacturer))
        return NS(status_records=[
            NS(attrid=a, status=0, value=NS(value=self.values[a])) if a in self.values
            else NS(attrid=a, status=0x86, value=None) for a in attrs])

    async def write_attributes_raw(self, attrs, manufacturer=None):
        self.sent.append(("write", [a.attrid for a in attrs], manufacturer))
        return [[NS(status=0)]]


class _Device:
    ieee = "54:ef:44:10:01:5a:14:eb"
    model = "lumi.plug.aeu002"

    def __init__(self, sleepy=False):
        # logical_type 2 + rx-off-when-idle = sleepy end device
        nd = NS(logical_type=2 if sleepy else 1, mac_capability_flags=0 if sleepy else 0x08)
        self.zigpy_dev = NS(node_desc=nd, model=self.model)
        self.state: dict = {}
        self.on_off = True     # the handler keys switch attributes off this

    def update_state(self, d, **_):
        self.state.update(d)


def _handler(listed=AEU002_EP1, values=None, sleepy=False, answers=True):
    dev = _Device(sleepy)
    cl = _Cluster(listed, values, answers)
    return AqaraManufacturerCluster(dev, cl), cl, dev


def _f7(*items) -> bytes:
    out = b""
    for tag, kind, val in items:
        out += bytes([tag]) + (b"\x39" + struct.pack("<f", val) if kind == "f"
                               else b"\x20" + bytes([val]))
    return out


def run() -> Checker:
    c = Checker("aqara_attributes")

    c.section("an EP that does not list a setting is never asked for it")
    h, cl, dev = _handler()
    asyncio.run(h.poll())
    c.check("discovery runs with the Aqara manufacturer code",
            any(s[0] == "discover" and s[2] == 0x115F for s in cl.sent), cl.sent)
    c.check("no read is sent for unlisted attributes (0x0201, 0x0200, ...)",
            not any(s[0] == "read" for s in cl.sent), cl.sent)
    names = [o["name"] for o in h.get_configuration_options()]
    c.check("power outage memory is not offered", "power_outage_memory" not in names, names)
    c.check("nothing unlisted is polled", h.get_pollable_attributes() == {},
            h.get_pollable_attributes())
    ok = asyncio.run(h.write_attribute(0x0201, 1))
    c.check("a write to an unlisted attribute is refused without traffic",
            ok is False and not any(s[0] == "write" for s in cl.sent), cl.sent)

    c.section("a listed setting is read by id, past zigpy's schema")
    h, cl, dev = _handler(listed=AEU002_EP1 | {0x0201}, values={0x0201: 1})
    asyncio.run(h.poll())
    reads = [s for s in cl.sent if s[0] == "read"]
    c.check("only the listed attribute is read, with the manufacturer code",
            reads == [("read", [0x0201], 0x115F)], cl.sent)
    c.check("and its value lands in state", dev.state.get("power_outage_memory") is True,
            dev.state)
    c.check("and it is offered", "power_outage_memory" in
            [o["name"] for o in h.get_configuration_options()])

    c.section("devices whose list we cannot learn keep the old behaviour")
    for label, kw in (("a sleepy device is not asked", {"sleepy": True}),
                      ("a device that ignores discovery", {"answers": False})):
        h, cl, dev = _handler(**kw)
        asyncio.run(h._learn_supported())
        c.check(f"{label}: every setting is still offered",
                "power_outage_memory" in [o["name"] for o in h.get_configuration_options()])
    h, cl, _ = _handler(sleepy=True)
    asyncio.run(h._learn_supported())
    c.check("a sleepy device gets no discovery traffic",
            not any(s[0] == "discover" for s in cl.sent), cl.sent)

    c.section("the 0xF7 tag map does not mislabel another model's floats")
    h, _, dev = _handler()
    h.attribute_updated(0x00F7, _f7((0x9A, "f", 0.0), (0x9B, "f", 9.398), (0x64, "i", 1)))
    c.check("a float in the indicator-mode slot is not stored as indicator_mode",
            "indicator_mode" not in dev.state, dev.state)
    c.check("an integer state tag still decodes", dev.state.get("switch_state") is True,
            dev.state)
    h, _, dev = _handler()
    h.attribute_updated(0x00F7, _f7((0x9B, "i", 1)))
    c.check("an integer indicator mode still decodes", dev.state.get("indicator_mode") == 1,
            dev.state)
    c.section("a button's value is never re-reported on a timer")
    c.check("no periodic reports (max interval 0): a re-report would read as a press",
            MultistateInputHandler.REPORT_CONFIG == [("present_value", 0, 0, 1)])

    c.section("the blob, as lumi.plug.aeu002 sends it")
    import tempfile
    d = tempfile.mkdtemp()
    device_profiles._store = ProfileStore(user_dir=d + "/u", bundled_dir=d + "/b",
                                          ieee_overrides_file=d + "/i.json",
                                          zmm_dir=str(REPO / "zmm_quirks"))
    try:
        h, _, dev = _handler()
        dev.zigpy_dev.manufacturer = "Aqara"
        em = NS(_measured=lambda: [0x050B])             # power only, like its 0x0B04
        dev.handlers = {(1, 0x0B04): em, (1, 0x0702): object(), (1, 0xFCC0): h}
        h.attribute_updated(0x00F7, AEU002_F7)
        c.check("tag 0x97 is the mains voltage the entry says it is",
                round(dev.state.get("voltage", 0), 1) == 234.4, dev.state)
        c.check("tags the entry drops write nothing (no 281.6 Hz, no 0.1 V)",
                "frequency" not in dev.state and "device_temperature" not in dev.state, dev.state)
        c.check("energy is left to the metering cluster", "energy" not in dev.state
                and "power_consumption" not in dev.state, dev.state)
        c.check("tags the map has right still decode",
                dev.state.get("switch_state") is False and dev.state.get("switch_state_ep2") is True)

        device_profiles._store = ProfileStore(user_dir=d + "/u2", bundled_dir=d + "/b2",
                                              ieee_overrides_file=d + "/i2.json", zmm_dir=d + "/z2")
        h, _, dev = _handler()
        dev.handlers = {}
        h.attribute_updated(0x00F7, AEU002_F7)
        c.check("with no entry and no standard clusters, the global map still applies",
                "power_consumption" in dev.state and "current_97" in dev.state, dev.state)
    finally:
        device_profiles._store = None
    return c


if __name__ == "__main__":
    run()
