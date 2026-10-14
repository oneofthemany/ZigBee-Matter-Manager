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

    types = {}          # {attr: datatype} where not a uint8

    async def read_attributes_raw(self, attrs, manufacturer=None):
        return NS(status_records=[
            NS(attrid=a, status=0, value=NS(value=self.values[a], type=self.types.get(a, 0x20)))
            for a in attrs if a in self.values])

    listed = None       # {attr: (datatype, acl)} once the device is awake to say
    read_only = ()      # attributes that refuse a write
    written = None      # [(attr, value)] the device was sent

    async def write_attributes_raw(self, attrs, manufacturer=None):
        if self.written is None:
            raise asyncio.TimeoutError
        self.written += [(a.attrid, int(a.value.value)) for a in attrs]
        return [[NS(status=0x88 if a.attrid in self.read_only else 0) for a in attrs]]

    async def discover_attributes_extended(self, start, count, manufacturer=None):
        if not self.listed or manufacturer is None:
            raise asyncio.TimeoutError
        return NS(discovery_complete=True, extended_attr_info=[
            NS(attrid=a, datatype=t, acl=acl) for a, (t, acl) in sorted(self.listed.items()) if a >= start])


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


PRESENCE_IEEE = "54:ef:44:10:01:65:58:b3"


def _presence_sensor(ieee=PRESENCE_IEEE):
    """A battery presence sensor that says so only on the maker's cluster."""
    ep = NS(endpoint_id=1, profile_id=0x0104, device_type=0x0107, out_clusters={})
    ep.in_clusters = {c: _Cluster(c, ep, {}) for c in (0x0000, 0x0001, 0x0400, 0x0402, 0xFCC0)}
    zdev = NS(endpoints={0: None, 1: ep}, node_desc=NS(is_mains_powered=False, manufacturer_code=0x115F),
              model="presence.unknown", manufacturer="Acme")   # no shipped entry
    return NS(ieee=ieee, zigpy_dev=zdev, state={}, handlers={})


def _vendor_report(attr, dtype, value: bytes) -> bytes:
    return bytes([0x1C, 0x5F, 0x11, 0x01, 0x0A]) + attr.to_bytes(2, "little") + bytes([dtype]) + value


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
        c.check("the outlet gets a switch test per socket and USB, a press per button, a setting "
                "and the metering map",
                keys == ["buttons.press.1", "buttons.press.2", "manufacturer_setting.from_history",
                         "manufacturer_setting.known_values", "manufacturer_setting.change_setting",
                         "manufacturer_setting.set_number",
                         "manufacturer_setting.try_setting", "manufacturer_setting.try_behaviour", "metered_outlet.switch_test.1",
                         "metered_outlet.switch_test.2", "metered_outlet.switch_test.3",
                         "metering_map"], keys)
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

        c.check("a device that is not an occupancy sensor is not asked to walk past it",
                not any(k.startswith("presence.") for k in keys + relay_keys), keys)
        c.check("a recipe may be offered by declared device type; a malformed one is rejected",
                validate({"id": "x", "applies_when": {"device_types_any": ["0x0107"]}, "steps": [
                    {"id": "s", "expect": "clear",
                     "infer": [{"op": "occupancy_attribute", "yields": "endpoints.*.clusters"}]}]})
                and validate({"id": "x", "applies_when": {"device_types_any": ["sensor"]}, "steps": [
                    {"id": "s", "infer": [{"op": "occupancy_attribute", "yields": "device_type"}]}]}) is None
                and validate({"id": "x", "steps": [{"id": "s", "expect": "sideways", "infer": [
                    {"op": "occupancy_attribute", "yields": "device_type"}]}]}) is None)

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
        watts(1, 2000)
        watts(2, 2001)                                 # EP2 is socket 1's reading (+ USB)
        live = {r["ep"]: r for r in device_learning.state(dev)["live"]}
        c.check("while it runs, the load shows live on the socket under test",
                live[1]["on"] is True and live[1]["power_w"] == 2000.0 and live[1]["moved"], live)
        c.check("and on any other EP that shows it", live[2]["moved"] and not live[3]["moved"], live)
        onoff(1, 0)
        watts(1, 0)
        watts(2, 0)
        device_learning.capture("00:00:00:00:00:00:00:99", 0x0104, 0x0B04, 1,
                                _report(0x050B, 0x21, (5).to_bytes(2, "little")))
        st = asyncio.run(device_learning.finish(dev, key))
        found = _step(st, key)["proposals"]
        props = {p["path"]: p for p in found if p["path"]}
        notes = " | ".join(p["evidence"] for p in found if not p["path"])
        c.check("one test proposes no metering by itself", "endpoints.1.metering" not in props, props)
        c.check("the cross-talk is spelled out", "EP2 also showed the load on Socket 1" in notes, notes)
        c.check("and so is whether the reading stopped", "fell to 0 W" in notes, notes)
        c.check("and the kettle gives the scaling",
                props["zmm.measurements.active_power"]["value"]["divisor"] == 1, props)
        order = [p["path"] for p in _step(st, key)["proposals"]]
        device_learning.decide(dev, key, [i for i, p in enumerate(order) if p])
        prov = _step(st, "metering_map")["proposals"]
        c.check("after one test the map is provisional",
                any("not tested yet" in p["evidence"] for p in prov), prov)

        def test(ep, on_eps_values, rating):
            k = f"metered_outlet.switch_test.{ep}"
            asyncio.run(device_learning.begin(dev, k, {"rating_w": rating}))
            onoff(ep, 1)
            for e, raw in on_eps_values:
                watts(e, raw)
            onoff(ep, 0)
            for e, _ in on_eps_values:
                watts(e, 0)
            return asyncio.run(device_learning.finish(dev, k))
        test(2, [(1, 1500), (3, 1500)], 1500)          # socket 2 shows on EP1 (total) and EP3
        st = test(3, [(1, 10), (2, 10)], 10)           # USB shows on EP1 and EP2, with socket 1
        m = {p["path"]: p["value"] for p in _step(st, "metering_map")["proposals"] if p["path"]}
        c.check("with every socket tested, the map is the outlet's real wiring",
                m == {"endpoints.1.metering": "device_total", "endpoints.2.metering": "measures:1,3",
                      "endpoints.3.metering": "measures:2"}, m)
        mp = _step(st, "metering_map")["proposals"]
        device_learning.decide(dev, "metering_map", [i for i, p in enumerate(mp) if p["path"]])
        learned = {f["subject"] for f in zigbee_cache.get_facts(IEEE) if f["source"] == "learned"}
        c.check("accepting the map records it", {"learned:endpoints.1.metering",
                                                  "learned:endpoints.2.metering",
                                                  "learned:endpoints.3.metering"} <= learned, learned)
        redo = asyncio.run(device_learning.begin(dev, "metered_outlet.switch_test.2", {}))
        c.check("Redo reuses the step's inputs instead of failing",
                redo["success"] and redo["running"] == "metered_outlet.switch_test.2", redo)
        asyncio.run(device_learning.finish(dev, "metered_outlet.switch_test.2"))
        c.check("the map step cannot be run itself",
                not asyncio.run(device_learning.begin(dev, "metering_map", {}))["success"])
        c.check("nothing applies to the device until saved",
                device_profiles._store.get_profile_for_device(model="lumi.plug.aeu002",
                                                              manufacturer="Aqara")["meta"]["source"] == "zmm")

        c.section("review")
        rv = device_learning.review(dev)
        e = rv["entry"]
        c.check("the learned map is in the entry", e["endpoints"]["1"].get("metering") == "device_total"
                and e["endpoints"]["2"].get("metering") == "measures:1,3"
                and e["endpoints"]["3"].get("metering") == "measures:2", e["endpoints"])
        c.check("the ZMM entry's own knowledge is carried (labels, blob tags)",
                e["endpoints"]["3"].get("label") == "USB"
                and e["zmm"].get("struct_tags", {}).get("0x95", {}).get("name") == "energy", e)
        c.check("what the ZMM entry already says is not shown as a change",
                not any(ch["what"].startswith("blob tag") for ch in rv["preview"]), rv["preview"])
        c.check("the preview says what changes",
                any(ch["what"] == "EP1 metering" and ch["to"] == "device_total" for ch in rv["preview"]),
                rv["preview"])

        c.section("the entry editor")
        ctx = rv["device"]
        c.check("the editor is told every EP and what each could measure",
                [x["ep"] for x in ctx["endpoints"]] == [1, 2, 3]
                and ctx["measurements"]["active_power"] == {"cluster": "0x0B04", "attr": "0x050B", "ep": 1}
                and ctx["model"] == "lumi.plug.aeu002", ctx)
        hand = json.loads(json.dumps(e))
        hand["endpoints"]["2"]["metering"] = "measures:1"
        hand["endpoints"]["3"]["metering"] = "measures:2,x"
        pv = device_learning.preview_entry(dev, hand)
        c.check("a hand edit previews as saving would store it: a malformed scope is dropped",
                pv["entry"]["endpoints"]["2"]["metering"] == "measures:1"
                and "metering" not in pv["entry"]["endpoints"]["3"], pv["entry"]["endpoints"])
        c.check("and the preview says what it changes",
                any(ch["what"] == "EP2 metering" and ch["to"] == "measures:1" for ch in pv["preview"]),
                pv["preview"])
        c.check("something that is not an entry is refused",
                not device_learning.preview_entry(dev, ["x"])["success"])

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

        c.section("a presence sensor, taught by walking in and out")
        from modules import device_class
        from modules.device_profiles_apply import build_discovery_configs, transform_state_with_profile
        pres = _presence_sensor()
        for m in (device_facts, device_decisions):
            m.forget(PRESENCE_IEEE)
        c.check("before learning it is whatever else it measures",
                device_class.device_class(pres, refresh=True).kind == "temperature_sensor")
        pkeys = [s["key"] for s in device_learning.start(pres)["steps"]]
        c.check("it is asked to walk in, then out", [k for k in pkeys if k.startswith("presence.")]
                == ["presence.walk_in", "presence.walk_out"], pkeys)
        says = lambda v: device_learning.capture(PRESENCE_IEEE, 0x0104, 0xFCC0, 1,
                                                 _vendor_report(0x0142, 0x20, bytes([v])))
        asyncio.run(device_learning.begin(pres, "presence.walk_in", {}))
        device_learning.capture(PRESENCE_IEEE, 0x0104, 0x0400, 1,
                                _report(0x0000, 0x21, (16721).to_bytes(2, "little")))
        says(1)
        st = asyncio.run(device_learning.finish(pres, "presence.walk_in"))
        found = _step(st, "presence.walk_in")["proposals"]
        PATH = "endpoints.1.clusters.0xFCC0.attributes.0x0142"
        c.check("asleep and unread, its first report on walking in is still offered",
                [p["path"] for p in found] == [PATH, "device_type"]
                and found[0]["confidence"] == "medium", found)
        asyncio.run(device_learning.begin(pres, "presence.walk_out", {}))
        says(0)
        st = asyncio.run(device_learning.finish(pres, "presence.walk_out"))
        found = _step(st, "presence.walk_out")["proposals"]
        c.check("walking out is compared with what walking in showed: now certain",
                found[0]["path"] == PATH and found[0]["confidence"] == "high"
                and "went 1 -> 0" in found[0]["evidence"], found)
        device_learning.decide(pres, "presence.walk_out", [0, 1])
        rv = device_learning.review(pres)
        c.check("the review names the reading and the device, and says so before saving",
                rv["entry"]["device_type"] == "motion_sensor"
                and any(ch["what"] == "EP1 0xFCC0/0x0142 reads as" and ch["to"] == "occupancy"
                        for ch in rv["preview"])
                and any(ch["what"] == "device type" and ch["to"] == "motion_sensor"
                        for ch in rv["preview"]), rv["preview"])
        c.check("a sensor's draft never calls it a switch",
                device_learning.review(_presence_sensor("aa:02"))["entry"]["device_type"] == "generic")
        saved = device_learning.save(pres, rv["entry"])
        device_class.invalidate(pres)
        c.check("saved, it is a motion sensor", saved["success"]
                and device_class.device_class(pres).kind == "motion_sensor", saved)
        out = transform_state_with_profile(pres, {"opple_0x0142": 1})
        c.check("the maker's raw value now reads as occupancy", out.get("occupancy") is True, out)
        c.check("and clears", transform_state_with_profile(pres, {"opple_0x0142": 0}).get("occupancy") is False)
        c.check("a Bool reads the same as a number",
                transform_state_with_profile(pres, {"opple_0x0142": True}).get("occupancy") is True)
        cfg = [x for x in build_discovery_configs(pres) if x["object_id"] == "occupancy"]
        c.check("Home Assistant gets an occupancy sensor whose state it can match",
                len(cfg) == 1 and cfg[0]["component"] == "binary_sensor"
                and cfg[0]["config"]["device_class"] == "occupancy"
                and cfg[0]["config"]["value_template"] == "{{ 'ON' if value_json.occupancy else 'OFF' }}",
                cfg)
        c.section("a battery device's own attributes: woken, asked, then matched to known values")
        c.check("a battery device with a maker's cluster is offered a wake-and-discover step; "
                "a mains one, which is probed by itself, is not",
                "discover.vendor_attributes" in pkeys and "discover.vendor_attributes" not in keys, pkeys)
        st = asyncio.run(device_learning.begin(pres, "manufacturer_setting.known_values",
                                               {"known": "schedule_time_raw: 589845"}))
        c.check("with nothing known of its attributes, matching says to discover first",
                "Wake and discover" in _step(st, "manufacturer_setting.known_values")["proposals"][0]["evidence"])
        async def discover():
            """Start the step, as the page does, and wait for the device's answer."""
            started = await device_learning.begin(pres, "discover.vendor_attributes", {})
            again = await device_learning.begin(pres, "discover.vendor_attributes", {})
            early = await device_learning.finish(pres, "discover.vendor_attributes")
            await device_learning._sessions[PRESENCE_IEEE].active["task"]
            return started, again, early, device_learning.state(pres)

        started, again, early, st = asyncio.run(discover())
        c.check("starting it answers at once, showing the step running while the device is asked",
                started["running"] == "discover.vendor_attributes" and started["live"] == []
                and _step(started, "discover.vendor_attributes")["status"] == "running", started)
        c.check("a second press, or Done, while it runs neither restarts it nor fails",
                again["running"] == early["running"] == "discover.vendor_attributes"
                and again["success"] and early["success"])
        found = _step(st, "discover.vendor_attributes")
        c.check("asleep, it does not answer: the step says to wake it and stays open",
                st["running"] is None and found["status"] == "pending"
                and "press its button" in found["proposals"][0]["evidence"], found)
        fcc0 = pres.zigpy_dev.endpoints[1].in_clusters[0xFCC0]
        fcc0.listed = {0x0142: (0x20, 0x05), 0x0162: (0x23, 0x07), 0x023E: (0x23, 0x07),
                       0x0163: (0x23, 0x07), 0x016A: (0x23, 0x07)}
        fcc0.values.update({0x0142: 0, 0x0162: 600000, 0x023E: 589845, 0x0163: 3600000, 0x016A: 3600000})
        # What this kind of sensor does: its functions are reported, never listed.
        device_learning.capture(PRESENCE_IEEE, 0x0104, 0xFCC0, 1, _vendor_report(0x014F, 0x20, bytes([30])))
        device_facts.record(PRESENCE_IEEE, [device_facts.Fact(
            1, "reports:0xFCC0/0x014F@0x115F", "observed", json.dumps({"nonzero": True, "last": 30}))])
        asyncio.run(zigbee_cache.drain())
        fcc0.values[0x014F], fcc0.types = 3500, {0x014F: 0x21}
        st = asyncio.run(discover())[3]
        found = _step(st, "discover.vendor_attributes")
        c.check("awake, it lists what it has, what can be written, and its values are read",
                st["running"] is None and found["status"] == "done"
                and "EP1 0xFCC0 lists 5 attributes, 4 writable; 5 read"
                in found["proposals"][0]["evidence"], found)
        c.check("an attribute it reports but does not list is read too, and said to be of unknown access",
                "also reports 1 attributes it does not list (1 read)" in found["proposals"][1]["evidence"],
                found)
        st = asyncio.run(device_learning.begin(pres, "manufacturer_setting.known_values",
                                               {"known": "absence_delay: 35"}))
        found = _step(st, "manufacturer_setting.known_values")["proposals"]
        c.check("its fresh value is what later steps match against: a reading, its access unknown",
                [(p["path"], p["value"]) for p in found if p["path"]]
                == [("endpoints.1.clusters.0xFCC0.attributes.0x014F", {"name": "absence_delay", "scale": 100})]
                and "not listed by the device" in found[0]["evidence"], found)

        c.section("whether an unlisted attribute can be written: its own value, written back")
        async def probe():
            await device_learning.begin(pres, "discover.write_access", {})
            await device_learning._sessions[PRESENCE_IEEE].active["task"]
            return device_learning.state(pres)

        c.check("the step follows discovery", pkeys.index("discover.write_access")
                == pkeys.index("discover.vendor_attributes") + 1, pkeys)
        st = asyncio.run(probe())
        found = _step(st, "discover.write_access")
        c.check("a device that does not answer the write is recorded as nothing known",
                found["status"] == "done" and "gave no answer" in found["proposals"][0]["evidence"], found)
        fcc0.written = []
        st = asyncio.run(probe())
        found = _step(st, "discover.write_access")
        c.check("only the unlisted attribute is written, with exactly the value just read",
                fcc0.written == [(0x014F, 3500)], fcc0.written)
        c.check("accepted: it can be written",
                "1 can be written" in found["proposals"][0]["evidence"]
                and "0x014F" in found["proposals"][0]["evidence"], found)
        st = asyncio.run(device_learning.begin(pres, "manufacturer_setting.known_values",
                                               {"known": "absence_delay: 35"}))
        got = [p["value"] for p in _step(st, "manufacturer_setting.known_values")["proposals"]
               if p["path"] == "zmm.settings"]
        c.check("so a known value now names it as a setting, with its type and scale",
                [(x["id"], x["attr"], x["type"], x.get("scale")) for x in got]
                == [("absence_delay", "0x014F", "uint16", 100)],
                _step(st, "manufacturer_setting.known_values")["proposals"])
        fcc0.read_only, fcc0.written = (0x014F,), []
        device_facts.record(PRESENCE_IEEE, [device_facts.Fact(
            1, "attr:0xFCC0/0x014F@0x115F", "answered",
            json.dumps({"type": "0x21/uint16", "acl": None, "value": 3500}))])
        asyncio.run(zigbee_cache.drain())
        st = asyncio.run(probe())
        c.check("refused: it is read-only, and says so",
                "1 are read-only" in _step(st, "discover.write_access")["proposals"][0]["evidence"],
                _step(st, "discover.write_access"))
        again = _step(asyncio.run(discover())[3], "discover.vendor_attributes")
        c.check("discovering again does not forget what the probe settled",
                "which can be written is already known" in again["proposals"][1]["evidence"], again)
        st = asyncio.run(probe())
        c.check("with every attribute's access known, there is nothing left to try",
                fcc0.written == [(0x014F, 3500)]
                and "nothing to try" in _step(st, "discover.write_access")["proposals"][0]["evidence"],
                _step(st, "discover.write_access"))

        st = asyncio.run(device_learning.begin(pres, "manufacturer_setting.known_values", {"known": json.dumps(
            {"schedule_time_raw": 589845, "temp_and_humidity_sampling_period": 600,
             "temp_reporting_interval": 3600, "led_disabled_night": False})}))
        found = _step(st, "manufacturer_setting.known_values")["proposals"]
        got = {p["value"]["id"]: p["value"] for p in found if p["path"] == "zmm.settings"}
        c.check("a known value names the one attribute holding it, as a setting since it can be written",
                got.get("schedule_time_raw", {}).get("attr") == "0x023E"
                and got["schedule_time_raw"]["type"] == "uint32", found)
        c.check("and one stored in other units is found with its scale: 600 s held as 600000",
                (got.get("temp_and_humidity_sampling_period", {}).get("attr"),
                 got["temp_and_humidity_sampling_period"].get("scale")) == ("0x0162", 1000), found)
        notes = " | ".join(p["evidence"] for p in found if not p["path"])
        c.check("a value two attributes hold is listed with both, not guessed",
                "temp_reporting_interval" not in got
                and "temp_reporting_interval = 3600 fits EP1 0xFCC0/0x0163, EP1 0xFCC0/0x016A" in notes, notes)
        c.check("an on/off value is named as not matched", "led_disabled_night" in notes, notes)
        device_learning.decide(pres, "manufacturer_setting.known_values",
                               [i for i, p in enumerate(found) if p["path"]])
        rv = device_learning.review(pres)
        c.check("accepted, they are settings in the entry the review would save",
                {x["id"] for x in rv["entry"]["zmm"]["settings"]}
                == {"schedule_time_raw", "temp_and_humidity_sampling_period"}, rv["entry"]["zmm"])
        asyncio.run(device_learning.end(pres))

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
            m.forget(PRESENCE_IEEE)
        shutil.rmtree(tmp, ignore_errors=True)
    return c


if __name__ == "__main__":
    run()
