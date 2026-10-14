"""
What the entry editor writes for an attribute becomes the device's own: a
setting with its labels, unit, range and scale in the settings form, and the
raw key gone from its attribute list.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

from harness import Checker

import modules.device_profiles as device_profiles
import modules.zigbee_cache as zigbee_cache
from modules import zmm_settings
from modules.device_profiles import normalise_profile
from modules.device_profiles_apply import named_raw_keys, transform_state_with_profile

ENTRY = normalise_profile({
    "id": "acme.sensor", "match": {"model": "acme.sensor", "manufacturer": "Acme"},
    "endpoints": {"1": {"clusters": {"0xFCC0": {"attributes": {
        "0x0142": {"name": "presence", "device_class": "occupancy", "value_map": {"0": False, "1": True}}}}}}},
    "zmm": {"settings": [
        {"id": "sampling", "label": "Sampling", "type": "uint8", "ep": 1, "cluster": "0xFCC0", "attr": "0x0170",
         "mfr": "0x115F", "values": {"0": "off", "1": "low", "2": "medium", "3": "high", "4": "custom"},
         "description": "How often it samples"},
        {"id": "sampling_period", "label": "Sampling period", "type": "uint32", "ep": 1, "cluster": "0xFCC0",
         "attr": "0x0162", "mfr": "0x115F", "scale": 1000, "unit": "sec", "min": 0.5, "max": 3600, "step": 0.5,
         "values": {}}]},
})


class _Store:
    def get_profile_for_device(self, **_):
        return ENTRY

    def get_ieee_mappings(self, _):
        return {}


def run() -> Checker:
    c = Checker("named_attributes")
    facts = [{"endpoint_id": 1, "subject": f"reports:0xFCC0/0x{a:04X}@0x115F", "source": "observed",
              "value": json.dumps({"last": v, "nonzero": bool(v)})} for a, v in ((0x0170, 1), (0x0162, 600000))]
    real = (device_profiles._store, zigbee_cache.get_facts, zigbee_cache._db)
    device_profiles._store, zigbee_cache.get_facts, zigbee_cache._db = _Store(), (lambda _i: facts), object()
    try:
        dev = NS(ieee="aa:bb:00:00:00:00:00:09", handlers={},
                 state={"opple_0x0170": 1, "opple_0x0162": 600000, "opple_0x0142": 1, "opple_0x0199": 0},
                 zigpy_dev=NS(model="acme.sensor", manufacturer="Acme", endpoints={0: None, 1: NS(in_clusters={})}))

        c.section("in the settings form")
        opts = {o["name"]: o for o in zmm_settings.options(dev)}
        sampling, period = opts.get("sampling", {}), opts.get("sampling_period", {})
        c.check("one of a list is a choice of its named values, not a number",
                sampling.get("type") == "select"
                and [o["label"] for o in sampling["options"]] == ["off", "low", "medium", "high", "custom"], sampling)
        c.check("a number carries its unit, range and step in the units a person uses",
                (period.get("type"), period.get("unit"), period.get("min"), period.get("max"), period.get("step"))
                == ("number", "sec", 0.5, 3600, 0.5), period)
        c.check("each shows the device's value: the choice it holds, the number divided by its scale",
                sampling.get("current_value") == 1 and period.get("current_value") == 600.0, (sampling, period))

        c.section("in the device's state")
        out = transform_state_with_profile(dev, dev.state)
        c.check("a report is filed under the setting's name, scaled",
                out.get("sampling_period") == 600.0 and out.get("sampling") == 1, out)
        c.check("a named reading is published as its value means", out.get("presence") is True, out)
        c.check("the raw keys it names are listed for the views to hide; one it does not name is not",
                named_raw_keys(dev) == ["opple_0x0142", "opple_0x0162", "opple_0x0170"], named_raw_keys(dev))
    finally:
        device_profiles._store, zigbee_cache.get_facts, zigbee_cache._db = real
    return c


if __name__ == "__main__":
    run()
