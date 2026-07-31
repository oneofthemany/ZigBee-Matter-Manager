"""
Variable-ratio resamplers for the OpenZone per-device pipeline (open-zone.md §4.2).

The actuator the sync loop drives is a ratio modulated a few hundred ppm around
unity: ``ratio = 1 + ε̂ + s(t)``. Two backends implement it, both reading the
master timeline by absolute sample position so a device's stream can be cut,
jumped or trimmed without the resampler needing to know.

``rust``   The default. Stateless windowed-sinc fractional-delay filter,
           evaluated in ``zmm_eq.interp_block``. Because the ratio never leaves
           ±1000 ppm of unity this is not really a sample-rate conversion — it
           is a fractional delay, and a delay filter can be evaluated at an
           arbitrary position with no history. That statelessness is worth a
           lot here: output length is exactly what was asked for, a jump is
           just a different position, and there is no filter memory to clear
           or latency to book-keep.

``sinc``   The same filter in numpy, and the automatic fallback where the
           ``zmm_eq`` wheel is absent. Identical output (agrees with the Rust
           path to float32 epsilon); it just allocates a frames×32×channels
           gather per block, which the Rust path does not.

This module used to bind ``libsoxr`` in variable-rate mode through ``ctypes``,
which was the reference path. It was removed after it aborted the process with
``double free or corruption (out)``: it handed raw numpy heap pointers to a C
library across a hand-declared ABI and freed a ``soxr_t`` by hand, from a
``close()`` that the session teardown could race against a live stream
generator. The replacement has no handle, no C ABI and no state, so that entire
class of failure is gone rather than narrowed. What was lost with it is real
but small: libsoxr's true variable-rate SRC tracked a +500 ppm command to
+498.87 ppm, where a fractional-delay filter is exact by construction because
it never resamples — it only reads the timeline at a different position.

``linear`` The original sub-sample linear interpolation. Measured −84 dBFS on
           the band-limited test signal, but its fraction-dependent HF response
           (−5 dB at 10 kHz at the interpolation midpoint) is audible on
           programme material. Kept for reproducing earlier measurements.

Every backend here is stateless, and all three satisfy §4.2's requirements
trivially: ratio resolution is float (≪ 0.1 ppm), the ratio may change every
block with no discontinuity because there is no state to discontinue, and
THD+N is set by the filter kernel, not by the ratio.
"""
from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger("modules.media.sync_resample")

try:
    import zmm_eq as _dsp
    _HAVE_RUST = hasattr(_dsp, "interp_block")
except ImportError:
    _dsp = None
    _HAVE_RUST = False

_WARNED_NO_RUST = False

# Windowed-sinc kernel. 32 taps of Lanczos-16 is flat to ~20 kHz at 44.1 kHz
# and puts image rejection below the s16 noise floor; the phase table quantises
# the fractional position to 1/2048 sample (≈ 11 ns), far below the ±2 ms
# synchronisation budget and below the interpolator's own error.
_HALF = 16
_TAPS = 2 * _HALF
_NPHASE = 2048


def _build_table() -> np.ndarray:
    """Kernel weights for every quantised fractional phase.

    For output position ``base + frac`` the taps sit at ``base + k`` for k in
    [−HALF+1, HALF], so the kernel argument is ``frac − k``. Rows are
    normalised to unity sum: that pins DC gain to exactly 1 at every phase, so
    a slewing ratio cannot produce phase-dependent level ripple."""
    ph = (np.arange(_NPHASE, dtype=np.float64) / _NPHASE)[:, None]
    k = np.arange(-_HALF + 1, _HALF + 1, dtype=np.float64)[None, :]
    x = ph - k
    w = np.sinc(x) * np.sinc(x / _HALF)          # sinc × Lanczos window
    w /= w.sum(axis=1, keepdims=True)
    return w.astype(np.float32)


_TABLE = _build_table()
_OFFS = np.arange(-_HALF + 1, _HALF + 1)

# Samples of context the kernel reads either side of its position. A reader
# positioned closer than this to the edge of a bounded source pulls zeros into
# the convolution, so callers clamp their start position by this margin.
READ_MARGIN = _HALF


def read_grid(source, i0: int, n: int, extra=None) -> np.ndarray:
    """``n`` samples of the master timeline from integer sample ``i0``, with any
    per-device timeline-domain injection mixed in.

    ``extra`` is the calibration chirp as ``(start_sample, mono_wave)``. It is
    mixed onto the integer grid *before* interpolation so it rides through the
    resampler exactly like programme material — which is the whole point: the
    chirp must measure the same path the audio takes.

    Requires ``source.read`` to return a fresh array rather than a view of its
    delay line: the chirp is mixed in with ``+=``, so a view would write the
    calibration signal permanently into the shared timeline and every other
    device would then play it too. Both sources allocate (``_Ring.read`` zero-
    fills a new block; the generated timeline is closed-form)."""
    src = source.read(i0, n)
    if extra is not None:
        c0, wave = extra
        a, b = max(i0, c0), min(i0 + n, c0 + len(wave))
        if a < b:
            src[a - i0:b - i0] += wave[a - c0:b - c0, None]
    return src


class _Stateless:
    """Positional interpolators. ``block`` consumes exactly ``adv`` timeline
    samples and returns exactly ``frames`` output samples."""

    stateful = False

    def block(self, source, pos: float, frames: int, adv: float,
              extra=None) -> tuple:
        idx = pos + np.arange(frames) * (adv / frames)
        base = np.floor(idx).astype(np.int64)
        frac = idx - base
        i0 = int(base[0])
        lo = i0 - _HALF + 1
        n = int(base[-1]) - i0 + 1 + _TAPS
        src = read_grid(source, lo, n, extra)
        return self._interp(src, base - lo, frac, frames), adv

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass


class LinearInterp(_Stateless):
    name = "linear"

    def _interp(self, src, rel, frac, frames):
        # `rel` is already relative to the window start, which includes the
        # kernel's left margin — do not offset it again.
        f = frac.astype(np.float32)[:, None]
        return src[rel] * (1.0 - f) + src[rel + 1] * f


class SincInterp(_Stateless):
    name = "sinc"

    def _interp(self, src, rel, frac, frames):
        ph = np.minimum((frac * _NPHASE).astype(np.int64), _NPHASE - 1)
        w = _TABLE[ph]                                    # (frames, TAPS)
        gather = src[rel[:, None] + _OFFS[None, :]]       # (frames, TAPS, ch)
        return np.einsum("ft,ftc->fc", w, gather, optimize=True)


class RustSinc(_Stateless):
    """The `sinc` filter evaluated in ``zmm_eq.interp_block``.

    ``block`` is overridden rather than ``_interp`` because the point of the
    Rust path is the temporaries it does *not* create: the numpy version
    materialises a (frames, 32, channels) gather — 2.2 MB per block at the
    stream block size — where this passes a window and a position and gets the
    finished block back."""

    name = "rust-sinc"

    def block(self, source, pos: float, frames: int, adv: float,
              extra=None) -> tuple:
        step = adv / frames
        i0 = int(np.floor(pos))
        last = int(np.floor(pos + (frames - 1) * step))
        # Window start carries the kernel's left margin; the +1 is slack so the
        # rightmost tap of the last output sample lands strictly inside.
        lo = i0 - _HALF + 1
        n = last - i0 + 1 + _TAPS
        src = np.ascontiguousarray(read_grid(source, lo, n, extra),
                                   dtype=np.float32)
        ch = src.shape[1]
        # `pos - lo` reduces an absolute timeline index (which reaches into the
        # hundreds of millions on a long session) to a window offset near zero
        # before it is used for phase arithmetic.
        raw = _dsp.interp_block(src, ch, pos - lo, step, frames)
        return np.frombuffer(raw, dtype="<f4").reshape(frames, ch), adv


_KINDS = {"rust": RustSinc, "rust-sinc": RustSinc,
          "sinc": SincInterp, "linear": LinearInterp}


def available() -> dict:
    return {"backend": "rust" if _HAVE_RUST else "numpy",
            "rust": _HAVE_RUST,
            "kinds": ["rust"] * _HAVE_RUST + ["sinc", "linear"]}


def make(kind: str, rate: int, channels: int):
    """Build a resampler.

    ``soxr`` is accepted as a deprecated alias so existing config keeps
    working — the libsoxr binding it named is gone (see the module docstring).
    The Rust backend falls back to the numpy filter when the ``zmm_eq`` wheel
    is absent: same output, more allocation, never a dead stream."""
    kind = (kind or "rust").lower()
    if kind == "soxr":
        kind = "rust"
    cls = _KINDS.get(kind)
    if cls is None:
        logger.warning(f"Unknown resampler '{kind}' — using windowed-sinc")
        return SincInterp()
    if cls is RustSinc and not _HAVE_RUST:
        global _WARNED_NO_RUST
        if not _WARNED_NO_RUST:      # once, not once per device per session
            _WARNED_NO_RUST = True
            logger.warning("zmm_eq wheel not installed — using numpy "
                           "windowed-sinc interpolation (identical output, "
                           "higher CPU)")
        return SincInterp()
    return cls()
