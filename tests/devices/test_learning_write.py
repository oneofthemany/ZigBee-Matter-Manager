"""
Write-and-revert (docs/plans/device-learning.md step 8): ZMM flips one
manufacturer toggle at a time while the user watches, and always puts it
back: on the answer, on a timeout, or when the session ends.
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
from modules import device_decisions, device_facts, device_learning
from modules.device_facts import Fact, _j, record
from modules.device_profiles import ProfileStore
from modules.learning_recipes import validate

IEEE = "54:ef:44:10:01:5a:14:eb"
INPUTS = {"setting_id": "button_leds", "label": "Button LEDs", "from_label": "On", "to_label": "Off"}
LEDS = {"ep": 1, "cluster": "0xFCC0", "attr": "0x0203"}


class _Fcc0:
    cluster_id = 0xFCC0

    def __init__(self, values):
        self.values = values
        self.writes = []
        self.refuse_after = None          # refuse writes once this many have been accepted

    async def read_attributes_raw(self, attrs, manufacturer=None):
        return NS(status_records=[NS(attrid=a, status=0, value=NS(value=self.values[a]))
                                  for a in attrs if a in self.values])

    async def write_attributes_raw(self, attrs, manufacturer=None):
        a = attrs[0]
        if self.refuse_after is not None and len(self.writes) >= self.refuse_after:
            return [[NS(status=0x87)]]
        self.writes.append((a.attrid, int(a.value.value), manufacturer))
        self.values[a.attrid] = int(a.value.value)
        return [[NS(status=0)]]


def _device(mains=True):
    fc = _Fcc0({0x0203: 1, 0x0009: 0, 0x0701: 10, 0x0270: 0})
    ep = NS(endpoint_id=1, profile_id=0x0104, device_type=0, out_clusters={},
            in_clusters={0xFCC0: fc})
    zdev = NS(endpoints={0: None, 1: ep}, node_desc=NS(is_mains_powered=mains),
              model="lumi.plug.aeu002", manufacturer="Aqara")
    return NS(ieee=IEEE, zigpy_dev=zdev, state={}, handlers={}), fc


def _facts():
    def f(attr, typ, acl, value):
        return Fact(1, f"attr:0xFCC0/0x{attr:04X}@0x115F", "answered",
                    _j({"type": typ, "acl": acl, "value": value}))
    record(IEEE, [f(0x0203, "0x10/bool", "RW", 1), f(0x0009, "0x20/uint8", "RWP", 0),
                  f(0x0701, "0x28/int8", "RWP", 10), f(0x0270, "0x20/uint8", "RW", 0),
                  f(0x00F6, "0x21/uint16", "RWP", 300), f(0x0005, "0x20/uint8", "RP", 1)])


KEY = "manufacturer_setting.try_setting"


def run() -> Checker:
    c = Checker("learning_write")
    tmp = Path(tempfile.mkdtemp(prefix="zmm_lw_"))
    zigbee_cache.DB_PATH = str(tmp / "cache.duckdb")
    zigbee_cache._db, zigbee_cache._INITIALISED = None, False
    for m in (device_facts, device_decisions):
        m.forget(IEEE)
    zigbee_cache.warm()
    device_profiles._store = ProfileStore(user_dir=str(tmp / "u"), bundled_dir=str(tmp / "b"),
                                          ieee_overrides_file=str(tmp / "i.json"), zmm_dir=str(tmp / "z"))
    _facts()
    real_timeout = device_learning.TRY_TIMEOUT
    try:
        c.section("recipes")
        c.check("a write step may only confirm a write", validate(
            {"id": "x", "steps": [{"id": "s", "mode": "try_write",
                                   "infer": [{"op": "press_signature", "yields": "zmm"}]}]}) is None)
        c.check("and confirming a write needs a write step", validate(
            {"id": "x", "steps": [{"id": "s", "infer": [{"op": "confirmed_write",
                                                          "yields": "zmm.settings"}]}]}) is None)

        c.section("what ZMM may flip")
        dev, fc = _device()
        device_learning.start(dev)
        cands = device_learning.candidates(dev, KEY)["candidates"]
        c.check("only writable manufacturer toggles now 0 or 1: not an int8 at 10, not a uint16, "
                "not read-only, not motor calibration",
                sorted(x["attr"] for x in cands) == ["0x0009", "0x0203"], cands)
        c.check("the observe-style start is refused for a write step",
                not asyncio.run(device_learning.begin(dev, KEY, INPUTS))["success"])

        c.section("a flip the user sees")
        async def flip_then(answer):
            st = await device_learning.try_write(dev, KEY, LEDS, INPUTS)
            second = await device_learning.try_write(dev, KEY, {**LEDS, "attr": "0x0009"}, INPUTS)
            return st, second, await device_learning.answer(dev, KEY, answer)
        st, second, done = asyncio.run(flip_then(True))
        c.check("the toggle is flipped with the maker's code", fc.writes[0] == (0x0203, 0, 0x115F), fc.writes)
        c.check("and shown as pending", st["trial"]["attr"] == "0x0203" and st["trial"]["new"] == 0)
        c.check("a second flip waits for the answer", not second["success"])
        c.check("on the answer it is put back", fc.writes[-1] == (0x0203, 1, 0x115F)
                and fc.values[0x0203] == 1 and done["trial"] is None, fc.writes)
        props = next(s for s in done["steps"] if s["key"] == KEY)["proposals"]
        c.check("and becomes the named setting, with both values named",
                props[0]["path"] == "zmm.settings" and props[0]["value"]["values"] == {"1": "On", "0": "Off"}
                and props[0]["value"]["type"] == "bool", props)
        device_learning.decide(dev, KEY, [0])
        c.check("accepted, it is a learned fact",
                any(f["subject"] == "learned:zmm.settings.button_leds" for f in zigbee_cache.get_facts(IEEE)))

        c.section("a flip that changes nothing")
        async def no_change():
            await device_learning.try_write(dev, KEY, {**LEDS, "attr": "0x0009"}, INPUTS)
            return await device_learning.answer(dev, KEY, False)
        st = asyncio.run(no_change())
        c.check("is put back and ruled out", fc.values[0x0009] == 0 and
                next(s for s in st["steps"] if s["key"] == KEY)["tried"] == ["EP1 0xFCC0/0x0009"])

        c.section("a put-back the device refuses")
        async def refused():
            await device_learning.try_write(dev, KEY, LEDS, INPUTS)
            fc.refuse_after = len(fc.writes)
            return await device_learning.answer(dev, KEY, True)
        st = asyncio.run(refused())
        note = next(s for s in st["steps"] if s["key"] == KEY)["proposals"][0]["evidence"]
        c.check("is reported, not hidden", "could not be confirmed put back" in note, note)
        fc.refuse_after = None
        fc.values[0x0203] = 1

        c.section("put back without an answer")
        device_learning.TRY_TIMEOUT = 0.05
        async def walk_away():
            await device_learning.try_write(dev, KEY, LEDS, INPUTS)
            await asyncio.sleep(0.2)
        asyncio.run(walk_away())
        c.check("after the timeout", fc.values[0x0203] == 1 and fc.writes[-1] == (0x0203, 1, 0x115F))
        device_learning.TRY_TIMEOUT = real_timeout

        async def end_mid_trial():
            await device_learning.try_write(dev, KEY, LEDS, INPUTS)
            await device_learning.end(dev)
        asyncio.run(end_mid_trial())
        c.check("and when the session ends", fc.values[0x0203] == 1)

        c.section("sleepy devices")
        sleepy, sfc = _device(mains=False)
        device_learning.start(sleepy)
        c.check("are never written: one might not be put back",
                not device_learning.candidates(sleepy, KEY)["success"] and sfc.writes == [])
        asyncio.run(device_learning.end(sleepy))
    finally:
        device_learning.TRY_TIMEOUT = real_timeout
        device_profiles._store = None
        if zigbee_cache._db is not None:
            zigbee_cache._db.close()
        zigbee_cache._db, zigbee_cache._INITIALISED = None, False
        for m in (device_facts, device_decisions):
            m.forget(IEEE)
        shutil.rmtree(tmp, ignore_errors=True)
    return c


if __name__ == "__main__":
    run()
