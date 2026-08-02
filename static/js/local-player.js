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
 * Routing modes (plain vs EQ), the proxy cascade, HLS and the Media Session:
 * docs/speaker_sync.md § "This device" (browser) playback.
 */
const log = zmmLog('local-player');
import { eqAttach, eqResume, eqState } from './eq.js';

export const LOCAL_ID = 'local:browser';

const _proxied = u => `/api/media/local/proxy?url=${encodeURIComponent(u)}`;

let _audio = null;
let _queue = [];          // MediaItem dicts, in play order
let _index = 0;
let _volume = 1.0;
let _loading = false;     // resolving a URL — reads as "buffering"
let _eqMode = false;      // current element is CORS-tagged and EQ-routed
let _srcStage = 'direct'; // current source: direct | proxy | plain | hls
let _directUrl = null;    // the playing track's un-proxied URL
let _noCorsHosts = new Set(); // hosts that refused CORS this session
let _onChange = () => {};
let _onError = () => {};

// Web Audio routing costs background playback — only take it when asked for.
const _wantEq = () => !!eqState().enabled;

// HLS

let _hls = null;          // live hls.js instance, when the stream needs one
let _hlsLib = null;       // in-flight/settled load of the library itself

const _isHlsItem = (item, url) =>
    /mpegurl/i.test((item && item.content_type) || '') || /\.m3u8(\?|#|$)/i.test(url);

function _loadHls() {
    if (window.Hls) return Promise.resolve(window.Hls);
    if (!_hlsLib) {
        _hlsLib = new Promise((resolve, reject) => {
            const s = document.createElement('script');
            s.src = '/static/js/vendor/hls.min.js';
            s.onload = () => resolve(window.Hls);
            s.onerror = () => { _hlsLib = null; reject(new Error('hls.js failed to load')); };
            document.head.appendChild(s);
        });
    }
    return _hlsLib;
}

/** Preload the library so play() isn't delayed past its user gesture. */
export function warmHls() { _loadHls().catch(() => { /* falls back at play time */ }); }

function _detachHls() {
    if (!_hls) return;
    try { _hls.destroy(); } catch { /* already gone */ }
    _hls = null;
}

/** Point `el` at an HLS stream. Returns false if this browser can't. */
async function _attachHls(el, url) {
    _detachHls();
    const src = _proxied(url);   // proxy rewrites the manifest — segments stay same-origin
    if (el.canPlayType('application/vnd.apple.mpegurl')) { el.src = src; return true; }
    let Hls;
    try { Hls = await _loadHls(); } catch (e) { log.warn(e.message); return false; }
    if (!Hls || !Hls.isSupported()) return false;
    _hls = new Hls({ enableWorker: true });
    let recovered = 0;
    _hls.on(Hls.Events.ERROR, (_ev, data) => {
        if (!data || !data.fatal) return;              // hls.js retries these itself
        // Capped: a stream that only ever fails must not loop.
        const recoverable = data.type === Hls.ErrorTypes.NETWORK_ERROR
            || data.type === Hls.ErrorTypes.MEDIA_ERROR;
        if (recoverable && recovered++ < 3) {
            log.log('HLS error — recovering', data.type, data.details);
            if (data.type === Hls.ErrorTypes.NETWORK_ERROR) _hls.startLoad();
            else _hls.recoverMediaError();
        } else {
            log.warn('HLS playback failed', data.type, data.details);
            _detachHls();
            _onError('This stream stopped responding');
            _onChange();
        }
    });
    _hls.loadSource(src);
    _hls.attachMedia(el);
    return true;
}

/** Opening stage for a URL: proxy when a direct load is known to fail. */
function _stageFor(url) {
    if (location.protocol === 'https:' && /^http:/i.test(url)) return 'proxy';  // mixed content
    if (_eqMode) {
        try {
            if (_noCorsHosts.has(new URL(url, location.href).host)) return 'proxy';
        } catch { /* data: etc. — treat as direct */ }
    }
    return 'direct';
}

export function initLocalPlayer({ onChange, onError }) {
    _onChange = onChange || (() => {});
    _onError = onError || (() => {});
}

/** EQ is on but this stream isn't going through it — the panel says so. */
export function eqBypassed() {
    return _wantEq() && !_eqMode && _queue.length > 0;
}

function audio() {
    if (_audio) return _audio;
    const a = new Audio();
    a.preload = 'auto';
    a.volume = _volume;
    if (_eqMode) {
        a.crossOrigin = 'anonymous';
        // Attach may fail (no Web Audio) — playback still works, just un-EQ'd.
        if (!eqAttach(a)) _eqMode = false;
    }
    // Re-render on anything that changes what the card shows. timeupdate is
    // deliberately absent: the position bar interpolates from an anchor, so
    // re-rendering 4x/sec would just fight the user's volume drag.
    ['playing', 'pause', 'ended', 'waiting', 'loadedmetadata', 'emptied']
        .forEach(ev => a.addEventListener(ev, () => {
            if (ev === 'ended') return next();   // next() re-renders itself
            _sessionState();
            _onChange();
        }));
    a.addEventListener('error', () => {
        const err = a.error;
        // hls.js owns the buffer and reports through its own ERROR event;
        // touching src here would tear down its MediaSource.
        if (_srcStage === 'hls') return;
        // The same-origin proxy fixes what fails a direct load: missing CORS
        // headers, an http source, a redirect the element won't follow.
        if (_directUrl && _srcStage === 'direct') {
            if (_eqMode) {
                try { _noCorsHosts.add(new URL(_directUrl, location.href).host); } catch { /* data: etc. */ }
            }
            log.log('direct load failed — rerouting via server proxy', _directUrl);
            _srcStage = 'proxy';
            a.src = _proxied(_directUrl);
            a.load();
            a.play().catch(e => _onError(`Tap play to start audio (${e.name})`));
            _onChange();
            return;
        }
        // EQ mode only: proxy failed too — drop the EQ, not the stream.
        if (_eqMode && _directUrl && _srcStage === 'proxy') {
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

// Swap the cached element for a fresh one in the given mode. The old element
// keeps its (single, permanent) MediaElementSource but is silenced and
// dropped; eq.js re-attaches per element.
function _newElement(eqRouted) {
    const old = _audio;
    _detachHls();                 // its MediaSource belongs to the old element
    if (old) {
        try { old.pause(); old.removeAttribute('src'); old.load(); } catch { /* dying anyway */ }
    }
    _audio = null;
    _eqMode = eqRouted;
    return audio();
}

/** Re-route the running stream after the EQ was switched on or off. */
export async function eqRoutingChanged() {
    if (!_queue.length || _wantEq() === _eqMode) return;
    const a = _audio;
    if (!a || !a.src || !_directUrl) { _eqMode = _wantEq(); return; }
    const at = isFinite(a.duration) ? a.currentTime : 0;
    const playing = !a.paused;
    const wasHls = _srcStage === 'hls';
    const el = _newElement(_wantEq());
    if (wasHls) {
        if (!await _attachHls(el, _directUrl)) { _onError('Could not reload the stream'); return; }
    } else {
        _srcStage = _stageFor(_directUrl);
        el.src = _srcStage === 'proxy' ? _proxied(_directUrl) : _directUrl;
    }
    // Only sticks once the new element knows the media.
    if (at) el.addEventListener('loadedmetadata',
        () => { try { el.currentTime = at; } catch { /* not seekable */ } }, { once: true });
    if (playing) {
        eqResume();
        el.play().catch(e => _onError(`Tap play to start audio (${e.name})`));
    }
    _onChange();
}

// Media Session — lock-screen controls, and what marks this tab as an audio
// session a phone should not freeze. Set on every load.

function _session(item) {
    const ms = navigator.mediaSession;
    if (!ms) return;
    try {
        const art = item && item.artwork_url;
        ms.metadata = new MediaMetadata({
            title: (item && item.title) || 'ZigBee Matter Manager',
            artist: (item && item.artist) || '',
            album: (item && item.album) || '',
            artwork: art ? [{ src: art, sizes: '512x512' }] : [],
        });
    } catch { /* MediaMetadata unsupported — handlers still help */ }
    const set = (action, fn) => {
        try { ms.setActionHandler(action, fn); } catch { /* action unsupported */ }
    };
    set('play', () => resume());
    set('pause', () => pause());
    set('stop', () => stop());
    // Only offer skip on a real queue; radio is a single endless item.
    set('nexttrack', _queue.length > 1 ? () => next() : null);
    set('previoustrack', _queue.length > 1 ? () => prev() : null);
    _sessionState();
}

function _sessionState() {
    const ms = navigator.mediaSession;
    if (!ms) return;
    const a = _audio;
    ms.playbackState = !a || !a.src || !_queue.length ? 'none'
        : a.paused ? 'paused' : 'playing';
    // Tracks only — radio's duration is Infinity.
    if (a && isFinite(a.duration) && a.duration > 0 && ms.setPositionState) {
        try {
            ms.setPositionState({
                duration: a.duration,
                position: Math.min(a.currentTime || 0, a.duration),
                playbackRate: a.playbackRate || 1,
            });
        } catch { /* stale numbers between loads */ }
    }
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
    const wantEq = _wantEq();
    _directUrl = url;
    const el = (_audio && _eqMode === wantEq) ? _audio : _newElement(wantEq);
    if (_isHlsItem(item, url)) {
        _srcStage = 'hls';
        if (!await _attachHls(el, url)) {
            _onError('This device cannot play HLS streams');
            _onChange();
            return;
        }
    } else {
        _detachHls();
        _srcStage = _stageFor(url);
        el.src = _srcStage === 'proxy' ? _proxied(url) : url;
    }
    _session(item);
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
    _detachHls();                 // stops segment fetching, not just playback
    a.pause();
    a.removeAttribute('src');
    a.load();                     // drop the buffer so radio stops fetching
    _queue = [];
    _index = 0;
    _directUrl = null;
    if (navigator.mediaSession) {
        navigator.mediaSession.metadata = null;   // clears the lock screen
        navigator.mediaSession.playbackState = 'none';
    }
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
