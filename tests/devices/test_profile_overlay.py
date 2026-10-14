"""
A user's profile saved over a shipped entry is an edit of it, laid over it:
a tool that saves only part of an entry cannot drop the rest.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from harness import Checker

from modules.device_profiles import ProfileStore

ENTRY = {
    "id": "lumi.sensor_occupy.agl8", "match": {"model": "lumi.sensor_occupy.agl8", "manufacturer": "Aqara"},
    "device_type": "motion_sensor", "capabilities": ["motion_sensor", "battery"],
    "endpoints": {"1": {"label": "Sensor", "role": "controller", "clusters": {"0xFCC0": {"attributes": {
        "0x0142": {"name": "occupancy", "device_class": "occupancy", "capability": "motion",
                   "value_map": {"1": True, "0": False}}}}}}},
    "zmm": {"struct_tags": {"0x17": {"name": "battery_voltage", "scale": 0.001}, "0x05": None},
            "settings": [{"id": "led", "label": "LED", "type": "bool", "ep": 1, "cluster": "0xFCC0",
                          "attr": "0x0203", "values": {}}]},
    "meta": {"source": "zmm"},
}

# What the Profile tab used to save from it: its table's fields, defaults for the rest.
SAVED = {
    "id": "lumi.sensor_occupy.agl8", "match": {"model": "lumi.sensor_occupy.agl8", "manufacturer": "Aqara"},
    "device_type": "generic", "capabilities": ["humidity_sensor"],
    "endpoints": {"1": {"role": "primary", "label": "", "clusters": {"0xFCC0": {"attributes": {
        "0x0142": {"name": "presence", "device_class": "occupancy"},
        "0x014D": {"name": "pir_detection", "device_class": "motion"}}}}}},
    "zmm": {"struct_tags": {"0x05": {"name": "events", "scale": 1}, "0x17": None},
            "settings": [{"id": "sensitivity", "label": "Sensitivity", "type": "uint8", "ep": 1,
                          "cluster": "0xFCC0", "attr": "0x010C", "values": {}}]},
    "meta": {"source": "user"},
}


def run() -> Checker:
    c = Checker("profile_overlay")
    d = Path(tempfile.mkdtemp())
    for sub, profile in (("z", ENTRY), ("u", SAVED)):
        (d / sub).mkdir()
        (d / sub / "lumi.sensor_occupy.agl8.json").write_text(json.dumps(profile))
    store = ProfileStore(user_dir=str(d / "u"), bundled_dir=str(d / "b"),
                         ieee_overrides_file=str(d / "i.json"), zmm_dir=str(d / "z"))
    p = store.get_profile_for_device(model="lumi.sensor_occupy.agl8", manufacturer="Aqara")
    a = p["endpoints"]["1"]["clusters"]["0xFCC0"]["attributes"]

    c.section("a partial save laid over the entry")
    c.check("what the user changed wins: the reading's new name", a["0x0142"]["name"] == "presence", a)
    c.check("what their tool did not carry is kept: which value means occupied",
            a["0x0142"]["value_map"] == {"1": True, "0": False} and a["0x0142"]["capability"] == "motion", a)
    c.check("and what they added is there", a["0x014D"]["name"] == "pir_detection", a)
    c.check("defaults the save filled in do not overwrite the entry: its name, role and type",
            (p["endpoints"]["1"]["label"], p["endpoints"]["1"]["role"], p["device_type"])
            == ("Sensor", "controller", "motion_sensor"), p["endpoints"]["1"])
    c.check("capabilities are the union", set(p["capabilities"]) >= {"motion_sensor", "battery", "humidity_sensor"},
            p["capabilities"])
    tags = p["zmm"]["struct_tags"]
    c.check("a blob tag the user named or dropped is as they said",
            tags["0x05"] == {"name": "events", "scale": 1} and tags["0x17"] is None, tags)
    c.check("settings merge by id", {x["id"] for x in p["zmm"]["settings"]} == {"led", "sensitivity"},
            p["zmm"]["settings"])
    c.check("it is still the user's profile", p["meta"]["source"] == "user")
    c.check("looked up by id, the same", store.get_profile("lumi.sensor_occupy.agl8") == p)

    c.section("what is not an edit of an entry")
    (d / "u" / "other.json").write_text(json.dumps({**SAVED, "id": "other", "match": {"model": "other"}}))
    store = ProfileStore(user_dir=str(d / "u"), bundled_dir=str(d / "b"),
                         ieee_overrides_file=str(d / "i.json"), zmm_dir=str(d / "z"))
    o = store.get_profile_for_device(model="other")
    c.check("a user profile under its own id stands alone",
            "value_map" not in o["endpoints"]["1"]["clusters"]["0xFCC0"]["attributes"]["0x0142"]
            and o["device_type"] == "generic", o)
    store.delete_profile("lumi.sensor_occupy.agl8")
    c.check("deleting the edit returns to the entry as shipped",
            store.get_profile_for_device(model="lumi.sensor_occupy.agl8", manufacturer="Aqara")
            ["endpoints"]["1"]["clusters"]["0xFCC0"]["attributes"]["0x0142"]["name"] == "occupancy")
    return c


if __name__ == "__main__":
    run()
