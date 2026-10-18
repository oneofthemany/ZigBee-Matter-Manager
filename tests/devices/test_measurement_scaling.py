"""
A polled measurement is scaled exactly as a reported one.

A poll stores each handler's parse_value() result as-is, so a handler that
scaled only in attribute_updated wrote raw centipercent humidity (5150 for
51.5 %) whenever a sensor was polled. Aqara heartbeats on some models also
carry device temperature in centidegrees under tag 0x03.
"""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace as NS

from harness import Checker

from handlers.aqara import AqaraManufacturerCluster
from handlers.sensors import RelativeHumidityHandler


class _Device:
    ieee = "54:ef:44:10:01:65:58:b3"
    model = "lumi.sensor_occupy.agl8"

    def __init__(self):
        self.zigpy_dev = NS(node_desc=NS(logical_type=2, mac_capability_flags=0), model=self.model)
        self.state: dict = {}

    def update_state(self, d, **_):
        self.state.update(d)


class _HumidityCluster:
    cluster_id = 0x0405

    def __init__(self, raw):
        self.endpoint = NS(endpoint_id=1, in_clusters={})
        self.raw = raw

    def add_listener(self, _l):
        pass

    def is_attribute_unsupported(self, _attr):
        return False

    async def read_attributes(self, attrs, **_):
        return ({a: self.raw for a in attrs if a == 0x0000}, {})


class _AqaraCluster:
    cluster_id = 0xFCC0

    def __init__(self):
        self.endpoint = NS(endpoint_id=1, in_clusters={})

    def add_listener(self, _l):
        pass


def _heartbeat(device_temperature: int) -> bytes:
    """An 0xF7 blob carrying only tag 0x03 as int16."""
    return bytes([0x03, 0x29]) + struct.pack("<h", device_temperature)


def run() -> Checker:
    c = Checker("measurement_scaling")

    c.section("humidity: a poll and a report give the same percentage")
    dev = _Device()
    handler = RelativeHumidityHandler(dev, _HumidityCluster(5150))
    polled = asyncio.run(handler.poll())
    c.check("a polled 5150 is stored as 51.5 %", polled.get("humidity") == 51.5, polled)
    c.check("the raw value is kept only under humidity_raw",
            polled.get("humidity_raw") == 5150, polled)
    handler.attribute_updated(0x0000, 5150)
    c.check("a reported 5150 is 51.5 % too", dev.state.get("humidity") == 51.5, dev.state)

    c.section("Aqara heartbeat device temperature")
    for raw, expected in ((2371, 23.71), (23, 23.0)):
        dev = _Device()
        AqaraManufacturerCluster(dev, _AqaraCluster()).attribute_updated(0x00F7, _heartbeat(raw))
        c.check(f"tag 0x03 = {raw} is {expected} °C",
                dev.state.get("device_temperature") == expected, dev.state)
    return c
