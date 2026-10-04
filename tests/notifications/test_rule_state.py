"""Cooldowns and last firings survive a hub restart."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import modules.notification_rules as nr
from harness import Checker, FakeDevice, Rig


def _restart(rig: Rig, tmp: Path, state: Path) -> Rig:
    """A new engine over the same rules and state file, as after a hub restart."""
    fresh = Rig(tmp, {"aa": FakeDevice("Hall Sensor", {"occupancy": False})},
                state_path=state, store=rig.store)
    fresh.now = rig.now
    return fresh


def run() -> Checker:
    c = Checker("rule_state")
    saved_delay = nr.STATE_SAVE_DELAY_S
    nr.STATE_SAVE_DELAY_S = 0          # the debounce is about bursts, not correctness
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp, state = Path(tmp), Path(tmp) / "state.json"
            c.section("across a restart")
            rig = Rig(tmp, {"aa": FakeDevice("Hall Sensor", {"occupancy": False})}, state_path=state)
            r = rig.rule(cooldownMinutes=15)
            rig.change("aa", occupancy=True)
            c.check("a firing is written to the state file", state.exists(), list(tmp.iterdir()))

            rig = _restart(rig, tmp, state)
            c.check("'last fired' is still known after a restart",
                    (rig.engine.last_fired(r["id"]) or {}).get("device") == "Hall Sensor", rig.engine.last_fired(r["id"]))
            rig.change("aa", occupancy=False)
            rig.now += 5 * 60
            c.check("the cooldown still holds after a restart", rig.change("aa", occupancy=True) == [])
            rig.change("aa", occupancy=False)
            rig.now += 11 * 60
            c.check("…and lets the next one through once it has run out", len(rig.change("aa", occupancy=True)) == 1)

            c.section("pruning")
            other = rig.rule(trigger="motion_cleared")
            rig.change("aa", occupancy=False)
            rig.store.delete("alice", other["id"])
            rig.now += 16 * 60                          # past the first rule's cooldown…
            rig.change("aa", occupancy=True)            # …so this fires, and any firing saves
            saved = json.loads(state.read_text())
            c.check("a deleted rule's entries are dropped from the file",
                    other["id"] not in saved["last"] and not any(k.startswith(other["id"]) for k in saved["fired_at"]), saved)
            rig.now += 2 * 60 * 60
            rig.change("aa", occupancy=False)
            rig.change("aa", occupancy=True)
            saved = json.loads(state.read_text())
            c.check("cooldowns that ran out long ago aren't kept",
                    all(v >= rig.now - 60 * 60 for v in saved["fired_at"].values()), saved["fired_at"])

        with tempfile.TemporaryDirectory() as tmp:
            tmp, state = Path(tmp), Path(tmp) / "state.json"
            c.section("robustness")
            state.write_text("{oops")
            rig = Rig(tmp, {"aa": FakeDevice("Hall Sensor", {"occupancy": False})}, state_path=state)
            rig.rule()
            c.check("an unreadable state file starts empty instead of failing", len(rig.change("aa", occupancy=True)) == 1)
            plain = Rig(tmp / "x", {"aa": FakeDevice("Hall Sensor", {"occupancy": False})}) if (tmp / "x").mkdir() is None else None
            plain.rule()
            plain.change("aa", occupancy=True)
            c.check("without a state path nothing is written", not any((tmp / "x").glob("*state*")))
    finally:
        nr.STATE_SAVE_DELAY_S = saved_delay
    return c
