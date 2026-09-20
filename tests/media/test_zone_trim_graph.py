"""
OpenZone: the differential trim graph (open-zone.md §7.6).

The real TrimGraph and the real OpenZone.trim_ms run; the store is a dict and
the providers are stood in for. What is asserted is that alignments compose —
two models related through a third without ever having shared a zone — and
that an edge measuring placement rather than hardware is dropped instead of
averaged.
"""

from __future__ import annotations

from harness import Checker, REPO  # noqa: F401  (REPO sets sys.path)

from modules.media.cast_sync import OpenZone
from modules.media.trim_graph import TrimGraph

HUB, WIIM, MINI, SONOS = ("Google Nest Hub", "WiiM Pro",
                          "Google Nest Mini", "Sonos One")


def _graph():
    store = {}
    return TrimGraph(lambda: store, store.update), store


def run() -> Checker:
    c = Checker("zone_trim_graph")

    c.section("alignments compose through a shared model")
    g, _ = _graph()
    g.observe_session([(WIIM, 300), (HUB, 219)])
    g.observe_session([(HUB, 219), (MINI, 140)])
    solved = g.solve(anchors={HUB: 219}, priors={})
    c.check("the anchored model's neighbour is placed",
            solved.get(WIIM) == 300, solved)
    c.check("so is the model on the other side", solved.get(MINI) == 140, solved)
    c.check("two models never heard together are now related",
            solved[WIIM] - solved[MINI] == 160, solved)

    c.section("a cycle's closing error is distributed, not dropped")
    g, _ = _graph()
    # Through the hub the WiiM/Mini difference is 160; measured directly it is
    # 180. No walk of the graph can honour both.
    g.observe_session([(WIIM, 300), (HUB, 219)])
    g.observe_session([(HUB, 219), (MINI, 140)])
    g.observe_session([(MINI, 140), (WIIM, 320)])
    solved = g.solve(anchors={HUB: 219}, priors={})
    c.check("every model still gets one answer", set(solved) == {WIIM, MINI},
            solved)
    direct = solved[WIIM] - solved[MINI]
    c.check("neither reading is taken at face value",
            160 < direct < 180, solved)
    c.check("nor is either edge satisfied exactly",
            solved[WIIM] not in (300, 320) and solved[MINI] != 140, solved)

    c.section("an edge that measures placement is not trusted")
    g, _ = _graph()
    for wiim_trim in (300, 340, 262):      # same pair, 78 ms of scatter
        g.observe_session([(WIIM, wiim_trim), (HUB, 219)])
    edge = g.describe()[0]
    c.check("the scatter is visible", edge["spread_ms"] > 25, edge)
    c.check("the edge is marked untrusted", edge["trusted"] is False, edge)
    c.check("and carries nothing into the solve",
            g.solve(anchors={HUB: 219}, priors={}) == {}, g.describe())

    c.section("a single observation is usable but weak")
    g, _ = _graph()
    g.observe_session([(WIIM, 300), (HUB, 219)])
    c.check("one observation still places the model",
            g.solve(anchors={HUB: 219}, priors={}).get(WIIM) is not None)
    c.check("repeating the same answer is not new evidence",
            g.observe(WIIM, 300, HUB, 219) is False)
    c.check("a different answer is", g.observe(WIIM, 305, HUB, 219) is True)

    c.section("a component with no absolute has shape but no position")
    g, _ = _graph()
    g.observe_session([(SONOS, 90), (MINI, 40)])     # neither anchored nor seeded
    c.check("nothing is invented for it",
            g.solve(anchors={}, priors={}) == {}, g.describe())
    c.check("one absolute grounds the whole component",
            g.solve(anchors={MINI: 140}, priors={}) == {SONOS: 190},
            g.solve(anchors={MINI: 140}, priors={}))

    c.section("either direction lands on the same edge")
    g, _ = _graph()
    g.observe(HUB, 219, WIIM, 300)
    g.observe(WIIM, 300, HUB, 219)
    c.check("one edge, one observation", len(g.describe()) == 1
            and g.describe()[0]["observations"] == 1, g.describe())

    c.section("persistence round-trips")
    g, store = _graph()
    g.observe_session([(WIIM, 300), (HUB, 219)])
    reloaded = TrimGraph(lambda: store, store.update)
    c.check("edges survive a restart",
            reloaded.solve(anchors={HUB: 219}, priors={}).get(WIIM)
            == g.solve(anchors={HUB: 219}, priors={}).get(WIIM))
    c.check("a corrupt store degrades to empty",
            TrimGraph(lambda: {"edges": {"a\x1fb": {"samples": ["x"]}}},
                      lambda d: None).describe() == [])

    # --- precedence inside trim_ms ---------------------------------------
    c.section("derived sits under learned and over shipped")

    class _Prov:
        def __init__(self, models):
            self._m = models

        def model_key(self, pid):
            return self._m.get(pid, "")

    prov = _Prov({"cast:hub": HUB, "wiim:1": WIIM, "cast:mini": MINI})
    oz = OpenZone.__new__(OpenZone)
    oz.cast = prov
    oz._provider_resolver = None
    oz._trims = {}
    oz._model_trims = {}
    store = {}
    oz._graph = TrimGraph(lambda: store, store.update)
    # A Mini aligned against a hub the listener measured at 200, not the
    # shipped 219: the derived Mini must follow the measurement.
    oz._model_trims[HUB] = 200
    oz._graph.observe_session([(HUB, 200), (MINI, 120)])
    c.check("the model measured here keeps its own value",
            oz.trim_ms("cast:hub") == 200)
    c.check("its neighbour is derived from that, not from the table",
            oz.trim_ms("cast:mini") == 120, oz.trim_ms("cast:mini"))
    c.check("a model with no edge falls through to the shipped prior",
            oz.trim_ms("wiim:1") == 300, oz.trim_ms("wiim:1"))

    c.section("an explicit per-device trim still outranks everything")
    oz._trims["cast:mini"] = 95
    c.check("explicit wins", oz.trim_ms("cast:mini") == 95)

    c.section("moving an anchor re-solves rather than serving a stale answer")
    oz._model_trims[HUB] = 240
    oz._graph.invalidate()
    c.check("the derived neighbour moves with it",
            oz.trim_ms("cast:mini") == 95 and oz._solve_graph()[MINI] == 160,
            oz._solve_graph())

    return c


if __name__ == "__main__":
    run()
