"""
Reporting configs are sent in a form the device accepts.

The Aqara H2 (lumi.plug.aeu002) declares active power uint16 where the ZCL has
int16 and refuses the spec type, and zigpy names 0x0702/0x0000
`current_summ_delivered`. The clusters are zigpy's own; only the radio is faked.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from harness import Checker

import zigpy.device
import zigpy.types as t
from zigpy.zcl import foundation

from handlers.power import ElectricalMeasurementHandler, MeteringHandler

IEEE = "54:ef:44:10:01:5a:14:eb"
OK = [[foundation.ConfigureReportingResponseRecord(status=foundation.Status.SUCCESS)]]


def _cluster(cluster_id: int, refuse: frozenset = frozenset()):
    """A real zigpy cluster whose reporting frames are recorded, not sent."""
    zdev = zigpy.device.Device(MagicMock(), t.EUI64.convert(IEEE), 0x60D8)
    cluster = zdev.add_endpoint(1).add_input_cluster(cluster_id)
    sent = []

    async def _configure_reporting(configs, **_):
        sent.extend((c.attrid, int(c.datatype), c.min_interval, c.max_interval,
                     c.reportable_change) for c in configs)
        bad = [foundation.ConfigureReportingResponseRecord(
                   status=foundation.Status.INVALID_DATA_TYPE,
                   direction=foundation.ReportingDirection.SendReports, attrid=c.attrid)
               for c in configs if int(c.datatype) in refuse]
        return [bad] if bad else OK

    async def bind():
        return [0]

    async def read_attributes(attrs, **_):
        return [{}, {}]

    cluster._configure_reporting = _configure_reporting
    cluster.bind, cluster.read_attributes = bind, read_attributes
    return cluster, sent


def _device():
    return NS(ieee=IEEE, model="lumi.plug.aeu002", state={}, handlers={},
              update_state=lambda d, **_: None)


def run() -> Checker:
    c = Checker("reporting_types")
    INT16, UINT16 = int(foundation.DataTypeId.int16), int(foundation.DataTypeId.uint16)

    c.section("a device that refuses the spec type is asked again in its own")
    cluster, sent = _cluster(0x0B04, refuse=frozenset({INT16}))
    h = ElectricalMeasurementHandler(_device(), cluster)
    h.REPORT_CONFIG = [("active_power", 10, 60, 10)]
    asyncio.run(h.configure())
    power = [s for s in sent if s[0] == 0x050B]
    c.check("active power goes out as int16 first", power[:1] == [(0x050B, INT16, 10, 60, 10)],
            power)
    c.check("then as uint16, same intervals", power[1:] == [(0x050B, UINT16, 10, 60, 10)],
            power)

    c.section("a device that accepts the spec type is asked once")
    cluster, sent = _cluster(0x0B04)
    h = ElectricalMeasurementHandler(_device(), cluster)
    h.REPORT_CONFIG = [("active_power", 10, 60, 10)]
    asyncio.run(h.configure())
    c.check("no retry", [s for s in sent if s[0] == 0x050B] == [(0x050B, INT16, 10, 60, 10)],
            sent)

    c.section("energy reporting reaches the device")
    cluster, sent = _cluster(0x0702)
    h = MeteringHandler(_device(), cluster)
    asyncio.run(h.configure())
    c.check("the summation attribute is configured",
            any(s[0] == 0x0000 and s[2:4] == (300, 3600) for s in sent), sent)
    for handler, cluster_id in ((MeteringHandler, 0x0702), (ElectricalMeasurementHandler, 0x0B04)):
        cluster, _ = _cluster(cluster_id)
        for name, *_ in handler.REPORT_CONFIG:
            try:
                cluster.find_attribute(name)
                known = True
            except KeyError:
                known = False
            c.check(f"zigpy knows {name}", known)
    return c
