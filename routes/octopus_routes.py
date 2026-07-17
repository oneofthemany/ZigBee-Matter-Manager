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
        Where the grid electricity goes: per-smart-plug kWh vs the metered
        total. Works with Octopus disabled — plug data is local — in which
        case grid_kwh/unmetered are null.
        """
        days, _ = _RANGES.get(range, _RANGES["week"])
        plug_rows = await _q(telemetry_db.query_plug_energy_by_day, days=days)

        names = {}
        if get_zigbee_service:
            try:
                zs = get_zigbee_service()
                devices = zs.get_all_devices_json() or {} if zs and hasattr(zs, "get_all_devices_json") else {}
                names = {ieee: _friendly_name(dev, ieee) for ieee, dev in devices.items()}
            except Exception as e:
                logger.debug(f"Device name lookup failed: {e}")

        per_device = {}
        for r in plug_rows:
            if r["kwh"] is None:
                continue
            per_device[r["ieee"]] = per_device.get(r["ieee"], 0.0) + float(r["kwh"])
        devices_out = sorted(
            ({"ieee": ieee, "name": names.get(ieee, ieee), "kwh": round(kwh, 3)}
             for ieee, kwh in per_device.items() if kwh > 0),
            key=lambda d: d["kwh"], reverse=True)

        grid = await _q(telemetry_db.query_octopus_consumption_buckets,
                        "electricity", days=days, group_by="day")
        grid_kwh = sum(r["kwh"] for r in grid if r["kwh"] is not None) if grid else None
        plugs_kwh = round(sum(d["kwh"] for d in devices_out), 3)
        return {
            "success": True,
            "range": range,
            "devices": devices_out,
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
