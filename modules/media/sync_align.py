"""
Alignment by ear, as a bisection (open-zone.md §7.7).

The trim below a device's reported position is not measurable from software
(latency_seed) and the acoustic sensor needs a mic (sync_chirp). What is left
is the listener, and the useful thing about a listener is that asking them for
a *number* wastes them: "which of these two fired first" is answered reliably
at a few milliseconds, while "how many milliseconds apart were they" is not
answered at all.

So the listener is used as a one-bit comparator and the search is a bisection.
Eight rounds take ±400 ms down to ~6 ms, which is below where the answer stops
being repeatable.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np

CLICK_S = 0.004          # transient enough that onset, not timbre, is judged
CLICK_AMP = 0.5          # matches the chirp's level in the PCM mix
CLICK_REPEATS = 3        # one pair is a guess; a train is a judgement
CLICK_GAP_S = 0.8

SPAN_MS = 400            # initial half-bracket
TOLERANCE_MS = 6         # below a listener's repeatability on clicks
MAX_ROUNDS = 8

SUBJECT, REFERENCE, TOGETHER = "subject", "reference", "together"


def click_train(rate: int, repeats: int = CLICK_REPEATS,
                gap_s: float = CLICK_GAP_S) -> np.ndarray:
    """Evenly spaced identical clicks, as one injectable wave.

    Identical by construction: both devices are handed this same array, so a
    difference in timbre cannot stand in for a difference in arrival.
    """
    n = int(CLICK_S * rate)
    rng = np.random.default_rng(0xC11C)          # fixed: every probe alike
    burst = rng.standard_normal(n)
    burst = CLICK_AMP * np.hanning(n) * burst / np.max(np.abs(burst))
    step = int(gap_s * rate)
    out = np.zeros(step * (repeats - 1) + n)
    for i in range(repeats):
        out[i * step:i * step + n] = burst
    return out


class Bisection:
    """Bracket on ``delta`` — the extra delay put on the subject.

    The subject is heard later than the reference by ``delta + E``, where E is
    the unknown difference in output latency. Coincidence is ``delta = -E``, so
    the answer the search converges on is added straight to the subject's trim.
    """

    def __init__(self, span_ms: int = SPAN_MS,
                 tolerance_ms: int = TOLERANCE_MS,
                 max_rounds: int = MAX_ROUNDS):
        self.lo = -int(span_ms)
        self.hi = int(span_ms)
        self.tolerance_ms = int(tolerance_ms)
        self.max_rounds = int(max_rounds)
        self.rounds = 0
        self.history: List[Tuple[int, str]] = []
        self._converged = False

    @property
    def delta_ms(self) -> int:
        return int(round((self.lo + self.hi) / 2.0))

    @property
    def done(self) -> bool:
        return (self._converged or self.rounds >= self.max_rounds
                or self.hi - self.lo <= self.tolerance_ms)

    @property
    def remaining(self) -> int:
        """Rounds still needed, for a progress bar that does not lie."""
        if self.done:
            return 0
        width, n = self.hi - self.lo, 0
        while width > self.tolerance_ms and n < self.max_rounds - self.rounds:
            width /= 2.0
            n += 1
        return n

    def answer(self, which: str) -> None:
        """Narrow on one judgement. ``TOGETHER`` ends the search: the listener
        cannot separate them, so the bracket is already inside their
        resolution and further rounds would be recording noise."""
        if self.done:
            return
        probe = self.delta_ms
        self.history.append((probe, which))
        self.rounds += 1
        if which == SUBJECT:
            self.lo = probe       # subject early: it needs more delay
        elif which == REFERENCE:
            self.hi = probe
        elif which == TOGETHER:
            half = self.tolerance_ms // 2
            self.lo, self.hi = probe - half, probe + half
            self._converged = True
        else:
            raise ValueError(f"unknown answer {which!r}")

    def result(self) -> Optional[int]:
        """The delta to fold into the subject's trim, or None if unconverged.

        A search that ran out of rounds without the bracket closing is refused
        rather than rounded off: inconsistent answers mean the two are not
        separable in the way the test assumes, and the midpoint of a bracket
        that never narrowed describes nothing.
        """
        if not self.done:
            return None
        if not self._converged and self.hi - self.lo > self.tolerance_ms:
            return None
        return self.delta_ms
