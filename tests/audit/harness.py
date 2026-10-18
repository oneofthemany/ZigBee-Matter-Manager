"""Shared scaffolding for the audit-tool tests — plain scripts, as tests/daylight."""

from __future__ import annotations

import shutil
import sys
import tempfile
import textwrap
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AUDIT = REPO / "scripts" / "audit"
if str(AUDIT) not in sys.path:
    sys.path.insert(0, str(AUDIT))


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


@contextmanager
def tree(files: dict[str, str]):
    """A throwaway repo: {relative path: source}."""
    root = Path(tempfile.mkdtemp(prefix="zmm_audit_"))
    try:
        for rel, src in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(src))
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
