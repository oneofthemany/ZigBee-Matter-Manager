"""
Octopus Energy integration service.

Polls the Octopus Energy REST API (https://api.octopus.energy/v1/) for
smart-meter consumption (electricity + gas, half-hourly) and tariff rates
(including half-hourly Agile pricing), persisting both to the telemetry
DuckDB so the Energy tab can chart them and the heating advisor can price
real usage.

Auth: HTTP Basic — the account API key as username, blank password.
Consumption endpoints need auth; product/tariff rate endpoints are public.

Config (config.yaml):
  octopus:
    enabled: true
    api_key: sk_live_...
    account_number: A-XXXXXXXX
    gas_calorific_value: 39.5   # MJ/m³, from your gas bill
    gas_unit: auto              # auto|kwh|m3 (SMETS1 reports kWh, SMETS2 m³)
    consumption_poll_minutes: 30
    rates_poll_minutes: 60
    backfill_days: 90

Smart-meter consumption lags by several hours to a day — "today" is usually
partial. Rates never break heating: heating_tariff() returns None on any
doubt and the advisor falls back to the manual tariff config.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from modules import telemetry_db

logger = logging.getLogger("modules.octopus")

API_BASE = "https://api.octopus.energy/v1"
LONDON = ZoneInfo("Europe/London")

# m³ → kWh: volume correction × calorific value (MJ/m³) / 3.6 MJ per kWh
GAS_VOLUME_CORRECTION = 1.02264
DEFAULT_CALORIFIC_VALUE = 39.5

FUELS = ("electricity", "gas")

# Agile publishes tomorrow's rates around 16:00 UK; retry window + cadence
AGILE_RETRY_FROM_HOUR = 16
AGILE_RETRY_SEC = 15 * 60


def _iso_to_utc_naive(s: str) -> datetime:
    """Parse an API ISO8601 timestamp (offset or Z) to UTC-naive."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso_z(dt: datetime) -> str:
    """UTC-naive datetime → API query format."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def product_code_from_tariff(tariff_code: str) -> Optional[str]:
    """
    E-1R-AGILE-24-10-01-C → AGILE-24-10-01
    (strip <fuel>-<n>R- prefix and -<region letter> suffix)
    """
    parts = (tariff_code or "").split("-")
    if len(parts) < 4:
        return None
    return "-".join(parts[2:-1]) or None


class OctopusEnergyService:
    """Periodic Octopus Energy fetcher + in-memory rate cache."""

    def __init__(self, config: dict):
        config = config or {}
        self.enabled = bool(config.get("enabled", False))
        self.api_key = str(config.get("api_key") or "")
        self.account_number = str(config.get("account_number") or "").strip()
        try:
            self.calorific_value = float(config.get("gas_calorific_value") or DEFAULT_CALORIFIC_VALUE)
        except (TypeError, ValueError):
            self.calorific_value = DEFAULT_CALORIFIC_VALUE
        self.gas_unit = str(config.get("gas_unit") or "auto").strip().lower()
        if self.gas_unit not in ("auto", "kwh", "m3"):
            self.gas_unit = "auto"
        self.consumption_poll = max(5, int(config.get("consumption_poll_minutes") or 30)) * 60
        self.rates_poll = max(15, int(config.get("rates_poll_minutes") or 60)) * 60
        self.backfill_days = min(730, max(1, int(config.get("backfill_days") or 90)))

        # Discovered at startup from /accounts/ — never cached to YAML so a
        # meter swap is picked up on the next restart.
        self._meters: Dict[str, Dict[str, Any]] = {}
        # {fuel: {unit_rates:[{from,to,p}], standing_charge_p, tariff_code,
        #         is_agile, fetched_at}} — last-good, survives API outages
        self._rates_cache: Dict[str, Dict[str, Any]] = {}
        self._status: Dict[str, Any] = {
            "errors": {},          # area → last error string
            "last_poll": {},       # area → epoch seconds
            "latest_data": {},     # fuel → newest interval_end iso
        }
        self._task: Optional[asyncio.Task] = None
        self._client = None
        self._last_agile_retry = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        if not self.enabled:
            logger.info("Octopus Energy service disabled")
            return
        if not self.api_key or not self.account_number:
            logger.warning("Octopus Energy: api_key/account_number not configured — disabled")
            self._status["errors"]["account"] = "API key or account number missing"
            return
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"Octopus Energy service started (account={self.account_number}, "
            f"consumption every {self.consumption_poll // 60}min, "
            f"rates every {self.rates_poll // 60}min)"
        )

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        if self._client is not None:
            client, self._client = self._client, None
            try:
                asyncio.get_event_loop().create_task(client.aclose())
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public accessors (sync, never raise) — heating seam + routes
    # ------------------------------------------------------------------

    def heating_tariff(self, fuel: str = "gas") -> Optional[Dict[str, Any]]:
        """
        Live tariff for the heating advisor, or None whenever Octopus can't
        confidently supply one (disabled, no data yet, API down since boot).
        None means "use the manual tariff" — this must never raise.
        """
        try:
            if not self.enabled:
                return None
            cache = self._rates_cache.get(fuel)
            if not cache:
                return None
            rate = self.current_unit_rate(fuel)
            if rate is None:
                return None
            out = {
                "source": "octopus",
                "type": "agile" if cache.get("is_agile") else "fixed",
                "fuel": fuel,
                "unit_rate_p": rate,
                "standing_charge_p": cache.get("standing_charge_p"),
                "tariff_code": cache.get("tariff_code"),
                "is_agile": bool(cache.get("is_agile")),
                "fetched_at": cache.get("fetched_at"),
            }
            if cache.get("is_agile"):
                window = self._cheapest_window(fuel)
                if window:
                    out.update(window)
            return out
        except Exception as e:  # pragma: no cover — belt and braces
            logger.debug(f"heating_tariff failed: {e}")
            return None

    def current_unit_rate(self, fuel: str = "electricity") -> Optional[float]:
        """Unit rate (p/kWh inc VAT) in force right now, from the cache."""
        cache = self._rates_cache.get(fuel)
        if not cache:
            return None
        now = _utc_now_naive()
        for r in cache.get("unit_rates", []):
            if r["from"] <= now and (r["to"] is None or now < r["to"]):
                return r["p"]
        return None

    def current_standing_charge(self, fuel: str = "electricity") -> Optional[float]:
        cache = self._rates_cache.get(fuel)
        return cache.get("standing_charge_p") if cache else None

    def rates_today(self, fuel: str = "electricity") -> List[Dict[str, Any]]:
        """Cached unit rates covering local today + tomorrow, oldest first."""
        cache = self._rates_cache.get(fuel)
        if not cache:
            return []
        start_local = datetime.now(LONDON).replace(hour=0, minute=0, second=0, microsecond=0)
        start = start_local.astimezone(timezone.utc).replace(tzinfo=None)
        end = start + timedelta(days=2)
        return [r for r in cache.get("unit_rates", [])
                if r["from"] < end and (r["to"] is None or r["to"] > start)]

    def get_status(self) -> Dict[str, Any]:
        gas_meter = self._meters.get("gas") or {}
        return {
            "enabled": self.enabled,
            "configured": bool(self.api_key and self.account_number),
            "account_number": self.account_number,
            "running": self._task is not None and not self._task.done(),
            "meters": {
                fuel: {k: v for k, v in m.items() if k != "serials"}
                for fuel, m in self._meters.items()
            },
            "gas_unit": self.gas_unit,
            "gas_unit_effective": gas_meter.get("unit_effective"),
            "gas_calorific_value": self.calorific_value,
            "tariffs": {
                fuel: {
                    "tariff_code": c.get("tariff_code"),
                    "is_agile": c.get("is_agile"),
                    "current_unit_rate_p": self.current_unit_rate(fuel),
                    "standing_charge_p": c.get("standing_charge_p"),
                    "fetched_at": c.get("fetched_at"),
                }
                for fuel, c in self._rates_cache.items()
            },
            "tomorrow_agile_published": self._tomorrow_rates_present("electricity"),
            "last_poll": dict(self._status["last_poll"]),
            "errors": {k: v for k, v in self._status["errors"].items() if v},
            "latest_data": dict(self._status["latest_data"]),
        }

    # ------------------------------------------------------------------
    # One-shot operations (routes)
    # ------------------------------------------------------------------

    async def test_connection(self, api_key: Optional[str] = None,
                              account_number: Optional[str] = None) -> Dict[str, Any]:
        """Validate credentials and return discovered meters/tariffs."""
        key = (api_key or "").strip() or self.api_key
        acct = (account_number or "").strip() or self.account_number
        if not key or not acct:
            return {"success": False, "error": "API key and account number are required"}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{API_BASE}/accounts/{acct}/", auth=(key, "")
                )
            if resp.status_code in (401, 403):
                return {"success": False, "error": "Authentication failed — check the API key"}
            if resp.status_code == 404:
                return {"success": False, "error": f"Account {acct} not found"}
            resp.raise_for_status()
            meters = self._parse_account(resp.json())
            if not meters:
                return {"success": False, "error": "Account has no smart meter points"}
            return {
                "success": True,
                "account_number": acct,
                "meters": {
                    fuel: {k: v for k, v in m.items() if k != "serials"}
                    for fuel, m in meters.items()
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def backfill(self, days: int):
        """Re-fetch a consumption + rates window (fire-and-forget from routes)."""
        days = min(730, max(1, int(days)))
        since = _utc_now_naive() - timedelta(days=days)
        try:
            await self._ensure_account()
            for fuel in FUELS:
                if fuel in self._meters:
                    await self._fetch_rates(fuel, period_from=since)
                    await self._fetch_consumption(fuel, period_from=since)
            logger.info(f"Octopus backfill complete ({days} days)")
        except Exception as e:
            logger.error(f"Octopus backfill failed: {e}")
            self._status["errors"]["backfill"] = str(e)

    # ------------------------------------------------------------------
    # Internal — polling
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(
                timeout=30, auth=(self.api_key, ""),
                headers={"User-Agent": "ZigBee-Matter-Manager"},
            )
        return self._client

    async def _poll_loop(self):
        # Account discovery first — nothing works without meter points.
        while True:
            try:
                await self._ensure_account()
                self._status["errors"]["account"] = None
                break
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"Octopus account discovery failed: {e}")
                self._status["errors"]["account"] = str(e)
                await asyncio.sleep(300)

        last_rates = 0.0
        last_cons = 0.0
        while True:
            now = time.time()
            if now - last_rates >= self.rates_poll or self._agile_retry_due():
                try:
                    for fuel in self._meters:
                        await self._fetch_rates(fuel)
                    self._status["errors"]["rates"] = None
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.error(f"Octopus rates fetch failed: {e}")
                    self._status["errors"]["rates"] = str(e)
                last_rates = now
                self._status["last_poll"]["rates"] = now
            if now - last_cons >= self.consumption_poll:
                try:
                    for fuel in self._meters:
                        await self._fetch_consumption(fuel)
                    self._status["errors"]["consumption"] = None
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.error(f"Octopus consumption fetch failed: {e}")
                    self._status["errors"]["consumption"] = str(e)
                last_cons = now
                self._status["last_poll"]["consumption"] = now
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return

    def _agile_retry_due(self) -> bool:
        """After 16:00 UK, retry every 15 min until tomorrow's Agile rates land."""
        cache = self._rates_cache.get("electricity")
        if not cache or not cache.get("is_agile"):
            return False
        if self._tomorrow_rates_present("electricity"):
            return False
        if datetime.now(LONDON).hour < AGILE_RETRY_FROM_HOUR:
            return False
        if time.time() - self._last_agile_retry < AGILE_RETRY_SEC:
            return False
        self._last_agile_retry = time.time()
        return True

    def _tomorrow_rates_present(self, fuel: str) -> bool:
        cache = self._rates_cache.get(fuel)
        if not cache:
            return False
        tomorrow_local = (datetime.now(LONDON) + timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0)
        probe = tomorrow_local.astimezone(timezone.utc).replace(tzinfo=None)
        return any(r["from"] <= probe and (r["to"] is None or r["to"] > probe)
                   for r in cache.get("unit_rates", []))

    # ------------------------------------------------------------------
    # Internal — API calls
    # ------------------------------------------------------------------

    async def _get_json(self, url: str, params: Optional[dict] = None) -> dict:
        resp = await self._get_client().get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _get_paginated(self, url: str, params: dict,
                             max_pages: int = 40) -> List[dict]:
        """Follow `next` links, newest/oldest order preserved as returned."""
        results: List[dict] = []
        next_url: Optional[str] = url
        next_params: Optional[dict] = params
        for _ in range(max_pages):
            data = await self._get_json(next_url, next_params)
            results.extend(data.get("results") or [])
            next_url = data.get("next")
            next_params = None  # the `next` URL already carries the query
            if not next_url:
                break
        return results

    async def _ensure_account(self):
        if self._meters:
            return
        data = await self._get_json(f"{API_BASE}/accounts/{self.account_number}/")
        self._meters = self._parse_account(data)
        if not self._meters:
            raise RuntimeError("Octopus account has no smart meter points")
        logger.info(
            "Octopus meters discovered: "
            + ", ".join(f"{fuel}={m.get('tariff_code')}" for fuel, m in self._meters.items())
        )

    def _parse_account(self, data: dict) -> Dict[str, Dict[str, Any]]:
        """Extract active meter points + current agreements per fuel."""
        meters: Dict[str, Dict[str, Any]] = {}
        now = datetime.now(timezone.utc)
        for prop in data.get("properties") or []:
            for point in prop.get("electricity_meter_points") or []:
                m = self._parse_meter_point(point, now, id_key="mpan")
                if m and "electricity" not in meters:
                    meters["electricity"] = m
            for point in prop.get("gas_meter_points") or []:
                m = self._parse_meter_point(point, now, id_key="mprn")
                if m and "gas" not in meters:
                    meters["gas"] = m
        return meters

    @staticmethod
    def _parse_meter_point(point: dict, now, id_key: str) -> Optional[Dict[str, Any]]:
        point_id = point.get(id_key) or point.get("mpan") or point.get("mprn")
        serials = [m.get("serial_number") for m in point.get("meters") or []
                   if m.get("serial_number")]
        if not point_id or not serials:
            return None
        # Export MPANs (solar sellback) have is_export=true — skip them,
        # this integration tracks import consumption/cost.
        if point.get("is_export"):
            return None
        tariff_code = None
        for ag in point.get("agreements") or []:
            valid_to = ag.get("valid_to")
            if valid_to is None:
                tariff_code = ag.get("tariff_code")
                break
            try:
                if datetime.fromisoformat(str(valid_to).replace("Z", "+00:00")) > now:
                    tariff_code = ag.get("tariff_code")
            except ValueError:
                continue
        return {
            id_key: point_id,
            # Newest meter first: a meter swap leaves dead serials behind
            "serial": serials[-1],
            "serials": list(reversed(serials)),
            "tariff_code": tariff_code,
            "product_code": product_code_from_tariff(tariff_code) if tariff_code else None,
            "is_agile": bool(tariff_code and "AGILE" in tariff_code.upper()),
        }

    async def _fetch_rates(self, fuel: str, period_from: Optional[datetime] = None):
        meter = self._meters.get(fuel)
        if not meter or not meter.get("tariff_code") or not meter.get("product_code"):
            return
        tariff_code = meter["tariff_code"]
        product = meter["product_code"]
        kind = "electricity-tariffs" if fuel == "electricity" else "gas-tariffs"
        base = f"{API_BASE}/products/{product}/{kind}/{tariff_code}"

        if period_from is None:
            # First run (no stored rates): cover the consumption backfill so
            # historical cost joins work; afterwards just refresh recent+future.
            have_rates = telemetry_db.query_octopus_rates_window(
                fuel, _utc_now_naive() - timedelta(days=2), _utc_now_naive())
            period_from = (_utc_now_naive() - timedelta(days=self.backfill_days)
                           if not have_rates else _utc_now_naive() - timedelta(days=2))
        params = {
            "period_from": _iso_z(period_from),
            "period_to": _iso_z(_utc_now_naive() + timedelta(days=2)),
            "page_size": 1500,
        }

        unit_raw = await self._get_paginated(f"{base}/standard-unit-rates/", params)
        standing_raw = await self._get_paginated(f"{base}/standing-charges/", dict(params))

        def _norm(rows):
            out = []
            for r in rows:
                try:
                    out.append({
                        "valid_from": _iso_to_utc_naive(r["valid_from"]),
                        "valid_to": _iso_to_utc_naive(r["valid_to"]) if r.get("valid_to") else None,
                        "value_inc_vat_p": float(r.get("value_inc_vat")),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
            out.sort(key=lambda x: x["valid_from"])
            return out

        unit_rates = _norm(unit_raw)
        standing = _norm(standing_raw)
        telemetry_db.write_octopus_rates(fuel, "unit", tariff_code, unit_rates)
        telemetry_db.write_octopus_rates(fuel, "standing", tariff_code, standing)

        now = _utc_now_naive()
        standing_now = next(
            (r["value_inc_vat_p"] for r in reversed(standing)
             if r["valid_from"] <= now and (r["valid_to"] is None or r["valid_to"] > now)),
            None)
        self._rates_cache[fuel] = {
            "unit_rates": [{"from": r["valid_from"], "to": r["valid_to"],
                            "p": r["value_inc_vat_p"]} for r in unit_rates],
            "standing_charge_p": standing_now,
            "tariff_code": tariff_code,
            "is_agile": meter["is_agile"],
            "fetched_at": time.time(),
        }
        logger.info(f"Octopus rates updated: {fuel} {tariff_code} "
                    f"({len(unit_rates)} unit rates)")

    async def _fetch_consumption(self, fuel: str, period_from: Optional[datetime] = None):
        meter = self._meters.get(fuel)
        if not meter:
            return
        if period_from is None:
            last = telemetry_db.query_octopus_last_interval(fuel)
            period_from = last or (_utc_now_naive() - timedelta(days=self.backfill_days))

        point_id = meter.get("mpan") or meter.get("mprn")
        kind = "electricity-meter-points" if fuel == "electricity" else "gas-meter-points"
        params = {
            "period_from": _iso_z(period_from),
            "order_by": "period",
            "page_size": 25000,
        }

        raw: List[dict] = []
        # A meter swap leaves dead serials on the account — remember the one
        # that actually returns readings.
        for serial in [meter["serial"]] + [s for s in meter["serials"] if s != meter["serial"]]:
            url = f"{API_BASE}/{kind}/{point_id}/meters/{serial}/consumption/"
            raw = await self._get_paginated(url, params)
            if raw:
                meter["serial"] = serial
                break

        rows = []
        for r in raw:
            try:
                value = float(r["consumption"])
                rows.append({
                    "interval_start": _iso_to_utc_naive(r["interval_start"]),
                    "interval_end": _iso_to_utc_naive(r["interval_end"]),
                    "consumption": value,
                    "consumption_kwh": self._to_kwh(fuel, value, meter),
                })
            except (KeyError, TypeError, ValueError):
                continue
        if rows:
            telemetry_db.write_octopus_consumption(fuel, rows)
            newest = max(r["interval_end"] for r in rows)
            self._status["latest_data"][fuel] = newest.isoformat() + "Z"
            logger.info(f"Octopus consumption updated: {fuel} +{len(rows)} intervals "
                        f"(latest {newest.isoformat()}Z)")

    def _to_kwh(self, fuel: str, value: float, meter: dict) -> float:
        if fuel != "gas":
            return value
        unit = self.gas_unit
        if unit == "auto":
            # SMETS2 (the common case) reports m³; SMETS1 reports kWh.
            # Assumption surfaced via get_status() so a ~11× error is obvious.
            unit = "m3"
        meter["unit_effective"] = unit
        if unit == "kwh":
            return value
        return value * GAS_VOLUME_CORRECTION * self.calorific_value / 3.6

    # ------------------------------------------------------------------
    # Internal — agile helpers
    # ------------------------------------------------------------------

    def _cheapest_window(self, fuel: str, slots: int = 6) -> Optional[Dict[str, Any]]:
        """
        Cheapest contiguous `slots`×30min window in the next 24h of cached
        rates → off-peak fields the heating advisor's tips understand.
        """
        cache = self._rates_cache.get(fuel)
        if not cache:
            return None
        now = _utc_now_naive()
        upcoming = [r for r in cache.get("unit_rates", [])
                    if r["to"] is not None and r["to"] > now
                    and r["from"] < now + timedelta(hours=24)]
        upcoming.sort(key=lambda r: r["from"])
        if len(upcoming) < slots:
            return None
        best_i, best_avg = None, None
        for i in range(len(upcoming) - slots + 1):
            window = upcoming[i:i + slots]
            # Must be contiguous half-hours
            if any(window[j]["to"] != window[j + 1]["from"] for j in range(slots - 1)):
                continue
            avg = sum(r["p"] for r in window) / slots
            if best_avg is None or avg < best_avg:
                best_i, best_avg = i, avg
        if best_i is None:
            return None

        def _local_hhmm(dt: datetime) -> str:
            return dt.replace(tzinfo=timezone.utc).astimezone(LONDON).strftime("%H:%M")

        return {
            "off_peak_start": _local_hhmm(upcoming[best_i]["from"]),
            "off_peak_end": _local_hhmm(upcoming[best_i + slots - 1]["to"]),
            "off_peak_rate_p": round(best_avg, 2),
        }
