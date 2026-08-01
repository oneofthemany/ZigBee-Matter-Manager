"""
Variable-ratio resamplers for the OpenZone per-device pipeline (open-zone.md §4.2).
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

_HALF = 16
_TAPS = 2 * _HALF
_NPHASE = 2048


def _build_table() -> np.ndarray:
    """Kernel weights for every quantised fractional phase."""
    ph = (np.arange(_NPHASE, dtype=np.float64) / _NPHASE)[:, None]
    k = np.arange(-_HALF + 1, _HALF + 1, dtype=np.float64)[None, :]
    x = ph - k
    w = np.sinc(x) * np.sinc(x / _HALF)          # sinc × Lanczos window
    w /= w.sum(axis=1, keepdims=True)
    return w.astype(np.float32)


_TABLE = _build_table()
_OFFS = np.arange(-_HALF + 1, _HALF + 1)

READ_MARGIN = _HALF


def read_grid(source, i0: int, n: int, extra=None) -> np.ndarray:
    """``n`` samples of the master timeline from integer sample ``i0``, with any
    per-device timeline-domain injection mixed in.
    """
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
    """The `sinc` filter evaluated in ``zmm_eq.interp_block``."""

    name = "rust-sinc"

    def block(self, source, pos: float, frames: int, adv: float,
              extra=None) -> tuple:
        step = adv / frames
        i0 = int(np.floor(pos))
        last = int(np.floor(pos + (frames - 1) * step))
        lo = i0 - _HALF + 1
        n = last - i0 + 1 + _TAPS
        src = np.ascontiguousarray(read_grid(source, lo, n, extra),
                                   dtype=np.float32)
        ch = src.shape[1]
        raw = _dsp.interp_block(src, ch, pos - lo, step, frames)
        return np.frombuffer(raw, dtype="<f4").reshape(frames, ch), adv


_KINDS = {"rust": RustSinc, "rust-sinc": RustSinc,
          "sinc": SincInterp, "linear": LinearInterp}


def available() -> dict:
    return {"backend": "rust" if _HAVE_RUST else "numpy",
            "rust": _HAVE_RUST,
            "kinds": ["rust"] * _HAVE_RUST + ["sinc", "linear"]}


def make(kind: str, rate: int, channels: int):
    """Build a resampler."""
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
