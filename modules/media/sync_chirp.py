"""
Acoustic chirp calibrator — the OpenZone 6.2 sensor.

Measures the audio in the air, which the status sensor cannot: each device
chirps in its own slot, a mic records, and GCC-PHAT recovers arrival times.
Differencing across devices cancels everything common, leaving true
misalignment. Pure DSP, numpy only, sounddevice imported lazily.
See docs/speaker_sync.md.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger("modules.media.sync_chirp")

CHIRP_S = 0.1            # chirp duration (open-zone.md §6.2: 100 ms log chirp)
CHIRP_F0 = 2000.0        # start frequency (Hz)
CHIRP_F1 = 8000.0        # end frequency (Hz) — above the test pad + click
CHIRP_AMP = 0.5          # linear amplitude in the PCM mix
CHIRP_GAP_S = 1.5        # slot spacing between devices
CHIRP_LEAD_S = 0.5       # schedule margin beyond the stream serve head
SEARCH_S = 0.7           # ± window around the expected arrival
MIN_PEAK_RATIO = 8.0     # correlation peak vs floor to accept a detection


def chirp_wave(rate: int) -> np.ndarray:
    """Hann-windowed logarithmic chirp, CHIRP_F0→CHIRP_F1 over CHIRP_S."""
    n = int(CHIRP_S * rate)
    t = np.arange(n) / rate
    k = (CHIRP_F1 / CHIRP_F0) ** (1.0 / CHIRP_S)
    phase = 2.0 * np.pi * CHIRP_F0 * (np.power(k, t) - 1.0) / np.log(k)
    return CHIRP_AMP * np.hanning(n) * np.sin(phase)


def record(duration_s: float, rate: int, device=None) -> np.ndarray:
    """Blocking mono capture (run via asyncio.to_thread)."""
    import sounddevice as sd
    frames = int(duration_s * rate)
    buf = sd.rec(frames, samplerate=rate, channels=1,
                 dtype="float32", device=device)
    sd.wait()
    return buf[:, 0]


def gcc_phat(sig: np.ndarray, template: np.ndarray,
             ) -> Tuple[Optional[float], float]:
    """GCC-PHAT arrival of ``template`` inside ``sig``.

    Returns (index, quality): fractional sample index in ``sig`` where the
    template starts (parabolic sub-sample interpolation around the peak),
    and the peak-to-floor ratio. PHAT whitening keeps the peak sharp in
    reverberant rooms (open-zone.md §8); the floor is the median |correlation|,
    so quality is directly comparable against MIN_PEAK_RATIO.
    """
    if len(sig) < 2 * len(template):
        return None, 0.0
    nfft = 1 << (len(sig) + len(template) - 1).bit_length()
    spec = np.fft.rfft(sig, nfft) * np.conj(np.fft.rfft(template, nfft))
    mag = np.abs(spec)
    spec /= np.maximum(mag, mag.max() * 1e-9 + 1e-30)
    cc = np.fft.irfft(spec, nfft)[: len(sig)]
    i = int(np.argmax(cc))
    quality = float(cc[i] / (np.median(np.abs(cc)) + 1e-12))
    idx = float(i)
    if 0 < i < len(cc) - 1:            # parabolic peak interpolation
        a, b, c = cc[i - 1], cc[i], cc[i + 1]
        den = a - 2.0 * b + c
        if den != 0.0:
            idx += 0.5 * (a - c) / den
    return idx, quality
