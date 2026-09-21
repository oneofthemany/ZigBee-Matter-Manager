"""
OpenZone: a slow start is not a pipeline, and one reading may not set the zone
(open-zone.md §7.3).

The fixtures are real pre-roll readings taken from a live six-speaker zone.
What makes them a test rather than an anecdote is that the same physical
speaker reports both 454 ms and 7913 ms on different days: a pipeline latency
is a property of the hardware, so a probe that returns both is not measuring
one, and the engine has to tell which reading to believe.
"""

from __future__ import annotations

import tempfile
import time

from harness import Checker, REPO  # noqa: F401  (REPO sets sys.path)

from modules.media import cast_sync
from modules.media.cast_sync import OpenZone, _Stream, PROBE_MODEL_FACTOR

# Observed on one zone across ten sessions. The Pixel Tablet's ~6.6 s is real
# and repeatable; the ~7.9 s readings land on whichever Google speaker was
# slow to start that day.
OBSERVED = {
    "Kitchen Right": [454, 500, 586, 747, 806, 1082, 1328, 7898, 7908, 7913],
    "Elena": [457, 510, 511, 637, 654, 783, 1249, 7811, 7866, 7920],
    "Pixel Tablet": [6605, 6608, 6616, 6634, 6660, 6689, 6692, 6778],
    "WiiM Ultra": [620, 624, 631, 641, 648, 677],
}


class _FakeSource:
    delay_s = 4.0

    def earliest_sample(self) -> int:
        return -10 ** 9

    def latest_sample(self) -> int:
        return 10 ** 9


def _zone(tmp: str, ring_s: float = 20.0) -> OpenZone:
    z = OpenZone(None, {
        "trims_file": f"{tmp}/t.json", "model_trims_file": f"{tmp}/mt.json",
        "groups_file": f"{tmp}/g.json", "model_file": f"{tmp}/m.json",
        "trim_graph_file": f"{tmp}/gr.json", "ring_capacity_s": ring_s,
    })
    z.running = True
    z._source = _FakeSource()
    z._epoch = time.monotonic() - 100.0
    return z


def _stream(z: OpenZone, sid: str, name: str) -> _Stream:
    st = _Stream(sid, f"cast:{sid}", name)
    st.pos, st.connected = 0.0, True
    z._streams[sid] = st
    return st


def run() -> Checker:
    c = Checker("zone_probe_sanity")

    with tempfile.TemporaryDirectory() as tmp:
        # --- the probe's plausibility test --------------------------------
        c.section("the model separates a slow start from a deep pipeline")
        z = _zone(tmp)
        kr = _stream(z, "kr", "Kitchen Right")
        pt = _stream(z, "pt", "Pixel Tablet")
        z._model[kr.player_id] = {"probe_floor_s": 0.454}   # its fastest start
        z._model[pt.player_id] = {"probe_floor_s": 6.605}   # genuinely this deep

        lat, ok = z._sane_probe(kr, 7.913)
        c.check("a 7.9 s reading on a 0.75 s speaker is rejected", not ok)
        c.check("and the model is used in its place", lat == 0.454, lat)
        lat, ok = z._sane_probe(pt, 6.660)
        c.check("the Pixel Tablet's real 6.6 s pipeline is kept", ok, lat)
        c.check("at the value it measured", lat == 6.660, lat)

        c.section("every honest reading in the fixture survives")
        for name, sid in (("Kitchen Right", "kr"), ("Elena", "el"),
                          ("Pixel Tablet", "pt"), ("WiiM Ultra", "wu")):
            st = z._streams.get(sid) or _stream(z, sid, name)
            z._model[st.player_id] = {"probe_floor_s": min(OBSERVED[name]) / 1000.0}
            good = [v for v in OBSERVED[name] if v < 7000]
            bogus = [v for v in OBSERVED[name] if v >= 7000]
            kept = all(z._sane_probe(st, v / 1000.0)[1] for v in good)
            dropped = all(not z._sane_probe(st, v / 1000.0)[1] for v in bogus)
            c.check(f"{name}: {len(good)} real kept", kept, good)
            c.check(f"{name}: {len(bogus)} slow starts dropped", dropped, bogus)

        c.section("with no history there is nothing to judge against")
        fresh = _stream(z, "new", "New Speaker")
        c.check("an unknown device is believed", z._sane_probe(fresh, 7.9)[1])

        c.section("a small reading is never second-guessed")
        z._model[kr.player_id] = {"probe_floor_s": 0.05}
        c.check("even at many times the model",
                z._sane_probe(kr, 0.9)[1], "under PROBE_MODEL_MIN_S")
        c.check("the factor is what admits a real re-measurement",
                z._sane_probe(pt, 6.605 * (PROBE_MODEL_FACTOR - 0.5))[1] is True)

        c.section("the floor is learned from the readings, not averaged")
        # Replay the real sessions in the order they arrived. A mean of these
        # lands near 2.9 s — between the two populations, describing neither,
        # and loose enough to admit the very readings this rejects.
        for name, sid in (("Kitchen Right", "k2"), ("Elena", "e2")):
            st = _stream(z, sid, name)
            m = z._model.setdefault(st.player_id, {})
            admitted = []
            for ms in OBSERVED[name]:
                lat, trusted = z._sane_probe(st, ms / 1000.0)
                if trusted:
                    admitted.append(ms)
                    z._learn_probe_floor(m, ms / 1000.0)
            floor = m["probe_floor_s"] * 1000.0
            mean = sum(OBSERVED[name]) / len(OBSERVED[name])
            # Not the exact minimum — the leak lets it ride up with the honest
            # readings. What has to hold is that it stays inside that
            # population, so the gap to a slow start is still several-fold.
            c.check(f"{name}: floor stays among the real readings",
                    min(admitted) <= floor <= max(admitted), floor)
            c.check(f"{name}: and well clear of a slow start",
                    floor * PROBE_MODEL_FACTOR < 7800, floor)
            c.check(f"{name}: no slow start was ever admitted",
                    max(admitted) < 7000, admitted)
            c.check(f"{name}: a mean would have admitted them",
                    mean * PROBE_MODEL_FACTOR > 7900, mean)

        c.section("a floor relaxes upward if the device really did slow down")
        st = _stream(z, "slow", "Slowed Down")
        m = z._model.setdefault(st.player_id, {})
        z._learn_probe_floor(m, 0.5)
        for _ in range(40):
            z._learn_probe_floor(m, 2.0)
        c.check("it climbs toward the new truth",
                1.9 < m["probe_floor_s"] <= 2.0, m["probe_floor_s"])

        # --- the target-lag bound ------------------------------------------
        c.section("one unserveable reading may not set the zone's target")
        z2 = _zone(tmp)
        for sid, name in (("a", "Kitchen Right"), ("b", "Elena"),
                          ("c", "Pixel Tablet")):
            _stream(z2, sid, name)
        cap = z2._target_lag_cap()
        c.check("the cap is what the delay line can seat against",
                4.0 < cap < 20.0, cap)

        ok_lags = {"a": 1.1, "b": 0.8, "c": 6.7}
        c.check("a serveable spread is passed through untouched",
                abs(z2._bounded_target(ok_lags)
                    - (6.7 + cast_sync.STREAM_LAG_MARGIN_S)) < 1e-9,
                z2._bounded_target(ok_lags))

        # The two targets this zone actually derived in the field.
        for bad, label in ((106.46, "106.81s"), (296.42, "296.77s")):
            got = z2._bounded_target({"a": 1.1, "b": 0.8, "c": bad})
            c.check(f"a reading that produced a {label} target is refused",
                    got == cap, got)
        c.check("the honest devices still get a usable target",
                z2._bounded_target({"a": 1.1, "b": 0.8, "c": 296.42}) >= 4.0)

        c.section("the cap follows the delay line's actual size")
        small = _zone(tmp, ring_s=8.0)
        _stream(small, "a", "One")
        c.check("a shorter ring caps lower",
                small._target_lag_cap() < cap, small._target_lag_cap())
        c.check("but never below the delay itself",
                _zone(tmp, ring_s=1.0)._target_lag_cap() >= 4.0)

    return c


if __name__ == "__main__":
    run()
