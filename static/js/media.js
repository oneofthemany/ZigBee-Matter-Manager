/**
 * media.js — Media tab (Phase 2)
 * Players (Cast / WiiM), transport + volume + queue, native group builder,
 * and a Radio / Tidal source switch for search & queueing.
 *
 * State is pushed live over the WebSocket as `media_state`; we also fetch on
 * tab-show. Control actions POST to /api/media/*.
 */

let _players = [];          // latest PlayerState snapshot
let _selectedId = null;     // player targeted by search "play"
let _groupBuilderOpen = false;
let _searchSource = 'radio'; // 'radio' | 'tidal'
let _tidalState = null;      // last known tidal status state string
let _tidalTab = 'search';    // tidal sub-tab: search | mixes | playlists | albums | artists

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
export function initMedia() {
    const tab = document.querySelector('[data-bs-target="#media"]');
    if (tab) {
        tab.addEventListener('shown.bs.tab', () => { loadPlayers(); refreshTidalNotice(); });
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
    else console.log(`[media:${kind}] ${msg}`);
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
        const nowPlaying = (p.title || p.artist)
            ? `<div class="small text-truncate">${esc(p.title)}${p.artist ? ' — ' + esc(p.artist) : ''}</div>`
            : '<div class="small text-muted fst-italic">Nothing playing</div>';
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
          ${nowPlaying}
          ${q ? `<div class="small text-muted">Track ${q.index + 1} of ${q.length}</div>` : ''}
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
                     <i class="fas fa-object-ungroup"></i></button>`
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
    if (src === 'tidal') renderTidalTabs();
    else showSearchBar(true);
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
    out.innerHTML = stations.map(s => `
        <div class="d-flex justify-content-between align-items-center border-bottom py-1">
          <div class="text-truncate me-2">
            <div class="small fw-semibold text-truncate">${esc(s.name)}</div>
            <div class="text-muted" style="font-size:.72rem">${esc(s.country)} ${s.bitrate ? '· ' + s.bitrate + 'kbps' : ''} ${esc(s.codec)}</div>
          </div>
          <button class="btn btn-sm btn-outline-success"
                  onclick="window.mediaPlayStation('${esc(s.uuid)}', ${JSON.stringify(s.name).replace(/"/g, '&quot;')})">
            <i class="fas fa-play"></i>
          </button>
        </div>`).join('');
}

async function playStation(uuid, name) {
    if (!requireSelected()) return;
    const r = await apiPost('/api/media/play', { player_id: _selectedId, station_uuid: uuid });
    if (!r.success) toast(r.error || 'Play failed', 'error');
    else { toast(`Playing ${name}`, 'success'); setTimeout(loadPlayers, 1500); }
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

async function tidalPlay(kind, id, mode, name) {
    if (!requireSelected()) return;
    const r = await apiPost('/api/media/tidal/play', { player_id: _selectedId, kind, id, mode });
    if (!r.success) { toast(r.error || 'Tidal play failed', 'error'); return; }
    const tag = r.radio ? ' radio ∞' : (r.count > 1 ? ` (${r.count} tracks)` : '');
    toast(`Playing ${name}${tag}`, 'success');
    setTimeout(loadPlayers, 1500);
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
      <div class="mb-2 fw-semibold"><i class="fas fa-object-group me-1"></i> Build a WiiM group</div>
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
