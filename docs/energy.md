# Energy — Octopus integration

`modules/octopus.py` polls the Octopus Energy REST API
(`https://api.octopus.energy/v1/`) for smart-meter consumption (electricity and
gas, half-hourly) and tariff rates including half-hourly Agile pricing,
persisting both so the Energy tab can chart them and the heating advisor can
price real usage.

Auth is HTTP Basic — the account API key as username, blank password.
Consumption endpoints need auth; product and tariff rate endpoints are public.

## Config

```yaml
octopus:
  enabled: true
  api_key: sk_live_...
  account_number: A-XXXXXXXX
  gas_calorific_value: 39.5   # MJ/m³, from your gas bill
  gas_unit: auto              # auto|kwh|m3 (SMETS1 reports kWh, SMETS2 m³)
  consumption_poll_minutes: 30
  rates_poll_minutes: 60
  backfill_days: 90
  home_mini: true             # near-real-time demand via the GraphQL API
  telemetry_poll_minutes: 5   # Home Mini sampling cadence (5–10 min typical)
  retention_days: 400         # local history kept in data/octopus.duckdb
```

Gas is converted m³ → kWh by volume correction × calorific value (MJ/m³) ÷ 3.6
MJ per kWh.

## Home Mini

With `home_mini` enabled the telemetry poll also persists each half-hour's
`consumptionDelta` as provisional `source='mini'` consumption rows, so the
Energy chart shows today in near real time instead of trailing the REST data
lag.

The REST poll stays the settlement-grade authority — it overwrites provisional
rows on the same interval key — but relaxes to a 3-hourly reconcile cadence for
electricity while Mini samples flow. Gas always keeps
`consumption_poll_minutes`; the Mini feed is electricity-only.

The last returned telemetry point covers the in-progress half hour: `demand` is
the meter's instantaneous demand (W) and `consumption` the running cumulative
total (Wh), so frequent polling yields a fine-grained demand series even though
the grouping is half-hourly.

The lookback doubles as gap-healing: 3 h on a steady-state poll to re-cover
missed cycles, 48 h on the first poll after a restart so downtime holes in
"today" fill immediately rather than waiting out the REST data lag.

## Caveats

Smart-meter consumption lags by several hours to a day, so "today" is usually
partial.

Rates never break heating: `heating_tariff()` returns `None` on any doubt and
the advisor falls back to the manual tariff config.

Agile publishes tomorrow's rates around 16:00 UK time, which is why the module
pins `Europe/London` for day boundaries. On hosts with no tz data it falls back
to UTC rather than crashing at import — UK-local labelling shifts by an hour in
summer, but everything keeps working.

Octopus data lives in its own `data/octopus.duckdb`; see
[telemetry database](telemetry_database.md#why-three-database-files) for why.

## Frontend

`static/js/energy.js` renders into `#energyDashboard`, called from `main.js`,
auto-refreshing every 5 minutes while the tab is visible. It works with Octopus
disabled, showing the local smart-plug breakdown plus a pointer to
Settings → APIs → Energy.
