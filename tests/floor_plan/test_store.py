"""
Floor-plan store tests — one plan, one file, every reader behind it.

    python3 tests/floor_plan/test_store.py

A hub that kept its plan at heating.floor_plan in config.yaml must come up with
the same plan read from data/floor_plan.json, and nothing may still read the
old key: a save has to be what heating, chambers and frames see next.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import REPO, Checker, sample_plan  # noqa: E402

from modules import chambers, floor_plan_store as store  # noqa: E402
from modules.floor_plan import clean_floor_plan  # noqa: E402


def _paths():
    d = tempfile.mkdtemp()
    return os.path.join(d, "data", "floor_plan.json"), os.path.join(d, "config.yaml")


def _write_config(path, cfg):
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f)


def run() -> Checker:
    c = Checker("test_store")
    plan = clean_floor_plan(sample_plan())

    c.section("a plan kept in config.yaml moves to data/floor_plan.json once")
    plan_path, cfg_path = _paths()
    _write_config(cfg_path, {"heating": {"floor_plan": plan, "enabled": True}})
    store.reset(plan_path, cfg_path)
    c.check("it is read", store.load_plan() == plan)
    c.check("and written to its own file", os.path.exists(plan_path)
            and json.load(open(plan_path)) == plan)
    c.check("config.yaml is not rewritten by the move",
            yaml.safe_load(open(cfg_path))["heating"]["floor_plan"] == plan)
    _write_config(cfg_path, {"heating": {"floor_plan": sample_plan("Stale")}})
    store.reset()
    c.check("once moved, the old key is never read again",
            store.load_plan()["levels"][0]["rooms"][0]["name"] == "Lounge")

    c.section("no plan anywhere")
    plan_path, cfg_path = _paths()
    _write_config(cfg_path, {"heating": {"enabled": True}})
    store.reset(plan_path, cfg_path)
    c.check("reads as None", store.load_plan() is None)
    c.check("and writes nothing", not os.path.exists(plan_path))
    store.reset(plan_path, os.path.join(os.path.dirname(cfg_path), "missing.yaml"))
    c.check("a missing config.yaml is also no plan", store.load_plan() is None)

    c.section("saving and deleting")
    store.save_plan(plan)
    c.check("a save is what the next read returns", store.load_plan() == plan)
    got = store.load_plan()
    got["levels"][0]["rooms"][0]["name"] = "Mutated"
    c.check("a caller's copy cannot change the plan",
            store.load_plan()["levels"][0]["rooms"][0]["name"] == "Lounge")
    store.reset()
    c.check("and it survives a restart", store.load_plan() == plan)
    store.delete_plan()
    c.check("delete removes the file", not os.path.exists(plan_path))
    c.check("and the plan", store.load_plan() is None)

    c.section("an unreadable file is not migrated over")
    plan_path, cfg_path = _paths()
    os.makedirs(os.path.dirname(plan_path))
    open(plan_path, "w").write("{not json")
    _write_config(cfg_path, {"heating": {"floor_plan": plan}})
    store.reset(plan_path, cfg_path)
    c.check("reads as None", store.load_plan() is None)
    c.check("the damaged file is left for a person to look at",
            open(plan_path).read() == "{not json")

    c.section("dropping the old key from a config about to be written")
    cfg = {"heating": {"floor_plan": plan, "controller": {}}}
    c.check("drops it", store.drop_legacy_key(cfg) and "floor_plan" not in cfg["heating"])
    c.check("keeps the rest", cfg["heating"] == {"controller": {}})
    c.check("says when there was nothing to drop", store.drop_legacy_key(cfg) is False)
    c.check("copes with no heating block", store.drop_legacy_key({}) is False)

    c.section("chambers read the same plan")
    plan_path, cfg_path = _paths()
    store.reset(plan_path, cfg_path)
    store.save_plan(plan)
    reg = chambers.build_registry({"heating": {}})
    c.check("a plan room is adopted as a chamber",
            any(r["id"] == "lounge" and r["source"] == "floor_plan" for r in reg), reg)
    c.check("with its level", chambers.levels() == [{"id": "ground", "name": "Ground",
                                                     "index": 0}], chambers.levels())
    c.check("a key left in config is ignored",
            not any(r["id"] == "attic" for r in chambers.build_registry(
                {"heating": {"floor_plan": {"levels": [{"id": "up", "rooms": [
                    {"id": "attic", "name": "Attic"}]}]}}})))
    c.check("a plan can still be passed in",
            chambers.floor_plan_rooms({"levels": []}) == [])
    ok, err = chambers.delete_chamber({"chambers": [{"id": "lounge", "name": "Lounge"}]},
                                      "lounge")
    c.check("an adopted plan room cannot be deleted", not ok and "floor plan" in (err or ""),
            (ok, err))

    c.section("nothing reads heating.floor_plan any more")
    offenders = []
    for folder in ("modules", "routes"):
        for path in (REPO / folder).rglob("*.py"):
            if path.name in ("floor_plan_store.py",):
                continue
            text = path.read_text()
            if 'get("floor_plan")' in text or '["floor_plan"]' in text:
                offenders.append(str(path.relative_to(REPO)))
    c.check("no module or route reads the old key", not offenders, offenders)

    store.reset(os.path.join(tempfile.mkdtemp(), "x.json"))
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
