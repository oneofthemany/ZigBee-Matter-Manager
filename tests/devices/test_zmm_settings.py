"""
Step 7 of docs/plans/zmm-quirks.md: settings a ZMM entry declares are offered
only where the endpoint lists the attribute, and written raw with the
entry's manufacturer code.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from harness import Checker

import modules.device_profiles as device_profiles
import modules.zigbee_cache as zigbee_cache
from modules import device_facts, zmm_settings
from modules.device_facts import Fact, _j, record
from modules.device_profiles import normalise_profile

IEEE = "54:ef:44:10:01:5a:14:eb"

ENTRY = normalise_profile({
    "id": "lumi.plug.aeu002", "match": {"model": "lumi.plug.aeu002"},
    "endpoints": {"2": {"label": "Socket 2"}},
    "zmm": {"settings": [
        {"id": "button_leds", "label": "Button LEDs", "type": "bool", "ep": 1,
         "cluster": "0xFCC0", "attr": "0x0203", "mfr": "0x115F"},
        {"id": "multi_click", "label": "Multi-click", "type": "uint8", "ep": "each",
         "cluster": "0xFCC0", "attr": "0x0286", "mfr": "0x115F",
         "values": {"1": "Off", "2": "On"}},
        {"id": "never_listed", "type": "bool", "ep": 1, "cluster": "0xFCC0", "attr": "0x0999",
         "mfr": "0x115F"},
    ]},
})


class _Store:
    def get_profile_for_device(self, **_):
        return ENTRY


class _Fcc0:
    cluster_id = 0xFCC0

    def __init__(self, refuse=False):
        self.refuse = refuse
        self.writes = []

    async def write_attributes_raw(self, attrs, manufacturer=None):
        a = attrs[0]
        self.writes.append((a.attrid, a.value.type, a.value.value, manufacturer))
        return [[NS(status=0x87 if self.refuse else 0)]]


def run() -> Checker:
    c = Checker("zmm_settings")
    device_profiles._store = _Store()
    tmp = tempfile.mkdtemp(prefix="zmm_set_")
    zigbee_cache.DB_PATH = str(Path(tmp) / "cache.duckdb")
    zigbee_cache._db, zigbee_cache._INITIALISED = None, False
    device_facts.forget(IEEE)
    zigbee_cache.warm()
    try:
        fc1, fc2, fc3 = _Fcc0(), _Fcc0(refuse=True), _Fcc0()
        eps = {i: NS(in_clusters={0xFCC0: fc}) for i, fc in ((1, fc1), (2, fc2), (3, fc3))}
        dev = NS(ieee=IEEE, zigpy_dev=NS(endpoints={0: None, **eps}), state={})
        record(IEEE, [
            Fact(1, "attr:0xFCC0/0x0203@0x115F", "answered", _j({"type": "0x10/bool", "acl": "RW", "value": 1})),
            Fact(2, "attr:0xFCC0/0x0286@0x115F", "answered", _j({"type": "0x20/uint8", "acl": "RWP", "value": 1})),
            Fact(3, "attr:0xFCC0/0x0286@0x115F", "answered", _j({"type": "0x20/uint8", "acl": "RWP", "value": 2})),
        ])

        c.section("offered only where the endpoint lists the attribute")
        opts = {o["name"]: o for o in zmm_settings.options(dev)}
        c.check("a one-EP setting, and a per-EP setting on each EP that lists it",
                sorted(opts) == ["button_leds", "multi_click_2", "multi_click_3"], sorted(opts))
        c.check("a setting no EP lists is not offered", "never_listed" not in opts)
        c.check("per-EP settings carry the EP's name",
                opts["multi_click_2"]["label"] == "Multi-click (Socket 2)"
                and opts["multi_click_3"]["label"] == "Multi-click (EP3)", opts["multi_click_2"])
        c.check("a boolean is an off/on choice",
                [o["value"] for o in opts["button_leds"]["options"]] == [0, 1])
        c.check("named values become the choices",
                [o["label"] for o in opts["multi_click_3"]["options"]] == ["Off", "On"])
        c.check("the current value comes from the evidence",
                opts["multi_click_3"]["current_value"] == 2)

        c.section("written raw, with the maker's code")
        res = asyncio.run(zmm_settings.apply(dev, {"button_leds": 0, "multi_click_3": 1,
                                                   "unrelated": 5}))
        c.check("each named setting is written", res == {"button_leds": True, "multi_click_3": True}, res)
        c.check("with the right type and the manufacturer code",
                fc1.writes == [(0x0203, 0x10, False, 0x115F)] and fc3.writes == [(0x0286, 0x20, 1, 0x115F)],
                (fc1.writes, fc3.writes))
        c.check("and the new value is shown", dev.state.get("multi_click_3") == 1)
        res = asyncio.run(zmm_settings.apply(dev, {"multi_click_2": 2}))
        c.check("a refused write is reported, and the state left alone",
                res == {"multi_click_2": False} and "multi_click_2" not in dev.state, (res, dev.state))
    finally:
        device_profiles._store = None
        if zigbee_cache._db is not None:
            zigbee_cache._db.close()
        zigbee_cache._db, zigbee_cache._INITIALISED = None, False
        device_facts.forget(IEEE)
        shutil.rmtree(tmp, ignore_errors=True)
    return c


if __name__ == "__main__":
    run()
