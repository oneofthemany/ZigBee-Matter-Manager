"""
OpenZone: a LinkPlay member's input is polled while the zone plays
(open-zone.md §7.1).

Cast may go on reporting PLAYING from a WiiM that has switched to HDMI-ARC, so
the box itself is read every few seconds: two foreign readings in a row step it
aside; one glitched reply does not. The mode a box reports *while the zone is
audibly playing on it* is learned as Cast's own — never an input, which beside
a PLAYING status means the status is stale. The real `_sweep_inputs`,
`_check_input`, policy and `LinkPlayDirectory` are exercised against a stubbed
httpapi.
"""

from __future__ import annotations

import asyncio
import json
import time

from harness import Checker, REPO  # noqa: F401  (REPO sets sys.path)

from modules.media import cast_sync
from modules.media.linkplay import LinkPlayDirectory
from test_zone_yield import _fake_cast, _stream, _loads
from test_zone_lock import _zone, HOST


def _directory(tmp: str, modes: dict) -> LinkPlayDirectory:
    d = LinkPlayDirectory(state_file=f"{tmp}/linkplay.json")
    d._devices[HOST] = {"ip": HOST, "name": "WiiM Ultra"}

    async def get(ip, command, timeout):
        m = modes.get("mode")
        return None if m is None else {"mode": str(m)}
    d._get_json = get
    return d


def _wire(z, d) -> None:
    z.set_input_resolver(d.owner_for_host)
    z.set_input_reader(d.read_host, d.learn_cast_mode)


def _playing(st) -> None:
    st.state = "PLAYING"
    st.last_lag_at = time.monotonic()


async def _poll(z, st) -> None:
    st.input_check_at = 0.0
    z._sweep_inputs()
    await asyncio.sleep(0.02)


def _dir_learning(c: Checker, tmp: str) -> None:
    c.section("directory: Cast's own mode is learned, an input never is")

    async def go():
        modes = {"mode": 2}
        d = _directory(tmp, modes)
        r = await d.read(HOST)
        c.check("DLNA reads as foreign before learning",
                r["owner"] == "DLNA" and not r["input"], r)
        c.check("learning it is accepted", d.learn_cast_mode(HOST, 2))
        c.check("…and it then reads as free",
                (await d.read(HOST))["owner"] is None)
        c.check("an input is refused", not d.learn_cast_mode(HOST, 49))
        c.check("…as is a multiroom follower", not d.learn_cast_mode(HOST, 99))
        d2 = _directory(tmp, modes)
        c.check("the learned mode survives a restart", d2.cast_mode(HOST) == 2)
        json.dump({"cast_modes": {HOST: 49}}, open(f"{tmp}/linkplay.json", "w"))
        d3 = _directory(tmp, modes)
        c.check("a saved input is never loaded as Cast's",
                d3.cast_mode(HOST) is None)
        modes["mode"] = None
        r = await d.read(HOST)
        c.check("no reply answers from memory, marked stale",
                r["stale"] and r["mode"] == 2, r)
    asyncio.run(go())


def _switch(c: Checker, tmp: str) -> None:
    c.section("a switch to HDMI-ARC is caught while Cast still says PLAYING")

    async def go():
        z = _zone(tmp, _fake_cast())
        loaded = _loads(z)
        modes = {"mode": 10}
        _wire(z, _directory(tmp, modes))
        st = _stream(z)
        _playing(st)
        await _poll(z, st)
        c.check("on the network input nothing happens",
                st.parked_since is None and st.input_mode == 10)
        c.check("status carries the reading",
                z.status()["devices"][0]["input"] == {"mode": 10, "owner": ""})
        modes["mode"] = 49
        _playing(st)
        await _poll(z, st)
        c.check("one reading is not enough", st.parked_since is None
                and st.input_strikes == 1)
        _playing(st)
        await _poll(z, st)
        c.check("the second steps it aside",
                st.yield_kind == "input" and st.yield_reason == "HDMI-ARC")
        c.check("no LOAD was issued", loaded == [])
        c.check("an input seen beside a live PLAYING is not learned",
                z._input_learner.__self__.cast_mode(HOST) is None)
    asyncio.run(go())


def _glitch(c: Checker, tmp: str) -> None:
    c.section("one glitched reading does not drop a speaker")

    async def go():
        z = _zone(tmp, _fake_cast())
        modes = {"mode": 49}
        _wire(z, _directory(tmp, modes))
        st = _stream(z)
        _playing(st)
        await _poll(z, st)
        modes["mode"] = 10
        await _poll(z, st)
        modes["mode"] = 49
        await _poll(z, st)
        c.check("the strike count resets on a network reading",
                st.parked_since is None and st.input_strikes == 1)
    asyncio.run(go())


def _learn(c: Checker, tmp: str) -> None:
    c.section("the mode Cast plays in is learned from a live zone")

    async def go():
        z = _zone(tmp, _fake_cast())
        modes = {"mode": 2}                    # suppose Cast reads as DLNA
        d = _directory(tmp, modes)
        _wire(z, d)
        st = _stream(z)
        for _ in range(3):
            _playing(st)
            await _poll(z, st)
        c.check("a zone audibly playing is not yielded over its own mode",
                st.parked_since is None, st.yield_reason)
        c.check("the mode is learned", d.cast_mode(HOST) == 2)
        c.check("the reading is shown as free", st.input_owner == "")
    asyncio.run(go())


def _stale(c: Checker, tmp: str) -> None:
    c.section("a stale Cast status teaches nothing")

    async def go():
        z = _zone(tmp, _fake_cast())
        modes = {"mode": 1}                    # AirPlay took it
        d = _directory(tmp, modes)
        _wire(z, d)
        st = _stream(z)
        st.state = "PLAYING"
        st.last_lag_at = time.monotonic() - 60  # no lag read for a minute
        await _poll(z, st)
        await _poll(z, st)
        c.check("it yields to AirPlay", st.yield_reason == "AirPlay")
        c.check("and AirPlay is not learned as Cast", d.cast_mode(HOST) is None)
    asyncio.run(go())


def _policies(c: Checker, tmp: str) -> None:
    c.section("the poll obeys the speaker's policy")

    async def go():
        z = _zone(tmp, _fake_cast())
        modes = {"mode": 49}
        _wire(z, _directory(tmp, modes))
        st = _stream(z)
        await z.set_policy("cast:a", mode="reclaim")
        for _ in range(3):
            await _poll(z, st)
        c.check("reclaim: never stepped aside", st.parked_since is None)
        await z.set_policy("cast:a", mode="sticky")
        for _ in range(2):
            await _poll(z, st)
        lock = z.policy("cast:a")["lock"]
        c.check("sticky: the switch becomes a lock",
                bool(lock) and lock["reason"] == "HDMI-ARC"
                and st.yield_kind == "lock", lock)
    asyncio.run(go())


def _cadence(c: Checker, tmp: str) -> None:
    c.section("cadence")

    async def go():
        z = _zone(tmp, _fake_cast())
        reads = []

        async def reader(host):
            reads.append(host)
            await asyncio.sleep(0.05)
            return None
        z.set_input_reader(reader)
        st = _stream(z)
        z._sweep_inputs()
        z._sweep_inputs()
        await asyncio.sleep(0.1)
        c.check("one read in flight, and none before the interval",
                len(reads) == 1, reads)
        c.check("a non-LinkPlay speaker carries no input",
                st.input_mode is None
                and z.status()["devices"][0]["input"] is None)
        z._yield_stream(st, "cast", "YouTube")
        st.input_check_at = 0.0
        z._sweep_inputs()
        await asyncio.sleep(0.1)
        c.check("a yielded speaker is left to its own probe", len(reads) == 1)
        z._preroll = True
        c.check("nothing is read during pre-roll",
                z._sweep_inputs() is None and len(reads) == 1)
    asyncio.run(go())


def run() -> Checker:
    c = Checker("zone_input")
    import tempfile
    for fn in (_dir_learning, _switch, _glitch, _learn, _stale, _policies,
               _cadence):
        with tempfile.TemporaryDirectory() as tmp:
            fn(c, tmp)
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
