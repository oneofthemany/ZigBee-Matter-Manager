"""HeatingController against stub devices and a recording sender — the real
tick, classification, receiver and TRV paths, no radio and no DuckDB."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime

from types import SimpleNamespace as NS

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

    c.section("solar suppression is on probation")
    import modules.solar_gain as sg
    real_suppress = sg.should_suppress_heat_call
    sg.should_suppress_heat_call = lambda **k: (k.get("outdoor_temp_c") is not None, 60.0)
    ctrl, dev, s = make(room_temp=19.0)
    ctrl._weather = NS(latitude=51.0, longitude=0.0, get_outdoor_temperature=lambda: 12.0,
                       get_solar_irradiance=lambda: 300.0, get_cloud_fraction=lambda: 0.2)
    ctrl._get_room_profile = lambda room: {"w_per_k": 30.0}
    r = room(tick(ctrl))
    c.check("sun the model trusts holds the call off at first",
            r["status"] == "solar_suppressed" and not r["calling_for_heat"], r["status"])
    ctrl._solar_hold["lounge"]["since"] -= hc.SOLAR_PROOF_SEC + 60
    r = room(tick(ctrl))
    c.check("a room that hasn't warmed after the proof window is heated",
            r["status"] == "cold" and r["calling_for_heat"], r["status"])
    c.check("and stays heated until that call is satisfied",
            room(tick(ctrl))["calling_for_heat"])
    dev[SENSOR]["state"]["temperature"] = 20.1
    tick(ctrl)
    c.check("a satisfied call clears the probation", "lounge" not in ctrl._solar_hold)
    ctrl, dev, s = make(room_temp=19.0)
    ctrl._weather = NS(latitude=51.0, longitude=0.0, get_outdoor_temperature=lambda: 12.0,
                       get_solar_irradiance=lambda: 300.0, get_cloud_fraction=lambda: 0.2)
    ctrl._get_room_profile = lambda room: {"w_per_k": 30.0}
    tick(ctrl)
    ctrl._solar_hold["lounge"]["since"] -= hc.SOLAR_PROOF_SEC + 60
    dev[SENSOR]["state"]["temperature"] = 19.3
    c.check("a room the sun is warming keeps its suppression",
            room(tick(ctrl))["status"] == "solar_suppressed")
    sg.should_suppress_heat_call = real_suppress
    c.check("the model needs the sun to cover the loss to outdoors, not the last degree",
            not sg.should_suppress_heat_call(
                room_config={"dimensions": {"windows": [{"area_m2": 2.0, "orientation": "S"}]}},
                lat=51.0, lon=0.0, current_temp_c=19.0, target_temp_c=20.0, w_per_k=30.0,
                dt_utc=datetime(2026, 6, 21, 12, 0), shortwave_wm2=100.0, cloud_fraction=0.9,
                outdoor_temp_c=5.0)[0])

    c.section("TRV sensor mode follows the room")
    async def sensor_mode(room_mode, device_mode):
        ctrl, dev, s = make(heating_cfg(external_temp_mode=room_mode))
        if device_mode is not None:
            dev[TRV]["state"]["sensor_type"] = device_mode
        await ctrl.apply_trv_config(TRV)
        return [x[2] for x in s.to(TRV) if x[1] == "sensor_type"]
    c.check("a TRV left in external mode is put back to internal when nothing feeds it",
            asyncio.run(sensor_mode("advisory", "external")) == [0])
    c.check("a push room switches its TRV to external",
            asyncio.run(sensor_mode("push", "internal")) == [1])
    c.check("a TRV already in the right mode is left alone",
            asyncio.run(sensor_mode("advisory", "internal")) == [])
    c.check("a TRV that reports no sensor mode is never switched to internal",
            asyncio.run(sensor_mode("advisory", None)) == [])

    c.section("TRV health")
    ctrl, dev, s = make()
    dev[TRV]["state"].update(calibrated=False, calibration_status="not_ready", system_mode="off")
    reasons = room(tick(ctrl))["health"]["reasons"]
    c.check("a TRV reporting itself uncalibrated is called out",
            any("not calibrated" in x for x in reasons), reasons)
    c.check("so is one that is switched off", any("switched off" in x for x in reasons), reasons)
    ctrl, dev, s = make()
    dev[TRV]["state"].update(calibrated=False, calibration_status="in_progress")
    c.check("a calibration still running is not a fault",
            not any("calibrat" in x for x in room(tick(ctrl))["health"]["reasons"]))
    ctrl, dev, s = make()
    dev[TRV]["state"].update(calibration_status="failed")
    c.check("a calibrate command that never arrived says nothing about the valve",
            not any("calibrat" in x for x in room(tick(ctrl))["health"]["reasons"]))

    c.section("pre-heat for the operating day")
    ctrl, _, _ = make()
    ctrl._oh_enabled = True
    ctrl._oh_weekday_start, ctrl._oh_weekday_end = "07:30", "22:30"
    ctrl._oh_weekend_start, ctrl._oh_weekend_end = "08:30", "23:00"
    plain = {"id": "lounge", "target_temp": 20.0, "schedule": []}
    nxt = ctrl._next_operating_start(plain, datetime(2026, 10, 12, 6, 0))       # Monday
    c.check("before a weekday start, the day start is the upcoming warm period",
            nxt and nxt["temp"] == 20.0 and nxt["minutes_until"] == 90.0, nxt)
    nxt = ctrl._next_operating_start(plain, datetime(2026, 10, 16, 23, 30))     # Friday night
    c.check("late on Friday it is Saturday's later start, still beyond the look-ahead",
            nxt is None, nxt)
    nxt = ctrl._next_operating_start(plain, datetime(2026, 10, 17, 6, 0))       # Saturday
    c.check("a weekend morning uses the weekend start", nxt and nxt["minutes_until"] == 150.0, nxt)
    c.check("nothing is upcoming during the day",
            ctrl._next_operating_start(plain, datetime(2026, 10, 12, 12, 0)) is None)
    c.check("a room with its own schedule is left to it",
            ctrl._next_operating_start({**plain, "schedule": [{"days": ["mon"]}]},
                                       datetime(2026, 10, 12, 6, 0)) is None)
    c.check("a circuit that opts out of operating hours gets none",
            ctrl._next_operating_start({**plain, "_oh_enabled_for_circuit": False},
                                       datetime(2026, 10, 12, 6, 0)) is None)
    ctrl._weather = NS(latitude=None, longitude=None, get_outdoor_temperature=lambda: 5.0,
                       get_solar_irradiance=lambda: 0.0, get_cloud_fraction=lambda: 1.0)
    ctrl._get_room_profile = lambda room: {"w_per_k": 60.0, "tau_seconds": 36000.0,
                                           "radiator_watts": 1500.0, "confidence": "medium"}
    target, info = ctrl._maybe_preheat_target(plain, 16.0, datetime(2026, 10, 12, 7, 20))
    c.check("ten minutes before the day starts, a cold room is already heating to target",
            target == 20.0 and info and info["preheating"], (target, info))
    target, info = ctrl._maybe_preheat_target(plain, 19.5, datetime(2026, 10, 12, 4, 0))
    c.check("hours ahead of what the room needs, it is left at setback",
            target is None and info and not info["preheating"], (target, info))

    c.section("TRV settings re-apply")
    async def reapply():
        ctrl, dev, s = make(heating_cfg(trvs=[{"ieee": TRV, "child_lock": False}]))
        ctrl._trv_config_applied.add(TRV)
        await ctrl.apply_config(heating_cfg(trvs=[{"ieee": TRV, "child_lock": True}]))
        changed = TRV not in ctrl._trv_config_applied
        ctrl._trv_config_applied.add(TRV)
        await ctrl.apply_config(heating_cfg(trvs=[{"ieee": TRV, "child_lock": True}]))
        ctrl.stop()
        return changed, TRV in ctrl._trv_config_applied
    changed, kept = asyncio.run(reapply())
    c.check("a TRV whose flags changed is configured again", changed)
    c.check("an unchanged TRV is not re-sent", kept)

    c.section("history names")
    from modules.heating_advisor import HeatingAdvisor
    svc = NS(friendly_names={TRV: "Kitchen TRV"})
    adv = HeatingAdvisor.__new__(HeatingAdvisor)
    adv._get_devices = lambda: {TRV: NS(state={"local_temperature": 20.0}, service=svc)}
    c.check("the history labels a device by its name, not its IEEE",
            [d["friendly_name"] for d in adv._find_hvac_devices()] == ["Kitchen TRV"])

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
