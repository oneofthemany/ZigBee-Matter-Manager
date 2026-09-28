"""
Physical plausibility of electrical readings.

Bounds come from what the endpoint is, not from known bad numbers: after a
power cut the Aurora double socket reports -8011 W on one EP and +8011 W on
the other, but any firmware can glitch to any value. A switched outlet cannot
export power or carry more than a mains socket's circuit; mains voltage has a
range. A ZMM entry can give a model's own limits (`zmm.measurements.<name>`
`min`/`max`).

A reading outside its bounds is held back (no state, no history). If no
power-of-ten rescale could make it plausible, or the EP has already read
sensibly, it is a device fault: flagged once per episode, cleared on the next
sane reading. If a rescale would fit a reading with no such history, the
scaling is suspect instead, and the device is not blamed.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger("modules.measurement_sanity")

# Above any single mains socket (16 A at 250 V) with headroom; below the glitch.
SWITCHED_OUTLET_MAX_W = 4000.0
SWITCHED_OUTLET_MAX_A = 20.0
MAINS_V = (80.0, 300.0)

UNITS = {"active_power": "W", "rms_voltage": "V", "rms_current": "A"}

# ieee -> {(ep, measurement): (value, reason)}
_faults: Dict[str, Dict[Tuple[int, str], Tuple[float, str]]] = {}
_seen_sane: set = set()      # (ieee, ep, measurement) with a plausible reading this run
_unscaled: set = set()       # (ieee, ep, measurement) already reported as unscaled


def _switches_a_load(handler) -> bool:
    return 0x0006 in (getattr(handler.endpoint, "in_clusters", None) or {})


def bounds(handler, measurement: str) -> Tuple[Optional[float], Optional[float], str]:
    """(min, max, what the bounds rest on) for this EP's measurement."""
    try:
        entry = handler._entry_measurements().get(measurement) or {}
    except Exception:
        entry = {}
    lo, hi = entry.get("min"), entry.get("max")
    if lo is not None or hi is not None:
        return lo, hi, "the model's rating in its ZMM entry"
    if measurement == "rms_voltage":
        return MAINS_V[0], MAINS_V[1], "mains voltage"
    if not _switches_a_load(handler):
        return None, None, ""       # a meter or clamp may export, and carry anything
    if measurement == "active_power":
        return 0.0, SWITCHED_OUTLET_MAX_W, "a switched outlet cannot export or exceed a socket circuit"
    if measurement == "rms_current":
        return 0.0, SWITCHED_OUTLET_MAX_A, "a switched outlet cannot exceed a socket circuit"
    return None, None, ""


def _within(value: float, lo: Optional[float], hi: Optional[float]) -> bool:
    return (lo is None or value >= lo) and (hi is None or value <= hi)


def implausible(handler, measurement: str, value: float) -> Optional[str]:
    """Why `value` cannot be real for this EP, or None."""
    lo, hi, basis = bounds(handler, measurement)
    unit = UNITS.get(measurement, "")
    if lo is not None and value < lo:
        return f"{value:g} {unit} is below {lo:g} {unit} ({basis})"
    if hi is not None and value > hi:
        return f"{value:g} {unit} is above {hi:g} {unit} ({basis})"
    return None


def rescale_that_fits(handler, measurement: str, value: float) -> Optional[int]:
    """The power of ten that would bring `value` within bounds, if any: a
    missing divisor (decivolts read as volts) rather than a faulty device."""
    lo, hi, _ = bounds(handler, measurement)
    for divisor in (10, 100, 1000):
        if _within(value / divisor, lo, hi):
            return divisor
    return None


def judge(handler, measurement: str, value: float) -> Tuple[str, Optional[str]]:
    """("ok" | "fault" | "unscaled", reason). A reading no rescale can save is a
    fault; one a rescale would fit is a fault only when this EP has already
    read sensibly this run, or another reading on the device is faulting:
    otherwise it is more likely unscaled than broken."""
    reason = implausible(handler, measurement, value)
    if reason is None:
        return "ok", None
    divisor = rescale_that_fits(handler, measurement, value)
    if divisor is None:
        return "fault", reason
    ieee, ep = str(handler.device.ieee), handler.endpoint.endpoint_id
    if (ieee, ep, measurement) in _seen_sane or _faults.get(ieee):
        return "fault", reason
    return "unscaled", f"{reason}; /{divisor} would fit, so the scaling is suspect"


def unscaled(handler, measurement: str, reason: str) -> bool:
    """Record a reading held back for suspect scaling (no alert: the device is
    not at fault). True the first time for this EP this run."""
    from modules import device_decisions
    ieee, ep = str(handler.device.ieee), handler.endpoint.endpoint_id
    key = (ieee, ep, measurement)
    if key in _unscaled:
        return False
    _unscaled.add(key)
    logger.warning(f"[{ieee}] EP{ep} {measurement} {reason}: held back")
    device_decisions.record(ieee, ep, f"readings:{measurement}", "unscaled", "rule", reason)
    return True


def _name(device) -> str:
    ieee = str(device.ieee)
    names = getattr(getattr(device, "service", None), "friendly_names", None) or {}
    return names.get(ieee) or ieee


def _alert_key(ieee: str) -> str:
    return f"implausible_readings:{ieee}"


def fault(handler, measurement: str, value: float, reason: str) -> bool:
    """Record an implausible reading. True when this EP/measurement is newly faulty."""
    from modules import device_decisions
    device = handler.device
    ieee, ep = str(device.ieee), handler.endpoint.endpoint_id
    faults = _faults.setdefault(ieee, {})
    new = (ep, measurement) not in faults
    faults[(ep, measurement)] = (value, reason)
    if not new:
        return False
    name = _name(device)
    logger.warning(f"[{ieee}] EP{ep} {measurement} {reason}: held back")
    device_decisions.record(ieee, ep, f"readings:{measurement}", "implausible", "rule", reason)
    detail = "; ".join(f"EP{e} {m.replace('_', ' ')} {r}" for (e, m), (_, r) in sorted(faults.items()))
    try:
        from modules.app_alerts import raise_alert
        raise_alert("warning", "devices", f"{name} needs a power cycle",
                    f"{name} is reporting readings it cannot physically produce: {detail}. "
                    "They are kept out of its state and history. Power-cycle the device; "
                    "this clears when it reports sensibly again.",
                    dedupe_key=_alert_key(ieee), data={"ieee": ieee})
    except Exception as e:
        logger.debug(f"[{ieee}] alert not raised: {e}")
    return True


def sane(handler, measurement: str) -> bool:
    """A plausible reading arrived. True when it ends this EP's fault."""
    from modules import device_decisions
    ieee, ep = str(handler.device.ieee), handler.endpoint.endpoint_id
    _seen_sane.add((ieee, ep, measurement))
    if (ieee, ep, measurement) in _unscaled:
        _unscaled.discard((ieee, ep, measurement))
        device_decisions.record(ieee, ep, f"readings:{measurement}", "ok", "rule", "plausible again")
    faults = _faults.get(ieee) or {}
    if (ep, measurement) not in faults:
        return False
    del faults[(ep, measurement)]
    device_decisions.record(ieee, ep, f"readings:{measurement}", "ok", "rule", "plausible again")
    logger.info(f"[{ieee}] EP{ep} {measurement} readings plausible again")
    if not faults:
        _faults.pop(ieee, None)
        try:
            from modules.app_alerts import resolve_alert
            resolve_alert(_alert_key(ieee))
        except Exception as e:
            logger.debug(f"[{ieee}] alert not resolved: {e}")
    return True


def history_bounds(device) -> Tuple[Dict[str, Tuple[Optional[float], Optional[float]]],
                                   Dict[str, list]]:
    """The same bounds, keyed by the state attributes history stores them under,
    and the aliases derived from them (telemetry_db.implausible_states)."""
    keys = {"active_power": "power_{}", "rms_voltage": "voltage_{}", "rms_current": "current_{}"}
    out: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
    for key, h in (getattr(device, "handlers", None) or {}).items():
        if not (isinstance(key, tuple) and key[1] == 0x0B04):
            continue
        for measurement, attr in keys.items():
            lo, hi, _ = bounds(h, measurement)
            if lo is not None or hi is not None:
                stored = h._power_key() if measurement == "active_power" and hasattr(h, "_power_key") \
                    else attr.format(key[0])
                out[stored] = (lo, hi)
    aliases = {"power": [a for a in out if a.startswith("power_")],
               "voltage": [a for a in out if a == "voltage_1"],
               "current": [a for a in out if a == "current_1"]}
    return out, {k: v for k, v in aliases.items() if v}


def faults_for(ieee: str) -> Dict[Tuple[int, str], Tuple[float, str]]:
    return dict(_faults.get(ieee) or {})


def forget(ieee: str) -> None:
    _faults.pop(ieee, None)
    for pool in (_seen_sane, _unscaled):
        for key in [k for k in pool if k[0] == ieee]:
            pool.discard(key)
