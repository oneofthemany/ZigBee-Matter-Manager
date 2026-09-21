"""
OpenZone: a correction is not re-decided until it has landed, and a late
joiner is not seated somewhere it cannot be corrected from (§7.1, §7.5).

The fixture is a live fault: one speaker slewed ±30-71 ms every 20-60 s for
27 minutes without converging, while its peers in the same zone slewed once
every 26 minutes. A slew is applied to the stream, so for as long as it takes
to serve and play out, the measured error still shows what the slew was issued
for — and re-deciding on that reading issues it a second time.
"""

from __future__ import annotations

import tempfile
import time

from harness import Checker, REPO  # noqa: F401  (REPO sets sys.path)

from modules.media import cast_sync as cs
from modules.media.cast_sync import OpenZone, _Stream, RATE

# The slews that speaker actually produced, in order.
OBSERVED_MS = [66, -68, 50, -39, -45, 32, -31, 33, 57, -71, 37, -30, 48, -41,
               36, 34, 49, -43, -33, 37, -31, -34, -51, 35, 71, -36, -37, 33]


class _FakeSource:
    """Delay line stand-in that tracks the timeline, so the seat bounds mean
    what they do in a live session: the write head runs ``delay_s`` ahead of
    the play point and the ring holds ``span_s`` of history behind it."""

    delay_s = 4.0

    def __init__(self, epoch: float, span_s: float = 20.0):
        self._epoch = epoch
        self._span = span_s

    def _head(self) -> int:
        return int((time.monotonic() - self._epoch + self.delay_s) * RATE)

    def earliest_sample(self) -> int:
        return self._head() - int(self._span * RATE)

    def latest_sample(self) -> int:
        return self._head()


def _zone(tmp: str) -> OpenZone:
    z = OpenZone(None, {
        "trims_file": f"{tmp}/t.json", "model_trims_file": f"{tmp}/mt.json",
        "groups_file": f"{tmp}/g.json", "model_file": f"{tmp}/m.json",
        "trim_graph_file": f"{tmp}/gr.json",
    })
    z.running = True
    z._epoch = time.monotonic() - 100.0
    z._source = _FakeSource(z._epoch)
    z._target_lag = 11.01
    return z


def _stream(z: OpenZone, sid: str, name: str, latency_s: float = 0.662):
    st = _Stream(sid, f"cast:{sid}", name)
    st.pos, st.connected = 0.0, True
    st.latency_s = latency_s
    z._streams[sid] = st
    return st


def run() -> Checker:
    c = Checker("zone_slew_settle")

    with tempfile.TemporaryDirectory() as tmp:
        z = _zone(tmp)
        st = _stream(z, "w", "WiiM Ultra")

        c.section("a slew is given time to reach the speaker")
        st.slew_s = 0.066
        hold = z._slew_cooldown_s(st)
        # Only the portion above the fast threshold is served quickly; the
        # rest drains gently by design.
        fast_s = (0.066 - cs.STREAM_SLEW_FAST_THRESH_S) / (cs.STREAM_SLEW_FAST_PPM / 1e6)
        c.check("it covers serving the fast part",
                hold > fast_s, (hold, fast_s))
        c.check("plus the device's own pipeline",
                hold >= fast_s + st.latency_s, hold)
        c.check("and is bounded so the ladder is never blind for long",
                hold <= cs.STREAM_COOLDOWN_MAX_S, hold)

        c.section("the hold scales with the correction, not a constant")
        st.slew_s = 0.031
        small = z._slew_cooldown_s(st)
        st.slew_s = 0.071
        large = z._slew_cooldown_s(st)
        c.check("a small correction waits briefly", small < 5.0, small)
        c.check("a large one waits longer", large > small, (small, large))

        c.section("every observed slew is held past its own flight time")
        worst = float("-inf")
        for ms in OBSERVED_MS:
            st.slew_s = ms / 1000.0
            flight = (max(0.0, abs(st.slew_s) - cs.STREAM_SLEW_FAST_THRESH_S)
                      / (cs.STREAM_SLEW_FAST_PPM / 1e6))
            hold = z._slew_cooldown_s(st)
            worst = max(worst, flight - hold)
        c.check("none can be re-authorised mid-flight", worst < 0.0, worst)

        c.section("a correction inside the quiet band is not held at all")
        st.slew_s = 0.010
        c.check("no fast portion to wait for",
                z._slew_cooldown_s(st) <= st.latency_s + cs.STREAM_POLL_S,
                z._slew_cooldown_s(st))

        # --- late joiner ---------------------------------------------------
        c.section("a seat inside the delay line is accepted")
        j = _stream(z, "j", "Late Joiner")
        c.check("a modest pre-comp fits", z._seat_fits(j, 1.0))
        c.check("so does the one the WiiM actually took",
                z._seat_fits(j, 6.345))

        c.section("a seat the timeline cannot hold is refused")
        c.check("a joiner slower than the target needs newer audio than exists",
                not z._seat_fits(j, -30.0), "negative pre-comp")
        c.check("and one needing more history than the ring holds",
                not z._seat_fits(j, 400.0))

        c.section("returning from absent does not spend the re-align budget")
        r = _stream(z, "r", "Returned")
        r.reloads_since_align = 0
        import asyncio
        loop = asyncio.new_event_loop()
        z._launch_stream = lambda *a, **k: asyncio.sleep(0)
        loop.run_until_complete(z._reload_stream(r, rejoin=True))
        c.check("a rejoin reload is not a strike", r.reloads_since_align == 0,
                r.reloads_since_align)
        c.check("but it is still counted for the health readout",
                r.reloads == 1, r.reloads)
        loop.run_until_complete(z._reload_stream(r))
        c.check("an ordinary reload still is a strike",
                r.reloads_since_align == 1, r.reloads_since_align)
        c.check("so the zone still re-aligns for a device it cannot reach",
                r.reloads_since_align < cs.STREAM_RELOADS_BEFORE_REALIGN)
        loop.close()

    return c


if __name__ == "__main__":
    run()
