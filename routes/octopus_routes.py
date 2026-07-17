"""
Octopus Energy API routes.

Serves the Energy tab (consumption/cost/rate charts, plug breakdown) and the
Settings → APIs → Energy pane (status, test-connection, backfill). Chart data
comes from the telemetry DuckDB via modules.telemetry_db, so it stays
available even when the Octopus API is unreachable; live tariff state comes
from the OctopusEnergyService in-memory cache.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request

from modules import telemetry_db

logger = logging.getLogger("routes.octopus")

# UI range → (window days, bucket granularity)
_RANGES = {
    "day": (2, "halfhour"),
    "week": (7, "day"),
    "month": (31, "day"),
}


def _fmt_ts(ts, group_by: str) -> str:
    """DuckDB timestamps → JSON: UTC ISO for half-hours, local date for buckets."""
    if not isinstance(ts, datetime):
        return str(ts)
    if group_by == "halfhour":
        return ts.isoformat() + "Z"
    return ts.strftime("%Y-%m-%d")


def _friendly_name(dev, ieee: str) -> str:
    if isinstance(dev, dict):
        return dev.get("friendly_name") or dev.get("name") or ieee
    return getattr(dev, "friendly_name", None) or getattr(dev, "name", None) or ieee


def _device_state(dev) -> dict:
    """Device state dict — tolerant of dict-of-dicts and dict-of-objects."""
    state = dev.get("state") if isinstance(dev, dict) else getattr(dev, "state", None)
    return state if isinstance(state, dict) else {}


def _live_power_w(dev) -> Optional[float]:
    """Instantaneous power (W) from a socket's reported attributes."""
    state = _device_state(dev)
    for key in ("power", "active_power"):
        try:
            v = state.get(key)
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


# Appliance-name heuristics for targeted saving tips
_SHIFTABLE_RE = ("wash", "dish", "dryer", "tumble", "dehumid")
_SCREEN_RE = ("tv", "telly", "television", "media", "entertainment")


def _socket_tips(sockets: list, rate_p: Optional[float],
                 cheap_window: Optional[dict], range_label: str) -> list:
    """
    Rule-based saving tips from real socket usage. Same {icon, title, detail}
    shape as the heating advisor's tips so the UI renders them alike.
    """
    tips = []
    by_kwh = [s for s in sockets if (s.get("kwh") or 0) > 0.05]
    by_kwh.sort(key=lambda s: s["kwh"], reverse=True)

    if by_kwh:
        top = by_kwh[0]
        cost = f" (~£{top['cost_gbp']:.2f})" if top.get("cost_gbp") is not None else ""
        tips.append({
            "icon": "trophy", "category": "usage",
            "title": f"{top['name']} is your biggest socket load",
            "detail": f"{top['kwh']:.1f} kWh{cost} over the last {range_label}. "
                      f"Worth checking its settings or schedule first — small "
                      f"changes here beat big changes anywhere else.",
        })

    # Load-shift advice only when the tariff actually varies (Agile/E7)
    if cheap_window:
        for s in by_kwh:
            if any(k in s["name"].lower() for k in _SHIFTABLE_RE):
                tips.append({
                    "icon": "clock", "category": "shift",
                    "title": f"Run {s['name']} off-peak",
                    "detail": f"Cheapest upcoming window is "
                              f"{cheap_window['off_peak_start']}–{cheap_window['off_peak_end']} "
                              f"(~{cheap_window['off_peak_rate_p']:.1f}p/kWh). Delay-start "
                              f"timers put its {s['kwh']:.1f} kWh into the cheap slots.",
                })
                if sum(1 for t in tips if t["category"] == "shift") >= 2:
                    break

    # Standby drains: sockets drawing a constant trickle right now
    for s in by_kwh:
        p = s.get("power_w")
        if p is not None and 2 <= p <= 25:
            yearly = None
            if rate_p is not None:
                yearly = p / 1000 * 24 * 365 * rate_p / 100
            tips.append({
                "icon": "moon", "category": "standby",
                "title": f"{s['name']} is drawing {p:.0f} W right now",
                "detail": "If that's standby, a schedule or smart-plug off state "
                          + (f"saves ~£{yearly:.0f}/year." if yearly is not None
                             else "eliminates the drain."),
            })
            if sum(1 for t in tips if t["category"] == "standby") >= 2:
                break

    for s in by_kwh:
        if any(k in s["name"].lower() for k in _SCREEN_RE):
            tips.append({
                "icon": "tv", "category": "usage",
                "title": f"Trim {s['name']}'s appetite",
                "detail": f"{s['kwh']:.1f} kWh over the last {range_label}. Dropping "
                          f"screen brightness or enabling eco mode typically cuts "
                          f"TV energy 20–30% with no visible difference in daylight.",
            })
            break

    return tips[:5]


def register_octopus_routes(app: FastAPI, get_octopus_service, get_zigbee_service=None):

    def _svc():
        try:
            return get_octopus_service()
        except Exception:
            return None

    # DuckDB queries run in worker threads: a slow scan on the event loop
    # would hang every request incl. /api/system/health (watchdog restart).
    async def _q(fn, *args, **kw):
        return await asyncio.to_thread(fn, *args, **kw)

    @app.get("/api/octopus/status")
    async def octopus_status():
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Octopus service unavailable"}
        return {"success": True, "status": svc.get_status()}

    @app.post("/api/octopus/test")
    async def octopus_test(request: Request):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Octopus service unavailable"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        result = await svc.test_connection(
            api_key=body.get("api_key"),
            account_number=body.get("account_number"),
        )
        return result

    @app.get("/api/octopus/summary")
    async def octopus_summary():
        """KPI payload: latest daily usage/cost per fuel + live rates."""
        svc = _svc()
        status = svc.get_status() if svc else {}
        fuels = {}
        for fuel in ("electricity", "gas"):
            days = await _q(telemetry_db.query_octopus_consumption_buckets,
                            fuel, days=3, group_by="day")
            latest = days[-1] if days else None
            previous = days[-2] if len(days) > 1 else None

            def _day(d):
                if not d:
                    return None
                return {
                    "date": _fmt_ts(d["ts"], "day"),
                    "kwh": round(d["kwh"], 3) if d["kwh"] is not None else None,
                    "cost_gbp": round(d["cost_p"] / 100, 2) if d["cost_p"] is not None else None,
                }

            current_rate = svc.current_unit_rate(fuel) if svc else None
            if current_rate is None:
                current_rate = await _q(telemetry_db.query_octopus_current_rate, fuel, "unit")
            standing = svc.current_standing_charge(fuel) if svc else None
            if standing is None:
                standing = await _q(telemetry_db.query_octopus_current_rate, fuel, "standing")
            fuels[fuel] = {
                "latest_day": _day(latest),
                "previous_day": _day(previous),
                "current_unit_rate_p": current_rate,
                "standing_charge_p": standing,
                "latest_data": (status.get("latest_data") or {}).get(fuel),
            }
        return {
            "success": True,
            "enabled": bool(status.get("enabled")),
            "fuels": fuels,
            "tomorrow_agile_published": bool(status.get("tomorrow_agile_published")),
            "errors": status.get("errors") or {},
        }

    @app.get("/api/octopus/consumption")
    async def octopus_consumption(fuel: str = "electricity", range: str = "week",
                                  date_from: Optional[str] = None,
                                  date_to: Optional[str] = None):
        """
        range=day|week|month for rolling windows, or an explicit calendar
        window via date_from/date_to (YYYY-MM-DD, inclusive, local days) —
        a single day comes back half-hourly, anything longer daily.
        """
        if fuel not in ("electricity", "gas"):
            return {"success": False, "error": f"Unknown fuel '{fuel}'"}
        if date_from and date_to:
            try:
                d0 = datetime.strptime(date_from, "%Y-%m-%d")
                d1 = datetime.strptime(date_to, "%Y-%m-%d")
            except ValueError:
                return {"success": False, "error": "Dates must be YYYY-MM-DD"}
            if d1 < d0:
                d0, d1 = d1, d0
                date_from, date_to = date_to, date_from
            span_days = (d1 - d0).days + 1
            group_by = "halfhour" if span_days == 1 else ("day" if span_days <= 92 else "week")
            rows = await _q(telemetry_db.query_octopus_consumption_buckets,
                            fuel, group_by=group_by,
                            day_from=date_from, day_to=date_to)
            range = "custom"
        else:
            days, group_by = _RANGES.get(range, _RANGES["week"])
            rows = await _q(telemetry_db.query_octopus_consumption_buckets,
                            fuel, days=days, group_by=group_by)
        return {
            "success": True,
            "fuel": fuel,
            "range": range,
            "date_from": date_from,
            "date_to": date_to,
            "group_by": group_by,
            "series": [{
                "ts": _fmt_ts(r["ts"], group_by),
                "kwh": round(r["kwh"], 3) if r["kwh"] is not None else None,
                "cost_gbp": round(r["cost_p"] / 100, 4) if r["cost_p"] is not None else None,
            } for r in rows],
        }

    @app.get("/api/octopus/rates")
    async def octopus_rates(fuel: str = "electricity"):
        """Unit rates covering local today + tomorrow, for the rate curve."""
        svc = _svc()
        rates = svc.rates_today(fuel) if svc else []
        if rates:
            series = [{
                "from": r["from"].isoformat() + "Z",
                "to": r["to"].isoformat() + "Z" if r["to"] else None,
                "p_per_kwh": r["p"],
            } for r in rates]
        else:
            # Cache empty (e.g. just restarted) — serve from DuckDB
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            start = now - timedelta(hours=now.hour + 1)
            stored = await _q(telemetry_db.query_octopus_rates_window,
                              fuel, start, now + timedelta(days=2))
            series = [{
                "from": r["valid_from"].isoformat() + "Z",
                "to": r["valid_to"].isoformat() + "Z" if r["valid_to"] else None,
                "p_per_kwh": r["value_inc_vat_p"],
            } for r in stored]
        standing = svc.current_standing_charge(fuel) if svc else None
        if standing is None:
            standing = await _q(telemetry_db.query_octopus_current_rate, fuel, "standing")
        now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
        return {
            "success": True,
            "fuel": fuel,
            "unit_rates": series,
            "standing_charge_p": standing,
            "now": now_iso,
        }

    @app.get("/api/octopus/breakdown")
    async def octopus_breakdown(range: str = "week"):
        """
        Where the grid electricity goes: per-socket kWh (+ live watts, cost,
        a stacked per-day series and saving tips) vs the metered total.
        Works with Octopus disabled — plug data is local — in which case
        grid/rest-of-home and £ figures are null.
        """
        days, _ = _RANGES.get(range, _RANGES["week"])
        plug_rows = await _q(telemetry_db.query_plug_energy_by_day, days=days)

        names, live_power = {}, {}
        if get_zigbee_service:
            try:
                zs = get_zigbee_service()
                devices = zs.get_all_devices_json() or {} if zs and hasattr(zs, "get_all_devices_json") else {}
                for ieee, dev in devices.items():
                    names[str(ieee)] = _friendly_name(dev, str(ieee))
                    p = _live_power_w(dev)
                    if p is not None:
                        live_power[str(ieee)] = p
            except Exception as e:
                logger.debug(f"Device lookup failed: {e}")

        svc = _svc()
        rate_p = svc.current_unit_rate("electricity") if svc else None
        if rate_p is None:
            rate_p = await _q(telemetry_db.query_octopus_current_rate,
                              "electricity", "unit")

        # Per-socket totals + per-day matrix for the stacked view
        per_device: dict = {}
        per_day: dict = {}          # ieee → {day: kwh}
        day_set = set()
        for r in plug_rows:
            if r["kwh"] is None:
                continue
            ieee = r["ieee"]
            day = r["day"].strftime("%Y-%m-%d") if hasattr(r["day"], "strftime") else str(r["day"])[:10]
            per_device[ieee] = per_device.get(ieee, 0.0) + float(r["kwh"])
            per_day.setdefault(ieee, {})[day] = per_day.get(ieee, {}).get(day, 0.0) + float(r["kwh"])
            day_set.add(day)

        def _cost(kwh):
            return round(kwh * rate_p / 100, 2) if rate_p is not None else None

        sockets = sorted(
            ({"ieee": ieee, "name": names.get(ieee, ieee),
              "kwh": round(kwh, 3), "cost_gbp": _cost(kwh),
              "power_w": live_power.get(ieee)}
             for ieee, kwh in per_device.items() if kwh > 0),
            key=lambda d: d["kwh"], reverse=True)
        # Energy-reporting sockets that show live power but no stored usage yet
        for ieee, p in live_power.items():
            if ieee not in per_device:
                sockets.append({"ieee": ieee, "name": names.get(ieee, ieee),
                                "kwh": 0.0, "cost_gbp": _cost(0.0) if rate_p is not None else None,
                                "power_w": p})

        grid = await _q(telemetry_db.query_octopus_consumption_buckets,
                        "electricity", days=days, group_by="day")
        grid_by_day = {_fmt_ts(r["ts"], "day"): r["kwh"] for r in grid
                       if r["kwh"] is not None}
        day_set |= set(grid_by_day)
        days_sorted = sorted(day_set)

        # Stacked series: top sockets individually, the rest folded, plus
        # "rest of home" so the stack totals the metered grid figure.
        MAX_STACK = 6
        top = sockets[:MAX_STACK]
        other_ieee = {s["ieee"] for s in sockets[MAX_STACK:]}
        stacks = [{
            "name": s["name"], "ieee": s["ieee"],
            "kwh": [round(per_day.get(s["ieee"], {}).get(d, 0.0), 3) for d in days_sorted],
        } for s in top if s["kwh"] > 0]
        if other_ieee:
            stacks.append({"name": f"Other sockets ({len(other_ieee)})", "ieee": None,
                           "kwh": [round(sum(per_day.get(i, {}).get(d, 0.0)
                                             for i in other_ieee), 3)
                                   for d in days_sorted]})
        rest = []
        for d in days_sorted:
            g = grid_by_day.get(d)
            plugs_day = sum(per_day.get(i, {}).get(d, 0.0) for i in per_device)
            rest.append(round(max(0.0, g - plugs_day), 3) if g is not None else None)

        grid_kwh = sum(v for v in grid_by_day.values()) if grid_by_day else None
        plugs_kwh = round(sum(d["kwh"] for d in sockets), 3)

        cheap_window = None
        if svc:
            t = svc.heating_tariff("electricity")
            if t and t.get("off_peak_start"):
                cheap_window = t
        tips = _socket_tips(sockets, rate_p, cheap_window,
                            {"day": "day", "week": "week", "month": "month"}.get(range, "week"))

        return {
            "success": True,
            "range": range,
            "rate_p": rate_p,
            "sockets": sockets,
            "series": {"days": days_sorted, "stacks": stacks, "rest": rest},
            "tips": tips,
            "plugs_kwh": plugs_kwh,
            "grid_kwh": round(grid_kwh, 3) if grid_kwh is not None else None,
            "unmetered_kwh": round(max(0.0, grid_kwh - plugs_kwh), 3) if grid_kwh is not None else None,
        }

    @app.get("/api/octopus/telemetry")
    async def octopus_telemetry():
        """Home Mini near-real-time demand samples (in-memory, ~5-min grain)."""
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Octopus service unavailable"}
        return {"success": True, **svc.get_live_telemetry()}

    @app.post("/api/octopus/backfill")
    async def octopus_backfill(request: Request):
        svc = _svc()
        if not svc:
            return {"success": False, "error": "Octopus service unavailable"}
        if not svc.enabled:
            return {"success": False, "error": "Octopus integration is not enabled"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            days = int(body.get("days") or svc.backfill_days)
        except (TypeError, ValueError):
            return {"success": False, "error": "days must be a number"}
        asyncio.create_task(svc.backfill(days))
        return {"success": True, "message": f"Backfill of {min(730, max(1, days))} days started"}

    logger.info("Octopus Energy routes registered")
