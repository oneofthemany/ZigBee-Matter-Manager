"""
Device learning end to end (docs/plans/device-learning.md): recipes give a
device its steps, a step captures raw frames and reads around the user's
action, accepted proposals become learned facts, and review / save / history /
rollback / export / import turn them into the user's own entry.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from harness import Checker

import modules.device_profiles as device_profiles
import modules.zigbee_cache as zigbee_cache
from handlers.general import OnOffHandler
from handlers.power import ElectricalMeasurementHandler
from modules import device_decisions, device_facts, device_learning
from modules.device_profiles import ProfileStore
from modules.learning_recipes import validate

REPO = Path(__file__).resolve().parents[2]
IEEE = "54:ef:44:10:01:5a:14:eb"


class _Cluster:
    def __init__(self, cid, ep, values):
        self.cluster_id, self.endpoint, self.values = cid, ep, values

    def add_listener(self, _l):
        pass

    async def read_attributes_raw(self, attrs, manufacturer=None):
        return NS(status_records=[NS(attrid=a, status=0, value=NS(value=self.values[a]))
                                  for a in attrs if a in self.values])


def _outlet():
    values = {}
    eps = {}
    for ep_id, ins in ((1, (0x0006, 0x0012, 0x0B04, 0x0702, 0xFCC0)),
                       (2, (0x0006, 0x0012, 0x0B04, 0xFCC0)), (3, (0x0006, 0x0B04, 0xFCC0))):
        ep = NS(endpoint_id=ep_id, profile_id=0x0104, device_type=0x0000, out_clusters={})
        ep.in_clusters = {}
        for c in ins:
            values[(ep_id, c)] = {0x0000: 0} if c == 0x0006 else ({0x050B: 0} if c == 0x0B04 else {})
            ep.in_clusters[c] = _Cluster(c, ep, values[(ep_id, c)])
        eps[ep_id] = ep
    zdev = NS(endpoints={0: None, **eps}, node_desc=NS(is_mains_powered=True),
              model="lumi.plug.aeu002", manufacturer="Aqara")
    dev = NS(ieee=IEEE, zigpy_dev=zdev, state={"sw_version": "0.0.0_0025"}, handlers={})
    for ep_id, ep in eps.items():
        dev.handlers[(ep_id, 0x0006)] = OnOffHandler(dev, ep.in_clusters[0x0006])
        dev.handlers[(ep_id, 0x0B04)] = ElectricalMeasurementHandler(dev, ep.in_clusters[0x0B04])
    return dev, values


def _report(attr, dtype, value: bytes) -> bytes:
    return bytes([0x18, 0x01, 0x0A]) + attr.to_bytes(2, "little") + bytes([dtype]) + value


def _step(state, key):
    return next(s for s in state["steps"] if s["key"] == key)


def run() -> Checker:
    c = Checker("device_learning")
    tmp = Path(tempfile.mkdtemp(prefix="zmm_learn_"))
    zigbee_cache.DB_PATH = str(tmp / "cache.duckdb")
    zigbee_cache._db, zigbee_cache._INITIALISED = None, False
    for m in (device_facts, device_decisions):
        m.forget(IEEE)
    zigbee_cache.warm()
    device_profiles._store = ProfileStore(user_dir=str(tmp / "u"), bundled_dir=str(tmp / "b"),
                                          ieee_overrides_file=str(tmp / "i.json"),
                                          zmm_dir=str(REPO / "zmm_quirks"))
    try:
        c.section("recipes")
        c.check("a recipe naming an unknown operation is rejected", validate(
            {"id": "x", "steps": [{"id": "s", "infer": [{"op": "run_shell", "yields": "zmm"}]}]}) is None)
        c.check("a recipe with an impossible window is rejected", validate(
            {"id": "x", "steps": [{"id": "s", "window_s": 9999,
                                   "infer": [{"op": "press_signature", "yields": "zmm"}]}]}) is None)

        dev, values = _outlet()
        st = device_learning.start(dev)
        keys = [s["key"] for s in st["steps"]]
        c.check("the outlet gets a load test per socket and USB, a press per button, and a setting",
                keys == ["buttons.press.1", "buttons.press.2", "manufacturer_setting.change_setting",
                         "manufacturer_setting.try_setting", "metered_outlet.switch_test.1",
                         "metered_outlet.switch_test.2", "metered_outlet.switch_test.3"], keys)
        relay = NS(ieee="aa:01", zigpy_dev=NS(endpoints={0: None, 1: NS(
            endpoint_id=1, profile_id=0x0104, device_type=0x0100, out_clusters={},
            in_clusters={0x0006: _Cluster(0x0006, None, {0x0000: 0})})}, model="relay",
            manufacturer="x"), state={}, handlers={})
        relay_keys = [s["key"] for s in device_learning.start(relay)["steps"]]
        asyncio.run(device_learning.end(relay))
        c.check("an unmetered relay is asked which lamp it drives; a metered outlet is not",
                relay_keys == ["light.which_is_the_lamp"]
                and not any(k.startswith("light.") for k in keys), relay_keys)
        c.check("steps name the EP the way the user knows it",
                "Socket 1" in _step(st, "metered_outlet.switch_test.1")["instruction"])

        c.section("a switch test")
        key = "metered_outlet.switch_test.1"
        bad = asyncio.run(device_learning.begin(dev, key, {"rating_w": "a lot"}))
        c.check("a rating that is not a number is refused, saying what arrived",
                not bad["success"] and "'a lot'" in bad["error"], bad)
        st = asyncio.run(device_learning.begin(dev, key, {"rating_w": 2000}))
        c.check("the step runs", st["running"] == key)
        onoff = lambda ep, on: device_learning.capture(IEEE, 0x0104, 0x0006, ep,
                                                       _report(0x0000, 0x10, bytes([on])))
        watts = lambda ep, raw: device_learning.capture(IEEE, 0x0104, 0x0B04, ep,
                                                        _report(0x050B, 0x21, raw.to_bytes(2, "little")))
        onoff(1, 1)
        watts(1, 20000)
        watts(2, 20010)                                # EP2 shows socket 1's load too
        live = {r["ep"]: r for r in device_learning.state(dev)["live"]}
        c.check("while it runs, the load shows live on the socket under test",
                live[1]["on"] is True and live[1]["power_w"] == 2000.0 and live[1]["moved"], live)
        c.check("and on any other EP that shows it", live[2]["moved"] and not live[3]["moved"], live)
        onoff(1, 0)
        watts(1, 0)
        watts(2, 0)
        values[(3, 0x0B04)][0x050B] = 19990            # EP3 does not report: the read at the end sees it
        device_learning.capture("00:00:00:00:00:00:00:99", 0x0104, 0x0B04, 1,
                                _report(0x050B, 0x21, (5).to_bytes(2, "little")))
        st = asyncio.run(device_learning.finish(dev, key))
        found = _step(st, key)["proposals"]
        props = {p["path"]: p for p in found if p["path"]}
        notes = " | ".join(p["evidence"] for p in found if not p["path"])
        c.check("every EP showed the load alike: whole device on EP1, the rest repeat it",
                props["endpoints.1.metering"]["value"] == "device_total"
                and props["endpoints.3.metering"]["value"] == "none", props)
        c.check("the cross-talk is spelled out", "EP2, EP3 also showed the load on Socket 1" in notes, notes)
        c.check("and so is whether the reading stopped", "fell to 0 W" in notes, notes)
        c.check("and the kettle gives the scaling",
                props["zmm.measurements.active_power"]["value"]["divisor"] == 10, props)
        order = [p["path"] for p in _step(st, key)["proposals"]]
        st = device_learning.decide(dev, key, [i for i, p in enumerate(order) if p])
        c.check("accepting records learned facts",
                sum(1 for f in zigbee_cache.get_facts(IEEE) if f["source"] == "learned") == 4)
        asyncio.run(device_learning.begin(dev, "metered_outlet.switch_test.2", {"rating_w": 1500}))
        asyncio.run(device_learning.finish(dev, "metered_outlet.switch_test.2"))
        redo = asyncio.run(device_learning.begin(dev, "metered_outlet.switch_test.2", {}))
        c.check("Redo reuses the step's inputs instead of failing",
                redo["success"] and redo["running"] == "metered_outlet.switch_test.2", redo)
        asyncio.run(device_learning.finish(dev, "metered_outlet.switch_test.2"))
        c.check("nothing applies to the device until saved",
                device_profiles._store.get_profile_for_device(model="lumi.plug.aeu002",
                                                              manufacturer="Aqara")["meta"]["source"] == "zmm")

        c.section("review")
        rv = device_learning.review(dev)
        e = rv["entry"]
        c.check("the learned scope is in the entry", e["endpoints"]["1"].get("metering") == "device_total"
                and e["endpoints"]["2"].get("metering") == "none", e["endpoints"])
        c.check("the ZMM entry's own knowledge is carried (labels, blob tags)",
                e["endpoints"]["3"].get("label") == "USB"
                and e["zmm"].get("struct_tags", {}).get("0x97", {}).get("name") == "voltage", e)
        c.check("what the ZMM entry already says is not shown as a change",
                not any(ch["what"].startswith("blob tag") for ch in rv["preview"]), rv["preview"])
        c.check("the preview says what changes",
                any(ch["what"] == "EP1 metering" and ch["to"] == "device_total" for ch in rv["preview"]),
                rv["preview"])

        c.section("save, history, rollback")
        out = device_learning.save(dev, e)
        store = device_profiles._store
        prof = store.get_profile_for_device(model="lumi.plug.aeu002", manufacturer="Aqara")
        c.check("saved as the user's profile for the model",
                out["success"] and prof["meta"]["source"] == "user"
                and prof["endpoints"]["1"]["metering"] == "device_total", out)
        wrong = dict(e, match={"model": "something.else"})
        c.check("an entry for another model is refused", not device_learning.save(dev, wrong)["success"])
        e2 = json.loads(json.dumps(e))
        e2["endpoints"]["3"]["label"] = "USB-A"
        device_learning.save(dev, e2)
        c.check("a second save keeps the first in history", len(store.history(prof["id"])) == 1)
        store.rollback(prof["id"])
        c.check("rollback restores it",
                store.get_profile_for_device(model="lumi.plug.aeu002")["endpoints"]["3"]["label"] == "USB")
        store.rollback(prof["id"])
        c.check("rolling back past the first save returns to the ZMM entry",
                store.get_profile_for_device(model="lumi.plug.aeu002",
                                             manufacturer="Aqara")["meta"]["source"] == "zmm")

        c.section("export and import")
        device_learning.save(dev, e)
        ex = device_learning.export(dev)
        blob = json.dumps(ex)
        c.check("an export carries the entry and what settled it",
                ex["format"] == "zmm-entry/1" and ex["evidence"]
                and ex["entry"]["endpoints"]["1"]["metering"] == "device_total", ex["evidence"])
        c.check("and no IEEE or probe file names", IEEE not in blob and "probes" not in blob)
        c.check("an import for another model is refused",
                not device_learning.import_entry(dev, {**ex, "entry": {**ex["entry"],
                                                       "match": {"model": "x"}}}, apply=False)["success"])
        c.check("a file that is not an export is refused",
                not device_learning.import_entry(dev, {"entry": ex["entry"]}, apply=False)["success"])
        imp = device_learning.import_entry(dev, ex, apply=True)
        c.check("an import previews and saves", imp["success"] and imp.get("saved_profile"), imp)

        asyncio.run(device_learning.end(dev))
        c.check("ending the session stops capture",
                device_learning.state(dev) == {"success": True, "active": False})
    finally:
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
