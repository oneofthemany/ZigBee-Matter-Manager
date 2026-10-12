"""
Heating anomaly watcher — on a timer, pulls recent telemetry for each room with
a known baseline tau and looks for fast-cool / slow-heat anomalies.

Active ones stay in memory for the dashboard and tips; resolved ones move to a
short history buffer so the UI can briefly show "was" cards.
See docs/heating.md.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional
# Used to build the heating-off gate passed into compute_profile, so cool-down
# windows that overlapped active heating are excluded from the baseline fit.
try:
    from modules.telemetry_db import query_room_heating_state
except Exception:
    query_room_heating_state = None

try:
    from modules.telemetry_db import query_state_since_change_sec
except Exception:
    query_state_since_change_sec = None

logger = logging.getLogger("modules.heating_anomaly_watcher")

SCAN_INTERVAL_SEC = 300        # every 5 minutes
HISTORY_KEEP_SEC = 6 * 3600    # keep resolved anomalies for 6h on dashboard


# Ticks are ~1/min and telemetry samples vary, so the gate is a piecewise lookup
# returning True for any timestamp inside or just after a "heating active" tick,
# with a staleness grace so gaps in the tick log do not silently read as "off".

GATE_STALENESS_SEC = 180   # 3× expected tick interval — if the last tick
# is older than this, we don't trust the gate
# and conservatively return True (i.e. assume
# heating was on, rejecting the window).


def _build_heating_state_getter(tick_rows: List[Dict[str, Any]]):
    """
    Return a callable `getter(ts_seconds) -> bool` where True means
    "heating was likely active at ts, so this sample is not a clean
    natural cool-down and should not feed the baseline fit."

    tick_rows come from telemetry_db.query_room_heating_state and are
    already sorted ascending by ts. Each row carries a boolean
    'heating_active'.

    If we have no tick data at all (e.g. the new schema was just added
    and nothing has been written yet), the getter returns False — i.e.
    no gate is applied. This preserves today's behaviour for a fresh
    install, so we never regress the data flow.
    """
    if not tick_rows:
        return lambda _ts: False

    # Pre-extract as two parallel lists for bisect.
    import bisect
    import datetime as _dt

    tick_ts: List[float] = []
    tick_active: List[bool] = []
    for r in tick_rows:
        t = r.get("ts")
        if isinstance(t, _dt.datetime):
            tick_ts.append(t.timestamp())
        elif isinstance(t, (int, float)):
            tick_ts.append(float(t))
        else:
            continue
        tick_active.append(bool(r.get("heating_active")))

    if not tick_ts:
        return lambda _ts: False

    def _getter(ts_seconds: float) -> bool:
        # Find the last tick at or before ts_seconds.
        idx = bisect.bisect_right(tick_ts, ts_seconds) - 1
        if idx < 0:
            # ts is before our earliest tick → we don't know.
            # Conservative: assume on (exclude from baseline).
            return True
        last_ts = tick_ts[idx]
        # If the tick is too stale, don't trust it.
        if ts_seconds - last_ts > GATE_STALENESS_SEC:
            return True
        return tick_active[idx]

    return _getter

def _duration(seconds: float) -> str:
    mins = max(1, round(seconds / 60))
    return f"{mins} min" if mins < 60 else f"{mins // 60} h {mins % 60} min"


def _names(names: List[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def contact_cause(contacts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    What a room's contact sensors say about a fast cool-down — a fact where
    the detector alone can only guess.

    Each contact: ``{name, is_open, open_for_sec, open_in_window_sec}``, with
    ``is_open`` None when the sensor's state is unknown. Returns
    ``{kind, text, sensors}`` or None when no contact has a known state, which
    leaves the detector's own suggestion standing.
      open         one is open now
      was_open     none is open now, but one was during the cooling window
      all_closed   every one stayed closed — the cause is elsewhere
    """
    known = [c for c in contacts if c.get("is_open") is not None]
    if not known:
        return None
    open_now = [c for c in known if c["is_open"]]
    if open_now:
        parts = []
        for c in open_now:
            since = c.get("open_for_sec")
            parts.append(c["name"] + (f" (open {_duration(since)})" if since else ""))
        verb = "is" if len(open_now) == 1 else "are"
        return {"kind": "open", "sensors": [c["name"] for c in open_now],
                "text": f"{_names(parts)} {verb} open."}
    was_open = [c for c in known if (c.get("open_in_window_sec") or 0) >= 60]
    if was_open:
        parts = [f"{c['name']} was open for {_duration(c['open_in_window_sec'])}" for c in was_open]
        return {"kind": "was_open", "sensors": [c["name"] for c in was_open],
                "text": f"{_names(parts)} while it cooled; closed now."}
    return {"kind": "all_closed", "sensors": [c["name"] for c in known],
            "text": f"{_names([c['name'] for c in known])} stayed closed — look for a window "
                    f"or door without a sensor, or a sudden change in the weather."}


def _open_seconds_in_window(rows: List[Dict[str, Any]], t0: float, t1: float) -> float:
    """Seconds an `is_open` history was True inside [t0, t1]. Row timestamps are
    read the same naive way as the temperature series the window came from."""
    import datetime as _dt
    total, prev_ts, prev_open = 0.0, None, None
    for r in rows:
        ts = r.get("ts")
        ts = ts.timestamp() if isinstance(ts, _dt.datetime) else float(ts or 0)
        if prev_open:
            total += max(0.0, min(ts, t1) - max(prev_ts, t0))
        prev_ts, prev_open = ts, str(r.get("value")).strip().lower() in ("true", "1", "open")
    if prev_open:
        total += max(0.0, t1 - max(prev_ts, t0))
    return total


class HeatingAnomalyWatcher:
    def __init__(
            self,
            config_getter: Callable[[], Dict[str, Any]],
            advisor_getter: Callable[[], Any],
            telemetry_query: Callable[[str, str, int], List[Dict[str, Any]]],
    ):
        self.config_getter = config_getter
        self.advisor_getter = advisor_getter
        self.telemetry_query = telemetry_query

        # Keyed by (circuit_id, room_id, kind) so the same room can't have
        # two fast-cool anomalies stacking.
        self._active: Dict[tuple, Dict[str, Any]] = {}
        self._history: List[Dict[str, Any]] = []
        self._task: Optional[asyncio.Task] = None
        self._last_scan_ts: float = 0.0

    def start(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Heating anomaly watcher started")

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run_loop(self):
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Anomaly scan failed: {e}", exc_info=True)
            await asyncio.sleep(SCAN_INTERVAL_SEC)

    async def scan_once(self) -> int:
        """Perform one scan across all configured rooms. Returns new anomaly count.

        The scan is pure sync work — DuckDB queries (first touch after boot
        opens/migrates the DB: seconds) plus profile math — so it runs in a
        worker thread to keep the event loop responsive.
        """
        return await asyncio.to_thread(self._scan_sync)

    def _scan_sync(self) -> int:
        from modules.thermal_profile import (
            detect_fast_cooling, detect_slow_heating,
            compute_profile,
        )

        cfg = self.config_getter() or {}
        heating = cfg.get("heating") or {}
        insulation = (heating.get("property") or {}).get("insulation", "partial")
        adv = self.advisor_getter()

        outdoor_getter = None
        try:
            from modules.telemetry_db import build_outdoor_temp_getter
            outdoor_getter = build_outdoor_temp_getter(72)
        except Exception:
            pass

        outdoor = None
        if adv and getattr(adv, "weather", None):
            try:
                outdoor = adv.weather.get_outdoor_temperature()
            except Exception:
                pass
        if outdoor is None:
            outdoor = 10.0

        if outdoor_getter is None:
            _const_outdoor = outdoor
            outdoor_getter = lambda _ts: _const_outdoor

        new_count = 0
        seen_keys = set()
        # Work on copies; swap in atomically at the end so get_snapshot()
        # (loop thread) never sees a half-updated scan.
        active = dict(self._active)
        history = list(self._history)

        for circuit in (heating.get("circuits") or []):
            cid = str(circuit.get("id"))
            for room in (circuit.get("rooms") or []):
                rid = str(room.get("id"))
                dimensions = room.get("dimensions")

                # Pick sensor
                sensor_ieee = room.get("temperature_sensor_ieee")
                if not sensor_ieee:
                    trvs = room.get("trvs") or []
                    if trvs and isinstance(trvs[0], dict):
                        sensor_ieee = trvs[0].get("ieee")
                if not sensor_ieee:
                    continue

                # Baseline tau from 14-day profile
                long_series = []
                try:
                    for attr in ("temperature", "local_temperature",
                                 "current_temperature", "internal_temperature"):
                        rows = self.telemetry_query(sensor_ieee, attr, 14 * 24) or []
                        if rows:
                            long_series = rows
                            break
                except Exception as e:
                    logger.debug(f"long-series fetch failed for {sensor_ieee}: {e}")
                    continue

                # Build the heating-off gate from persisted controller ticks.
                # If the schema/function isn't available (fresh install, old
                # build), or there are simply no ticks yet, the getter is
                # None and compute_profile falls back to today's behaviour.
                heating_state_getter = None
                if query_room_heating_state is not None:
                    try:
                        tick_rows = query_room_heating_state(
                            circuit_id=cid, room_id=rid, hours=14 * 24,
                        )
                        if tick_rows:
                            heating_state_getter = _build_heating_state_getter(tick_rows)
                    except Exception as e:
                        logger.debug(
                            f"heating-state gate unavailable for {cid}/{rid}: {e}"
                        )

                profile = compute_profile(
                    room_id=rid,
                    dimensions=dimensions,
                    insulation=insulation,
                    temperature_series=long_series,
                    outdoor_temp_getter=outdoor_getter,
                    heating_state_getter=heating_state_getter,
                )
                baseline_tau = profile.tau_seconds
                if baseline_tau is None:
                    continue  # no baseline yet, skip this room

                # Recent (last 3h) — used for both cool and heat detection
                recent = []
                try:
                    for attr in ("temperature", "local_temperature",
                                 "current_temperature", "internal_temperature"):
                        rows = self.telemetry_query(sensor_ieee, attr, 3) or []
                        if rows:
                            recent = rows
                            break
                except Exception as e:
                    logger.debug(f"recent-series fetch failed for {sensor_ieee}: {e}")
                    continue
                if len(recent) < 4:
                    continue

                # Check both detectors
                fast = detect_fast_cooling(
                    room_id=rid,
                    recent_temperature_series=recent,
                    outdoor_temp_c=outdoor,
                    baseline_tau_seconds=baseline_tau,
                )
                slow = detect_slow_heating(
                    room_id=rid,
                    recent_temperature_series=recent,
                    expected_tau_seconds=baseline_tau,
                )

                for anomaly in (fast, slow):
                    if anomaly is None:
                        continue
                    key = (cid, rid, anomaly.kind)
                    seen_keys.add(key)

                    # A door that has since been opened or shut changes the
                    # explanation of an anomaly still on screen, so the cause
                    # is re-read every scan.
                    cause = None
                    if anomaly.kind == "fast_cool":
                        cause = self._contact_cause(room, adv, anomaly)
                    message = (f"{anomaly.summary} {cause['text']}"
                               if cause and anomaly.summary else anomaly.message)

                    existing = active.get(key)
                    if existing and \
                            existing.get("window_end_ts") == anomaly.window_end_ts:
                        active[key] = {**existing, "message": message, "cause": cause}
                        continue  # already reported this exact window

                    # New or refreshed anomaly
                    record = {
                        "circuit_id": cid,
                        "circuit_name": circuit.get("name"),
                        "room_id": rid,
                        "room_name": room.get("name"),
                        **anomaly.to_dict(),
                        "message": message,
                        "cause": cause,
                    }
                    active[key] = record
                    new_count += 1
                    logger.warning(
                        f"Anomaly [{anomaly.severity}] {anomaly.kind} in "
                        f"{circuit.get('name')}/{room.get('name')}: "
                        f"{message}"
                    )

        # Resolve: anything active that we didn't re-detect this pass
        now = time.time()
        resolved = []
        for key in list(active.keys()):
            if key not in seen_keys:
                rec = active.pop(key)
                rec["resolved_at"] = now
                resolved.append(rec)
                history.insert(0, rec)

        # Trim history
        cutoff = now - HISTORY_KEEP_SEC
        history = [h for h in history
                   if h.get("resolved_at", now) >= cutoff][:50]

        self._active = active
        self._history = history
        self._last_scan_ts = now
        return new_count

    def _contact_cause(self, room: Dict[str, Any], adv: Any, anomaly: Any) -> Optional[Dict[str, Any]]:
        """Read the room's contact sensors and their recent history. Blocking
        (DuckDB) — called from the scan's worker thread only."""
        configured = [c for c in (room.get("contact_sensors") or [])
                      if isinstance(c, dict) and c.get("ieee") and c.get("enabled", True)]
        if not configured:
            return None
        try:
            devices = (adv._get_devices() or {}) if adv is not None else {}
        except Exception:
            devices = {}
        contacts = []
        for c in configured:
            ieee = c["ieee"]
            dev = devices.get(ieee)
            state = (dev.get("state") if isinstance(dev, dict)
                     else getattr(dev, "state", None)) or {}
            names = getattr(getattr(dev, "service", None), "friendly_names", None) or {}
            is_open = None
            if "is_open" in state:
                is_open = bool(state["is_open"])
            elif "contact" in state:
                is_open = not bool(state["contact"])
            entry = {"name": names.get(ieee) or c.get("name") or ieee, "is_open": is_open,
                     "open_for_sec": None, "open_in_window_sec": 0.0}
            try:
                if is_open and query_state_since_change_sec is not None:
                    entry["open_for_sec"] = query_state_since_change_sec(ieee, "is_open", 24)
                if anomaly.window_start_ts and anomaly.window_end_ts:
                    entry["open_in_window_sec"] = _open_seconds_in_window(
                        self.telemetry_query(ieee, "is_open", 6) or [],
                        anomaly.window_start_ts, anomaly.window_end_ts)
            except Exception as e:
                logger.debug(f"contact history unavailable for {ieee}: {e}")
            contacts.append(entry)
        return contact_cause(contacts)

    def get_snapshot(self) -> Dict[str, Any]:
        return {
            "last_scan_ts": self._last_scan_ts,
            "last_scan_age_seconds": (time.time() - self._last_scan_ts) if self._last_scan_ts else None,
            "active": list(self._active.values()),
            "recently_resolved": self._history[:10],
        }