"""
WiiM device panel: the speaker's own settings, read and driven over the WiiM
HTTP API (v1.2 §2.1–2.10), with the inputs a box has decoded from
``plm_support`` as python-linkplay does.

The fixtures are a WiiM Ultra's real replies, captured on HDMI-ARC over
Ethernet; the provider's ``_command`` is stubbed, so every command string that
would reach the box is asserted exactly. The automation step and the route are
checked by reading, as neither module imports without the rest of the stack.
"""

from __future__ import annotations

import ast
import asyncio
import json

from harness import Checker, REPO

from modules.media import linkplay as lp
from modules.media.players.wiim import WiiMPlayerProvider

IP = "192.168.1.102"
PID = f"wiim:{IP}"

ULTRA = {
    "getStatusEx": json.dumps({
        "DeviceName": "WiiM Ultra", "project": "WiiM_Ultra",
        "firmware": "Linkplay.5.2.828047", "Release": "20260901",
        "MAC": "9C:B8:B4:23:61:62", "eth0": IP, "essid": "", "RSSI": "0",
        "internet": "1", "VersionUpdate": "0", "NewVer": "0",
        "plm_support": "0x2b10416", "preset_key": "12"}),
    "getPlayerStatus": json.dumps({
        "mode": "49", "loop": "4", "status": "stop", "curpos": "875809",
        "totlen": "0", "vol": "40", "mute": "0"}),
    "getMetaInfo": json.dumps({"metaData": {
        "album": "unknow", "title": "unknow", "artist": "unknow",
        "albumArtURI": "unknow", "sampleRate": "48000", "bitDepth": "16",
        "bitRate": "1536"}}),
    "getPresetInfo": json.dumps({"preset_num": 1, "preset_list": [
        {"number": 3, "name": "Radio Paradise", "source": "RadioParadise",
         "url": "http://stream.radioparadise.com/flacm", "picurl": "http://x/p.png"}]}),
    "getNewAudioOutputHardwareMode": json.dumps(
        {"hardware": "1", "source": "0", "audiocast": "0"}),
    "getShutdown": "0",
}


def _wiim(replies: dict, sent: list) -> WiiMPlayerProvider:
    w = WiiMPlayerProvider([IP])

    async def command(ip, cmd):
        sent.append(cmd)
        if cmd in replies:
            return replies[cmd]
        return replies.get("*", "OK")
    w._command = command
    return w


def _decode(c: Checker) -> None:
    c.section("inputs from plm_support")
    ids = [i["id"] for i in lp.supported_inputs("0x2b10416")]
    c.check("a WiiM Ultra has network, line-in, BT, optical, HDMI, phono",
            ids == ["wifi", "line-in", "bluetooth", "optical", "HDMI", "phono"],
            ids)
    c.check("an unreadable mask still offers network + line-in + BT",
            [i["id"] for i in lp.supported_inputs(None)]
            == ["wifi", "line-in", "bluetooth"])
    c.check("HDMI-ARC (49) is the HDMI input", lp.input_id(49) == "HDMI")
    c.check("a network mode is the network input", lp.input_id(10) == "wifi")
    c.check("an unknown physical input is not claimed", lp.input_id(55) == "")


def _panel(c: Checker) -> None:
    c.section("panel from a WiiM Ultra's real replies")

    async def go():
        p = await _wiim(ULTRA, []).device_panel(PID)
        d = p["device"]
        c.check("identity", d["model"] == "WiiM Ultra"
                and d["firmware"] == "Linkplay.5.2.828047"
                and d["network"] == "Ethernet" and d["internet"], d)
        c.check("on HDMI-ARC, named as such",
                p["input"]["current"] == "HDMI"
                and p["input"]["owner"] == "HDMI-ARC", p["input"])
        c.check("placeholder metadata is blanked, format kept",
                p["audio"]["title"] == "" and p["audio"]["sample_rate"] == "48000")
        c.check("a live stream cannot be seeked", not p["playback"]["can_seek"])
        c.check("an undocumented loop value is passed through, not guessed",
                p["playback"]["loop"] == 4)
        c.check("presets", p["presets"]["slots"] == 12
                and p["presets"]["items"][0]["name"] == "Radio Paradise")
        c.check("output", p["output"]["current"] == 1)
        c.check("a bare-number getShutdown is read", p["sleep"]["seconds"] == 0)

        old = {**ULTRA, "getPresetInfo": "unknown command",
               "getNewAudioOutputHardwareMode": "unknown command",
               "getShutdown": "unknown command"}
        p = await _wiim(old, []).device_panel(PID)
        c.check("older firmware: those sections are left out, not failed",
                "presets" not in p and "output" not in p and "sleep" not in p)
        dead = await _raises(_wiim({"*": None, **{k: None for k in ULTRA}},
                                   []).device_panel(PID))
        c.check("a box that answers nothing is an error", dead)
        c.check("an unknown WiiM is refused",
                await _raises(_wiim(ULTRA, []).device_panel("wiim:10.9.9.9")))
    asyncio.run(go())


async def _raises(coro) -> bool:
    try:
        await coro
    except (ValueError, RuntimeError):
        return True
    return False


def _actions(c: Checker) -> None:
    c.section("actions reach the box as documented commands, and only valid ones")

    async def go():
        sent = []
        w = _wiim(ULTRA, sent)
        ok = [("input", "HDMI", "setPlayerCmd:switchmode:HDMI"),
              ("input", "wifi", "setPlayerCmd:switchmode:wifi"),
              ("preset", 3, "MCUKeyShortClick:3"),
              ("loop", -1, "setPlayerCmd:loopmode:-1"),
              ("seek", 90.7, "setPlayerCmd:seek:90"),
              ("output", 2, "setAudioOutputHardwareMode:2"),
              ("sleep", 1800, "setShutdown:1800"),
              ("sleep", -1, "setShutdown:-1"),
              ("toggle", None, "setPlayerCmd:onepause"),
              ("reboot", None, "reboot")]
        for action, value, cmd in ok:
            sent.clear()
            await w.device_action(PID, action, value)
            c.check(f"{action} {value} → {cmd}", sent[-1] == cmd, sent)
        bad = [("input", "co-axial"), ("input", "HDMI; reboot"),
               ("preset", 13), ("loop", 7), ("output", 4),
               ("sleep", 0), ("sleep", 999999), ("dance", None)]
        for action, value in bad:
            sent.clear()
            refused = await _raises(w.device_action(PID, action, value))
            c.check(f"{action} {value!r} refused before reaching the box",
                    refused and not any(s.startswith(("setPlayer", "MCU", "setA",
                                                      "setS", "reboot"))
                                        for s in sent), sent)
        w2 = _wiim({**ULTRA, "*": "Failed"}, [])
        c.check("a 'Failed' reply is an error",
                await _raises(w2.device_action(PID, "preset", 1)))
    asyncio.run(go())


def _source(c: Checker) -> None:
    c.section("wiring (read from source)")
    auto = (REPO / "modules" / "automation.py").read_text()
    routes = (REPO / "routes" / "media_routes.py").read_text()
    ast.parse(auto)
    ast.parse(routes)
    c.check("automation accepts a device step", '"device")' in auto
            and "device needs input, preset, sleep, output or loop" in auto)
    c.check("…refused on a zone",
            "device controls need a speaker, not a zone" in auto)
    c.check("…and runs it through the controller",
            "svc.controller.device_action(player_id, da, dv)" in auto)
    c.check("the players route advertises the panel",
            '"device_panel": bool(getattr(p, "has_device_panel", False))' in routes)
    c.check("the panel route carries the zone lock",
            '"zone": _zone_policy(svc, player_id)' in routes)


def run() -> Checker:
    c = Checker("wiim_panel")
    _decode(c)
    _panel(c)
    _actions(c)
    _source(c)
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
