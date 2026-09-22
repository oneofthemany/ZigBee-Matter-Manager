"""
OpenZone: per-speaker zone policy and locks (open-zone.md §7.1).

A lock is the user saying "leave this speaker alone" — gaming on a WiiM's
HDMI-ARC while the zone plays on elsewhere. The mode says what happens when
its input changes by itself: ``auto`` steps aside and rejoins when free,
``sticky`` turns the change into a lock, ``reclaim`` takes it back. The real
policy store, gate, sweeps and rejoin are exercised against the fakes from
test_zone_yield; the automation step is checked by reading, as the engine
module cannot be imported without the Zigbee stack.
"""

from __future__ import annotations

import ast
import asyncio
import json
import time
from types import SimpleNamespace

from harness import Checker, REPO

from modules.media import cast_sync
from modules.media.cast_sync import OpenZone
from test_zone_yield import (_fake_cast, _FakeCastProvider, _FakeSource,
                             _stream, _loads, _input)

HOST = "192.168.1.102"


def _zone(tmp: str, cast) -> OpenZone:
    prov = _FakeCastProvider(cast)
    prov._infos = {"a": SimpleNamespace(host=HOST, friendly_name="WiiM Ultra",
                                        is_group=False)}
    z = OpenZone(prov, {
        "trims_file": f"{tmp}/trims.json",
        "model_trims_file": f"{tmp}/model_trims.json",
        "groups_file": f"{tmp}/groups.json",
        "model_file": f"{tmp}/model.json",
        "policy_file": f"{tmp}/policy.json",
    })
    z.running = True
    z._source = _FakeSource()
    z._epoch = time.monotonic() - 100.0
    return z


def _idle():
    return _fake_cast(app_id=None, player_state="IDLE")


def _lock_blocks(c: Checker, tmp: str) -> None:
    c.section("a lock keeps a speaker out of every zone")

    async def go():
        z = _zone(tmp, _idle())
        loaded = _loads(z)
        _input(z, {})
        st = _stream(z)
        res = await z.set_policy("cast:a", lock="lock", by="sean")
        c.check("the lock is reported", res["success"] and res["lock"], res)
        await z._launch_stream("cast:a", "a", gate="input")
        c.check("session start does not LOAD a locked speaker", loaded == [])
        c.check("it is yielded to the lock", st.yield_kind == "lock"
                and st.yield_reason == "locked", st.yield_reason)
        await z._rejoin_stream(st)
        c.check("the probe does not let it back while locked", loaded == [])
        saved = json.load(open(f"{tmp}/policy.json"))
        c.check("the lock is persisted, with who set it",
                saved["cast:a"]["lock"]["by"] == "sean", saved)
        z2 = _zone(tmp, _idle())
        c.check("…and survives a restart", bool(z2.policy("cast:a")["lock"]))
    asyncio.run(go())


def _lock_live(c: Checker, tmp: str) -> None:
    c.section("locking a speaker the zone is playing on frees it at once")

    async def go():
        cast = _fake_cast()
        quits = []
        cast.quit_app = lambda: quits.append(1)
        z = _zone(tmp, cast)
        _loads(z)
        st = _stream(z)
        await z.set_policy("cast:a", lock="lock")
        c.check("it leaves the group", st.parked_since is not None
                and st.yield_kind == "lock")
        c.check("the zone's stream on it is stopped", quits == [1], quits)
        cast.status.app_id = "233637DE"
        await z.set_policy("cast:a", lock="unlock")
        await z.set_policy("cast:a", lock="lock")
        c.check("someone else's app is not stopped", quits == [1], quits)
    asyncio.run(go())


def _unlock_hands_back(c: Checker, tmp: str) -> None:
    c.section("unlocking hands the speaker back, once")

    async def go():
        z = _zone(tmp, _idle())
        loaded = _loads(z)
        holder = {HOST: "HDMI-ARC"}
        _input(z, holder)
        st = _stream(z)
        await z.set_policy("cast:a", lock="lock")
        await z.set_policy("cast:a", lock="unlock")
        c.check("the handback is armed", z.policy("cast:a")["handback"])
        c.check("the next probe is due now", st.park_probe_at == 0.0)
        await z._rejoin_stream(st)
        c.check("it rejoins over HDMI-ARC without a settle wait",
                len(loaded) == 1 and st.parked_since is None, loaded)
        c.check("the handback is spent on that LOAD",
                not z.policy("cast:a")["handback"])
        await z._reload_stream(st)
        c.check("the next recovery yields to the input again",
                len(loaded) == 1 and st.yield_reason == "HDMI-ARC")
        # A handback left unused does not outlive its window.
        await z.set_policy("cast:a", lock="lock")
        await z.set_policy("cast:a", lock="unlock")
        z._policies["cast:a"]["handback_until"] = time.time() - 1
        c.check("an old handback lapses", not z.policy("cast:a")["handback"])
    asyncio.run(go())


def _timed(c: Checker, tmp: str) -> None:
    c.section("a timed lock expires by itself")

    async def go():
        z = _zone(tmp, _idle())
        loaded = _loads(z)
        _input(z, {})
        st = _stream(z)
        res = await z.set_policy("cast:a", lock="lock", minutes=60)
        until = res["lock"]["until"]
        c.check("it carries its end", 3590 < until - time.time() <= 3600)
        c.check("…and says so", st.yield_reason.startswith("locked until "),
                st.yield_reason)
        z._policies["cast:a"]["lock"]["until"] = time.time() - 1
        await z._rejoin_stream(st)
        c.check("expired: it rejoins at the next probe", len(loaded) == 1)
        c.check("the default is not stored", "cast:a" not in z._policies,
                z._policies)
    asyncio.run(go())


def _toggle(c: Checker, tmp: str) -> None:
    c.section("toggle — one button for a Zigbee remote")

    async def go():
        z = _zone(tmp, _idle())
        a = await z.set_policy("cast:a", lock="toggle")
        b = await z.set_policy("cast:a", lock="toggle")
        c.check("first press locks, second unlocks",
                bool(a["lock"]) and not b["lock"])
        bad = await z.set_policy("cast:a", lock="jam")
        c.check("an unknown lock action is refused", not bad["success"])
        bad = await z.set_policy("cast:a", mode="loud")
        c.check("an unknown mode is refused", not bad["success"])
    asyncio.run(go())


def _sticky(c: Checker, tmp: str) -> None:
    c.section("sticky: changing input is the lock")

    async def go():
        z = _zone(tmp, _idle())
        loaded = _loads(z)
        holder = {HOST: "HDMI-ARC"}
        _input(z, holder)
        st = _stream(z)
        await z.set_policy("cast:a", mode="sticky")
        await z._reload_stream(st)
        lock = z.policy("cast:a")["lock"]
        c.check("switching to HDMI-ARC locks it",
                bool(lock) and lock["by"] == "sticky"
                and lock["reason"] == "HDMI-ARC", lock)
        holder.clear()                          # the TV went to sleep
        st.free_since = time.monotonic() - 3600
        await z._rejoin_stream(st)
        await z._rejoin_stream(st)
        c.check("back on the network input, it still stays out",
                loaded == [] and st.parked_since is not None)
        holder[HOST] = "HDMI-ARC"
        await z.set_policy("cast:a", lock="unlock")
        await z._rejoin_stream(st)
        c.check("an unlock is not re-locked while it is still on HDMI",
                len(loaded) == 1 and not z.policy("cast:a")["lock"], loaded)
        c.check("the mode survives the unlock",
                z.policy("cast:a")["mode"] == "sticky")
    asyncio.run(go())


def _reclaim(c: Checker, tmp: str) -> None:
    c.section("reclaim: the zone always takes it back")

    async def go():
        cast = _fake_cast(app_id="233637DE", display_name="YouTube",
                          player_state="IDLE")
        z = _zone(tmp, cast)
        loaded = _loads(z)
        _input(z, {HOST: "HDMI-ARC"})
        st = _stream(z)
        await z.set_policy("cast:a", mode="reclaim")
        await z._read_media_time(st)
        c.check("another app mid-session is not yielded to",
                st.parked_since is None and st.interrupted_since is not None)
        await z._reload_stream(st)
        c.check("a device on HDMI-ARC is re-LOADed", len(loaded) == 1)
        await z.set_policy("cast:a", lock="lock")
        await z._reload_stream(st)
        c.check("a lock still outranks it", len(loaded) == 1
                and st.yield_kind == "lock")
    asyncio.run(go())


def _keys(c: Checker, tmp: str) -> None:
    c.section("a lock set from the WiiM card lands on the zone member")

    async def go():
        z = _zone(tmp, _idle())
        wiim = SimpleNamespace(device_key=lambda pid: pid.split(":", 1)[1])
        z.set_provider_resolver(
            lambda pid: wiim if pid.startswith("wiim:") else z.cast)
        res = await z.set_policy(f"wiim:{HOST}", lock="lock")
        c.check("wiim:<ip> is stored under the Cast id at that address",
                res["player_id"] == "cast:a" and z.policy("cast:a")["lock"],
                res)
        c.check("status lists it by name",
                z.status()["policies"]["cast:a"]["name"] == "WiiM Ultra")
    asyncio.run(go())


def _automation(c: Checker) -> None:
    c.section("automation step (read from source)")
    src = (REPO / "modules" / "automation.py").read_text()
    ast.parse(src)
    c.check("zone_lock is a valid media_action", '"zone_lock"' in src)
    c.check("it is refused on a zone",
            "zone_lock needs a speaker, not a zone" in src)
    c.check("it calls set_policy with the rule as author",
            'by=f"rule {rule_id}"' in src and "zone.set_policy(" in src)


def run() -> Checker:
    c = Checker("zone_lock")
    import tempfile
    for fn in (_lock_blocks, _lock_live, _unlock_hands_back, _timed, _toggle,
               _sticky, _reclaim, _keys):
        with tempfile.TemporaryDirectory() as tmp:
            fn(c, tmp)
    _automation(c)
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
