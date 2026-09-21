"""
Shared scaffolding for the auth tests.

Plain scripts, matching tests/media and tests/fuel: no framework, each module
exposes `run()` driven by tests/auth/run_all.py.

Nothing here imports FastAPI. The route table is recovered by reading the
decorators out of routes/*.py and main.py, which keeps the coverage test
runnable on a box with none of the app's dependencies (AGENTS.md §The dev box)
and is why `modules.auth_scopes` carries no framework import.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Tuple

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: Matches `@app.get("/api/...")` and the router spelling, across the line
#: break that black leaves when a decorator carries dependencies.
_DECORATOR = re.compile(
    r'@(?:app|router)\.(get|post|put|delete|patch|websocket)\(\s*'
    r'(?:[rf]?["\'])(?P<path>[^"\']*)',
    re.S,
)


def registered_routes() -> List[Tuple[str, str, str]]:
    """Every (method, path, source file) the app registers.

    Read from source rather than from `app.routes` so the test needs no venv.
    A route registered by a call this regex cannot see would be invisible here
    — see test_scope_coverage's total-count assertion, which pins the number
    it finds so a change in registration style cannot silently shrink it.
    """
    out: List[Tuple[str, str, str]] = []
    files = sorted((REPO / "routes").glob("*.py")) + [REPO / "main.py"]
    for f in files:
        for m in _DECORATOR.finditer(f.read_text()):
            out.append((m.group(1).upper(), m.group("path"), f.name))
    return out


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
