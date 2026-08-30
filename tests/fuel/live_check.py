#!/usr/bin/env python3
"""
Hit every region's real API and sanity-check what comes back.

    python3 tests/fuel/live_check.py [REGION ...]

Deliberately not part of run_all.py: it needs the network, the upstreams, and
aiohttp. It is what proves an adapter against the live service rather than
against a fixture, and what caught the French export returning 930 stations
instead of 9,677.

Regions needing a key are skipped unless one is configured, except Germany,
which falls back to Tankerkoenig's published demo key so the request and parse
are still exercised (its prices are placeholders).
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.fuel import registry                              # noqa: E402

#: region -> a coordinate inside it.
PLACES = {
    "GB": ("London", 51.5074, -0.1278),
    "DE": ("Berlin", 52.5200, 13.4050),
    "ES": ("Madrid", 40.4168, -3.7038),
    "FR": ("Paris", 48.8566, 2.3522),
    "IT": ("Rome", 41.9028, 12.4964),
    "AU-NSW": ("Sydney", -33.8688, 151.2093),
    "AU-QLD": ("Brisbane", -27.4698, 153.0251),
    "AU-WA": ("Perth", -31.9523, 115.8613),
    "US": ("Denver", 39.7392, -104.9903),
}

TANKERKOENIG_DEMO = "00000000-0000-0000-0000-000000000002"


def _config(region: str) -> dict:
    if region == "DE":
        return {"fuel": {"tankerkoenig": {"api_key": TANKERKOENIG_DEMO}}}
    return {"fuel": {}}


async def check_region(region: str) -> list[str]:
    place, lat, lon = PLACES[region]
    provider = registry.build_provider(region, _config(region))
    print(f"\n{region} — {place} ({provider.source})")

    if not provider.configured:
        print("    SKIP — needs credentials that are not configured")
        return []

    failures = []

    def check(label, ok, detail="") -> bool:
        print(("    ok   " if ok else "    FAIL ") + label +
              ("" if ok else f"  <- {detail!r}"))
        if not ok:
            failures.append(f"{region}: {label}")
        return bool(ok)

    started = time.monotonic()
    try:
        found = await provider.nearby(lat, lon, min(15.0, provider.max_radius_km))
    except Exception as e:                                    # noqa: BLE001
        check("the fetch completed", False, repr(e))
        return failures
    elapsed = time.monotonic() - started

    if not check("stations returned", len(found) > 0,
                 f"{len(found)}, last_error={provider.last_error}"):
        return failures

    national = len(provider.stations)
    print(f"    ({len(found)} nearby in {elapsed:.1f}s"
          + (f", {national} cached nationally" if national else "") + ")")

    prices = [v for s in found for k, v in s.items()
              if k in provider.grades and isinstance(v, (int, float))]
    if check("prices present", bool(prices)):
        low, high = min(prices), max(prices)
        median = statistics.median(prices)
        shown = median * 100 if provider.display_scale == "minor" else median
        print(f"    {low:.3f}–{high:.3f}, median {median:.3f} "
              f"{provider.currency}/{provider.volume_unit} "
              f"(shown as {shown:.1f})")
        # A US gallon is about 3.8 litres, so the same fuel is a far bigger
        # number there; the bounds follow the unit, not the currency.
        lo, hi = (1.5, 12.0) if provider.volume_unit == "gal_us" else (0.3, 5.0)
        check("the median is a plausible pump price", lo <= median <= hi, median)
        check("no price is absurd", all(lo <= p <= hi for p in prices),
              [p for p in prices if not (lo <= p <= hi)][:5])

    check("every station has coordinates",
          all(s.get("latitude") is not None and s.get("longitude") is not None
              for s in found))
    check("every station has a distance", all(s.get("dist") is not None for s in found))
    check("distances are within the radius",
          all(s["dist"] <= 16.0 for s in found), max(s["dist"] for s in found))
    check("no undeclared grade keys",
          all(k in provider.grades for s in found for k in s
              if k not in {"site_id", "brand", "address", "town", "postcode",
                           "latitude", "longitude", "last_updated", "dist"}))

    nearest = min(found, key=lambda s: s["dist"])
    print(f"    nearest: {nearest.get('brand')} — {nearest.get('address')} "
          f"({nearest['dist']} km) {nearest.get('postcode') or ''}")
    return failures


async def main(regions: list[str]) -> int:
    failures = []
    for region in regions:
        failures.extend(await check_region(region))
    print("\n" + ("ALL LIVE CHECKS PASS" if not failures
                  else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    wanted = [r.upper() for r in sys.argv[1:]] or list(PLACES)
    unknown = [r for r in wanted if r not in PLACES]
    if unknown:
        print(f"unknown region(s): {unknown}; known: {list(PLACES)}")
        sys.exit(2)
    sys.exit(asyncio.run(main(wanted)))
