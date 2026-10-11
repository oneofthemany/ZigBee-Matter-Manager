"""HeatingController against stub devices and a recording sender — the real
tick, classification, receiver and TRV paths, no radio and no DuckDB."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime

from harness import Checker

import modules.heating_controller as hc

# No DuckDB on these paths: freshness reads None, writes are skipped.
hc._write_heating_tick = None
hc._query_last_report_age = None
hc._query_distinct_values = None

RX = "rr:00:00:00:00:00:00:01"
TRV = "tt:00:00:00:00:00:00:01"
SENSOR = "ss:00:00:00:00:00:00:01"


class Sender:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def __call__(self, ieee, command, value=None, endpoint_id=None):
        self.calls.append((ieee, command, value))
        return {"success": not self.fail, "error": "Device rejected command" if self.fail else None}

    def to(self, ieee):
        return [c for c in self.calls if c[0] == ieee]


def heating_cfg(**room):
    r = {"id": "lounge", "name": "Lounge", "target_temp": 20.0,
         "temperature_sensor_ieee": SENSOR, "external_temp_mode": "advisory",
         "trvs": [{"ieee": TRV}]}
    r.update(room)
    return {"enabled": True, "controller": {"enabled": True, "dry_run": False},
            "circuits": [{"id": "down", "name": "Down", "receiver_ieee": RX,
                          "receiver_command": "thermostat", "rooms": [r]}]}


def make(cfg=None, room_temp=19.0, trv_temp=22.0, rx_mode="off", trv_sp=20.0):
    devices = {
        SENSOR: {"state": {"temperature": room_temp}},
        TRV: {"state": {"local_temperature": trv_temp, "occupied_heating_setpoint": trv_sp}},
        RX: {"model": "Generic", "state": {"system_mode": rx_mode}},
    }
    sender = Sender()
    ctrl = hc.HeatingController(cfg or heating_cfg(), lambda: devices, sender)
    ctrl._overshoot = {}
    return ctrl, devices, sender


def tick(ctrl):
    asyncio.run(ctrl._tick())
    return ctrl.get_state()["circuits"][0]


def room(state):
    return state["rooms"][0]


def run() -> Checker:
    c = Checker("controller")

    c.section("enabled gates everything")
    ctrl, dev, s = make(room_temp=15.0)
    ctrl.enabled = False
    asyncio.run(ctrl._tick())
    c.check("a disabled controller sends nothing even when a room is cold",
            s.calls == [], s.calls)

    async def disable_flow():
        ctrl, dev, s = make(room_temp=15.0)
        await ctrl._tick()
        assert ("system_mode", "heat") in [(x[1], x[2]) for x in s.to(RX)]
        cfg = heating_cfg()
        cfg["controller"]["enabled"] = False
        await ctrl.apply_config(cfg)
        return ctrl, s

    ctrl, s = asyncio.run(disable_flow())
    c.check("disabling a live controller that was calling stands the receiver down",
            s.to(RX)[-1][1:] == ("system_mode", "off"), s.to(RX))
    c.check("disabling stops the loop task", ctrl._task is None, ctrl._task)

    async def enable_flow():
        cfg = heating_cfg()
        cfg["controller"]["enabled"] = False
        ctrl, dev, s = make(cfg)
        await ctrl.apply_config(heating_cfg())
        started = ctrl._task is not None
        ctrl.stop()
        return started

    c.check("enabling at runtime starts the loop without a restart", asyncio.run(enable_flow()))

    c.section("dry-run boundary")
    cfg = heating_cfg()
    cfg["controller"]["dry_run"] = True
    ctrl, dev, s = make(cfg, room_temp=15.0)
    tick(ctrl)
    c.check("dry run sends nothing", s.calls == [], s.calls)
    asyncio.run(ctrl.set_dry_run(False))
    tick(ctrl)
    c.check("going live after a simulated heat call really sends it",
            ("system_mode", "heat") in [x[1:] for x in s.to(RX)], s.to(RX))

    c.section("hysteresis latch")
    ctrl, dev, s = make(room_temp=19.4)
    c.check("starts calling below target - COLD_BAND", room(tick(ctrl))["calling_for_heat"])
    dev[SENSOR]["state"]["temperature"] = 19.7
    c.check("keeps calling inside the dead band once started",
            room(tick(ctrl))["calling_for_heat"])
    dev[SENSOR]["state"]["temperature"] = 20.0
    c.check("stops at target", not room(tick(ctrl))["calling_for_heat"])
    dev[SENSOR]["state"]["temperature"] = 19.7
    c.check("does not restart inside the dead band when falling",
            not room(tick(ctrl))["calling_for_heat"])

    c.section("overshoot learning")
    ctrl, dev, s = make(room_temp=19.0)
    tick(ctrl)
    dev[SENSOR]["state"]["temperature"] = 20.0
    tick(ctrl)                                   # latch releases, watch starts
    dev[SENSOR]["state"]["temperature"] = 21.0
    tick(ctrl)
    dev[SENSOR]["state"]["temperature"] = 20.8
    tick(ctrl)                                   # peak 21.0 recorded
    learned = ctrl._overshoot["lounge"]["learned_offset_c"]
    c.check("a single 1 °C overshoot teaches a fraction, not the whole of it",
            0 < learned < 0.5, learned)
    ctrl._overshoot["lounge"]["learned_offset_c"] = hc.OVERSHOOT_MAX_OFFSET_C
    ctrl._heat_latch["lounge"] = False
    dev[SENSOR]["state"]["temperature"] = 19.4
    c.check("a large learned offset never stops a room starting below the cold band",
            room(tick(ctrl))["calling_for_heat"])
    dev[SENSOR]["state"]["temperature"] = 19.55
    c.check("the early stop is capped above the start threshold",
            room(tick(ctrl))["calling_for_heat"])

    ctrl, dev, s = make(room_temp=19.0)
    tick(ctrl)
    dev[SENSOR]["state"]["temperature"] = 20.0
    tick(ctrl)
    dev[SENSOR]["state"]["temperature"] = 24.0   # shower, not radiator
    tick(ctrl)
    dev[SENSOR]["state"]["temperature"] = 23.0
    tick(ctrl)
    c.check("an implausible spike is not learned",
            ctrl._overshoot["lounge"]["learned_offset_c"] == 0.0, ctrl._overshoot["lounge"])

    hc.OVERSHOOT_STATE_FILE = os.path.join(os.environ["ZMM_DATA_DIR"], "overshoot.json")
    ctrl._overshoot["lounge"]["learned_offset_c"] = 0.3
    ctrl._save_overshoot()
    c.check("learned offsets survive a restart",
            hc.HeatingController._load_overshoot()["lounge"]["learned_offset_c"] == 0.3,
            json.load(open(hc.OVERSHOOT_STATE_FILE)))

    c.section("TRV setpoints")
    ctrl, dev, s = make(room_temp=19.0, trv_temp=22.0)
    st = tick(ctrl)
    sent = [x for x in s.to(TRV) if x[1] == "temperature"]
    c.check("advisory cold room opens its TRV above target",
            sent and sent[-1][2] == 20.0 + hc.ADVISORY_OPEN_BOOST_C, sent)
    # Force-close aims for target - 1 = 19 °C; the configured floor is above it.
    cfg = heating_cfg(trvs=[{"ieee": TRV, "min_setpoint": 19.5}])
    ctrl, dev, s = make(cfg, room_temp=23.0, trv_sp=22.0)
    tick(ctrl)
    sent = [x for x in s.to(TRV) if x[1] == "temperature"]
    c.check("force-close is floored at the configured min_setpoint",
            sent and sent[-1][2] == 19.5, sent)

    c.section("receiver")
    ctrl, dev, s = make(room_temp=15.0)
    s.fail = True
    r = tick(ctrl)
    c.check("a rejected receiver write is reported as not sent",
            r["receiver_action"].get("sent") is False, r["receiver_action"])
    attempts = len(s.to(RX))
    s.fail = False
    r = tick(ctrl)
    c.check("and is retried next tick rather than cached as done",
            len(s.to(RX)) > attempts and r["receiver_action"].get("sent") is True,
            (s.to(RX), r["receiver_action"]))

    ctrl, dev, s = make(room_temp=15.0, rx_mode="emergency_heat")
    tick(ctrl)
    ctrl._last_command[RX] = ("system_mode", "heat", time.time() - 3600)
    n = len(s.to(RX))
    tick(ctrl)
    c.check("a receiver reporting emergency_heat is not bounced as stalled",
            len(s.to(RX)) == n, s.to(RX)[n:])
    ctrl, dev, s = make(room_temp=15.0, rx_mode="off")
    tick(ctrl)
    ctrl._last_command[RX] = ("system_mode", "heat", time.time() - 3600)
    tick(ctrl)
    c.check("an unconfirmed heat call is bounced after the stall window",
            s.to(RX)[-1][1:] == ("system_mode", "off"), s.to(RX))

    c.section("stuck sensors")
    ctrl, dev, s = make(room_temp=25.5)
    hc._query_last_report_age = lambda *a: 10.0      # reported 10 s ago
    hc._freshness_cache.clear()
    hc._freshness_cache[SENSOR] = (time.time(), time.time() - 10)
    hc._flatline_cache[SENSOR] = (time.time(), True)
    dev[TRV]["state"]["local_temperature"] = 19.0
    r = room(tick(ctrl))
    hc._query_last_report_age = None
    hc._flatline_cache.clear()
    hc._freshness_cache.clear()
    c.check("a recently-reported but frozen sensor is rejected",
            r["sensor_stale"] and r["temp_source"] == "trv_mean", r)
    c.check("and the room health says why",
            any("unchanging" in x for x in r["health"]["reasons"]), r["health"])

    cfg = heating_cfg(trvs=[])
    ctrl, dev, s = make(cfg)
    dev.pop(SENSOR)
    r = room(tick(ctrl))
    c.check("an unmeasurable room is unknown and never calls",
            r["status"] == "unknown" and not r["calling_for_heat"], r["status"])

    c.section("targets")
    ctrl, _, _ = make()
    rm = {"target_temp": 21.0, "night_setback": 16.0, "min_temp": 14.0,
          "schedule": [{"days": list(hc.DAY_KEYS), "start": "07:00", "end": "09:00", "temp": 21.0}]}
    c.check("a scheduled room sets back outside its slots",
            ctrl._effective_target(rm, datetime(2026, 10, 12, 13, 0)) == 16.0)
    c.check("and holds the slot temperature inside one",
            ctrl._effective_target(rm, datetime(2026, 10, 12, 8, 0)) == 21.0)
    ctrl._oh_enabled = True
    ctrl._oh_action = "off"
    c.check("out-of-hours 'off' is off, not min_temp",
            ctrl._effective_target({"target_temp": 21.0, "min_temp": 16.0},
                                   datetime(2026, 10, 12, 3, 0)) == hc.OUT_OF_HOURS_OFF_C)

    c.section("config mode")
    mixed = {"circuits": [{"id": "a", "name": "A", "rooms": []}],
             "controller": {"config_mode": "manual",
                            "circuits": [{"id": "b", "name": "B", "rooms": []}]}}
    c.check("manual mode reads heating.circuits only",
            [x["id"] for x in hc.circuits_for_mode(mixed)] == ["a"])
    mixed["circuits"] = []
    c.check("and never falls back to the floor-plan list",
            hc.circuits_for_mode(mixed) == [])
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
