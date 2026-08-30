"""
The query layer: response contract, region selection, geocoding, the registry.

The response-shape checks are a regression gate. The keys in `NEARBY_KEYS` and
`STATUS_KEYS` are what the Drive tab and the Android client were written against
before regions existed; none may ever disappear or be renamed. The contract only
grows, and what it grew by is named explicitly so an accidental extra key is
still caught rather than waved through.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import yaml
from harness import REPO, Checker

from modules import location
from modules.fuel import registry
from modules.fuel.base import BulkSnapshotProvider
from modules.fuel.service import FUEL_TYPES, FuelPriceService, maps_url

NEARBY_KEYS = {"success", "fuel", "fuel_label", "radius_km", "count",
               "stations", "data_age_s"}
NEARBY_ADDED = {"region", "units", "station_level", "attribution"}
STATION_KEYS = {"site_id", "brand", "address", "postcode", "distance_km",
                "latitude", "longitude", "price", "prices", "last_updated",
                "maps_url"}
STATUS_KEYS = {"stations_loaded", "source", "last_refresh", "last_error",
               "fuel_types"}
STATUS_ADDED = {"region", "region_label", "units", "station_level",
                "attribution", "default_grade"}

ROWS = [
    {"site_id": "a", "brand": "Shell", "address": "1 St", "postcode": "SW1",
     "latitude": 51.5074, "longitude": -0.1278, "last_updated": "2026-08-30",
     "E10": 1.399, "B7": 1.459},
    {"site_id": "b", "brand": "BP", "address": "2 St", "postcode": "N1",
     "latitude": 51.5100, "longitude": -0.1300, "last_updated": "2026-08-30",
     "E10": 1.359},
    {"site_id": "c", "brand": "Esso", "address": "3 St", "postcode": "E1",
     "latitude": 51.5090, "longitude": -0.1290, "last_updated": "2026-08-30",
     "E10": 1.359},
]


class Fake(BulkSnapshotProvider):
    region, label = "GB", "fake"
    grades = FUEL_TYPES
    currency, currency_symbol, display_scale = "GBP", "£", "minor"
    attribution = "Test attribution"

    def __init__(self, rows=ROWS):
        super().__init__({})
        self.rows = rows

    async def _fetch_all(self):
        return list(self.rows)


def _service(provider) -> FuelPriceService:
    svc = FuelPriceService({})
    svc._provider = provider
    svc._region = "GB"
    return svc


async def _responses(c: Checker) -> None:
    c.section("best_nearby response contract")
    r = await _service(Fake()).best_nearby(51.5074, -0.1278, "E10", 8.0, 10)
    c.check("no original key lost", NEARBY_KEYS <= set(r), NEARBY_KEYS - set(r))
    c.check("keys exactly as expected", set(r) == NEARBY_KEYS | NEARBY_ADDED,
            set(r) ^ (NEARBY_KEYS | NEARBY_ADDED))
    c.check("fuel label from the region's grades", r["fuel_label"] == "Petrol (E10)")
    c.check("count is the pre-slice total", r["count"] == 3, r["count"])
    c.check("station keys exactly as expected", set(r["stations"][0]) == STATION_KEYS,
            set(r["stations"][0]) ^ STATION_KEYS)
    c.check("cheapest first",
            [s["price"] for s in r["stations"]] == [1.359, 1.359, 1.399])
    c.check("distance breaks a price tie",
            [s["site_id"] for s in r["stations"][:2]] == ["c", "b"],
            [(s["site_id"], s["distance_km"]) for s in r["stations"]])
    c.check("the prices map omits absent grades",
            r["stations"][2]["prices"] == {"E10": 1.399, "B7": 1.459})
    c.check("units block present", set(r["units"]) ==
            {"currency", "symbol", "volume", "distance", "display_scale", "decimals"},
            r["units"])
    c.check("the UK displays pence", r["units"]["display_scale"] == "minor")
    c.check("prices stay in major units", r["stations"][0]["price"] == 1.359)
    c.check("attribution passed through", r["attribution"] == "Test attribution")

    c.section("limits and errors")
    r = await _service(Fake()).best_nearby(51.5074, -0.1278, "E10", 8.0, 1)
    c.check("limit slices the list", len(r["stations"]) == 1 and r["count"] == 3)
    r = await _service(Fake()).best_nearby(51.5074, -0.1278, "E10", 999, 10)
    c.check("radius clamped", r["radius_km"] == 40.0, r["radius_km"])

    r = await _service(Fake()).best_nearby(51.5, -0.1, "XYZ", 8.0, 10)
    c.check("an unknown grade is rejected",
            r["success"] is False and "Unknown fuel" in r["error"])

    class Dead(Fake):
        async def _fetch_all(self):
            self._last_error = "feed down"
            return []

    r = await _service(Dead()).best_nearby(51.5, -0.1, "E10", 8.0, 10)
    c.check("a dead feed is an error payload, not a raise",
            r["success"] is False and r["error"] == "feed down", r)

    class Raiser(Fake):
        async def nearby(self, *a):
            raise RuntimeError("boom")

    r = await _service(Raiser()).best_nearby(51.5, -0.1, "E10", 8.0, 10)
    c.check("a raising provider becomes an error payload",
            r["success"] is False and "boom" in r["error"], r)

    far = [{**ROWS[0], "latitude": 53.4808, "longitude": -2.2426}]
    r = await _service(Fake(far)).best_nearby(51.5074, -0.1278, "E10", 8.0, 10)
    c.check("nothing in radius still succeeds",
            r["success"] is True and r["count"] == 0, r)
    r = await _service(Fake([ROWS[1]])).best_nearby(51.5074, -0.1278, "SDV", 8.0, 10)
    c.check("a grade no station sells succeeds with zero",
            r["success"] is True and r["count"] == 0, r)

    c.section("status contract")
    st = _service(Fake()).status()
    c.check("no original status key lost", STATUS_KEYS <= set(st), STATUS_KEYS - set(st))
    c.check("status keys exactly as expected", set(st) == STATUS_KEYS | STATUS_ADDED,
            set(st) ^ (STATUS_KEYS | STATUS_ADDED))


def _maps(c: Checker) -> None:
    c.section("maps_url")
    c.check("brand and postcode preferred",
            maps_url({"brand": "Shell", "postcode": "SW1A 1AA"}).endswith("Shell+SW1A+1AA"))
    url = maps_url({"brand": "Agip Eni", "address": "Via Roma 1",
                    "town": "AGRIGENTO", "latitude": 37.3, "longitude": 13.5})
    c.check("address and town when there is no postcode",
            "Agip+Eni" in url and "AGRIGENTO" in url, url)
    c.check("coordinates are the last resort",
            "37.3%2C13.5" in maps_url({"latitude": 37.3, "longitude": 13.5}))


def _locations(c: Checker) -> None:
    c.section("location block")
    c.check("reads location", location.home_coords(
        {"location": {"latitude": 51.5, "longitude": -0.1}}) == (51.5, -0.1))
    c.check("falls back to weather", location.home_coords(
        {"weather": {"latitude": 52.0, "longitude": 0.5}}) == (52.0, 0.5))
    c.check("location wins over weather", location.home_coords(
        {"location": {"latitude": 51.5, "longitude": -0.1},
         "weather": {"latitude": 52.0, "longitude": 0.5}}) == (51.5, -0.1))
    c.check("blank location falls through", location.home_coords(
        {"location": {"latitude": None, "longitude": None},
         "weather": {"latitude": 52.0, "longitude": 0.5}}) == (52.0, 0.5))
    c.check("nothing set is None", location.home_coords({}) is None)
    c.check("junk coordinates are None", location.home_coords(
        {"location": {"latitude": "abc", "longitude": "x"}}) is None)
    c.check("out of range is None", location.home_coords(
        {"location": {"latitude": 999, "longitude": 0}}) is None)
    c.check("country uppercased", location.country({"location": {"country": "de"}}) == "DE")
    c.check("a three-letter country is rejected",
            location.country({"location": {"country": "DEU"}}) == "")
    c.check("subdivision uppercased",
            location.subdivision({"location": {"subdivision": "nsw"}}) == "NSW")

    c.section("config writes keep comments")
    tmp = Path(tempfile.mkdtemp()) / "config.yaml"
    shutil.copy(REPO / "config" / "config.yaml", tmp)
    before = tmp.read_text()
    location.persist({"country": "au"}, path=tmp)
    after = tmp.read_text()
    comments = (sum(1 for l in before.splitlines() if l.strip().startswith("#")),
                sum(1 for l in after.splitlines() if l.strip().startswith("#")))
    c.check("every comment survives", comments[0] == comments[1], comments)
    diff = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    c.check("exactly one line changed", len(diff) == 1, diff)
    c.check("and it is the country line",
            diff and diff[0][1].strip() == "country: AU", diff)
    c.check("no lines added or removed",
            len(before.splitlines()) == len(after.splitlines()))
    c.check("the file still parses", yaml.safe_load(after)["location"]["country"] == "AU")

    for key, value, label in (("country", "DEU", "a three-letter country"),
                              ("latitude", 999, "an out-of-range latitude"),
                              ("longitude", "abc", "a non-numeric longitude"),
                              ("subdivision", "TOOLONG", "an over-long subdivision")):
        try:
            location.persist({key: value}, path=tmp)
            rejected = False
        except ValueError:
            rejected = True
        c.check(f"rejects {label}", rejected)


def _registry(c: Checker) -> None:
    c.section("registry")
    expected = ["GB", "DE", "ES", "FR", "IT", "AU-NSW", "AU-QLD", "AU-WA"]
    c.check("every region registered", list(registry.REGIONS) == expected,
            list(registry.REGIONS))
    c.check("known_regions drops the build callable",
            all("build" not in r for r in registry.known_regions()))

    cfg = {"fuel": {}}
    for key in expected:
        provider = registry.build_provider(key, cfg)
        c.check(f"{key} builds", provider is not None)
        c.check(f"{key} region matches its key", provider.region == key, provider.region)
        c.check(f"{key} default grade is one of its own",
                provider.default_grade in provider.grades,
                (provider.default_grade, list(provider.grades)))
        c.check(f"{key} carries attribution", bool(provider.attribution))
        c.check(f"{key} grade codes are uppercase",
                all(k and k == k.upper() for k in provider.grades), list(provider.grades))

    c.section("region resolution")
    c.check("blank falls back to the default", registry.resolve_region("", "") == "GB")
    c.check("an unregistered country falls back", registry.resolve_region("JP") == "GB")
    c.check("lowercase resolves", registry.resolve_region("fr") == "FR")
    c.check("a subdivided country resolves",
            registry.resolve_region("AU", "NSW") == "AU-NSW")
    c.check("queensland resolves", registry.resolve_region("au", "qld") == "AU-QLD")
    c.check("a country with only subdivisions picks the first",
            registry.resolve_region("AU") == "AU-NSW")
    c.check("an unregistered state falls back to a registered one",
            registry.resolve_region("AU", "VIC") == "AU-NSW")

    c.section("credentials")
    c.check("keyless regions are ready out of the box",
            all(registry.build_provider(k, cfg).configured
                for k in ("ES", "FR", "IT", "AU-NSW", "AU-WA")))
    c.check("regions needing a key are not",
            not any(registry.build_provider(k, cfg).configured
                    for k in ("DE", "AU-QLD")))

    c.section("dialects")
    for key, currency, scale in (("GB", "GBP", "minor"), ("DE", "EUR", "major"),
                                 ("ES", "EUR", "major"), ("FR", "EUR", "major"),
                                 ("IT", "EUR", "major"), ("AU-NSW", "AUD", "minor"),
                                 ("AU-QLD", "AUD", "minor"), ("AU-WA", "AUD", "minor")):
        provider = registry.build_provider(key, cfg)
        c.check(f"{key} is {currency}", provider.currency == currency, provider.currency)
        c.check(f"{key} displays {scale} units",
                provider.display_scale == scale, provider.display_scale)


async def _region_from_config(c: Checker) -> None:
    c.section("region from config")
    for cfg, expect in (({}, "GB"),
                        ({"location": {"country": "GB"}}, "GB"),
                        ({"location": {"country": "DE"}}, "DE"),
                        ({"location": {"country": "fr"}}, "FR"),
                        ({"location": {"country": "AU", "subdivision": "QLD"}}, "AU-QLD"),
                        ({"location": {"country": "JP"}}, "GB")):
        got = FuelPriceService(cfg).region
        c.check(f"{cfg.get('location', {}) or 'blank'} -> {expect}", got == expect, got)


def run() -> Checker:
    c = Checker("service")
    asyncio.run(_responses(c))
    _maps(c)
    _locations(c)
    _registry(c)
    asyncio.run(_region_from_config(c))
    return c
