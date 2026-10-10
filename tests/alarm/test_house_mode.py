"""
House mode (modules/house_mode.py) over a real WorkerManager: designating the
worker, setting it, and following presence.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import types
from pathlib import Path

from harness import Checker

from modules.house_mode import HouseMode
from modules.workers import WorkerManager


class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


def _presence(home=0, away=0, unknown=0):
    return types.SimpleNamespace(household=types.SimpleNamespace(state={
        "home_count": home, "away_count": away, "unknown_count": unknown,
        "total": home + away + unknown}))


def run() -> Checker:
    c = Checker("house_mode")

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            wm = WorkerManager(data_file=os.path.join(tmp, "workers.json"))
            presence = {"p": _presence(home=1, away=1)}
            clock = Clock()
            h = HouseMode(lambda: wm, lambda: presence["p"], path=Path(tmp) / "hm.json", clock=clock)

            c.section("designation")
            c.check("no house mode until one is set up", h.status()["configured"] is False)
            try:
                await h.set("away")
                c.check("setting with none set up is refused", False)
            except ValueError:
                c.check("setting with none set up is refused", True)
            st = h.ensure_worker()
            c.check("create makes the house_mode worker the swarm already looks for",
                    st["configured"] and st["worker"] == "house_mode"
                    and st["options"] == ["home", "away", "night", "holiday"] and st["mode"] == "home", st)
            c.check("calling it again adopts the existing worker", h.ensure_worker()["worker"] == "house_mode")
            wm.create({"id": "flag", "name": "Flag", "type": "boolean"})
            try:
                h.update_config({"worker": "flag"})
                c.check("only a Mode worker can be the house mode", False)
            except ValueError:
                c.check("only a Mode worker can be the house mode", True)

            st = await h.set("NIGHT")
            c.check("a mode is matched case-insensitively and set on the worker",
                    st["mode"] == "night" and wm.get("house_mode").state["value"] == "night", st)
            try:
                await h.set("party")
                c.check("a mode the worker lacks is refused", False)
            except ValueError:
                c.check("a mode the worker lacks is refused", True)
            h2 = HouseMode(lambda: wm, lambda: presence["p"], path=Path(tmp) / "hm.json")
            c.check("the designation survives a restart", h2.status()["worker"] == "house_mode")

            c.section("following presence")
            await h.set("home")
            presence["p"] = _presence(away=2)
            await h.check_presence()
            c.check("off by default: everyone leaving changes nothing", h.current() == "home")
            h.update_config({"follow_presence": True, "away_after_minutes": 10})
            await h.check_presence()
            c.check("everyone out starts the wait, without changing yet", h.current() == "home")
            clock.t += 9 * 60
            await h.check_presence()
            c.check("still home before the wait is up", h.current() == "home")
            clock.t += 61
            await h.check_presence()
            c.check("away once everyone has been out long enough", h.current() == "away")

            presence["p"] = _presence(home=1, away=1)
            await h.check_presence()
            c.check("someone arriving makes it home", h.current() == "home")

            presence["p"] = _presence(away=1, unknown=1)
            clock.t += 3600
            await h.check_presence()
            clock.t += 3600
            await h.check_presence()
            c.check("an unknown phone is not a departure", h.current() == "home")

            await h.set("night")
            presence["p"] = _presence(away=2)
            await h.check_presence()
            clock.t += 3600
            await h.check_presence()
            c.check("leaving doesn't override night, which someone chose", h.current() == "night")

            await h.set("holiday")
            presence["p"] = _presence(home=1)
            await h.check_presence()
            c.check("coming back ends holiday", h.current() == "home")

            presence["p"] = _presence(away=2)
            h.update_config({"away_after_minutes": 0})
            h.observe("user::sam", {"presence": "away"})
            for _ in range(5):
                await asyncio.sleep(0)
            c.check("a presence change through the engine listener acts on its own", h.current() == "away")
            await h.set("home")
            h.observe("0x00124b0001", {"presence": "away"})
            for _ in range(5):
                await asyncio.sleep(0)
            c.check("a sensor's own 'presence' attribute isn't mistaken for a person",
                    h.current() == "home")

    asyncio.run(scenario())
    return c
