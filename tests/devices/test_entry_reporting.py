"""
docs/device-profiles.md §4, Reporting only: an entry whose rows are the device's
whole reporting setup gets every slot of a small table. The FP300 keeps about
five rows; the hub's generic rows took the ones temperature or presence needed.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from typing import NamedTuple

from harness import Checker

import modules.device_profiles as device_profiles
from device.handlers import DeviceHandlerManagerMixin
from modules.device_profiles import ProfileStore, normalise_profile
from modules.device_profiles_apply import REPORTING_DELETE, apply_reporting, entry_owns_reporting

REPO = Path(__file__).resolve().parents[2]


class _Attr(NamedTuple):
    id: int


class _Cluster:
    def __init__(self, cid, log):
        self.cluster_id, self.log = cid, log

    def find_attribute(self, aid):
        return _Attr(aid)

    async def bind(self):
        self.log.append(("bind", self.cluster_id))
        return [0]

    async def configure_reporting_multiple(self, cfg):
        (attr, rc), = cfg.items()
        self.log.append(("report", self.cluster_id, attr.id, rc.max_interval))
        return [NS(status=0)]


class _Handler:
    def __init__(self, cid, log, sets_reporting=True):
        self.cluster_id, self.log = cid, log
        self.endpoint = NS(endpoint_id=1)
        self.CONFIGURE_SETS_REPORTING = sets_reporting

    async def configure(self):
        self.log.append(("handler", self.cluster_id))
        return True


def _fp300(log):
    clusters = {cid: _Cluster(cid, log) for cid in (0x0001, 0x0012, 0x0400, 0x0402, 0x0405, 0xFCC0)}
    zdev = NS(model="lumi.sensor_occupy.agl8", manufacturer="Aqara",
              endpoints={0: None, 1: NS(in_clusters=clusters, out_clusters={})})
    return NS(ieee="54:ef:44:10:01:65:4d:91", zigpy_dev=zdev, state={}, handlers={}, is_coordinator=False)


def run() -> Checker:
    c = Checker("entry_reporting")
    real = device_profiles._store
    d = tempfile.mkdtemp()
    device_profiles._store = ProfileStore(user_dir=d + "/u", bundled_dir=d + "/b",
                                          ieee_overrides_file=d + "/i.json",
                                          zmm_dir=str(REPO / "zmm_quirks"))
    try:
        c.section("the flag")
        p = normalise_profile({"id": "x", "match": {"model": "x"}, "zmm": {"reporting_only": True}})
        c.check("the loader keeps zmm.reporting_only", p["zmm"].get("reporting_only") is True, p.get("zmm"))
        p = normalise_profile({"id": "x", "match": {"model": "x"}, "zmm": {"reporting_only": "yes"}})
        c.check("anything but true is not the flag", not (p.get("zmm") or {}).get("reporting_only"), p.get("zmm"))

        c.section("the FP300 entry")
        log: list = []
        dev = _fp300(log)
        c.check("it owns its reporting", entry_owns_reporting(dev))
        rows = [(r["cluster"], r["attr"], r["max"])
                for r in device_profiles.profile_for_device(dev)["reporting"]]
        deletes = [r for r in rows if r[2] == REPORTING_DELETE]
        kept = [(cl, at) for cl, at, mx in rows if mx != REPORTING_DELETE]
        c.check("it deletes the button and battery rows first, to free their slots",
                rows[:2] == [("0x0012", "0x0055", 65535), ("0x0001", "0x0021", 65535)], rows)
        c.check("then keeps five: radar, PIR, temperature, humidity, lux",
                kept == [("0xFCC0", "0x0142"), ("0xFCC0", "0x014D"), ("0x0402", "0x0000"),
                         ("0x0405", "0x0000"), ("0x0400", "0x0000")], kept)
        temp = next(r for r in device_profiles.profile_for_device(dev)["reporting"] if r["cluster"] == "0x0402")
        c.check("temperature reports on a 0.5 C change", temp["delta"] == 50, temp)

        c.section("applying it")
        res = asyncio.run(apply_reporting(dev, device_profiles.profile_for_device(dev)))
        c.check("every row is taken", res["success"] and res["configured"] == 7, res)
        c.check("a delete row is sent with max 0xFFFF and no bind",
                ("report", 0x0012, 0x0055, 65535) in log and ("bind", 0x0012) not in log
                and ("bind", 0x0001) not in log, log)
        c.check("the rows it keeps are bound, so their reports reach the hub",
                all(("bind", cid) in log for cid in (0xFCC0, 0x0402, 0x0405, 0x0400)), log)

        c.section("handler setup leaves the table to the entry")
        log.clear()
        dev.capabilities = NS(is_endpoint_configurable=lambda ep: True,
                              is_cluster_configurable=lambda cid, ep: True,
                              get_endpoint_role=lambda ep: "sensor")
        generic = _Handler(0x0402, log)
        aqara = _Handler(0xFCC0, log, sets_reporting=False)
        dev.handlers = {(1, 0x0402): generic, (1, 0xFCC0): aqara}
        asyncio.run(DeviceHandlerManagerMixin.configure(dev))
        c.check("a handler that sends generic rows is skipped", ("handler", 0x0402) not in log, log)
        c.check("one whose configure sets no reporting still runs", ("handler", 0xFCC0) in log, log)

        log.clear()
        other = NS(ieee="aa:bb", zigpy_dev=NS(model="acme.none", manufacturer="Acme", endpoints={0: None}),
                   state={}, is_coordinator=False, capabilities=dev.capabilities,
                   handlers={(1, 0x0402): generic})
        asyncio.run(DeviceHandlerManagerMixin.configure(other))
        c.check("a device with no such entry is configured as before", ("handler", 0x0402) in log, log)
    finally:
        device_profiles._store = real
    return c


if __name__ == "__main__":
    run()
