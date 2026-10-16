"""
What a live report adds beside its raw keys (derived_changes): a named
reading that moved, and a binary one the report carries even unmoved.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from harness import Checker

import modules.device_profiles as device_profiles
from modules.device_profiles import normalise_profile
from modules.device_profiles_apply import derived_changes

ENTRY = normalise_profile({
    "id": "acme.presence", "match": {"model": "acme.presence", "manufacturer": "Acme"},
    "endpoints": {"1": {"clusters": {"0xFCC0": {"attributes": {
        "0x0142": {"name": "occupancy", "device_class": "occupancy", "value_map": {"1": True, "0": False}},
        "0x014D": {"name": "pir_detection", "device_class": "motion", "value_map": {"1": True, "0": False}},
        "0x0150": {"name": "distance", "scale": 100}}}}}},
})


class _Store:
    def get_profile_for_device(self, **_):
        return ENTRY

    def get_ieee_mappings(self, _):
        return {}


def _report(dev, raw):
    """As update_state then handle_device_update see it: state holds the raw
    keys, the derived ones are added after."""
    dev.state.update(raw)
    extras = derived_changes(dev, raw)
    dev.state.update(extras)
    return extras


def run() -> Checker:
    c = Checker("derived_changes")
    real = device_profiles._store
    device_profiles._store = _Store()
    try:
        dev = NS(ieee="aa:bb:00:00:00:00:00:0a", handlers={}, state={},
                 zigpy_dev=NS(model="acme.presence", manufacturer="Acme", endpoints={}))

        c.section("a presence sensor")
        on = _report(dev, {"opple_0x014d": 1, "opple_0x0142": 1})
        c.check("walking in reads as occupancy and motion",
                on == {"occupancy": True, "pir_detection": True}, on)

        # A restart resets the event keys but keeps the raw value it last had.
        dev.state.update(occupancy=False)
        off = _report(dev, {"opple_0x014d": 0, "opple_0x0142": 0})
        c.check("after a restart's reset, the clear still carries occupancy",
                off == {"occupancy": False, "pir_detection": False}, off)

        pir = _report(dev, {"opple_0x014d": 1})
        c.check("a report of one reading carries only that one", pir == {"pir_detection": True}, pir)

        c.section("a reading that is not an event")
        _report(dev, {"opple_0x0150": 120})
        same = _report(dev, {"opple_0x0150": 120})
        c.check("a repeated distance adds nothing", same == {}, same)
        moved = _report(dev, {"opple_0x0150": 250})
        c.check("a moved one is added, scaled", moved == {"distance": 2.5}, moved)
    finally:
        device_profiles._store = real
    return c


if __name__ == "__main__":
    run()
