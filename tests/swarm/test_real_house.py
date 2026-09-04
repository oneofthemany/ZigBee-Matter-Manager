"""
End-to-end check against a real household's device shapes.

    python3 tests/swarm/test_real_house.py

Every device here is modelled on one that actually exists on a live network,
including the awkward parts: sensors that advertise an OnOff cluster for
binding and therefore get an on/off command they will never honour, a metering
socket that advertises a level cluster it does not use, and a dual-gang socket
that reports both an aggregate and per-outlet readings.

These shapes are what turned door sensors into switches and motion sensors into
lights, and produced suggestions like "when Sean arrives home, turn on Door -
Balcony". This file exists so that class of nonsense cannot come back.
"""

from __future__ import annotations

import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker, FakeCapabilities, FakeDevice  # noqa: E402

from modules.swarm import suggestions as sg  # noqa: E402
from modules.swarm.network import describe_network  # noqa: E402
from modules.swarm.stigmergy import StigmergyStore  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
STORE = StigmergyStore(bundled_dir=str(REPO / "modules" / "swarm" / "patterns"),
                       user_dir="/nonexistent")

ONOFF = [{"command": "on", "label": "On", "endpoint_id": 1},
         {"command": "off", "label": "Off", "endpoint_id": 1},
         {"command": "toggle", "label": "Toggle", "endpoint_id": 1}]
DIM = ONOFF + [{"command": "brightness", "label": "Brightness", "endpoint_id": 1}]
COLOUR = DIM + [{"command": "color_temp", "label": "Colour", "endpoint_id": 1}]
COVER = [{"command": "open", "endpoint_id": 1}, {"command": "close", "endpoint_id": 1},
         {"command": "stop", "endpoint_id": 1}, {"command": "position", "endpoint_id": 1}]
TRV = [{"command": "temperature", "label": "Setpoint", "endpoint_id": 1}]

ROOMS = {"bathroom": "Bathroom", "elena": "Elena's Room", "ensuite": "Ensuite",
         "hallway": "Hallway", "kitchen": "Kitchen", "living": "Living Room",
         "master": "Master Bedroom"}


def _house():
    """The devices, with the quirks their stacks actually apply.

    An Aqara magnet and a Hue SML both advertise OnOff, and Zigbee capability
    detection discards the resulting switch/light claims. The command list does
    not, which is the whole point of the fixture: `commands=ONOFF` on a sensor
    is not a mistake here, it is what the device really offers.
    """
    d = {}

    def add(ieee, name, room, state, caps, commands=None):
        d[ieee] = FakeDevice(ieee, name, state, commands=commands or [],
                             capabilities=FakeCapabilities(caps))
        d[ieee]._room = room

    # Aqara door contacts. The lumi quirk discards switch/light; the OnOff
    # binding cluster still yields on/off commands.
    for ieee, name, room in (
            ("00:15:8d:00:06:a2:16:1a", "Door - Bathroom", "bathroom"),
            ("00:15:8d:00:06:a2:15:9e", "Door - Elena", "elena"),
            ("00:15:8d:00:07:e0:60:11", "Door - Ensuite", "ensuite"),
            ("00:15:8d:00:06:a2:89:46", "Door - Balcony", "kitchen")):
        add(ieee, name, room, {"contact": True, "battery": 95, "voltage": 3000},
            ["contact_sensor", "battery"], ONOFF)

    # Hue SML motion sensors: the quirk discards switch/light/on_off, but the
    # controller endpoint still produces on/off/brightness commands.
    for ieee, name, room in (
            ("00:17:88:01:09:16:37:1b", "Motion - Bathroom", "bathroom"),
            ("00:17:88:01:09:16:33:33", "Motion - Kitchen", "kitchen"),
            ("00:17:88:01:09:16:54:fb", "Motion - Master", "master")):
        add(ieee, name, room,
            {"occupancy": False, "illuminance_lux": 42, "temperature": 20.1,
             "battery": 88},
            ["motion_sensor", "occupancy_sensing", "illuminance_sensor",
             "temperature_sensor", "battery"], DIM)

    # A Thread contact sensor, declared a motion sensor by the IAS Zone guess.
    add("e4:56:ac:ff:fe:4c:d0:77", "Contact - Side Door", None,
        {"contact": True, "tamper": False, "zone_status": 0, "battery_low": False},
        ["ias_zone", "motion_sensor", "battery"], ONOFF)

    add("34:10:f4:ff:fe:e3:dd:6c", "Blinds - Elena", "elena",
        {"position": 100}, ["window_covering", "cover"], COVER)
    add("34:10:f4:ff:fe:e9:2f:cf", "Blinds - Master", "master",
        {"position": 0}, ["window_covering", "cover"], COVER)

    add("f0:82:c0:ff:fe:6f:0c:76", "Pendant - Elena", "elena",
        {"state": "OFF", "brightness": 200, "color_temp": 300},
        ["light", "on_off", "level_control", "color_control"], COLOUR)
    add("54:ef:44:10:01:12:a9:2e", "Pendant - Living", "living",
        {"state": "ON", "brightness": 254, "color_temp": 366},
        ["light", "on_off", "level_control", "color_control"], COLOUR)
    for ieee, name in (("60:b6:47:ff:fe:21:11:0c", "Pendant - Hallway 1"),
                       ("60:b6:47:ff:fe:21:05:d4", "Pendant - Hallway 2")):
        add(ieee, name, "hallway", {"state": "OFF", "brightness": 180},
            ["light", "on_off", "level_control"], DIM)
    add("28:76:81:ff:fe:bf:96:01", "Pendant - Master", "master",
        {"state": "OFF", "brightness": 254}, ["light", "on_off", "level_control"], DIM)
    add("00:17:88:01:08:25:60:5d", "Lamp - Living", "living",
        {"state": "OFF", "brightness": 120}, ["light", "on_off", "level_control"], DIM)

    # A metering socket advertising a level cluster it does not use, reporting
    # both an aggregate and per-outlet readings.
    add("00:15:8d:00:02:56:f8:bf", "Socket - Media", "living",
        {"state": "ON", "state_1": "ON", "state_2": "OFF", "on": True,
         "on_1": True, "on_2": False, "brightness": 254, "brightness_1": 254,
         "level": 254, "level_1": 254, "power_1": 43.2, "power_2": 0.4,
         "current_1": 0.2, "current_2": 0.0, "voltage_1": 241, "voltage_2": 241},
        ["on_off", "switch", "light", "level_control", "metering",
         "power_monitoring", "multi_endpoint", "multi_switch"],
        ONOFF + [{"command": "on", "endpoint_id": 2},
                 {"command": "off", "endpoint_id": 2},
                 {"command": "brightness", "endpoint_id": 1}])

    for ieee, name in (("5c:02:72:ff:fe:c6:18:f5", "Socket - Kitchen Left"),
                       ("60:a4:23:ff:fe:9d:98:4d", "Socket - Kitchen Right")):
        add(ieee, name, "kitchen", {"state": "OFF", "power": 0.0},
            ["on_off", "switch", "metering", "power_monitoring"], ONOFF)

    for ieee, name, room in (
            ("00:1e:5e:09:02:a3:e7:27", "Thermostat - Elena", "elena"),
            ("00:1e:5e:09:02:a3:e4:c1", "Thermostat - Living", "living"),
            ("00:1e:5e:09:02:a4:40:5a", "Receiver - Elena", "hallway"),
            ("00:1e:5e:09:02:a4:49:4b", "Receiver - Living", "hallway"),
            ("54:ef:44:10:00:67:62:ad", "TRV - Kitchen", "kitchen"),
            ("54:ef:44:10:00:67:3e:a6", "TRV - Living", "living")):
        add(ieee, name, room,
            {"local_temperature": 20.0, "occupied_heating_setpoint": 21.0,
             "pi_heating_demand": 0, "battery": 90},
            ["thermostat", "hvac", "battery"], TRV)

    add("nuki_933964765", "Lock - Front Door", None,
        {"locked": True, "lock_state": "locked", "door_state": "closed"},
        ["lock"], [{"command": "lock"}, {"command": "unlock"}])

    add("user::sean", "Sean", None,
        {"presence": "home", "place": "home", "distance_m": 0.0}, [])
    return d


def run() -> Checker:
    c = Checker("test_real_house")
    devices = _house()
    names = {i: d.friendly_name for i, d in devices.items()}
    settings = {i: {"chamber": d._room} for i, d in devices.items() if d._room}
    net = describe_network(devices, names, settings, ROOMS)
    by_ieee = {d["ieee"]: d for d in net["devices"]}

    c.section("every device is read as the kind of thing it is")
    expected = {
        "00:15:8d:00:06:a2:16:1a": "contact_sensor",
        "00:15:8d:00:06:a2:15:9e": "contact_sensor",
        "00:15:8d:00:07:e0:60:11": "contact_sensor",
        "00:15:8d:00:06:a2:89:46": "contact_sensor",
        "e4:56:ac:ff:fe:4c:d0:77": "contact_sensor",
        "00:17:88:01:09:16:37:1b": "presence_sensor",
        "00:17:88:01:09:16:33:33": "presence_sensor",
        "00:17:88:01:09:16:54:fb": "presence_sensor",
        "34:10:f4:ff:fe:e3:dd:6c": "cover",
        "34:10:f4:ff:fe:e9:2f:cf": "cover",
        "f0:82:c0:ff:fe:6f:0c:76": "color_light",
        "54:ef:44:10:01:12:a9:2e": "color_light",
        "60:b6:47:ff:fe:21:11:0c": "light",
        "60:b6:47:ff:fe:21:05:d4": "light",
        "28:76:81:ff:fe:bf:96:01": "light",
        "00:17:88:01:08:25:60:5d": "light",
        "00:15:8d:00:02:56:f8:bf": "plug",
        "5c:02:72:ff:fe:c6:18:f5": "plug",
        "60:a4:23:ff:fe:9d:98:4d": "plug",
        "00:1e:5e:09:02:a3:e7:27": "thermostat",
        "00:1e:5e:09:02:a4:40:5a": "thermostat",
        "54:ef:44:10:00:67:62:ad": "thermostat",
        "nuki_933964765": "lock",
        "user::sean": "person",
    }
    for ieee, want in expected.items():
        got = by_ieee[ieee]["device_class"]
        if not c.check(f"{names[ieee]:<24} -> {want}", got == want, got):
            break

    c.section("a sensor is never offered as something to switch on")
    # The bug that started this: sensors advertise OnOff for binding, so the
    # command list offers on/off they will never honour.
    sensors = [i for i, w in expected.items()
               if w in ("contact_sensor", "presence_sensor")]
    for ieee in sensors:
        acts = [a["key"] for a in by_ieee[ieee]["actions"]]
        if not c.check(f"{names[ieee]:<24} offers no actions", acts == [], acts):
            break

    c.section("and the things that should be switchable still are")
    for ieee in ("60:b6:47:ff:fe:21:11:0c", "00:15:8d:00:02:56:f8:bf",
                 "5c:02:72:ff:fe:c6:18:f5", "nuki_933964765"):
        acts = [a["key"] for a in by_ieee[ieee]["actions"]]
        if not c.check(f"{names[ieee]:<24} can still be driven", bool(acts), acts):
            break
    c.check("the dual-gang socket exposes both outlets",
            {"on_off:turn_on", "on_off:turn_on:ep2"} <=
            {a["key"] for a in by_ieee["00:15:8d:00:02:56:f8:bf"]["actions"]},
            [a["key"] for a in by_ieee["00:15:8d:00:02:56:f8:bf"]["actions"]])

    c.section("sensors still trigger")
    c.check("a door reports opening",
            any(t["key"] == "contact:opened"
                for t in by_ieee["00:15:8d:00:06:a2:16:1a"]["triggers"]))
    c.check("a motion sensor reports presence",
            any(t["key"] == "presence:detected"
                for t in by_ieee["00:17:88:01:09:16:37:1b"]["triggers"]))
    c.check("and its lux, so 'after dark' can be answered in that room",
            any(t["capability"] == "illuminance"
                for t in by_ieee["00:17:88:01:09:16:37:1b"]["conditions"]))

    c.section("no suggestion asks to switch on a sensor")
    built = sg.build(net["devices"], rules=[], rooms=ROOMS, patterns=STORE.all())
    sensor_names = {names[i] for i in sensors}
    bad = [s["sentence"] for s in built["suggestions"]
           for d in s["devices"]
           if d["ieee"] in sensors and d["offer"].startswith(("on_off:", "cover:"))]
    c.check("none found", bad == [], bad[:4])
    c.check("nothing was rejected", built["rejected"] == [], built["rejected"][:3])

    c.section("the suggestions it does make name real lights")
    lit = [s["sentence"] for s in built["suggestions"]
           if s["pattern_id"] == "presence_light_when_dark"]
    c.check("the presence pattern matched", lit, built["summary"])
    c.check("and turns on a light, not a door",
            all("Door" not in x for x in lit), lit[:3])
    c.check("bathroom motion drives a light in the bathroom",
            not any("Bathroom" in x and "Kitchen" in x for x in lit), lit[:3])

    return c


if __name__ == "__main__":
    checker = run()
    print(f"\n{checker.passed} passed, {len(checker.failures)} failed")
    sys.exit(1 if checker.failures else 0)
