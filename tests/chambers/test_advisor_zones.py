"""The heating advisor's zones are chambers: named by the registry, holding the
heating devices in that chamber, and following the chamber through a merge."""

from __future__ import annotations

from harness import Checker

from modules import chambers as ch
from modules.heating_advisor import HeatingAdvisor

TRV_A = "tt:00:00:00:00:00:00:0a"
TRV_B = "tt:00:00:00:00:00:00:0b"
STAT = "tt:00:00:00:00:00:00:0c"


def trv(chamber, temp):
    return {"friendly_name": f"TRV {temp}", "settings": {"chamber": chamber} if chamber else {},
            "state": {"local_temperature": temp, "occupied_heating_setpoint": 21}}


def run() -> Checker:
    c = Checker("advisor_zones")

    c.section("a zone is its chamber")
    devices = {TRV_A: trv("lounge", 19.0), TRV_B: trv("lounge", 21.0), STAT: trv(None, 18.0)}
    adv = HeatingAdvisor({"enabled": True, "zones": [
        {"id": "lounge", "target_temp": 20}, {"id": "kitchen"}]},
        weather_service=None, device_getter=lambda: devices)
    HeatingAdvisor._zone_names = staticmethod(lambda: {"lounge": "Living Room"})
    hvac = adv._find_hvac_devices()
    zones = {z["id"]: z for z in adv._analyse_zones(hvac, None)}
    c.check("named by the registry", zones["lounge"]["name"] == "Living Room", zones["lounge"])
    c.check("holding the heating devices in that chamber",
            sorted(d["ieee"] for d in zones["lounge"]["devices"]) == [TRV_A, TRV_B],
            zones["lounge"]["devices"])
    c.check("averaging their temperatures", zones["lounge"]["current_temp"] == 20.0)
    c.check("a chamber with no heating devices is an empty zone",
            zones["kitchen"]["device_count"] == 0)
    tips = adv._generate_tips(None, None, False, adv._estimate_epc(None), [])
    c.check("a device in no zoned chamber is flagged",
            any(t.get("category") == "zones" and t["title"].startswith("1 ") for t in tips), tips)
    c.check("a zone without a chamber id is dropped",
            adv._clean_zones([{"name": "Upstairs"}]) == [])

    c.section("zones follow their chamber")
    cfg = {"chambers": [{"id": "lounge", "name": "Lounge"}, {"id": "den", "name": "Den"},
                        {"id": "attic", "name": "Attic"}],
           "heating": {"circuits": [{"id": "c", "rooms": [{"id": "den", "name": "Den"}]}],
                       "zones": [{"id": "lounge", "target_temp": 19}, {"id": "attic"}]}}
    c.check("the registry marks a zoned chamber",
            {x["id"]: x["zoned"] for x in ch.build_registry(cfg, {})}
            == {"lounge": True, "den": False, "attic": True})
    ch.merge_chambers(cfg, {}, "lounge", "den")
    c.check("merging into a heated chamber moves the zone to its id",
            cfg["heating"]["zones"][0] == {"id": "den", "target_temp": 19}, cfg["heating"]["zones"])
    ok, _ = ch.delete_chamber(cfg, "attic", {})
    c.check("deleting a chamber removes its zone",
            ok and [z["id"] for z in cfg["heating"]["zones"]] == ["den"], cfg["heating"]["zones"])
    return c


if __name__ == "__main__":
    raise SystemExit(1 if run().failures else 0)
