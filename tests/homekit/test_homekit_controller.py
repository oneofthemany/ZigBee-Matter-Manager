"""
The HomeKit controller.

Two layers:

  * Always: a hand-written HAP /accessories database shaped like a TV (short
    UUIDs, as accessories send them) drives the TV mapping, the write
    translation, the pairing store and the controller's caching against a
    stub pairing — no network and no library.
  * When aiohomekit imports: its own FakeController/FakePairing stand in for
    the TV, so discovery, pair-setup with a code, reading and writing run
    through the real library's model. This catches an aiohomekit upgrade
    changing the pairing flow or the characteristic UUIDs.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

from harness import Checker

from modules import homekit_controller as H


def _run(coro):
    return asyncio.run(coro)


def _c(iid, t, value=None, perms=("pr",)):
    c = {"iid": iid, "type": t, "perms": list(perms)}
    if value is not None:
        c["value"] = value
    return c


def _input(iid, ident, name, kind=3, configured=1, hidden=0):
    return {"iid": iid, "type": "D9", "characteristics": [
        _c(iid + 1, "E6", ident), _c(iid + 2, "E3", name), _c(iid + 3, "DB", kind),
        _c(iid + 4, "D6", configured), _c(iid + 5, "135", hidden)]}


def tv_db(active=1, active_id=2, mute=False):
    return [{"aid": 1, "services": [
        {"iid": 1, "type": "3E", "characteristics": [
            _c(2, "23", "Living room"), _c(3, "20", "Sky"), _c(4, "21", "LT055"),
            _c(5, "30", "SN1"), _c(6, "52", "1.0")]},
        {"iid": 10, "type": "D8", "characteristics": [
            _c(11, "B0", active, ("pr", "pw", "ev")), _c(12, "E7", active_id, ("pr", "pw", "ev")),
            _c(13, "E3", "Sky Glass"), _c(14, "E1", None, ("pw",))]},
        {"iid": 20, "type": "113", "characteristics": [
            _c(21, "11A", mute, ("pr", "pw")), _c(22, "EA", None, ("pw",))]},
        _input(30, 1, "Sky TV", kind=2),
        _input(40, 2, "HDMI 1"),
        _input(50, 3, "Hidden HDMI", hidden=1),
        _input(60, 4, "Unused", configured=0),
    ]}]


class StubPairing:
    def __init__(self, db=None, fail=None):
        self.db, self.fail = db or tv_db(), fail
        self.writes, self.removed, self.closed = [], [], False

    async def list_accessories_and_characteristics(self):
        if self.fail:
            raise self.fail
        return self.db

    async def put_characteristics(self, writes):
        self.writes.extend(writes)
        return {}

    async def remove_pairing(self, pid):
        if self.fail:
            raise self.fail
        self.removed.append(pid)
        return True

    async def close(self):
        self.closed = True


class StubController:
    def __init__(self, pairings):
        self.aliases = dict(pairings)
        self.pairings = {}


def _controller(tmp, pairing, enabled=True):
    ctl = H.HomeKitController({"enabled": enabled}, pairings_file=str(Path(tmp) / "hk.json"))
    ctl._ctl = StubController({"tv": pairing})
    ctl._pairings = {"tv": {"AccessoryPairingID": "TV", "iOSPairingId": "hub"}}
    return ctl


def _mapping(c: Checker) -> None:
    c.section("television mapping")
    tv = H.map_television(tv_db())
    c.check("a Television service is found by its short UUID", tv is not None and tv["aid"] == 1, tv)
    names = [i["name"] for i in tv["inputs"]]
    c.check("shown, configured inputs are listed in identifier order", names == ["Sky TV", "HDMI 1"], names)
    c.check("an input type is named, not numbered", tv["inputs"][0]["type"] == "tuner", tv["inputs"][0])

    s = H.normalise_status("tv", tv)
    c.check("the configured name wins over the accessory name", s["name"] == "Sky Glass", s["name"])
    c.check("Active 1 reads as on", s["power"] is True)
    c.check("the active input is named", s["input_name"] == "HDMI 1", s["input_name"])
    caps = s["capabilities"]
    c.check("write-only remote and volume buttons count as capabilities",
            caps["remote"] and caps["volume_step"] and caps["mute"], caps)
    c.check("an absent absolute volume is not offered", caps["volume_level"] is False, caps)

    off = H.normalise_status("tv", H.map_television(tv_db(active=0)))
    c.check("Active 0 reads as standby", off["power"] is False)

    lamp = [{"aid": 1, "services": [{"iid": 1, "type": "43", "characteristics": [_c(2, "25", 1)]}]}]
    c.check("an accessory with no TV service maps to nothing", H.map_television(lamp) is None)

    c.check("a spaced code is reformatted for HAP", H.format_pin("123 45 678") == "123-45-678")
    c.check("a bare 8-digit code is reformatted for HAP", H.format_pin("12345678") == "123-45-678")
    try:
        H.format_pin("1234")
        c.check("a short code is refused", False)
    except H.HomeKitError:
        c.check("a short code is refused", True)


def _writes(c: Checker) -> None:
    c.section("control translation")
    tv = H.map_television(tv_db())
    c.check("power off writes Active 0", H.build_writes(tv, {"power": False}) == [(1, 11, 0)])
    c.check("input switches by identifier", H.build_writes(tv, {"input": 1}) == [(1, 12, 1)])
    c.check("a remote key writes its HAP code", H.build_writes(tv, {"key": "select"}) == [(1, 14, 8)])
    c.check("volume up is VolumeSelector 0", H.build_writes(tv, {"volume_step": "up"}) == [(1, 22, 0)])
    c.check("volume down is VolumeSelector 1", H.build_writes(tv, {"volume_step": "down"}) == [(1, 22, 1)])
    for label, changes in (("a hidden input is refused", {"input": 3}),
                           ("an unknown key is refused", {"key": "teleport"}),
                           ("an unsupported absolute volume is refused", {"volume": 10}),
                           ("an empty change is refused", {})):
        try:
            H.build_writes(tv, changes)
            c.check(label, False)
        except H.HomeKitError:
            c.check(label, True)


def _store(c: Checker) -> None:
    c.section("pairing store")
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "sub" / "hk.json")
        H.save_pairings({"tv": {"iOSDeviceLTSK": "secret"}}, path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        c.check("the pairing file holding the private key is 0600", mode == 0o600, oct(mode))
        c.check("pairings round-trip", H.load_pairings(path) == {"tv": {"iOSDeviceLTSK": "secret"}})
        c.check("no temp file is left behind", not any(p.suffix == ".tmp" for p in Path(path).parent.iterdir()))
        Path(path).write_text("{not json")
        c.check("a corrupt file reads as no pairings", H.load_pairings(path) == {})
        c.check("a missing file reads as no pairings", H.load_pairings(str(Path(tmp) / "none.json")) == {})


def _controller_behaviour(c: Checker) -> None:
    c.section("controller")
    with tempfile.TemporaryDirectory() as tmp:
        ctl = H.HomeKitController({"enabled": False}, pairings_file=str(Path(tmp) / "hk.json"))
        try:
            _run(ctl.discover())
            c.check("a disabled integration refuses to start", False)
        except H.HomeKitError as e:
            c.check("a disabled integration refuses to start", "disabled" in str(e), str(e))

        saved = sys.modules.get("aiohomekit")
        sys.modules["aiohomekit"] = None
        try:
            _run(H.HomeKitController({"enabled": True}, str(Path(tmp) / "hk.json")).discover())
            c.check("a missing library raises", False)
        except H.HomeKitError as e:
            c.check("a missing library raises", "not installed" in str(e), str(e))
        finally:
            if saved is None:
                sys.modules.pop("aiohomekit", None)
            else:
                sys.modules["aiohomekit"] = saved

        pairing = StubPairing()
        ctl = _controller(tmp, pairing)
        s = _run(ctl.status("tv"))
        c.check("status reads the TV", s["name"] == "Sky Glass" and s["online"], s)
        pairing.db = tv_db(active=0)
        c.check("a fresh cache is served without a round trip", _run(ctl.status("tv"))["power"] is True)
        c.check("max_age 0 forces a re-read", _run(ctl.status("tv", max_age=0))["power"] is False)

        pairing.fail = OSError("No route to host")
        s = _run(ctl.status("tv", max_age=0))
        c.check("an unreachable TV serves its last state, flagged",
                s.get("stale") and s["online"] is False and "route" in s["error"], s)

        pairing.fail = None
        s = _run(ctl.control("tv", {"power": True, "input": 1}))
        c.check("control writes reach the pairing", pairing.writes == [(1, 11, 1), (1, 12, 1)], pairing.writes)
        c.check("control returns re-read state", s["power"] is False, s)

        pairing.fail = OSError("gone")
        confirmed = _run(ctl.unpair("tv"))
        c.check("an unreachable TV is still forgotten locally", confirmed is False and "tv" not in ctl._pairings)
        c.check("forgetting persists to the pairing file", H.load_pairings(ctl._pairings_file) == {})
        c.check("the session to the TV is closed on unpair", pairing.closed)

        try:
            _run(ctl.finish_pairing("other", "12345678"))
            c.check("finishing without a started pairing is refused", False)
        except H.HomeKitError as e:
            c.check("finishing without a started pairing is refused", "start again" in str(e), str(e))


def _real_library(c: Checker) -> None:
    c.section("real aiohomekit contract")
    try:
        from aiohomekit.model import Accessories, Accessory
        from aiohomekit.model.characteristics import CharacteristicsTypes as CT
        from aiohomekit.model.characteristics.const import RemoteKeyValues
        from aiohomekit.model.services import ServicesTypes as ST
        from aiohomekit.testing import FakeController
    except ImportError:
        print("    skipped (aiohomekit not installed)")
        return

    c.check("characteristic UUIDs match the library's",
            all(getattr(H, f"C_{n}") == getattr(CT, n) for n in
                ("ACTIVE", "ACTIVE_IDENTIFIER", "CONFIGURED_NAME", "REMOTE_KEY", "IDENTIFIER",
                 "IS_CONFIGURED", "INPUT_SOURCE_TYPE", "MUTE", "VOLUME", "VOLUME_SELECTOR", "NAME")))
    c.check("service UUIDs match the library's",
            (H.S_TELEVISION, H.S_INPUT_SOURCE, H.S_SPEAKER) ==
            (ST.TELEVISION, ST.INPUT_SOURCE, ST.SPEAKER))
    c.check("remote key codes match the library's",
            all(H.REMOTE_KEYS[k] == RemoteKeyValues[n] for k, n in
                (("up", "ARROW_UP"), ("select", "SELECT"), ("back", "BACK"),
                 ("play_pause", "PLAY_PAUSE"), ("info", "INFORMATION"))))

    acc = Accessory.create_with_info(1, "Living room", "Sky", "LT055", "SN1", "1.0")
    tv = acc.add_service(ST.TELEVISION)
    active = tv.add_char(CT.ACTIVE, value=0)
    ident = tv.add_char(CT.ACTIVE_IDENTIFIER, value=1)
    tv.add_char(CT.CONFIGURED_NAME, value="Sky Glass")
    key = tv.add_char(CT.REMOTE_KEY)
    for n, name in ((1, "Sky TV"), (2, "HDMI 1")):
        src = acc.add_service(ST.INPUT_SOURCE)
        src.add_char(CT.IDENTIFIER, value=n)
        src.add_char(CT.CONFIGURED_NAME, value=name)
        src.add_char(CT.IS_CONFIGURED, value=1)
        src.add_char(CT.CURRENT_VISIBILITY_STATE, value=0)
        tv.add_linked_service(src)
    accessories = Accessories()
    accessories.add_accessory(acc)

    async def flow():
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeController()
            fake.add_device(accessories)
            ctl = H.HomeKitController({"enabled": True}, pairings_file=str(Path(tmp) / "hk.json"))
            ctl._ctl = fake
            found = await ctl.discover()
            c.check("discovery lists the unpaired accessory as available",
                    len(found) == 1 and found[0]["available"] and not found[0]["paired_here"], found)
            did = found[0]["id"]

            await ctl.start_pairing(did)
            try:
                await ctl.finish_pairing(did, "999-99-999")
                c.check("a wrong code fails pairing", False)
            except H.HomeKitError:
                c.check("a wrong code fails pairing", True)
            c.check("a failed code needs a fresh start", did not in ctl._pending)

            await ctl.start_pairing(did)
            s = await ctl.finish_pairing(did, "11122333")
            c.check("the right code pairs and reads the TV", s["name"] == "Sky Glass", s)
            c.check("the pairing is saved", did in H.load_pairings(ctl._pairings_file))
            c.check("inputs come through the library's model", [i["name"] for i in s["inputs"]] ==
                    ["Sky TV", "HDMI 1"], s["inputs"])

            # FakePairing reports accepted writes as events; it does not store them.
            written = {}
            fake.aliases[did].dispatcher_connect(
                lambda ev: written.update({k: v["value"] for k, v in ev.items()}))
            await ctl.control(did, {"power": True, "input": 2})
            c.check("power reaches the Active characteristic", written.get((1, active.iid)) == 1, written)
            c.check("input reaches ActiveIdentifier", written.get((1, ident.iid)) == 2, written)
            await ctl.control(did, {"key": "select"})
            c.check("a remote key reaches RemoteKey",
                    written.get((1, key.iid)) == RemoteKeyValues.SELECT, written)

    _run(flow())


def run() -> Checker:
    c = Checker("homekit_controller")
    _mapping(c)
    _writes(c)
    _store(c)
    _controller_behaviour(c)
    _real_library(c)
    return c


if __name__ == "__main__":
    checker = run()
    sys.exit(1 if checker.failures else 0)
