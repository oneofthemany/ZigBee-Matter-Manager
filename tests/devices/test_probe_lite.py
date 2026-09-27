"""
Step 4 of docs/plans/zmm-quirks.md: attribute discovery at join, and passive
observation of which endpoint reports what.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from harness import Checker

import modules.zigbee_cache as zigbee_cache
from modules import device_facts, device_observer, probe_lite

IEEE = "54:ef:44:10:01:5a:14:eb"


def _report(attr: int, dtype: int, value: bytes, mfr: int = None) -> bytes:
    """A ZCL Report Attributes frame, as the radio hands it over."""
    if mfr is None:
        head = bytes([0x18, 0x01, 0x0A])
    else:
        head = bytes([0x1C]) + mfr.to_bytes(2, "little") + bytes([0x01, 0x0A])
    return head + attr.to_bytes(2, "little") + bytes([dtype]) + value


def _power(watts_x10: int) -> bytes:
    return _report(0x050B, 0x29, watts_x10.to_bytes(2, "little", signed=True))


def _observed(ep, subject):
    for f in zigbee_cache.get_facts(IEEE):
        if (f["endpoint_id"], f["subject"], f["source"]) == (ep, subject, "observed"):
            return json.loads(f["value"])
    return None


class _Cluster:
    def __init__(self, cid, listed, mfr_listed=None):
        self.cluster_id = cid
        self.listed, self.mfr_listed = listed, mfr_listed or {}
        self.sent = []

    async def discover_attributes_extended(self, start, count, manufacturer=None):
        self.sent.append(("discover_ext", manufacturer))
        table = self.mfr_listed if manufacturer else self.listed
        ids = sorted(a for a in table if a >= start)
        return NS(extended_attr_info=[NS(attrid=a, datatype=table[a][0], acl=table[a][1])
                                      for a in ids[:count]],
                  discovery_complete=len(ids) <= count)

    async def discover_attributes(self, *a, **k):
        raise AssertionError("extended discovery should have answered")


def _device(mains=True, ieee=IEEE):
    em = _Cluster(0x0B04, {0x050B: (0x29, 0x05), 0x0605: (0x21, 0x01)})
    fcc0 = _Cluster(0xFCC0, {}, {0x0009: (0x20, 0x03)})
    zdev = NS(node_desc=NS(is_mains_powered=mains, manufacturer_code=0x115F),
              endpoints={0: None, 1: NS(in_clusters={0x0B04: em, 0xFCC0: fcc0})})
    return NS(ieee=ieee, zigpy_dev=zdev, is_coordinator=False), em, fcc0


def run() -> Checker:
    c = Checker("probe_lite")
    tmp = tempfile.mkdtemp(prefix="zmm_pl_")
    zigbee_cache.DB_PATH = str(Path(tmp) / "cache.duckdb")
    zigbee_cache._db, zigbee_cache._INITIALISED = None, False
    device_observer.forget(IEEE)
    zigbee_cache.warm()
    try:
        c.section("observation: which EP reports what")
        obs = device_observer.observe
        c.check("a non-report frame is ignored without decoding",
                obs(IEEE, 0x0104, 0x0B04, 1, bytes([0x18, 0x01, 0x01, 0x0B, 0x05, 0x00])) == 0)
        c.check("the first report of an attribute on an EP is recorded",
                obs(IEEE, 0x0104, 0x0B04, 3, _power(0)) == 1
                and _observed(3, "reports:0x0B04/0x050B") == {"nonzero": False, "last": 0})
        c.check("a repeat is not written", obs(IEEE, 0x0104, 0x0B04, 3, _power(0)) == 0)
        c.check("the first non-zero value is",
                obs(IEEE, 0x0104, 0x0B04, 3, _power(82)) == 1
                and _observed(3, "reports:0x0B04/0x050B")["nonzero"] is True)
        c.check("later values are not", obs(IEEE, 0x0104, 0x0B04, 3, _power(90)) == 0)
        c.check("the same attribute on another EP is its own fact",
                obs(IEEE, 0x0104, 0x0B04, 2, _power(20)) == 1
                and _observed(2, "reports:0x0B04/0x050B")["nonzero"] is True)
        obs(IEEE, 0x0104, 0xFCC0, 1, _report(0x00F7, 0x20, b"\x01", mfr=0x115F))
        c.check("a manufacturer report is scoped by the maker's code",
                _observed(1, "reports:0xFCC0/0x00F7@0x115F") is not None)

        device_observer.forget(IEEE)      # a restart
        obs(IEEE, 0x0104, 0x0B04, 3, _power(0))
        c.check("an idle first report after a restart keeps the stored non-zero",
                _observed(3, "reports:0x0B04/0x050B")["nonzero"] is True)

        c.section("probe-lite at join")
        dev, em, fcc0 = _device()
        n = asyncio.run(probe_lite.probe_lite(dev))
        subjects = {f["subject"]: json.loads(f["value"]) for f in zigbee_cache.get_facts(IEEE)
                    if f["source"] == "answered"}
        c.check("every listed attribute is recorded with its access",
                subjects.get("attr:0x0B04/0x050B") == {"type": "0x29/int16", "acl": "RP"}, subjects)
        c.check("vendor clusters are discovered under the maker's code",
                subjects.get("attr:0xFCC0/0x0009@0x115F", {}).get("acl") == "RW"
                and ("discover_ext", 0x115F) in fcc0.sent, fcc0.sent)
        c.check("standard clusters are not", ("discover_ext", 0x115F) not in em.sent)
        c.check("it reports how many", n == 3, n)
        sent = len(em.sent)
        c.check("a device already probed is left alone",
                asyncio.run(probe_lite.probe_lite(dev)) == 0 and len(em.sent) == sent)
        c.check("a join forces a fresh pass", asyncio.run(probe_lite.probe_lite(dev, force=True)) == 3)
        battery, bem, _ = _device(mains=False, ieee="00:00:00:00:00:00:00:01")
        c.check("a battery device is not probed",
                asyncio.run(probe_lite.probe_lite(battery)) == 0 and bem.sent == [])

        c.section("one device at a time")
        order = []

        class _Slow(_Cluster):
            async def discover_attributes_extended(self, *a, **k):
                order.append(("start", self.tag))
                await asyncio.sleep(0.01)
                order.append(("end", self.tag))
                return NS(extended_attr_info=[], discovery_complete=True)

        def _slow(tag, ieee):
            cl = _Slow(0x0006, {})
            cl.tag = tag
            zdev = NS(node_desc=NS(is_mains_powered=True, manufacturer_code=None),
                      endpoints={1: NS(in_clusters={0x0006: cl})})
            return NS(ieee=ieee, zigpy_dev=zdev, is_coordinator=False)

        async def both():
            await asyncio.gather(probe_lite.probe_lite(_slow("a", "aa:01")),
                                 probe_lite.probe_lite(_slow("b", "aa:02")))
        asyncio.run(both())
        c.check("two joins never discover at the same time",
                [e for e, _ in order] == ["start", "end", "start", "end"], order)

        c.section("the backfill")
        fresh, fem, _ = _device(ieee="00:00:00:00:00:00:00:02")
        done = asyncio.run(probe_lite.backfill([dev, fresh], delay=0, spacing=0))
        c.check("covers only devices never probed", done == 1 and fem.sent, (done, fem.sent))
    finally:
        if zigbee_cache._db is not None:
            zigbee_cache._db.close()
        zigbee_cache._db, zigbee_cache._INITIALISED = None, False
        device_observer.forget(IEEE)
        shutil.rmtree(tmp, ignore_errors=True)
    return c


if __name__ == "__main__":
    run()
