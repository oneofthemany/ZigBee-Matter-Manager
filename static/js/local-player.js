/**
 * local-player.js — "This device" playback.
 *
 * Plays radio and Tidal in the browser instead of casting, so the media tab
 * works on a phone with no speaker around. It presents itself as an ordinary
 * player (same shape as a Cast/WiiM PlayerState), so the Players list, the
 * select-then-play flow and the therapy pane all treat it like any other
 * target — see snapshot().
 *
 * Radio items carry a direct stream URL. Tidal items don't: their URLs are
 * short-lived, so we resolve each track just-in-time via
 * /api/media/local/track_url, which returns 320k AAC (DASH/FLAC is Cast-only)
 * — natively playable in an <audio> element, no MSE or dash.js needed.
 */
const log = zmmLog('local-player');

export const LOCAL_ID = 'local:browser';

let _audio = null;
let _queue = [];          // MediaItem dicts, in play order
let _index = 0;
let _volume = 1.0;
let _loading = false;     // resolving a URL — reads as "buffering"
let _onChange = () => {};
let _onError = () => {};

export function initLocalPlayer({ onChange, onError }) {
    _onChange = onChange || (() => {});
    _onError = onError || (() => {});
}

function audio() {
    if (_audio) return _audio;
    const a = new Audio();
    a.preload = 'auto';
    a.volume = _volume;
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

// ── Queue ───────────────────────────────────────────────────────────────

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
    a.src = url;
    if (autoplay) {
        try {
            // Must be reached from the click that started playback, or the
            // browser's autoplay policy rejects it.
            await a.play();
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
    audio().play().catch(e => _onError(`Could not resume (${e.name})`));
}

export function stop() {
    const a = audio();
    a.pause();
    a.removeAttribute('src');
    a.load();                     // drop the buffer so radio stops fetching
    _queue = [];
    _index = 0;
    _onChange();
}

export function setVolume(level) {
    _volume = Math.max(0, Math.min(1, Number(level)));
    audio().volume = _volume;
    _onChange();
}

export function isActive() { return _queue.length > 0; }

// ── State ───────────────────────────────────────────────────────────────

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
