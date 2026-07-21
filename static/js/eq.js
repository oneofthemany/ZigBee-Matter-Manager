/**
 * eq.js — 10-band graphic equaliser for "This device" playback.
 *
 * Web Audio chain, built once and kept for the page's lifetime:
 *   <audio> → MediaElementSource → preamp → 10 × BiquadFilter → analyser → out
 *
 * Bands are the ISO octave centres every hardware graphic EQ uses
 * (31 Hz … 16 kHz): low shelf at the bottom, high shelf at the top, peaking
 * in between. Presets are the iTunes/industry-canonical curves mapped onto
 * those bands. "Bypass" zeroes every filter rather than rewiring the graph —
 * a 0 dB biquad is bit-transparent, and never re-routing avoids glitches.
 *
 * Clipping guard: boosting bands adds headroom risk, so the preamp always
 * sits at −(largest boost) dB. Cuts never cost loudness.
 *
 * CORS reality (see local-player.js): a MediaElementSource on a cross-origin
 * stream without CORS headers outputs pure silence, so the local player only
 * routes elements created with crossorigin="anonymous" through this graph,
 * and falls back to a plain, un-EQ'd element when a stream refuses CORS.
 */
const log = zmmLog('eq');

export const BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
export const BAND_LABELS = ['31', '62', '125', '250', '500', '1k', '2k', '4k', '8k', '16k'];
export const GAIN_MIN = -12, GAIN_MAX = 12;

// Industry-standard curves (the iTunes preset canon, in dB on the ISO bands),
// plus "Late Night" for quiet-hours listening. Order is the menu order.
export const PRESETS = {
    'Flat':           [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    'Acoustic':       [5, 5, 4, 1, 2, 2, 3.5, 4, 3.5, 2],
    'Bass Booster':   [5.5, 4.5, 3.5, 2.5, 1.5, 0, 0, 0, 0, 0],
    'Bass Reducer':   [-5.5, -4.5, -3.5, -2.5, -1.5, 0, 0, 0, 0, 0],
    'Classical':      [4.5, 3.5, 3, 2.5, -1.5, -1.5, 0, 2, 3, 3.5],
    'Dance':          [3.5, 6.5, 5, 0, 2, 3.5, 5, 4.5, 3.5, 0],
    'Deep':           [5, 3.5, 1.5, 1, 3, 2.5, 1.5, -2, -3.5, -4.5],
    'Electronic':     [4.5, 4, 1.5, 0, -2, 2, 1, 1.5, 4, 4.5],
    'Hip-Hop':        [5, 4, 1.5, 3, -1, -1, 1.5, -0.5, 2, 3],
    'Jazz':           [4, 3, 1.5, 2, -1.5, -1.5, 0, 1.5, 3, 3.5],
    'Latin':          [4.5, 3, 0, 0, -1.5, -1.5, -1.5, 0, 3, 4.5],
    'Loudness':       [6, 4, 0, 0, -2, 0, -1, -4.5, 5, 1],
    'Lounge':         [-3, -1.5, -0.5, 1.5, 4, 2.5, 0, -1.5, 2, 1],
    'Piano':          [3, 2, 0, 2.5, 3, 1.5, 3.5, 4.5, 3, 3.5],
    'Pop':            [-1.5, -1, 0, 2, 4, 4, 2, 0, -1, -1.5],
    'R&B':            [3, 7, 5.5, 1.5, -3, -1.5, 2.5, 3, 3, 3.5],
    'Rock':           [5, 4, 3, 1.5, -0.5, -1, 0.5, 2.5, 3.5, 4.5],
    'Small Speakers': [5.5, 4.5, 3.5, 2.5, 1.5, 0, -1.5, -2.5, -3.5, -4.5],
    'Spoken Word':    [-3.5, -0.5, 0, 0.5, 3.5, 4.5, 4.5, 4.5, 2.5, 0],
    'Treble Booster': [0, 0, 0, 0, 0, 1.5, 2.5, 3.5, 4.5, 5.5],
    'Treble Reducer': [0, 0, 0, 0, 0, -1.5, -2.5, -3.5, -4.5, -5.5],
    'Vocal Booster':  [-1.5, -3, -3, 1.5, 4, 4, 3, 1.5, 0, -1.5],
    'Late Night':     [4, 3, 1.5, 0.5, 0, 0, -1, -2, -3.5, -5],
};

const STORE_KEY = 'zmm.eq.local';
const RAMP_S = 0.05;          // param smoothing — kills zipper noise on drags

let _ctx = null;              // AudioContext (page-lifetime singleton)
let _preamp = null;
let _filters = [];            // BiquadFilterNodes, one per band
let _analyser = null;
let _source = null;           // current MediaElementSource
let _sources = new WeakMap(); // element → its MediaElementSource (once, ever)
let _attachedEl = null;

// Settings survive reloads; sliders come back exactly where they were left.
let _s = (() => {
    try {
        const v = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
        const gains = Array.isArray(v.gains) && v.gains.length === BANDS.length
            ? v.gains.map(g => _clamp(g)) : PRESETS.Flat.slice();
        return { enabled: !!v.enabled, preset: v.preset || 'Flat', gains };
    } catch { return { enabled: false, preset: 'Flat', gains: PRESETS.Flat.slice() }; }
})();

function _clamp(g) { return Math.max(GAIN_MIN, Math.min(GAIN_MAX, Number(g) || 0)); }
function _save() { localStorage.setItem(STORE_KEY, JSON.stringify(_s)); }

function _buildGraph() {
    if (_ctx) return;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) { log.warn('Web Audio unsupported — EQ unavailable'); return; }
    _ctx = new AC();
    _preamp = _ctx.createGain();
    _filters = BANDS.map((f, i) => {
        const biq = _ctx.createBiquadFilter();
        // Shelves on the edge bands, peaking in between — standard graphic-EQ
        // topology; Q≈1.25 gives ~octave-wide bells that overlap smoothly.
        biq.type = i === 0 ? 'lowshelf' : i === BANDS.length - 1 ? 'highshelf' : 'peaking';
        biq.frequency.value = f;
        if (biq.type === 'peaking') biq.Q.value = 1.25;
        biq.gain.value = 0;
        return biq;
    });
    _analyser = _ctx.createAnalyser();
    _analyser.fftSize = 2048;
    _analyser.smoothingTimeConstant = 0.75;
    let node = _preamp;
    for (const f of _filters) { node.connect(f); node = f; }
    node.connect(_analyser);
    _analyser.connect(_ctx.destination);
    _applyAll();
}

/**
 * Route an <audio> element through the EQ graph. Safe to call repeatedly;
 * a browser allows exactly ONE MediaElementSource per element, ever, so the
 * node is cached per element and only the chain hookup changes.
 * Returns false when Web Audio isn't available.
 */
export function eqAttach(el) {
    _buildGraph();
    if (!_ctx) return false;
    if (_attachedEl === el) return true;
    let src = _sources.get(el);
    if (!src) {
        try { src = _ctx.createMediaElementSource(el); }
        catch (e) { log.warn('MediaElementSource failed', e); return false; }
        _sources.set(el, src);
    }
    if (_source) { try { _source.disconnect(); } catch { /* already detached */ } }
    _source = src;
    _source.connect(_preamp);
    _attachedEl = el;
    return true;
}

/** Autoplay policy: contexts start suspended — resume inside play gestures. */
export function eqResume() {
    if (_ctx && _ctx.state === 'suspended') _ctx.resume().catch(() => {});
}

function _applyAll() {
    if (!_ctx) return;
    const t = _ctx.currentTime;
    const on = _s.enabled;
    _filters.forEach((f, i) =>
        f.gain.setTargetAtTime(on ? _s.gains[i] : 0, t, RAMP_S));
    // Headroom: pull the whole signal down by the largest boost.
    const maxBoost = on ? Math.max(0, ...(_s.gains)) : 0;
    _preamp.gain.setTargetAtTime(Math.pow(10, -maxBoost / 20), t, RAMP_S);
}

// ── Settings API (media.js renders from this) ──────────────────────────

export function eqState() {
    return {
        enabled: _s.enabled,
        preset: _s.preset,
        gains: _s.gains.slice(),
        preampDb: -Math.max(0, ...(_s.enabled ? _s.gains : [0])),
        active: !!_attachedEl,          // an element is actually routed
    };
}

export function eqSetEnabled(on) {
    _s.enabled = !!on;
    _save();
    _applyAll();
}

export function eqSetBand(i, db) {
    if (i < 0 || i >= BANDS.length) return;
    _s.gains[i] = _clamp(db);
    _s.preset = 'Custom';               // diverged from any named curve
    _save();
    _applyAll();
}

export function eqApplyPreset(name) {
    const gains = PRESETS[name];
    if (!gains) return;
    _s.preset = name;
    _s.gains = gains.slice();
    if (name !== 'Flat') _s.enabled = true;   // picking a curve means "use it"
    _save();
    _applyAll();
}

// ── Spectrum (the panel's live backdrop) ────────────────────────────────
// One rAF loop, target canvas looked up by id each frame — re-renders of the
// player list can replace the canvas node without anyone re-wiring.

let _specRaf = 0, _specId = null;

export function eqSpectrumStart(canvasId) {
    _specId = canvasId;
    if (!_specRaf) _specRaf = requestAnimationFrame(_specFrame);
}

export function eqSpectrumStop() {
    _specId = null;
    if (_specRaf) { cancelAnimationFrame(_specRaf); _specRaf = 0; }
}

function _specFrame() {
    _specRaf = 0;
    if (!_specId) return;
    const cv = document.getElementById(_specId);
    if (cv && _analyser) {
        const ctx2d = cv.getContext('2d');
        const W = cv.width = cv.clientWidth || cv.width;
        const H = cv.height;
        ctx2d.clearRect(0, 0, W, H);
        const data = new Uint8Array(_analyser.frequencyBinCount);
        _analyser.getByteFrequencyData(data);
        // Log-frequency bars so the display lines up with the sliders below.
        const bars = 48, nyq = _ctx.sampleRate / 2;
        const fLo = 25, fHi = Math.min(18000, nyq);
        const accent = getComputedStyle(document.documentElement)
            .getPropertyValue('--bs-primary').trim() || '#0d6efd';
        ctx2d.fillStyle = accent;
        ctx2d.globalAlpha = 0.45;
        for (let b = 0; b < bars; b++) {
            const f0 = fLo * Math.pow(fHi / fLo, b / bars);
            const f1 = fLo * Math.pow(fHi / fLo, (b + 1) / bars);
            const i0 = Math.floor(f0 / nyq * data.length);
            const i1 = Math.max(i0 + 1, Math.ceil(f1 / nyq * data.length));
            let peak = 0;
            for (let i = i0; i < i1 && i < data.length; i++) peak = Math.max(peak, data[i]);
            const h = (peak / 255) * (H - 2);
            const w = W / bars;
            ctx2d.fillRect(b * w + 1, H - h, w - 2, h);
        }
        ctx2d.globalAlpha = 1;
    }
    _specRaf = requestAnimationFrame(_specFrame);
}
