"""
Shared scaffolding for the fuel tests.

These are plain scripts, not pytest: the project carries no test framework and
adding one to run eight files is a dependency for its own sake. Each test module
exposes `run()` and is driven by tests/fuel/run_all.py.

`aiohttp` is stubbed on import so the parsers can be exercised on a machine that
has not installed the app's dependencies. Nothing here makes a network call —
every payload comes from tests/fuel/fixtures, captured from the live APIs.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def stub_aiohttp() -> None:
    """
    Stand in for aiohttp so provider modules import without it.

    Only the names touched at import time are needed: every test drives the
    pure `_parse` half, never the HTTP half.
    """
    if "aiohttp" in sys.modules:
        return
    stub = types.ModuleType("aiohttp")

    class _ClientTimeout:
        def __init__(self, *a, **k):
            pass

    stub.ClientTimeout = _ClientTimeout
    stub.ClientSession = object
    sys.modules["aiohttp"] = stub


stub_aiohttp()


class Checker:
    """Collects pass/fail lines so a module can report as a group."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.failures: list[str] = []
        self.passed = 0

    def section(self, title: str) -> None:
        print(f"\n  {title}")

    def check(self, label: str, ok: bool, detail: object = "") -> bool:
        if ok:
            self.passed += 1
            print(f"    ok   {label}")
        else:
            self.failures.append(f"{self.name}: {label}")
            print(f"    FAIL {label}  <- {detail!r}"[:400])
        return bool(ok)


def fixture(name: str):
    """A captured payload. JSON is parsed; anything else is returned as text."""
    path = FIXTURES / name
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


#: Keys on a station dict that are not fuel grades.
META_KEYS = {"site_id", "brand", "address", "town", "postcode",
             "latitude", "longitude", "last_updated", "dist"}


def implausible_prices(stations, lo=0.3, hi=5.0):
    """
    Prices that are not plausible money per litre.

    The assertion that earns its keep: it is what catches a comma decimal read
    as an English one (1599.0), an Australian cents value left unscaled (180.0),
    and a Queensland tenth-of-a-cent value divided by the wrong factor.
    """
    return [(s.get("site_id"), k, v) for s in stations for k, v in s.items()
            if k not in META_KEYS and isinstance(v, (int, float))
            and not (lo <= v <= hi)]


def undeclared_grades(stations, provider_cls):
    """Grade keys a station carries that the provider never declared."""
    return sorted({k for s in stations for k in s
                   if k not in META_KEYS and k not in provider_cls.grades})


def coordinates_sane(stations) -> bool:
    return all(-90 <= s["latitude"] <= 90 and -180 <= s["longitude"] <= 180
               for s in stations)
