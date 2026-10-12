"""A fast cool-down is explained by the room's contact sensors when they know,
and by the detector's guess only when they don't."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace as NS

from harness import Checker

import modules.heating_anomaly_watcher as w
from modules.thermal_profile import Anomaly

DOOR = "cc:00:00:00:00:00:00:01"
WINDOW = "cc:00:00:00:00:00:00:02"


def run() -> Checker:
    c = Checker("anomaly_contacts")

    c.section("what the sensors say")
    door = {"name": "Kitchen door", "is_open": True, "open_for_sec": 62 * 60, "open_in_window_sec": 0}
    cause = w.contact_cause([door])
    c.check("an open door is named, with how long it has been open",
            cause["kind"] == "open" and cause["text"] == "Kitchen door (open 1 h 2 min) is open.", cause)
    cause = w.contact_cause([door, {"name": "Patio", "is_open": True, "open_for_sec": None,
                                    "open_in_window_sec": 0}])
    c.check("two open contacts are both named",
            cause["text"] == "Kitchen door (open 1 h 2 min) and Patio are open.", cause)
    cause = w.contact_cause([{"name": "Kitchen door", "is_open": False, "open_for_sec": None,
                              "open_in_window_sec": 12 * 60}])
    c.check("a door shut again is still named for the time it was open",
            cause["kind"] == "was_open" and "Kitchen door was open for 12 min" in cause["text"], cause)
    cause = w.contact_cause([{"name": "Kitchen door", "is_open": False, "open_for_sec": None,
                              "open_in_window_sec": 0}])
    c.check("closed sensors rule themselves out and point elsewhere",
            cause["kind"] == "all_closed" and "stayed closed" in cause["text"], cause)
    c.check("an open contact wins over one that was merely open earlier",
            w.contact_cause([door, {"name": "Patio", "is_open": False, "open_for_sec": None,
                                    "open_in_window_sec": 600}])["sensors"] == ["Kitchen door"])
    c.check("no sensor with a known state leaves the guess standing",
            w.contact_cause([{"name": "x", "is_open": None}]) is None and w.contact_cause([]) is None)

    c.section("time open inside the cooling window")
    t = lambda m: dt.datetime(2026, 10, 12, 7, m)                     # noqa: E731
    rows = [{"ts": t(0), "value": "False"}, {"ts": t(10), "value": "True"},
            {"ts": t(22), "value": "False"}, {"ts": t(50), "value": "True"}]
    c.check("only the open stretch inside the window counts",
            w._open_seconds_in_window(rows, t(5).timestamp(), t(30).timestamp()) == 12 * 60)
    c.check("a door still open runs to the window's end",
            w._open_seconds_in_window(rows, t(40).timestamp(), t(55).timestamp()) == 5 * 60)

    c.section("the watcher's message")
    svc = NS(friendly_names={DOOR: "Kitchen door"})
    adv = NS(_get_devices=lambda: {DOOR: NS(state={"is_open": True}, service=svc),
                                   WINDOW: NS(state={"contact": True}, service=svc)})
    watcher = w.HeatingAnomalyWatcher(lambda: {}, lambda: adv, lambda ieee, attr, hours: [])
    w.query_state_since_change_sec = lambda ieee, attr, hours: 18 * 60
    anomaly = Anomaly(room_id="k", kind="fast_cool", severity="warning", detected_at=0,
                      summary="Room cooling 52% faster than normal: dropped 0.6°C in 25 min.",
                      window_start_ts=1.0, window_end_ts=2.0)
    room = {"contact_sensors": [{"ieee": DOOR, "name": "89:46", "enabled": True},
                                {"ieee": WINDOW, "name": "Window", "enabled": True}]}
    cause = watcher._contact_cause(room, adv, anomaly)
    c.check("uses the device's name and its real state",
            cause and cause["text"] == "Kitchen door (open 18 min) is open.", cause)
    c.check("a room with no contact sensors gets no cause",
            watcher._contact_cause({"contact_sensors": []}, adv, anomaly) is None)
    c.check("a disabled contact is not consulted",
            watcher._contact_cause({"contact_sensors": [{"ieee": DOOR, "enabled": False}]},
                                   adv, anomaly) is None)
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
