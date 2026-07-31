/**
 * eq-scope.js — the studio display behind the graphic EQ.
 *
 * One renderer, two signal sources, because the audio is in two different
 * places depending on where it is playing:
 *
 *   local  — "This device" plays through a Web Audio graph in this tab, so an
 *            AnalyserNode is right there and the browser does the FFT.
 *   zone   — a sync zone plays on the speakers and never touches the browser.
 *            The server analyses the master timeline at the AUDIBLE position
 *            and pushes bands over the websocket (cast_sync._spectrum_feed).
 *
 * Both arrive as the same thing — N log-spaced band levels in 0..1 — so
 * everything below this line is drawing, and neither source knows about the
 * other. A scope with no frames for a while fades out instead of freezing: a
 * still picture of a spectrum reads as live audio that has gone silent, which
 * is a lie when what actually happened is the feed stopped.
 *
 * The curve drawn over the spectrum is the EQ's own magnitude response,
 * computed from the band gains with the RBJ cookbook formulas — the same
 * design Web Audio's peaking/shelf biquads implement, so the line matches what
 * the local filters actually do rather than being a smoothed sketch of the
 * slider positions.
 */
const log = zmmLog('eq-scope');

const GRID_DB = [0, -12, -24, -36, -48, -60];
const AXIS_HZ = [[31, '31'], [125, '125'], [500, '500'],
                 [2000, '2k'], [8000, '8k'], [16000, '16k']];
// Peak caps fall at roughly the rate a mechanical meter returns — fast enough
// to track a mix, slow enough that the eye can read the peak it just missed.
const PEAK_FALL_PER_S = 0.55;
const SILENT_AFTER_MS = 1200;   // no frames for this long: fade the scope out
const FADE_PER_S = 7;           // grace + this ramp ≈ 1.8 s from last frame to blank

const _scopes = new Map();      // canvasId -> scope state

function _tokens() {
    const cs = getComputedStyle(document.documentElement);
    const pick = (name, fallback) =>
        (cs.getPropertyValue(name) || '').trim() || fallback;
    return {
        honey: pick('--honey', '#f2a93b'),
        honeySoft: pick('--honey-soft', '#f7bb5e'),
        honeyDeep: pick('--honey-deep', '#c97f1e'),
        ink: pick('--text-muted', '#8b949e'),
    };
}

/**
 * RBJ cookbook magnitude response, evaluated at one frequency.
 *
 * Returned in dB so it can be plotted straight onto the same axis as the grid.
 * Q matches the value eq.js gives its peaking biquads (1.25). It is not a free
 * parameter here: the point of computing the response rather than sketching it
 * is that the line is what the filters do, and a different Q would draw bells
 * narrower or wider than the ones in the signal path.
 */
const PEAK_Q = 1.25;

function _bandResponseDb(f, fc, gainDb, kind, rate) {
    if (!gainDb) return 0;
    const A = Math.pow(10, gainDb / 40);
    const w = 2 * Math.PI * fc / rate;
    const cw = Math.cos(w), sw = Math.sin(w);
    const Q = PEAK_Q;
    let b0, b1, b2, a0, a1, a2;
    if (kind === 'lowshelf' || kind === 'highshelf') {
        const S = 1.0;
        const alpha = sw / 2 * Math.sqrt((A + 1 / A) * (1 / S - 1) + 2);
        const tsa = 2 * Math.sqrt(A) * alpha;
        if (kind === 'lowshelf') {
            b0 = A * ((A + 1) - (A - 1) * cw + tsa);
            b1 = 2 * A * ((A - 1) - (A + 1) * cw);
            b2 = A * ((A + 1) - (A - 1) * cw - tsa);
            a0 = (A + 1) + (A - 1) * cw + tsa;
            a1 = -2 * ((A - 1) + (A + 1) * cw);
            a2 = (A + 1) + (A - 1) * cw - tsa;
        } else {
            b0 = A * ((A + 1) + (A - 1) * cw + tsa);
            b1 = -2 * A * ((A - 1) + (A + 1) * cw);
            b2 = A * ((A + 1) + (A - 1) * cw - tsa);
            a0 = (A + 1) - (A - 1) * cw + tsa;
            a1 = 2 * ((A - 1) - (A + 1) * cw);
            a2 = (A + 1) - (A - 1) * cw - tsa;
        }
    } else {
        const alpha = sw / (2 * Q);
        b0 = 1 + alpha * A; b1 = -2 * cw; b2 = 1 - alpha * A;
        a0 = 1 + alpha / A; a1 = -2 * cw; a2 = 1 - alpha / A;
    }
    // |H(e^jw)| at the evaluation frequency.
    const we = 2 * Math.PI * f / rate;
    const c1 = Math.cos(we), s1 = Math.sin(we);
    const c2 = Math.cos(2 * we), s2 = Math.sin(2 * we);
    const nr = b0 + b1 * c1 + b2 * c2, ni = -(b1 * s1 + b2 * s2);
    const dr = a0 + a1 * c1 + a2 * c2, di = -(a1 * s1 + a2 * s2);
    const num = Math.hypot(nr, ni), den = Math.hypot(dr, di);
    return den === 0 ? 0 : 20 * Math.log10(num / den);
}

function _curveDb(f, bands, gains, rate) {
    let sum = 0;
    for (let i = 0; i < bands.length; i++) {
        const kind = i === 0 ? 'lowshelf'
            : i === bands.length - 1 ? 'highshelf' : 'peaking';
        sum += _bandResponseDb(f, bands[i], gains[i] || 0, kind, rate);
    }
    return sum;
}

/**
 * Mount a scope on a canvas. Returns a handle; call .destroy() to release it.
 *
 * `opts.getFrame()` returns {bands: Float/Uint array 0..1 or 0..255, fLo, fHi}
 * or null when nothing has arrived. `opts.getEq()` returns
 * {enabled, bands:[Hz], gains:[dB], rate} for the overlaid curve, or null.
 */
export function mountScope(canvasId, opts = {}) {
    const existing = _scopes.get(canvasId);
    if (existing) existing.destroy();
    const st = {
        raf: 0, peaks: null, last: 0, lastFrameAt: 0, alpha: 0,
        opts, destroyed: false,
    };
    st.destroy = () => {
        st.destroyed = true;
        if (st.raf) cancelAnimationFrame(st.raf);
        _scopes.delete(canvasId);
    };
    _scopes.set(canvasId, st);

    const frame = (now) => {
        st.raf = 0;
        if (st.destroyed) return;
        const cv = document.getElementById(canvasId);
        if (cv) _draw(cv, st, now);
        st.raf = requestAnimationFrame(frame);
    };
    st.raf = requestAnimationFrame(frame);
    log.debug(`scope mounted on #${canvasId}`);
    return st;
}

export function unmountScope(canvasId) {
    const st = _scopes.get(canvasId);
    if (st) st.destroy();
}

function _draw(cv, st, now) {
    const dpr = window.devicePixelRatio || 1;
    const W = Math.max(1, cv.clientWidth), H = Math.max(1, cv.clientHeight);
    // Backing store in device pixels, drawing in CSS pixels — a canvas sized
    // only in CSS pixels renders soft on every phone and most laptops, which
    // is exactly where a thin grid line stops being legible.
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
        cv.width = Math.round(W * dpr);
        cv.height = Math.round(H * dpr);
    }
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);

    const t = _tokens();
    const dt = st.last ? Math.min(0.1, (now - st.last) / 1000) : 0;
    st.last = now;

    const f = st.opts.getFrame ? st.opts.getFrame() : null;
    if (f && f.bands && f.bands.length) {
        st.lastFrameAt = now;
        st.frame = f;
    }
    // Fade rather than freeze when the feed stops (see the module note). The
    // grace absorbs a websocket hiccup without blinking the panel; the ramp
    // that follows is per-second, not per-frame, so the same fade takes the
    // same time on a 60 Hz panel and a 144 Hz one.
    const live = st.lastFrameAt && (now - st.lastFrameAt) < SILENT_AFTER_MS;
    st.alpha += ((live ? 1 : 0) - st.alpha) * Math.min(1, dt * FADE_PER_S);

    const padL = 26, padB = 14, padT = 4;
    const plotW = Math.max(1, W - padL - 4), plotH = Math.max(1, H - padB - padT);
    const fLo = st.frame?.fLo || 25, fHi = st.frame?.fHi || 18000;
    const xOf = (hz) => padL + plotW * Math.log(hz / fLo) / Math.log(fHi / fLo);
    const yOf = (norm) => padT + plotH * (1 - norm);

    // ── grid ────────────────────────────────────────────────────────────
    g.lineWidth = 1;
    g.font = '9px ui-monospace, SFMono-Regular, Menlo, monospace';
    g.textBaseline = 'middle';
    // Lines stay; labels thin out. Six of them need ~66px to sit apart, and on
    // the short local strip they collide into an unreadable smear — a scale
    // you cannot read is worse than a scale with fewer marks on it.
    const labelEvery = plotH >= 66 ? 1 : plotH >= 40 ? 2 : GRID_DB.length;
    for (let i = 0; i < GRID_DB.length; i++) {
        const db = GRID_DB[i];
        const y = Math.round(yOf((db + 72) / 72)) + 0.5;
        g.strokeStyle = db === 0 ? 'rgba(255,255,255,0.22)' : 'rgba(255,255,255,0.08)';
        g.beginPath(); g.moveTo(padL, y); g.lineTo(W - 4, y); g.stroke();
        if (i % labelEvery) continue;
        g.fillStyle = t.ink; g.globalAlpha = 0.75; g.textAlign = 'right';
        g.fillText(String(db), padL - 4, y);
        g.globalAlpha = 1;
    }
    g.textAlign = 'center'; g.textBaseline = 'top';
    for (const [hz, label] of AXIS_HZ) {
        if (hz < fLo || hz > fHi) continue;
        const x = Math.round(xOf(hz)) + 0.5;
        g.strokeStyle = 'rgba(255,255,255,0.07)';
        g.beginPath(); g.moveTo(x, padT); g.lineTo(x, padT + plotH); g.stroke();
        g.fillStyle = t.ink; g.globalAlpha = 0.75;
        g.fillText(label, x, padT + plotH + 3);
        g.globalAlpha = 1;
    }

    // ── spectrum ────────────────────────────────────────────────────────
    const bands = st.frame?.bands;
    if (bands && bands.length && st.alpha > 0.01) {
        const n = bands.length;
        const scale = st.frame.scale255 ? 1 / 255 : 1;
        if (!st.peaks || st.peaks.length !== n) st.peaks = new Float32Array(n);
        const bw = plotW / n;

        // Bright at the top where the peaks are, and only as far as --honey at
        // the floor. Running the ramp all the way down to --honey-deep looks
        // right on a sparse spectrum and turns to mud on a dense one, where
        // every bar is tall and the bottom two-thirds of the panel becomes one
        // dark brown block instead of a set of readable bars.
        const grad = g.createLinearGradient(0, padT, 0, padT + plotH);
        grad.addColorStop(0, t.honeySoft);
        grad.addColorStop(1, t.honey);

        g.globalAlpha = st.alpha * 0.85;
        g.fillStyle = grad;
        for (let i = 0; i < n; i++) {
            const v = Math.max(0, Math.min(1, bands[i] * scale));
            const h = v * plotH;
            if (h > 0.5) g.fillRect(padL + i * bw + 0.5, padT + plotH - h,
                                    Math.max(1, bw - 1), h);
            // Peak decays in dB-of-display per second, not per frame, so the
            // fall rate is the same on a 60 Hz panel and a 144 Hz one.
            st.peaks[i] = v > st.peaks[i]
                ? v : Math.max(v, st.peaks[i] - PEAK_FALL_PER_S * dt);
        }
        g.globalAlpha = st.alpha * 0.9;
        g.fillStyle = t.honeySoft;
        for (let i = 0; i < n; i++) {
            if (st.peaks[i] <= 0.01) continue;
            const y = padT + plotH - st.peaks[i] * plotH;
            g.fillRect(padL + i * bw + 0.5, y, Math.max(1, bw - 1), 1.5);
        }
        g.globalAlpha = 1;
    }

    // ── EQ curve ────────────────────────────────────────────────────────
    const eq = st.opts.getEq ? st.opts.getEq() : null;
    if (eq && eq.enabled && eq.gains?.some(v => v)) {
        const rate = eq.rate || 48000;
        g.beginPath();
        for (let px = 0; px <= plotW; px += 2) {
            const hz = fLo * Math.pow(fHi / fLo, px / plotW);
            const db = _curveDb(hz, eq.bands, eq.gains, rate);
            // The curve shares the panel but not the scale: ±12 dB of EQ over
            // the middle third of a 72 dB spectrum axis would be a flat line.
            const y = padT + plotH * (0.5 - db / 32);
            const x = padL + px;
            px === 0 ? g.moveTo(x, y) : g.lineTo(x, y);
        }
        g.strokeStyle = t.honeySoft;
        g.lineWidth = 2;
        g.shadowColor = t.honey;
        g.shadowBlur = 6;
        g.stroke();
        g.shadowBlur = 0;
    }
}
