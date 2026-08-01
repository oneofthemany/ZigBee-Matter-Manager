//! zmm_eq — the server-side audio DSP for Cast playback. Independent pieces,
//! sharing a crate because they share a wheel and a build marker:
//!
//!   * `EqChain`      — 10-band graphic equaliser over interleaved s16le PCM.
//!   * `interp_block` — the OpenZone §4.2 variable-ratio resampler.
//!   * `xfade_mix`    — the OpenZone §4.1a item-boundary crossfade.
//!
//! # 1. Equaliser
//!
//! One `EqChain` filters one audio stream: an RBJ-cookbook biquad cascade on
//! the ISO octave centres (31 Hz … 16 kHz) — low shelf at the bottom, high
//! shelf at the top, peaking bells between, the same topology as the
//! browser-side Web Audio EQ so both ends of the app sound identical.
//!
//! Built for LIVE control: `set_gains` recomputes coefficients under a lock
//! while each band's delay-line state is preserved, so a slider drag lands on
//! the running stream in the next block with no click and no restart. The
//! preamp (auto headroom: −largest boost) is smoothed per-sample for the same
//! reason. `set_enabled(false)` is a bit-transparent bypass that keeps state
//! flowing, so re-enabling is seamless too.
//!
//! The hot loop runs with the GIL released; a 16 KiB block is a
//! few tens of microseconds, so filtering costs the server essentially
//! nothing next to the ffmpeg decode that feeds it.
//!
//! # 2. Resampler
//!
//! `interp_block` is the per-device variable-ratio resampler the multi-room
//! sync loop drives (`ratio = 1 + ε̂ + s(t)`, a few hundred ppm either side of
//! unity). It replaces a `ctypes` binding to libsoxr, and the reason is
//! memory safety rather than speed: that binding handed raw numpy heap
//! pointers to a C library and freed a `soxr_t` by hand, which is the one
//! shape of code in this app that can — and did — abort the process with
//! `double free or corruption`.
//!
//! Because the ratio never leaves ±1000 ppm this is not really sample-rate
//! conversion, it is a *fractional delay*, and a delay filter can be evaluated
//! at an arbitrary position with no history. So this is a pure function:
//! no instance, no handle, no filter memory, nothing to free and nothing to
//! reset across a jump. Two concurrent callers cannot corrupt each other
//! because there is no shared state to corrupt.
//!
//! The kernel is the same 32-tap Lanczos-16 windowed sinc the numpy `sinc`
//! backend uses, with the fractional position quantised to 1/2048 sample
//! (≈ 11 ns at 44.1 kHz — far below the ±2 ms sync budget). Rows are
//! normalised to unit sum so DC gain is exactly 1 at every phase and a
//! slewing ratio cannot produce phase-dependent level ripple.

use pyo3::buffer::PyBuffer;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::collections::VecDeque;
use std::f64::consts::PI;
use std::sync::{Mutex, OnceLock};

/// ISO octave band centres — must match the UI's band list.
const BANDS: [f64; 10] = [
    31.0, 62.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0,
];
const NBANDS: usize = BANDS.len();
const CHANNELS: usize = 2;
/// Bell width for the peaking bands (~one octave, matches the Web Audio EQ).
const PEAK_Q: f64 = 1.25;
/// Preamp smoothing time constant, seconds (glitch-free gain changes).
const PREAMP_TAU: f64 = 0.05;

#[derive(Clone, Copy, Default)]
struct Coeffs {
    b0: f64,
    b1: f64,
    b2: f64,
    a1: f64,
    a2: f64,
}

#[derive(Clone, Copy, Default)]
struct BiquadState {
    x1: f64,
    x2: f64,
    y1: f64,
    y2: f64,
}

impl BiquadState {
    #[inline(always)]
    fn tick(&mut self, c: &Coeffs, x: f64) -> f64 {
        let y = c.b0 * x + c.b1 * self.x1 + c.b2 * self.x2 - c.a1 * self.y1 - c.a2 * self.y2;
        self.x2 = self.x1;
        self.x1 = x;
        self.y2 = self.y1;
        self.y1 = y;
        y
    }
}

/// RBJ Audio-EQ-Cookbook coefficients. `kind`: 0 = low shelf, 1 = peaking,
/// 2 = high shelf. Shelf slope S = 1.
fn rbj(kind: u8, freq: f64, gain_db: f64, q: f64, fs: f64) -> Coeffs {
    let a = 10f64.powf(gain_db / 40.0);
    let w0 = 2.0 * PI * (freq.min(fs * 0.45)) / fs;
    let (sw, cw) = (w0.sin(), w0.cos());
    let (b0, b1, b2, a0, a1, a2) = match kind {
        1 => {
            let alpha = sw / (2.0 * q);
            (
                1.0 + alpha * a,
                -2.0 * cw,
                1.0 - alpha * a,
                1.0 + alpha / a,
                -2.0 * cw,
                1.0 - alpha / a,
            )
        }
        0 => {
            let alpha = sw / 2.0 * 2f64.sqrt(); // S = 1
            let sa = 2.0 * a.sqrt() * alpha;
            (
                a * ((a + 1.0) - (a - 1.0) * cw + sa),
                2.0 * a * ((a - 1.0) - (a + 1.0) * cw),
                a * ((a + 1.0) - (a - 1.0) * cw - sa),
                (a + 1.0) + (a - 1.0) * cw + sa,
                -2.0 * ((a - 1.0) + (a + 1.0) * cw),
                (a + 1.0) + (a - 1.0) * cw - sa,
            )
        }
        _ => {
            let alpha = sw / 2.0 * 2f64.sqrt(); // S = 1
            let sa = 2.0 * a.sqrt() * alpha;
            (
                a * ((a + 1.0) + (a - 1.0) * cw + sa),
                -2.0 * a * ((a - 1.0) + (a + 1.0) * cw),
                a * ((a + 1.0) + (a - 1.0) * cw - sa),
                (a + 1.0) - (a - 1.0) * cw + sa,
                2.0 * ((a - 1.0) - (a + 1.0) * cw),
                (a + 1.0) - (a - 1.0) * cw - sa,
            )
        }
    };
    Coeffs {
        b0: b0 / a0,
        b1: b1 / a0,
        b2: b2 / a0,
        a1: a1 / a0,
        a2: a2 / a0,
    }
}

fn band_kind(i: usize) -> u8 {
    match i {
        0 => 0,
        n if n == NBANDS - 1 => 2,
        _ => 1,
    }
}

struct Inner {
    coeffs: [Coeffs; NBANDS],
    /// true = this band is exactly 0 dB and can be skipped entirely
    flat: [bool; NBANDS],
    state: [[BiquadState; NBANDS]; CHANNELS],
    preamp: f64,        // current linear preamp (smoothed toward target)
    preamp_target: f64, // linear; −(largest boost) dB
    enabled: bool,
}

#[pyclass]
pub struct EqChain {
    inner: Mutex<Inner>,
    fs: f64,
    smooth: f64, // per-sample preamp smoothing factor
}

#[pymethods]
impl EqChain {
    #[new]
    fn new(sample_rate: f64) -> PyResult<Self> {
        if !(8000.0..=192000.0).contains(&sample_rate) {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "sample_rate out of range",
            ));
        }
        let mut coeffs = [Coeffs::default(); NBANDS];
        for (i, f) in BANDS.iter().enumerate() {
            coeffs[i] = rbj(band_kind(i), *f, 0.0, PEAK_Q, sample_rate);
        }
        Ok(Self {
            inner: Mutex::new(Inner {
                coeffs,
                flat: [true; NBANDS],
                state: [[BiquadState::default(); NBANDS]; CHANNELS],
                preamp: 1.0,
                preamp_target: 1.0,
                enabled: true,
            }),
            fs: sample_rate,
            smooth: 1.0 - (-1.0 / (PREAMP_TAU * sample_rate)).exp(),
        })
    }

    /// Band count / centre frequencies, so Python never hardcodes them.
    #[staticmethod]
    fn bands() -> Vec<f64> {
        BANDS.to_vec()
    }

    /// Live-retune: recompute all ten biquads for `gains` (dB, clamped ±24)
    /// and retarget the auto-preamp. Filter state is deliberately kept — a
    /// coefficient swap on a live stream is inaudible, resetting state is not.
    fn set_gains(&self, gains: Vec<f64>) -> PyResult<()> {
        if gains.len() != NBANDS {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "expected {NBANDS} gains, got {}",
                gains.len()
            )));
        }
        let mut inner = self.inner.lock().unwrap();
        let mut max_boost: f64 = 0.0;
        for (i, g) in gains.iter().enumerate() {
            let g = g.clamp(-24.0, 24.0);
            inner.coeffs[i] = rbj(band_kind(i), BANDS[i], g, PEAK_Q, self.fs);
            inner.flat[i] = g.abs() < 1e-3;
            max_boost = max_boost.max(g);
        }
        inner.preamp_target = 10f64.powf(-max_boost / 20.0);
        Ok(())
    }

    /// Bit-transparent bypass; the stream keeps flowing either way.
    fn set_enabled(&self, on: bool) {
        self.inner.lock().unwrap().enabled = on;
    }

    fn is_enabled(&self) -> bool {
        self.inner.lock().unwrap().enabled
    }

    /// Filter one block of interleaved s16le stereo. Returns a same-sized
    /// block. Trailing odd bytes (a torn frame at EOF) pass through untouched.
    fn process<'py>(&self, py: Python<'py>, data: Vec<u8>) -> PyResult<Bound<'py, PyBytes>> {
        let out = py.detach(move || self.run(data));
        Ok(PyBytes::new(py, &out))
    }
}

impl EqChain {
    fn run(&self, mut data: Vec<u8>) -> Vec<u8> {
        let mut inner = self.inner.lock().unwrap();
        if !inner.enabled {
            return data;
        }
        let inner = &mut *inner;
        let frame = 2 * CHANNELS; // bytes per frame
        let usable = data.len() - data.len() % frame;
        let mut i = 0;
        while i < usable {
            // The preamp eases toward its target so gain jumps never click.
            inner.preamp += (inner.preamp_target - inner.preamp) * self.smooth;
            for ch in 0..CHANNELS {
                let o = i + ch * 2;
                let raw = i16::from_le_bytes([data[o], data[o + 1]]);
                let mut s = raw as f64 / 32768.0 * inner.preamp;
                for b in 0..NBANDS {
                    if inner.flat[b] {
                        continue;
                    }
                    s = inner.state[ch][b].tick(&inner.coeffs[b], s);
                }
                let v = (s * 32767.0).clamp(-32768.0, 32767.0) as i16;
                let le = v.to_le_bytes();
                data[o] = le[0];
                data[o + 1] = le[1];
            }
            i += frame;
        }
        data
    }
}

// ----------------------------------------------------------------------
// Variable-ratio resampler (open-zone.md §4.2)
// ----------------------------------------------------------------------

/// Taps either side of the interpolation position.
const HALF: usize = 16;
const TAPS: usize = 2 * HALF;
/// Quantisation of the fractional position: 1/2048 sample.
const NPHASE: usize = 2048;

fn sinc(x: f64) -> f64 {
    if x.abs() < 1e-12 {
        1.0
    } else {
        let p = PI * x;
        p.sin() / p
    }
}

/// Kernel weights for every quantised phase, built once.
///
/// For output position `base + frac` the taps sit at `base + k` for k in
/// [−HALF+1, HALF], so the kernel argument is `frac − k`.
fn phase_table() -> &'static [[f32; TAPS]; NPHASE] {
    static TABLE: OnceLock<Box<[[f32; TAPS]; NPHASE]>> = OnceLock::new();
    TABLE.get_or_init(|| {
        let mut table = Box::new([[0f32; TAPS]; NPHASE]);
        for (p, row) in table.iter_mut().enumerate() {
            let ph = p as f64 / NPHASE as f64;
            let mut w = [0f64; TAPS];
            let mut sum = 0.0;
            for (t, wt) in w.iter_mut().enumerate() {
                let k = t as f64 - (HALF as f64 - 1.0);
                let x = ph - k;
                *wt = sinc(x) * sinc(x / HALF as f64); // sinc × Lanczos window
                sum += *wt;
            }
            // Unit sum pins DC gain to exactly 1 at every phase.
            for (t, wt) in w.iter().enumerate() {
                row[t] = (*wt / sum) as f32;
            }
        }
        table
    })
}

/// Interpolate one output block out of a window of the master timeline.
///
/// `src` is an interleaved, C-contiguous float32 window of `channels`-wide
/// frames — the caller reads it off the ring and is responsible for placing
/// it (see `sync_resample.RustSinc.block`). `pos` is the fractional position
/// of output sample 0 *relative to the first frame of `src`*, and `step` is
/// the timeline advance per output sample (the commanded ratio). Returns
/// `frames * channels` little-endian float32 samples.
///
/// Passing `pos` window-relative rather than absolute is deliberate: a
/// timeline position is hundreds of millions of samples in by the end of a
/// long session, and reducing it against the window start keeps the phase
/// arithmetic in the region where an f64 has bits to spare.
#[pyfunction]
#[pyo3(signature = (src, channels, pos, step, frames))]
fn interp_block<'py>(
    py: Python<'py>,
    src: PyBuffer<f32>,
    channels: usize,
    pos: f64,
    step: f64,
    frames: usize,
) -> PyResult<Bound<'py, PyBytes>> {
    if channels == 0 {
        return Err(PyValueError::new_err("channels must be > 0"));
    }
    if !pos.is_finite() || !step.is_finite() {
        return Err(PyValueError::new_err("pos and step must be finite"));
    }
    // to_vec() enforces C-contiguity and the float32 format, so the window is
    // a plain owned slice from here on — no borrowed view into a numpy buffer
    // that Python could free while the GIL is released below.
    let flat = src.to_vec(py)?;
    if flat.len() % channels != 0 {
        return Err(PyValueError::new_err(
            "source is not a whole number of frames",
        ));
    }
    let n = flat.len() / channels;
    if n < TAPS {
        return Err(PyValueError::new_err(format!(
            "source window needs at least {TAPS} frames, got {n}"
        )));
    }
    let table = phase_table();
    let out = py.detach(move || {
        let mut out = vec![0u8; frames * channels * 4];
        let mut acc = vec![0f64; channels];
        for f in 0..frames {
            let p = pos + f as f64 * step;
            let base = p.floor();
            let ph = (((p - base) * NPHASE as f64) as usize).min(NPHASE - 1);
            let w = &table[ph];
            // Leftmost source frame this output sample reads. Clamped per tap
            // to the window: with a correctly placed window it never bites,
            // and if the caller ever gets the placement wrong the result is
            // edge-padding rather than an out-of-bounds read.
            let start = base as isize - (HALF as isize - 1);
            acc.iter_mut().for_each(|a| *a = 0.0);
            for (t, &wt) in w.iter().enumerate() {
                let j = (start + t as isize).clamp(0, n as isize - 1) as usize;
                let frame = &flat[j * channels..(j + 1) * channels];
                for (c, a) in acc.iter_mut().enumerate() {
                    *a += wt as f64 * frame[c] as f64;
                }
            }
            let o = (f * channels) * 4;
            for (c, a) in acc.iter().enumerate() {
                out[o + c * 4..o + c * 4 + 4].copy_from_slice(&(*a as f32).to_le_bytes());
            }
        }
        out
    });
    Ok(PyBytes::new(py, &out))
}

// ----------------------------------------------------------------------
// Item-boundary crossfade (open-zone.md §4.1a)
// ----------------------------------------------------------------------

/// Look-ahead gain envelope holding `need` without stepping the level:
/// centred running minimum over `w`, Hann-smoothed, clamped back under `need`.
/// Both stages O(n) — monotonic deque, and `k[d] = (0.5 − 0.5cos(ωd))/S` split
/// into a box term and two quadrature terms (open-zone.md §4.1a).
fn guard_envelope(need: &[f32], w: usize) -> Vec<f32> {
    let n = need.len();
    let pad = w / 2;
    let m = n + 2 * pad;
    let edge = |x: &[f32], i: isize| x[i.clamp(0, x.len() as isize - 1) as usize];

    let mut dq: VecDeque<(usize, f32)> = VecDeque::new();
    let mut rmin: Vec<f32> = Vec::with_capacity(n);
    for i in 0..m {
        let v = edge(need, i as isize - pad as isize);
        while dq.back().is_some_and(|&(_, x)| x >= v) {
            dq.pop_back();
        }
        dq.push_back((i, v));
        if dq[0].0 + w <= i {
            dq.pop_front();
        }
        if i + 1 >= w {
            rmin.push(dq[0].1);
        }
    }

    let om = 2.0 * PI / (w - 1) as f64;
    let s = 0.5 * w as f64 - 0.5;
    let mut pa: Vec<f64> = Vec::with_capacity(m + 1);
    let mut pc: Vec<f64> = Vec::with_capacity(m + 1);
    let mut ps: Vec<f64> = Vec::with_capacity(m + 1);
    pa.push(0.0);
    pc.push(0.0);
    ps.push(0.0);
    for i in 0..m {
        let x = edge(&rmin, i as isize - pad as isize) as f64;
        let (sn, cs) = (om * i as f64).sin_cos();
        pa.push(pa[i] + x);
        pc.push(pc[i] + x * cs);
        ps.push(ps[i] + x * sn);
    }
    (0..n)
        .map(|j| {
            let (av, cv, dv) = (pa[j + w] - pa[j], pc[j + w] - pc[j], ps[j + w] - ps[j]);
            let (sn, cs) = (om * j as f64).sin_cos();
            let g = 0.5 / s * (av - cs * cv - sn * dv);
            (g as f32).min(need[j])
        })
        .collect()
}

fn mix_guarded(
    a: &[f32],
    b: &[f32],
    ch: usize,
    n: usize,
    p: f64,
    peak_ceil: f32,
    w: usize,
) -> Vec<f32> {
    let mut mixed = vec![0f32; n * ch];
    let mut amp = vec![0f32; n];
    let mut src_peak = 0f32;
    let denom = if n > 1 { (n - 1) as f64 } else { 1.0 };
    for i in 0..n {
        let t = if n > 1 { i as f64 / denom } else { 0.0 };
        let (sn, cs) = (PI * t / 2.0).sin_cos();
        // Clamped before the power: cos(π/2) lands a hair below zero.
        let g_out = cs.max(0.0).powf(p) as f32;
        let g_in = sn.max(0.0).powf(p) as f32;
        let mut peak = 0f32;
        for c in 0..ch {
            let (x, y) = (a[i * ch + c], b[i * ch + c]);
            src_peak = src_peak.max(x.abs()).max(y.abs());
            let v = x * g_out + y * g_in;
            mixed[i * ch + c] = v;
            peak = peak.max(v.abs());
        }
        amp[i] = peak;
    }
    let ceiling = peak_ceil.max(src_peak);
    if amp.iter().any(|&v| v > ceiling) {
        let need: Vec<f32> = amp
            .iter()
            .map(|&v| (ceiling / v.max(1e-9)).min(1.0))
            .collect();
        let g = guard_envelope(&need, w);
        for i in 0..n {
            for c in 0..ch {
                mixed[i * ch + c] *= g[i];
            }
        }
    }
    mixed
}

/// Mix one item boundary: fade `tail` out under `head` in, peak-guarded
/// (open-zone.md §4.1a).
///
/// `tail` and `head` are interleaved, C-contiguous float32 blocks of equal
/// length. `p` is the fade exponent (1 = constant power, 2 = constant
/// amplitude), `peak_ceil` the floor under the guard's ceiling, `guard_win`
/// its odd look-ahead width. Returns `frames * channels` LE float32 samples.
#[pyfunction]
#[pyo3(signature = (tail, head, channels, p, peak_ceil, guard_win))]
fn xfade_mix<'py>(
    py: Python<'py>,
    tail: PyBuffer<f32>,
    head: PyBuffer<f32>,
    channels: usize,
    p: f64,
    peak_ceil: f32,
    guard_win: usize,
) -> PyResult<Bound<'py, PyBytes>> {
    if channels == 0 {
        return Err(PyValueError::new_err("channels must be > 0"));
    }
    if guard_win < 3 || guard_win % 2 == 0 {
        return Err(PyValueError::new_err("guard_win must be odd and >= 3"));
    }
    if !p.is_finite() || p <= 0.0 {
        return Err(PyValueError::new_err("p must be finite and > 0"));
    }
    let a = tail.to_vec(py)?;
    let b = head.to_vec(py)?;
    if a.len() != b.len() {
        return Err(PyValueError::new_err("tail and head differ in length"));
    }
    if a.len() % channels != 0 {
        return Err(PyValueError::new_err(
            "blocks are not a whole number of frames",
        ));
    }
    let n = a.len() / channels;
    if n == 0 {
        return Err(PyValueError::new_err("empty overlap"));
    }
    let out = py.detach(move || {
        let mixed = mix_guarded(&a, &b, channels, n, p, peak_ceil, guard_win);
        let mut out = vec![0u8; mixed.len() * 4];
        for (i, v) in mixed.iter().enumerate() {
            out[i * 4..i * 4 + 4].copy_from_slice(&v.to_le_bytes());
        }
        out
    });
    Ok(PyBytes::new(py, &out))
}

#[pymodule]
fn zmm_eq(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<EqChain>()?;
    m.add_function(wrap_pyfunction!(interp_block, m)?)?;
    m.add_function(wrap_pyfunction!(xfade_mix, m)?)?;
    // The window margin the kernel reads either side of its position, so
    // Python never hardcodes it.
    m.add("RESAMPLE_HALF", HALF)?;
    m.add("RESAMPLE_TAPS", TAPS)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The O(n·w) forms `guard_envelope` replaces.
    fn guard_envelope_direct(need: &[f32], w: usize) -> Vec<f32> {
        let n = need.len();
        let pad = w / 2;
        let edge = |x: &[f32], i: isize| x[i.clamp(0, x.len() as isize - 1) as usize];
        let rmin: Vec<f32> = (0..n)
            .map(|i| {
                (0..w).fold(f32::INFINITY, |acc, d| {
                    acc.min(edge(need, (i + d) as isize - pad as isize))
                })
            })
            .collect();
        let kr: Vec<f64> = (0..w)
            .map(|d| 0.5 - 0.5 * (2.0 * PI * d as f64 / (w - 1) as f64).cos())
            .collect();
        let ks: f64 = kr.iter().sum();
        (0..n)
            .map(|j| {
                let g: f64 = (0..w)
                    .map(|d| edge(&rmin, (j + d) as isize - pad as isize) as f64 * kr[d] / ks)
                    .sum();
                (g as f32).min(need[j])
            })
            .collect()
    }

    fn lcg(seed: &mut u64) -> f32 {
        *seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        ((*seed >> 33) as f32 / (1u64 << 31) as f32) - 1.0
    }

    #[test]
    fn envelope_matches_direct_form() {
        let mut seed = 7u64;
        for &(n, w) in &[(400, 21), (400, 5), (1000, 51), (300, 221), (64, 21), (5, 3)] {
            let need: Vec<f32> = (0..n).map(|_| 0.5 + 0.5 * lcg(&mut seed).abs()).collect();
            let fast = guard_envelope(&need, w);
            let slow = guard_envelope_direct(&need, w);
            let d = fast
                .iter()
                .zip(&slow)
                .fold(0f32, |acc, (a, b)| acc.max((a - b).abs()));
            assert!(d < 1e-6, "n={n} w={w}: max |fast - direct| = {d:e}");
        }
    }

    #[test]
    fn envelope_never_exceeds_need_and_is_positive() {
        let mut seed = 11u64;
        let need: Vec<f32> = (0..2000).map(|_| 0.2 + 0.8 * lcg(&mut seed).abs()).collect();
        let g = guard_envelope(&need, 221);
        for (i, &v) in g.iter().enumerate() {
            assert!(v > 0.0 && v <= need[i] + 1e-6, "i={i}: g={v} need={}", need[i]);
        }
    }

    #[test]
    fn mix_holds_the_louder_source_peak() {
        let (n, ch) = (44100, 2);
        let mut seed = 3u64;

        let a: Vec<f32> = (0..n * ch).map(|_| 0.9 * lcg(&mut seed)).collect(); // uncorrelated
        let b: Vec<f32> = (0..n * ch).map(|_| 0.9 * lcg(&mut seed)).collect();
        let src = a.iter().chain(&b).fold(0f32, |m, v| m.max(v.abs()));
        let unguarded = {
            let mut m = 0f32;
            let d = (n - 1) as f64;
            for i in 0..n {
                let (sn, cs) = (PI * i as f64 / d / 2.0).sin_cos();
                for c in 0..ch {
                    let v = a[i * ch + c] * cs as f32 + b[i * ch + c] * sn as f32;
                    m = m.max(v.abs());
                }
            }
            m
        };
        let out = mix_guarded(&a, &b, ch, n, 1.0, 0.97, 221);
        let got = out.iter().fold(0f32, |m, v| m.max(v.abs()));
        assert!(unguarded > src * 1.05, "test signal must actually overshoot");
        assert!(got <= src.max(0.97) + 1e-4, "guard failed: {got} > {src}");
    }

    #[test]
    fn mix_endpoints_are_the_sources_at_unity() {
        let (n, ch) = (1000, 2);
        let a = vec![0.5f32; n * ch];
        let b = vec![-0.25f32; n * ch];
        let out = mix_guarded(&a, &b, ch, n, 1.0, 0.97, 21);
        assert!((out[0] - 0.5).abs() < 1e-6, "start = {}", out[0]);
        assert!((out[out.len() - 1] + 0.25).abs() < 1e-6, "end = {}", out[out.len() - 1]);
    }
}
