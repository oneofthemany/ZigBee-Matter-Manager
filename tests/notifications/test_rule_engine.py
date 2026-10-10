"""The hub-side rule engine: what fires, for whom, and when it stays quiet."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from harness import Checker, FakeDevice, Rig


def _rig(tmp, **kw):
    devices = {
        "aa": FakeDevice("Hall Sensor", {"occupancy": False}),
        "bb": FakeDevice("Garage Sensor", {"occupancy": False}),
        "cc": FakeDevice("Lounge TRV", {"temperature": 4.0}),
        "dd": FakeDevice("Remote", {"action": ""}),
    }
    return Rig(Path(tmp), devices, **kw)


def run() -> Checker:
    c = Checker("rule_engine")

    with tempfile.TemporaryDirectory() as tmp:
        c.section("edge-triggered delivery to the owner")
        rig = _rig(tmp)
        r = rig.rule("alice", title="Someone's in")
        sent = rig.change("aa", occupancy=True)
        c.check("motion starting notifies the rule's owner", len(sent) == 1 and sent[0][0] == "alice", sent)
        p = sent[0][1] if sent else {}
        c.check("payload carries the rule title and default body",
                p.get("title") == "Someone's in" and p.get("body") == "Motion detected — Hall Sensor", p)
        c.check("push and in-page share one tag per rule and device",
                p.get("tag") == f"zmm-rule-{r['id']}-aa", p.get("tag"))
        c.check("a steady state does not fire again", rig.change("aa", occupancy=True) == [])
        c.check("motion clearing doesn't fire a motion-detected rule", rig.change("aa", occupancy=False) == [])

    with tempfile.TemporaryDirectory() as tmp:
        c.section("first sight after a restart")
        rig = _rig(tmp)
        rig.rule()
        c.check("the first change seen for a device still fires (no warm-up poll needed)",
                len(rig.change("bb", occupancy=True)) == 1)

    with tempfile.TemporaryDirectory() as tmp:
        c.section("scope")
        rig = _rig(tmp, tabs={"Outside": ["bb"]})
        rig.rule("alice", scope="devices", devices=["aa"])
        rig.rule("bob", scope="tab", tab="Outside")
        c.check("a selected-devices rule ignores other devices",
                [o for o, _ in rig.change("bb", occupancy=True)] == ["bob"])
        c.check("a tab rule follows the hub's device tabs",
                [o for o, _ in rig.change("aa", occupancy=True)] == ["alice"])
        rig.tabs["Outside"].append("aa")
        rig.change("aa", occupancy=False)
        c.check("a device added to the tab is covered without editing the rule",
                sorted(o for o, _ in rig.change("aa", occupancy=True)) == ["alice", "bob"])

    with tempfile.TemporaryDirectory() as tmp:
        c.section("disabled rules")
        rig = _rig(tmp)
        r = rig.rule()
        rig.store.update("alice", r["id"], {**r, "enabled": False})
        c.check("a disabled rule stays quiet", rig.change("aa", occupancy=True) == [])

    with tempfile.TemporaryDirectory() as tmp:
        c.section("cooldown")
        rig = _rig(tmp)
        rig.rule(cooldownMinutes=5)
        rig.change("aa", occupancy=True); rig.change("aa", occupancy=False)
        rig.now += 60
        c.check("a repeat inside the cooldown is suppressed", rig.change("aa", occupancy=True) == [])
        rig.change("aa", occupancy=False)
        rig.now += 5 * 60
        c.check("the next one after the cooldown fires", len(rig.change("aa", occupancy=True)) == 1)
        c.check("cooldown is per device, not per rule",
                len(rig.change("bb", occupancy=True)) == 1)

    with tempfile.TemporaryDirectory() as tmp:
        c.section("time window")
        rig = _rig(tmp, local_time="23:30")
        rig.rule(timeFrom="22:00", timeTo="06:00")
        c.check("a window that wraps midnight includes 23:30", len(rig.change("aa", occupancy=True)) == 1)
        rig.change("aa", occupancy=False)
        rig.local_time = "12:00"
        c.check("…and excludes midday", rig.change("aa", occupancy=True) == [])

    with tempfile.TemporaryDirectory() as tmp:
        c.section("thresholds, buttons and custom text")
        rig = _rig(tmp)
        rig.rule(trigger="temp_below", threshold=3, message="{device} is freezing")
        c.check("staying above the threshold doesn't fire", rig.change("cc", temperature=3.5) == [])
        sent = rig.change("cc", temperature=2.5)
        c.check("crossing below the threshold fires with {device} filled in",
                len(sent) == 1 and sent[0][1]["body"] == "Lounge TRV is freezing", sent)
        rig.rule(trigger="button_pressed")
        rig.change("dd", action="single")
        c.check("pressing the same button action twice notifies twice",
                len(rig.change("dd", action="single")) == 1)

    with tempfile.TemporaryDirectory() as tmp:
        c.section("online / offline (no state event to ride on)")
        rig = _rig(tmp)
        rig.rule(trigger="offline")
        rig.rule(trigger="online")
        c.check("the first sweep only records a baseline", rig.sweep() == [])
        rig.devices["bb"].available = False
        sent = rig.sweep()
        c.check("a device dropping off is reported by the sweep",
                [p["body"] for _, p in sent] == ["Garage Sensor is offline"], sent)
        c.check("still offline on the next sweep: no repeat", rig.sweep() == [])
        rig.devices["bb"].available = True
        sent = rig.change("bb", occupancy=True)
        c.check("a device coming back via a state report fires 'online'",
                [p["body"] for _, p in sent] == ["Garage Sensor is online"], sent)

    with tempfile.TemporaryDirectory() as tmp:
        c.section("bell switches run as rules")
        rig = _rig(tmp)
        rig.store.set_bell("alice", {"enabled": True, "deviceOffline": True, "deviceOnline": False,
                                     "lowBattery": False, "thermostatReached": False, "suppressMinutes": 5})
        rig.sweep()
        rig.devices["bb"].available = False
        sent = rig.sweep()
        c.check("a device going offline notifies with the bell's own wording",
                [(o, p["title"], p["body"]) for o, p in sent] == [("alice", "Device Offline", "Garage Sensor has gone offline")], sent)
        rig.devices["bb"].available = True
        rig.change("bb", occupancy=True)
        rig.devices["bb"].available = False
        c.check("the bell's suppression time holds back a repeat", rig.sweep() == [])

    with tempfile.TemporaryDirectory() as tmp:
        c.section("last fired and test sends")
        rig = _rig(tmp)
        r = rig.rule(cooldownMinutes=5, message="{device} saw someone")
        c.check("a rule that hasn't fired reports nothing", rig.engine.last_fired(r["id"]) is None)
        rig.now = 1_000_100.0
        rig.change("aa", occupancy=True)
        last = rig.engine.last_fired(r["id"])
        c.check("a real firing is recorded with when, which device and what it said",
                last == {"at": 1_000_100.0, "ieee": "aa", "device": "Hall Sensor", "body": "Hall Sensor saw someone"}, last)
        before = len(rig.sent)
        rig._run(lambda: asyncio.ensure_future(rig.engine.send_test(r)))
        test = rig.sent[before:]
        c.check("a test reaches the owner with the rule's own wording",
                len(test) == 1 and test[0][0] == "alice" and test[0][1]["body"] == "Test device saw someone"
                and test[0][1]["test"] is True, test)
        c.check("a test doesn't count as a firing", rig.engine.last_fired(r["id"])["ieee"] == "aa")
        rig.change("aa", occupancy=False)
        rig.now += 6 * 60
        c.check("a test doesn't start the cooldown", len(rig.change("aa", occupancy=True)) == 1)

    with tempfile.TemporaryDirectory() as tmp:
        c.section("cameras")
        from harness import FakeDevice as FD
        rig = Rig(Path(tmp) / "cam", {"camera::front": FD("Front door", {"person": False, "motion": False}),
                                      "aa": FD("Porch door", {"contact": True})})
        rig.rule("alice", trigger="person_detected")
        rig.rule("alice", trigger="contact_opened", camera="front")
        sent = rig.change("camera::front", person=True)
        c.check("a person appearing on a camera fires, named for the camera",
                len(sent) == 1 and sent[0][1]["body"] == "Person seen — Front door" and sent[0][1]["ieee"] == "camera::front", sent)
        c.check("…with no camera of its own chosen: delivery uses the one it fired on", sent[0][1]["camera"] is None)
        c.check("still there is not a new sighting", rig.change("camera::front", person=True) == [])
        rig.change("camera::front", person=False)
        rig.devices["camera::front"].zone_names = {"z1": "Driveway"}
        sent = rig.change("camera::front", person=True, person_z1=True)
        c.check("a sighting in a zone names it as it is called now, whatever its id",
                len(sent) == 1 and sent[0][1]["body"] == "Person seen — Front door (Driveway)", sent)
        sent = rig.change("aa", contact=False)
        c.check("a door rule can name a camera to send a snapshot from", len(sent) == 1 and sent[0][1]["camera"] == "front", sent)
        c.section("camera zones")
        rig = Rig(Path(tmp) / "zone", {
            "camera::front": FD("Front door", {"person": False, "person_drive": False, "person_porch": False, "vehicle_drive": False}),
            "camera::yard": FD("Yard", {"person": False, "person_drive": False})})
        rig.devices["camera::front"].zone_names = {"drive": "Driveway", "porch": "Porch"}
        rig.rule("alice", trigger="person_detected", zone={"camera": "front", "id": "drive"},
                 scope="devices", devices=["camera::yard"])
        c.check("a person on the camera but outside the rule's zone doesn't fire it",
                rig.change("camera::front", person=True, person_porch=True) == [])
        sent = rig.change("camera::front", person_drive=True)
        c.check("walking into the zone does, though they were already in view",
                len(sent) == 1 and sent[0][1]["body"] == "Person seen — Front door (Driveway)", sent)
        c.check("…naming only that zone, not the others they are in", "Porch" not in sent[0][1]["body"])
        c.check("staying there doesn't fire again", rig.change("camera::front", person_drive=True) == [])
        c.check("another kind of thing in the zone doesn't fire a person rule", rig.change("camera::front", vehicle_drive=True) == [])
        c.check("a zone with the same id on another camera is a different zone — and the rule's device list is ignored",
                rig.change("camera::yard", person=True, person_drive=True) == [])
        rig.change("camera::front", person_drive=False)
        rig.devices["camera::front"].zone_names = {"drive": "Front drive", "porch": "Porch"}
        sent = rig.change("camera::front", person_drive=True)
        c.check("after the zone is renamed the rule still fires, with the new name",
                len(sent) == 1 and sent[0][1]["body"] == "Person seen — Front door (Front drive)", sent)
        from modules.notification_rules import normalise_rule
        c.check("a zone is kept only on a camera trigger",
                normalise_rule({"trigger": "person_detected", "zone": {"camera": "front", "id": "drive"}})["zone"]
                == {"camera": "front", "id": "drive"}
                and normalise_rule({"trigger": "motion_detected", "zone": {"camera": "front", "id": "drive"}})["zone"] is None)
        for bad in ("front/drive", {"camera": "front"}, {"camera": "../x", "id": "drive"}):
            try:
                normalise_rule({"trigger": "person_detected", "zone": bad})
                ok = False
            except ValueError:
                ok = True
            c.check(f"a malformed zone is refused: {bad}", ok)

        try:
            normalise_rule({"trigger": "contact_opened", "camera": "../etc"})
            ok = False
        except ValueError:
            ok = True
        c.check("a camera id that couldn't be one is refused", ok)
        c.check("no zone is the default", normalise_rule({"trigger": "person_detected"})["zone"] is None)
        c.check("no camera is the default", normalise_rule({"trigger": "contact_opened"})["camera"] is None)

        c.section("robustness")
        rig = _rig(tmp)
        rig.rule(trigger="low_battery")
        c.check("a non-numeric battery value is skipped, not raised",
                rig.change("aa", battery="unknown") == [])
        c.check("an unknown device is ignored",
                rig._run(lambda: rig.engine.observe("zz", {"occupancy": True})) == [])

    return c
