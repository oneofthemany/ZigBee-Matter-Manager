"""Shared scaffolding for the heating tests — plain scripts, as tests/floor_plan."""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# App modules default their data dir to ./data; a test must never touch it.
if "ZMM_DATA_DIR" not in os.environ:
    os.environ["ZMM_DATA_DIR"] = tempfile.mkdtemp(prefix="zmm_test_data_")
    atexit.register(shutil.rmtree, os.environ["ZMM_DATA_DIR"], True)

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


def two_room_plan() -> dict:
    """Lounge (0..5) and hall (5..8) on one level, an internal door between
    them, a south window in the lounge and one radiator each. North offset 0:
    the y=0 wall faces south."""
    return {
        "north_offset_deg": 0.0,
        "levels": [{
            "id": "ground", "name": "Ground", "index": 0, "ceiling_height_m": 2.4,
            "rooms": [
                {"id": "lounge", "name": "Lounge", "circuit_id": "down",
                 "polygon": [[0, 0], [5, 0], [5, 4], [0, 4]]},
                {"id": "hall", "name": "Hall", "circuit_id": "down",
                 "polygon": [[5, 0], [8, 0], [8, 4], [5, 4]]},
            ],
            "walls": [
                {"id": "ws", "x1": 0, "y1": 0, "x2": 8, "y2": 0, "type": "external"},
                {"id": "wn", "x1": 8, "y1": 4, "x2": 0, "y2": 4, "type": "external"},
                {"id": "ww", "x1": 0, "y1": 4, "x2": 0, "y2": 0, "type": "external"},
                {"id": "we", "x1": 8, "y1": 0, "x2": 8, "y2": 4, "type": "external"},
                {"id": "wi", "x1": 5, "y1": 0, "x2": 5, "y2": 4, "type": "internal"},
            ],
            "openings": [
                {"id": "win1", "wall_id": "ws", "kind": "window", "offset_m": 1.0,
                 "width_m": 1.5, "height_m": 1.2, "glazing": "double"},
                {"id": "door1", "wall_id": "wi", "kind": "door", "offset_m": 1.0,
                 "width_m": 0.9, "height_m": 2.0, "door_type": "internal"},
            ],
            "radiators": [
                {"id": "rad_l", "room_id": "lounge", "watts_at_dt50": 1500},
                {"id": "rad_h", "room_id": "hall", "watts_at_dt50": 800},
            ],
            "sensors": [
                {"id": "sen_l", "room_id": "lounge", "ieee": "aa:00:00:00:00:00:00:01",
                 "height_m": 1.5, "primary": True},
            ],
            "contacts": [
                {"id": "con_d", "opening_id": "door1", "ieee": "cc:00:00:00:00:00:00:01",
                 "debounce_open_seconds": 30, "require_temp_drop_c": 0.5,
                 "max_close_minutes": 60, "enabled": True},
            ],
        }],
        "circuits": [{"id": "down", "name": "Downstairs",
                      "receiver_ieee": "rr:00:00:00:00:00:00:01",
                      "receiver_command": "thermostat"}],
    }
