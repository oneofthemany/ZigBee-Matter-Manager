"""
Variable-ratio resamplers for the OpenZone per-device pipeline (open-zone.md §4.2).

The actuator the sync loop drives is a ratio modulated a few hundred ppm around
unity: ``ratio = 1 + ε̂ + s(t)``. Two backends implement it, both reading the
master timeline by absolute sample position so a device's stream can be cut,
jumped or trimmed without the resampler needing to know.

``soxr``   libsoxr in variable-rate mode via ctypes. The reference path, and
           the default. libsoxr is already present in the image (ffmpeg links
           it), so this costs no build change and no new wheel — it ships as a
           code patch. VR mode is selected by passing ``SOXR_VR`` as the
           *flags* argument of ``soxr_quality_spec``; passing it as the recipe,
           or omitting it, makes ``soxr_set_io_ratio`` fail with "varying O/I
           ratio is not supported with this quality level". VR is available at
           every quality level, so we ask for VHQ.

``sinc``   Stateless windowed-sinc fractional-delay filter in numpy. Because
           the ratio never leaves ±1000 ppm of unity this is not really a
           sample-rate conversion — it is a fractional delay, and a delay
           filter can be evaluated at an arbitrary position with no history.
           That statelessness is worth a lot here: output length is exactly
           what was asked for, a jump is just a different position, and there
           is no filter memory to clear or latency to book-keep.

``linear`` The original sub-sample linear interpolation. Measured −84 dBFS on
           the band-limited test signal, but its fraction-dependent HF response
           (−5 dB at 10 kHz at the interpolation midpoint) is audible on
           programme material. Kept for reproducing earlier measurements.

Both stateless backends satisfy §4.2's requirements trivially: ratio resolution
is float (≪ 0.1 ppm), the ratio may change every block with no discontinuity
because there is no state to discontinue, and THD+N is set by the filter
kernel, not by the ratio.
"""
from __future__ import annotations

import ctypes as C
import logging
from typing import Optional

import numpy as np

logger = logging.getLogger("modules.media.sync_resample")

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
    chirp must measure the same path the audio takes."""
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


# ----------------------------------------------------------------------
# libsoxr variable-rate (ctypes)
# ----------------------------------------------------------------------
SOXR_FLOAT32_I = 0
SOXR_VHQ = 6
SOXR_VR = 32                  # quality-spec FLAGS bit, not a recipe


class _IoSpec(C.Structure):
    _fields_ = [("itype", C.c_int), ("otype", C.c_int), ("scale", C.c_double),
                ("e", C.c_void_p), ("flags", C.c_ulong)]


class _QualitySpec(C.Structure):
    _fields_ = [("precision", C.c_double), ("phase_response", C.c_double),
                ("passband_end", C.c_double), ("stopband_begin", C.c_double),
                ("e", C.c_void_p), ("flags", C.c_ulong)]


def _load_soxr():
    for name in ("libsoxr.so.0", "libsoxr.so"):
        try:
            lib = C.CDLL(name)
        except OSError:
            continue
        lib.soxr_io_spec.restype = _IoSpec
        lib.soxr_io_spec.argtypes = [C.c_int, C.c_int]
        lib.soxr_quality_spec.restype = _QualitySpec
        lib.soxr_quality_spec.argtypes = [C.c_ulong, C.c_ulong]
        lib.soxr_create.restype = C.c_void_p
        lib.soxr_create.argtypes = [C.c_double, C.c_double, C.c_uint,
                                    C.POINTER(C.c_char_p), C.POINTER(_IoSpec),
                                    C.POINTER(_QualitySpec), C.c_void_p]
        lib.soxr_process.restype = C.c_char_p
        lib.soxr_process.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t,
                                     C.POINTER(C.c_size_t), C.c_void_p,
                                     C.c_size_t, C.POINTER(C.c_size_t)]
        lib.soxr_set_io_ratio.restype = C.c_char_p
        lib.soxr_set_io_ratio.argtypes = [C.c_void_p, C.c_double, C.c_size_t]
        lib.soxr_delay.restype = C.c_double
        lib.soxr_delay.argtypes = [C.c_void_p]
        lib.soxr_clear.restype = C.c_char_p
        lib.soxr_clear.argtypes = [C.c_void_p]
        lib.soxr_delete.argtypes = [C.c_void_p]
        lib.soxr_version.restype = C.c_char_p
        return lib
    return None


_SOXR = _load_soxr()


def soxr_version() -> str:
    return _SOXR.soxr_version().decode() if _SOXR else ""


class SoxrVR:
    """One libsoxr VR instance per device stream.

    Unlike the stateless backends this holds filter memory, so it cannot be
    evaluated at an arbitrary position: it is fed the timeline sequentially and
    told what ratio to run at. Two consequences the caller must respect —
    ``block`` returns how many timeline samples it *actually* consumed (which
    is only asymptotically ``adv``, because libsoxr buffers internally), and a
    deliberate jump must call ``reset`` so stale filter memory is not carried
    across the discontinuity."""

    stateful = True
    name = "soxr"

    def __init__(self, rate: int, channels: int, quality: int = SOXR_VHQ):
        if _SOXR is None:
            raise RuntimeError("libsoxr not available")
        self._ch = channels
        self._rate = rate
        err = C.c_char_p()
        io = _SOXR.soxr_io_spec(SOXR_FLOAT32_I, SOXR_FLOAT32_I)
        q = _SOXR.soxr_quality_spec(quality, SOXR_VR)
        self._h = _SOXR.soxr_create(float(rate), float(rate), channels,
                                    C.byref(err), C.byref(io), C.byref(q), None)
        if not self._h:
            raise RuntimeError(f"soxr_create failed: {err.value!r}")
        self._carry = np.zeros((0, channels), dtype=np.float32)
        self._ratio = 1.0

    def _set_ratio(self, ratio: float) -> None:
        if ratio != self._ratio:
            e = _SOXR.soxr_set_io_ratio(self._h, float(ratio), 0)
            if e:
                raise RuntimeError(f"soxr_set_io_ratio: {e.decode()}")
            self._ratio = ratio

    def block(self, source, pos: float, frames: int, adv: float,
              extra=None) -> tuple:
        # io_ratio is input-per-output: consuming `adv` timeline samples to
        # emit `frames` output samples is exactly adv/frames.
        self._set_ratio(adv / frames)
        out = [self._carry] if len(self._carry) else []
        have = len(self._carry)
        consumed = 0.0
        read_at = int(np.floor(pos))
        frac0 = pos - read_at
        # Honour the sub-sample part of `pos` once, on entry: libsoxr tracks
        # phase itself from there on.
        if frac0 and not len(self._carry):
            read_at += 1
            consumed += 1.0 - frac0
        guard = 0
        while have < frames:
            want = max(1024, int(round((frames - have) * self._ratio)) + 64)
            src = np.ascontiguousarray(read_grid(source, read_at, want, extra))
            buf = np.zeros((want + 256, self._ch), dtype=np.float32)
            idone, odone = C.c_size_t(), C.c_size_t()
            e = _SOXR.soxr_process(self._h, src.ctypes.data, want,
                                   C.byref(idone), buf.ctypes.data, len(buf),
                                   C.byref(odone))
            if e:
                raise RuntimeError(f"soxr_process: {e.decode()}")
            read_at += idone.value
            consumed += idone.value
            if odone.value:
                out.append(buf[:odone.value])
                have += odone.value
            guard += 1
            if guard > 64:
                raise RuntimeError("soxr_process made no progress")
        merged = np.concatenate(out) if len(out) > 1 else out[0]
        self._carry = merged[frames:].copy()
        return merged[:frames], consumed

    def delay_samples(self) -> float:
        return float(_SOXR.soxr_delay(self._h)) if self._h else 0.0

    def reset(self) -> None:
        if self._h:
            _SOXR.soxr_clear(self._h)
            self._ratio = 1.0
        self._carry = np.zeros((0, self._ch), dtype=np.float32)

    def close(self) -> None:
        if self._h:
            _SOXR.soxr_delete(self._h)
            self._h = None


_KINDS = {"linear": LinearInterp, "sinc": SincInterp}


def available() -> dict:
    return {"soxr": _SOXR is not None, "soxr_version": soxr_version(),
            "kinds": ["soxr"] * bool(_SOXR) + list(_KINDS)}


def make(kind: str, rate: int, channels: int):
    """Build a resampler. ``soxr`` falls back to the windowed-sinc filter when
    libsoxr is missing — degraded per §4.2, but never a dead stream."""
    kind = (kind or "soxr").lower()
    if kind == "soxr":
        if _SOXR is not None:
            try:
                return SoxrVR(rate, channels)
            except Exception as e:
                logger.warning(f"libsoxr VR unavailable ({e}) — using windowed-sinc")
        else:
            logger.warning("libsoxr not found — using windowed-sinc interpolation")
        return SincInterp()
    cls = _KINDS.get(kind)
    if cls is None:
        logger.warning(f"Unknown resampler '{kind}' — using windowed-sinc")
        return SincInterp()
    return cls()
