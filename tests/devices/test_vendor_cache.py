"""
What a device has already reported on its maker's cluster is surfaced from
zigpy's attribute cache: every attribute visible at once, nothing replaced.
"""

from __future__ import annotations

import tempfile
from types import SimpleNamespace as NS

from harness import Checker

import modules.device_facts as device_facts
import modules.device_profiles as device_profiles
from handlers.aqara import AqaraManufacturerCluster
from modules import vendor_cache
from modules.device_profiles import ProfileStore

AGL8_F7 = bytes.fromhex("052101000a21529c0c200a0d232a4100001320001721960c1820641c1000642000652002672000")


def _device(state):
    """A battery Aqara sensor as it is after a restart: zigpy's cache holds
    what it reported (definition-less attributes by id alone)."""
    item = lambda v: NS(value=v)
    cache = NS(_cache={(0x023E, 0x115F): item(589845)},
               _legacy_cache={0x0142: item(1), 0x014D: item(0), 0x00F7: item(AGL8_F7)})
    ep = NS(endpoint_id=1, in_clusters={})
    cluster = NS(cluster_id=0xFCC0, endpoint=ep, _attr_cache=cache, add_listener=lambda _l: None)
    basic = NS(cluster_id=0x0000, endpoint=ep, _attr_cache=NS(_cache={(5, None): item("x")}, _legacy_cache={}))
    ep.in_clusters = {0x0000: basic, 0xFCC0: cluster}
    dev = NS(ieee="aa:bb:00:00:00:00:00:08", state=dict(state), handlers={}, published=[],
             zigpy_dev=NS(model="unknown.sensor", manufacturer="Acme", endpoints={0: None, 1: ep},
                          node_desc=NS(is_mains_powered=False, manufacturer_code=0x115F,
                                       logical_type=2, mac_capability_flags=0)))
    dev.update_state = lambda d, **_: dev.published.append(d)
    dev.service = NS(state_cache={}, _cache_dirty=False)
    dev.handlers[(1, 0xFCC0)] = AqaraManufacturerCluster(dev, cluster)
    return dev


def run() -> Checker:
    c = Checker("vendor_cache")
    d = tempfile.mkdtemp()
    device_profiles._store = ProfileStore(user_dir=d + "/u", bundled_dir=d + "/b",
                                          ieee_overrides_file=d + "/i.json", zmm_dir=d + "/z")
    recorded = []
    real_record, device_facts.record = device_facts.record, lambda ieee, facts: recorded.extend(facts) or 0
    try:
        c.section("what zigpy holds")
        dev = _device({})
        got = {(cid, aid): (mfr, v) for _, cid, aid, mfr, v in vendor_cache.cached(dev.zigpy_dev)}
        c.check("every attribute cached on the maker's cluster, with the maker's code",
                set(got) == {(0xFCC0, 0x0142), (0xFCC0, 0x014D), (0xFCC0, 0x00F7), (0xFCC0, 0x023E)}
                and {m for m, _ in got.values()} == {0x115F}, got)
        c.check("and nothing from a standard cluster", (0x0000, 5) not in got)

        c.section("surfacing it after a restart")
        dev = _device({"opple_0x0142": 0, "battery": 0, "battery_voltage": 0.0, "opple_0x0a18": " ",
                       "temperature": 21.5})
        n = vendor_cache.seed(dev)
        c.check("attributes the state lacked appear", dev.state.get("opple_0x014d") == 0
                and dev.state.get("opple_0x023e") == 589845, dev.state)
        c.check("a value a handler already holds is not replaced by an older cached one",
                dev.state["opple_0x0142"] == 0, dev.state)
        c.check("a battery level of 0 is no reading: the cached status blob fills it in",
                (dev.state["battery"], dev.state["battery_voltage"]) == (100, 3.222), dev.state)
        c.check("a raw key for an attribute the device never reported is dropped",
                "opple_0x0a18" not in dev.state, dev.state)
        c.check("other readings are untouched, and the count says what was added",
                dev.state["temperature"] == 21.5 and n == 4, (n, dev.state))
        c.check("nothing is published or handed to automations: these are not new events",
                dev.published == [], dev.published)
        c.check("the device's own update path is left as it was", dev.update_state({"x": 1}) is None
                and dev.published == [{"x": 1}])
        c.check("the filled state is persisted", dev.service._cache_dirty
                and dev.service.state_cache[dev.ieee]["battery"] == 100)
        subjects = {f.subject for f in recorded}
        c.check("each is recorded as an observed report, for learning to match against",
                "reports:0xFCC0/0x023E@0x115F" in subjects and "reports:0xFCC0/0x0142@0x115F" in subjects
                and all(f.source == "observed" for f in recorded), subjects)

        dev = _device({})
        dev.zigpy_dev.endpoints[1].in_clusters[0xFCC0]._attr_cache = NS(_cache={}, _legacy_cache={})
        dev.state = {"opple_0x0142": 1}
        c.check("a device with nothing cached is left alone", vendor_cache.seed(dev) == 0
                and dev.state == {"opple_0x0142": 1})
    finally:
        device_facts.record = real_record
        device_profiles._store = None
    return c


if __name__ == "__main__":
    run()
