"""Per-device temperature freshness: each sensor judged against its own report
cadence (heating-controller.md, "Per-device freshness")."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace as NS

from harness import Checker, REPO

import modules.device_profiles as dp
import modules.heating_controller as hc
import modules.zmm_settings as zs

SENSOR = "ss:00:00:00:00:00:00:02"
TRV = "tt:00:00:00:00:00:00:02"
RX = "rr:00:00:00:00:00:00:02"

FP300 = dp.normalise_profile(json.loads(
    (REPO / "zmm_quirks" / "lumi.sensor_occupy.agl8.json").read_text()))
TRV_PROFILE = dp.normalise_profile({
    "id": "test.trv", "protocol": "zigbee", "match": {"model": "test.trv"},
    "reporting": [{"ep": 1, "cluster": "0x0201", "attr": "0x0000", "min": 30, "max": 1800, "delta": 10}]})


def dev(ieee, profile, **state):
    return NS(ieee=ieee, state=dict(state), _profile=profile)


def room(**kw):
    r = {"id": "kitchen", "name": "Kitchen", "target_temp": 20.0,
         "temperature_sensor_ieee": SENSOR, "external_temp_mode": "advisory",
         "trvs": [{"ieee": TRV}]}
    r.update(kw)
    return r


def decision(temp_source="external"):
    d = hc.RoomDecision("kitchen", "Kitchen")
    d.temp_source = temp_source
    d.trvs = [{"ieee": TRV, "online": True, "current_temp": 20.0}]
    return d


def run() -> Checker:
    c = Checker("freshness")
    saved = (dp.profile_for_device, hc._last_temperature_ts, hc._query_last_report_age,
             hc._is_flatlined, hc._device_settings_getter)
    dp.profile_for_device = lambda d: getattr(d, "_profile", None)
    hc._is_flatlined = lambda ieee: False
    zs._PENDING_READS.clear()
    ages = {}
    hc._last_temperature_ts = lambda ieee: (time.time() - ages[ieee]) if ieee in ages else None
    try:
        _cadence(c)
        _thresholds(c)
        _health(c, ages)
        _paths(c, ages)
    finally:
        (dp.profile_for_device, hc._last_temperature_ts, hc._query_last_report_age,
         hc._is_flatlined, hc._device_settings_getter) = saved
        zs._PENDING_READS.clear()
    return c


def _cadence(c):
    c.section("report cadence from the quirk")
    cad = dp.report_cadence(dev(SENSOR, FP300, temp_reporting_mode=3,
                                temperature_reporting_interval=3600), "temperature")
    c.check("FP300 set to 3600 s reads its setting",
            (cad["interval_sec"], cad["source"], cad["unread"]) == (3600.0, "setting", []), cad)
    cad = dp.report_cadence(dev(SENSOR, FP300, temp_reporting_mode="threshold and interval",
                                temperature_reporting_interval=1200), "temperature")
    c.check("a mode held as its label still counts as timed",
            cad["interval_sec"] == 1200.0 and not cad["change_only"], cad)
    cad = dp.report_cadence(dev(SENSOR, FP300), "temperature")
    c.check("settings never read: the quirk's max, and both asked for",
            cad["interval_sec"] == 3600.0 and cad["source"] == "setting_max"
            and set(cad["unread"]) == {"temp_reporting_mode", "temperature_reporting_interval"}, cad)
    cad = dp.report_cadence(dev(SENSOR, FP300, temp_reporting_mode=1,
                                temperature_reporting_interval=3600), "temperature")
    c.check("threshold-only mode is change-only", cad["change_only"] and cad["interval_sec"] is None, cad)
    cad = dp.report_cadence(dev(TRV, TRV_PROFILE), "temperature")
    c.check("a ZCL reporting row gives its max",
            (cad["interval_sec"], cad["source"]) == (1800.0, "reporting"), cad)
    cad = dp.report_cadence(dev(TRV, TRV_PROFILE), "temperature", requested_max=2400)
    c.check("the modal's saved reporting max outranks the entry's row",
            (cad["interval_sec"], cad["source"]) == (2400.0, "requested"), cad)
    cad = dp.report_cadence(dev(TRV, None), "temperature")
    c.check("no profile: nothing known", cad["interval_sec"] is None and not cad["change_only"], cad)


def _thresholds(c):
    c.section("thresholds")
    hc._device_settings_getter = None
    fp = dev(SENSOR, FP300, temp_reporting_mode=3, temperature_reporting_interval=3600)
    fr = hc._device_freshness(room(), fp, SENSOR)
    c.check("a1: 3600 s interval -> 2 x 3600 + 300 s", fr["threshold_sec"] == 7500.0, fr)
    fr = hc._device_freshness(room(freshness_threshold_minutes=30), fp, SENSOR)
    c.check("a4: an explicit room override wins", (fr["threshold_sec"], fr["source"]) == (1800.0, "room"), fr)
    fr = hc._device_freshness(room(), dev(TRV, None), TRV)
    c.check("a4: no metadata keeps the 15-min default", fr["threshold_sec"] == 900.0, fr)
    fast = dp.normalise_profile({"id": "t.fast", "protocol": "zigbee", "match": {"model": "t.fast"},
                                 "reporting": [{"ep": 1, "cluster": "0x0402", "attr": "0x0000", "max": 60}]})
    fr = hc._device_freshness(room(), dev(TRV, fast), TRV)
    c.check("a fast reporter never goes below the default", fr["threshold_sec"] == 900.0, fr)
    hc._device_settings_getter = lambda: {TRV: {"qos": 0, "updates": {"reporting_max": 1200}}}
    fr = hc._device_freshness(room(), dev(TRV, TRV_PROFILE), TRV)
    c.check("the saved modal value reaches the controller", fr["threshold_sec"] == 2700.0, fr)
    hc._device_settings_getter = None

    zs._PENDING_READS.clear()
    fresh = dev(SENSOR, FP300)
    fr = hc._device_freshness(room(), fresh, SENSOR)
    c.check("a2: unknown interval falls back to the quirk max", fr["threshold_sec"] == 7500.0, fr)
    c.check("a2: and asks for the settings at the next wake",
            zs._PENDING_READS.get(SENSOR) == {"temp_reporting_mode", "temperature_reporting_interval"},
            zs._PENDING_READS)


def _health(c, ages):
    c.section("room health")
    devices = {SENSOR: dev(SENSOR, FP300, temp_reporting_mode=3, temperature_reporting_interval=3600,
                           temperature=19.5),
               TRV: dev(TRV, None, local_temperature=20.0)}
    raw = {"temperature": 19.5}
    ages[SENSOR] = 65 * 60
    h = hc._check_room_health(room(), devices, decision(), True, raw)
    c.check("a1: 65 min silent on an hourly sensor is healthy", h["level"] == "ok", h)
    ages[SENSOR] = 7500 + 60
    h = hc._check_room_health(room(), devices, decision(), True, raw)
    c.check("a1: past 2 x interval + 5 min is critical", h["level"] == "critical", h)
    c.check("the reason names the expected interval and missed reports",
            any("every 60 min" in r and "2 reports are missed" in r for r in h["reasons"]), h["reasons"])
    devices[SENSOR].state["temp_reporting_mode"] = 1
    ages[SENSOR] = 3 * 3600
    h = hc._check_room_health(room(), devices, decision(), True, raw)
    c.check("a3: change-only mode is a config warning, not critical",
            h["level"] == "warning" and h["warnings"] and not h["reasons"], h)
    devices[SENSOR].state["temp_reporting_mode"] = 3
    ages[TRV] = 20 * 60
    ages[SENSOR] = 10 * 60
    h = hc._check_room_health(room(), devices, decision("trv"), True, raw)
    c.check("a TRV without metadata keeps the 15-min rule", h["level"] == "critical"
            and any(r.startswith(f"TRV {TRV}") for r in h["reasons"]), h)
    ages.pop(TRV)


def _paths(c, ages):
    c.section("a5: fallback and push use the same threshold")
    hc._query_last_report_age = lambda *a: None   # only needs to be set; the age comes from the stub
    cfg_room = room(external_temp_mode="push")
    cfg = {"enabled": True, "controller": {"enabled": True, "dry_run": False},
           "circuits": [{"id": "down", "name": "Down", "receiver_ieee": RX,
                         "receiver_command": "thermostat", "rooms": [cfg_room]}]}
    devices = {SENSOR: dev(SENSOR, FP300, temp_reporting_mode=3, temperature_reporting_interval=3600,
                           temperature=19.5),
               TRV: {"state": {"local_temperature": 22.0, "occupied_heating_setpoint": 20.0}},
               RX: {"model": "Generic", "state": {"system_mode": "off"}}}
    sent = []

    async def sender(ieee, command, value=None, endpoint_id=None):
        sent.append((ieee, command, value))
        return {"success": True}

    ctrl = hc.HeatingController(cfg, lambda: devices, sender)
    ctrl._overshoot = {}
    ages[SENSOR] = 65 * 60
    d = ctrl._evaluate_room(ctrl.circuits[0]["rooms"][0], devices, __import__("datetime").datetime.now())
    c.check("65 min silent: the sensor still steers the room",
            d.temp_source == "external" and not d.sensor_stale, (d.temp_source, d.sensor_stale))
    asyncio.run(ctrl._push_external_temps_once())
    c.check("65 min silent: the reading is still pushed to the TRV",
            any(x[0] == TRV and x[1] == "external_temp" for x in sent), sent)
    ages[SENSOR] = 7500 + 60
    d = ctrl._evaluate_room(ctrl.circuits[0]["rooms"][0], devices, __import__("datetime").datetime.now())
    c.check("past the threshold: falls back to the TRV", d.sensor_stale and d.temp_source != "external",
            (d.temp_source, d.sensor_stale))
    sent.clear()
    ctrl._last_ext_push = {}
    asyncio.run(ctrl._push_external_temps_once())
    c.check("past the threshold: nothing is pushed",
            not any(x[1] == "external_temp" for x in sent), sent)


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
