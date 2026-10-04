"""The hub-side rule engine: what fires, for whom, and when it stays quiet."""

from __future__ import annotations

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
        c.section("robustness")
        rig = _rig(tmp)
        rig.rule(trigger="low_battery")
        c.check("a non-numeric battery value is skipped, not raised",
                rig.change("aa", battery="unknown") == [])
        c.check("an unknown device is ignored",
                rig._run(lambda: rig.engine.observe("zz", {"occupancy": True})) == [])

    return c
