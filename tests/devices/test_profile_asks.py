"""
What an entry asks of the device: reporting configured on a maker's
attribute zigpy has no definition for, values read at join, and one-shot
writes as actions, each carrying its type and manufacturer code.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from harness import Checker

from modules.device_profiles import normalise_profile
from modules import device_profiles_apply as apply


class _Cluster:
    """A maker's cluster zigpy knows nothing about (as for 0xFCC0 with no quirk)."""
    cluster_id = 0xFCC0

    def __init__(self):
        self.configured, self.read, self.written, self.bound = [], [], [], 0

    def find_attribute(self, aid):
        raise KeyError(aid)

    async def bind(self):
        self.bound += 1

    async def configure_reporting_multiple(self, config):
        for attr_def, cfg in config.items():
            self.configured.append((int(attr_def.id), attr_def.manufacturer_code, attr_def.zcl_type,
                                    cfg.min_interval, cfg.max_interval, cfg.reportable_change))
        return [NS(status=0, attrid=None)]

    async def read_attributes_raw(self, attrs, manufacturer=None):
        self.read.append((list(attrs), manufacturer))
        return NS(status_records=[NS(attrid=a, status=0, value=NS(value=1)) for a in attrs])

    async def write_attributes_raw(self, attrs, mfr=None):
        self.written += [(a.attrid, int(a.value.value), mfr) for a in attrs]
        return [[NS(status=0)]]


PROFILE = normalise_profile({
    "id": "x", "match": {"model": "x"},
    "reporting": [{"ep": 1, "cluster": "0xFCC0", "attr": "0x0142", "min": 0, "max": 3600, "delta": 1,
                   "mfr": "0x115F", "type": "uint8"}],
    "reads": [{"ep": 1, "cluster": "0xFCC0", "attr": "0x00F7", "mfr": "0x115F"}],
    "actions": [{"id": "restart", "label": "Restart", "ep": 1, "cluster": "0xFCC0",
                 "writes": [{"ep": 1, "cluster": "0xFCC0", "attr": "0x00E8", "value": 1, "type": "uint8",
                             "mfr": "0x115F"}]}],
})


def run() -> Checker:
    c = Checker("profile_asks")
    c.section("the entry as stored")
    c.check("a minimum report gap of 0 is kept, not turned into 30 s", PROFILE["reporting"][0]["min"] == 0,
            PROFILE["reporting"])
    c.check("a row keeps its type and maker's code",
            (PROFILE["reporting"][0]["mfr"], PROFILE["reporting"][0]["type"]) == ("0x115F", "uint8"))
    c.check("reads and action writes keep the maker's code",
            PROFILE["reads"][0]["mfr"] == "0x115F" and PROFILE["actions"][0]["writes"][0]["mfr"] == "0x115F")

    cl = _Cluster()
    seen = []
    dev = NS(ieee="aa:bb:00:00:00:00:00:10", handlers={(1, 0xFCC0): NS(attribute_updated=lambda a, v: seen.append((a, v)))},
             zigpy_dev=NS(endpoints={1: NS(in_clusters={0xFCC0: cl})}))

    c.section("applied to the device")
    out = asyncio.run(apply.apply_reporting(dev, PROFILE))
    c.check("reporting is configured on an attribute zigpy has no definition for, with its code and type",
            out["configured"] == 1 and cl.configured == [(0x0142, 0x115F, 0x20, 0, 3600, 1)], (out, cl.configured))
    c.check("after binding the cluster to the hub", cl.bound == 1)
    n = asyncio.run(apply.apply_reads(dev, PROFILE))
    c.check("a listed attribute is read with the maker's code, and its value handled as a report",
            n == 1 and cl.read == [([0x00F7], 0x115F)] and seen == [(0x00F7, 1)], (cl.read, seen))

    no_type = normalise_profile({"id": "y", "match": {"model": "y"},
                                 "reporting": [{"ep": 1, "cluster": "0xFCC0", "attr": "0x0150", "mfr": "0x115F"}]})
    apply._answered_type, real = (lambda *a: None), apply._answered_type
    try:
        out = asyncio.run(apply.apply_reporting(dev, no_type))
    finally:
        apply._answered_type = real
    c.check("an attribute whose type is not known is reported as such, not sent wrongly",
            out["configured"] == 0 and "type unknown" in out["results"][0]["error"], out)

    dev.profile_actions = PROFILE["actions"]
    apply.resolve_profile_for_device, real_r = (lambda d: PROFILE), apply.resolve_profile_for_device
    try:
        res = asyncio.run(apply.run_action(dev, "restart"))
    finally:
        apply.resolve_profile_for_device = real_r
    c.check("an action writes its value raw, typed, with the maker's code",
            res.get("success") and cl.written == [(0x00E8, 1, 0x115F)], (res, cl.written))
    return c


if __name__ == "__main__":
    run()
