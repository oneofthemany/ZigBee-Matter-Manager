/**
 * "This device" playback — plays radio and Tidal in the browser instead of
 * casting, so the media tab works on a phone with no speaker around.
 *
 * Presents itself as an ordinary player (same shape as a Cast/WiiM
 * PlayerState) so the Players list and select-then-play flow treat it like any
 * other target — see snapshot(). Tidal URLs are short-lived, so each track is
 * resolved just-in-time via /api/media/local/track_url, which returns 320k AAC
 * (DASH/FLAC is Cast-only) — natively playable, no MSE needed.
 *
 * EQ routing needs crossorigin="anonymous": a MediaElementSource on a
 * cross-origin stream without CORS headers is pure silence, and with the
 * attribute a non-CORS stream refuses to load at all. So try the CORS element
 * first, fall back to the same-origin passthrough (/api/media/local/proxy,
 * where CORS never applies and EQ keeps working) and remember the host, and
 * only if that fails too play the source directly, un-EQ'd.
 */
const log = zmmLog('local-player');
import { eqAttach, eqResume } from './eq.js';

export const LOCAL_ID = 'local:browser';

const _proxied = u => `/api/media/local/proxy?url=${encodeURIComponent(u)}`;

let _audio = null;
let _queue = [];          // MediaItem dicts, in play order
let _index = 0;
let _volume = 1.0;
let _loading = false;     // resolving a URL — reads as "buffering"
let _corsMode = true;     // current element is CORS-tagged (EQ-capable)
let _srcStage = 'direct'; // current source: direct | proxy | plain
let _directUrl = null;    // the playing track's un-proxied URL
let _noCorsHosts = new Set(); // hosts that refused CORS this session
let _onChange = () => {};
let _onError = () => {};

export function initLocalPlayer({ onChange, onError }) {
    _onChange = onChange || (() => {});
    _onError = onError || (() => {});
}

/** True while playback runs on the plain fallback element — the EQ can't
 *  touch this stream, and the panel says so instead of silently lying. */
export function eqBypassed() {
    return !_corsMode && _queue.length > 0;
}

function audio() {
    if (_audio) return _audio;
    const a = new Audio();
    a.preload = 'auto';
    a.volume = _volume;
    if (_corsMode) {
        a.crossOrigin = 'anonymous';
        // Attach may fail (no Web Audio) — playback still works, just un-EQ'd.
        if (!eqAttach(a)) _corsMode = false;
    }
    // Re-render on anything that changes what the card shows. timeupdate is
    // deliberately absent: the position bar interpolates from an anchor, so
    // re-rendering 4x/sec would just fight the user's volume drag.
    ['playing', 'pause', 'ended', 'waiting', 'loadedmetadata', 'emptied']
        .forEach(ev => a.addEventListener(ev, () => {
            if (ev === 'ended') return next();   // next() re-renders itself
            _onChange();
        }));
    a.addEventListener('error', () => {
        const err = a.error;
        // CORS-tagged elements refuse streams that lack CORS headers — retry
        // through the server's same-origin proxy first (EQ keeps working);
        // only if that fails too, fall back to a plain element (EQ bypassed).
        if (_corsMode && _directUrl && _srcStage === 'direct') {
            try { _noCorsHosts.add(new URL(_directUrl, location.href).host); } catch { /* data: etc. */ }
            log.log('stream refused CORS — rerouting via server proxy', _directUrl);
            _srcStage = 'proxy';
            a.src = _proxied(_directUrl);
            a.load();
            a.play().catch(e => _onError(`Tap play to start audio (${e.name})`));
            _onChange();
            return;
        }
        if (_corsMode && _directUrl && _srcStage === 'proxy') {
            log.log('proxy stream failed — playing direct without EQ routing', _directUrl);
            _srcStage = 'plain';
            const b = _newElement(false);
            b.src = _directUrl;
            b.play().catch(e => _onError(`Tap play to start audio (${e.name})`));
            _onChange();
            return;
        }
        const msg = err && err.code === 4
            ? 'This device cannot play that stream (unsupported format or blocked source)'
            : 'Playback failed on this device';
        log.warn('local playback error', err && err.code, a.src);
        _onError(msg);
        _onChange();
    });
    _audio = a;
    return a;
}

// Swap the cached element for a fresh one in the given CORS mode. The old
// element keeps its (single, permanent) MediaElementSource but is silenced
// and dropped; eq.js re-attaches per element.
function _newElement(cors) {
    const old = _audio;
    if (old) {
        try { old.pause(); old.removeAttribute('src'); old.load(); } catch { /* dying anyway */ }
    }
    _audio = null;
    _corsMode = cors;
    return audio();
}

// Queue

/** Play a resolved list of MediaItem dicts from the start. */
export async function playItems(items) {
    _queue = Array.isArray(items) ? items.filter(Boolean) : [];
    _index = 0;
    if (!_queue.length) { _onError('Nothing to play'); return; }
    await _load(0, true);
}

async function _load(i, autoplay) {
    if (i < 0 || i >= _queue.length) return;
    _index = i;
    const item = _queue[i];
    const a = audio();
    let url = item.url || '';
    // Tidal hands back no URL at search time (signed + short-lived) — resolve now.
    if (!url && item.source_id) {
        _loading = true;
        _onChange();
        try {
            const r = await fetch(`/api/media/local/track_url?source_id=${encodeURIComponent(item.source_id)}`)
                .then(x => x.json());
            if (!r.success || !r.url) throw new Error(r.error || 'no URL');
            url = r.url;
        } catch (e) {
            _loading = false;
            _onError(`Could not resolve "${item.title || 'track'}": ${e.message}`);
            _onChange();
            return;
        }
        _loading = false;
    }
    if (!url) { _onError('No playable URL for this item'); return; }
    // Per-track routing: always start on the EQ-capable element; hosts that
    // already refused CORS this session go straight to the server proxy
    // (saves a guaranteed-to-fail load on every queue advance).
    _directUrl = url;
    _srcStage = 'direct';
    try {
        if (_noCorsHosts.has(new URL(url, location.href).host)) _srcStage = 'proxy';
    } catch { /* keep direct */ }
    const el = _corsMode ? a : _newElement(true);
    el.src = _srcStage === 'proxy' ? _proxied(url) : url;
    if (autoplay) {
        try {
            // Must be reached from the click that started playback, or the
            // browser's autoplay policy rejects it.
            eqResume();
            await el.play();
        } catch (e) {
            _onError(`Tap play to start audio (${e.name})`);
        }
    }
    _onChange();
}

export function next() {
    if (_index + 1 < _queue.length) { _load(_index + 1, true); return true; }
    stop();                       // end of queue
    return false;
}

export function prev() {
    const a = audio();
    // Standard transport behaviour: restart the track unless we're near its start.
    if (a.currentTime > 3 || _index === 0) { a.currentTime = 0; _onChange(); return; }
    _load(_index - 1, true);
}

export function pause() { audio().pause(); }

export function resume() {
    eqResume();
    audio().play().catch(e => _onError(`Could not resume (${e.name})`));
}

export function stop() {
    const a = audio();
    a.pause();
    a.removeAttribute('src');
    a.load();                     // drop the buffer so radio stops fetching
    _queue = [];
    _index = 0;
    _directUrl = null;
    _onChange();
}

export function setVolume(level) {
    _volume = Math.max(0, Math.min(1, Number(level)));
    audio().volume = _volume;
    _onChange();
}

export function isActive() { return _queue.length > 0; }


/** A PlayerState-shaped snapshot so renderPlayers() can draw us unmodified. */
export function snapshot() {
    const a = _audio;
    const item = _queue[_index] || null;
    let state = 'idle';
    if (_loading) state = 'buffering';
    else if (a && item) {
        if (a.error) state = 'idle';
        else if (!a.paused && a.readyState >= 3) state = 'playing';
        else if (!a.paused) state = 'buffering';
        else state = 'paused';
    }
    // Radio is endless: duration is Infinity/NaN → 0, which renderPlayers
    // reads as "no progress bar", same as a cast radio stream.
    const durSec = a && isFinite(a.duration) ? a.duration : 0;
    return {
        player_id: LOCAL_ID,
        name: 'This device',
        provider: 'local',
        is_group: false,
        available: true,
        state,
        title: item ? (item.title || '') : '',
        artist: item ? (item.artist || '') : '',
        artwork_url: item ? (item.artwork_url || '') : '',
        media_type: item ? (item.media_type || '') : '',
        now_playing_id: item ? (item.source_id || '') : '',
        position_ms: a ? Math.round((a.currentTime || 0) * 1000) : 0,
        duration_ms: Math.round(durSec * 1000),
        volume: _volume,
        muted: false,
        queue: _queue.length > 1 ? { index: _index, length: _queue.length } : null,
    };
}
