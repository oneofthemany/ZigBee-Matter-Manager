/**
 * media.js — Media tab (Phase 2)
 * Players (Cast / WiiM), transport + volume + queue, native group builder,
 * and a Radio / Tidal source switch for search & queueing.
 *
 * State is pushed live over the WebSocket as `media_state`; we also fetch on
 * tab-show. Control actions POST to /api/media/*.
 */
const log = zmmLog('media');

import { confirmDialog } from './dialogs.js';
import { openSyncLab, restoreSyncLab, syncLabGroup } from './sync-lab.js';
import * as local from './local-player.js';
import { LOCAL_ID } from './local-player.js';
import * as eq from './eq.js';
import { mountScope, unmountScope } from './eq-scope.js';

let _remote = [];           // players from /api/media/players (Cast / WiiM)
let _players = [];          // _remote + the "This device" entry, as rendered
let _selectedId = null;     // player targeted by search "play"
let _groupBuilderOpen = false;
let _groupTab = 'wiim';     // group-builder sub-tab: 'wiim' | 'sync'
let _syncGroups = [];       // saved speaker-sync groups (from /api/media/sync/groups)
let _syncStatus = null;     // last /api/media/sync/status snapshot
let _syncTimer = null;      // stats poll while the sync pane is open
let _syncLabKeep = null;    // live Sync Lab DOM node — survives pane wipes so
//                             charts are re-attached, never rebuilt mid-test
// OpenZone splits in two: the zones you build and run, and the results you
// read. They were stacked, which put a page of charts under every control —
// a tab keeps each one a screenful.
let _syncSub = localStorage.getItem('zmm.openzone.tab') === 'results'
    ? 'results' : 'zones';
let _searchSource = 'radio'; // 'radio' | 'tidal' | 'therapy'
let _pane = null;            // mobile pane: 'players' | 'browse' (null = not yet chosen)
let _tidalState = null;      // last known tidal status state string
let _recentCache = [];       // recently-played items, for replay-by-index
let _tidalTab = 'search';    // tidal sub-tab: search | mixes | playlists | albums | artists
let _tidalLib = {};          // library rows by kind, cached for the zone picker
let _radioFavs = [];         // pinned radio stations (full dicts)
let _radioSearchCache = [];  // last radio search results, for star-toggle by index
let _posAnchors = {};        // player_id -> {pos,at,dur,playing} for smooth progress
let _posTimer = null;        // 1s interpolation ticker for the progress bars
let _eqOpen = new Set();     // player_ids with their EQ panel expanded
let _ctlOpen = new Set();    // player_ids with their controls expanded (touch only)
let _syncTrimOpen = new Set(); // player_ids with their trim slider expanded (touch only)
let _eqDev = {};             // player_id -> cached /api/media/eq result (wiim/cast)
let _eqSend = {};            // player_id -> {timer, gains} debounced slider POSTs

function _fmtTime(ms) {
    if (!isFinite(ms) || ms < 0) ms = 0;
    const s = Math.floor(ms / 1000), m = Math.floor(s / 60);
    return m + ':' + String(s % 60).padStart(2, '0');
}

// Re-anchor each player's playhead from the latest snapshot; the ticker
// interpolates between the (10s) updates so the bar advances smoothly.
function _syncPosAnchors() {
    const now = performance.now(), seen = new Set();
    for (const p of _players) {
        seen.add(p.player_id);
        _posAnchors[p.player_id] = { pos: p.position_ms || 0, at: now,
            dur: p.duration_ms || 0, playing: p.state === 'playing' };
    }
    for (const k of Object.keys(_posAnchors)) if (!seen.has(k)) delete _posAnchors[k];
}

function _tickPositions() {
    // Nothing to animate in a background tab, and the bar is re-anchored from
    // the player state on the next tick anyway.
    if (document.hidden) return;
    const now = performance.now();
    for (const [pid, a] of Object.entries(_posAnchors)) {
        if (a.dur <= 0) continue;                         // radio/live: no bar
        const bar = document.getElementById('prog-' + pid);
        if (!bar) continue;
        const cur = Math.min(a.playing ? a.pos + (now - a.at) : a.pos, a.dur);
        const pct = Math.max(0, Math.min(100, (cur / a.dur) * 100)).toFixed(2) + '%';
        if (bar.style.width !== pct) bar.style.width = pct;
        const lbl = document.getElementById('ptime-' + pid);
        const text = _fmtTime(cur) + ' / ' + _fmtTime(a.dur);
        if (lbl && lbl.textContent !== text) lbl.textContent = text;
    }
}

export function initMedia() {
    local.initLocalPlayer({
        onChange: _localChanged,
        onError: (msg) => toast(msg, 'error'),
    });
    autoPane();   // in case the media tab is already the visible one on load
    const tab = document.querySelector('[data-bs-target="#media"]');
    if (tab) {
        tab.addEventListener('shown.bs.tab', () => { loadPlayers(); refreshTidalNotice(); loadRecent(); loadRadioFavourites(); loadKaraoke(); autoPane(); });
    }
    // The therapy iframe asks for the current player selection when it mounts
    // (it may load after a player was already selected).
    window.addEventListener('message', (e) => {
        if (e.origin !== location.origin) return;
        if (e.data && e.data.type === 'zmm-get-selected-player') notifyTherapyFrame();
    });
    // Expose handlers for inline onclick + the websocket dispatcher.
    window.handleMediaState = handleMediaState;
    window.mediaRefresh = loadPlayers;
    window.mediaOpenGroupBuilder = toggleGroupBuilder;
    window.mediaPlayStation = playStation;
    window.mediaControl = control;
    window.mediaSetVolume = setVolume;
    window.mediaVolStep = volStep;
    window.mediaSelect = selectPlayer;
    window.mediaCtlToggle = ctlToggle;
    window.mediaVolLabel = volLabel;
    window.mediaPane = setPane;
    window.mediaUngroup = ungroup;
    window.mediaSubmitGroup = submitGroup;
    // Speaker-sync groups (group-builder sub-tab)
    window.mediaGroupTab = switchGroupTab;
    window.mediaSyncCreate = syncCreateGroup;
    window.mediaSyncDelete = syncDeleteGroup;
    window.mediaSyncStart = syncStartGroup;
    window.mediaSyncStop = syncStopSession;
    window.mediaSyncDur = syncSetDuration;
    window.mediaSyncSrc = syncSetSource;
    window.mediaSyncUrl = syncSetCustomUrl;
    window.mediaSyncLoop = syncSetLoop;
    window.mediaSyncXfade = syncSetCrossfade;
    window.mediaSyncTidalKind = syncTidalKind;
    window.mediaSyncTidalItem = syncTidalItem;
    window.mediaSyncCalibrate = syncCalibrate;
    window.mediaSyncTrim = syncSetTrim;
    window.mediaSyncNudge = syncNudgeTrim;
    window.mediaSyncTrimToggle = syncTrimToggle;
    // The group card's lab button now also switches to the Results tab —
    // opening a view you cannot see is worse than not opening it.
    window.mediaSyncLab = (gid) => {
        if (_syncSub !== 'results') window.mediaSyncSubTab('results');
        return openSyncLab(gid, _syncGroups.find(g => g.id === gid));
    };
    window.mediaSyncSubTab = (tab) => {
        _syncSub = tab === 'results' ? 'results' : 'zones';
        localStorage.setItem('zmm.openzone.tab', _syncSub);
        _applySyncSubTab();
    };
    // The lab's own close button leaves the Results tab empty — put the
    // group picker back rather than a blank panel.
    document.addEventListener('synclabchange', () => {
        if (_syncSub === 'results') _syncResultsPick();
    });
    // Phase 2
    window.mediaSetSource = setSource;
    window.mediaSearch = doSearch;
    window.mediaTidalPlay = tidalPlay;
    window.mediaTidalTab = tidalTab;
    window.mediaTidalFav = tidalFav;
    window.mediaTidalLyrics = tidalLyrics;
    window.mediaQueueMode = queueMode;
    window.mediaQueueClear = queueClear;
    window.mediaReplay = replayRecent;
    window.mediaPlayFav = playFavourite;
    window.mediaRadioFavAdd = radioFavAdd;
    window.mediaRadioFavRemove = radioFavRemove;
    window.mediaSetKaraoke = setKaraoke;
    window.mediaLyricsScreen = openLyricsScreen;
    window.mediaLyricsClose = closeLyricsScreen;
    window.mediaArtistPanel = artistPanel;
    window.mediaPlayTidalOn = playTidalOn;
    // Equaliser
    window.mediaEqToggle = eqToggle;
    window.mediaEqEnable = eqEnable;
    window.mediaEqPreset = eqPreset;
    window.mediaEqBand = eqBand;
    window.mediaEqBandRemote = eqBandRemote;
    if (!_posTimer) _posTimer = setInterval(_tickPositions, 1000);
}

// Karaoke mode (cast synced lyrics to the custom receiver)
async function loadKaraoke() {
    const wrap = document.getElementById('mediaKaraokeWrap');
    const box = document.getElementById('mediaKaraoke');
    if (!wrap || !box) return;
    const d = await apiGet('/api/media/karaoke');
    if (!d || !d.success) { wrap.style.display = 'none'; return; }
    // Only surface the switch when a custom lyrics receiver is configured —
    // otherwise it does nothing and would just confuse.
    wrap.style.display = d.receiver_configured ? '' : 'none';
    box.checked = !!d.enabled;
}

async function setKaraoke(on) {
    const r = await apiPost('/api/media/karaoke', { enabled: !!on });
    if (!r || !r.success) { toast('Could not change karaoke mode', 'error'); return; }
    toast(`Karaoke ${r.karaoke ? 'on' : 'off'}`, 'success');
}

// API helpers
async function apiGet(url) {
    const res = await fetch(url);
    return res.json();
}
async function apiPost(url, body) {
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
    });
    return res.json();
}

function toast(msg, kind = 'info') {
    if (typeof window.showToast === 'function') window.showToast(msg, kind);
    else log.log(`[media:${kind}] ${msg}`);
}

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

// Players
async function loadPlayers() {
    const el = document.getElementById('mediaPlayers');
    if (!el) return;
    const data = await apiGet('/api/media/players');
    if (!data.success) {
        el.innerHTML = `<div class="alert alert-warning mb-0">${esc(data.error || 'Media service unavailable')}</div>`;
        return;
    }
    _remote = data.players || [];
    _rebuild();
    autoSelect();
    renderPlayers();
}

// "This device" is always offered first — it needs no discovery and is the
// only target that works with no speaker on the network at all. Anything
// actively playing floats above the rest: that's the card being reached for,
// and on a phone it saves scrolling past every idle speaker. The sort is
// stable, so ties keep the usual order (local, then discovery order).
function _rebuild() {
    _players = [local.snapshot(), ..._remote];
    _players.sort((a, b) => (b.state === 'playing') - (a.state === 'playing'));
    _syncPosAnchors();
}

// Re-render when the local <audio> changes state (play/pause/track/volume).
function _localChanged() {
    _rebuild();
    if (_groupBuilderOpen) return;
    renderPlayers();
}

// If nothing is selected yet and there's exactly one (available) *remote*
// player, select it automatically so radio "play" just works without a hidden
// step. "This device" is excluded: it's always present, so counting it would
// disable the auto-select that users already rely on.
function autoSelect() {
    if (_selectedId) return;
    const avail = _remote.filter(p => p.available);
    if (avail.length === 1) _selectedId = avail[0].player_id;
}

function handleMediaState(payload) {
    if (!payload) return;
    _remote = payload.players || [];
    _rebuild();
    autoSelect();
    // Don't yank a volume slider out from under the user mid-drag.
    const active = document.activeElement;
    if (active && active.type === 'range') return;
    if (_groupBuilderOpen) return;   // builder owns the panel while open
    renderPlayers();
}

function iconFor(p) {
    // Return the FULL Font Awesome class incl. style prefix. `fa-chromecast`
    // is a BRAND icon (fab); `fa-speaker` doesn't exist in FA6-free — both
    // render as a missing-glyph box if forced to `fas`.
    if (p.provider === 'zone') return 'fas fa-object-group';     // an OpenZone zone
    if (p.is_group) return 'fas fa-layer-group';          // any group: stacked icon
    if (p.provider === 'local') return 'fas fa-mobile-screen';   // this browser
    return p.provider === 'cast' ? 'fab fa-chromecast' : 'fas fa-volume-up';
}

// Distinct, provider-aware badge so a Cast speaker GROUP is visually different
// from an individual Cast device and from a WiiM group.
function groupBadge(p) {
    if (!p.is_group) return '';
    if (p.provider === 'zone') {
        return `<span class="badge bg-success ms-1" title="OpenZone — one clock-aligned timeline across ${p.group_members.length} speakers">`
             + `<i class="zmm-openzone-icon me-1"></i>Zone · ${p.group_members.length}</span>`;
    }
    if (p.provider === 'cast') {
        return '<span class="badge bg-primary ms-1" title="Google Cast speaker group">'
             + '<i class="fab fa-chromecast me-1"></i>Cast group</span>';
    }
    if (p.provider === 'wiim') {
        return '<span class="badge bg-info text-dark ms-1" title="WiiM multiroom group">'
             + '<i class="fas fa-volume-up me-1"></i>WiiM group</span>';
    }
    return '<span class="badge bg-secondary ms-1">group</span>';
}

function stateBadge(p) {
    if (!p.available) return '<span class="badge bg-secondary">offline</span>';
    const map = {
        playing: 'bg-success', paused: 'bg-warning text-dark',
        buffering: 'bg-info text-dark', idle: 'bg-light text-muted',
        unknown: 'bg-light text-muted',
    };
    return `<span class="badge ${map[p.state] || 'bg-light text-muted'}">${esc(p.state)}</span>`;
}

function renderPlayers() {
    const el = document.getElementById('mediaPlayers');
    if (!el) return;
    if (_groupBuilderOpen) return renderGroupBuilder();
    // "This device" is always in _players, so an empty *remote* list is the
    // real "nothing discovered" case — still usable, just local-only.
    const noRemote = !_remote.length
        ? `<div class="text-muted small text-center pb-2">
             No speakers found — playing on this device still works. Add WiiM device IPs
             under Settings → APIs, and make sure Cast devices are on the same subnet.</div>`
        : '';
    el.innerHTML = noRemote + _players.map(p => {
        const selected = p.player_id === _selectedId;
        const playing = p.state === 'playing';
        const lyricsLink = (p.media_type === 'tidal' && p.now_playing_id)
            ? ` <button class="btn btn-link p-0 ms-1 align-baseline" title="Lyrics" style="font-size:.72rem"
                  onclick="event.stopPropagation();window.mediaTidalLyrics('${esc(p.now_playing_id)}', ${JSON.stringify(p.title || 'Lyrics').replace(/"/g, '&quot;')})"><i class="fas fa-align-left"></i></button>`
              + ` <button class="btn btn-link p-0 ms-1 align-baseline" title="Full-screen synced lyrics" style="font-size:.72rem"
                  onclick="event.stopPropagation();window.mediaLyricsScreen('${esc(p.player_id)}')"><i class="fas fa-closed-captioning"></i></button>`
            : '';
        const nowPlaying = (p.title || p.artist)
            ? `<div class="small text-truncate">${esc(p.title)}${p.artist ? ' — ' + esc(p.artist) : ''}${lyricsLink}</div>`
            : '<div class="small text-muted fst-italic">Nothing playing</div>';
        const pidE = esc(p.player_id);
        // Position bar for tracks with a known length (radio is live → no bar).
        const prog = (p.duration_ms > 0) ? `
          <div class="d-flex align-items-center gap-2 mt-1">
            <div class="progress flex-grow-1" style="height:4px">
              <div id="prog-${pidE}" class="progress-bar bg-info zmm-media-progress"
                   style="width:${Math.min(100, (p.position_ms || 0) / p.duration_ms * 100).toFixed(2)}%"></div>
            </div>
            <small class="text-muted" id="ptime-${pidE}" style="font-variant-numeric:tabular-nums;white-space:nowrap">${_fmtTime(p.position_ms || 0)} / ${_fmtTime(p.duration_ms)}</small>
          </div>` : '';
        // Album art thumbnail + an artist button (radio + other albums) for Tidal.
        const art = (p.artwork_url && (p.title || p.artist))
            ? `<img src="${esc(p.artwork_url)}" alt="" style="width:46px;height:46px;border-radius:6px;object-fit:cover;flex:0 0 auto">`
            : '';
        const artistBtn = (p.media_type === 'tidal' && p.now_playing_id)
            ? `<button class="btn btn-link p-0 ms-1 align-self-center text-decoration-none" title="Artist: radio & other albums"
                  onclick="event.stopPropagation();window.mediaArtistPanel('${esc(p.player_id)}','${esc(p.now_playing_id)}')"><i class="fas fa-record-vinyl"></i></button>`
            : '';
        const vol = Math.round((p.volume || 0) * 100);
        const disabled = p.available ? '' : 'disabled';
        const pid = esc(p.player_id);
        const q = p.queue;                       // queue summary or null
        const hasQueue = q && q.length > 1;
        const ctlOpen = _ctlOpen.has(p.player_id);
        return `
        <div class="border rounded p-2 mb-2 ${selected ? 'border-primary bg-primary-subtle' : ''}"
             onclick="window.mediaSelect('${pid}')" style="cursor:pointer">
          <div class="d-flex justify-content-between align-items-center">
            <div class="text-truncate me-2">
              <i class="${iconFor(p)} me-1 text-muted"></i>
              <span class="fw-semibold">${esc(p.name)}</span>
              ${groupBadge(p)}
            </div>
            <div class="d-flex align-items-center gap-2 flex-shrink-0">
              ${stateBadge(p)}
              <span class="small text-muted d-lg-none" id="volhd-${pidE}"
                    style="font-variant-numeric:tabular-nums">${vol}%</span>
              <button class="btn btn-sm btn-outline-secondary d-lg-none py-0 px-2"
                      aria-expanded="${ctlOpen}" aria-controls="ctl-${pidE}"
                      title="${ctlOpen ? 'Hide' : 'Show'} controls"
                      aria-label="${ctlOpen ? 'Hide' : 'Show'} controls for ${esc(p.name)}"
                      onclick="event.stopPropagation();window.mediaCtlToggle('${pid}')">
                <i class="fas ${ctlOpen ? 'fa-chevron-up' : 'fa-chevron-down'}"></i>
              </button>
            </div>
          </div>
          <div class="d-flex gap-2 align-items-center mt-1">
            ${art}
            <div class="flex-grow-1" style="min-width:0">
              ${nowPlaying}
              ${prog}
            </div>
            ${artistBtn}
          </div>
          ${q ? `<div class="small text-muted">Track ${q.index + 1} of ${q.length}</div>` : ''}
          <div id="artist-${pidE}" class="mt-2"></div>
          <div class="zmm-player-ctl${ctlOpen ? ' zmm-ctl-open' : ''}" id="ctl-${pidE}">
          <div class="d-flex align-items-center gap-2 mt-2 flex-wrap" onclick="event.stopPropagation()">
            <button class="btn btn-sm btn-outline-secondary" ${disabled} ${hasQueue ? '' : 'disabled'}
                    onclick="window.mediaControl('${pid}','prev')" title="Previous">
              <i class="fas fa-backward-step"></i>
            </button>
            <button class="btn btn-sm btn-outline-secondary" ${disabled}
                    onclick="window.mediaControl('${pid}','${playing ? 'pause' : 'resume'}')">
              <i class="fas ${playing ? 'fa-pause' : 'fa-play'}"></i>
            </button>
            <button class="btn btn-sm btn-outline-secondary" ${disabled} ${hasQueue ? '' : 'disabled'}
                    onclick="window.mediaControl('${pid}','next')" title="Next">
              <i class="fas fa-forward-step"></i>
            </button>
            <button class="btn btn-sm btn-outline-secondary" ${disabled}
                    onclick="window.mediaControl('${pid}','stop')" title="Stop">
              <i class="fas fa-stop"></i>
            </button>
            <button class="btn btn-sm ${_eqOpen.has(p.player_id) ? 'btn-primary' : 'btn-outline-secondary'}" ${disabled}
                    onclick="window.mediaEqToggle('${pid}')" title="Equaliser">
              <i class="fas fa-sliders"></i>
            </button>
            <div class="d-flex align-items-center gap-1 flex-grow-1 zmm-vol">
              <button class="btn btn-sm btn-outline-secondary zmm-vol-step" ${disabled}
                      onclick="window.mediaVolStep('${pid}', -5)" title="Volume down 5%">
                <i class="fas fa-volume-low"></i>
              </button>
              <input type="range" class="form-range flex-grow-1" id="vol-${pidE}" min="0" max="100" value="${vol}" ${disabled}
                     oninput="window.mediaVolLabel('${pid}', this.value)"
                     onchange="window.mediaSetVolume('${pid}', this.value)">
              <button class="btn btn-sm btn-outline-secondary zmm-vol-step" ${disabled}
                      onclick="window.mediaVolStep('${pid}', 5)" title="Volume up 5%">
                <i class="fas fa-volume-high"></i>
              </button>
              <span class="small text-muted" id="vol-lbl-${pidE}" style="width:2.5em">${vol}%</span>
            </div>
            ${p.is_group && p.provider === 'wiim'
                ? `<button class="btn btn-sm btn-outline-danger" onclick="window.mediaUngroup('${pid}')" title="Ungroup">
                     <i class="far fa-object-ungroup"></i></button>`
                : ''}
          </div>
          <div id="eqp-${pidE}" onclick="event.stopPropagation()"></div>
          ${q && p.provider !== 'local' ? queueControls(p, q) : ''}
          </div>
        </div>`;
    }).join('');
    for (const pid of _eqOpen) renderEqPanel(pid);
    if (!_eqOpen.has(LOCAL_ID)) unmountScope('eqspec-local');
    updateSearchTarget();
}

// Repeat/shuffle toggles + an up-next preview, shown when a queue exists.
function queueControls(p, q) {
    const pid = esc(p.player_id);
    const repeatNext = { off: 'all', all: 'one', one: 'off' }[q.repeat] || 'all';
    const repeatIcon = q.repeat === 'one' ? 'fa-1' : 'fa-repeat';
    const repeatActive = q.repeat !== 'off' ? 'btn-primary' : 'btn-outline-secondary';
    const shuffleActive = q.shuffle ? 'btn-primary' : 'btn-outline-secondary';
    const upNext = (q.items || []).slice(q.index + 1, q.index + 4)
        .map(qi => `<div class="text-truncate text-muted">• ${esc(qi.item.title || qi.item.url)}</div>`)
        .join('');
    return `
      <div class="d-flex align-items-center gap-2 mt-2 pt-2 border-top" onclick="event.stopPropagation()">
        <button class="btn btn-sm ${repeatActive}" title="Repeat: ${q.repeat}"
                onclick="window.mediaQueueMode('${pid}', {repeat:'${repeatNext}'})">
          <i class="fas ${repeatIcon}"></i>
        </button>
        <button class="btn btn-sm ${shuffleActive}" title="Shuffle"
                onclick="window.mediaQueueMode('${pid}', {shuffle:${!q.shuffle}})">
          <i class="fas fa-shuffle"></i>
        </button>
        <button class="btn btn-sm btn-outline-danger ms-auto" title="Clear queue"
                onclick="window.mediaQueueClear('${pid}')">
          <i class="fas fa-trash"></i>
        </button>
      </div>
      ${upNext ? `<div class="small mt-1">${upNext}</div>` : ''}`;
}

// Equaliser panel — three flavours behind one button:
//   local → the Web Audio 10-band EQ (eq.js): sliders + presets, client-side
//   wiim  → the speaker's own DSP presets via /api/media/eq (device EQ list)
//   cast  → the SAME 10-band sliders, but applied by the server's DSP stream
//           proxy (Rust biquads on the audio feeding the speaker). Slider
//           moves retune the running stream live; only the on/off switch
//           reloads the track (the audio path itself changes).
function eqToggle(pid) {
    if (_eqOpen.has(pid)) _eqOpen.delete(pid);
    else {
        delete _eqDev[pid];   // refetch on open — state may have changed elsewhere
        _eqOpen.add(pid);
    }
    renderPlayers();          // repaints the button state + panel container
}

function renderEqPanel(pid) {
    const box = document.getElementById('eqp-' + pid);
    if (!box) return;
    const p = _players.find(x => x.player_id === pid);
    if (!p) { box.innerHTML = ''; return; }
    if (p.provider === 'local') {
        box.innerHTML = eqLocalHtml();
        // Local playback: the analyser is in this tab, and the curve is drawn
        // from the same gains the biquads are running.
        mountScope('eqspec-local', {
            getFrame: () => eq.eqFrame(),
            getEq: () => {
                const s = eq.eqState();
                return { enabled: s.enabled, gains: s.gains,
                         bands: eq.BANDS, rate: eq.eqRate() };
            },
        });
        return;
    }
    const c = _eqDev[pid];
    if (!c) {
        eqDevFetch(pid);
        box.innerHTML = `<div class="border-top mt-2 pt-2">${spinner()}</div>`;
        return;
    }
    if (c.error) {
        box.innerHTML = `<div class="border-top mt-2 pt-2 small text-muted">${esc(c.error)}</div>`;
        return;
    }
    if (!c.supported || !c.eq) {
        box.innerHTML = `<div class="border-top mt-2 pt-2 small text-muted">
            This speaker doesn't expose EQ control.</div>`;
        return;
    }
    box.innerHTML = c.eq.mode === 'bands' ? eqCastHtml(pid, c.eq) : eqWiimHtml(pid, c.eq);
}

const _fmtDb = v => (v > 0 ? '+' : '') + Number(v).toFixed(1).replace(/\.0$/, '');

function eqLocalHtml() {
    const st = eq.eqState();
    const names = Object.keys(eq.PRESETS);
    const opts = (st.preset === 'Custom' ? ['Custom', ...names] : names)
        .map(n => `<option value="${esc(n)}" ${n === st.preset ? 'selected' : ''}>${esc(n)}</option>`)
        .join('');
    const bypassed = local.eqBypassed();
    return `
      <div class="border-top mt-2 pt-2 zmm-eq">
        <div class="d-flex align-items-center gap-2 flex-wrap mb-1">
          <div class="form-check form-switch m-0">
            <input class="form-check-input" type="checkbox" id="eqOnLocal" ${st.enabled ? 'checked' : ''}
                   onchange="window.mediaEqEnable('${LOCAL_ID}', this.checked)">
            <label class="form-check-label small fw-semibold" for="eqOnLocal">EQ</label>
          </div>
          <select class="form-select form-select-sm w-auto" id="eqPresetSel" title="Preset"
                  onchange="window.mediaEqPreset('${LOCAL_ID}', this.value)">${opts}</select>
          <span class="small text-muted ms-auto" id="eqPreampLbl"
                title="Automatic headroom: the signal is lowered by the largest boost so no band can clip">
            preamp ${st.preampDb.toFixed(1)} dB</span>
        </div>
        ${bypassed ? `<div class="small text-warning mb-1"><i class="fas fa-triangle-exclamation me-1"></i>
            This stream refused browser audio processing (no CORS) and the server relay
            failed too — playing it unprocessed.</div>` : ''}
        ${st.enabled ? `<div class="small text-muted mb-1"><i class="fas fa-mobile-screen me-1"></i>
            With the EQ on, playback needs this page awake — a phone that locks its
            screen suspends browser audio processing. Switch the EQ off for
            background listening.</div>` : ''}
        <canvas id="eqspec-local" class="zmm-eq-spec" height="46"></canvas>
        <div class="zmm-eq-bands">
          ${eq.BANDS.map((f, i) => `
            <div class="zmm-eq-band">
              <span class="zmm-eq-db" id="eqdb-${i}">${_fmtDb(st.gains[i])}</span>
              <input type="range" class="zmm-eq-slider" orient="vertical"
                     min="${eq.GAIN_MIN}" max="${eq.GAIN_MAX}" step="0.5" value="${st.gains[i]}"
                     ${st.enabled ? '' : 'disabled'} aria-label="${eq.BAND_LABELS[i]} Hz band"
                     oninput="window.mediaEqBand(${i}, this.value)">
              <span class="zmm-eq-hz">${eq.BAND_LABELS[i]}</span>
            </div>`).join('')}
        </div>
      </div>`;
}

function eqWiimHtml(pid, info) {
    const cur = info.preset || '';
    const opts = [`<option value="" ${cur ? '' : 'selected'} disabled>${cur ? '' : 'Choose preset…'}</option>`,
        ...info.presets.map(n =>
            `<option value="${esc(n)}" ${n === cur ? 'selected' : ''}>${esc(n)}</option>`)].join('');
    return `
      <div class="border-top mt-2 pt-2 zmm-eq">
        <div class="d-flex align-items-center gap-2 flex-wrap">
          <div class="form-check form-switch m-0">
            <input class="form-check-input" type="checkbox" id="eqOn-${esc(pid)}" ${info.enabled ? 'checked' : ''}
                   onchange="window.mediaEqEnable('${esc(pid)}', this.checked)">
            <label class="form-check-label small fw-semibold" for="eqOn-${esc(pid)}">EQ</label>
          </div>
          <select class="form-select form-select-sm w-auto"
                  onchange="window.mediaEqPreset('${esc(pid)}', this.value)">${opts}</select>
          <span class="small text-muted ms-auto"
                title="EQ runs on the speaker itself — per-band control isn't exposed by the WiiM API">
            <i class="fas fa-microchip me-1"></i>device DSP</span>
        </div>
      </div>`;
}

// Cast panel: identical band sliders to "This device", but the DSP runs on
// the server, inside the stream feeding the speaker.
function eqCastHtml(pid, info) {
    const pidE = esc(pid);
    if (!info.available) {
        return `<div class="border-top mt-2 pt-2 small text-muted">
            <i class="fas fa-sliders me-1"></i>Cast EQ needs the server DSP stream:
            ${esc(info.reason || 'not available')}</div>`;
    }
    const names = Object.keys(eq.PRESETS);
    const withCustom = names.includes(info.preset) ? names : ['Custom', ...names];
    const opts = withCustom
        .map(n => `<option value="${esc(n)}" ${n === (info.preset || 'Flat') ? 'selected' : ''}>${esc(n)}</option>`)
        .join('');
    const live = info.live
        ? '<span class="badge bg-success" title="EQ stream running — sliders retune it live">live</span>'
        : (info.enabled
            ? '<span class="badge bg-secondary" title="Applies when the next track starts through the EQ stream">armed</span>'
            : '');
    return `
      <div class="border-top mt-2 pt-2 zmm-eq">
        <div class="d-flex align-items-center gap-2 flex-wrap mb-1">
          <div class="form-check form-switch m-0">
            <input class="form-check-input" type="checkbox" id="eqrOn-${pidE}" ${info.enabled ? 'checked' : ''}
                   onchange="window.mediaEqEnable('${pidE}', this.checked)">
            <label class="form-check-label small fw-semibold" for="eqrOn-${pidE}">EQ</label>
          </div>
          <select class="form-select form-select-sm w-auto" id="eqrSel-${pidE}" title="Preset"
                  onchange="window.mediaEqPreset('${pidE}', this.value)">${opts}</select>
          ${live}
          <span class="small text-muted ms-auto"
                title="Processed on the server before it reaches the speaker — Cast devices have no DSP of their own">
            <i class="fas fa-server me-1"></i>stream DSP</span>
        </div>
        <div class="zmm-eq-bands">
          ${info.gains.map((g, i) => `
            <div class="zmm-eq-band">
              <span class="zmm-eq-db" id="eqrdb-${pidE}-${i}">${_fmtDb(g)}</span>
              <input type="range" class="zmm-eq-slider" orient="vertical"
                     min="-${info.gain_limit || 12}" max="${info.gain_limit || 12}" step="0.5" value="${g}"
                     ${info.enabled ? '' : 'disabled'} aria-label="${eq.BAND_LABELS[i]} Hz band"
                     oninput="window.mediaEqBandRemote('${pidE}', ${i}, this.value)">
              <span class="zmm-eq-hz">${eq.BAND_LABELS[i]}</span>
            </div>`).join('')}
        </div>
      </div>`;
}

async function eqDevFetch(pid) {
    const d = await apiGet('/api/media/eq?player_id=' + encodeURIComponent(pid));
    _eqDev[pid] = d.success ? d : { error: d.error || 'EQ query failed' };
    if (_eqOpen.has(pid)) renderEqPanel(pid);
}

async function eqEnable(pid, on) {
    if (pid === LOCAL_ID) {
        eq.eqSetEnabled(on);
        local.eqRoutingChanged();     // audio path itself changes — see local-player.js
        renderEqPanel(pid);           // sliders follow the enabled state
        return;
    }
    const r = await apiPost('/api/media/eq', { player_id: pid, enabled: !!on });
    if (!r.success) { toast(r.error || 'EQ change failed', 'error'); return; }
    _eqDev[pid] = { success: true, supported: true, eq: r.eq };
    renderEqPanel(pid);
    if (r.eq && r.eq.restarted) toast('Reloading the track through the EQ stream', 'info');
}

async function eqPreset(pid, name) {
    if (pid === LOCAL_ID) {
        eq.eqApplyPreset(name);
        local.eqRoutingChanged();     // a named curve switches the EQ on
        renderEqPanel(pid);
        return;
    }
    if (!name || name === 'Custom') return;
    const body = { player_id: pid, preset: name };
    const c = _eqDev[pid];
    // Band-mode targets (Cast) get the preset's curve as explicit gains —
    // the preset library lives client-side, the server just applies numbers.
    if (c && c.eq && c.eq.mode === 'bands' && eq.PRESETS[name]) body.gains = eq.PRESETS[name];
    const r = await apiPost('/api/media/eq', body);
    if (!r.success) { toast(r.error || 'EQ preset failed', 'error'); return; }
    _eqDev[pid] = { success: true, supported: true, eq: r.eq };
    renderEqPanel(pid);
    toast(`EQ preset: ${name}`, 'success');
}

// Remote band drag: label updates instantly, the POST is debounced so a drag
// becomes a handful of calls, and the server retunes the running stream live.
function eqBandRemote(pid, i, val) {
    const c = _eqDev[pid];
    if (!c || !c.eq) return;
    const pend = _eqSend[pid] || (_eqSend[pid] = { timer: null, gains: c.eq.gains.slice() });
    pend.gains[i] = Number(val);
    const lbl = document.getElementById(`eqrdb-${pid}-${i}`);
    if (lbl) lbl.textContent = _fmtDb(pend.gains[i]);
    const sel = document.getElementById(`eqrSel-${pid}`);
    if (sel && sel.value !== 'Custom') {
        if (!sel.querySelector('option[value="Custom"]'))
            sel.insertAdjacentHTML('afterbegin', '<option value="Custom">Custom</option>');
        sel.value = 'Custom';
    }
    clearTimeout(pend.timer);
    pend.timer = setTimeout(async () => {
        delete _eqSend[pid];
        const r = await apiPost('/api/media/eq',
            { player_id: pid, gains: pend.gains, preset: 'Custom' });
        if (r.success) _eqDev[pid] = { success: true, supported: true, eq: r.eq };
        else toast(r.error || 'EQ update failed', 'error');
    }, 250);
}

// Slider drags update the engine + labels in place — no re-render, so the
// drag never fights the DOM (same principle as the volume slider).
function eqBand(i, val) {
    eq.eqSetBand(i, Number(val));
    const st = eq.eqState();
    const db = document.getElementById('eqdb-' + i);
    if (db) db.textContent = _fmtDb(st.gains[i]);
    const pre = document.getElementById('eqPreampLbl');
    if (pre) pre.textContent = `preamp ${st.preampDb.toFixed(1)} dB`;
    const sel = document.getElementById('eqPresetSel');
    if (sel && sel.value !== 'Custom') {
        if (!sel.querySelector('option[value="Custom"]'))
            sel.insertAdjacentHTML('afterbegin', '<option value="Custom">Custom</option>');
        sel.value = 'Custom';
    }
}

// Below lg a card's controls are collapsed until asked for: with every card
// open, a scroll that starts on a volume slider drags it instead of the page —
// which is how a speaker ends up at 100%. One card open at a time keeps the
// sliders that aren't being aimed at out of reach. Desktop ignores the class.
function ctlToggle(pid) {
    if (_ctlOpen.has(pid)) _ctlOpen.delete(pid);
    else { _ctlOpen.clear(); _ctlOpen.add(pid); }
    // An EQ panel inside a folded card is a canvas animating where nobody can
    // see it — fold the card, close its equaliser.
    for (const id of [..._eqOpen]) if (!_ctlOpen.has(id)) _eqOpen.delete(id);
    renderPlayers();
}

// Slider drag → both readouts (the one beside it, and the collapsed-card one
// in the header) without a re-render, which would fight the drag.
function volLabel(pid, value) {
    for (const id of ['vol-lbl-' + pid, 'volhd-' + pid]) {
        const el = document.getElementById(id);
        if (el) el.textContent = value + '%';
    }
}

function selectPlayer(id) {
    _selectedId = id;
    renderPlayers();
    const p = _players.find(x => x.player_id === id);
    if (p) toast(`Selected ${p.name} — playback targets this player`, 'info');
    updateSearchTarget();
    // On a phone, picking a target is the cue that Browse is what's next.
    if (_pane === 'players' && window.matchMedia('(max-width: 991.98px)').matches) setPane('browse');
}

// Mobile panes — below lg only one of Players/Browse is shown at a time.
// The CSS class is a no-op at lg and up, so desktop always shows both.
function setPane(p) {
    _pane = p;
    document.getElementById('mediaPanePlayers')?.classList.toggle('zmm-pane-hidden', p !== 'players');
    document.getElementById('mediaPaneBrowse')?.classList.toggle('zmm-pane-hidden', p !== 'browse');
    for (const [id, on] of [['mediaPaneBtnPlayers', p === 'players'], ['mediaPaneBtnBrowse', p === 'browse']]) {
        const b = document.getElementById(id);
        if (!b) continue;
        b.classList.toggle('btn-primary', on);
        b.classList.toggle('btn-outline-primary', !on);
    }
}

// Land on whichever pane still has work to do: pick a target first, then
// search. Only applies until the user taps the switch themselves.
function autoPane() {
    if (_pane) return;
    setPane(_selectedId ? 'browse' : 'players');
}

// Sticky mobile bar: where audio is going, and what's on.
function renderNowBar() {
    const el = document.getElementById('mediaNowBar');
    if (!el) return;
    const p = _players.find(x => x.player_id === _selectedId);
    if (!p) {
        el.classList.remove('d-none');
        el.innerHTML = '<span class="text-muted"><i class="fas fa-circle-exclamation me-1"></i>No player selected</span>';
        return;
    }
    el.classList.remove('d-none');
    const playing = p.state === 'playing';
    const what = (p.title || p.artist)
        ? `${esc(p.title)}${p.artist ? ' — ' + esc(p.artist) : ''}`
        : '<span class="text-muted fst-italic">Nothing playing</span>';
    el.innerHTML = `
      <div class="d-flex align-items-center gap-2">
        <i class="${iconFor(p)} text-muted"></i>
        <div class="flex-grow-1 text-truncate">
          <div class="text-truncate">${what}</div>
          <div class="text-muted" style="font-size:.72rem">→ ${esc(p.name)}</div>
        </div>
        ${p.available ? `
        <button class="btn btn-sm btn-outline-secondary"
                onclick="window.mediaControl('${esc(p.player_id)}','${playing ? 'pause' : 'resume'}')"
                aria-label="${playing ? 'Pause' : 'Play'}">
          <i class="fas ${playing ? 'fa-pause' : 'fa-play'}"></i>
        </button>` : ''}
      </div>`;
}

function updateSearchTarget() {
    const el = document.getElementById('mediaSearchTarget');
    if (!el) return;
    const p = _players.find(x => x.player_id === _selectedId);
    el.textContent = p ? `→ ${p.name}` : 'select a player';
    renderNowBar();
    notifyTherapyFrame();
}

// Tell the therapy iframe which player is selected — therapy plays on the
// media tab's selected player, the same flow as radio/Tidal.
function notifyTherapyFrame() {
    const frame = document.getElementById('mediaTherapyFrame');
    if (!frame || !frame.contentWindow) return;
    // "This device" is deliberately reported as no player: therapy already
    // falls back to its own in-browser synth when nothing is selected, which
    // is exactly what local playback means there. Casting to 'local:browser'
    // would be meaningless.
    const p = _players.find(x => x.player_id === _selectedId && x.provider !== 'local');
    frame.contentWindow.postMessage({
        type: 'zmm-selected-player',
        id: p ? p.player_id : null,
        name: p ? p.name : null,
    }, location.origin);
}

async function control(playerId, action) {
    // "This device" is driven by the <audio> element, not the media API.
    if (playerId === LOCAL_ID) {
        ({ pause: local.pause, resume: local.resume, stop: local.stop,
           next: local.next, prev: local.prev }[action] || (() => {}))();
        return;
    }
    const r = await apiPost('/api/media/control', { player_id: playerId, action });
    if (!r.success) toast(r.error || 'Control failed', 'error');
    else loadPlayers();
}

async function setVolume(playerId, value) {
    const level = Math.max(0, Math.min(1, Number(value) / 100));
    if (playerId === LOCAL_ID) { local.setVolume(level); return; }
    const r = await apiPost('/api/media/volume', { player_id: playerId, level });
    if (!r.success) toast(r.error || 'Volume failed', 'error');
}

// ±step nudge for the mobile volume buttons. Reads the slider rather than the
// last snapshot so repeated taps compound instead of racing the 10s refresh.
function volStep(playerId, delta) {
    const slider = document.getElementById('vol-' + playerId);
    if (!slider || slider.disabled) return;
    const next = Math.max(0, Math.min(100, Number(slider.value) + delta));
    slider.value = next;
    volLabel(playerId, next);
    setVolume(playerId, next);
}

// Queue mode (repeat / shuffle / clear)
async function queueMode(playerId, mode) {
    const r = await apiPost('/api/media/queue/mode', { player_id: playerId, ...mode });
    if (!r.success) toast(r.error || 'Queue update failed', 'error');
    else loadPlayers();
}

async function queueClear(playerId) {
    const r = await apiPost('/api/media/queue/clear', { player_id: playerId, action: 'clear' });
    if (!r.success) toast(r.error || 'Clear failed', 'error');
    else loadPlayers();
}

function requireSelected() {
    if (!_selectedId) {
        toast('Select a player first (click one in the Players list)', 'warning');
        return false;
    }
    return true;
}

// Search — Radio / Tidal / Therapy source switch
function setSource(src) {
    _searchSource = src;
    const mark = (id, on) => {
        const b = document.getElementById(id);
        if (!b) return;
        b.classList.toggle('btn-primary', on);
        b.classList.toggle('btn-outline-primary', !on);
    };
    mark('mediaSrcRadio', src === 'radio');
    mark('mediaSrcTidal', src === 'tidal');
    mark('mediaTherapyBtn', src === 'therapy');

    // Therapy swaps the whole card body for the SPA iframe. Both panes stay
    // in the DOM so a running therapy session keeps playing while hidden.
    const isTherapy = src === 'therapy';
    document.getElementById('mediaSourceContent')?.classList.toggle('d-none', isTherapy);
    document.getElementById('mediaTherapyPane')?.classList.toggle('d-none', !isTherapy);
    if (isTherapy) {
        const frame = document.getElementById('mediaTherapyFrame');
        // Full path to index.html — the StaticFiles mount has no directory-index
        // support, so '/static/therapy/' alone would return FastAPI's JSON 404.
        if (frame && !frame.src) frame.src = '/static/therapy/index.html';
        notifyTherapyFrame();
        return;
    }

    const input = document.getElementById('mediaSearchQuery');
    if (input) input.placeholder = src === 'tidal'
        ? 'Search Tidal (tracks, albums, artists, playlists)'
        : 'Search stations (e.g. jazz, BBC, classical)';
    document.getElementById('mediaSearchResults').innerHTML = '';
    refreshTidalNotice();
    if (src === 'tidal') { renderTidalTabs(); renderRadioFavStrip(); }
    else { showSearchBar(true); renderRadioFavStrip(); }
}

// Tidal sub-tabs: Search | Playlists | Albums | Artists (the library)
function renderTidalTabs() {
    const out = document.getElementById('mediaSearchResults');
    if (!out) return;
    const tab = (key, label) =>
        `<li class="nav-item"><button class="nav-link ${_tidalTab === key ? 'active' : ''}"
            onclick="window.mediaTidalTab('${key}')">${label}</button></li>`;
    out.innerHTML = `
      <ul class="nav nav-pills nav-fill mb-2 small">
        ${tab('search', 'Search')}${tab('mixes', 'Mixes')}${tab('playlists', 'Playlists')}${tab('albums', 'Albums')}${tab('artists', 'Artists')}
      </ul>
      <div id="tidalTabContent"></div>`;
    tidalTab(_tidalTab);
}

function tidalTab(key) {
    _tidalTab = key;
    document.querySelectorAll('#mediaSearchResults .nav-link').forEach(b =>
        b.classList.toggle('active', b.textContent.trim().toLowerCase() === key));
    showSearchBar(key === 'search');
    if (key === 'search') {
        const c = document.getElementById('tidalTabContent');
        if (c && !c.innerHTML) c.innerHTML = '<div class="text-muted small py-2">Type above to search Tidal.</div>';
    } else {
        loadTidalLibrary(key);
    }
}

function showSearchBar(show) {
    const bar = document.getElementById('mediaSearchBar');
    if (bar) bar.style.display = show ? '' : 'none';
}

async function doSearch() {
    return _searchSource === 'tidal' ? tidalSearch() : radioSearch();
}

async function radioSearch() {
    const q = document.getElementById('mediaSearchQuery')?.value?.trim();
    const out = document.getElementById('mediaSearchResults');
    if (!q || !out) return;
    out.innerHTML = spinner();
    const data = await apiGet(`/api/media/radio/search?q=${encodeURIComponent(q)}&limit=25`);
    if (!data.success) { out.innerHTML = warn(data.error || 'Search failed'); return; }
    const stations = data.stations || [];
    if (!stations.length) { out.innerHTML = '<div class="text-muted small py-2">No stations found.</div>'; return; }
    _radioSearchCache = stations;
    if (stations.some(s => s.hls)) local.warmHls();   // load it before play is clicked
    out.innerHTML = stations.map((s, i) => {
        const faved = isFav(s.uuid);
        return `
        <div class="d-flex justify-content-between align-items-center border-bottom py-1">
          <div class="text-truncate me-2">
            <div class="small fw-semibold text-truncate">${esc(s.name)}</div>
            <div class="text-muted" style="font-size:.72rem">${esc(s.country)} ${s.bitrate ? '· ' + s.bitrate + 'kbps' : ''} ${esc(s.codec)}</div>
          </div>
          <div class="btn-group btn-group-sm flex-shrink-0">
            <button class="btn ${faved ? 'btn-warning' : 'btn-outline-warning'}" title="${faved ? 'Remove favourite' : 'Add favourite'}"
                    onclick="window.mediaRadioFavAdd(${i})">
              <i class="${faved ? 'fas' : 'far'} fa-star"></i>
            </button>
            <button class="btn btn-outline-success" title="Play"
                    onclick="window.mediaPlayStation('${esc(s.uuid)}', ${JSON.stringify(s.name).replace(/"/g, '&quot;')})">
              <i class="fas fa-play"></i>
            </button>
          </div>
        </div>`;
    }).join('');
}

// Radio favourites
function isFav(uuid) {
    return _radioFavs.some(f => f.uuid === uuid);
}

async function loadRadioFavourites() {
    const data = await apiGet('/api/media/radio/favourites');
    _radioFavs = (data && data.success && data.stations) ? data.stations : [];
    if (_radioFavs.some(f => f.hls)) local.warmHls();   // see radioSearch()
    renderRadioFavStrip();
}

function renderRadioFavStrip() {
    const strip = document.getElementById('mediaRadioFav');
    if (!strip) return;
    if (!_radioFavs.length) { strip.innerHTML = ''; return; }
    strip.innerHTML = `
      <div class="small text-muted mb-1"><i class="fas fa-star text-warning me-1"></i>Favourite stations</div>
      <div class="d-flex flex-wrap gap-1">
        ${_radioFavs.map(f => `
          <div class="btn-group btn-group-sm">
            <button class="btn btn-outline-primary" title="Play ${esc(f.name)}"
                    onclick="window.mediaPlayFav('${esc(f.uuid)}', ${JSON.stringify(f.name).replace(/"/g, '&quot;')})">
              <i class="fas fa-play me-1"></i>${esc(f.name)}
            </button>
            <button class="btn btn-outline-secondary" title="Remove favourite"
                    onclick="window.mediaRadioFavRemove('${esc(f.uuid)}')">
              <i class="fas fa-times"></i>
            </button>
          </div>`).join('')}
      </div>`;
}

async function radioFavAdd(index) {
    const s = _radioSearchCache[index];
    if (!s) return;
    if (isFav(s.uuid)) { return radioFavRemove(s.uuid); }  // star toggles off
    const r = await apiPost('/api/media/radio/favourites', s);
    if (!r.success) { toast(r.error || 'Could not save favourite', 'error'); return; }
    _radioFavs.push(s);
    renderRadioFavStrip();
    radioSearch();                       // refresh stars in the result list
    toast(`Added ${s.name}`, 'success');
}

async function radioFavRemove(uuid) {
    const res = await fetch(`/api/media/radio/favourites/${encodeURIComponent(uuid)}`, { method: 'DELETE' });
    const r = await res.json().catch(() => ({}));
    if (!r.success) { toast(r.error || 'Could not remove favourite', 'error'); return; }
    _radioFavs = _radioFavs.filter(f => f.uuid !== uuid);
    renderRadioFavStrip();
    if (_searchSource === 'radio' && _radioSearchCache.length) radioSearch();
}

// Resolve something to a browser-playable queue and hand it to the <audio>.
// Shared by radio and Tidal — the only difference is what we ask for.
async function playLocal(body, name) {
    const r = await apiPost('/api/media/local/playlist', body);
    if (!r.success) { toast(r.error || 'Could not resolve for local playback', 'error'); return; }
    await local.playItems(r.items || []);
    const n = (r.items || []).length;
    toast(`Playing ${name} on this device${n > 1 ? ` (${n} tracks)` : ''}`, 'success');
}

// Snapshot for a uuid from what's already on screen; sent with every play so
// a directory outage can't lose a station we can already see.
function knownStation(uuid) {
    return _radioFavs.find(f => f.uuid === uuid)
        || _radioSearchCache.find(s => s.uuid === uuid)
        || null;
}

async function playFavourite(uuid, name) {
    if (!requireSelected()) return;
    const station = knownStation(uuid);
    if (_selectedId === LOCAL_ID) return playLocal({ station_uuid: uuid, station }, name);
    const r = await apiPost('/api/media/radio/favourites/play',
        { player_id: _selectedId, station_uuid: uuid, station });
    if (!r.success) toast(r.error || 'Play failed', 'error');
    else { toast(`Playing ${name}`, 'success'); setTimeout(loadPlayers, 1500); setTimeout(loadRecent, 2000); }
}

async function playStation(uuid, name) {
    if (!requireSelected()) return;
    const station = knownStation(uuid);
    if (_selectedId === LOCAL_ID) return playLocal({ station_uuid: uuid, station }, name);
    const r = await apiPost('/api/media/play',
        { player_id: _selectedId, station_uuid: uuid, station });
    if (!r.success) toast(r.error || 'Play failed', 'error');
    else { toast(`Playing ${name}`, 'success'); setTimeout(loadPlayers, 1500); setTimeout(loadRecent, 2000); }
}

// Tidal
async function refreshTidalNotice() {
    const notice = document.getElementById('mediaTidalNotice');
    if (!notice) return;
    if (_searchSource !== 'tidal') { notice.innerHTML = ''; return; }
    const data = await apiGet('/api/media/tidal/status');
    _tidalState = data.success ? data.status.state : 'unavailable';
    if (_tidalState === 'logged_in') { notice.innerHTML = ''; return; }
    const msg = {
        unavailable: 'Tidal isn\'t enabled. Turn it on under Settings → APIs → Media.',
        logged_out: 'Not logged in to Tidal. Log in under Settings → APIs → Media.',
        pending: 'Tidal login pending — finish authorising in the opened link.',
    }[_tidalState] || 'Tidal unavailable.';
    notice.innerHTML = `<div class="alert alert-warning small py-2">${esc(msg)}</div>`;
}

async function tidalSearch() {
    const q = document.getElementById('mediaSearchQuery')?.value?.trim();
    const out = document.getElementById('tidalTabContent');
    if (!q || !out) return;
    if (_tidalState !== 'logged_in') { await refreshTidalNotice(); if (_tidalState !== 'logged_in') return; }
    out.innerHTML = spinner();
    const data = await apiGet(`/api/media/tidal/search?q=${encodeURIComponent(q)}&limit=20`);
    if (!data.success) { out.innerHTML = warn(data.error || 'Search failed'); return; }
    const r = data.results || {};
    const section = (title, rows) => rows ? `<div class="fw-bold small text-uppercase text-muted mt-2">${title}</div>${rows}` : '';
    const tracks = (r.tracks || []).map(t => mediaCard(t.artwork_url, t.title, t.artist, [
        playBtn('track', t.source_id, t.title), radioBtn('track', t.source_id, t.title),
        lyricsBtn(t.source_id, t.title), favBtn('track', t.source_id, false),
    ])).join('');
    const albums = (r.albums || []).map(a => mediaCard(a.artwork, a.name, a.artist, [
        playBtn('album', a.id, a.name), favBtn('album', a.id, false),
    ])).join('');
    const artists = (r.artists || []).map(a => mediaCard(a.artwork, a.name, 'Artist', [
        playBtn('artist', a.id, a.name), radioBtn('artist', a.id, a.name), favBtn('artist', a.id, false),
    ])).join('');
    const playlists = (r.playlists || []).map(pl => mediaCard(pl.artwork, pl.name, pl.artist, [
        playBtn('playlist', pl.id, pl.name), favBtn('playlist', pl.id, false),
    ])).join('');
    out.innerHTML = (tracks || albums || artists || playlists)
        ? section('Tracks', tracks) + section('Artists', artists) + section('Albums', albums) + section('Playlists', playlists)
        : '<div class="text-muted small py-2">No results.</div>';
}

async function loadTidalLibrary(kind) {
    const out = document.getElementById('tidalTabContent');
    if (!out) return;
    if (_tidalState !== 'logged_in') { await refreshTidalNotice(); if (_tidalState !== 'logged_in') { out.innerHTML = ''; return; } }
    out.innerHTML = spinner();
    const data = await apiGet(`/api/media/tidal/library?kind=${kind}`);
    if (!data.success) { out.innerHTML = warn(data.error || 'Load failed'); return; }
    const items = data.items || [];
    if (!items.length) { out.innerHTML = `<div class="text-muted small py-2">No ${kind} in your library.</div>`; return; }
    out.innerHTML = items.map(it => {
        const k = it.type;  // album | artist | playlist | mix
        let actions;
        if (k === 'mix') {
            // Mixes are personalised, not favouritable — just play them.
            actions = [playBtn('mix', it.id, it.name)];
        } else if (k === 'artist') {
            actions = [playBtn('artist', it.id, it.name), radioBtn('artist', it.id, it.name),
                       favBtn('artist', it.id, true)];
        } else {
            actions = [playBtn(k, it.id, it.name), favBtn(k, it.id, true)];
        }
        return mediaCard(it.artwork, it.name, it.artist, actions);
    }).join('');
}

// A result row with album/artist artwork thumbnail + action buttons.
function mediaCard(art, title, subtitle, actions) {
    const img = art
        ? `<img src="${esc(art)}" width="40" height="40" class="rounded me-2 flex-shrink-0" style="object-fit:cover" loading="lazy">`
        : '<span class="rounded me-2 bg-secondary-subtle d-inline-flex align-items-center justify-content-center flex-shrink-0" style="width:40px;height:40px"><i class="fas fa-music text-muted"></i></span>';
    return `
      <div class="d-flex align-items-center border-bottom py-1">
        ${img}
        <div class="text-truncate me-2 flex-grow-1">
          <div class="small fw-semibold text-truncate">${esc(title)}</div>
          <div class="text-muted text-truncate" style="font-size:.72rem">${esc(subtitle || '')}</div>
        </div>
        <div class="btn-group btn-group-sm flex-shrink-0">${actions.join('')}</div>
      </div>`;
}

function playBtn(kind, id, name) {
    return `<button class="btn btn-outline-success" title="Play"
      onclick="window.mediaTidalPlay('${kind}','${esc(id)}','play', ${JSON.stringify(name).replace(/"/g, '&quot;')})">
      <i class="fas fa-play"></i></button>`;
}
function radioBtn(kind, id, name) {
    return `<button class="btn btn-outline-primary" title="Radio (infinite)"
      onclick="window.mediaTidalPlay('${kind}','${esc(id)}','radio', ${JSON.stringify(name).replace(/"/g, '&quot;')})">
      <i class="fas fa-infinity"></i></button>`;
}
// Heart toggle. `faved` seeds the visual state (library items are favourites
// already; search results default to un-favourited). State flips optimistically
// from the server response, tracked on the button's data-faved attribute.
function favBtn(kind, id, faved) {
    return `<button class="btn ${faved ? 'btn-danger' : 'btn-outline-danger'}"
      title="${faved ? 'Remove from' : 'Add to'} favourites" data-faved="${faved ? '1' : '0'}"
      onclick="window.mediaTidalFav('${kind}','${esc(id)}', this)">
      <i class="fas fa-heart"></i></button>`;
}
function lyricsBtn(trackId, title) {
    return `<button class="btn btn-outline-secondary" title="Lyrics"
      onclick="window.mediaTidalLyrics('${esc(trackId)}', ${JSON.stringify(title).replace(/"/g, '&quot;')})">
      <i class="fas fa-align-left"></i></button>`;
}

async function tidalFav(kind, id, el) {
    const action = el.getAttribute('data-faved') === '1' ? 'remove' : 'add';
    const r = await apiPost('/api/media/tidal/favorite', { kind, id, action });
    if (!r.success) { toast(r.error || 'Favourite failed', 'error'); return; }
    const on = !!r.favorited;
    el.setAttribute('data-faved', on ? '1' : '0');
    el.classList.toggle('btn-danger', on);
    el.classList.toggle('btn-outline-danger', !on);
    el.title = (on ? 'Remove from' : 'Add to') + ' favourites';
    toast(on ? 'Added to favourites' : 'Removed from favourites', 'success');
}

async function tidalLyrics(trackId, title) {
    showOverlay(title || 'Lyrics', spinner());
    const data = await apiGet(`/api/media/tidal/lyrics?track_id=${encodeURIComponent(trackId)}`);
    if (!data.success) {
        showOverlay(title || 'Lyrics', `<div class="text-muted small">${esc(data.error || 'No lyrics')}</div>`);
        return;
    }
    const lyr = data.lyrics || {};
    const plain = lyr.text || lrcToPlain(lyr.synced);
    showOverlay(title || 'Lyrics',
        `<pre class="mb-0 small" style="white-space:pre-wrap;font-family:inherit">${esc(plain || 'No lyrics')}</pre>`);
}

// Strip [mm:ss.xx] timestamps from LRC-style synced lyrics for plain display.
function lrcToPlain(lrc) {
    return String(lrc || '').replace(/\[\d{1,2}:\d{2}(\.\d{1,3})?\]/g, '').trim();
}

// Lightweight centred modal overlay (used for lyrics). Click the backdrop to close.
function showOverlay(title, innerHtml) {
    let ov = document.getElementById('mediaOverlay');
    if (!ov) {
        ov = document.createElement('div');
        ov.id = 'mediaOverlay';
        ov.className = 'position-fixed top-0 start-0 w-100 h-100 d-flex align-items-center justify-content-center';
        ov.style.cssText = 'background:rgba(0,0,0,.5);z-index:1080';
        ov.addEventListener('click', e => { if (e.target === ov) ov.remove(); });
        document.body.appendChild(ov);
    }
    ov.innerHTML = `
      <div class="card shadow" style="max-width:520px;width:90%;max-height:80vh">
        <div class="card-header d-flex justify-content-between align-items-center py-2">
          <span class="fw-semibold text-truncate me-2">${esc(title)}</span>
          <button class="btn btn-sm btn-outline-secondary"
                  onclick="document.getElementById('mediaOverlay').remove()">
            <i class="fas fa-times"></i></button>
        </div>
        <div class="card-body overflow-auto">${innerHtml}</div>
      </div>`;
}

// Full-screen synced lyrics ("now playing" karaoke screen) — free, in-app.
// Reuses the LRC engine from the Cast receiver, driven by the player's reported
// position (interpolated between polls) + the Tidal lyrics API. No Cast needed.
let _lyr = null;
// Manual sync offset (ms) — devices report a playback position that lags the
// audio actually coming out of the speaker (output buffer + reporting delay),
// so we lead the lyrics by this much. Positive = lyrics earlier. Calibratable
// per-setup and persisted; also combined with any [offset:] tag in the LRC.
let _lyrOffsetMs = (() => {
    const v = parseInt(localStorage.getItem('mediaLyricsOffsetMs'), 10);
    return Number.isFinite(v) ? v : 400;
})();
let _lyrLrcOffsetMs = 0;   // from an [offset:NNN] tag in the current LRC

function _lyrSetOffset(ms) {
    _lyrOffsetMs = Math.max(-5000, Math.min(5000, ms | 0));
    localStorage.setItem('mediaLyricsOffsetMs', String(_lyrOffsetMs));
    const lbl = document.getElementById('lyrOffLbl');
    if (lbl) lbl.textContent = (_lyrOffsetMs >= 0 ? '+' : '') + (_lyrOffsetMs / 1000).toFixed(1) + 's';
    if (_lyr) _lyr.activeIdx = -1;   // force re-highlight at the new offset
}
window.mediaLyricsNudge = d => _lyrSetOffset(_lyrOffsetMs + d);

function openLyricsScreen(pid) {
    closeLyricsScreen();
    _lyr = { pid, trackId: null, cues: [], haveSynced: false, activeIdx: -1,
             anchorMs: 0, anchorAt: performance.now(), playing: false, art: null, raf: 0, poll: 0 };
    buildLyricsScreenDOM();
    // Re-anchor from a FRESH device read every 1.5s (vs the 10s media poll) so
    // the lyrics stay locked to the playhead; rAF interpolates in between.
    _lyrFreshPoll();
    _lyr.poll = setInterval(_lyrFreshPoll, 1500);
    _lyr.raf = requestAnimationFrame(lyricsTick);
}

function closeLyricsScreen() {
    if (_lyr) { cancelAnimationFrame(_lyr.raf); clearInterval(_lyr.poll); _lyr = null; }
    document.getElementById('mediaLyricsScreen')?.remove();
    document.removeEventListener('keydown', _lyrKey);
}

async function _lyrFreshPoll() {
    if (!_lyr) return;
    const d = await apiGet('/api/media/position?player_id=' + encodeURIComponent(_lyr.pid));
    if (!_lyr) return;
    const p = (d && d.success && d.player) ? d.player : null;
    if (!p) { _lyr.playing = false; return; }
    _lyrTick_meta(p);                                   // art/title/artist + track change
    _lyr.anchorMs = p.position_ms || 0;
    _lyr.anchorAt = performance.now();
    _lyr.playing = (p.state === 'playing');
}

function _lyrKey(e) { if (e.key === 'Escape') closeLyricsScreen(); }

function buildLyricsScreenDOM() {
    document.getElementById('mediaLyricsScreen')?.remove();
    const el = document.createElement('div');
    el.id = 'mediaLyricsScreen';
    el.style.cssText = 'position:fixed;inset:0;z-index:1090;background:#000;color:#fff;overflow:hidden';
    el.innerHTML = `
      <style>
        #lyrLayout{position:absolute;inset:0;display:flex;align-items:center;gap:5vw;padding:6vh 6vw}
        #lyrArtCol{flex:0 0 32vh;display:flex;flex-direction:column;align-items:center;text-align:center;min-width:0}
        #lyrArt{width:32vh;height:32vh;border-radius:16px;object-fit:cover;box-shadow:0 20px 60px rgba(0,0,0,.6);background:#222}
        #lyrTitle{font-size:3vh;font-weight:700;margin-top:2.5vh}
        #lyrArtist{font-size:2vh;opacity:.75;margin-top:.5vh}
        #lyrCol{flex:1 1 auto;height:100%;min-height:0;position:relative;overflow:hidden;
                -webkit-mask-image:linear-gradient(180deg,transparent,#000 18%,#000 82%,transparent);
                mask-image:linear-gradient(180deg,transparent,#000 18%,#000 82%,transparent)}
        /* Phones / portrait: stack the lyrics BELOW the album art instead of beside it */
        @media (max-width: 767px), (orientation: portrait) {
          #lyrLayout{flex-direction:column;gap:2.5vh;padding:9vh 5vw 3vh;align-items:center}
          #lyrArtCol{flex:0 0 auto}
          #lyrArt{width:min(22vh,60vw);height:min(22vh,60vw);border-radius:12px}
          #lyrTitle{font-size:2.3vh;margin-top:1.2vh}
          #lyrArtist{font-size:1.7vh;margin-top:.3vh}
          #lyrCol{height:auto;width:100%;align-self:stretch;text-align:center}
          #lyrCol .lyrln{transform-origin:center center !important;font-size:2.6vh !important}
        }
      </style>
      <div id="lyrBg" style="position:absolute;inset:0;background-size:cover;background-position:center;filter:blur(60px) brightness(.35);transform:scale(1.2)"></div>
      <div style="position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,0,0,.25),rgba(0,0,0,.7))"></div>
      <button class="btn btn-outline-light btn-sm" style="position:absolute;top:1rem;right:1rem;z-index:2"
              onclick="window.mediaLyricsClose()" title="Close (Esc)"><i class="fas fa-times"></i></button>
      <div style="position:absolute;top:1rem;left:1rem;z-index:2;display:flex;align-items:center;gap:.4rem"
           title="Nudge lyric timing — devices report position behind the actual audio">
        <button class="btn btn-outline-light btn-sm" onclick="window.mediaLyricsNudge(-100)"><i class="fas fa-minus"></i></button>
        <span class="small" style="min-width:4.5rem;text-align:center"><i class="fas fa-stopwatch me-1"></i><span id="lyrOffLbl"></span></span>
        <button class="btn btn-outline-light btn-sm" onclick="window.mediaLyricsNudge(100)"><i class="fas fa-plus"></i></button>
      </div>
      <div id="lyrLayout">
        <div id="lyrArtCol">
          <img id="lyrArt" alt="">
          <div id="lyrTitle"></div>
          <div id="lyrArtist"></div>
        </div>
        <div id="lyrCol">
          <div id="lyrInner" style="position:absolute;left:0;right:0;top:50%;transition:transform .45s cubic-bezier(.22,.61,.36,1)">
            <div style="opacity:.5;font-size:2.4vh">Waiting for playback…</div>
          </div>
        </div>
      </div>`;
    document.body.appendChild(el);
    _lyrSetOffset(_lyrOffsetMs);      // populate the calibration label
    document.addEventListener('keydown', _lyrKey);
}

function _lrcCues(lrc) {
    const out = [], re = /\[(\d+):(\d+)(?:[.:](\d+))?\]/g;
    for (const raw of String(lrc || '').split('\n')) {
        const text = raw.replace(re, '').trim();
        re.lastIndex = 0; let m;
        while ((m = re.exec(raw)) !== null) {
            const frac = m[3] ? parseInt(m[3].padEnd(3, '0').slice(0, 3), 10) / 1000 : 0;
            out.push({ t: (+m[1]) * 60 + (+m[2]) + frac, text });
        }
    }
    return out.sort((a, b) => a.t - b.t);
}

async function _lyrTrackChange(p) {
    _lyr.trackId = p.now_playing_id;
    _lyr.cues = []; _lyr.haveSynced = false; _lyr.activeIdx = -1;
    const inner = document.getElementById('lyrInner');
    if (inner) { inner.style.transition = ''; inner.innerHTML = '<div style="opacity:.5;font-size:2.4vh">Loading lyrics…</div>'; }
    if (p.media_type !== 'tidal' || !p.now_playing_id) {
        if (inner) inner.innerHTML = '<div style="opacity:.5;font-size:2.4vh">No lyrics for this source</div>';
        return;
    }
    const data = await apiGet(`/api/media/tidal/lyrics?track_id=${encodeURIComponent(p.now_playing_id)}`);
    if (!_lyr || _lyr.trackId !== p.now_playing_id) return;   // track moved on while fetching
    const lyr = (data && data.success && data.lyrics) ? data.lyrics : null;
    if (lyr && lyr.synced) {
        const om = /\[offset:\s*([+-]?\d+)\s*\]/i.exec(lyr.synced);
        _lyrLrcOffsetMs = om ? parseInt(om[1], 10) : 0;
        _lyr.cues = _lrcCues(lyr.synced); _lyr.haveSynced = _lyr.cues.length > 0;
        if (inner) inner.innerHTML = _lyr.cues.map((c, i) =>
            `<div class="lyrln" data-i="${i}" style="font-size:3.2vh;line-height:1.5;font-weight:600;opacity:.32;padding:1vh 0;transition:opacity .3s,transform .3s;transform-origin:left center">${esc(c.text || '♪')}</div>`).join('');
    } else if (lyr && lyr.text) {
        if (inner) { inner.style.transition = 'none'; inner.style.transform = 'translateY(-50%)';
            inner.innerHTML = `<div style="font-size:2.8vh;line-height:1.6;white-space:pre-wrap;opacity:.9">${esc(lyr.text)}</div>`; }
    } else if (inner) {
        inner.innerHTML = '<div style="opacity:.5;font-size:2.4vh">No lyrics for this track</div>';
    }
}

function _lyrTick_meta(p) {
    if (p.now_playing_id !== _lyr.trackId) _lyrTrackChange(p);
    if (p.artwork_url && _lyr.art !== p.artwork_url) {
        _lyr.art = p.artwork_url;
        const a = document.getElementById('lyrArt'); if (a) a.src = p.artwork_url;
        const bg = document.getElementById('lyrBg'); if (bg) bg.style.backgroundImage = `url("${p.artwork_url}")`;
    }
    const t = document.getElementById('lyrTitle'); if (t) t.textContent = p.title || '';
    const ar = document.getElementById('lyrArtist'); if (ar) ar.textContent = p.artist || '';
}

function lyricsTick() {
    if (!_lyr) return;
    if (_lyr.haveSynced) {
        // Interpolate from the last fresh anchor (re-anchored every 1.5s by
        // _lyrFreshPoll); lead by the manual offset + any LRC [offset:] tag.
        const estMs = (_lyr.playing ? _lyr.anchorMs + (performance.now() - _lyr.anchorAt) : _lyr.anchorMs)
            + _lyrOffsetMs + _lyrLrcOffsetMs;
        _lyrHighlight(estMs / 1000);
    }
    _lyr.raf = requestAnimationFrame(lyricsTick);
}

function _lyrHighlight(tSec) {
    const inner = document.getElementById('lyrInner'); if (!inner) return;
    let idx = -1; const cues = _lyr.cues;
    for (let i = 0; i < cues.length; i++) { if (cues[i].t <= tSec + 0.15) idx = i; else break; }
    if (idx === _lyr.activeIdx) return;
    inner.querySelectorAll('.lyrln').forEach((el, i) => {
        el.style.opacity = i === idx ? '1' : (i < idx ? '.3' : '.32');
        el.style.transform = i === idx ? 'scale(1.04)' : 'scale(1)';
    });
    const active = inner.querySelector(`.lyrln[data-i="${idx}"]`);
    if (active) inner.style.transform = `translateY(${-active.offsetTop}px)`;
    _lyr.activeIdx = idx;
}

// Artist context panel on a player card: artist radio + the artist's other
// albums, resolved from the now-playing track id. Toggles open/closed.
async function artistPanel(playerId, trackId) {
    const box = document.getElementById('artist-' + playerId);
    if (!box) return;
    if (box.innerHTML.trim()) { box.innerHTML = ''; return; }   // collapse
    box.innerHTML = spinner();
    const d = await apiGet(`/api/media/tidal/track/${encodeURIComponent(trackId)}/context`);
    if (!d || !d.success) { box.innerHTML = `<div class="small text-muted">${esc((d && d.error) || 'No artist info')}</div>`; return; }
    const a = d.artist || {}, albums = d.albums || [];
    const nm = s => JSON.stringify(s || '').replace(/"/g, '&quot;');
    const cards = albums.slice(0, 30).map(al => `
        <div class="text-center" style="width:88px;flex:0 0 auto">
          <img src="${esc(al.artwork || '')}" alt="" title="Play ${esc(al.name)}"
               style="width:80px;height:80px;border-radius:6px;object-fit:cover;cursor:pointer;background:#e9ecef"
               onclick="window.mediaPlayTidalOn('${esc(playerId)}','album','${esc(al.id)}','play',${nm(al.name)})">
          <div class="text-truncate small" style="width:80px" title="${esc(al.name)}">${esc(al.name)}</div>
        </div>`).join('');
    box.innerHTML = `
      <div class="border-top pt-2">
        <div class="d-flex align-items-center justify-content-between mb-2">
          <span class="small fw-semibold text-truncate me-2"><i class="fas fa-user me-1"></i>${esc(a.name || 'Artist')}</span>
          <button class="btn btn-sm btn-outline-primary flex-shrink-0"
                  onclick="window.mediaPlayTidalOn('${esc(playerId)}','artist','${esc(a.id)}','radio',${nm(a.name)})">
            <i class="fas fa-broadcast-tower me-1"></i>Artist radio
          </button>
        </div>
        ${albums.length
            ? `<div class="small text-muted mb-1">More from this artist</div>
               <div class="d-flex gap-2 overflow-auto pb-1">${cards}</div>`
            : '<div class="small text-muted">No other albums found.</div>'}
      </div>`;
}

// Play a Tidal item on a SPECIFIC player (the card it was launched from),
// without disturbing the current selection visually until playback refreshes.
async function playTidalOn(playerId, kind, id, mode, name) {
    _selectedId = playerId;
    await tidalPlay(kind, id, mode, name);
}

async function tidalPlay(kind, id, mode, name) {
    if (!requireSelected()) return;
    if (_selectedId === LOCAL_ID) return playLocal({ kind, id, mode }, name);
    const r = await apiPost('/api/media/tidal/play', { player_id: _selectedId, kind, id, mode });
    if (!r.success) { toast(r.error || 'Tidal play failed', 'error'); return; }
    const tag = r.radio ? ' radio ∞' : (r.count > 1 ? ` (${r.count} tracks)` : '');
    toast(`Playing ${name}${tag}`, 'success');
    setTimeout(loadPlayers, 1500);
    setTimeout(loadRecent, 2000);
}

// Recently played (quick replay)
async function loadRecent() {
    const el = document.getElementById('mediaRecent');
    if (!el) return;
    const data = await apiGet('/api/media/recent');
    const items = (data.success && data.items) ? data.items : [];
    _recentCache = items;
    if (!items.length) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="fw-bold small text-uppercase text-muted mb-1 mt-2">
        <i class="fas fa-clock-rotate-left me-1"></i>Recently played</div>`
      + items.slice(0, 8).map((it, i) => {
        const art = it.artwork_url
            ? `<img src="${esc(it.artwork_url)}" width="32" height="32" class="rounded me-2 flex-shrink-0" style="object-fit:cover" loading="lazy">`
            : '<span class="rounded me-2 bg-secondary-subtle d-inline-flex align-items-center justify-content-center flex-shrink-0" style="width:32px;height:32px"><i class="fas fa-music text-muted"></i></span>';
        return `<div class="d-flex align-items-center border-bottom py-1">
            ${art}
            <div class="text-truncate me-2 flex-grow-1">
              <div class="small text-truncate">${esc(it.title)}</div>
              <div class="text-muted text-truncate" style="font-size:.7rem">${esc(it.artist || '')}</div>
            </div>
            <button class="btn btn-sm btn-outline-success" title="Play again" onclick="window.mediaReplay(${i})">
              <i class="fas fa-rotate-right"></i></button>
          </div>`;
      }).join('');
}

async function replayRecent(i) {
    if (!requireSelected()) return;
    const it = _recentCache[i];
    if (!it) return;
    if (it.media_type === 'tidal' && it.source_id)
        return tidalPlay('track', it.source_id, 'play', it.title);
    // Radio / generic URL — replay straight from the stored URL.
    const r = await apiPost('/api/media/play', { player_id: _selectedId, url: it.url, title: it.title, artist: it.artist });
    if (!r.success) toast(r.error || 'Replay failed', 'error');
    else { toast(`Playing ${it.title}`, 'success'); setTimeout(loadPlayers, 1500); }
}

function spinner() { return '<div class="text-muted small py-2"><i class="fas fa-spinner fa-spin"></i> Loading…</div>'; }
function warn(m) { return `<div class="alert alert-warning mb-0">${esc(m)}</div>`; }

// Group builder — two sub-tabs: WiiM native multiroom | speaker-sync groups
function toggleGroupBuilder() {
    _groupBuilderOpen = !_groupBuilderOpen;
    if (_groupBuilderOpen) renderGroupBuilder();
    else { _stopSyncPoll(); renderPlayers(); }
}

function switchGroupTab(tab) {
    _groupTab = tab;
    renderGroupBuilder();
}

function renderGroupBuilder() {
    const el = document.getElementById('mediaPlayers');
    if (!el) return;
    // Idempotent: player refreshes re-enter here constantly while the
    // builder is open. Rebuilding the scaffold each time destroyed
    // #mediaGroupPane — and the live Sync Lab inside it — mid-test (the
    // recurring "charts drop out" bug). Build once; after that only the
    // pill states are touched, and the sync pane (which keeps its own
    // state + poller) is left alone unless the tab actually changed.
    let pane = document.getElementById('mediaGroupPane');
    if (!pane) {
        el.innerHTML = `
          <ul class="nav nav-pills mb-2 flex-nowrap zmm-group-tabs">
            <li class="nav-item">
              <button class="nav-link py-1 px-3 text-nowrap" id="mediaGroupTabWiim"
                      onclick="window.mediaGroupTab('wiim')">
                <i class="fas fa-volume-up me-1"></i>WiiM<span class="d-none d-sm-inline"> multiroom</span></button>
            </li>
            <li class="nav-item">
              <button class="nav-link py-1 px-3 text-nowrap" id="mediaGroupTabSync"
                      onclick="window.mediaGroupTab('sync')">
                <i class="zmm-openzone-icon me-1"></i>OpenZone <span class="badge bg-warning text-dark ms-1">beta</span></button>
            </li>
            <li class="nav-item ms-auto">
              <button class="nav-link py-1 px-3 text-nowrap" onclick="window.mediaOpenGroupBuilder()"
                      aria-label="Close the group builder">
                <i class="fas fa-xmark"></i><span class="d-none d-sm-inline ms-1">Close</span></button>
            </li>
          </ul>
          <div id="mediaGroupPane"></div>`;
        pane = document.getElementById('mediaGroupPane');
    }
    document.getElementById('mediaGroupTabWiim')
        ?.classList.toggle('active', _groupTab === 'wiim');
    document.getElementById('mediaGroupTabSync')
        ?.classList.toggle('active', _groupTab === 'sync');
    if (_groupTab === 'wiim') {
        _stopSyncPoll();
        renderWiimBuilder();          // cheap + depends on _players
    } else if (pane.dataset.tab !== 'sync') {
        renderSyncPane();             // first show / tab switch only
    }
}

function renderWiimBuilder() {
    const el = document.getElementById('mediaGroupPane');
    if (!el) return;
    el.dataset.tab = 'wiim';
    const wiim = _players.filter(p => p.provider === 'wiim' && p.available && !p.is_group);
    if (wiim.length < 2) {
        el.innerHTML = `<div class="alert alert-info mb-0">
            Native grouping here needs at least two available WiiM players.
            <div class="small mt-1">Google Cast speakers: use the <em>OpenZone</em> tab
            (no Google Home needed), or a Google-Home group (appears automatically).</div></div>`;
        return;
    }
    el.innerHTML = `
      <div class="mb-2 fw-semibold"><i class="far fa-object-group me-1"></i> Build a WiiM group</div>
      <p class="small text-muted">Pick a master (plays the source) and the members to sync to it.</p>
      <div class="mb-2">
        <label class="form-label small">Master</label>
        <select class="form-select form-select-sm" id="mediaGroupMaster">
          ${wiim.map(p => `<option value="${esc(p.player_id)}">${esc(p.name)}</option>`).join('')}
        </select>
      </div>
      <label class="form-label small">Members</label>
      ${wiim.map(p => `
        <div class="form-check">
          <input class="form-check-input media-group-member" type="checkbox" value="${esc(p.player_id)}" id="gm_${esc(p.player_id)}">
          <label class="form-check-label small" for="gm_${esc(p.player_id)}">${esc(p.name)}</label>
        </div>`).join('')}
      <div class="mt-3 d-flex gap-2">
        <button class="btn btn-sm btn-primary" onclick="window.mediaSubmitGroup()">Create group</button>
      </div>`;
}

// Speaker-sync groups (Cast multi-speaker sync without Google Home)
function _stopSyncPoll() {
    if (_syncTimer) { clearInterval(_syncTimer); _syncTimer = null; }
}

async function _syncFetch() {
    const [g, s] = await Promise.all([
        fetch('/api/media/sync/groups').then(r => r.json()),
        fetch('/api/media/sync/status').then(r => r.json()),
    ]);
    _syncGroups = g.groups || [];
    _syncStatus = s;
    _zonePlayMigrate();
}

function _syncDeviceInfo(pid) {
    return ((_syncStatus || {}).devices || []).find(d => d.player_id === pid);
}

function _syncStatLine(st) {
    const s = st && st.stats;
    if (!s || s.offset_ms == null) return '';
    const rtt = Number.isFinite(s.rtt_ms) ? ` · rtt ${s.rtt_ms} ms` : '';
    const drift = Number.isFinite(s.drift_ppm) ? ` · drift ${s.drift_ppm} ppm` : '';
    return `offset ${s.offset_ms} ms${rtt}${drift} · late ${s.late} · resyncs ${s.resyncs}`;
}

// Deviation meter: a centered bar on the same ±500 ms scale as the trim
// slider below it. Fill grows from the zero line — right = plays late,
// left = plays early — coloured by error magnitude (in sync / drifting /
// off). The numeric offset stays in the stat line, so colour is never the
// only signal.
const _SYNC_METER_FS = 500;   // full-scale ms, matches the trim slider

function _syncMeter(pid) {
    return `
      <div class="position-relative mt-1" style="height:10px;background:var(--bs-secondary-bg);border-radius:5px;"
           title="Playback offset vs the group target — aim for the centre">
        <div style="position:absolute;left:50%;top:-2px;bottom:-2px;width:2px;background:var(--bs-border-color);"></div>
        <div id="syncfill-${esc(pid)}"
             style="position:absolute;top:2px;height:6px;border-radius:3px;width:0;left:50%;"></div>
      </div>
      <div class="d-flex justify-content-between text-muted" style="font-size:.65rem;">
        <span>plays early −${_SYNC_METER_FS} ms</span><span>0</span><span>+${_SYNC_METER_FS} ms plays late</span>
      </div>`;
}

function _syncMeterPaint(pid, st) {
    const fill = document.getElementById('syncfill-' + pid);
    if (!fill) return;
    const o = st && st.stats ? st.stats.offset_ms : null;
    if (o == null || !Number.isFinite(o)) { fill.style.width = '0'; return; }
    const c = Math.max(-_SYNC_METER_FS, Math.min(_SYNC_METER_FS, o));
    const pct = Math.abs(c) / _SYNC_METER_FS * 50;
    fill.style.width = Math.max(pct, 0.8) + '%';   // keep a sliver visible at ~0
    fill.style.left = (o >= 0 ? 50 : 50 - pct) + '%';
    fill.style.background = Math.abs(o) <= 60 ? 'var(--bs-success)'
        : Math.abs(o) <= 150 ? 'var(--bs-warning)' : 'var(--bs-danger)';
}

function _syncMemberRow(m, groupActive) {
    const pidE = esc(m.player_id);
    const st = groupActive ? _syncDeviceInfo(m.player_id) : null;
    const pill = !groupActive ? ''
        : (st && st.connected
            ? '<span class="badge bg-success ms-1">connected</span>'
            : '<span class="badge bg-secondary ms-1">launching…</span>');
    const open = _syncTrimOpen.has(m.player_id);
    return `
      <div class="border-top pt-2 mt-2">
        <div class="d-flex align-items-center small">
          <span class="fw-semibold text-truncate">${esc(m.name)}</span>
          <span id="syncpill-${pidE}" class="flex-shrink-0">${pill}</span>
          <span class="ms-auto d-flex align-items-center gap-1 flex-shrink-0">
            <button class="btn btn-outline-secondary btn-sm py-0 px-1 zmm-trim-step" title="1 ms earlier"
                    onclick="window.mediaSyncNudge('${pidE}', -1)">−</button>
            <span class="text-muted" id="synctrimlbl-${pidE}">${m.trim_ms} ms</span>
            <button class="btn btn-outline-secondary btn-sm py-0 px-1 zmm-trim-step" title="1 ms later"
                    onclick="window.mediaSyncNudge('${pidE}', 1)">+</button>
            <button class="btn btn-outline-secondary btn-sm py-0 px-1 zmm-trim-step d-lg-none"
                    id="synctrimbtn-${pidE}" data-name="${esc(m.name)}"
                    aria-expanded="${open}" aria-controls="synctrimbox-${pidE}"
                    title="${open ? 'Hide' : 'Show'} trim slider"
                    aria-label="${open ? 'Hide' : 'Show'} trim slider for ${esc(m.name)}"
                    onclick="window.mediaSyncTrimToggle('${pidE}')">
              <i class="fas ${open ? 'fa-chevron-up' : 'fa-chevron-down'}"></i>
            </button>
          </span>
        </div>
        ${groupActive ? _syncMeter(m.player_id) : ''}
        <div class="zmm-sync-trim${open ? ' zmm-trim-open' : ''}"
             id="synctrimbox-${pidE}" data-pid="${pidE}">
          <input type="range" class="form-range" min="-500" max="500" step="1"
                 value="${m.trim_ms}" id="synctrim-${pidE}"
                 oninput="document.getElementById('synctrimlbl-${pidE}').textContent = this.value + ' ms'"
                 onchange="window.mediaSyncTrim('${pidE}', this.value)">
        </div>
        <div class="small text-muted" id="syncstat-${pidE}">${_syncStatLine(st)}</div>
      </div>`;
}

// Below lg a member row's trim slider is folded away until asked for. It is
// the one full-width horizontal control on the row, so left open on every
// member it lines the whole scroll path and a swipe that starts on one
// re-times a speaker by tens of ms instead of scrolling — pan-y hands back a
// clean vertical gesture, not a diagonal one. One row open at a time keeps
// the sliders that aren't being aimed at out of reach; the ±1 ms buttons and
// the readout stay visible, so a deliberate nudge never needs the fold. The
// class is a no-op above lg, so desktop rows are untouched.
//
// Repaints in place rather than through renderSyncPane(): that refetches, and
// would tear down a live Sync Lab and any trim drag in flight.
function syncTrimToggle(pid) {
    const open = !_syncTrimOpen.has(pid);
    _syncTrimOpen.clear();
    if (open) _syncTrimOpen.add(pid);
    for (const box of document.querySelectorAll('#mediaGroupPane .zmm-sync-trim')) {
        const on = _syncTrimOpen.has(box.dataset.pid);
        box.classList.toggle('zmm-trim-open', on);
        const btn = document.getElementById('synctrimbtn-' + box.dataset.pid);
        if (!btn) continue;
        const label = `${on ? 'Hide' : 'Show'} trim slider`;
        btn.setAttribute('aria-expanded', String(on));
        btn.title = label;
        btn.setAttribute('aria-label', `${label} for ${btn.dataset.name || 'speaker'}`);
        btn.innerHTML = `<i class="fas ${on ? 'fa-chevron-up' : 'fa-chevron-down'}"></i>`;
    }
}

/** Show one OpenZone sub-tab. Both panes stay in the DOM — the hidden one
 *  is still being painted by the stats poller, and a live Sync Lab must not
 *  be torn down and rebuilt just because you looked at the controls. */
function _applySyncSubTab() {
    const zones = document.getElementById('syncZonesPane');
    const results = document.getElementById('syncResultsPane');
    if (!zones || !results) return;
    zones.classList.toggle('d-none', _syncSub !== 'zones');
    results.classList.toggle('d-none', _syncSub !== 'results');
    document.getElementById('syncSubTabZones')
        ?.classList.toggle('active', _syncSub === 'zones');
    document.getElementById('syncSubTabResults')
        ?.classList.toggle('active', _syncSub === 'results');
    if (_syncSub === 'results') _syncResultsPick();
}

/** On the Results tab: open the obvious group's lab, or offer the choice.
 *  "Obvious" is the one that is playing, or the only one there is. */
function _syncResultsPick() {
    const picker = document.getElementById('syncLabPicker');
    if (!picker) return;
    const open = syncLabGroup();
    if (!open) {
        const active = _syncGroups.find(g => g.active);
        const only = _syncGroups.length === 1 ? _syncGroups[0] : null;
        const auto = active || only;
        if (auto) { window.mediaSyncLab(auto.id); return; }
    }
    if (!_syncGroups.length) {
        picker.innerHTML = '<div class="text-muted small">No sync groups yet — '
            + 'create one on the Zones tab.</div>';
        return;
    }
    picker.innerHTML = `
      <div class="d-flex align-items-center gap-2 flex-wrap mb-2">
        <span class="small text-muted">Session results for</span>
        ${_syncGroups.map(g => `
          <button class="btn btn-sm ${g.id === open ? 'btn-primary' : 'btn-outline-secondary'}"
                  onclick="window.mediaSyncLab('${esc(g.id)}')">
            ${esc(g.name)}${g.active ? ' <span class="badge bg-success ms-1">live</span>' : ''}
          </button>`).join('')}
      </div>`;
}

async function renderSyncPane() {
    const el = document.getElementById('mediaGroupPane');
    if (!el) return;
    log.debug(`sync pane rebuild (caller: ${new Error().stack?.split('\n')[2]?.trim() || '?'})`);
    el.dataset.tab = 'sync';
    // Fetch BEFORE touching the DOM: the old content (including a live
    // Sync Lab) stays attached and visible for the whole round-trip, so
    // charts don't blink out while the pane refreshes.
    const hadContent = el.childElementCount > 0;
    if (!hadContent) el.innerHTML = '<div class="text-muted small">Loading…</div>';
    try { await Promise.all([_syncFetch(), _ensureTidalState()]); }
    catch (e) { el.innerHTML = warn('Could not load sync groups: ' + e.message); return; }
    // Keep a live Sync Lab (charts, zoom, scroll) across pane rebuilds —
    // rebuilding the lab from scratch every time is exactly the "charts
    // keep resetting" bug. The module-level fallback still finds the node
    // when a parent rebuild detached it (getElementById can't).
    const savedLab = document.getElementById('syncLabHost') || _syncLabKeep;
    const keepLab = savedLab && savedLab.childElementCount > 0;

    const disabled = !_syncStatus || !!_syncStatus.error;
    const unregistered = !disabled && !_syncStatus.configured;
    const casts = _players.filter(p => p.provider === 'cast' && !p.is_group);
    const running = !disabled && _syncStatus.running;

    const banner = disabled
        ? `<div class="alert alert-info small py-2">OpenZone is disabled — enable it under
             <strong>Settings → Audio</strong>.</div>`
        : unregistered
            ? `<div class="alert alert-info small py-2">Running in <strong>built-in receiver
                 mode</strong> — no Cast registration needed. Sync is auto-corrected to within
                 a few tens of ms; use the trim sliders for the final by-ear alignment.
                 (Registering a custom receiver under <strong>Settings → Audio</strong>
                 upgrades this to sample-accurate sync.)</div>`
            : '';

    const groupCards = _syncGroups.map(g => `
      <div class="card mb-2">
        <div class="card-body py-2">
          <div class="d-flex align-items-center gap-2 flex-wrap zmm-zone-head">
            <div class="d-flex align-items-center gap-2 zmm-zone-name" style="min-width:0">
              <span class="fw-semibold text-truncate">${esc(g.name)}</span>
              <span class="badge bg-light text-muted border flex-shrink-0">${g.members.length} speakers</span>
            </div>
            <div class="d-flex align-items-center gap-2 flex-wrap ms-auto zmm-zone-actions">
            ${g.active
                ? `<button class="btn btn-sm btn-outline-primary" id="syncCalBtn"
                           onclick="window.mediaSyncCalibrate()"
                           title="Play a chirp on each speaker and measure the real in-air offsets with the server microphone — sets the trims automatically (takes ~15 s)">
                     <i class="fas fa-microphone me-1"></i>Calibrate</button>
                   <button class="btn btn-sm btn-danger" onclick="window.mediaSyncStop()">
                     <i class="fas fa-stop me-1"></i>${(((_syncStatus || {}).source || {}).kind === 'media') ? 'Stop' : 'Stop test'}
                     <span id="syncRemain" class="ms-1"></span></button>`
                : `${_syncSrcSelect(g.id, running || disabled)}
                   <select class="form-select form-select-sm w-auto" id="syncdur-${esc(g.id)}"
                           ${running || disabled ? 'disabled' : ''}
                           title="Session length — fixed windows keep sessions comparable in the Sync Lab"
                           onchange="window.mediaSyncDur('${esc(g.id)}', this.value)">
                     ${[[120, '2 min'], [300, '5 min'], [600, '10 min'], [0, 'Until stopped']]
                         .map(([v, l]) => `<option value="${v}" ${v === _syncDurFor(g.id) ? 'selected' : ''}>${l}</option>`)
                         .join('')}
                   </select>
                   <button class="btn btn-sm btn-outline-primary zmm-zone-play"
                           id="syncstart-${esc(g.id)}"
                           ${running || disabled || _syncNeedsUrl(g.id) ? 'disabled' : ''}
                           onclick="window.mediaSyncStart('${esc(g.id)}')"
                           title="${_syncNeedsUrl(g.id)
                               ? 'Enter a URL below first'
                               : _syncSrcFor(g.id)
                                   ? esc('Play ' + _syncSrcLabel(g.id) + ' on all members, clock-aligned')
                                   : 'Play the sync test signal (clicks every 2 s) on all members'}">
                     <i class="fas fa-play me-1"></i>${_syncSrcFor(g.id) ? 'Play' : 'Test'}</button>`}
            <button class="btn btn-sm btn-outline-secondary" onclick="window.mediaSyncLab('${esc(g.id)}')"
                    title="Sync Lab — session analysis &amp; learned model"><i class="fas fa-wave-square"></i></button>
            <button class="btn btn-sm btn-outline-danger" onclick="window.mediaSyncDelete('${esc(g.id)}')"
                    title="Delete group"><i class="far fa-trash-alt"></i></button>
            </div>
          </div>
          ${g.active ? _syncZoneScope(g.id) + _syncActiveHint()
                     : _syncCustomRow(g.id, running || disabled)
                       + _syncTidalRow(g.id, running || disabled)
                       + _syncXfadeRow(g.id, running || disabled)}
          ${g.members.map(m => _syncMemberRow(m, g.active)).join('')}
        </div>
      </div>`).join('');

    el.innerHTML = `
      <ul class="nav nav-tabs mb-2 flex-nowrap" role="tablist">
        <li class="nav-item"><button class="nav-link py-1 px-3" id="syncSubTabZones"
              onclick="window.mediaSyncSubTab('zones')" role="tab">
            <i class="zmm-openzone-icon me-1"></i>Zones</button></li>
        <li class="nav-item"><button class="nav-link py-1 px-3" id="syncSubTabResults"
              onclick="window.mediaSyncSubTab('results')" role="tab">
            <i class="fas fa-wave-square me-1"></i>Results</button></li>
      </ul>
      <div id="syncZonesPane">
      ${banner}
      <p class="small text-muted mb-2">Sync groups play the same audio on several Cast speakers,
        clock-aligned by ZigBee Manager — no Google-Home group required. Per-speaker trim (±ms)
        is remembered per device.</p>
      ${groupCards || '<div class="text-muted small mb-2">No sync groups yet — create one below.</div>'}
      <div class="card">
        <div class="card-body py-2">
          <div class="fw-semibold small mb-2"><i class="fas fa-plus-circle me-1"></i> New sync group</div>
          ${casts.length < 2
              ? '<div class="text-muted small">Needs at least two discovered Cast speakers.</div>'
              : `
          <input type="text" class="form-control form-control-sm mb-2" id="syncGroupName"
                 placeholder="Group name, e.g. Downstairs">
          ${casts.map(p => `
            <div class="form-check">
              <input class="form-check-input sync-member" type="checkbox" value="${esc(p.player_id)}"
                     id="sm_${esc(p.player_id)}">
              <label class="form-check-label small" for="sm_${esc(p.player_id)}">${esc(p.name)}</label>
            </div>`).join('')}
          <button class="btn btn-sm btn-primary mt-2" onclick="window.mediaSyncCreate()">Create sync group</button>`}
        </div>
      </div>
      </div>
      <div id="syncResultsPane">
        <div id="syncLabPicker"></div>
        <div id="syncLabHost"></div>
      </div>`;

    // Poll while a session runs so pills + stats stay live (in place — a full
    // re-render would fight an in-progress trim drag).
    for (const d of ((_syncStatus || {}).devices || [])) _syncMeterPaint(d.player_id, d);
    if (keepLab) document.getElementById('syncLabHost').replaceWith(savedLab);
    else restoreSyncLab();
    _syncLabKeep = document.getElementById('syncLabHost');
    _applySyncSubTab();
    // A group already set to a Tidal container needs its list on first sight,
    // not only after the kind select is touched.
    for (const g of _syncGroups) {
        const t = _syncTidalPick(g.id);
        if (t) { _syncLoadTidalLib(t.kind); break; }
    }
    _mountZoneScopes();
    _stopSyncPoll();
    if (running) _syncTimer = setInterval(refreshSyncStats, 3000);
}

async function refreshSyncStats() {
    try {
        const prevRunning = !!(_syncStatus && _syncStatus.running);
        _syncStatus = await (await fetch('/api/media/sync/status')).json();
        if (!!_syncStatus.running !== prevRunning) { renderSyncPane(); return; }
        const remain = document.getElementById('syncRemain');
        if (remain) remain.textContent = _fmtRemain(_syncStatus.remaining_s);
        for (const d of (_syncStatus.devices || [])) {
            const pill = document.getElementById('syncpill-' + d.player_id);
            if (pill) pill.innerHTML = d.connected
                ? '<span class="badge bg-success ms-1">connected</span>'
                : '<span class="badge bg-secondary ms-1">launching…</span>';
            const stat = document.getElementById('syncstat-' + d.player_id);
            if (stat) stat.textContent = _syncStatLine(d);
            _syncMeterPaint(d.player_id, d);
        }
        // Refresh the source line in place (underruns tick up mid-session).
        // It sits above the member rows, so this can't disturb a trim drag.
        const hint = document.getElementById('syncActiveHint');
        if (hint) hint.outerHTML = _syncActiveHint();
    } catch (e) { /* transient — next tick */ }
}

async function syncCreateGroup() {
    const name = document.getElementById('syncGroupName')?.value?.trim();
    const members = Array.from(document.querySelectorAll('.sync-member:checked')).map(c => c.value);
    if (!name) { toast('Give the group a name', 'warning'); return; }
    if (members.length < 2) { toast('Pick at least two speakers', 'warning'); return; }
    const r = await apiPost('/api/media/sync/groups', { name, members });
    if (!r.success) { toast(r.error || 'Could not save group', 'error'); return; }
    toast('Sync group saved', 'success');
    renderSyncPane();
}

async function syncDeleteGroup(gid) {
    const g = _syncGroups.find(x => x.id === gid);
    const ok = await confirmDialog(`Delete sync group “${g ? g.name : gid}”?`);
    if (!ok) return;
    const r = await apiPost('/api/media/sync/groups/delete', { id: gid });
    if (!r.success) toast(r.error || 'Delete failed', 'error');
    renderSyncPane();
}

// What a zone plays
// Held on the server with the zone (cast_sync.group_config), not in this tab:
// a zone whose source only existed in one browser could not be started by an
// automation, a schedule, or a second phone. `key`, `custom_url` and `loop`
// are this picker's memory; `media` is the block the start path reads, derived
// from them on every change so the two cannot drift.
const ZONE_PLAY_DEFAULT = { key: '', custom_url: '', loop: false, media: null,
                            duration_s: null, crossfade_s: null };

function _zonePlay(gid) {
    const g = _syncGroups.find(x => x.id === gid);
    return Object.assign({}, ZONE_PLAY_DEFAULT, (g && g.play) || {});
}

/** Apply a change to the zone's playback config and persist it. The patch
 *  lands first so `_syncMediaFor` — which reads the picker through these same
 *  accessors — sees the new state when it rebuilds the media block. */
function _zonePlaySet(gid, patch, delay) {
    const g = _syncGroups.find(x => x.id === gid);
    if (!g) return;
    g.play = Object.assign(_zonePlay(gid), patch);
    g.play.media = _syncMediaFor(gid);
    _zonePlayPush(gid, delay);
}

// Debounced: a dragged crossfade slider and a typed URL both fire per event,
// and only the value the user stops on is worth a round trip.
const _zonePlayTimers = {};

function _zonePlayPush(gid, delay = 400) {
    clearTimeout(_zonePlayTimers[gid]);
    _zonePlayTimers[gid] = setTimeout(async () => {
        const r = await apiPost('/api/media/sync/groups/config',
                                Object.assign({ id: gid }, _zonePlay(gid)));
        if (!r.success) toast(r.error || 'Could not save what this zone plays', 'error');
    }, delay);
}

// Test-window length per zone. Fixed windows keep the learned model and the
// Sync Lab's session-by-session trends comparable — a 5-minute run and a
// 40-second run don't measure the same thing.
function _syncDurFor(gid) {
    const v = Number(_zonePlay(gid).duration_s);
    return Number.isFinite(v) && [0, 120, 300, 600].includes(v) ? v : 300;
}

function syncSetDuration(gid, val) {
    _zonePlaySet(gid, { duration_s: Number(val) || 0 }, 0);
}

// Sync source picker
// A stable key — "" (test signal), "fav:<uuid>", "tidal:<id>", "url:<url>",
// "tc:<kind>[:<id>]" or "custom" — never a list index, because favourites and
// recently-played both reorder underneath us. Split on the FIRST colon so a
// URL keeps its own. The custom URL and the loop flag are held apart from the
// key so switching away to a favourite and back doesn't lose what was typed.
function _syncSrcFor(gid) {
    return _zonePlay(gid).key;
}

function _syncCustomUrl(gid) {
    return (_zonePlay(gid).custom_url || '').trim();
}

function _syncLoopFor(gid) {
    return !!_zonePlay(gid).loop;
}

function syncSetSource(gid, val) {
    _zonePlaySet(gid, { key: val || '' }, 0);
    renderSyncPane();                     // the start button follows the choice
}

function syncSetCustomUrl(gid, val) {
    _zonePlaySet(gid, { custom_url: String(val || '').trim() }, 0);
    renderSyncPane();                     // enables/disables the start button
}

function syncSetLoop(gid, on) {
    _zonePlaySet(gid, { loop: !!on });
}

/** Carry a zone's source out of this browser and onto the server, once.
 *  Zones predating server-side config have their choice in localStorage;
 *  leaving it there would mean an automation could not play what the Media
 *  page shows the zone playing. */
function _zonePlayMigrate() {
    for (const g of _syncGroups) {
        const key = localStorage.getItem('zmm.syncsrc.' + g.id);
        const url = localStorage.getItem('zmm.syncurl.' + g.id);
        const dur = localStorage.getItem('zmm.syncdur.' + g.id);
        const xf = localStorage.getItem('zmm.syncxfade.' + g.id);
        const loop = localStorage.getItem('zmm.syncloop.' + g.id);
        const local = key !== null || url !== null || dur !== null
                      || xf !== null || loop !== null;
        // Only for a zone the server has nothing for — a config saved from
        // another browser is newer than whatever this tab remembers.
        if (!local || (g.play && g.play.key)) continue;
        g.play = Object.assign(_zonePlay(g.id), {
            key: key || '',
            custom_url: (url || '').trim(),
            loop: loop === '1',
            duration_s: dur === null ? null : Number(dur) || 0,
            crossfade_s: xf === null ? null : Number(xf) || 0,
        });
        g.play.media = _syncMediaFor(g.id);
        _zonePlayPush(g.id, 0);
        for (const k of ['syncsrc', 'syncurl', 'syncdur', 'syncxfade', 'syncloop'])
            localStorage.removeItem(`zmm.${k}.${g.id}`);
        log.info(`Migrated zone ${g.id} playback config to the server`);
    }
}

/** Load one slice of the library, once. The picker renders "Loading…" until
 *  it lands; `null` marks a fetch in flight so a re-render can't start a
 *  second one. */
async function _syncLoadTidalLib(kind) {
    if (_tidalLib[kind] !== undefined) return;
    _tidalLib[kind] = null;
    try {
        const d = await apiGet(`/api/media/tidal/library?kind=${encodeURIComponent(kind)}`);
        _tidalLib[kind] = d.success ? (d.items || []) : [];
        if (!d.success) toast(d.error || 'Tidal library unavailable', 'error');
    } catch (e) {
        _tidalLib[kind] = [];
        toast('Tidal library unavailable: ' + e.message, 'error');
    }
    // The stored media block carries the title and art a speaker shows while
    // the set plays; a zone whose choice was saved before this slice loaded
    // holds a placeholder. Re-derive it now that the row exists.
    for (const g of _syncGroups) {
        const t = _syncTidalPick(g.id);
        if (t && t.id && t.kind === kind && _syncTidalItem(g.id)) _zonePlaySet(g.id, {});
    }
    renderSyncPane();
}

/** Tidal status without needing the search tab to have been opened — the
 *  zone picker offers the library only when there is one to offer. */
async function _ensureTidalState() {
    if (_tidalState !== null) return;
    try {
        const d = await apiGet('/api/media/tidal/status');
        _tidalState = (d.success && d.status && d.status.state) || 'unavailable';
    } catch (e) {
        _tidalState = 'unavailable';
    }
}

/** Switch which slice of the Tidal library the zone picker is showing.
 *  Clears the chosen item: an album id means nothing under Artists. */
async function syncTidalKind(gid, kind) {
    _zonePlaySet(gid, { key: `tc:${kind}` }, 0);
    renderSyncPane();                     // shows "Loading…" against the list
    await _syncLoadTidalLib(kind);
}

function syncTidalItem(gid, id) {
    const t = _syncTidalPick(gid) || { kind: 'playlists' };
    _zonePlaySet(gid, { key: id ? `tc:${t.kind}:${id}` : `tc:${t.kind}` }, 0);
    renderSyncPane();                     // enables Play once something is set
}

function _syncSrcParts(gid) {
    const v = _syncSrcFor(gid);
    if (!v) return null;
    // "custom" carries no id of its own — the URL is edited separately and may
    // legitimately be empty while the user is still typing it.
    if (v === 'custom') return { kind: 'custom', id: _syncCustomUrl(gid) };
    const i = v.indexOf(':');
    return i < 0 ? null : { kind: v.slice(0, i), id: v.slice(i + 1) };
}

/** A Tidal container choice, stored as "tc:<kind>:<id>" — the id is empty
 *  while the pickers below are still being filled in. */
function _syncTidalPick(gid) {
    const p = _syncSrcParts(gid);
    if (!p || p.kind !== 'tc') return null;
    const i = p.id.indexOf(':');
    return i < 0 ? { kind: p.id || 'playlists', id: '' }
                 : { kind: p.id.slice(0, i), id: p.id.slice(i + 1) };
}

/** The chosen library row, if it has been loaded. */
function _syncTidalItem(gid) {
    const t = _syncTidalPick(gid);
    if (!t || !t.id) return null;
    return (_tidalLib[t.kind] || []).find(x => String(x.id) === t.id) || null;
}

// The `media` body for /sync/start, or null for the generated test signal
// (which the API expresses as an absent body).
function _syncMediaFor(gid) {
    const p = _syncSrcParts(gid);
    if (!p) return null;
    // Favourites travel by id, not URL: the server re-resolves them, so a
    // station that moved still starts.
    if (p.kind === 'fav') return { station_uuid: p.id };
    if (p.kind === 'tc') {
        const t = _syncTidalPick(gid);
        if (!t || !t.id) return null;     // nothing picked yet
        const row = _syncTidalItem(gid);
        return {
            source_id: t.id, media_type: 'tidal',
            // The library lists plurals; the API names one item.
            kind: { mixes: 'mix', playlists: 'playlist',
                    albums: 'album', artists: 'artist' }[t.kind] || 'playlist',
            title: (row && (row.title || row.name)) || 'Tidal',
            artist: (row && row.artist) || '',
            artwork_url: (row && (row.artwork || row.artwork_url)) || '',
            ...(_syncLoopFor(gid) ? { loop: true } : {}),
        };
    }
    if (!p.id) return null;               // custom, nothing entered yet
    if (p.kind === 'tidal') {
        const t = _recentCache.find(x => x.source_id === p.id);
        return { source_id: p.id, media_type: 'tidal', kind: 'track',
                 title: (t && t.title) || 'Tidal',
                 artwork_url: (t && t.artwork_url) || '',
                 ...(_syncLoopFor(gid) ? { loop: true } : {}) };
    }
    const it = _recentCache.find(x => x.url === p.id);
    const media = { url: p.id, title: (it && it.title) || _syncUrlName(p.id) };
    // Looping only makes sense for something finite, which in practice means a
    // custom file — a station never ends, and the server ignores it there.
    if (p.kind === 'custom' && _syncLoopFor(gid)) media.loop = true;
    return media;
}

// A readable name for a bare URL: last path segment, else the host.
function _syncUrlName(url) {
    try {
        const u = new URL(url, 'file:///');
        const last = (u.pathname || '').split('/').filter(Boolean).pop();
        return decodeURIComponent(last || u.hostname || url);
    } catch (e) { return url; }
}

// A source picked but not yet specified — there is nothing to start, and
// silently falling back to the test signal would be a lie.
function _syncNeedsUrl(gid) {
    if (_syncSrcFor(gid) === 'custom' && !_syncCustomUrl(gid)) return true;
    const t = _syncTidalPick(gid);
    return !!t && !t.id;
}

function _syncSrcLabel(gid) {
    const p = _syncSrcParts(gid);
    if (!p) return 'Sync test';
    if (p.kind === 'fav') {
        const f = _radioFavs.find(x => x.uuid === p.id);
        return f ? f.name : 'Favourite station';
    }
    if (p.kind === 'tc') {
        const row = _syncTidalItem(gid);
        return (row && (row.title || row.name)) || 'Tidal library';
    }
    if (!p.id) return 'Custom URL';
    if (p.kind === 'tidal') {
        const t = _recentCache.find(x => x.source_id === p.id);
        return (t && t.title) || 'Tidal track';
    }
    const it = _recentCache.find(x => x.url === p.id);
    return (it && it.title) || _syncUrlName(p.id);
}

// Recently-played entries worth offering. Tidal travels by source id, never by
// URL: its stream URLs are signed for minutes, so a stored one would be dead
// before it was next used. The engine resolves an id at session start and
// again each time the decoder restarts, which is what makes a long session
// survive the token expiring underneath it.
function _syncRecentKey(it) {
    if (it.media_type === 'tidal') {
        return it.source_id ? 'tidal:' + it.source_id : '';
    }
    return it.url ? 'url:' + it.url : '';
}

function _syncRecentPickable() {
    const seen = new Set();
    return _recentCache.filter(it => {
        const k = _syncRecentKey(it);
        if (!k || seen.has(k)) return false;
        seen.add(k);
        return true;
    }).slice(0, 8);
}

// While a session runs, say what it is playing and give advice that matches:
// "until the clicks land together" is meaningless over music.
/** The zone's scope. Markup only — mounting happens after the pane is in the
 *  DOM (renderSyncPane), because the renderer looks the canvas up by id. */
function _syncZoneScope(gid) {
    return `<canvas id="zonespec-${esc(gid)}" class="zmm-eq-spec zmm-zone-spec"
                    aria-hidden="true"></canvas>`;
}

/** Mount a scope per active zone and drop the rest. Called on every pane
 *  render: groups start and stop, and a scope on a canvas that no longer
 *  exists would burn a rAF loop for the life of the page. */
function _mountZoneScopes() {
    for (const g of (_syncGroups || [])) {
        const id = `zonespec-${g.id}`;
        if (!g.active || !document.getElementById(id)) { unmountScope(id); continue; }
        mountScope(id, {
            getFrame: () => _zoneFrame(g.id),
            getEq: () => _zoneEq[g.id] || null,
        });
        if (!(g.id in _zoneEq)) _zoneEqFetch(g.id);
    }
}

// The zone's EQ curve comes from the server chain (eq_stream keys it
// "syncgroup:<id>"), not from this browser's local EQ — different filters,
// different gains. Fetched once per group and refreshed when the pane
// re-renders after a settings change.
const _zoneEq = {};

async function _zoneEqFetch(gid) {
    _zoneEq[gid] = null;                       // in flight — don't refetch
    try {
        const r = await fetch('/api/media/eq?player_id='
                              + encodeURIComponent('syncgroup:' + gid))
            .then(x => x.json());
        _zoneEq[gid] = {
            enabled: !!r.enabled,
            gains: Array.isArray(r.gains) ? r.gains : [],
            bands: eq.BANDS,
            rate: 44100,                       // the sync timeline's rate
        };
    } catch {
        _zoneEq[gid] = { enabled: false, gains: [], bands: eq.BANDS, rate: 44100 };
    }
}

function _syncActiveHint() {
    const src = (_syncStatus || {}).source || {};
    const np = (_syncStatus || {}).now_playing || {};
    const generated = src.kind !== 'media';
    const what = src.title || (generated ? 'Sync test signal' : 'Media');
    // A queue says where it is; the speakers' own displays can't (their
    // metadata is fixed at load, or the zone would re-buffer per track).
    const pos = np.count > 1
        ? ` <span class="text-muted">· ${np.index + 1} of ${np.count}</span>` : '';
    const art = np.artwork_url
        ? `<img src="${esc(np.artwork_url)}" alt="" class="rounded me-2"
                style="width:34px;height:34px;object-fit:cover;vertical-align:middle">` : '';
    const under = (src.underruns || 0) > 0
        ? ` <span class="text-warning" title="The decoder could not keep up — ${src.underrun_ms} ms of silence so far">
              <i class="fas fa-triangle-exclamation"></i> ${src.underruns} underrun${src.underruns === 1 ? '' : 's'}</span>`
        : '';
    // What the last seam actually did. Shortening or abandoning a fade is
    // normal — the overlap comes out of buffer that may not be there when an
    // item happens to end — so without saying so, a listener who asked for
    // 1.2s and heard a splice has no way to tell that from a broken feature.
    const xf = src.crossfade_s > 0 ? (() => {
        const n = src.crossfades || 0;
        const last = src.crossfade_last || '';
        const short = last && !/^\d/.test(last);   // a reason, not a duration
        return ` <span class="${short ? 'text-muted' : ''}"
                       title="Asked for ${src.crossfade_s}s. ${last ? 'Last seam: ' + esc(last) + '.' : ''}">
                   <i class="fas fa-right-left"></i> ${n} crossfade${n === 1 ? '' : 's'}${short ? ' · last seam spliced' : ''}</span>`;
    })() : '';
    const advice = generated
        ? 'Stand between the speakers and drag each trim until the clicks land together. Positive = plays later.'
        : 'Drag each trim until the speakers stop echoing. Positive = plays later — tune on the test signal first, the clicks are far easier to align by ear.';
    return `<div class="small text-muted mt-1 d-flex align-items-start" id="syncActiveHint">
              ${art}
              <div>
                <div><i class="fas fa-music me-1"></i>
                  <span class="fw-semibold">${esc(np.title || what)}</span>
                  ${np.artist ? `<span class="text-muted"> — ${esc(np.artist)}</span>` : ''}
                  ${pos}${under}${xf}</div>
                <div>${advice}</div>
              </div>
            </div>`;
}

function _syncSrcSelect(gid, disabled) {
    const sel = _syncSrcFor(gid);
    const opt = (v, label) =>
        `<option value="${esc(v)}" ${sel === v ? 'selected' : ''}>${esc(label)}</option>`;
    const favs = _radioFavs.length
        ? `<optgroup label="Favourite stations">
             ${_radioFavs.map(f => opt('fav:' + f.uuid, f.name)).join('')}</optgroup>`
        : '';
    const recent = _syncRecentPickable();
    const recents = recent.length
        ? `<optgroup label="Recently played">
             ${recent.map(it => opt(_syncRecentKey(it),
                                    it.title || it.url)).join('')}</optgroup>`
        : '';
    return `
      <select class="form-select form-select-sm w-auto" id="syncsrc-${esc(gid)}"
              ${disabled ? 'disabled' : ''}
              title="What this group plays. The test signal is the tuning ruler; anything else is real audio, equalised and clock-aligned server-side."
              onchange="window.mediaSyncSrc('${esc(gid)}', this.value)">
        ${opt('', 'Sync test signal')}${favs}${recents}
        ${_tidalState === 'logged_in'
            ? `<option value="tc:${esc(_syncTidalPick(gid)?.kind || 'playlists')}"
                       ${sel.startsWith('tc:') ? 'selected' : ''}>Tidal library…</option>`
            : ''}
        ${opt('custom', 'Custom URL…')}
      </select>`;
}

/** Second row for "Tidal library…": what kind, then which one.
 *
 *  Two small selects rather than one giant flat list — a library runs to
 *  hundreds of albums, and the whole point of a zone picker is to stay one
 *  line tall next to the Play button. The list loads only when its kind is
 *  chosen, and is cached for the session. */
function _syncTidalRow(gid, disabled) {
    const t = _syncTidalPick(gid);
    if (!t) return '';
    const rows = _tidalLib[t.kind];
    const KINDS = [['mixes', 'Mixes'], ['playlists', 'Playlists'],
                   ['albums', 'Albums'], ['artists', 'Artists']];
    const label = it => {
        const name = it.title || it.name || '(untitled)';
        const sub = it.artist || it.subtitle || '';
        return sub ? `${name} — ${sub}` : name;
    };
    return `
      <div class="d-flex align-items-center gap-2 mt-2 flex-wrap">
        <select class="form-select form-select-sm w-auto" ${disabled ? 'disabled' : ''}
                title="Which part of your Tidal library"
                onchange="window.mediaSyncTidalKind('${esc(gid)}', this.value)">
          ${KINDS.map(([k, l]) =>
              `<option value="${k}" ${k === t.kind ? 'selected' : ''}>${l}</option>`).join('')}
        </select>
        <select class="form-select form-select-sm" style="max-width: 22rem"
                ${disabled || !rows || !rows.length ? 'disabled' : ''}
                title="Plays the whole set, in order, across the zone"
                onchange="window.mediaSyncTidalItem('${esc(gid)}', this.value)">
          ${!rows
              ? '<option>Loading…</option>'
              : !rows.length
                  ? `<option value="">No ${t.kind} in your library</option>`
                  : `<option value="" ${t.id ? '' : 'selected'}>Choose a ${
                        { mixes: 'mix', playlists: 'playlist', albums: 'album',
                          artists: 'artist' }[t.kind]}…</option>`
                    + rows.map(it => `<option value="${esc(String(it.id))}"
                          ${String(it.id) === t.id ? 'selected' : ''}>${esc(label(it))}</option>`).join('')}
        </select>
        <div class="form-check mb-0 flex-shrink-0">
          <input class="form-check-input" type="checkbox" id="synctloop-${esc(gid)}"
                 ${_syncLoopFor(gid) ? 'checked' : ''} ${disabled ? 'disabled' : ''}
                 onchange="window.mediaSyncLoop('${esc(gid)}', this.checked)">
          <label class="form-check-label small text-nowrap" for="synctloop-${esc(gid)}"
                 title="Start again from the top when the set finishes">Repeat</label>
        </div>
      </div>`;
}

// Shown under the group row while "Custom URL…" is picked. Anything ffmpeg can
// open works — a stream, or a file path reachable inside the container.
function _syncCustomRow(gid, disabled) {
    if (_syncSrcFor(gid) !== 'custom') return '';
    const url = _syncCustomUrl(gid);
    return `
      <div class="d-flex align-items-center gap-2 mt-2 flex-wrap">
        <input type="text" class="form-control form-control-sm" id="syncurl-${esc(gid)}"
               style="flex:1 1 12rem"
               ${disabled ? 'disabled' : ''}
               placeholder="https://stream.example/live.mp3  or  /data/music/album.flac"
               value="${esc(url)}"
               title="Any source ffmpeg can open, reachable from the server"
               onchange="window.mediaSyncUrl('${esc(gid)}', this.value)">
        <div class="form-check mb-0 flex-shrink-0">
          <input class="form-check-input" type="checkbox" id="syncloop-${esc(gid)}"
                 ${_syncLoopFor(gid) ? 'checked' : ''} ${disabled ? 'disabled' : ''}
                 onchange="window.mediaSyncLoop('${esc(gid)}', this.checked)">
          <label class="form-check-label small text-nowrap" for="syncloop-${esc(gid)}"
                 title="Repeat a finite source forever. No effect on a live stream.">Loop</label>
        </div>
      </div>`;
}

// Crossfade (per zone, applied at session start)
// Stored with the zone like duration, source and loop, and sent in the start
// body: the engine reads it once when it builds the source, so a change takes
// effect on the next session rather than the current one.

const XFADE_FALLBACK_MAX_S = 1.65;   // until /status reports the real ceiling

/** The longest fade this server can actually honour. The overlap is taken out
 *  of written-but-unserved timeline, so the ceiling is a property of the
 *  source delay — asking for more does not fail, it silently degrades to a
 *  splice, which is precisely the confusion worth designing out. */
function _syncXfadeMax() {
    const m = Number(_syncStatus?.crossfade?.max_s);
    return Number.isFinite(m) && m > 0 ? m : XFADE_FALLBACK_MAX_S;
}

function _syncXfadeFor(gid) {
    const raw = _zonePlay(gid).crossfade_s;
    const dflt = Number(_syncStatus?.crossfade?.default_s) || 0;
    const v = raw === null || raw === undefined ? dflt : Number(raw);
    return Number.isFinite(v) ? Math.min(Math.max(v, 0), _syncXfadeMax()) : 0;
}

function syncSetCrossfade(gid, val) {
    const v = Math.min(Math.max(Number(val) || 0, 0), _syncXfadeMax());
    _zonePlaySet(gid, { crossfade_s: v });
    const out = document.getElementById(`syncxfv-${gid}`);
    if (out) out.textContent = _syncXfadeLabel(v);
}

function _syncXfadeLabel(v) {
    return v <= 0 ? 'off' : `${v.toFixed(2)}s`;
}

/** Slider rather than a preset list: the useful range is narrow, continuous,
 *  and bounded by something the user cannot otherwise see. The max attribute
 *  IS the ceiling, so an unhonourable value cannot be chosen in the first
 *  place — better than accepting it and degrading quietly. */
function _syncXfadeRow(gid, disabled) {
    const v = _syncXfadeFor(gid);
    const max = _syncXfadeMax();
    return `
      <div class="d-flex align-items-center gap-2 mt-2 flex-wrap zmm-zone-xfade">
        <label class="small text-muted text-nowrap mb-0" for="syncxf-${esc(gid)}"
               title="Overlap between queue items. Taken from audio already buffered but not yet sent, so it costs no extra delay — and cannot exceed what that buffer holds.">
          <i class="fas fa-right-left me-1"></i>Crossfade</label>
        <input type="range" class="form-range flex-grow-1" id="syncxf-${esc(gid)}"
               min="0" max="${max.toFixed(2)}" step="0.05" value="${v.toFixed(2)}"
               ${disabled ? 'disabled' : ''}
               oninput="window.mediaSyncXfade('${esc(gid)}', this.value)">
        <span class="small font-monospace text-nowrap" id="syncxfv-${esc(gid)}"
              style="min-width:3.2rem;text-align:right">${_syncXfadeLabel(v)}</span>
        <span class="small text-muted text-nowrap" title="Ceiling for this server: the fade is paid for out of the source delay, less a guard.">
          max ${max.toFixed(2)}s</span>
      </div>`;
}

// Zone spectrum (server-analysed, arrives over the websocket)
// Held as one latest-frame slot rather than a queue: frames arrive faster than
// the display repaints on a slow tab, and a backlog of spectra is worthless —
// only the newest is the present.
let _zoneSpec = null;

window.addEventListener('zmm-zone-spectrum', (ev) => {
    const p = ev.detail;
    if (!p || !Array.isArray(p.bands)) return;
    _zoneSpec = { bands: p.bands, fLo: p.f_lo, fHi: p.f_hi,
                  scale255: true, groupId: p.group_id, at: performance.now() };
});

/** Latest frame for a group, or null if the newest one belongs elsewhere —
 *  a zone that is not this one must not paint into this canvas. */
function _zoneFrame(gid) {
    if (!_zoneSpec || _zoneSpec.groupId !== gid) return null;
    return _zoneSpec;
}

function _fmtRemain(s) {
    if (s == null || !isFinite(s)) return '';
    const m = Math.floor(s / 60), sec = Math.max(0, Math.round(s % 60));
    return `${m}:${String(sec).padStart(2, '0')}`;
}

/** Bees circling a comb cell. Markup only — see .hive-loader in
 *  hive-components.css. Child order matters: the cell is first. */
function _hiveLoader(cls = '') {
    return `<span class="hive-loader ${cls}" role="status" aria-hidden="true">
              <span class="hive-loader__cell"></span>
              <span class="hive-loader__orbit"><span class="hive-loader__bee"></span></span>
              <span class="hive-loader__orbit"><span class="hive-loader__bee"></span></span>
              <span class="hive-loader__orbit"><span class="hive-loader__bee"></span></span>
            </span>`;
}

async function syncStartGroup(gid) {
    const media = _syncMediaFor(gid);
    // /sync/start does not return until the delay line holds enough timeline
    // for the deepest-pre-compensated member to read from, which is bounded by
    // how fast the source arrives — a live stream yields one second of buffer
    // per second, so this is a wait of seconds with nothing else to show for
    // it. Say what is happening for the whole of it, and name the reason:
    // "buffering" is the difference between a slow start and a broken button.
    const btn = document.getElementById(`syncstart-${gid}`);
    const restore = btn?.innerHTML;
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `${_hiveLoader('hive-loader--inline')}`
            + `<span class="ms-2">Buffering…</span>`;
    }
    let r;
    try {
        r = await apiPost('/api/media/sync/start', {
            group_id: gid, duration_s: _syncDurFor(gid),
            crossfade_s: _syncXfadeFor(gid),
            ...(media ? { media } : {}),  // omitted body = generated test signal
        });
    } catch (e) {
        r = { success: false, error: e.message };
    }
    if (!r.success) {
        // Only on the failure path: a success re-renders the whole pane below,
        // which replaces this button with the running state anyway.
        if (btn) { btn.disabled = false; btn.innerHTML = restore; }
        toast(r.error || 'Start failed', 'error');
        return;
    }
    const what = _syncSrcLabel(gid);
    toast(r.duration_s
        ? `${what} starting — ${Math.round(r.duration_s / 60)} min window`
        : `${what} starting — speakers join within a few seconds`, 'success');
    renderSyncPane();
}

async function syncStopSession() {
    await apiPost('/api/media/sync/stop', {});
    renderSyncPane();
}

async function syncCalibrate() {
    const btn = document.getElementById('syncCalBtn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Listening…';
    }
    try {
        const r = await apiPost('/api/media/sync/calibrate', {});
        if (r.success) {
            const parts = (r.devices || []).filter(d => d.detected)
                .map(d => `${d.name} ${d.rel_ms > 0 ? '+' : ''}${Math.round(d.rel_ms)} ms`);
            toast(`Calibrated (in-air spread was ${Math.round(r.spread_ms)} ms): `
                  + `${parts.join(', ')} — trims set`, 'success');
        } else {
            toast(r.error || 'Calibration failed', 'error');
        }
    } catch (e) {
        toast('Calibration failed: ' + e.message, 'error');
    } finally {
        renderSyncPane();   // restores the button + shows the new trims
    }
}

async function syncSetTrim(pid, val) {
    const r = await apiPost('/api/media/sync/trim', { player_id: pid, trim_ms: Number(val) });
    if (!r.success) toast(r.error || 'Trim failed', 'error');
}

async function syncNudgeTrim(pid, delta) {
    const sl = document.getElementById('synctrim-' + pid);
    if (!sl) return;
    const v = Math.max(-500, Math.min(500, Number(sl.value) + delta));
    sl.value = v;
    const lbl = document.getElementById('synctrimlbl-' + pid);
    if (lbl) lbl.textContent = v + ' ms';
    await syncSetTrim(pid, v);
}

async function submitGroup() {
    const master = document.getElementById('mediaGroupMaster')?.value;
    const members = Array.from(document.querySelectorAll('.media-group-member:checked'))
        .map(c => c.value)
        .filter(v => v !== master);
    if (!master || !members.length) {
        toast('Pick a master and at least one different member', 'warning');
        return;
    }
    const r = await apiPost('/api/media/group', { master_id: master, member_ids: members });
    if (!r.success) { toast(r.error || 'Grouping failed', 'error'); return; }
    toast('Group created', 'success');
    _groupBuilderOpen = false;
    setTimeout(loadPlayers, 1500);
}

async function ungroup(masterId) {
    const r = await apiPost('/api/media/ungroup', { master_id: masterId });
    if (!r.success) toast(r.error || 'Ungroup failed', 'error');
    else { toast('Group dissolved', 'success'); setTimeout(loadPlayers, 1500); }
}
