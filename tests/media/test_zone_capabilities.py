"""
OpenZone: what a zone may be built from, and what counts as one speaker.

Two questions the engine used to answer by pattern-matching an id. Membership
is a capability of the ecosystem — can a zone serve this device and read it
back — and identity is a property of the box, which may answer to more than
one ecosystem at once.
"""

from __future__ import annotations

import tempfile

from harness import Checker, REPO  # noqa: F401  (REPO sets sys.path)

from modules.media.cast_sync import OpenZone
from modules.media.players.base import PlayerProvider
from modules.media.trim_graph import TrimGraph


class _Cast(PlayerProvider):
    provider, label = "cast", "Cast"
    zone_transport = True

    def __init__(self, hosts=None, models=None):
        self._hosts = hosts or {}
        self._models = models or {}

    async def list_players(self): return []
    async def get_state(self, player_id): return None
    async def play_url(self, player_id, item): return None
    async def pause(self, player_id): return None
    async def resume(self, player_id): return None
    async def stop_playback(self, player_id): return None
    async def set_volume(self, player_id, level): return None

    def device_key(self, player_id): return self._hosts.get(player_id, "")
    def model_key(self, player_id): return self._models.get(player_id, "")


class _WiiM(_Cast):
    provider, label = "wiim", "WiiM"
    zone_transport = False
    groups_natively = True

    def device_key(self, player_id): return player_id.split(":", 1)[-1]


def _zone(tmp: str, registry):
    z = OpenZone.__new__(OpenZone)
    z.cast = registry.get("cast")
    z._provider_resolver = None
    z._trims, z._model_trims = {}, {}
    store = {}
    z._graph = TrimGraph(lambda: store, store.update)
    z.set_provider_resolver(lambda pid: registry.get(pid.split(":", 1)[0]))
    return z


def run() -> Checker:
    c = Checker("zone_capabilities")

    with tempfile.TemporaryDirectory() as tmp:
        # The same box on 192.168.1.60, answering both ecosystems.
        cast = _Cast(hosts={"cast:uuid-wiim": "192.168.1.60",
                            "cast:uuid-hub": "192.168.1.61"},
                     models={"cast:uuid-wiim": "WiiM Ultra",
                             "cast:uuid-hub": "Google Nest Hub"})
        wiim = _WiiM()
        reg = {"cast": cast, "wiim": wiim}
        z = _zone(tmp, reg)

        c.section("membership is a capability, not a prefix")
        c.check("a Cast speaker can be driven by a zone",
                z._zone_capable("cast:uuid-hub"))
        c.check("a LinkPlay speaker cannot, yet",
                not z._zone_capable("wiim:192.168.1.60"))
        c.check("nor can an ecosystem the engine has never heard of",
                not z._zone_capable("beoplay:1"))

        c.section("a zone refuses members it could not correct")
        z._groups, z._groups_file = {}, f"{tmp}/g.json"
        z._write_json = staticmethod(lambda *a: None)
        out = z.save_group("Kitchen", ["cast:uuid-hub", "wiim:192.168.1.60"])
        c.check("a mixed group is refused while only one can be driven",
                out.get("success") is False, out)
        out = z.save_group("Kitchen", ["cast:uuid-hub", "cast:uuid-wiim"])
        c.check("two drivable speakers are accepted", out.get("success"), out)
        c.check("and both were kept",
                len(z._groups[out["id"]]["members"]) == 2, z._groups)

        c.section("one box reached two ways is one box")
        c.check("the Cast side reports the address",
                z._device_key("cast:uuid-wiim") == "192.168.1.60")
        c.check("the LinkPlay side reports the same one",
                z._device_key("wiim:192.168.1.60") == "192.168.1.60")
        c.check("a different speaker is a different box",
                z._device_key("cast:uuid-hub") != z._device_key("cast:uuid-wiim"))

        c.section("a trim set on it through one ecosystem is its trim")
        z._trims["wiim:192.168.1.60"] = 300
        c.check("the Cast id inherits it",
                z.trim_ms("cast:uuid-wiim") == 300, z.trim_ms("cast:uuid-wiim"))
        c.check("an unrelated speaker is untouched",
                z.trim_ms("cast:uuid-hub") != 300)

        c.section("but the device's own explicit trim still wins")
        z._trims["cast:uuid-wiim"] = 310
        c.check("its own value outranks its sibling's",
                z.trim_ms("cast:uuid-wiim") == 310)

        c.section("a provider with no address opts out rather than colliding")
        blind = _Cast(hosts={})
        z2 = _zone(tmp, {"cast": blind, "wiim": wiim})
        z2._trims["wiim:192.168.1.60"] = 300
        c.check("no identity, no inheritance",
                z2._device_key("cast:uuid-wiim") == "")
        c.check("and no trim borrowed from a box it cannot claim to be",
                z2.trim_ms("cast:uuid-wiim") == 0,
                z2.trim_ms("cast:uuid-wiim"))

    return c


if __name__ == "__main__":
    run()
