"""
Button EPs a device still reports on a timer are rewritten once per start;
EPs already right, silent devices and sleepy ones are left alone.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from harness import Checker

from handlers.aqara import MultistateInputHandler
from modules import reporting_heal


class _Cluster:
    cluster_id = 0x0012

    def __init__(self, ep_id, held=None):
        self.endpoint = NS(endpoint_id=ep_id)
        self.held = held            # max interval the device reports it holds; None: no answer
        self.sent = []

    def add_listener(self, _l):
        pass

    async def general_command(self, command, records, **_):
        self.sent.append(("read_config", [int(r.attrid) for r in records]))
        if self.held is None:
            raise asyncio.TimeoutError
        cfg = NS(attrid=records[0].attrid, max_interval=self.held)
        return NS(attribute_configs=[NS(status=0, config=cfg)])

    async def configure_reporting(self, attr, lo, hi, change):
        self.sent.append(("configure", attr, lo, hi, change))
        return [NS(status=0)]


def _device(helds, mains=True):
    nd = NS(is_mains_powered=mains)
    dev = NS(ieee="54:ef:44:10:01:5a:14:eb", zigpy_dev=NS(node_desc=nd), handlers={},
             is_coordinator=False, state={})
    clusters = {}
    for ep, held in helds.items():
        cl = _Cluster(ep, held)
        clusters[ep] = cl
        dev.handlers[(ep, 0x0012)] = MultistateInputHandler(dev, cl)
    return dev, clusters


def run() -> Checker:
    c = Checker("reporting_heal")

    dev, cl = _device({1: 3600, 2: 0, 3: None})
    n = asyncio.run(reporting_heal.heal_device(dev))
    c.check("an EP still reporting every hour is rewritten to no periodic reports",
            ("configure", "present_value", 0, 0, 1) in cl[1].sent, cl[1].sent)
    c.check("the device was asked first, for the button's value",
            cl[1].sent[0] == ("read_config", [0x0055]), cl[1].sent)
    c.check("an EP already right is left alone", cl[2].sent == [("read_config", [0x0055])])
    c.check("an EP that does not answer is left alone",
            not any(s[0] == "configure" for s in cl[3].sent))
    c.check("it reports how many it rewrote", n == 1, n)

    dev, cl = _device({1: 3600}, mains=False)
    c.check("a sleepy device is never asked",
            asyncio.run(reporting_heal.heal_device(dev)) == 0 and cl[1].sent == [])

    dev, cl = _device({1: 3600})
    c.check("the start-up pass covers every device",
            asyncio.run(reporting_heal.heal([dev], delay=0)) == 1)
    return c


if __name__ == "__main__":
    run()
