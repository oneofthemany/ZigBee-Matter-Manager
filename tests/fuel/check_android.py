#!/usr/bin/env python3
"""
Static checks on the Android fuel code.

    python3 tests/fuel/check_android.py

There is no JVM in this project's dev setup, so the Kotlin cannot be compiled
here. This is not a substitute for that — it catches the mistakes that are
findable without a compiler: unbalanced delimiters, references to symbols that
were renamed, string resources whose placeholder count does not match the call,
and hardcoded currency or unit literals creeping back into the car screen.

Run it before trusting an Android change that has not been built.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "android/app/src/main/java/com/zmm/presence"
STRINGS = REPO / "android/app/src/main/res/values/strings.xml"

FILES = {
    "HubClient.kt": SRC / "HubClient.kt",
    "Prefs.kt": SRC / "Prefs.kt",
    "FuelScreen.kt": SRC / "car/FuelScreen.kt",
}

failures: list[str] = []


def check(label: str, ok: bool, detail: object = "") -> bool:
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else f"  <- {detail!r}"))
    if not ok:
        failures.append(label)
    return bool(ok)


def strip(src: str) -> str:
    """Remove strings and comments, so delimiters inside them do not count."""
    src = re.sub(r'"""[\s\S]*?"""', '""', src)
    src = re.sub(r'"(\\.|[^"\\\n])*"', '""', src)
    src = re.sub(r"//[^\n]*", "", src)
    return re.sub(r"/\*[\s\S]*?\*/", "", src)


def main() -> int:
    sources = {name: path.read_text(encoding="utf-8") for name, path in FILES.items()}

    print("\n  delimiters balance")
    for name, src in sources.items():
        bare = strip(src)
        check(f"{name} braces", bare.count("{") == bare.count("}"),
              bare.count("{") - bare.count("}"))
        check(f"{name} parens", bare.count("(") == bare.count(")"),
              bare.count("(") - bare.count(")"))

    print("\n  the units contract is wired through")
    hub = sources["HubClient.kt"]
    check("FuelUnits exists", "data class FuelUnits(" in hub)
    check("FuelNearby exists", "data class FuelNearby(" in hub)
    check("fetchFuelNearby returns it", "Result<FuelNearby>" in hub)
    check("units are parsed from the response", "FuelUnits.from(" in hub)
    check("station_level is read", 'optBoolean("station_level"' in hub)
    check("the grade list is read", 'optJSONObject("fuel_types")' in hub)
    for field in ("currency", "symbol", "volume", "distance", "display_scale", "decimals"):
        check(f"units.{field} parsed", f'"{field}"' in hub)

    screen = sources["FuelScreen.kt"]
    check("prices are formatted by the region", "units.format(" in screen)
    check("markers are too", "units.markerLabel(" in screen)
    check("distance follows the region", 'units.distance == "km"' in screen)
    check("the average branch exists", "!stationLevel ->" in screen)
    check("grades cycle from the region", "prefs.carFuelGradeCodes" in screen)
    check("and are cached", "prefs.setCarFuelGrades(" in screen)

    print("\n  no hardcoded UK units remain in the car screen")
    for pattern, why in (
        (r'"%\.1fp"', "no pence format string"),
        (r'"%\.0f"\.format\(\s*s\.price', "no pence marker label"),
        (r'listOf\("E10", "E5", "B7", "SDV"\)', "no hardcoded UK grade cycle"),
        (r'"premium petrol"', "no hardcoded UK grade label"),
    ):
        check(why, re.search(pattern, screen) is None, re.search(pattern, screen))

    print("\n  Prefs")
    prefs = sources["Prefs.kt"]
    check("grades are stored", "var carFuelGrades" in prefs)
    check("codes are exposed in order", "val carFuelGradeCodes" in prefs)
    check("labels are looked up", "fun carFuelGradeLabel" in prefs)
    check("a UK fallback exists for a fresh install", "UK_GRADE_CODES" in prefs)
    check("stored as JSON, not a delimited string", "org.json.JSONArray(" in prefs)

    print("\n  string resources")
    root = ET.parse(STRINGS).getroot()
    strings = {e.get("name"): "".join(e.itertext()) for e in root if e.tag == "string"}
    used = set(re.findall(r"R\.string\.(\w+)", screen))
    for name in sorted(used):
        check(f"{name} is defined", name in strings, sorted(strings)[:5])

    # A getString() call must pass as many arguments as the resource expects.
    for name in sorted(used):
        if name not in strings:
            continue
        expected = len(set(re.findall(r"%(\d+)\$", strings[name])))
        calls = re.findall(
            r"getString\(\s*com\.zmm\.presence\.R\.string\.%s\s*(,[^;]*?)?\)\s*$" % name,
            screen, re.M)
        if expected == 0:
            continue
        # Count top-level commas in the argument tail of each call site.
        found = None
        for tail in calls:
            if tail is None:
                found = 0
                continue
            # Kotlin permits a trailing comma in an argument list, so strip
            # one before counting or every call looks like it passes one extra.
            tail = tail.rstrip().rstrip(",")
            depth, count = 0, 0
            for ch in tail:
                if ch in "([":
                    depth += 1
                elif ch in ")]":
                    depth -= 1
                elif ch == "," and depth == 0:
                    count += 1
            found = count
        if found is not None:
            check(f"{name} gets its {expected} argument(s)", found == expected,
                  f"found {found}")

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
