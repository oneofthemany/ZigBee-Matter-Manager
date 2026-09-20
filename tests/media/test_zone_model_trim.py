"""
OpenZone: model-keyed trim defaults across ecosystems (open-zone.md §A.4).

The trim carries the part of a device's chain below its reported media
position, which no control protocol exposes, so it is a hardware constant
rather than something the engine can measure. These tests cover how that
constant is keyed and where it comes from: identity is asked of whichever
provider owns the device, and the shipped prior is the last resort under
anything established on this network.

The real `trim_ms`, `_model_key` and `_provider_for` run; only the providers
are stood in for, and the WiiM one is the real class with its `getStatusEx`
cache pre-filled, since that cache is what the identity is read from.
"""

from __future__ import annotations

from harness import Checker, REPO  # noqa: F401  (REPO sets sys.path)

from modules.media import latency_seed as seed
from modules.media.cast_sync import OpenZone
from modules.media.players.wiim import WiiMPlayerProvider
from modules.media.trim_graph import TrimGraph


class _FakeCast:
    """Cast provider stand-in: only model_key is reached from here."""

    provider = "cast"

    def __init__(self, models=None):
        self._models = models or {}

    def model_key(self, player_id: str) -> str:
        return self._models.get(player_id, "")


def _zone(cast, registry=None):
    """An OpenZone with nothing built but the trim store — __init__ opens
    files and a discovery browser, neither of which this needs."""
    oz = OpenZone.__new__(OpenZone)
    oz.cast = cast
    oz._provider_resolver = None
    oz._trims = {}
    oz._model_trims = {}
    store = {}
    oz._graph = TrimGraph(lambda: store, store.update)   # empty: §7.6 is
    if registry is not None:                             # tested separately
        oz.set_provider_resolver(
            lambda pid: registry.get(pid.split(":", 1)[0]))
    return oz


def run() -> Checker:
    c = Checker("zone_model_trim")

    # --- the seed table's matching ---------------------------------------
    c.section("a model names itself differently per ecosystem")
    c.check("LinkPlay's build string matches the readable entry",
            seed.trim_ms("WiiM_Pro_with_gc4a") == 300,
            seed.trim_ms("WiiM_Pro_with_gc4a"))
    c.check("the chipset qualifier is not part of identity",
            seed.normalise("WiiM_Pro_with_gc4a") == seed.normalise("WiiM Pro"))
    c.check("Cast's marketing name matches as written",
            seed.trim_ms("Google Nest Hub") == 219, seed.trim_ms("Google Nest Hub"))
    c.check("an unlisted model asks for nothing",
            seed.trim_ms("Google Home Mini") == 0)
    c.check("no identity asks for nothing", seed.trim_ms("") == 0)

    # --- identity comes from the owning provider -------------------------
    c.section("identity is asked of the ecosystem that owns the device")
    wiim = WiiMPlayerProvider(["192.168.1.50", "192.168.1.51"])
    wiim._models["192.168.1.50"] = "WiiM_Pro_with_gc4a"   # as getStatusEx reports
    cast = _FakeCast({"cast:hub": "Google Nest Hub"})
    oz = _zone(cast, {"cast": cast, "wiim": wiim})

    c.check("a WiiM keys on its LinkPlay project",
            oz._model_key("wiim:192.168.1.50") == "WiiM_Pro_with_gc4a",
            oz._model_key("wiim:192.168.1.50"))
    c.check("a Cast device still keys on its model name",
            oz._model_key("cast:hub") == "Google Nest Hub")
    c.check("an unprobed unit has no identity rather than a wrong one",
            oz._model_key("wiim:192.168.1.51") == "")

    c.section("without a resolver, only Cast can be identified")
    bare = _zone(cast)
    c.check("Cast is unaffected", bare.trim_ms("cast:hub") == 219)
    c.check("other ecosystems opt out silently",
            bare.trim_ms("wiim:192.168.1.50") == 0)

    # --- precedence -------------------------------------------------------
    c.section("a measurement always beats a table")
    c.check("the prior seeds an untouched WiiM",
            oz.trim_ms("wiim:192.168.1.50") == 300,
            oz.trim_ms("wiim:192.168.1.50"))
    c.check("the prior seeds an untouched hub", oz.trim_ms("cast:hub") == 219)
    c.check("a unit with no identity gets no prior",
            oz.trim_ms("wiim:192.168.1.51") == 0)

    oz._model_trims["WiiM_Pro_with_gc4a"] = 287
    c.check("a trim learned on this network beats the prior",
            oz.trim_ms("wiim:192.168.1.50") == 287,
            oz.trim_ms("wiim:192.168.1.50"))

    oz._trims["wiim:192.168.1.50"] = 310
    c.check("an explicit per-device trim beats both",
            oz.trim_ms("wiim:192.168.1.50") == 310)
    c.check("its untrimmed sibling is unaffected",
            oz.trim_ms("wiim:192.168.1.51") == 0)

    c.section("the prior survives a model the learner dropped as positional")
    # Units of one model trimmed further apart than TRIM_MODEL_AGREE_MS: the
    # difference is where they stand, not what they are, so _learn_model_trim
    # drops the model entry. A third, untrimmed unit must not inherit either
    # unit's placement — but the hardware constant underneath still holds.
    disputed = WiiMPlayerProvider(["192.168.1.50", "192.168.1.51",
                                   "192.168.1.52"])
    for ip in ("192.168.1.50", "192.168.1.51", "192.168.1.52"):
        disputed._models[ip] = "WiiM_Pro_with_gc4a"
    dropped = _zone(cast, {"cast": cast, "wiim": disputed})
    dropped._trims = {"wiim:192.168.1.50": 310, "wiim:192.168.1.52": 180}
    c.check("each disputing unit keeps its own value",
            dropped.trim_ms("wiim:192.168.1.50") == 310
            and dropped.trim_ms("wiim:192.168.1.52") == 180)
    c.check("no model entry survives the dispute",
            "WiiM_Pro_with_gc4a" not in dropped._model_trims)
    c.check("an untrimmed unit inherits the hardware, not a placement",
            dropped.trim_ms("wiim:192.168.1.51") == 300,
            dropped.trim_ms("wiim:192.168.1.51"))

    c.section("a model key follows the hardware, not the address")
    moved = WiiMPlayerProvider(["192.168.1.99"])
    moved._models["192.168.1.99"] = "WiiM_Pro_with_gc4a"
    oz2 = _zone(cast, {"cast": cast, "wiim": moved})
    c.check("the same model at a new IP keeps the prior",
            oz2.trim_ms("wiim:192.168.1.99") == 300,
            oz2.trim_ms("wiim:192.168.1.99"))

    return c


if __name__ == "__main__":
    run()
