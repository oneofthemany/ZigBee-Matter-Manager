/**
 * media.js — Media tab (Phase 2)
 * Players (Cast / WiiM), transport + volume + queue, native group builder,
 * and a Radio / Tidal source switch for search & queueing.
 *
 * State is pushed live over the WebSocket as `media_state`; we also fetch on
 * tab-show. Control actions POST to /api/media/*.
 */
const log = zmmLog('media');


let _players = [];          // latest PlayerState snapshot
let _selectedId = null;     // player targeted by search "play"
let _groupBuilderOpen = false;
let _searchSource = 'radio'; // 'radio' | 'tidal'
let _tidalState = null;      // last known tidal status state string
let _recentCache = [];       // recently-played items, for replay-by-index
let _tidalTab = 'search';    // tidal sub-tab: search | mixes | playlists | albums | artists
let _radioFavs = [];         // pinned radio stations (full dicts)
let _radioSearchCache = [];  // last radio search results, for star-toggle by index
let _posAnchors = {};        // player_id -> {pos,at,dur,playing} for smooth progress
let _posTimer = null;        // 1s interpolation ticker for the progress bars

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
    const now = performance.now();
    for (const [pid, a] of Object.entries(_posAnchors)) {
        if (a.dur <= 0) continue;                         // radio/live: no bar
        const bar = document.getElementById('prog-' + pid);
        if (!bar) continue;
        const cur = Math.min(a.playing ? a.pos + (now - a.at) : a.pos, a.dur);
        bar.style.width = Math.max(0, Math.min(100, (cur / a.dur) * 100)).toFixed(2) + '%';
        const lbl = document.getElementById('ptime-' + pid);
        if (lbl) lbl.textContent = _fmtTime(cur) + ' / ' + _fmtTime(a.dur);
    }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
export function initMedia() {
    const tab = document.querySelector('[data-bs-target="#media"]');
    if (tab) {
        tab.addEventListener('shown.bs.tab', () => { loadPlayers(); refreshTidalNotice(); loadRecent(); loadRadioFavourites(); loadKaraoke(); });
    }
    // Expose handlers for inline onclick + the websocket dispatcher.
    window.handleMediaState = handleMediaState;
    window.mediaRefresh = loadPlayers;
    window.mediaOpenGroupBuilder = toggleGroupBuilder;
    window.mediaPlayStation = playStation;
    window.mediaControl = control;
    window.mediaSetVolume = setVolume;
    window.mediaSelect = selectPlayer;
    window.mediaUngroup = ungroup;
    window.mediaSubmitGroup = submitGroup;
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
    if (!_posTimer) _posTimer = setInterval(_tickPositions, 1000);
}

// ── Karaoke mode (cast synced lyrics to the custom receiver) ────────────────
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

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------
// Players
// ---------------------------------------------------------------------------
async function loadPlayers() {
    const el = document.getElementById('mediaPlayers');
    if (!el) return;
    const data = await apiGet('/api/media/players');
    if (!data.success) {
        el.innerHTML = `<div class="alert alert-warning mb-0">${esc(data.error || 'Media service unavailable')}</div>`;
        return;
    }
    _players = data.players || [];
    _syncPosAnchors();
    autoSelect();
    renderPlayers();
}

// If nothing is selected yet and there's exactly one (available) player,
// select it automatically so radio "play" just works without a hidden step.
function autoSelect() {
    if (_selectedId) return;
    const avail = _players.filter(p => p.available);
    if (avail.length === 1) _selectedId = avail[0].player_id;
}

function handleMediaState(payload) {
    if (!payload) return;
    _players = payload.players || [];
    _syncPosAnchors();
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
    if (p.is_group) return 'fas fa-layer-group';          // any group: stacked icon
    return p.provider === 'cast' ? 'fab fa-chromecast' : 'fas fa-volume-up';
}

// Distinct, provider-aware badge so a Cast speaker GROUP is visually different
// from an individual Cast device and from a WiiM group.
function groupBadge(p) {
    if (!p.is_group) return '';
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
    if (!_players.length) {
        el.innerHTML = `<div class="text-muted text-center py-4">
            No players found. Add WiiM device IPs under Settings → APIs, and make sure
            Cast devices are on the same subnet.</div>`;
        return;
    }
    el.innerHTML = _players.map(p => {
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
              <div id="prog-${pidE}" class="progress-bar bg-info"
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
        return `
        <div class="border rounded p-2 mb-2 ${selected ? 'border-primary bg-primary-subtle' : ''}"
             onclick="window.mediaSelect('${pid}')" style="cursor:pointer">
          <div class="d-flex justify-content-between align-items-center">
            <div class="text-truncate me-2">
              <i class="${iconFor(p)} me-1 text-muted"></i>
              <span class="fw-semibold">${esc(p.name)}</span>
              ${groupBadge(p)}
            </div>
            ${stateBadge(p)}
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
          <div class="d-flex align-items-center gap-2 mt-2" onclick="event.stopPropagation()">
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
            <input type="range" class="form-range flex-grow-1" min="0" max="100" value="${vol}" ${disabled}
                   oninput="this.nextElementSibling.textContent=this.value+'%'"
                   onchange="window.mediaSetVolume('${pid}', this.value)">
            <span class="small text-muted" style="width:2.5em">${vol}%</span>
            ${p.is_group && p.provider === 'wiim'
                ? `<button class="btn btn-sm btn-outline-danger" onclick="window.mediaUngroup('${pid}')" title="Ungroup">
                     <i class="far fa-object-ungroup"></i></button>`
                : ''}
          </div>
          ${q ? queueControls(p, q) : ''}
        </div>`;
    }).join('');
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

function selectPlayer(id) {
    _selectedId = id;
    renderPlayers();
    const p = _players.find(x => x.player_id === id);
    if (p) toast(`Selected ${p.name} — playback targets this player`, 'info');
    updateSearchTarget();
}

function updateSearchTarget() {
    const el = document.getElementById('mediaSearchTarget');
    if (!el) return;
    const p = _players.find(x => x.player_id === _selectedId);
    el.textContent = p ? `→ ${p.name}` : 'select a player';
}

async function control(playerId, action) {
    const r = await apiPost('/api/media/control', { player_id: playerId, action });
    if (!r.success) toast(r.error || 'Control failed', 'error');
    else loadPlayers();
}

async function setVolume(playerId, value) {
    const level = Math.max(0, Math.min(1, Number(value) / 100));
    const r = await apiPost('/api/media/volume', { player_id: playerId, level });
    if (!r.success) toast(r.error || 'Volume failed', 'error');
}

// ---------------------------------------------------------------------------
// Queue mode (repeat / shuffle / clear)
// ---------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------
// Search — Radio / Tidal source switch
// ---------------------------------------------------------------------------
function setSource(src) {
    _searchSource = src;
    document.getElementById('mediaSrcRadio')?.classList.toggle('btn-primary', src === 'radio');
    document.getElementById('mediaSrcRadio')?.classList.toggle('btn-outline-primary', src !== 'radio');
    document.getElementById('mediaSrcTidal')?.classList.toggle('btn-primary', src === 'tidal');
    document.getElementById('mediaSrcTidal')?.classList.toggle('btn-outline-primary', src !== 'tidal');
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

// ── Radio favourites ────────────────────────────────────────────────────────
function isFav(uuid) {
    return _radioFavs.some(f => f.uuid === uuid);
}

async function loadRadioFavourites() {
    const data = await apiGet('/api/media/radio/favourites');
    _radioFavs = (data && data.success && data.stations) ? data.stations : [];
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

async function playFavourite(uuid, name) {
    if (!requireSelected()) return;
    const r = await apiPost('/api/media/radio/favourites/play', { player_id: _selectedId, station_uuid: uuid });
    if (!r.success) toast(r.error || 'Play failed', 'error');
    else { toast(`Playing ${name}`, 'success'); setTimeout(loadPlayers, 1500); setTimeout(loadRecent, 2000); }
}

async function playStation(uuid, name) {
    if (!requireSelected()) return;
    const r = await apiPost('/api/media/play', { player_id: _selectedId, station_uuid: uuid });
    if (!r.success) toast(r.error || 'Play failed', 'error');
    else { toast(`Playing ${name}`, 'success'); setTimeout(loadPlayers, 1500); setTimeout(loadRecent, 2000); }
}

// ---------------------------------------------------------------------------
// Tidal
// ---------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------
// Full-screen synced lyrics ("now playing" karaoke screen) — free, in-app.
// Reuses the LRC engine from the Cast receiver, driven by the player's reported
// position (interpolated between polls) + the Tidal lyrics API. No Cast needed.
// ---------------------------------------------------------------------------
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
      <div style="position:absolute;inset:0;display:flex;align-items:center;gap:5vw;padding:6vh 6vw">
        <div style="flex:0 0 32vh;display:flex;flex-direction:column;align-items:center;text-align:center">
          <img id="lyrArt" alt="" style="width:32vh;height:32vh;border-radius:16px;object-fit:cover;box-shadow:0 20px 60px rgba(0,0,0,.6);background:#222">
          <div id="lyrTitle" style="font-size:3vh;font-weight:700;margin-top:2.5vh"></div>
          <div id="lyrArtist" style="font-size:2vh;opacity:.75;margin-top:.5vh"></div>
        </div>
        <div id="lyrCol" style="flex:1 1 auto;height:100%;position:relative;overflow:hidden;
             -webkit-mask-image:linear-gradient(180deg,transparent,#000 18%,#000 82%,transparent);
             mask-image:linear-gradient(180deg,transparent,#000 18%,#000 82%,transparent)">
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
    const r = await apiPost('/api/media/tidal/play', { player_id: _selectedId, kind, id, mode });
    if (!r.success) { toast(r.error || 'Tidal play failed', 'error'); return; }
    const tag = r.radio ? ' radio ∞' : (r.count > 1 ? ` (${r.count} tracks)` : '');
    toast(`Playing ${name}${tag}`, 'success');
    setTimeout(loadPlayers, 1500);
    setTimeout(loadRecent, 2000);
}

// ---------------------------------------------------------------------------
// Recently played (quick replay)
// ---------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------
// Group builder (WiiM native multiroom)
// ---------------------------------------------------------------------------
function toggleGroupBuilder() {
    _groupBuilderOpen = !_groupBuilderOpen;
    if (_groupBuilderOpen) renderGroupBuilder();
    else renderPlayers();
}

function renderGroupBuilder() {
    const el = document.getElementById('mediaPlayers');
    if (!el) return;
    const wiim = _players.filter(p => p.provider === 'wiim' && p.available && !p.is_group);
    if (wiim.length < 2) {
        el.innerHTML = `<div class="alert alert-info">
            Native grouping here needs at least two available WiiM players.
            <div class="small mt-1">Cast speaker groups are created in the Google Home app and appear automatically.</div>
            <button class="btn btn-sm btn-secondary mt-2" onclick="window.mediaOpenGroupBuilder()">Back</button></div>`;
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
        <button class="btn btn-sm btn-secondary" onclick="window.mediaOpenGroupBuilder()">Cancel</button>
      </div>`;
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
