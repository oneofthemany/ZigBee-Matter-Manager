"""
OpenZone: a reload re-measures its device instead of re-seating from the model
(open-zone.md §7.5).

The real `_reload_stream`, `_probe_reload`, `_end_probe` and `_seat_position`
are exercised. What is stubbed is everything outside the engine: the cast
provider, the LOAD, and the device's reported media time — which is simulated
from a known pipeline latency so the lag the monitor *would* read back is
computable, and the test can assert on alignment rather than on bookkeeping.
"""

from __future__ import annotations

import asyncio
import time

from harness import Checker, REPO  # noqa: F401  (REPO sets sys.path)

from modules.media import cast_sync
from modules.media.cast_sync import OpenZone, _Stream, RATE


class _FakeSource:
    """Delay line stand-in: unbounded, so seats are never clamped."""

    def __init__(self, delay_s: float = 4.0):
        self.delay_s = delay_s

    def earliest_sample(self) -> int:
        return -10 ** 9

    def latest_sample(self) -> int:
        return 10 ** 9


# Session start measured this device at 0.40 s and seated it on an 8.34 s
# target against a 4.0 s delay line: precomp = target - delay - latency.
PRECOMP_AT_START = 8.34 - 4.0 - 0.40


def _zone(tmp: str, delay_s: float = 4.0) -> OpenZone:
    z = OpenZone(None, {
        "trims_file": f"{tmp}/trims.json",
        "model_trims_file": f"{tmp}/model_trims.json",
        "groups_file": f"{tmp}/groups.json",
        "model_file": f"{tmp}/model.json",
    })
    z.running = True
    z._source = _FakeSource(delay_s)
    z._epoch = time.monotonic() - 100.0
    return z


def _stream(z: OpenZone, sid: str, name: str, latency_s: float) -> _Stream:
    st = _Stream(sid, f"cast:{sid}", name)
    st.pos = 0.0
    st.natural_lag = 1.0
    st.connected = True
    z._streams[sid] = st
    st._true_latency = latency_s          # test-only: what the device will do
    return st


def _lag_if_seated_now(z: OpenZone, st: _Stream, latency_s: float) -> float:
    """The lag the monitor would read once this device plays its first sample.

    Mirrors _measure_lag_once against a device whose pipeline delays the seated
    stream by `latency_s`, which is the identity §7.5 rests on.
    """
    t_play0 = time.monotonic() + latency_s
    ct = 0.0                                    # reading taken at first sample
    played = (st.start_pos + st.shift) / RATE + ct
    return (t_play0 - z._epoch) - played - st.trim_ms / 1000.0


def _patch_device(z: OpenZone, launched: list) -> None:
    """Stub the LOAD and the media-time sensor for every stream."""

    async def launch(player_id, sid, gate="full"):
        launched.append(sid)
        st = z._streams[sid]
        st.opened_at = time.monotonic()      # the fetch the generator opens
        st.preroll_frames = 0

    async def read(st):
        if st.opened_at is None:
            return None
        elapsed = time.monotonic() - st.opened_at
        ct = elapsed - st._true_latency      # media time trails by the pipeline
        if ct <= 0:
            return None                      # still filling: no report yet
        return time.monotonic(), ct

    z._launch_stream = launch
    z._read_media_time = read


def run() -> Checker:
    c = Checker("zone_reload_probe")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        _probe_lands_on_target(c, tmp)
        _model_path_misses(c, tmp)
        _timeout_falls_back(c, tmp)
        _timeout_before_fetch_still_seats(c, tmp)
        _no_target_skips_probe(c, tmp)
        _monitor_skips_probing(c, tmp)
        _realign_probes_and_mutes(c, tmp)
        _realign_seats_from_spread(c, tmp)
        _realign_keeps_model_and_parked_clean(c, tmp)
        _realign_silence_is_bounded(c, tmp)
    return c


def _realign_silence_is_bounded(c: Checker, tmp: str) -> None:
    c.section("one budget bounds the whole re-align, lead included")

    async def go():
        z = _zone(tmp)
        z._target_lag = 8.34
        z._fade_start = time.monotonic() - 60
        st = _stream(z, "a", "Kitchen", latency_s=0.5)
        _patch_device(z, [])

        async def silent(_st):
            return None                     # never settles: the worst case
        z._read_media_time = silent

        cast_sync.STREAM_REALIGN_MAX_S = 2.0
        try:
            began = time.monotonic()
            await z._realign_group("test")
            c.check("budget narrowed for the re-align",
                    z._acquire_max_s == 2.0, z._acquire_max_s)
            await asyncio.wait_for(z._preroll_task, timeout=20)
            lead = time.monotonic() - began
            c.check(f"lead capped ({lead:.1f}s)", lead < 4.0, lead)
            # The lead is part of the silence, so the acquisition after it must
            # inherit the same origin rather than restart the clock.
            c.check("budget not restarted at the end of the lead",
                    z._acquire_from <= began + 0.05,
                    (z._acquire_from - began))
            # Budget already spent by the lead → content fades in at once.
            c.check("fades in rather than muting again",
                    z._acquire_gain(128) is None or z._fade_start is not None)
        finally:
            cast_sync.STREAM_REALIGN_MAX_S = 12.0

    asyncio.run(go())


def _realign_probes_and_mutes(c: Checker, tmp: str) -> None:
    c.section("a re-align re-enters the pre-roll and mutes the zone")

    async def go():
        z = _zone(tmp)
        z._target_lag = 8.34
        z._fade_start = time.monotonic() - 60      # session long since faded in
        a = _stream(z, "a", "Kitchen", latency_s=0.50)
        b = _stream(z, "b", "Study", latency_s=1.30)
        launched = []
        _patch_device(z, launched)

        await z._realign_group("test")
        c.check("pre-roll re-entered", z._preroll is True)
        c.check("flagged as a re-align", z._preroll_realign is True)
        c.check("target cleared", z._target_lag is None)
        c.check("every device re-LOADed", sorted(launched) == ["a", "b"],
                launched)
        c.check("zone muted", z._fade_start is None)
        gain = z._acquire_gain(128)
        c.check("...and the gain is actually zero",
                gain is not None and float(gain.max()) == 0.0, gain)
        # Muting is what buys silence over the echo, so it must not be
        # reachable only via the un-probed timeout path.
        c.check("readers not seated from the model",
                a.start_pos == 0 and b.start_pos == 0,
                (a.start_pos, b.start_pos))
        z._preroll = False
        if z._preroll_task:
            z._preroll_task.cancel()

    asyncio.run(go())


def _realign_seats_from_spread(c: Checker, tmp: str) -> None:
    c.section("the probe's spread, not the model, sets pre-compensation")

    async def go():
        z = _zone(tmp)
        z._target_lag = 8.34
        z._fade_start = time.monotonic() - 60
        a = _stream(z, "a", "Kitchen", latency_s=0.50)
        b = _stream(z, "b", "Study", latency_s=1.30)
        # Stale model figures that disagree with what the devices now do.
        for st in (a, b):
            z._model[st.player_id] = {"lag_s": 0.10}
        launched = []
        _patch_device(z, launched)

        await z._realign_group("test")
        await asyncio.wait_for(z._preroll_task, timeout=25)

        c.check("pre-roll ended", z._preroll is False)
        c.check("re-align flag cleared", z._preroll_realign is False)
        c.check("slowest device gets no pre-comp",
                abs(b.precomp_s) < 0.05, b.precomp_s)
        c.check("fastest is pre-compensated by the spread",
                abs(a.precomp_s - 0.80) < 0.05, a.precomp_s)
        # Only the differences are an alignment: both devices must emit the
        # same timeline sample at the same wall-clock instant.
        skew = ((a.precomp_s + a._true_latency)
                - (b.precomp_s + b._true_latency))
        c.check(f"inter-device skew {skew * 1000:.0f} ms", abs(skew) < 0.05,
                skew)
        c.check("fade released by the settled probe", z._fade_start is not None)

    asyncio.run(go())


def _realign_keeps_model_and_parked_clean(c: Checker, tmp: str) -> None:
    c.section("a re-align teaches the model nothing and ignores parked units")

    async def go():
        z = _zone(tmp)
        z._target_lag = 8.34
        z._fade_start = time.monotonic() - 60
        a = _stream(z, "a", "Kitchen", latency_s=0.50)
        b = _stream(z, "b", "Study", latency_s=1.30)
        # Absent, holding a stale probe from session start that would otherwise
        # define the group's slowest device.
        gone = _stream(z, "z", "Shed", latency_s=9.0)
        gone.parked_since = time.monotonic()
        gone.probe_hist = [9.0] * cast_sync.PREROLL_READS
        before = {k: dict(v) for k, v in z._model.items()}
        launched = []
        _patch_device(z, launched)

        await z._realign_group("test")
        await asyncio.wait_for(z._preroll_task, timeout=25)

        c.check("parked device not re-LOADed", "z" not in launched, launched)
        c.check("parked stale probe did not set the spread",
                abs(a.precomp_s - 0.80) < 0.05, a.precomp_s)
        c.check("model untouched", z._model == before,
                {k: v for k, v in z._model.items() if before.get(k) != v})

    asyncio.run(go())


def _probe_lands_on_target(c: Checker, tmp: str) -> None:
    c.section("a probed reload seats the device on the group's target")

    async def go():
        z = _zone(tmp, delay_s=4.0)
        z._target_lag = 8.34
        st = _stream(z, "a", "Kitchen", latency_s=1.20)
        # Same stale seat as the case above, which the probe has to correct.
        st.precomp_s = PRECOMP_AT_START
        launched = []
        _patch_device(z, launched)

        await z._reload_stream(st)
        c.check("probe armed", st.probing is True)
        c.check("seat deferred while probing", st.start_pos == 0, st.start_pos)
        await asyncio.wait_for(st.probe_task, timeout=20)

        c.check("probe cleared", st.probing is False)
        c.check("latency recovered", abs(st.latency_s - 1.20) < 0.05,
                st.latency_s)
        # The generator would now leave the lead and seat; do that directly,
        # with no lead served (preroll_frames == 0), which is _seat_position.
        z._seat_after_preroll(st, z._source)
        lag = _lag_if_seated_now(z, st, st._true_latency)
        c.check(f"lands on target ({lag:.3f}s vs 8.340s)",
                abs(lag - z._target_lag) < 0.05, lag)

    asyncio.run(go())


def _model_path_misses(c: Checker, tmp: str) -> None:
    c.section("the path it replaces carries the stale pre-comp onto content")

    async def go():
        z = _zone(tmp, delay_s=4.0)
        z._target_lag = 8.34
        # Session start measured 0.40 s and seated this device on target. The
        # disturbance that earned the reload left it buffering 1.20 s — the
        # session-to-session spread §3 records for exactly these devices.
        st = _stream(z, "a", "Kitchen", latency_s=1.20)
        st.precomp_s = PRECOMP_AT_START
        z._seat_position(st, z._source)
        lag = _lag_if_seated_now(z, st, st._true_latency)
        err = lag - z._target_lag
        c.check(f"unprobed seat is {err * 1000:.0f} ms off target",
                abs(err - 0.80) < 0.05, err)
        c.check("...which is past the step rung's forward authority",
                abs(err) > cast_sync.STREAM_JUMP_MIN_S, err)

    asyncio.run(go())


def _timeout_falls_back(c: Checker, tmp: str) -> None:
    c.section("a device that never reports is seated from the model")

    async def go():
        z = _zone(tmp)
        z._target_lag = 8.34
        st = _stream(z, "b", "Study", latency_s=1.0)
        st.precomp_s = 0.77
        launched = []
        _patch_device(z, launched)

        async def silent(_st):
            return None                      # never reports
        z._read_media_time = silent
        cast_sync.STREAM_PROBE_MAX_S = 1.0   # keep the test quick
        try:
            await z._reload_stream(st)
            await asyncio.wait_for(st.probe_task, timeout=10)
        finally:
            cast_sync.STREAM_PROBE_MAX_S = 15.0
        c.check("probe released", st.probing is False)
        c.check("model pre-comp untouched", abs(st.precomp_s - 0.77) < 1e-9,
                st.precomp_s)

    asyncio.run(go())


def _timeout_before_fetch_still_seats(c: Checker, tmp: str) -> None:
    c.section("a probe that ends before the device fetches still re-seats")

    async def go():
        z = _zone(tmp)
        z._target_lag = 8.34
        st = _stream(z, "e", "Porch", latency_s=1.0)
        st.start_pos = -12345               # the pre-reload seat
        st.pos = -12345.0

        async def never_fetch(player_id, sid, gate="full"):
            pass                             # LOAD lands nowhere
        z._launch_stream = never_fetch

        async def silent(_st):
            return None
        z._read_media_time = silent

        cast_sync.STREAM_PROBE_MAX_S = 1.0
        try:
            await z._reload_stream(st)
            await asyncio.wait_for(st.probe_task, timeout=10)
        finally:
            cast_sync.STREAM_PROBE_MAX_S = 15.0

        # The generator never ran, so nothing else can have seated this reader.
        c.check("reader re-seated anyway", st.start_pos != -12345, st.start_pos)
        c.check("shift zeroed by the seat", st.shift == 0.0, st.shift)

    asyncio.run(go())


def _no_target_skips_probe(c: Checker, tmp: str) -> None:
    c.section("no target to aim at → the old path, unchanged")

    async def go():
        z = _zone(tmp)
        z._target_lag = None                 # as just after a re-align
        st = _stream(z, "c", "Hall", latency_s=1.0)
        launched = []
        _patch_device(z, launched)
        await z._reload_stream(st)
        c.check("not probing", st.probing is False)
        c.check("seated immediately", st.start_pos != 0, st.start_pos)
        c.check("no probe task", st.probe_task is None)

    asyncio.run(go())


def _monitor_skips_probing(c: Checker, tmp: str) -> None:
    c.section("a probing device is out of measurement and the sweeps")

    async def go():
        z = _zone(tmp)
        z._target_lag = 8.34
        st = _stream(z, "d", "Den", latency_s=1.0)
        st.probing = True
        items = [(sid, s) for sid, s in z._streams.items()
                 if s.connected and s.pos is not None
                 and s.parked_since is None and not s.probing]
        c.check("excluded from the poll", items == [], items)

        # Silent for longer than the sweep's threshold, but by design.
        st.last_lag_at = time.monotonic() - cast_sync.STREAM_SILENT_MAX_S - 5
        escalated = []
        z._escalate_shortfall = lambda s, r: escalated.append(r)
        z._sweep_silent()
        c.check("silence sweep skips it", escalated == [], escalated)

        # Held out of playback for longer than the interruption threshold.
        st.interrupted_since = time.monotonic() - 30
        st.cooldown_until = 0.0
        reloads = []
        z._reload_stream = lambda s: reloads.append(s.sid)
        z._sweep_interrupted()
        c.check("interruption sweep skips it", reloads == [], reloads)

    asyncio.run(go())


if __name__ == "__main__":
    ck = run()
    print(f"\n{ck.passed} passed, {len(ck.failures)} failed")
    for f in ck.failures:
        print(f"  FAIL {f}")
    raise SystemExit(1 if ck.failures else 0)
