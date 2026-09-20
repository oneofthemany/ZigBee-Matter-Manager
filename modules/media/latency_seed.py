"""
Shipped output-latency priors, keyed by model (open-zone.md §7.4).

A trim carries the chain *below* the media position a device reports — output
buffer, DSP, DAC, amp, transducer. The pre-roll probe measures everything down
to that reported clock; no control protocol exposes what is under it, so the
trim is not measurable from software and must be told once. It is a constant
of the hardware, so values established elsewhere ship here rather than being
rediscovered per install.

Lowest precedence in ``OpenZone.trim_ms``: explicit per-device, then learned
here, then this.
"""
from __future__ import annotations

import re
from typing import Dict

# model (as a human would write it) -> trim ms
MODEL_TRIM_MS: Dict[str, int] = {
    # Screened Cast devices: a longer pipeline than the speakers, consistently.
    "Google Nest Hub": 219,
    "Google Nest Hub Max": 219,
    "Pixel Tablet": 219,
    # LinkPlay streamers.
    "WiiM Pro": 300,
    "WiiM Pro Plus": 300,
    "WiiM Ultra": 300,
    "WiiM Amp": 300,
}

# LinkPlay's trailing ``_with_<chip>`` marks a silicon revision, not a
# different output path, so it is not part of identity.
_QUALIFIER = re.compile(r"[_-]with[_-].*$", re.IGNORECASE)


def normalise(model: str) -> str:
    """Fold to the matched form: one model names itself differently per
    ecosystem — Cast a marketing name, LinkPlay a build string — differing in
    case, separators and that qualifier."""
    s = _QUALIFIER.sub("", (model or "").strip())
    return " ".join(re.split(r"[\s_-]+", s.lower())).strip()


_TABLE = {normalise(k): v for k, v in MODEL_TRIM_MS.items()}


def trim_ms(model_key: str) -> int:
    """The shipped prior for this model, or 0 when there is none."""
    return _TABLE.get(normalise(model_key), 0)
