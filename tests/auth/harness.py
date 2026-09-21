"""
Shared scaffolding for the auth tests.

Nothing here imports FastAPI: the route table is recovered by reading
decorators out of source, so the coverage test runs on a bare box
(AGENTS.md §The dev box).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Tuple

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: `@app.get("/api/...")` and the router spelling, across a wrapped line.
_DECORATOR = re.compile(
    r'@(?P<obj>app|router)\.(?P<method>get|post|put|delete|patch|websocket)\(\s*'
    r'(?:[rf]?["\'])(?P<path>[^"\']*)',
    re.S,
)

#: Paths on a prefixed router are relative; the prefix must be put back or
#: the route reads as "/chat" and is dropped as non-API.
_ROUTER_PREFIX = re.compile(r'APIRouter\(\s*prefix\s*=\s*["\'](?P<prefix>[^"\']*)')

#: Any "/api/..." literal in the frontend. Placeholders become "X".
_FRONTEND_CALL = re.compile(r"""['"`](/api/[A-Za-z0-9_\-/{}$.]*)""")


def frontend_api_paths() -> "dict[str, set]":
    """Every /api/ path the UI references → the files using it. Catches a tab
    going 403 that a complete route table would not."""
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


def source_files() -> List[Path]:
    """Every file that may register a route. modules/*_api.py registers ~100
    directly on `app`, so routes/ alone is not the surface."""
    files = sorted(REPO.glob("modules/**/*.py"))
    files += sorted((REPO / "routes").glob("*.py"))
    files += sorted((REPO / "core").glob("*.py"))
    files += sorted((REPO / "handlers").glob("*.py"))
    files.append(REPO / "main.py")
    return [f for f in files if "__pycache__" not in str(f)]


def registered_routes() -> List[Tuple[str, str, str]]:
    """Every (method, path, file) the app registers, read from source so the
    test needs no venv. Pinned counts in test_scope_coverage stop a change in
    registration style shrinking this silently."""
    out: List[Tuple[str, str, str]] = []
    for f in source_files():
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
