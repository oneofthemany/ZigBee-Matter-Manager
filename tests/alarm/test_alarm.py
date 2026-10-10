"""
The alarm panel (modules/alarm.py): arming, delays, zones, bypass, PINs,
sirens, alerts, the automation device and restarts. Fake clock, fake devices,
real state machine.
"""

from __future__ import annotations

import asyncio
import stat
import tempfile
from pathlib import Path

from harness import Checker

from modules import alarm as A


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Dev:
    def __init__(self, name, **state):
        self.friendly_name = name
        self.state = dict(state)
        self.sent = []
        self.online = True

    def is_available(self):
        return self.online

    async def send_command(self, command, value=None, endpoint_id=None):
        self.sent.append(command)
        return {"success": True}


class Rig:
    def __init__(self, tmp, **cfg):
        self.clock = Clock()
        self.devs = {"front": Dev("Front door", contact=True), "back": Dev("Back door", contact=True),
                     "pir": Dev("Hall PIR", occupancy=False), "siren": Dev("Siren", state="OFF")}
        self.alerts, self.events, self.evals, self.modes = [], [], [], []

        async def notify(users, payload):
            self.alerts.append((users, payload))

        async def broadcast(t, p):
            self.events.append((t, p["state"]))

        async def evaluate(ieee, changed):
            self.evals.append((ieee, changed["state"]))

        self.panel = A.AlarmPanel(
            get_devices=lambda: self.devs,
            get_names=lambda: {k: d.friendly_name for k, d in self.devs.items()},
            notify=notify, broadcast=broadcast, evaluate=evaluate,
            get_users=lambda: ["alex", "sam"], path=Path(tmp) / "alarm.json", clock=self.clock)

        async def mode_hook(mode, source):
            self.modes.append(mode)
        self.panel.set_mode_hook(mode_hook)
        self.panel.config = A.normalise_config({
            "zones": [{"ieee": "front", "entry": True, "modes": ["home", "away", "night"]},
                      {"ieee": "back", "entry": False, "modes": ["home", "away", "night"]},
                      {"ieee": "pir", "entry": False, "modes": ["away"]}],
            "sirens": ["siren"], "exit_delay_s": {"away": 60, "home": 0, "night": 0},
            "entry_delay_s": 30, "siren_minutes": 3, **cfg}, self.panel.config)

    async def settle(self):
        # Saves run in a thread, so give real time, not just loop turns.
        for _ in range(10):
            await asyncio.sleep(0.01)

    async def later(self, seconds):
        self.clock.t += seconds
        await self.panel.tick()
        await self.settle()

    async def sensor(self, ieee, **changed):
        self.devs[ieee].state.update(changed)
        self.panel.observe(ieee, changed)
        await self.settle()


def run() -> Checker:
    c = Checker("alarm")

    async def arming():
        c.section("arming")
        with tempfile.TemporaryDirectory() as tmp:
            r = Rig(tmp)
            p = r.panel
            res = await p.arm("away", "alex", None)
            c.check("arming away starts the exit delay", p.state == A.ARMING and res["success"], p.state)
            await r.sensor("pir", occupancy=True)
            await r.sensor("front", contact=False)
            c.check("walking out during the exit delay trips nothing", p.state == A.ARMING, p.state)
            await r.sensor("front", contact=True)
            await r.later(61)
            c.check("the exit delay ends armed away", p.state == "armed_away", p.state)
            c.check("arming sets the house mode", r.modes == ["away"], r.modes)
            c.check("each change reaches the websocket and the rule engine",
                    ("alarm_state", "armed_away") in r.events and (A.IEEE, "armed_away") in r.evals,
                    (r.events, r.evals))

            r.devs["back"].state["contact"] = False
            p.state, p.armed_mode = A.DISARMED, None
            res = await p.arm("night", "alex", None)
            c.check("arming with a door open is refused and names it",
                    not res["success"] and res["open"] == [{"ieee": "back", "name": "Back door"}], res)
            res = await p.arm("night", "alex", None, force=True)
            c.check("arming anyway bypasses the open door", res["success"] and p.state == "armed_night"
                    and p.bypassed == ["back"], (p.state, p.bypassed))
            await r.sensor("back", contact=False)
            c.check("a bypassed door doesn't trip", p.state == "armed_night", p.state)
            await r.sensor("back", contact=True)
            c.check("once it closes it is watched again", p.bypassed == [], p.bypassed)
            await r.sensor("back", contact=False)
            c.check("and then trips instantly", p.state == A.TRIGGERED, p.state)

    async def triggering():
        c.section("entry delay and triggering")
        with tempfile.TemporaryDirectory() as tmp:
            r = Rig(tmp)
            p = r.panel
            await p.arm("home", "alex", None)
            c.check("arming home with no exit delay arms at once", p.state == "armed_home", p.state)
            await r.sensor("pir", occupancy=True)
            c.check("a sensor not in the armed mode's zones is ignored", p.state == "armed_home", p.state)
            await r.sensor("front", contact=False)
            c.check("an entry door starts the entry delay", p.state == A.PENDING
                    and p.cause["name"] == "Front door" and p.cause["event"] == "opened", (p.state, p.cause))
            c.check("nothing sounds during the entry delay", not r.devs["siren"].sent and not r.alerts)
            await r.later(31)
            c.check("an entry delay that runs out triggers", p.state == A.TRIGGERED, p.state)
            c.check("sirens go on", r.devs["siren"].sent == ["on"], r.devs["siren"].sent)
            c.check("everyone is alerted, urgently, with what tripped",
                    len(r.alerts) == 1 and r.alerts[0][0] == ["alex", "sam"] and r.alerts[0][1]["urgent"]
                    and "Front door opened" in r.alerts[0][1]["body"], r.alerts)
            res = await p.arm("away", "alex", None)
            c.check("you can't re-arm over a sounding alarm", not res["success"], res)
            await r.later(181)
            c.check("after the siren time it falls quiet and stays armed",
                    p.state == "armed_home" and r.devs["siren"].sent == ["on", "off"], (p.state, r.devs["siren"].sent))

            await r.sensor("back", contact=False)
            c.check("an instant door triggers with no delay", p.state == A.TRIGGERED, p.state)

        with tempfile.TemporaryDirectory() as tmp:
            r = Rig(tmp, notify_users=["sam"])
            await r.panel.arm("home", "alex", None)
            await r.sensor("back", contact=False)
            c.check("alerts go only to the chosen people when set", r.alerts[0][0] == ["sam"], r.alerts)

    async def pins():
        c.section("disarming and PINs")
        with tempfile.TemporaryDirectory() as tmp:
            r = Rig(tmp)
            p = r.panel
            await p.arm("home", "alex", None)
            res = await p.disarm("alex", "1234")
            c.check("disarming without a PIN set says to set one", not res["success"] and "PIN" in res["error"], res)
            try:
                await p.set_pin("alex", "12")
                c.check("a too-short PIN is refused", False)
            except ValueError:
                c.check("a too-short PIN is refused", True)
            await p.set_pin("alex", "2468")
            f = Path(tmp) / "alarm.json"
            c.check("the PIN is stored hashed, in a 0600 file",
                    "2468" not in f.read_text() and stat.S_IMODE(f.stat().st_mode) == 0o600)
            try:
                await p.set_pin("alex", "1357", current="0000")
                c.check("changing a PIN needs the current one", False)
            except PermissionError:
                c.check("changing a PIN needs the current one", True)
            res = await p.disarm("sam", "2468")
            c.check("someone else's PIN doesn't disarm", not res["success"] and p.state == "armed_home", res)
            res = await p.disarm("alex", "0000")
            c.check("a wrong PIN doesn't disarm", not res["success"] and p.state == "armed_home", res)

            await r.sensor("back", contact=False)
            res = await p.disarm("alex", "2468")
            await r.settle()
            c.check("the right PIN disarms a sounding alarm",
                    res["success"] and p.state == A.DISARMED, (res, p.state))
            c.check("disarming silences the sirens and returns the house to home",
                    r.devs["siren"].sent[-1] == "off" and r.modes[-1] == "home", (r.devs["siren"].sent, r.modes))
            c.check("and tells everyone who disarmed it",
                    r.alerts[-1][1]["title"] == "Alarm disarmed" and "alex" in r.alerts[-1][1]["body"], r.alerts[-1])

            await p.arm("home", "alex", None)
            for _ in range(A.MAX_PIN_FAILURES):
                await p.disarm("alex", "0000")
            res = await p.disarm("alex", "2468")
            c.check("after five wrong PINs even the right one is locked out",
                    not res["success"] and "Too many" in res["error"], res)
            r.clock.t += A.LOCKOUT_S + 1
            res = await p.disarm("alex", "2468")
            c.check("the lockout ends", res["success"], res)

            c.check("an admin clearing a PIN removes it", await p.clear_pin("alex") and not p.has_pin("alex"))

        with tempfile.TemporaryDirectory() as tmp:
            r = Rig(tmp, arm_requires_pin=True)
            res = await r.panel.arm("away", "alex", None)
            c.check("with arm-needs-PIN on, arming without one is refused", not res["success"], res)

    async def automation():
        c.section("automations and house mode")
        with tempfile.TemporaryDirectory() as tmp:
            r = Rig(tmp)
            p, dev = r.panel, r.panel.device
            res = await dev.send_command("set", "armed_night")
            c.check("a rule can arm through the alarm device", res["success"] and p.state == "armed_night", res)
            c.check("the device reports its state to rules",
                    dev.state["state"] == "armed_night" and dev.state["armed"] == 1, dev.state)
            res = await dev.send_command("set", "disarmed")
            c.check("a rule can't disarm by default", not res["success"] and p.state == "armed_night", res)
            c.check("and the rule builder isn't offered disarm",
                    "disarmed" not in dev.get_control_commands()[0]["options"])
            p.update_config({"allow_automation_disarm": True})
            res = await dev.send_command("set", "disarmed")
            c.check("unless the admin allowed it", res["success"] and p.state == A.DISARMED, res)

            p.update_config({"follow_house_mode": True})
            p.on_house_mode("home")
            await r.settle()
            c.check("house mode home doesn't arm", p.state == A.DISARMED, p.state)
            p.on_house_mode("Holiday")
            await r.settle()
            c.check("house mode holiday arms away", p.state == A.ARMING and p.armed_mode == "away", p.state)
            r.modes.clear()
            p.on_house_mode("home")
            await r.settle()
            c.check("and a house mode change never disarms", p.state == A.ARMING, p.state)
            c.check("an arm caused by the house mode doesn't set it back", r.modes == [], r.modes)

            p.state, p.armed_mode = A.DISARMED, None
            r.devs["back"].state["contact"] = False
            p.on_house_mode("night")
            await r.settle()
            c.check("if a house-mode arm is blocked by an open door, everyone is told why",
                    p.state == A.DISARMED and r.alerts[-1][1]["title"] == "Alarm not armed"
                    and "Back door" in r.alerts[-1][1]["body"], r.alerts[-1:])

    async def restart():
        c.section("restarts and status")
        with tempfile.TemporaryDirectory() as tmp:
            r = Rig(tmp)
            await r.panel.arm("away", "alex", None)
            await r.settle()
            r2 = Rig(tmp)
            r2.panel.load()
            c.check("an arming alarm is still arming after a restart",
                    r2.panel.state == A.ARMING and r2.panel.armed_mode == "away", r2.panel.state)
            r2.clock.t = r.clock.t + 120
            await r2.panel.tick()
            c.check("and a delay that ran out while the hub was down has ended", r2.panel.state == "armed_away")
            r2.devs["pir"].online = False
            st = r2.panel.status()
            pir = next(z for z in st["zones"] if z["ieee"] == "pir")
            c.check("status names zones and flags offline ones", pir["name"] == "Hall PIR" and not pir["online"], pir)
            try:
                r2.panel.update_config({"entry_delay_s": 9999})
                c.check("an out-of-range delay is refused", False)
            except ValueError:
                c.check("an out-of-range delay is refused", True)
            cfg = A.normalise_config({"zones": [{"ieee": "x", "modes": ["away", "bogus"]},
                                                {"ieee": "x", "modes": ["home"]}, {"ieee": A.IEEE}]},
                                     r2.panel.config)
            c.check("zones drop unknown modes, duplicates and the panel itself",
                    cfg["zones"] == [{"ieee": "x", "entry": False, "modes": ["away"]}], cfg["zones"])

    for scenario in (arming, triggering, pins, automation, restart):
        asyncio.run(scenario())
    return c
