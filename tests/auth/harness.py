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
    r'@(?P<obj>app|router)\.(?P<method>get|post|put|delete|patch|websocket)\(\s*'
    r'(?:[rf]?["\'])(?P<path>[^"\']*)',
    re.S,
)

#: `router = APIRouter(prefix="/api/ai")` — paths on that router are relative,
#: so the prefix has to be put back or the route looks like "/chat" and gets
#: dropped as non-API. modules/{ai,telemetry,dongle_jedi}_api.py and
#: modules/safe_deploy.py all do this.
_ROUTER_PREFIX = re.compile(r'APIRouter\(\s*prefix\s*=\s*["\'](?P<prefix>[^"\']*)')

#: Any "/api/..." string literal in the frontend, however it is called —
#: fetch, apiFetch, a bare constant. Template placeholders become "X".
_FRONTEND_CALL = re.compile(r"""['"`](/api/[A-Za-z0-9_\-/{}$.]*)""")


def frontend_api_paths() -> "dict[str, set]":
    """Every /api/ path the shipped UI references, mapped to the files using it.

    The route table can be complete and still wrong if the SPA calls a path no
    route declares, or one the scope table does not map — this is the check
    that catches a tab going 403 after deny-by-default lands.
    """
    found: dict = {}
    roots = [Path(REPO / "static")]
    for root in roots:
        for f in list(root.rglob("*.js")) + list(root.rglob("*.html")):
            if "vendor" in str(f) or "node_modules" in str(f):
                continue
            for m in _FRONTEND_CALL.finditer(f.read_text(errors="ignore")):
                path = re.sub(r"\$\{[^}]*\}", "X", m.group(1)).rstrip("/?")
                if path.startswith("/api/"):
                    found.setdefault(path, set()).add(f.name)
    return found


def registered_routes() -> List[Tuple[str, str, str]]:
    """Every (method, path, source file) the app registers.

    Read from source rather than from `app.routes` so the test needs no venv.
    A route registered by a call this regex cannot see would be invisible here
    — see test_scope_coverage's total-count assertion, which pins the number
    it finds so a change in registration style cannot silently shrink it.
    """
    out: List[Tuple[str, str, str]] = []
    # routes/ is the FastAPI surface by convention, but the *_api.py modules
    # under modules/ register ~100 more directly on `app` (ai, automations,
    # swarm, telemetry, zones, safe_deploy, cast_sync). Scanning only routes/
    # is how those were missed once already — glob the engine too.
    files = sorted(REPO.glob("modules/**/*.py"))
    files += sorted((REPO / "routes").glob("*.py"))
    files += sorted((REPO / "core").glob("*.py"))
    files += sorted((REPO / "handlers").glob("*.py"))
    files.append(REPO / "main.py")
    for f in files:
        if "__pycache__" in str(f):
            continue
        text = f.read_text()
        pm = _ROUTER_PREFIX.search(text)
        prefix = pm.group("prefix") if pm else ""
        for m in _DECORATOR.finditer(text):
            path = m.group("path")
            if m.group("obj") == "router" and prefix:
                path = prefix + path
            out.append((m.group("method").upper(), path, f.name))
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
