"""
OpenZone: alignment by ear (open-zone.md §7.7).

The real Bisection, the real align_* engine calls and the real click injection
run. Stubbed is the listener, simulated from a known true offset so the test
can assert the search converges on it, and the device, which only has to hold
an injection slot the generator would read.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time

import numpy as np

from harness import Checker, REPO  # noqa: F401  (REPO sets sys.path)

from modules.media import sync_align as al
from modules.media.cast_sync import OpenZone, _Stream, RATE
from modules.media.sync_align import Bisection, REFERENCE, SUBJECT, TOGETHER


class _FakeSource:
    delay_s = 4.0

    def earliest_sample(self) -> int:
        return -10 ** 9

    def latest_sample(self) -> int:
        return 10 ** 9


def _zone(tmp: str) -> OpenZone:
    z = OpenZone(None, {
        "trims_file": f"{tmp}/trims.json",
        "model_trims_file": f"{tmp}/model_trims.json",
        "groups_file": f"{tmp}/groups.json",
        "model_file": f"{tmp}/model.json",
        "trim_graph_file": f"{tmp}/graph.json",
    })
    z.running = True
    z._source = _FakeSource()
    z._epoch = time.monotonic() - 100.0
    z._target_lag = 8.0
    return z


def _stream(z: OpenZone, sid: str, name: str, trim_ms: int = 0) -> _Stream:
    st = _Stream(sid, f"cast:{sid}", name)
    st.pos = 0.0
    st.connected = True
    st.trim_ms = trim_ms
    z._streams[sid] = st
    return st


def _listener(truth_ms: int):
    """A listener whose speakers truly coincide at ``delta == truth_ms``,
    perfect down to the tolerance and unable to separate them below it."""
    def judge(delta_ms: int) -> str:
        err = delta_ms - truth_ms
        if abs(err) < al.TOLERANCE_MS / 2:
            return TOGETHER
        return REFERENCE if err > 0 else SUBJECT
    return judge


def run() -> Checker:
    c = Checker("zone_align")
    loop = asyncio.new_event_loop()

    # --- the bisection alone ---------------------------------------------
    c.section("a one-bit listener is enough")
    for truth in (0, 81, -137, 399):
        b = Bisection()
        while not b.done:
            d = b.delta_ms
            b.answer(SUBJECT if d < truth else REFERENCE)
        c.check(f"converges on {truth:+d} ms",
                b.result() is not None
                and abs(b.result() - truth) <= al.TOLERANCE_MS,
                (truth, b.result()))
    c.check("inside eight rounds", b.rounds <= al.MAX_ROUNDS, b.rounds)

    c.section('"together" ends the search where it stands')
    b = Bisection()
    b.answer(SUBJECT)
    b.answer(TOGETHER)
    c.check("done after two", b.done and b.rounds == 2)
    c.check("the answer is where the listener stopped being able to tell",
            b.result() == 200, b.result())

    c.section("an unconverged search refuses to answer")
    b = Bisection(max_rounds=3)
    for _ in range(3):
        b.answer(SUBJECT if b.rounds % 2 == 0 else REFERENCE)
    c.check("out of rounds with the bracket still open", b.done)
    c.check("no number is invented", b.result() is None, b.result())

    c.section("the probe train is identical on both devices")
    w = al.click_train(RATE)
    n = int(al.CLICK_S * RATE)
    c.check("every click in the train is the same sample",
            np.array_equal(w[:n], w[int(al.CLICK_GAP_S * RATE):
                                     int(al.CLICK_GAP_S * RATE) + n]))
    c.check("two builds agree — timbre cannot stand in for timing",
            np.array_equal(w, al.click_train(RATE)))

    # --- the engine -------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        z = _zone(tmp)
        ref = _stream(z, "r", "Kitchen")
        subj = _stream(z, "s", "Lounge", trim_ms=40)

        c.section("a probe schedules both trains, offset by the bracket")
        out = loop.run_until_complete(
            z.align_start(ref.player_id, subj.player_id))
        c.check("started", out.get("success") and out.get("running"), out)
        c.check("both devices carry an injection",
                ref.inject is not None and subj.inject is not None)
        gap_ms = (subj.inject[0] - ref.inject[0]) / RATE * 1000.0
        c.check("their separation is the probed delta",
                abs(gap_ms - out["delta_ms"]) < 1.0, (gap_ms, out["delta_ms"]))
        c.check("the clicks land ahead of both readers",
                ref.inject[0] > max(ref.pos, subj.pos), ref.inject[0])
        c.check("the reported trim tracks the bracket, not the device",
                out["trim_ms"] == 40 + out["delta_ms"], out)

        c.section("answering drives the search to a trim")
        judge = _listener(-95)
        guard = 0
        while out.get("running") and guard < 20:
            out = loop.run_until_complete(z.align_answer(judge(out["delta_ms"])))
            guard += 1
        c.check("it finished", out.get("done") is True, out)
        c.check("and applied a trim", out.get("applied") is True, out)
        c.check("landing within tolerance of the truth",
                abs(out["trim_ms"] - (40 - 95)) <= al.TOLERANCE_MS, out)
        c.check("the trim is the one the device now carries",
                z.trim_ms(subj.player_id) == out["trim_ms"])
        c.check("the injections are cleared",
                ref.inject is None and subj.inject is None)
        c.check("no session is left running",
                z.align_status() == {"running": False})

        c.section("replay re-fires without consuming a round")
        out = loop.run_until_complete(
            z.align_start(ref.player_id, subj.player_id))
        before = out["round"]
        out = loop.run_until_complete(z.align_answer("replay"))
        c.check("same round", out["round"] == before, (before, out["round"]))
        c.check("same delta", out["delta_ms"] == 0, out)

        c.section("cancelling leaves the trim alone")
        held = z.trim_ms(subj.player_id)
        loop.run_until_complete(z.align_answer(SUBJECT))
        c.check("cancelled", z.align_cancel().get("cancelled") is True)
        c.check("trim untouched", z.trim_ms(subj.player_id) == held)
        c.check("clicks stopped", subj.inject is None)
        c.check("a second cancel is refused",
                z.align_cancel().get("success") is False)

        c.section("the guards")
        c.check("one speaker cannot be aligned against itself",
                loop.run_until_complete(
                    z.align_start(ref.player_id,
                                  ref.player_id)).get("success") is False)
        c.check("nor a device that is not in the zone",
                loop.run_until_complete(
                    z.align_start(ref.player_id,
                                  "cast:absent")).get("success") is False)
        c.check("an answer with no session is refused",
                loop.run_until_complete(
                    z.align_answer(SUBJECT)).get("success") is False)
        loop.run_until_complete(z.align_start(ref.player_id, subj.player_id))
        c.check("an unknown answer is refused",
                loop.run_until_complete(
                    z.align_answer("maybe")).get("success") is False)
        c.check("and does not end the session",
                z.align_status().get("running") is True)
        subj.connected = False
        c.check("a speaker leaving mid-search ends it",
                loop.run_until_complete(
                    z.align_answer(SUBJECT)).get("success") is False)
        z.align_cancel()

        c.section("a finished alignment feeds the differential graph")
        os.makedirs(tmp + "/g", exist_ok=True)
        z2 = _zone(tmp + "/g")
        a = _stream(z2, "a", "Hub")
        b2 = _stream(z2, "b", "Streamer")
        z2._model_key = lambda pid: {"cast:a": "Google Nest Hub",
                                     "cast:b": "WiiM Pro"}.get(pid, "")
        z2._trims["cast:a"] = 219
        out = loop.run_until_complete(z2.align_start(a.player_id, b2.player_id))
        judge = _listener(300)
        guard = 0
        while out.get("running") and guard < 20:
            out = loop.run_until_complete(z2.align_answer(judge(out["delta_ms"])))
            guard += 1
        c.check("the subject's trim was applied", out.get("applied") is True, out)
        # The settle task is what feeds the graph; drive its body directly
        # rather than waiting out STREAM_TRIM_SETTLE_S.
        pending = list(z2._trim_learn_tasks.values())
        for t in pending:
            t.cancel()
        loop.run_until_complete(asyncio.gather(*pending,
                                               return_exceptions=True))
        z2._graph.observe_session(
            [(z2._model_key(pid), int(v)) for pid, v in z2._trims.items()])
        edges = z2._graph.describe()
        c.check("an edge now relates the two models", len(edges) == 1, edges)
        c.check("carrying the difference the listener found",
                abs(edges[0]["delta_ms"]) == abs(219 - z2._trims["cast:b"]),
                (edges, z2._trims))

    loop.close()
    return c


if __name__ == "__main__":
    run()
