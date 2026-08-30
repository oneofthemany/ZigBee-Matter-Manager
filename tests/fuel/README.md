# Fuel tests

```
python3 tests/fuel/run_all.py          # everything, no network
python3 tests/fuel/live_check.py       # hits the real APIs
python3 tests/fuel/live_check.py FR IT # …or just these regions
```

No test framework: the project carries none, and adding one to run six files
would be a dependency for its own sake. Each module exposes `run()` and
`run_all.py` drives them.

## What is where

| File | Covers |
| --- | --- |
| `test_base.py` | the provider contract — TTL caching, radius filtering, the rate limiter, fallback chaining, the truncated-response guard |
| `test_service.py` | the response contract, region resolution, the `location:` block and its comment-preserving writes, the registry |
| `test_providers.py` | all eight region adapters, against captured payloads |
| `test_history.py` | the regional schema, its migration from the pre-region one, per-region scoping. Needs `duckdb`; skipped with a note without it |
| `js/test_formatters.js` | the price/distance formatters in `drive.js` |
| `js/test_settings.js` | the Settings region picker in `settings.js` |
| `js/test_drive_render.js` | the Drive tab's fuel render path |
| `check_android.py` | static checks on the Kotlin — see below |

The JavaScript tests slice the real functions out of the shipped `.js` files and
run them. That is the point: `node --check` parses but cannot see an undefined
identifier inside a function body, which is how a `ReferenceError` once reached
the browser. `test_settings.js` fails on exactly that bug if it is reintroduced.

## The Android checks are not a compile

There is no JVM in this project's dev setup, so `check_android.py` cannot build
the Kotlin. It catches what is findable without a compiler: unbalanced
delimiters, references to symbols that were renamed, `getString()` calls whose
argument count does not match the string resource, and hardcoded currency or
unit literals creeping back into the car screen.

Anything touching `android/` still needs a real build before it is trusted.

## Fixtures

`fixtures/` holds real responses captured from each live API, trimmed to a
handful of stations. Two exceptions, both stated in the files themselves:

- **Queensland** is built from the worked examples in the published spec
  (*Fuel Prices QLD Direct API (OUT) v1.6*), because the API needs a subscriber
  token. Its adapter has never been run against the live service.
- **The United States** envelope is the documented EIA API v2 shape, but every
  price in it is real, lifted from EIA's own public bulk file
  (`api.eia.gov/bulk/PET.zip`) for the week ending 2026-08-24. The adapter has
  never run against the keyed API. Its other moving part — resolving a
  coordinate to a state and then to an EIA area — *has* been checked live
  against Nominatim for nine states.
- **Germany's** prices are placeholders — the fixture was fetched with
  Tankerkönig's public demo key, which returns `1.009` for everything. The
  shape is real; the numbers are not.

To refresh a fixture, fetch the endpoint in `modules/fuel/providers/<region>.py`
and trim it to a few stations, keeping at least one with a missing price.

## The assertion that earns its keep

`implausible_prices()` asserts every price is plausible money per unit sold.
That one check catches a Spanish comma decimal read as an English one (1599.0),
an Australian cents value left unscaled (203.9), and a Queensland
tenth-of-a-cent value divided by the wrong factor — three bugs that would
otherwise reach a user looking like a plausible price.

Its bounds follow the **unit**, not the currency: a US gallon is about 3.8
litres, and American diesel was $5.65 a gallon when these were set, which a
per-litre ceiling would have flagged as a parsing bug. `PRICE_BOUNDS` in
`harness.py` holds both.

## Live checks

`live_check.py` is separate because it needs the network and `aiohttp`. It is
what proves an adapter against the real service rather than a fixture, and it
is what caught the French export answering one request with 930 stations
instead of 9,677 — an HTTP 200 that "stale beats empty" does not catch, and the
reason `BulkSnapshotProvider` rejects a snapshot that has lost half its
stations. Regions needing a key are skipped unless one is configured.
