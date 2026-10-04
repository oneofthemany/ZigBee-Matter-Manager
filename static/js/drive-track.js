/* Track maths for the Drive tab's trip map: colour scales, stops, peaks.
   No DOM, so it runs under node — tests/frontend/js/test_drive_track.mjs.
   See docs/journeys.md §Map layers. */

// Mirrors of modules/journeys.py, so the pins agree with the trip's own counts.
export var STOPPED_SPEED_MPS = 0.5;
export var MAX_IDLE_SEGMENT_S = 30;
export var MAX_PLAUSIBLE_SPEED_MPS = 90;

export var NO_DATA_COLOUR = '#7a8894';

function hexRgb(h) {
    return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
}

/** Colour at t (clamped to 0..1) along a list of evenly spaced hex stops. */
export function rampColour(ramp, t) {
    t = Math.min(1, Math.max(0, t));
    var x = t * (ramp.length - 1);
    var i = Math.min(ramp.length - 2, Math.floor(x));
    var a = hexRgb(ramp[i]), b = hexRgb(ramp[i + 1]);
    return '#' + [0, 1, 2].map(function (k) {
        return Math.round(a[k] + (b[k] - a[k]) * (x - i)).toString(16).padStart(2, '0');
    }).join('');
}

function maxOf(track, value, abs) {
    var m = null;
    track.forEach(function (p) {
        var v = value(p);
        if (v == null) return;
        if (abs) v = Math.abs(v);
        if (m == null || v > m) m = v;
    });
    return m;
}

/**
 * Continuous map layers. Each domain is fitted to the trip and rounded out to
 * a round number, so the legend ticks are readable and no value falls outside
 * the scale. Every ramp is dark enough to read on light map tiles.
 */
export var LAYERS = {
    speed: {
        label: 'Speed', icon: 'fa-gauge-high', unit: 'mph', decimals: 0,
        title: 'Speed over the journey',
        ramp: ['#2b6cb0', '#1a9e8f', '#d9a400', '#e8590c', '#c92a2a'],
        value: function (p) {
            return p.speed_mps == null || p.speed_mps > MAX_PLAUSIBLE_SPEED_MPS
                ? null : p.speed_mps * 2.23694;
        },
        domain: function (track) {
            var m = maxOf(track, LAYERS.speed.value) || 0;
            return [0, Math.max(30, Math.ceil(m / 10) * 10)];
        }
    },
    roughness: {
        label: 'Road', icon: 'fa-road', unit: 'm/s²', decimals: 2,
        title: 'Road roughness over the journey',
        ramp: ['#1a9e8f', '#d9a400', '#c92a2a'],
        value: function (p) { return p.vert_rms_mps2 == null ? null : p.vert_rms_mps2; },
        domain: function (track) {
            var m = maxOf(track, LAYERS.roughness.value) || 0;
            return [0, Math.max(2, Math.ceil(m))];
        }
    },
    gradient: {
        label: 'Hills', icon: 'fa-mountain', unit: '%', decimals: 1,
        title: 'Road gradient over the journey',
        // Diverging about a dark midpoint: a pale one vanishes on the tiles.
        ramp: ['#1d6fd8', '#3f3f46', '#d9480f'],
        value: function (p) { return p.gradient_pct == null ? null : p.gradient_pct; },
        domain: function (track) {
            var m = maxOf(track, LAYERS.gradient.value, true) || 0;
            var top = Math.max(10, Math.ceil(m / 5) * 5);
            return [-top, top];
        }
    }
};

/** Colour for a layer value, or the no-data grey — never the scale's low end. */
export function layerColour(layer, domain, v) {
    if (v == null) return NO_DATA_COLOUR;
    return rampColour(layer.ramp, (v - domain[0]) / (domain[1] - domain[0]));
}

function stopped(p) {
    return p && p.speed_mps != null && p.speed_mps < STOPPED_SPEED_MPS;
}

/**
 * Where the car came to rest, and how long it idled there.
 *
 * A stop is a moving→stopped transition, as in _MOTION_SQL, so the pin count
 * is the trip's stop_count. Standing time before the first movement has no
 * such transition and gets no pin; it is at the Start marker anyway.
 */
export function findStops(track) {
    var stops = [], run = null;
    for (var i = 0; i < track.length; i++) {
        var p = track[i], prev = track[i - 1];
        if (!stopped(p)) { run = null; continue; }
        if (prev && prev.speed_mps != null && prev.speed_mps >= STOPPED_SPEED_MPS) {
            run = { ts: p.ts, lat: p.lat, lon: p.lon, idle_s: 0 };
            stops.push(run);
        }
        if (run && prev && p.ts - prev.ts <= MAX_IDLE_SEGMENT_S) run.idle_s += p.ts - prev.ts;
    }
    return stops;
}

/**
 * The fixes holding the trip's peak braking, acceleration and cornering —
 * the same extremes _MOTION_SQL reports, located. Each is null when unmeasured.
 */
export function findPeaks(track) {
    var out = { brake: null, accel: null, corner: null };
    function take(kind, p, v) {
        if (!out[kind] || v > out[kind].value) out[kind] = { point: p, value: v };
    }
    track.forEach(function (p) {
        if (p.long_peak_mps2 != null && p.long_peak_mps2 < 0) take('brake', p, -p.long_peak_mps2);
        if (p.long_peak_mps2 != null && p.long_peak_mps2 > 0) take('accel', p, p.long_peak_mps2);
        if (p.lat_peak_mps2 != null) take('corner', p, p.lat_peak_mps2);
    });
    return out;
}

/** Index of the track point nearest in time to ts, or -1 for an empty track. */
export function nearestIndex(track, ts) {
    var best = -1, gap = Infinity;
    for (var i = 0; i < track.length; i++) {
        var g = Math.abs(track[i].ts - ts);
        if (g < gap) { gap = g; best = i; }
    }
    return best;
}

// Within this of level (%) a road is flat: barometer noise alone reaches it.
export var LEVEL_BAND_PCT = 2;
// Mirror of MAX_GRADIENT_GAP_S: beyond it a fix says nothing about the stretch before.
export var MAX_TERRAIN_GAP_S = 30;

/** 'up' | 'down' | 'level' for a fix, or null where the gradient is unknown. */
export function terrainOf(p) {
    if (!p || p.gradient_pct == null) return null;
    if (p.gradient_pct > LEVEL_BAND_PCT) return 'up';
    if (p.gradient_pct < -LEVEL_BAND_PCT) return 'down';
    return 'level';
}

/**
 * Distance, time, average speed and peak braking / acceleration driven
 * uphill, on the level and downhill. Peaks are null where unmeasured.
 *
 * Distance is speed × interval, the same horizontal measure the gradient was
 * derived from. Crawling traffic has no gradient and falls outside all three,
 * so the classes need not sum to the trip — `unknown_s` is what was left out.
 */
export function terrainSummary(track) {
    var out = { up: null, level: null, down: null, unknown_s: 0 };
    for (var i = 1; i < track.length; i++) {
        var p = track[i], dt = p.ts - track[i - 1].ts;
        if (!(dt > 0) || dt > MAX_TERRAIN_GAP_S) continue;
        var kind = terrainOf(p);
        if (!kind || p.speed_mps == null) { out.unknown_s += dt; continue; }
        var c = out[kind] || (out[kind] = { distance_m: 0, duration_s: 0, avg_speed_mps: 0,
                                            max_brake_mps2: null, max_accel_mps2: null });
        var lp = p.long_peak_mps2;
        if (lp != null && lp < 0 && -lp > (c.max_brake_mps2 || 0)) c.max_brake_mps2 = -lp;
        if (lp != null && lp > 0 && lp > (c.max_accel_mps2 || 0)) c.max_accel_mps2 = lp;
        c.distance_m += p.speed_mps * dt;
        c.duration_s += dt;
        c.avg_speed_mps = c.distance_m / c.duration_s;
    }
    return out;
}

/**
 * Stretches of sustained climb or descent as {kind, from, to} timestamps.
 * A lone fix is dropped: one steep sample is as likely a window opening as a hill.
 */
export function terrainRuns(track) {
    var runs = [], run = null;
    function close() {
        if (run && run.n >= 2) runs.push({ kind: run.kind, from: run.from, to: run.to });
        run = null;
    }
    for (var i = 1; i < track.length; i++) {
        var kind = terrainOf(track[i]);
        if (kind !== 'up' && kind !== 'down') { close(); continue; }
        if (!run || run.kind !== kind) {
            close();
            run = { kind: kind, from: track[i - 1].ts, to: track[i].ts, n: 1 };
        } else { run.to = track[i].ts; run.n++; }
    }
    close();
    return runs;
}
