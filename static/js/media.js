/**
 * media.js — Media tab (Phase 1)
 * Players (Cast / WiiM), transport + volume, radio search, native group builder.
 *
 * State is pushed live over the WebSocket as `media_state`; we also fetch on
 * tab-show. Control actions POST to /api/media/*.
 */

let _players = [];          // latest PlayerState snapshot
let _selectedId = null;     // player targeted by radio "play"
let _groupBuilderOpen = false;

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
export function initMedia() {
    const tab = document.querySelector('[data-bs-target="#media"]');
    if (tab) {
        tab.addEventListener('shown.bs.tab', () => loadPlayers());
    }
    // Expose handlers for inline onclick + the websocket dispatcher.
    window.handleMediaState = handleMediaState;
    window.mediaRefresh = loadPlayers;
    window.mediaRadioSearch = radioSearch;
    window.mediaOpenGroupBuilder = toggleGroupBuilder;
    window.mediaPlayStation = playStation;
    window.mediaControl = control;
    window.mediaSetVolume = setVolume;
    window.mediaSelect = selectPlayer;
    window.mediaUngroup = ungroup;
    window.mediaSubmitGroup = submitGroup;
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
    renderPlayers();
}

function handleMediaState(payload) {
    if (!payload) return;
    _players = payload.players || [];
    // Don't yank a volume slider out from under the user mid-drag.
    const active = document.activeElement;
    if (active && active.type === 'range') return;
    if (_groupBuilderOpen) return;   // builder owns the panel while open
    renderPlayers();
}

function iconFor(p) {
    if (p.is_group) return 'fa-object-group';
    return p.provider === 'cast' ? 'fa-chromecast' : 'fa-speaker';
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
        return `
        <div class="border rounded p-2 mb-2 ${selected ? 'border-primary bg-primary-subtle' : ''}"
             onclick="window.mediaSelect('${esc(p.player_id)}')" style="cursor:pointer">
          <div class="d-flex justify-content-between align-items-center">
            <div class="text-truncate me-2">
              <i class="fas ${iconFor(p)} me-1 text-muted"></i>
              <span class="fw-semibold">${esc(p.name)}</span>
              ${p.is_group ? '<span class="badge bg-info text-dark ms-1">group</span>' : ''}
            </div>
            ${stateBadge(p)}
          </div>
          ${nowPlaying}
          <div class="d-flex align-items-center gap-2 mt-2" onclick="event.stopPropagation()">
            <button class="btn btn-sm btn-outline-secondary" ${disabled}
                    onclick="window.mediaControl('${esc(p.player_id)}','${playing ? 'pause' : 'resume'}')">
              <i class="fas ${playing ? 'fa-pause' : 'fa-play'}"></i>
            </button>
            <button class="btn btn-sm btn-outline-secondary" ${disabled}
                    onclick="window.mediaControl('${esc(p.player_id)}','stop')">
              <i class="fas fa-stop"></i>
            </button>
            <input type="range" class="form-range flex-grow-1" min="0" max="100" value="${vol}" ${disabled}
                   onchange="window.mediaSetVolume('${esc(p.player_id)}', this.value)">
            <span class="small text-muted" style="width:2.5em">${vol}%</span>
            ${p.is_group && p.provider === 'wiim'
                ? `<button class="btn btn-sm btn-outline-danger" onclick="window.mediaUngroup('${esc(p.player_id)}')" title="Ungroup">
                     <i class="fas fa-object-ungroup"></i></button>`
                : ''}
          </div>
        </div>`;
    }).join('');
}

function selectPlayer(id) {
    _selectedId = id;
    renderPlayers();
    const p = _players.find(x => x.player_id === id);
    if (p) toast(`Selected ${p.name} — radio will play here`, 'info');
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
// Radio
// ---------------------------------------------------------------------------
async function radioSearch() {
    const q = document.getElementById('mediaRadioQuery')?.value?.trim();
    const out = document.getElementById('mediaRadioResults');
    if (!q || !out) return;
    out.innerHTML = '<div class="text-muted small py-2"><i class="fas fa-spinner fa-spin"></i> Searching…</div>';
    const data = await apiGet(`/api/media/radio/search?q=${encodeURIComponent(q)}&limit=25`);
    if (!data.success) {
        out.innerHTML = `<div class="alert alert-warning mb-0">${esc(data.error || 'Search failed')}</div>`;
        return;
    }
    const stations = data.stations || [];
    if (!stations.length) {
        out.innerHTML = '<div class="text-muted small py-2">No stations found.</div>';
        return;
    }
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
    if (!_selectedId) {
        toast('Select a player first (click one in the Players list)', 'warning');
        return;
    }
    const r = await apiPost('/api/media/play', { player_id: _selectedId, station_uuid: uuid });
    if (!r.success) toast(r.error || 'Play failed', 'error');
    else { toast(`Playing ${name}`, 'success'); setTimeout(loadPlayers, 1500); }
}

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
