"""
The Clusters modal's live discovery reads, and never writes.

It used to write each readable attribute's value back to learn whether it was
writable (a real write to a TRV or a relay), and it iterated zigpy's discovery
response as if it were a list, found nothing, and fell back to reading every
attribute zigpy knows one at a time: minutes per cluster.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from harness import Checker

import modules.zigbee_cache as zigbee_cache
from core.service import ZigbeeService


class _Cluster:
    def __init__(self, cid, listed, values, extended=True, mfr_listed=None, schema=None):
        self.cluster_id = cid
        self.listed = listed                   # {attr: (datatype, acl)}
        self.mfr_listed = mfr_listed or {}
        self.values = values
        self.extended = extended
        self.attributes = schema or {}
        self.sent: list = []

    def _page(self, table, start, count):
        ids = sorted(a for a in table if a >= start)
        return ids[:count], len(ids) <= count

    async def discover_attributes_extended(self, start, count, manufacturer=None):
        self.sent.append(("discover_ext", manufacturer))
        if not self.extended:
            return NS()                        # default response: unsupported
        table = self.mfr_listed if manufacturer else self.listed
        page, done = self._page(table, start, count)
        return NS(extended_attr_info=[NS(attrid=a, datatype=table[a][0], acl=table[a][1])
                                      for a in page], discovery_complete=done)

    async def discover_attributes(self, start, count, manufacturer=None):
        self.sent.append(("discover", manufacturer))
        table = self.mfr_listed if manufacturer else self.listed
        page, done = self._page(table, start, count)
        return NS(attribute_info=[NS(attrid=a, datatype=table[a][0]) for a in page],
                  discovery_complete=done)

    async def read_attributes_raw(self, attrs, manufacturer=None):
        self.sent.append(("read", list(attrs), manufacturer))
        return NS(status_records=[
            NS(attrid=a, status=0, value=NS(value=self.values[a])) if a in self.values
            else NS(attrid=a, status=0x86, value=None) for a in attrs])

    async def write_attributes(self, *a, **k):
        self.sent.append(("write",))

    async def write_attributes_raw(self, *a, **k):
        self.sent.append(("write",))


def _service(cluster, mfr_code=None):
    svc = ZigbeeService.__new__(ZigbeeService)
    ep = NS(in_clusters={cluster.cluster_id: cluster}, out_clusters={})
    zdev = NS(endpoints={1: ep}, node_desc=NS(manufacturer_code=mfr_code))
    svc.devices = {"aa": NS(zigpy_dev=zdev)}
    return svc


def run() -> Checker:
    c = Checker("cluster_discovery")
    cached = []
    real = (zigbee_cache.record_attribute_metadata, zigbee_cache.keep_only_attributes)
    zigbee_cache.record_attribute_metadata = lambda *a, **k: cached.append((a, k))
    kept = []
    zigbee_cache.keep_only_attributes = lambda ieee, ep, cid, ids: kept.append((cid, sorted(ids)))

    c.section("electrical measurement on the Aqara outlet")
    em = _Cluster(0x0B04,
                  listed={0x0000: (0x1B, 0x01), 0x050B: (0x29, 0x05), 0x0604: (0x21, 0x01),
                          0x0605: (0x21, 0x01), 0x0800: (0x19, 0x03)},
                  values={0x0000: 1, 0x050B: 2, 0x0604: 1, 0x0605: 10, 0x0800: 0})
    r = asyncio.run(_service(em).discover_cluster_attributes("aa", 1, 0x0B04))
    by_id = {a["id_int"]: a for a in r["attributes"]}
    c.check("nothing is written to the device", not any(s[0] == "write" for s in em.sent),
            em.sent)
    c.check("every listed attribute is returned", sorted(by_id) == sorted(em.listed),
            sorted(by_id))
    c.check("writable comes from the access flags",
            by_id[0x0800]["writable"] is True and by_id[0x050B]["writable"] is False)
    c.check("reportable too", by_id[0x050B]["reportable"] is True)
    c.check("values are read", by_id[0x050B]["value"] == 2 and by_id[0x0605]["value"] == 10)
    reads = [s for s in em.sent if s[0] == "read"]
    c.check("reads are chunked, not one per attribute", len(reads) == 2, reads)
    c.check("attributes it no longer lists are dropped from the cache",
            kept == [(0x0B04, sorted(em.listed))], kept)

    c.section("a manufacturer cluster is also discovered under the maker's code")
    fcc0 = _Cluster(0xFCC0, listed={}, values={0x0201: 1, 0x0009: 0},
                    mfr_listed={0x0009: (0x20, 0x03), 0x0201: (0x10, 0x03)})
    cached.clear()
    r = asyncio.run(_service(fcc0, mfr_code=0x115F).discover_cluster_attributes("aa", 1, 0xFCC0))
    ids = [a["id_int"] for a in r["attributes"]]
    c.check("the Aqara attributes are found", ids == [0x0009, 0x0201], ids)
    c.check("and read with the manufacturer code",
            ("read", [0x0009, 0x0201], 0x115F) in fcc0.sent, fcc0.sent)
    c.check("and cached with it", any(k.get("manufacturer_code") == 0x115F for _, k in cached),
            cached)

    c.section("a device without extended discovery")
    plain = _Cluster(0x0006, listed={0x0000: (0x10, None)}, values={0x0000: 1}, extended=False)
    r = asyncio.run(_service(plain).discover_cluster_attributes("aa", 1, 0x0006))
    a = r["attributes"][0]
    c.check("plain discovery still finds the attribute", a["id_int"] == 0 and a["readable"])
    c.check("writability is reported unknown, not probed by writing",
            a["writable"] is None and not any(s[0] == "write" for s in plain.sent), plain.sent)

    c.section("a device that answers no discovery")
    silent = _Cluster(0x0000, listed={}, values={0x0004: "Aqara"},
                      schema={0x0004: NS(name="manufacturer", type=str),
                              0x0005: NS(name="model", type=str)})
    r = asyncio.run(_service(silent).discover_cluster_attributes("aa", 1, 0x0000))
    c.check("zigpy's schema is offered, keeping only what answered",
            [a["name"] for a in r["attributes"]] == ["manufacturer"], r["attributes"])
    zigbee_cache.record_attribute_metadata, zigbee_cache.keep_only_attributes = real
    return c


if __name__ == "__main__":
    run()
