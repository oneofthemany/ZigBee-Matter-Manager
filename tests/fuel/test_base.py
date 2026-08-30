"""The provider contract: caching, radius filtering, fallback, degradation."""

from __future__ import annotations

import asyncio
import time

from harness import Checker
from modules.fuel.base import (BulkSnapshotProvider, FallbackProvider,
                               RadiusQueryProvider, haversine_km)

LONDON = {"site_id": "a", "brand": "Shell", "latitude": 51.5074,
          "longitude": -0.1278, "postcode": "SW1", "E10": 1.399}
NEAR = {"site_id": "b", "brand": "BP", "latitude": 51.52, "longitude": -0.12,
        "postcode": "N1", "E10": 1.359}
MANCHESTER = {"site_id": "c", "brand": "Esso", "latitude": 53.4808,
              "longitude": -2.2426, "postcode": "M1", "E10": 1.299}


class Bulk(BulkSnapshotProvider):
    region, label = "GB", "test"
    grades = {"E10": "Petrol"}

    def __init__(self, rows):
        super().__init__({})
        self.rows = rows
        self.calls = 0

    async def _fetch_all(self):
        self.calls += 1
        return list(self.rows)


class Radius(RadiusQueryProvider):
    region, label = "DE", "test"
    max_radius_km = 25.0
    min_interval_s = 0.15

    def __init__(self):
        super().__init__({})
        self.calls = 0

    async def _fetch_nearby(self, lat, lon, radius_km):
        self.calls += 1
        return [{"site_id": f"s{self.calls}", "latitude": lat,
                 "longitude": lon, "E10": 1.7}]


async def _bulk(c: Checker) -> None:
    c.section("BulkSnapshotProvider")
    p = Bulk([LONDON, NEAR, MANCHESTER])
    got = await p.nearby(51.5074, -0.1278, 8.0)
    c.check("radius filters to 2 of 3", len(got) == 2, [g["site_id"] for g in got])
    c.check("distance attached", all("dist" in g for g in got))
    c.check("source rows not mutated", "dist" not in LONDON)

    await p.nearby(51.5074, -0.1278, 8.0)
    c.check("TTL serves the second query from cache", p.calls == 1, p.calls)
    await p.refresh()
    c.check("refresh forces a fetch", p.calls == 2, p.calls)

    stale = Bulk([LONDON])
    await stale.nearby(51.5, -0.13, 8.0)
    stale.rows = []
    c.check("an empty fetch keeps stale data",
            await stale.refresh() and len(stale.stations) == 1)

    class Boom(Bulk):
        async def _fetch_all(self):
            raise RuntimeError("upstream down")

    b = Boom([])
    c.check("a raising fetch returns [], not an exception",
            await b.nearby(51.5, -0.1, 8) == [])
    c.check("and records why", "upstream down" in (b.last_error or ""), b.last_error)

    clamped = Bulk([MANCHESTER])
    c.check("radius clamped to the provider maximum",
            await clamped.nearby(51.5074, -0.1278, 100000) == [])


async def _truncation(c: Checker) -> None:
    c.section("truncated-response guard")
    rows = [{"site_id": str(i), "latitude": 51.5, "longitude": -0.1, "E10": 1.4}
            for i in range(100)]
    p = Bulk(rows)
    await p.nearby(51.5, -0.1, 8)
    c.check("full snapshot cached", len(p.stations) == 100, len(p.stations))

    p.rows = rows[:20]
    await p.refresh()
    c.check("a truncated reply is rejected", len(p.stations) == 100, len(p.stations))
    c.check("and says what it saw", "down from 100" in (p.last_error or ""), p.last_error)
    c.check("the query still answers", len(await p.nearby(51.5, -0.1, 8)) == 100)

    p.rows = rows[:60]
    await p.refresh()
    c.check("a plausible shrink is accepted", len(p.stations) == 60, len(p.stations))

    first = Bulk(rows[:5])
    await first.nearby(51.5, -0.1, 8)
    c.check("a first fetch is never rejected", len(first.stations) == 5)

    off = Bulk(rows)
    off.min_retained_fraction = 0.0
    await off.nearby(51.5, -0.1, 8)
    off.rows = rows[:1]
    await off.refresh()
    c.check("the guard can be disabled", len(off.stations) == 1)


async def _radius(c: Checker) -> None:
    c.section("RadiusQueryProvider")
    p = Radius()
    await p.nearby(52.52, 13.40, 10)
    await p.nearby(52.52, 13.40, 10)
    c.check("the same bucket is one upstream call", p.calls == 1, p.calls)
    await p.nearby(48.13, 11.58, 10)
    c.check("a different bucket queries again", p.calls == 2, p.calls)
    c.check("radius clamped to the cap", p.clamp_radius(100) == 25.0)

    limited = Radius()
    started = time.monotonic()
    await asyncio.gather(*[limited.nearby(50.0 + i, 8.0, 10) for i in range(3)])
    elapsed = time.monotonic() - started
    c.check("the limiter spaces concurrent misses", elapsed >= 0.30, f"{elapsed:.3f}s")
    c.check("all three buckets fetched", limited.calls == 3, limited.calls)

    failing = Radius()
    await failing.nearby(52.52, 13.40, 10)
    failing.cache_ttl_s = 0

    async def boom(*a):
        raise RuntimeError("429")

    failing._fetch_nearby = boom
    c.check("an upstream failure serves the stale bucket",
            len(await failing.nearby(52.52, 13.40, 10)) == 1)


async def _fallback(c: Checker) -> None:
    c.section("FallbackProvider")
    primary, secondary = Bulk([]), Bulk([LONDON])
    chain = FallbackProvider("GB", "United Kingdom", [primary, secondary])
    got = await chain.nearby(51.5074, -0.1278, 8.0)
    c.check("falls through to the second source", len(got) == 1, got)
    c.check("reports which source answered", chain.source == secondary.source)
    c.check("speaks the head's dialect", chain.grades == primary.grades)

    good = Bulk([LONDON])
    winner = FallbackProvider("GB", "UK", [good, secondary])
    await winner.nearby(51.5074, -0.1278, 8.0)
    c.check("the primary wins when it answers", winner._active is good)

    rural = FallbackProvider("GB", "UK", [Bulk([MANCHESTER])])
    got = await rural.nearby(51.5074, -0.1278, 8.0)
    c.check("an empty radius is not an error",
            got == [] and rural.last_error is None, rural.last_error)

    class Unconfigured(Bulk):
        @property
        def configured(self):
            return False

    skipped = FallbackProvider("GB", "UK", [Unconfigured([]), Bulk([LONDON])])
    c.check("an unconfigured child is skipped",
            len(await skipped.nearby(51.5074, -0.1278, 8)) == 1)


def run() -> Checker:
    c = Checker("base")
    c.section("haversine")
    distance = haversine_km(51.5074, -0.1278, 53.4808, -2.2426)
    c.check("London to Manchester is about 262 km", 255 < distance < 270, distance)
    c.check("identity is zero", haversine_km(51.5, -0.1, 51.5, -0.1) == 0.0)

    for coroutine in (_bulk, _truncation, _radius, _fallback):
        asyncio.run(coroutine(c))
    return c
