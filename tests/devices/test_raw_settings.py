"""
Every maker's attribute a device accepts writes to is a control from the day
it is found, under its id until something names it.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

from harness import Checker

import modules.zigbee_cache as zigbee_cache
from modules import zmm_settings

IEEE = "54:ef:44:10:01:65:58:b3"


def fact(attr, type_, acl, value, mfr="@0x115F", cluster="0xFCC0"):
    return {"endpoint_id": 1, "subject": f"attr:{cluster}/0x{attr:04X}{mfr}", "source": "answered",
            "value": json.dumps({"type": type_, "acl": acl, "value": value})}


FACTS = [fact(0x0162, "0x23/uint32", "RW", 600000), fact(0x0203, "0x10/bool", "RW", 0),
         fact(0x010C, "0x20/uint8", "RW", 2), fact(0x0142, "0x20/uint8", "R", 1),
         fact(0x019A, "0x41/octstr", "RW", "0003ffffff"), fact(0x0270, "0x20/uint8", "RW", 0),
         fact(0x0000, "0x21/int16", "RW", 5, mfr="", cluster="0x0402")]


class _Cluster:
    def __init__(self):
        self.written = []

    async def write_attributes_raw(self, attrs, mfr=None):
        self.written += [(a.attrid, int(a.value.value), mfr) for a in attrs]
        return [[NS(status=0)]]


def run() -> Checker:
    c = Checker("raw_settings")
    real_get, real_db = zigbee_cache.get_facts, zigbee_cache._db
    zigbee_cache.get_facts, zigbee_cache._db = (lambda ieee: FACTS), object()
    try:
        cluster = _Cluster()
        dev = NS(ieee=IEEE, state={"opple_0x0162": 600000, "opple_0x0203": False},
                 zigpy_dev=NS(endpoints={1: NS(in_clusters={0xFCC0: cluster})}))

        c.section("which attributes become controls")
        opts = {o["name"]: o for o in zmm_settings.raw_options(dev, covered={0x010C})}
        c.check("each writable maker's attribute of a writable type, by EP, cluster and id",
                set(opts) == {"raw_1_fcc0_0162", "raw_1_fcc0_0203"}, sorted(opts))
        c.check("not one a handler or entry already offers (motion sensitivity)",
                "raw_1_fcc0_010c" not in opts)
        c.check("not a read-only one, bytes, a standard cluster's, or one that starts an action",
                not {"raw_1_fcc0_0142", "raw_1_fcc0_019a", "raw_1_0402_0000", "raw_1_fcc0_0270"} & set(opts))
        num, flag = opts["raw_1_fcc0_0162"], opts["raw_1_fcc0_0203"]
        c.check("a number is bounded by what its type holds, and labelled by its id",
                (num["type"], num["min"], num["max"], num["label"]) == ("number", 0, 0xFFFFFFFF, "0xFCC0/0x0162"),
                num)
        c.check("an on/off one is a choice, showing the device's current value",
                flag["type"] == "select" and flag["current_value"] == 0 and num["current_value"] == 600000, flag)
        c.check("both are grouped as unnamed and say so",
                {num["group"], flag["group"]} == {"unnamed"} and "not named" in num["description"])

        c.section("writing one")
        got = asyncio.run(zmm_settings.apply_raw(dev, {"raw_1_fcc0_0162": "300000", "qos": 1}))
        c.check("it is written raw, with the maker's code, and the state follows",
                got == {"raw_1_fcc0_0162": True} and cluster.written == [(0x0162, 300000, 0x115F)]
                and dev.state["opple_0x0162"] == 300000, (got, cluster.written))
        got = asyncio.run(zmm_settings.apply_raw(dev, {"raw_1_fcc0_0162": "-1"}))
        c.check("a value its type cannot hold is refused before anything is sent",
                got == {"raw_1_fcc0_0162": False} and len(cluster.written) == 1, cluster.written)
        got = asyncio.run(zmm_settings.apply_raw(dev, {"raw_1_fcc0_0142": "0"}))
        c.check("and so is a control for an attribute that is not writable",
                got == {"raw_1_fcc0_0142": False} and len(cluster.written) == 1)
    finally:
        zigbee_cache.get_facts, zigbee_cache._db = real_get, real_db
    return c


if __name__ == "__main__":
    run()
