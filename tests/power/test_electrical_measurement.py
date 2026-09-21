"""
The Aurora double socket is bound and left alone; every other meter is not.

Z2M binds DoubleSocket50AU's metering cluster without writing a reporting
config and exposes power only. What matters is what reaches the device, so
the fake cluster records every bind, reporting write and read.
"""

from __future__ import annotations

import asyncio

from harness import Checker

from handlers.power import ElectricalMeasurementHandler


class _Endpoint:
    def __init__(self, ep_id: int):
        self.endpoint_id = ep_id


class _Cluster:
    cluster_id = 0x0B04

    def __init__(self, ep_id: int = 1):
        self.endpoint = _Endpoint(ep_id)
        self.sent: list = []

    def add_listener(self, _l):
        pass

    async def bind(self):
        self.sent.append(("bind",))
        return [0]

    async def configure_reporting_multiple(self, records):
        self.sent.append(("configure_reporting", sorted(records)))
        return [[]]

    async def configure_reporting(self, *a, **k):
        self.sent.append(("configure_reporting", a[0] if a else None))

    async def read_attributes(self, attrs, **_):
        self.sent.append(("read", list(attrs)))
        return [{}, {}]


class _Device:
    def __init__(self, model: str):
        self.ieee = "00:15:8d:00:02:56:f8:bf"
        self.model = model
        self.state: dict = {}

    def update_state(self, d):
        self.state.update(d)


def _reads(values: dict):
    async def read_attributes(attrs, **_):
        return [{a: values[a] for a in attrs if a in values},
                {a: 134 for a in attrs if a not in values}]
    return read_attributes


def _handler(model: str, ep: int = 1):
    dev = _Device(model)
    cl = _Cluster(ep)
    return ElectricalMeasurementHandler(dev, cl), cl, dev


def run() -> Checker:
    c = Checker("electrical_measurement")

    c.section("the Aurora double socket is bound and nothing else is written")
    h, cl, dev = _handler("DoubleSocket50AU")
    asyncio.run(h.configure())
    c.check("the cluster is bound", ("bind",) in cl.sent, cl.sent)
    c.check("no reporting config reaches the socket",
            not any(s[0] == "configure_reporting" for s in cl.sent), cl.sent)
    c.check("no scaling read is attempted",
            not any(s[0] == "read" for s in cl.sent), cl.sent)
    c.check("the class default is untouched for other devices",
            ElectricalMeasurementHandler.REPORT_CONFIG != [])

    c.section("it shows power only")
    c.check("only active power is polled",
            h.get_pollable_attributes() == {h.ATTR_ACTIVE_POWER: "power_1"},
            h.get_pollable_attributes())
    ids = [d["object_id"] for d in h.get_discovery_configs()]
    c.check("only a power sensor is published", ids == ["power_1"], ids)
    h.attribute_updated(h.ATTR_ACTIVE_POWER, 45)
    h.attribute_updated(h.ATTR_RMS_VOLTAGE, 1012)
    h.attribute_updated(h.ATTR_RMS_CURRENT, 7072)
    c.check("a power report is recorded per socket", dev.state.get("power_1") == 45.0,
            dev.state)
    c.check("voltage and current reports are ignored",
            "voltage_1" not in dev.state and "current_1" not in dev.state, dev.state)
    h2, _, dev2 = _handler("DoubleSocket50AU", ep=2)
    h2.attribute_updated(h2.ATTR_ACTIVE_POWER, 12)
    c.check("the right socket reports as power_2", dev2.state.get("power_2") == 12.0,
            dev2.state)

    c.section("the wattage the UI reads is still published")
    # modules/frames.py:83 keys the readout on "power"; 784197c dropped it and
    # the Frames card has read "not reported yet" ever since.
    h, cl, dev = _handler("SingleSocket50AU")
    h.attribute_updated(h.ATTR_ACTIVE_POWER, 45)
    c.check("a single meter publishes power", dev.state.get("power") == 45.0,
            dev.state)
    h.attribute_updated(h.ATTR_RMS_VOLTAGE, 2400)
    h.attribute_updated(h.ATTR_RMS_CURRENT, 250)
    c.check("and voltage", dev.state.get("voltage") == 2400.0, dev.state)
    c.check("and current", dev.state.get("current") == 0.25, dev.state)

    left, cl_l, dev_m = _handler("DoubleSocket50AU", ep=1)
    right = ElectricalMeasurementHandler(dev_m, _Cluster(2))
    left.attribute_updated(left.ATTR_ACTIVE_POWER, 45)
    c.check("one socket reporting gives the device total", dev_m.state["power"] == 45.0,
            dev_m.state)
    right.attribute_updated(right.ATTR_ACTIVE_POWER, 30)
    c.check("both sockets sum to the device total", dev_m.state["power"] == 75.0,
            dev_m.state)
    left.attribute_updated(left.ATTR_ACTIVE_POWER, 10)
    c.check("a change on one socket re-totals", dev_m.state["power"] == 40.0,
            dev_m.state)
    c.check("the per-socket figures stay",
            (dev_m.state["power_1"], dev_m.state["power_2"]) == (10.0, 30.0),
            dev_m.state)

    c.section("polled values are aliased too")
    h, cl, dev = _handler("DoubleSocket50AU")
    h.cluster.read_attributes = _reads({h.ATTR_ACTIVE_POWER: 45})
    polled = asyncio.run(h.poll())
    c.check("a poll publishes power", polled.get("power") == 45.0, polled)

    c.section("a model learnt after attach is still honoured")
    h, cl, dev = _handler("")
    dev.model = "DoubleSocket50AU"
    asyncio.run(h.configure())
    c.check("bind only once the model is known",
            not any(s[0] == "configure_reporting" for s in cl.sent), cl.sent)

    c.section("every other meter is unchanged")
    h, cl, dev = _handler("SmartPlug51AU")
    asyncio.run(h.configure())
    c.check("reporting is still configured",
            any(s[0] == "configure_reporting" for s in cl.sent), cl.sent)
    c.check("scaling is still read", any(s[0] == "read" for s in cl.sent), cl.sent)
    c.check("voltage and current are still polled",
            len(h.get_pollable_attributes()) == 3, h.get_pollable_attributes())
    c.check("and published", len(h.get_discovery_configs()) == 3)
    h.attribute_updated(h.ATTR_RMS_VOLTAGE, 2400)
    c.check("and recorded", dev.state.get("voltage_1") == 2400.0, dev.state)
    return c


if __name__ == "__main__":
    run()
