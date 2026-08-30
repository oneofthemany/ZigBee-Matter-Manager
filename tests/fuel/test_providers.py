"""
Every region adapter, against payloads captured from its live API.

The fixtures in tests/fuel/fixtures are real responses, trimmed to a handful of
stations — except the Queensland ones, which are built from the worked examples
in the published spec because the API needs a subscriber token nobody here has.
That file says so itself.
"""

from __future__ import annotations

from harness import (Checker, coordinates_sane, fixture, implausible_prices,
                     undeclared_grades)

from modules.fuel.providers.au_common import (postcode_from_address,
                                              price_from_cents)
from modules.fuel.providers.au_nsw import NewSouthWalesFuelCheck
from modules.fuel.providers.au_qld import QueenslandFuelPrices
from modules.fuel.providers.au_wa import WesternAustraliaFuelWatch
from modules.fuel.providers.de_tankerkoenig import GermanyTankerkoenig, _postcode
from modules.fuel.providers.es_minetur import SpainMinetur
from modules.fuel.providers.es_minetur import _num as es_num
from modules.fuel.providers.fr_gouv import FranceGouv
from modules.fuel.providers.fr_gouv import _num as fr_num
from modules.fuel.providers.it_mimit import ItalyMimit
from modules.fuel.providers.us_eia import (UnitedStatesEIA, _area_name,
                                           area_for_state)


def _spain(c: Checker) -> None:
    c.section("Spain — comma decimals")
    c.check("comma decimal", es_num("1,599") == 1.599, es_num("1,599"))
    c.check("negative comma decimal", es_num("-1,539167") == -1.539167)
    c.check("blank is absent", es_num("") is None)
    c.check("junk is absent", es_num("n/a") is None)
    c.check("a dot decimal still parses", es_num("1.599") == 1.599)

    provider = SpainMinetur({})
    stations = provider._parse(fixture("es_minetur.json"))
    c.check("stations parsed", len(stations) >= 3, len(stations))
    s = next((x for x in stations if x["site_id"] == "4375"), None)
    if c.check("the known station is present", s is not None,
               [x["site_id"] for x in stations]):
        c.check("latitude un-commaed", abs(s["latitude"] - 39.211417) < 1e-6, s["latitude"])
        c.check("negative longitude", abs(s["longitude"] + 1.539167) < 1e-6, s["longitude"])
        c.check("petrol price", s.get("G95E5") == 1.599, s.get("G95E5"))
        c.check("diesel price", s.get("GOA") == 1.789, s.get("GOA"))
        c.check("a blank grade is omitted", "G95E10" not in s)
        c.check("postcode keeps its leading zero", s["postcode"] == "02250", s["postcode"])
        c.check("brand from the forecourt sign", s["brand"] == "Nº 10.935", s["brand"])
        c.check("town carried", s["town"] == "Abengibre", s["town"])
        c.check("document timestamp used", bool(s["last_updated"]))
    c.check("prices plausible", not implausible_prices(stations),
            implausible_prices(stations))
    c.check("no undeclared grades", not undeclared_grades(stations, SpainMinetur),
            undeclared_grades(stations, SpainMinetur))
    c.check("agricultural diesel is not offered",
            "GOB" not in SpainMinetur.grades)
    c.check("an empty document is empty, not a crash", provider._parse({}) == [])
    c.check("a null document is empty", provider._parse(None) == [])


def _france(c: Checker) -> None:
    c.section("France — scaled coordinates, 'None' strings")
    c.check("the string 'None' is absent", fr_num("None") is None)
    c.check("empty is absent", fr_num("") is None)
    c.check("a negative parses", fr_num("-269000") == -269000.0)

    provider = FranceGouv({})
    stations = provider._parse(fixture("fr_gouv.json"))
    c.check("stations parsed", len(stations) >= 3, len(stations))
    s = next((x for x in stations if x["site_id"] == "80570001"), None)
    if c.check("the known station is present", s is not None,
               [x["site_id"] for x in stations]):
        c.check("latitude descaled", abs(s["latitude"] - 50.04475) < 1e-6, s["latitude"])
        c.check("longitude descaled", abs(s["longitude"] - 1.52562) < 1e-6, s["longitude"])
        c.check("gazole price", s.get("GAZOLE") == 2.225, s.get("GAZOLE"))
        c.check("sp95 price", s.get("SP95") == 2.069, s.get("SP95"))
        c.check("an absent grade is omitted", "E10" not in s, s.get("E10"))
        c.check("postcode carried", s["postcode"] == "80570", s["postcode"])
        c.check("brand falls back to the town", s["brand"] == "Dargnies", s["brand"])
        c.check("newest per-fuel stamp used", bool(s["last_updated"]))
    c.check("prices plausible", not implausible_prices(stations),
            implausible_prices(stations))
    c.check("no undeclared grades", not undeclared_grades(stations, FranceGouv),
            undeclared_grades(stations, FranceGouv))
    c.check("coordinates in range", coordinates_sane(stations))
    c.check("an empty export is empty", provider._parse([]) == [])
    c.check("coordinates that would leave the planet are dropped",
            provider._parse([{"id": "x", "latitude": "999999999",
                              "longitude": "1", "gazole_prix": "1.7"}]) == [])


def _italy(c: Checker) -> None:
    c.section("Italy — two CSVs, self-service preferred")
    provider = ItalyMimit({})
    stations = provider._parse(fixture("it_anagrafica.csv"), fixture("it_prezzo.csv"))
    c.check("stations parsed", len(stations) >= 1, len(stations))
    s = next((x for x in stations if x["site_id"] == "59183"), None)
    if c.check("the known station is present", s is not None,
               [x["site_id"] for x in stations]):
        c.check("brand from the flag", s["brand"] == "Agip Eni", s["brand"])
        c.check("coordinates parsed", abs(s["latitude"] - 37.333935) < 1e-6, s["latitude"])
        c.check("town carried", s["town"] == "AGRIGENTO", s["town"])
        c.check("no postcode invented", s["postcode"] is None, s["postcode"])
    c.check("prices plausible", not implausible_prices(stations, 0.3, 3.5),
            implausible_prices(stations, 0.3, 3.5))
    c.check("no undeclared grades", not undeclared_grades(stations, ItalyMimit),
            undeclared_grades(stations, ItalyMimit))
    c.check("the internal marker does not leak",
            not any("_updated" in x for x in stations))

    head = ("Estrazione del 2026-08-29\n"
            "idImpianto|descCarburante|prezzo|isSelf|dtComu\n")
    register = ("Estrazione del 2026-08-29\n"
                "idImpianto|Gestore|Bandiera|Tipo Impianto|Nome Impianto|"
                "Indirizzo|Comune|Provincia|Latitudine|Longitudine\n"
                "1|G|Q8|Stradale|N|Via Roma|Roma|RM|41.9|12.5\n")

    both = head + ("1|Benzina|2.509|0|28/08/2026 12:00:39\n"
                   "1|Benzina|2.149|1|28/08/2026 12:00:40\n")
    got = provider._parse(register, both)
    c.check("self-service wins", got and got[0]["BENZINA"] == 2.149, got)

    reversed_rows = head + ("1|Benzina|2.149|1|28/08/2026 12:00:40\n"
                            "1|Benzina|2.509|0|28/08/2026 12:00:39\n")
    got = provider._parse(register, reversed_rows)
    c.check("a served price cannot overwrite self-service",
            got and got[0]["BENZINA"] == 2.149, got)

    served = head + "1|Benzina|2.509|0|28/08/2026 12:00:39\n"
    got = provider._parse(register, served)
    c.check("the served price is used when it is all there is",
            got and got[0]["BENZINA"] == 2.509, got)

    branded = head + "1|Blue Diesel|2.999|1|28/08/2026 12:00:39\n"
    c.check("branded variants are ignored", provider._parse(register, branded) == [])
    c.check("a station with no prices is dropped", provider._parse(register, head) == [])
    c.check("a banner-only file is empty",
            provider._parse("Estrazione\n", "Estrazione\n") == [])


def _germany(c: Checker) -> None:
    c.section("Germany — Tankerkönig")
    c.check("postcode zero-padded", _postcode(1067) == "01067", _postcode(1067))
    c.check("five digits unchanged", _postcode(10407) == "10407")
    c.check("absent stays absent", _postcode(None) is None)

    provider = GermanyTankerkoenig({"api_key": "x"})
    stations = provider._parse(fixture("de_tankerkoenig.json"))
    c.check("stations parsed", len(stations) >= 5, len(stations))
    c.check("prices plausible", not implausible_prices(stations),
            implausible_prices(stations))
    c.check("no undeclared grades", not undeclared_grades(stations, GermanyTankerkoenig),
            undeclared_grades(stations, GermanyTankerkoenig))
    c.check("distance taken from the API", all(x["dist"] is not None for x in stations))
    c.check("brand present", all(x["brand"] for x in stations))
    c.check("postcodes are five characters",
            all(x["postcode"] is None or len(x["postcode"]) == 5 for x in stations))

    failed = GermanyTankerkoenig({"api_key": "x"})
    c.check("ok=false yields nothing even on HTTP 200",
            failed._parse({"ok": False, "status": "error",
                           "message": "apikey nicht angegeben"}) == [])
    c.check("the upstream message is surfaced",
            "apikey" in (failed.last_error or ""), failed.last_error)
    c.check("radius clamped to 25 km", provider.clamp_radius(100) == 25.0)
    c.check("the rate limit is over a minute",
            GermanyTankerkoenig.min_interval_s > 60)
    c.check("no key means unconfigured", GermanyTankerkoenig({}).configured is False)
    c.check("a key means configured",
            GermanyTankerkoenig({"api_key": "k"}).configured is True)
    c.check("disabled means unconfigured",
            GermanyTankerkoenig({"api_key": "k", "enabled": False}).configured is False)

    closed = {"ok": True, "stations": [
        {"id": "a", "lat": 52.5, "lng": 13.4, "e10": 1.7, "isOpen": False,
         "brand": "Aral", "postCode": 10407},
        {"id": "b", "lat": 52.5, "lng": 13.4, "e10": 1.7, "isOpen": True,
         "brand": "Shell", "postCode": 10407}]}
    only_open = GermanyTankerkoenig({"api_key": "x", "only_open": True})
    c.check("only_open hides closed stations",
            [x["site_id"] for x in only_open._parse(closed)] == ["b"])
    c.check("closed stations are shown by default",
            len(GermanyTankerkoenig({"api_key": "x"})._parse(closed)) == 2)


def _au_common(c: Checker) -> None:
    c.section("Australia — shared dialect")
    c.check("postcode from the end of an address",
            postcode_from_address("36 Henderson Road, Alexandria NSW 2015") == "2015")
    c.check("no postcode when there is none",
            postcode_from_address("Via Roma 1, Roma") is None)
    c.check("empty address", postcode_from_address("") is None)
    c.check("cents to dollars", price_from_cents(203.9) == 2.039)
    c.check("tenths of a cent to dollars",
            price_from_cents(1679, 1000.0) == 1.679)
    c.check("zero is not a price", price_from_cents(0) is None)
    c.check("negative is not a price", price_from_cents(-1) is None)
    c.check("junk is not a price", price_from_cents("abc") is None)


def _nsw(c: Checker) -> None:
    c.section("Australia NSW — FuelCheck")
    provider = NewSouthWalesFuelCheck({})
    merged: dict = {}
    provider._merge(merged, fixture("au_nsw_u91.json"))
    provider._merge(merged, fixture("au_nsw_dl.json"))
    stations = [s for s in merged.values()
                if any(g in s for g in NewSouthWalesFuelCheck.grades)]

    c.check("stations parsed", len(stations) >= 5, len(stations))
    c.check("prices plausible", not implausible_prices(stations, 0.5, 5.0),
            implausible_prices(stations, 0.5, 5.0))
    c.check("no undeclared grades",
            not undeclared_grades(stations, NewSouthWalesFuelCheck),
            undeclared_grades(stations, NewSouthWalesFuelCheck))
    c.check("cents converted to dollars — nothing near 200",
            all(v < 5 for s in stations for k, v in s.items()
                if k in NewSouthWalesFuelCheck.grades))
    c.check("distance from the API", all(s["dist"] is not None for s in stations))
    c.check("postcode pulled out of the address",
            any(s["postcode"] and len(s["postcode"]) == 4 for s in stations),
            [s["postcode"] for s in stations][:5])
    c.check("brand present", all(s["brand"] for s in stations))
    c.check("a timestamp survives the merge",
            any(s["last_updated"] for s in stations))
    # The two captured payloads happen to be disjoint — inner Sydney sells E10
    # rather than U91, so no station appears in both — which makes them a good
    # test that the union is taken rather than the last reply winning.
    u91 = {s["code"] for s in fixture("au_nsw_u91.json")["stations"]}
    dl = {s["code"] for s in fixture("au_nsw_dl.json")["stations"]}
    c.check("both payloads' stations are kept",
            len(stations) == len(u91 | dl), (len(stations), len(u91 | dl)))

    # And the join itself, proven on a station that does sell both.
    two_grades = _merged_grades(provider, {
        "stations": [{"code": 42, "brand": "BP", "address": "1 Rd, Sydney NSW 2000",
                      "location": {"latitude": -33.8, "longitude": 151.2,
                                   "distance": 1.0}}],
        "prices": [{"stationcode": 42, "fueltype": "U91", "price": 203.9,
                    "lastupdated": "2026-08-30 01:00:00"}]})
    merged_two: dict = {}
    provider._merge(merged_two, {
        "stations": [{"code": 42, "brand": "BP", "address": "1 Rd, Sydney NSW 2000",
                      "location": {"latitude": -33.8, "longitude": 151.2,
                                   "distance": 1.0}}],
        "prices": [{"stationcode": 42, "fueltype": "U91", "price": 203.9,
                    "lastupdated": "2026-08-30 01:00:00"}]})
    provider._merge(merged_two, {
        "stations": [{"code": 42, "brand": "BP", "address": "1 Rd, Sydney NSW 2000",
                      "location": {"latitude": -33.8, "longitude": 151.2,
                                   "distance": 1.0}}],
        "prices": [{"stationcode": 42, "fueltype": "DL", "price": 248.9,
                    "lastupdated": "2026-08-30 02:00:00"}]})
    joined = merged_two.get("42") or {}
    c.check("two grades merge onto one station",
            joined.get("U91") == 2.039 and joined.get("DL") == 2.489, joined)
    c.check("one station, not two", len(merged_two) == 1, sorted(merged_two))
    c.check("the newer timestamp wins",
            joined.get("last_updated") == "2026-08-30 02:00:00",
            joined.get("last_updated"))
    c.check("prices join on code, not stationid", bool(two_grades))

    c.check("EV is never a selectable grade", "EV" not in NewSouthWalesFuelCheck.grades)
    c.check("an EV price row is ignored",
            _merged_grades(provider, {
                "stations": [{"code": 1, "brand": "X", "address": "A 2000",
                              "location": {"latitude": -33.8, "longitude": 151.2,
                                           "distance": 1.0}}],
                "prices": [{"stationcode": 1, "fueltype": "EV", "price": 0.0}]}) == [])
    c.check("a zero price is not a price",
            _merged_grades(provider, {
                "stations": [{"code": 2, "brand": "X", "address": "A 2000",
                              "location": {"latitude": -33.8, "longitude": 151.2,
                                           "distance": 1.0}}],
                "prices": [{"stationcode": 2, "fueltype": "U91", "price": 0.0}]}) == [])
    c.check("configured grades are filtered to real ones",
            NewSouthWalesFuelCheck({"grades": ["U91", "NOPE"]})._wanted == ("U91",))
    c.check("an all-bogus grade list falls back to the default",
            NewSouthWalesFuelCheck({"grades": ["NOPE"]})._wanted
            == NewSouthWalesFuelCheck({})._wanted)
    c.check("no credentials needed", provider.configured is True)


def _merged_grades(provider, payload):
    merged: dict = {}
    provider._merge(merged, payload)
    return [s for s in merged.values()
            if any(g in s for g in NewSouthWalesFuelCheck.grades)]


def _wa(c: Checker) -> None:
    c.section("Australia WA — FuelWatch")
    provider = WesternAustraliaFuelWatch({})
    merged: dict = {}
    provider._merge(merged, "ULP", fixture("au_wa_ulp.json"))
    provider._merge(merged, "DSL", fixture("au_wa_dsl.json"))
    stations = provider._parse(merged)

    c.check("stations parsed", len(stations) >= 3, len(stations))
    c.check("prices plausible", not implausible_prices(stations, 0.5, 5.0),
            implausible_prices(stations, 0.5, 5.0))
    c.check("no undeclared grades",
            not undeclared_grades(stations, WesternAustraliaFuelWatch),
            undeclared_grades(stations, WesternAustraliaFuelWatch))
    c.check("cents converted to dollars",
            all(v < 5 for s in stations for k, v in s.items()
                if k in WesternAustraliaFuelWatch.grades))
    c.check("coordinates sane", coordinates_sane(stations))
    c.check("postcode carried as text",
            all(s["postcode"] is None or s["postcode"].isdigit() for s in stations),
            [s["postcode"] for s in stations][:5])
    c.check("town carried", any(s["town"] for s in stations))
    c.check("brand present", all(s["brand"] for s in stations))

    # Today's price is what a driver pays; tomorrow's is fixed but not yet due.
    row = {"id": 1, "siteName": "X", "brandName": "B",
           "address": {"line1": "1 St", "location": "PERTH", "postCode": 6000,
                       "latitude": -31.95, "longitude": 115.86},
           "product": {"priceToday": 186.7, "priceTomorrow": 999.9}}
    got = provider._parse(_merge_one(provider, "ULP", [row]))
    c.check("today's price is used, not tomorrow's",
            got and got[0]["ULP"] == 1.867, got)

    out_of_supply = dict(row, id=2,
                         product={"priceToday": 186.7, "isOutOfSupply": True})
    c.check("an out-of-supply product is skipped",
            provider._parse(_merge_one(provider, "ULP", [out_of_supply])) == [])

    closed = dict(row, id=3, isClosedNow=True)
    hider = WesternAustraliaFuelWatch({"only_open": True})
    c.check("only_open hides a closed site",
            hider._parse(_merge_one(hider, "ULP", [closed])) == [])
    c.check("closed sites are shown by default",
            len(provider._parse(_merge_one(provider, "ULP", [closed]))) == 1)
    c.check("no credentials needed", provider.configured is True)


def _merge_one(provider, grade, rows):
    merged: dict = {}
    provider._merge(merged, grade, rows)
    return merged


def _qld(c: Checker) -> None:
    c.section("Australia QLD — Fuel Price Reporting (spec-derived fixture)")
    provider = QueenslandFuelPrices({"token": "x"})
    provider._brands = provider._parse_brands(fixture("au_qld_brands.json"))
    provider._sites = provider._parse_sites(fixture("au_qld_sites.json"))

    c.check("brands parsed", provider._brands.get(2) == "Caltex", provider._brands)
    c.check("sites parsed", len(provider._sites) == 2, sorted(provider._sites))
    c.check("a site with no coordinates is dropped", "61000001" not in provider._sites)
    c.check("null island is dropped", "61000002" not in provider._sites)
    site = provider._sites.get("61290151") or {}
    c.check("minified name field used", site.get("brand") == "Caltex Surat", site)
    c.check("minified address field used", site.get("address") == "61 Burrowes St")
    c.check("postcode carried", site.get("postcode") == "4417", site.get("postcode"))
    c.check("a blank name falls back to the brand lookup",
            (provider._sites.get("61477713") or {}).get("brand") == "Caltex",
            provider._sites.get("61477713"))

    stations = provider._parse(fixture("au_qld_prices.json"))
    c.check("stations priced", len(stations) == 2, [s["site_id"] for s in stations])
    c.check("prices plausible", not implausible_prices(stations, 0.5, 5.0),
            implausible_prices(stations, 0.5, 5.0))
    c.check("no undeclared grades", not undeclared_grades(stations, QueenslandFuelPrices),
            undeclared_grades(stations, QueenslandFuelPrices))

    brisbane = next(s for s in stations if s["site_id"] == "61477713")
    c.check("tenths of a cent become dollars", brisbane["E10"] == 1.679, brisbane["E10"])
    c.check("a second grade on the same site", brisbane["U91"] == 1.759, brisbane)
    c.check("timestamp carried", bool(brisbane["last_updated"]))

    surat = next(s for s in stations if s["site_id"] == "61290151")
    c.check("the 9999 sentinel is not a price", "U91" not in surat, surat)
    c.check("an unmapped fuel id is ignored",
            not undeclared_grades([surat], QueenslandFuelPrices), surat)
    c.check("diesel priced", surat["DL"] == 1.899, surat.get("DL"))
    c.check("a price for an unknown site is dropped",
            all(s["site_id"] != "62000000" for s in stations))

    c.check("no token means unconfigured", QueenslandFuelPrices({}).configured is False)
    c.check("a token means configured",
            QueenslandFuelPrices({"token": "t"}).configured is True)
    c.check("disabled means unconfigured",
            QueenslandFuelPrices({"token": "t", "enabled": False}).configured is False)
    c.check("the register is stale when empty", QueenslandFuelPrices({})._stale_register())

    empty = QueenslandFuelPrices({"token": "x"})
    empty._sites = provider._sites
    c.check("no price rows is empty, not a crash", empty._parse({"SitePrices": []}) == [])
    c.check("a bare list envelope is accepted",
            len(empty._parse([{"SiteId": 61290151, "FuelId": 8, "Price": 1899}])) == 1)


def _us(c: Checker) -> None:
    c.section("United States — EIA area averages")
    c.check("a state with its own series", area_for_state("California") == ("SCA", "California"))
    c.check("a state code works too", area_for_state("TX")[0] == "STX")
    c.check("lowercase works", area_for_state("ohio")[0] == "SOH")
    c.check("a state without one falls back to its PADD",
            area_for_state("Wyoming") == ("R40", "the Rocky Mountains (PADD 4)"))
    c.check("New England is PADD 1A", area_for_state("Vermont")[0] == "R1X")
    c.check("the Gulf Coast is PADD 3", area_for_state("Louisiana")[0] == "R30")
    c.check("somewhere unrecognised falls back to the nation",
            area_for_state("Puerto Rico") == ("NUS", "the United States"))
    c.check("blank falls back to the nation", area_for_state("")[0] == "NUS")
    c.check("every state maps to a PADD",
            len(__import__("modules.fuel.providers.us_eia", fromlist=["x"]).STATE_PADDS) == 51)
    c.check("area names round-trip", _area_name("SCA") == "California")
    c.check("an unknown area code is returned as itself", _area_name("ZZZ") == "ZZZ")

    provider = UnitedStatesEIA({"api_key": "x"})
    stations = provider._parse(fixture("us_eia.json"), "NUS",
                               "the United States", 39.7392, -104.9903)
    c.check("exactly one record", len(stations) == 1, len(stations))
    s = stations[0]
    c.check("it is an area, not a station", s["site_id"] == "EIA:NUS", s["site_id"])
    c.check("named for the area", s["brand"] == "the United States", s["brand"])
    c.check("no address invented", s["address"] is None and s["postcode"] is None)
    c.check("distance is zero — you are in it", s["dist"] == 0.0)
    c.check("carries the query point", (s["latitude"], s["longitude"]) == (39.7392, -104.9903))
    c.check("regular price", s.get("REGULAR") == 4.085, s.get("REGULAR"))
    c.check("midgrade price", s.get("MIDGRADE") == 4.68, s.get("MIDGRADE"))
    c.check("premium price", s.get("PREMIUM") == 5.06, s.get("PREMIUM"))
    c.check("diesel price", s.get("DIESEL") == 5.652, s.get("DIESEL"))
    c.check("the week-ending date is carried",
            s["last_updated"] == "2026-08-24", s["last_updated"])
    c.check("newest row wins, not the oldest",
            s["REGULAR"] == 4.085, [s.get(g) for g in ("REGULAR",)])
    c.check("prices plausible per gallon",
            not implausible_prices(stations, volume="gal_us"),
            implausible_prices(stations, volume="gal_us"))
    c.check("a per-litre ceiling would wrongly flag these",
            bool(implausible_prices(stations, volume="L")))
    c.check("no undeclared grades", not undeclared_grades(stations, UnitedStatesEIA),
            undeclared_grades(stations, UnitedStatesEIA))

    california = provider._parse(fixture("us_eia_sca.json"), "SCA", "California",
                                 34.05, -118.24)
    c.check("a state series parses", california[0]["REGULAR"] == 5.45, california)
    c.check("named for the state", california[0]["brand"] == "California")
    c.check("only the grades it publishes",
            set(california[0]) - set(META_KEYS_FOR_US) == {"REGULAR"},
            sorted(set(california[0])))

    c.section("United States — declarations and failure")
    c.check("station_level is False", UnitedStatesEIA.station_level is False)
    c.check("priced per US gallon", UnitedStatesEIA.volume_unit == "gal_us")
    c.check("distances shown in miles", UnitedStatesEIA.distance_unit == "mi")
    c.check("dollars are shown as dollars", UnitedStatesEIA.display_scale == "major")
    c.check("no key means unconfigured", UnitedStatesEIA({}).configured is False)
    c.check("a key means configured", UnitedStatesEIA({"api_key": "k"}).configured is True)
    c.check("disabled means unconfigured",
            UnitedStatesEIA({"api_key": "k", "enabled": False}).configured is False)
    c.check("an area can be pinned",
            UnitedStatesEIA({"api_key": "k", "area": "sca"}).area_override == "SCA")

    failing = UnitedStatesEIA({"api_key": "x"})
    c.check("an empty reply yields nothing",
            failing._parse({"response": {"data": []}}, "NUS", "n", 0, 0) == [])
    c.check("and says which area", "NUS" in (failing.last_error or ""), failing.last_error)
    c.check("an error envelope is surfaced",
            failing._parse({"error": "API_KEY_INVALID"}, "NUS", "n", 0, 0) == [])
    c.check("a null payload is empty",
            failing._parse(None, "NUS", "n", 0, 0) == [])
    c.check("an unwrapped data list is accepted",
            len(failing._parse({"data": [{"period": "2026-08-24", "product": "EPMR",
                                          "value": 4.085}]}, "NUS", "n", 0, 0)) == 1)
    c.check("a non-numeric value is skipped",
            failing._parse({"data": [{"period": "p", "product": "EPMR",
                                      "value": None}]}, "NUS", "n", 0, 0) == [])
    c.check("an unknown product is ignored",
            failing._parse({"data": [{"period": "p", "product": "ZZZZ",
                                      "value": 3.0}]}, "NUS", "n", 0, 0) == [])


#: The US record carries the same meta keys as any other station.
META_KEYS_FOR_US = {"site_id", "brand", "address", "town", "postcode",
                    "latitude", "longitude", "last_updated", "dist"}


def run() -> Checker:
    c = Checker("providers")
    for section in (_spain, _france, _italy, _germany,
                    _au_common, _nsw, _wa, _qld, _us):
        section(c)
    return c
