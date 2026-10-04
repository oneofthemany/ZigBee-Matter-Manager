"""
The Aurora double socket is bound and left alone; every other meter is not.

Z2M binds DoubleSocket50AU's metering cluster without writing a reporting
config. The socket reports power itself and answers reads for voltage and
current, which it will not report. What matters is what reaches the device,
so the fake cluster records every bind, reporting write and read.
"""

from __future__ import annotations

import asyncio

from harness import Checker

import modules.app_alerts as app_alerts
from handlers import power
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

    c.section("voltage and current come from the poll")
    # Unreportable (0x8C) on this socket, so the poll is their only source.
    c.check("power, voltage and current are polled",
            h.get_pollable_attributes() == {h.ATTR_ACTIVE_POWER: "power_1",
                                            h.ATTR_RMS_VOLTAGE: "voltage_1",
                                            h.ATTR_RMS_CURRENT: "current_1"},
            h.get_pollable_attributes())
    ids = [d["object_id"] for d in h.get_discovery_configs()]
    c.check("all three sensors are published",
            ids == ["power_1", "voltage_1", "current_1"], ids)
    c.check("none is retracted", h.get_retired_discovery_configs() == [],
            h.get_retired_discovery_configs())
    h.attribute_updated(h.ATTR_ACTIVE_POWER, 45)
    c.check("a power report is recorded per socket", dev.state.get("power_1") == 45.0,
            dev.state)
    h.cluster.read_attributes = _reads({h.ATTR_ACTIVE_POWER: 69, h.ATTR_RMS_VOLTAGE: 242,
                                        h.ATTR_RMS_CURRENT: 501})
    polled = asyncio.run(h.poll())
    c.check("a poll reads volts as sent", polled.get("voltage_1") == 242.0, polled)
    c.check("and milliamps as amps", polled.get("current_1") == 0.501, polled)
    c.check("socket 1 carries the device voltage and current",
            (polled.get("voltage"), polled.get("current")) == (242.0, 0.501), polled)
    h.cluster.read_attributes = _reads({h.ATTR_RMS_VOLTAGE: 1012})
    real = app_alerts.raise_alert       # a fault raises an alert: keep it out of ./data
    app_alerts.raise_alert = lambda *a, **k: None
    try:
        polled = asyncio.run(h.poll())
    finally:
        app_alerts.raise_alert = real
    c.check("an impossible voltage is blanked, not shown",
            polled.get("voltage_1") is None, polled)

    c.section("a power report prompts a voltage and current read")
    clock = [1000.0]
    real_clock = power.time.monotonic
    power.time.monotonic = lambda: clock[0]

    async def _reports(model, *steps):
        """Feed power reports, advancing the clock by each step first."""
        hr, clr, devr = _handler(model)
        reads = []

        async def read_attributes(attrs, **_):
            reads.append(list(attrs))
            return [{hr.ATTR_RMS_VOLTAGE: 242, hr.ATTR_RMS_CURRENT: 501}, {}]
        hr.cluster.read_attributes = read_attributes
        for step in steps:
            clock[0] += step
            hr.attribute_updated(hr.ATTR_ACTIVE_POWER, 69)
            await asyncio.sleep(0.01)
        return reads, devr

    try:
        reads, devr = asyncio.run(_reports("DoubleSocket50AU", 0))
        c.check("voltage and current are read, not power again",
                reads == [[h.ATTR_RMS_VOLTAGE, h.ATTR_RMS_CURRENT]], reads)
        c.check("and land in state",
                (devr.state.get("voltage_1"), devr.state.get("current_1")) == (242.0, 0.501),
                devr.state)
        reads, _ = asyncio.run(_reports("DoubleSocket50AU", 0, 6, 6, 6))
        c.check("reports six seconds apart share one read", len(reads) == 1, reads)
        reads, _ = asyncio.run(_reports("DoubleSocket50AU", 0, 29, 1))
        c.check("the next read comes at 30 seconds", len(reads) == 2, reads)
        reads, _ = asyncio.run(_reports("SmartPlug51AU", 0))
        c.check("a meter that reports them itself is not read", reads == [], reads)
    finally:
        power.time.monotonic = real_clock

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
    h.attribute_updated(h.ATTR_RMS_VOLTAGE, 240)
    h.attribute_updated(h.ATTR_RMS_CURRENT, 250)
    c.check("and voltage", dev.state.get("voltage") == 240.0, dev.state)
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
    h.attribute_updated(h.ATTR_RMS_VOLTAGE, 240)
    c.check("and recorded", dev.state.get("voltage_1") == 240.0, dev.state)

    c.section("a meter without voltage or current (Aqara aeu002) gets neither")
    # zigpy records UNSUPPORTED_ATTRIBUTE answers; the fake answers as it would.
    missing = {"rms_voltage", "rms_current", 0x0505, 0x0508}
    h, cl, dev = _handler("lumi.plug.aeu002", ep=3)
    cl.is_attribute_unsupported = lambda a: a in missing
    asyncio.run(h.configure())
    reads = [s[1] for s in cl.sent if s[0] == "read"]
    c.check("the measurements are read before reporting is set up",
            reads and "rms_voltage" in reads[0], cl.sent)
    configured = [name for s in cl.sent if s[0] == "configure_reporting" for name in s[1]]
    c.check("reporting is configured for active power only",
            configured == ["active_power"], cl.sent)
    c.check("only active power is polled",
            h.get_pollable_attributes() == {h.ATTR_ACTIVE_POWER: "power_3"},
            h.get_pollable_attributes())
    ids = [d["object_id"] for d in h.get_discovery_configs()]
    c.check("HA gets a power sensor only", ids == ["power_3"], ids)
    retired = sorted(d["object_id"] for d in h.get_retired_discovery_configs())
    c.check("the old voltage and current sensors are retracted",
            retired == ["current_3", "voltage_3"], retired)
    return c


if __name__ == "__main__":
    run()
