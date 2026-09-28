"""
Energy (Metering 0x0702) is scaled by the device's multiplier/divisor.

They are static attributes a device rarely reports, and the handler only took
them from reports, so the divisor stayed 1: the Aqara outlet's 34 (Wh, divisor
1000) read as 34 kWh, and polled totals were not scaled at all.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from harness import Checker

import modules.device_profiles as device_profiles
from handlers.power import MeteringHandler

IEEE = "54:ef:44:10:01:5a:14:eb"


class _Store:
    def __init__(self, profile=None):
        self.profile = profile

    def get_profile_for_device(self, **_):
        return self.profile


class _Cluster:
    cluster_id = 0x0702

    def __init__(self, ep_id=1, cache=None, answers=None):
        self.endpoint = NS(endpoint_id=ep_id)
        self.cache, self.answers = cache or {}, answers or {}

    def add_listener(self, _l):
        pass

    def get(self, attr):
        return self.cache.get(attr)

    async def bind(self):
        return [0]

    async def configure_reporting_multiple(self, records):
        return [[]]

    async def read_attributes(self, attrs, **_):
        return [{a: self.answers[a] for a in attrs if a in self.answers}, {}]


def _handler(**kw):
    dev = NS(ieee=IEEE, zigpy_dev=NS(model="lumi.plug.aeu002", manufacturer="Aqara"),
             state={}, handlers={})
    dev.update_state = lambda d, **_: dev.state.update(d)
    return MeteringHandler(dev, _Cluster(**kw)), dev


def run() -> Checker:
    c = Checker("energy")
    device_profiles._store = _Store()
    try:
        c.section("scaling survives a restart")
        h, dev = _handler(cache={"multiplier": 1, "divisor": 1000})
        h.attribute_updated(h.ATTR_CURRENT_SUMMATION_DELIVERED, 34)
        c.check("34 Wh reads as 0.034 kWh, from zigpy's cached divisor",
                dev.state.get("energy_1") == 0.034 and dev.state.get("energy") == 0.034, dev.state)

        c.section("with nothing cached")
        h, dev = _handler(answers={"multiplier": 1, "divisor": 1000})
        c.check("the default is 1/1 until the device is asked", h._divisor == 1)
        asyncio.run(h.configure())
        c.check("configure reads the divisor", h._divisor == 1000, h._divisor)

        c.section("a ZMM entry's scaling wins")
        device_profiles._store = _Store({"id": "lumi.plug.aeu002", "meta": {"source": "zmm"},
                                         "zmm": {"measurements": {"energy": {"divisor": 1000}}}})
        h, dev = _handler(cache={"divisor": 1})
        h.attribute_updated(h.ATTR_CURRENT_SUMMATION_DELIVERED, 34)
        c.check("the entry's /1000 applies over a wrong cached value", dev.state.get("energy_1") == 0.034)
        device_profiles._store = _Store()

        c.section("polled totals are scaled too")
        h, dev = _handler(cache={"divisor": 1000})
        c.check("polled under energy_<ep>",
                h.get_pollable_attributes()[h.ATTR_CURRENT_SUMMATION_DELIVERED] == "energy_1")
        c.check("and scaled", h.parse_value(h.ATTR_CURRENT_SUMMATION_DELIVERED, 34) == 0.034)
    finally:
        device_profiles._store = None
    return c


if __name__ == "__main__":
    run()
