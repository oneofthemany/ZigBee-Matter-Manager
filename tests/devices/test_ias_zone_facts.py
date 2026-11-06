"""
An IAS zone is typed only by what the device says (handlers/security.py,
modules/device_capabilities.py): its zone_type from a read, a report, its
Enroll Request or the cluster cache. A zone that hasn't said is not a motion
sensor; its alarm is published as itself and the type is read while it's awake.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from harness import Checker

from handlers.security import IASZoneHandler
from modules.device_capabilities import DeviceCapabilities

IEEE = "00:00:00:00:00:0a:5e:01"


class _Zone:
    cluster_id = 0x0500

    def __init__(self, zone_type=None, answers=None):
        self.endpoint = NS(endpoint_id=1)
        self._cache = {} if zone_type is None else {0x0001: zone_type}
        self.answers, self.reads = answers, 0

    def add_listener(self, _l):
        pass

    def get(self, key, default=None):
        return self._cache.get(0x0001 if key in (0x0001, "zone_type") else key, default)

    async def read_attributes(self, attrs):
        self.reads += 1
        if self.answers is None:
            raise TimeoutError("asleep")
        self._cache[0x0001] = self.answers
        return ({0x0001: self.answers}, {})


class _Caps:
    def __init__(self):
        self.detected = 0

    def _detect_capabilities(self):
        self.detected += 1


def _device():
    dev = NS(ieee=IEEE, state={}, capabilities=_Caps())
    dev.update_state = dev.state.update
    return dev


class _Cl:
    def __init__(self, cid, zone_type=None):
        self.cluster_id = cid
        self._zt = zone_type

    def get(self, key, default=None):
        return self._zt if key == "zone_type" else default


def _caps(zone_type=None):
    ep = NS(endpoint_id=1, profile_id=0x0104, device_type=0x0402,
            in_clusters={c: _Cl(c, zone_type if c == 0x0500 else None)
                         for c in (0x0000, 0x0001, 0x0003, 0x0500)},
            out_clusters={})
    zdev = NS(model="zone.test", manufacturer="Acme", endpoints={0: None, 1: ep},
              node_desc=NS(is_mains_powered=False))
    return DeviceCapabilities(NS(ieee=IEEE, zigpy_dev=zdev, state={}, handlers={},
                                 is_coordinator=False)).get_capabilities()


def run() -> Checker:
    c = Checker("ias_zone_facts")

    c.section("capabilities of a zone that hasn't given its type")
    caps = _caps()
    c.check("is a bare IAS zone, not a motion sensor",
            "ias_zone" in caps and "motion_sensor" not in caps, caps)
    caps = _caps(0x0015)
    c.check("a zone that says contact is a contact sensor",
            "contact_sensor" in caps and "motion_sensor" not in caps, caps)

    c.section("an alarm before the type is known")

    async def unknown():
        dev, zone = _device(), _Zone(answers=0x0015)
        h = IASZoneHandler(dev, zone)
        h.cluster_command(1, 0x00, (0x0001,))
        c.check("is published as a neutral alarm",
                dev.state.get("alarm") is True
                and not {"motion", "occupancy", "presence", "contact"} & set(dev.state), dev.state)
        await asyncio.sleep(0)
        c.check("and the zone type is read while the device is awake",
                zone.reads == 1 and h._zone_type == 0x0015 and dev.capabilities.detected == 1,
                (zone.reads, h._zone_type, dev.capabilities.detected))
        h.cluster_command(2, 0x00, (0x0001,))
        c.check("after which Alarm1 means open", dev.state.get("is_open") is True
                and dev.state.get("contact") is False, dev.state)

        dev, zone = _device(), _Zone(answers=None)
        h = IASZoneHandler(dev, zone)
        h.cluster_command(1, 0x00, (0x0001,))
        await asyncio.sleep(0)
        c.check("a read that fails leaves it unknown, and the next alarm tries again",
                h._zone_type is None and h._zone_type_read is None, h._zone_type)
    asyncio.run(unknown())

    c.section("the device's other statements of its type")

    async def enroll():
        dev, zone = _device(), _Zone()
        h = IASZoneHandler(dev, zone)
        h._send_enroll_response = _noop
        h.cluster_command(1, 0x01, (0x0015, 0x115F))
        await asyncio.sleep(0)
        c.check("an Enroll Request's zone type is taken, and capabilities re-detected",
                h._zone_type == 0x0015 and dev.state.get("zone_type") == "contact_switch"
                and dev.capabilities.detected == 1, (h._zone_type, dev.state))
        h.attribute_updated(0x0001, 0x0015)
        c.check("the same type again re-detects nothing", dev.capabilities.detected == 1,
                dev.capabilities.detected)
    asyncio.run(enroll())

    dev, zone = _device(), _Zone(zone_type=0x0015)
    h = IASZoneHandler(dev, zone)
    h.cluster_command(1, 0x00, (0x0000,))
    c.check("after a restart the cached type is used: closed, no motion",
            dev.state.get("contact") is True and "motion" not in dev.state and zone.reads == 0,
            dev.state)

    dev, zone = _device(), _Zone(zone_type=0x002D)
    h = IASZoneHandler(dev, zone)
    h.cluster_command(1, 0x00, (0x0001,))
    c.check("0x002D is vibration, per the ZCL", dev.state.get("vibration") is True, dev.state)
    return c


async def _noop():
    pass


if __name__ == "__main__":
    run()
