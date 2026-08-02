"""
Measured solar heating impact per room, from telemetry already collected.

The empirical counterpart to solar_gain.py: fits a no-solar baseline from
night-time cool-down windows, then reads the residual warmth of sunlit ones.
Method and its caveats: docs/heating.md.
"""

from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("zbm.solar_impact")

# Clear-sky model average that marks a cool-down window as "sunlit".
SUNLIT_MIN_MODELLED_W = 30.0
# ...and the ceiling below which a window counts as solar-free baseline.
BASELINE_MAX_MODELLED_W = 5.0
# Baseline windows needed before sunlit residuals are trusted at all.
MIN_BASELINE_WINDOWS = 2
# Newton-fit quality gate (same threshold the diagnostics funnel uses).
FIT_MIN_R2 = 0.5
# Sampling step when averaging the clear-sky model across a window.
MODEL_SAMPLE_STEP_S = 20 * 60
# Temperature attributes to try, in order (mirrors the diagnostics route).
TEMP_ATTRS = ("temperature", "local_temperature",
              "current_temperature", "internal_temperature")


def _median(vals: List[float]) -> Optional[float]:
    return statistics.median(vals) if vals else None


def _room_has_window_geometry(room: Dict[str, Any]) -> bool:
    dims = room.get("dimensions") or {}
    return bool(dims.get("windows"))


def modelled_clearsky_watts(
        room: Dict[str, Any],
        lat: float,
        lon: float,
        t0: float,
        t1: float,
        step_s: int = MODEL_SAMPLE_STEP_S,
) -> Optional[float]:
    """
    Average clear-sky solar gain [W] for the room over [t0, t1] (unix secs).
    None when the room has no window geometry. Cloud-free by construction —
    an upper bound; the measured/modelled ratio absorbs average cloudiness.
    """
    if not _room_has_window_geometry(room):
        return None
    try:
        from modules.sun_position import sun_position
        from modules.solar_gain import _room_gain_at_position
    except ImportError:                                   # flat-path fallback
        from sun_position import sun_position             # type: ignore
        from solar_gain import _room_gain_at_position     # type: ignore

    total = 0.0
    n = 0
    ts = float(t0)
    end = float(t1)
    while ts <= end:
        pos = sun_position(lat, lon, datetime.fromtimestamp(ts, tz=timezone.utc))
        if pos.get("is_daylight"):
            total += _room_gain_at_position(
                room_config=room,
                sun_azimuth_deg=pos["azimuth_deg"],
                sun_elevation_deg=pos["elevation_deg"],
                shortwave_wm2=None,
                cloud_fraction=0.0,
            )
        n += 1
        ts += step_s
    return (total / n) if n else None


def _all_dark(lat: float, lon: float, t0: float, t1: float,
              step_s: int = MODEL_SAMPLE_STEP_S) -> bool:
    """True when the sun is below the horizon for the whole interval."""
    try:
        from modules.sun_position import sun_position
    except ImportError:
        from sun_position import sun_position  # type: ignore
    ts = float(t0)
    while ts <= float(t1):
        pos = sun_position(lat, lon, datetime.fromtimestamp(ts, tz=timezone.utc))
        if pos.get("is_daylight"):
            return False
        ts += step_s
    return True


def _find_warmup_windows(
        temp_series: List[Dict[str, Any]],
        min_duration_sec: int = 30 * 60,
        max_duration_sec: int = 6 * 3600,
        min_rise_c: float = 0.3,
        heating_state_getter: Optional[Callable[[float], Optional[bool]]] = None,
        heating_active_tolerance: float = 0.1,
) -> List[Tuple[float, float, List[Tuple[float, float]]]]:
    """
    Mirror of thermal_profile._find_cooldown_windows for RISING runs: heating
    is off but the temperature climbs anyway. These are the strongest solar
    events — a cooling-only detector would silently drop them and bias the
    measured solar gain low. Returns [(start_ts, end_ts, samples)] with
    samples as (elapsed_seconds, temp_c).
    """
    if not temp_series or len(temp_series) < 4:
        return []
    pts: List[Tuple[float, float]] = []
    for p in temp_series:
        v = p.get("numeric_val")
        if v is None:
            try:
                v = float(p.get("value"))
            except (TypeError, ValueError):
                continue
        ts = p.get("ts")
        if isinstance(ts, datetime):
            pts.append((ts.timestamp(), float(v)))
    if len(pts) < 4:
        return []
    pts.sort(key=lambda x: x[0])

    windows: List[Tuple[float, float, List[Tuple[float, float]]]] = []

    def _commit_span(i0: int, i1: int) -> None:
        if i1 - i0 < 3:
            return
        t0, t1 = pts[i0][0], pts[i1][0]
        if t1 - t0 < min_duration_sec:
            return
        if pts[i1][1] - pts[i0][1] < min_rise_c:
            return
        if heating_state_getter is not None:
            tainted = 0
            total = i1 - i0 + 1
            for p in pts[i0:i1 + 1]:
                try:
                    if heating_state_getter(p[0]):
                        tainted += 1
                except Exception:
                    return  # gate failure → fail closed
            if total == 0 or (tainted / total) > heating_active_tolerance:
                return
        windows.append((t0, t1, [(p[0] - t0, p[1]) for p in pts[i0:i1 + 1]]))

    def _commit(i0: int, i1: int) -> None:
        # A strong sunny day produces one long monotonic rise; splitting it
        # into <= max_duration chunks keeps every chunk usable rather than
        # discarding the best solar events of all.
        start = i0
        chunk_t0 = pts[i0][0]
        for i in range(i0 + 1, i1 + 1):
            if pts[i][0] - chunk_t0 >= max_duration_sec:
                _commit_span(start, i)
                start = i
                chunk_t0 = pts[i][0]
        if start < i1:
            _commit_span(start, i1)

    NOISE = 0.1  # °C — tolerate tiny dips inside a warm-up run
    cur_start_i = 0
    for i in range(1, len(pts)):
        if pts[i][1] < pts[i - 1][1] - NOISE:
            _commit(cur_start_i, i - 1)
            cur_start_i = i
    _commit(cur_start_i, len(pts) - 1)
    return windows


def analyse_from_series(
        room: Dict[str, Any],
        temp_series: List[Dict[str, Any]],
        tick_rows: Optional[List[Dict[str, Any]]],
        outdoor_getter: Optional[Callable[[float], Optional[float]]],
        current_outdoor: Optional[float],
        lat: Optional[float],
        lon: Optional[float],
        insulation: str = "partial",
        floor_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Pure analysis core — everything it needs is passed in, so it can be unit
    tested with synthetic series. `analyse_room` is the data-fetching wrapper.
    """
    from modules.thermal_profile import (
        _find_cooldown_windows, _fit_newton_cooling, compute_profile,
        LEARN_MIN_DURATION_SEC, LEARN_MIN_DROP_C, LEARN_MAX_DURATION_SEC,
    )

    result: Dict[str, Any] = {
        "room_id": str(room.get("id")),
        "room_name": room.get("name"),
        "status": "ok",
        "counts": {
            "cooldown_windows": 0, "baseline": 0, "sunlit": 0,
            "sunlit_warmups": 0, "ambiguous": 0, "baseline_fit_ok": 0,
            "skipped": 0,
        },
        "tau_night_hours": None,
        "thermal": {"w_per_k": None, "capacitance_kj_per_k": None},
        "solar": {
            "measured_w_median": None,
            "modelled_clearsky_w_median": None,
            "calibration_ratio": None,
            "events": [],
        },
        "confidence": "none",
        "has_window_geometry": _room_has_window_geometry(room),
    }

    if not temp_series or len(temp_series) < 10:
        result["status"] = "no_telemetry"
        return result
    if lat is None or lon is None:
        result["status"] = "location_missing"
        return result

    # Heating-state gate, same as the τ-learning pipeline
    gate = None
    if tick_rows:
        try:
            from modules.heating_anomaly_watcher import _build_heating_state_getter
            gate = _build_heating_state_getter(tick_rows)
        except Exception:
            gate = None

    cooldowns = _find_cooldown_windows(
        temp_series, None,
        min_duration_sec=LEARN_MIN_DURATION_SEC,
        max_duration_sec=LEARN_MAX_DURATION_SEC,
        min_drop_c=LEARN_MIN_DROP_C,
        heating_state_getter=gate,
    )
    result["counts"]["cooldown_windows"] = len(cooldowns)
    if not cooldowns:
        result["status"] = "no_cooldown_windows"
        return result

    def _outdoor_at(ts: float) -> Optional[float]:
        if outdoor_getter is not None:
            try:
                v = outdoor_getter(ts)
                if v is not None:
                    return float(v)
            except Exception:
                pass
        return current_outdoor

    # Classify windows against the clear-sky model
    baseline_windows: List[Tuple[float, float, list]] = []
    sunlit_windows: List[Tuple[float, float, list, float]] = []
    has_geometry = result["has_window_geometry"]
    for (t0, t1, samples) in cooldowns:
        if has_geometry:
            mw = modelled_clearsky_watts(room, lat, lon, t0, t1)
        else:
            mw = None
        if mw is None:
            # No geometry: only strictly-dark intervals are safe baseline.
            if _all_dark(lat, lon, t0, t1):
                baseline_windows.append((t0, t1, samples))
            else:
                result["counts"]["skipped"] += 1
        elif mw <= BASELINE_MAX_MODELLED_W:
            baseline_windows.append((t0, t1, samples))
        elif mw >= SUNLIT_MIN_MODELLED_W:
            sunlit_windows.append((t0, t1, samples, mw))
        else:
            result["counts"]["ambiguous"] += 1
    result["counts"]["baseline"] = len(baseline_windows)
    result["counts"]["sunlit"] = len(sunlit_windows)

    # Baseline: the room's own no-solar cooling constant
    taus: List[float] = []
    for (t0, t1, samples) in baseline_windows:
        out = _outdoor_at((t0 + t1) / 2)
        if out is None:
            continue
        tau, r2 = _fit_newton_cooling(samples, out)
        if tau is not None and r2 is not None and r2 >= FIT_MIN_R2:
            taus.append(tau)
    result["counts"]["baseline_fit_ok"] = len(taus)
    if len(taus) < MIN_BASELINE_WINDOWS:
        result["status"] = "insufficient_baseline"
        return result
    tau_night = _median(taus)
    result["tau_night_hours"] = round(tau_night / 3600.0, 2)

    # Thermal capacitance from the room's profile
    capacitance_j_per_k: Optional[float] = None
    try:
        profile = compute_profile(
            room_id=str(room.get("id")),
            dimensions=room.get("dimensions"),
            insulation=insulation,
            temperature_series=temp_series,
            outdoor_temp_getter=outdoor_getter,
            heating_state_getter=gate,
            floor_plan=floor_plan,
            floor_plan_ref=room.get("floor_plan_ref"),
        )
        w_per_k = profile.blended_w_per_k
        if w_per_k:
            result["thermal"]["w_per_k"] = round(w_per_k, 1)
            capacitance_j_per_k = w_per_k * tau_night
            result["thermal"]["capacitance_kj_per_k"] = round(capacitance_j_per_k / 1000.0, 1)
    except Exception as e:
        logger.debug(f"solar_impact: profile failed for {room.get('id')}: {e}")

    # Warm-up windows: heating off but temperature RISING
    # The strongest solar events. Direct energy balance:
    #   W_solar = C·dT/dt + UA·(T_avg − T_out)
    warmups = _find_warmup_windows(temp_series, heating_state_getter=gate)
    sunlit_warmups: List[Tuple[float, float, list, float]] = []
    for (t0, t1, samples) in warmups:
        mw = modelled_clearsky_watts(room, lat, lon, t0, t1) if has_geometry else None
        if mw is not None and mw >= SUNLIT_MIN_MODELLED_W:
            sunlit_warmups.append((t0, t1, samples, mw))
    result["counts"]["sunlit_warmups"] = len(sunlit_warmups)

    if not sunlit_windows and not sunlit_warmups:
        result["status"] = "no_sunlit_windows" if has_geometry else "no_window_geometry"
        return result

    w_per_k_val = result["thermal"]["w_per_k"]

    # Sunlit residuals vs the night baseline
    events: List[Dict[str, Any]] = []
    measured_ws: List[float] = []
    modelled_ws: List[float] = []
    for (t0, t1, samples, mw) in sunlit_windows:
        out = _outdoor_at((t0 + t1) / 2)
        if out is None:
            result["counts"]["skipped"] += 1
            continue
        T0 = samples[0][1]
        T_end = samples[-1][1]
        duration_s = samples[-1][0] - samples[0][0]
        if duration_s <= 0:
            continue
        predicted_end = out + (T0 - out) * math.exp(-duration_s / tau_night)
        residual_c = T_end - predicted_end
        ev: Dict[str, Any] = {
            "kind": "slow_cooling",
            "start_ts": t0,
            "end_ts": t1,
            "duration_min": round(duration_s / 60.0),
            "modelled_clearsky_w": round(mw, 1),
            "residual_c": round(residual_c, 3),
            "residual_c_per_h": round(residual_c / (duration_s / 3600.0), 3),
        }
        modelled_ws.append(mw)
        if capacitance_j_per_k:
            measured_w = capacitance_j_per_k * residual_c / duration_s
            ev["measured_w"] = round(measured_w, 1)
            measured_ws.append(measured_w)
        events.append(ev)

    # Warm-up events — direct energy balance, no baseline prediction needed
    for (t0, t1, samples, mw) in sunlit_warmups:
        out = _outdoor_at((t0 + t1) / 2)
        if out is None:
            result["counts"]["skipped"] += 1
            continue
        T0 = samples[0][1]
        T_end = samples[-1][1]
        duration_s = samples[-1][0] - samples[0][0]
        if duration_s <= 0:
            continue
        rise_c = T_end - T0
        t_avg = sum(t for _, t in samples) / len(samples)
        ev = {
            "kind": "solar_rise",
            "start_ts": t0,
            "end_ts": t1,
            "duration_min": round(duration_s / 60.0),
            "modelled_clearsky_w": round(mw, 1),
            "residual_c": round(rise_c, 3),
            "residual_c_per_h": round(rise_c / (duration_s / 3600.0), 3),
        }
        modelled_ws.append(mw)
        if capacitance_j_per_k and w_per_k_val:
            measured_w = (capacitance_j_per_k * rise_c / duration_s
                          + w_per_k_val * max(0.0, t_avg - out))
            ev["measured_w"] = round(measured_w, 1)
            measured_ws.append(measured_w)
        events.append(ev)

    events.sort(key=lambda e: e["end_ts"], reverse=True)
    result["solar"]["events"] = events[:10]
    result["solar"]["modelled_clearsky_w_median"] = (
        round(_median(modelled_ws), 1) if modelled_ws else None)
    if measured_ws:
        med_meas = _median(measured_ws)
        result["solar"]["measured_w_median"] = round(med_meas, 1)
        med_model = _median(modelled_ws)
        if med_model and med_model > 0:
            result["solar"]["calibration_ratio"] = round(med_meas / med_model, 2)

    # Confidence
    n_sun = len(events)
    n_base = len(taus)
    if measured_ws and n_sun >= 5 and n_base >= 4:
        result["confidence"] = "high"
    elif measured_ws and n_sun >= 2 and n_base >= MIN_BASELINE_WINDOWS:
        result["confidence"] = "medium"
    elif events:
        result["confidence"] = "low"
    return result


def analyse_room(
        room: Dict[str, Any],
        circuit_id: Optional[str],
        lat: Optional[float],
        lon: Optional[float],
        days: int = 14,
        insulation: str = "partial",
        floor_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Fetch this room's telemetry and delegate to `analyse_from_series`."""
    room_id = str(room.get("id"))
    sensor_ieee = room.get("temperature_sensor_ieee")
    if not sensor_ieee:
        trvs = room.get("trvs") or []
        if trvs and isinstance(trvs[0], dict):
            sensor_ieee = trvs[0].get("ieee")
    if not sensor_ieee:
        return {"room_id": room_id, "room_name": room.get("name"),
                "status": "no_sensor", "confidence": "none"}

    hours = max(24, int(days) * 24)
    temp_series: List[Dict[str, Any]] = []
    try:
        from modules.telemetry_db import query_device_state_history
        for attr in TEMP_ATTRS:
            rows = query_device_state_history(sensor_ieee, attr, hours) or []
            if rows:
                temp_series = rows
                break
    except Exception as e:
        logger.warning(f"solar_impact: telemetry fetch failed for {room_id}: {e}")

    tick_rows: List[Dict[str, Any]] = []
    if circuit_id:
        try:
            from modules.telemetry_db import query_room_heating_state
            tick_rows = query_room_heating_state(
                circuit_id=circuit_id, room_id=room_id, hours=hours)
        except Exception as e:
            logger.debug(f"solar_impact: tick fetch failed for {room_id}: {e}")

    outdoor_getter = None
    current_outdoor = None
    try:
        from modules.telemetry_db import build_outdoor_temp_getter
        outdoor_getter = build_outdoor_temp_getter(hours)
        if outdoor_getter is not None:
            current_outdoor = outdoor_getter(datetime.now(timezone.utc).timestamp())
    except Exception as e:
        logger.debug(f"solar_impact: outdoor getter failed: {e}")

    out = analyse_from_series(
        room=room,
        temp_series=temp_series,
        tick_rows=tick_rows,
        outdoor_getter=outdoor_getter,
        current_outdoor=current_outdoor,
        lat=lat, lon=lon,
        insulation=insulation,
        floor_plan=floor_plan,
    )
    out["sensor_ieee"] = sensor_ieee
    out["circuit_id"] = circuit_id
    return out
