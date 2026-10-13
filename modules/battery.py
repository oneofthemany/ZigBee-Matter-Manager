"""
Battery level and voltage from whichever source gives a real one.

A device may answer Power Configuration, carry its level in a maker's status
blob, give only a voltage, or answer zeros on a cluster it does not use. Each
source offers what it has, and the strongest to have given a real value so
far owns the state key, so nothing is configured per model.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# Strongest first: a level the device states outranks one derived from a voltage,
# and the standard cluster outranks a maker's blob.
LEVEL_SOURCES = ("cluster_percent", "blob_percent", "cluster_volts", "blob_volts")
VOLT_SOURCES = ("cluster_volts", "blob_volts")

# CR2450 discharge curve (volts -> %), linearly interpolated. Coin cells hold
# ~3.0 V then fall away steeply below 2.9 V, so a flat 2.5-3.0 map over-reads.
COIN_CELL_CURVE = [
    (3.00, 100), (2.95, 90), (2.90, 70), (2.85, 55), (2.80, 45),
    (2.75, 35), (2.70, 25), (2.65, 18), (2.60, 12), (2.50, 5), (2.40, 0),
]
# One 3 V cell or two 1.5 V cells. A bigger pack passes through this range
# only when flat, where the curve would call it full.
DERIVE_MAX_V = 3.6
# What one or two cells, or a lithium cell, can read in millivolts.
CELL_MV = (1500, 4500)


def voltage_to_pct(voltage: Optional[float]) -> Optional[int]:
    """Map a coin-cell voltage to a percentage via the discharge curve."""
    if voltage is None:
        return None
    pts = COIN_CELL_CURVE
    if voltage >= pts[0][0]:
        return 100
    if voltage <= pts[-1][0]:
        return 0
    for (v_hi, p_hi), (v_lo, p_lo) in zip(pts, pts[1:]):
        if v_lo <= voltage <= v_hi:
            frac = (voltage - v_lo) / (v_hi - v_lo)
            return int(round(p_lo + frac * (p_hi - p_lo)))
    return None


def _owns(device, key: str, source: str, order) -> bool:
    """True, and recorded, unless a stronger source already holds `key`."""
    owners = device.__dict__.setdefault("_battery_owner", {})
    held = owners.get(key)
    if held is not None and order.index(held) < order.index(source):
        return False
    owners[key] = source
    return True


def offer(device, source: str, percent: Optional[float] = None,
          volts: Optional[float] = None) -> Dict[str, Any]:
    """The state updates `source` may make with what it read. 0 V is no
    reading (a flat device does not report), so it claims nothing."""
    updates: Dict[str, Any] = {}
    if percent is not None and _owns(device, "battery", source, LEVEL_SOURCES):
        updates["battery"] = percent
    if volts is not None and volts > 0:
        if _owns(device, "battery_voltage", source, VOLT_SOURCES):
            updates["battery_voltage"] = volts
        if volts <= DERIVE_MAX_V and _owns(device, "battery", source, LEVEL_SOURCES):
            updates["battery"] = voltage_to_pct(volts)
    return updates


def release(device, key: str, source: str) -> None:
    """Withdraw `source`'s hold on `key`: what it gave was not a reading."""
    owners = device.__dict__.get("_battery_owner") or {}
    if owners.get(key) == source:
        del owners[key]
