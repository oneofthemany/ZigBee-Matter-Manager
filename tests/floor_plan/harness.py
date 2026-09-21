"""Shared scaffolding for the floor-plan tests — plain scripts, as tests/swarm."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


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


def sample_plan(room_name: str = "Lounge") -> dict:
    """One 5×4 m room with a south window — the floor_plan.py self-test house."""
    return {
        "north_offset_deg": 0.0,
        "levels": [{
            "id": "ground", "name": "Ground", "index": 0, "ceiling_height_m": 2.4,
            "rooms": [{"id": "lounge", "name": room_name,
                       "polygon": [[0, 0], [5, 0], [5, 4], [0, 4]]}],
            "walls": [
                {"id": "ws", "x1": 0, "y1": 0, "x2": 5, "y2": 0, "type": "external"},
                {"id": "we", "x1": 5, "y1": 0, "x2": 5, "y2": 4, "type": "external"},
                {"id": "wn", "x1": 5, "y1": 4, "x2": 0, "y2": 4, "type": "external"},
                {"id": "ww", "x1": 0, "y1": 4, "x2": 0, "y2": 0, "type": "external"},
            ],
            "openings": [{"id": "win1", "wall_id": "ws", "kind": "window",
                          "offset_m": 1.0, "width_m": 1.4, "height_m": 1.2,
                          "glazing": "double", "room_id": "lounge"}],
        }],
    }
