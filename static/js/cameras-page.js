/* Cameras tab, as three views: Live (the grid, anyone with camera:read),
   Recordings, and Settings (admin: the sidecars' state and the camera list).
   A camera is added and edited in its own window, a tab per concern.
   Backend: modules/cameras.py, modules/go2rtc.py — docs/cameras.md. */

import { escapeHtml } from './utils.js';
import { attachPlayer } from './camera-player.js';
import { mountZoneEditor } from './camera-zones.js';
import { renderRecordings } from './camera-recordings.js';

let cameras = [];
const players = new Map();            // id -> player, only for cards on screen
let observer = null;
let recordingsView = null;            // set while the Recordings view is open
let zoneEditor = null;                // set while a camera is being edited
let view = 'live';

async function api(method, url, body) {
    const res = await fetch(url, {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`);
    return data;
}
const can = s => !!(window.zmmAuth?.hasScope?.('admin') || window.zmmAuth?.hasScope?.(s));
const isAdmin = () => !!window.zmmAuth?.hasScope?.('admin');

function stopAll() {
    for (const p of players.values()) p.stop();
    players.clear();
    observer?.disconnect();
}

// Grid

// What object detection can report; the keys are the device's state keys.
const OBJECTS = { person: ['fa-person', 'Person'], vehicle: ['fa-car-side', 'Vehicle'], animal: ['fa-paw', 'Animal'] };

function card(c) {
    let motion = c.motion ? '<span class="badge bg-warning text-dark ms-1"><i class="fas fa-person-running"></i> motion</span>' : '';
    for (const [k, [icon, label]] of Object.entries(OBJECTS)) {
        if (c.objects?.[k]) motion += `<span class="badge bg-danger ms-1"><i class="fas ${icon}"></i> ${label.toLowerCase()}</span>`;
    }
    const offline = c.online === false ? '<span class="badge bg-secondary ms-1">offline</span>' : '';
    return `<div class="col-12 col-md-6 col-xl-4">
        <div class="card shadow-sm h-100">
            <div class="card-header py-2 d-flex align-items-center">
                <span class="fw-semibold text-truncate">${escapeHtml(c.name)}</span>${motion}${offline}
                ${c.enabled === false ? '<span class="badge bg-secondary ms-auto">disabled</span>' :
                  `<button class="btn btn-sm btn-link ms-auto p-0 cam-full" data-id="${escapeHtml(c.id)}"
                     title="Full screen" aria-label="Full screen ${escapeHtml(c.name)}"><i class="fas fa-expand"></i></button>`}
            </div>
            <div class="card-body p-1"><div class="cam-view ratio ratio-16x9 bg-black rounded" data-id="${escapeHtml(c.id)}"></div></div>
        </div></div>`;
}

let lastError = null;
function renderGrid(root, error = lastError) {
    lastError = error;
    stopAll();
    const grid = root.querySelector('#cam-grid');
    const live = cameras.filter(c => c.enabled !== false);
    grid.innerHTML = cameras.length ? cameras.map(card).join('') : `
        <div class="col-12 text-muted small">No cameras yet.${isAdmin() ? ' Add one under <strong>Settings</strong>.' : ''}</div>`;
    root.querySelector('#cam-error').innerHTML = error && isAdmin()
        ? `<div class="alert alert-warning small py-2">${escapeHtml(error)} — see Settings.</div>` : '';
    // Stream only what's on screen; a hidden card costs the hub a transcode-free
    // copy of the stream, but the camera's bandwidth is the scarcer thing.
    observer = new IntersectionObserver(entries => {
        for (const e of entries) {
            const id = e.target.dataset.id;
            if (e.isIntersecting && !players.has(id)) {
                const inner = document.createElement('div');
                e.target.append(inner);
                players.set(id, attachPlayer(inner, id));
            } else if (!e.isIntersecting && players.has(id)) {
                players.get(id).stop();
                players.delete(id);
                e.target.innerHTML = '';
            }
        }
    }, { threshold: 0.2 });
    grid.querySelectorAll('.cam-view').forEach(el => {
        if (live.some(c => c.id === el.dataset.id)) observer.observe(el);
    });
    grid.querySelectorAll('.cam-full').forEach(b => b.addEventListener('click', () => openCameraModal(b.dataset.id)));
}

// Manage (admin)

// The editor: one window, a tab per concern. Every field stays in the DOM
// whichever tab is showing, so collectForm reads them all.
function field(id, label, value, attrs = '', col = 'col-12') {
    return `<div class="${col}"><label class="form-label small mb-1" for="${id}">${label}</label>
        <input class="form-control form-control-sm" id="${id}" value="${escapeHtml(value ?? '')}" ${attrs}></div>`;
}
const tip = text => `<div class="small text-muted mt-2">${text}</div>`;

function streamPane(c) {
    return `
        ${c.id ? '' : `<div class="mb-2"><button type="button" class="btn btn-sm btn-outline-primary" id="cam_discover">
            <i class="fas fa-magnifying-glass me-1"></i>Find ONVIF cameras</button><div id="cam_found" class="mt-2"></div></div>`}
        <div class="row g-2">
            ${field('cf_name', 'Name', c.name, 'maxlength="60"', 'col-12 col-md-5')}
            ${field('cf_url', 'Stream URL', c.url, 'placeholder="rtsp://192.168.1.50:554/stream1" autocomplete="off" spellcheck="false"', 'col-12 col-md-7')}
            ${field('cf_user', 'Username', c.username, 'autocomplete="off"', 'col-6')}
            <div class="col-6"><label class="form-label small mb-1" for="cf_pass">Password</label>
                <input class="form-control form-control-sm" id="cf_pass" type="password" autocomplete="new-password"
                       placeholder="${c.has_credentials ? 'stored — blank keeps it' : ''}"></div>
            ${field('cf_ohost', 'ONVIF host <span class="text-muted">(for motion)</span>', c.onvif?.host, 'placeholder="optional"', 'col-8')}
            ${field('cf_oport', 'Port', c.onvif?.port || 80, 'type="number" min="1" max="65535"', 'col-4')}
        </div>
        <div id="cam_profiles" class="mt-2"></div>
        <div class="form-check form-switch mt-2">
            <input class="form-check-input" type="checkbox" id="cf_motion" ${c.motion ? 'checked' : ''}>
            <label class="form-check-label small" for="cf_motion">Use the camera's ONVIF motion events</label>
        </div>
        <div class="form-check form-switch">
            <input class="form-check-input" type="checkbox" id="cf_enabled" ${c.enabled === false ? '' : 'checked'}>
            <label class="form-check-label small" for="cf_enabled">Enabled</label>
        </div>`;
}

function detectPane(c) {
    return `
        <div class="form-check form-switch">
            <input class="form-check-input" type="checkbox" id="cf_detect" ${c.detect?.enabled ? 'checked' : ''}>
            <label class="form-check-label" for="cf_detect">Detect objects on this camera</label>
        </div>
        <div class="small fw-semibold mt-3 mb-1">Look for</div>
        ${Object.entries(OBJECTS).map(([k, [icon, label]]) => `<div class="form-check form-check-inline">
            <input class="form-check-input cf-obj" type="checkbox" id="cf_obj_${k}" value="${k}"
                ${(c.detect?.labels || Object.keys(OBJECTS)).includes(k) ? 'checked' : ''}>
            <label class="form-check-label small" for="cf_obj_${k}"><i class="fas ${icon} me-1"></i>${label}</label></div>`).join('')}
        <div class="row g-2 mt-2">
            ${field('cf_dconf', 'Sure by (%)', Math.round((c.detect?.threshold || 0.5) * 100), 'type="number" min="30" max="95" step="5"', 'col-4')}
            ${field('cf_durl', 'Low-resolution stream', c.detect?.url, 'placeholder="optional — the sub-stream" autocomplete="off" spellcheck="false"', 'col-8')}
        </div>
        ${tip(`Needs object detection enabled in the ZMM Manager. Left blank, detection shares the stream on the
            Stream tab — one connection to the camera. A sub-stream is a second connection, but much less work for the hub.`)}
        ${c.id && c.detect?.enabled ? `<button type="button" class="btn btn-sm btn-outline-secondary mt-2" id="cf_last">
            <i class="fas fa-image me-1"></i>Last detection</button><div id="cf_last_view" class="mt-2"></div>` : ''}`;
}

function zonesPane(c) {
    return c.id ? `<div id="cf_zones"></div>
        <div class="form-check mt-2">
            <input class="form-check-input" type="checkbox" id="cf_zonly" ${c.detect?.zones_only ? 'checked' : ''}>
            <label class="form-check-label small" for="cf_zonly">Ignore anything outside the zones
                <span class="text-muted">— the camera's person / vehicle / animal then mean "in a zone"</span></label>
        </div>` : '<div class="small text-muted">Save the camera first, then draw zones on its picture.</div>';
}

function recordPane(c) {
    return `
        <div class="row g-2">
            <div class="col-12"><label class="form-label small mb-1" for="cf_rmode">Keep</label>
                <select class="form-select form-select-sm" id="cf_rmode">
                    ${[['off', 'Nothing'], ['events', 'Clips of events'], ['continuous', 'Everything, and clips of events']].map(([v, l]) =>
                        `<option value="${v}" ${(c.record?.mode || 'off') === v ? 'selected' : ''}>${l}</option>`).join('')}
                </select></div>
        </div>
        <div class="small fw-semibold mt-3 mb-1">An event is</div>
        ${[['motion', 'fa-person-running', 'Motion'], ...Object.entries(OBJECTS).map(([k, v]) => [k, ...v])].map(([k, icon, label]) => `<div class="form-check form-check-inline">
            <input class="form-check-input cf-rev" type="checkbox" id="cf_rev_${k}" value="${k}"
                ${(c.record?.events?.length ? c.record.events : Object.keys(OBJECTS)).includes(k) ? 'checked' : ''}>
            <label class="form-check-label small" for="cf_rev_${k}"><i class="fas ${icon} me-1"></i>${label}</label></div>`).join('')}
        <div class="row g-2 mt-2">
            ${field('cf_rpre', 'Seconds before', c.record?.pre_s ?? 5, 'type="number" min="0" max="30"', 'col-6 col-md-3')}
            ${field('cf_rpost', 'Seconds after', c.record?.post_s ?? 10, 'type="number" min="0" max="60"', 'col-6 col-md-3')}
            ${field('cf_rdays', 'Keep clips (days)', c.record?.clip_days ?? 14, 'type="number" min="1" max="90"', 'col-6 col-md-3')}
            ${field('cf_rhours', 'Keep everything (hours)', c.record?.hours ?? 24, 'type="number" min="1" max="168"', 'col-6 col-md-3')}
        </div>
        ${tip(`Needs Recording enabled in the ZMM Manager. Motion needs ONVIF events; person, vehicle and animal need
            detection on. Whether cameras record only while everyone is away is one switch for the house, under Recordings.`)}`;
}

const TABS = [['stream', 'Stream', streamPane], ['detect', 'Detection', detectPane],
              ['zones', 'Zones', zonesPane], ['record', 'Recording', recordPane]];

function collectForm() {
    const v = id => document.getElementById(id)?.value?.trim() ?? '';
    const host = v('cf_ohost');
    return {
        name: v('cf_name'), url: v('cf_url'), username: v('cf_user'), password: v('cf_pass'),
        onvif: host ? { host, port: Number(v('cf_oport')) || 80 } : null,
        motion: document.getElementById('cf_motion').checked,
        enabled: document.getElementById('cf_enabled').checked,
        record: {
            mode: v('cf_rmode'), events: [...document.querySelectorAll('.cf-rev:checked')].map(b => b.value),
            clip_days: Number(v('cf_rdays')) || 14, hours: Number(v('cf_rhours')) || 24,
            pre_s: Number(v('cf_rpre') || 5), post_s: Number(v('cf_rpost') || 10),
        },
        detect: {
            enabled: document.getElementById('cf_detect').checked,
            labels: [...document.querySelectorAll('.cf-obj:checked')].map(b => b.value),
            url: v('cf_durl'), threshold: (Number(v('cf_dconf')) || 50) / 100,
            // Absent keeps what's stored: the add form has no zone editor.
            ...(zoneEditor ? { zones: zoneEditor.value(), zones_only: !!document.getElementById('cf_zonly')?.checked } : {}),
        },
    };
}

// Settings (admin): the sidecars at a glance, then the camera list.

function statusRow(icon, name, ok, text, managerLink) {
    return `<div class="d-flex flex-wrap align-items-center gap-2 py-1">
        <i class="fas ${icon} fa-fw text-muted"></i><span class="fw-semibold">${name}</span>
        <span class="badge ${ok ? 'bg-success' : 'bg-secondary'}">${escapeHtml(text)}</span>
        ${managerLink ? `<a class="small ms-auto" href="#" data-zmm-manager>${ok ? 'Manage' : 'Enable'} in ZMM Manager</a>` : ''}</div>`;
}

function camBadges(c) {
    const b = [];
    if (c.enabled === false) b.push('<span class="badge bg-secondary">disabled</span>');
    if (c.motion) b.push('<span class="badge text-bg-light border">ONVIF motion</span>');
    if (c.detect?.enabled) b.push(`<span class="badge text-bg-light border"><i class="fas fa-eye me-1"></i>${escapeHtml(c.detect.labels.join(', '))}</span>`);
    if (c.detect?.zones?.length) b.push(`<span class="badge text-bg-light border"><i class="fas fa-draw-polygon me-1"></i>${c.detect.zones.length} zone${c.detect.zones.length === 1 ? '' : 's'}</span>`);
    if (c.record?.mode && c.record.mode !== 'off') b.push(`<span class="badge text-bg-light border"><i class="fas fa-circle text-danger me-1" style="font-size:.5rem;vertical-align:middle"></i>${c.record.mode === 'continuous' ? 'records all' : 'clips'}</span>`);
    return b.join(' ');
}

async function renderSettings(root) {
    const el = root.querySelector('#cam-view-settings');
    el.innerHTML = '<div class="text-muted small">Loading…</div>';
    const [g, vis] = await Promise.all([api('GET', '/api/cameras/go2rtc').catch(e => ({ error: e.message })),
                                        api('GET', '/api/cameras/vision').catch(() => null)]);
    const where = { coral: 'Coral Edge TPU', cpu: 'CPU' }[vis?.backend] || vis?.backend;
    el.innerHTML = `
        <div class="card shadow-sm mb-3"><div class="card-body py-2">
            ${statusRow('fa-tower-broadcast', 'Streaming (go2rtc)', !!g.healthy, g.healthy ? 'running' : 'not reachable', true)}
            ${g.error ? `<div class="small text-danger">${escapeHtml(g.error)}</div>` : ''}
            ${statusRow('fa-eye', 'Object detection', !!(vis?.reachable && vis.ready),
                        !vis?.reachable ? 'not running' : !vis.ready ? 'getting the model…' : `on ${where}`, true)}
            ${vis?.note ? `<div class="small text-warning-emphasis">${escapeHtml(vis.note)}</div>` : ''}
            <details class="mt-1"><summary class="small text-muted">go2rtc address</summary>
                <div class="row g-2 mt-1">
                    ${field('g2_url', 'ZMM reaches it at', g.url, '', 'col-12 col-md-6')}
                    ${field('g2_listen', 'It listens on', g.listen, '', 'col-12 col-md-6')}
                </div>
                <button class="btn btn-sm btn-outline-secondary mt-2" id="g2_save">Save and restart go2rtc</button>
            </details>
        </div></div>
        <div class="d-flex align-items-center mb-2">
            <strong>Cameras</strong>
            <button class="btn btn-sm btn-primary ms-auto" id="cam_new"><i class="fas fa-plus me-1"></i>Add camera</button>
        </div>
        <div class="list-group mb-3">
            ${cameras.length ? cameras.map(c => `<div class="list-group-item">
                <div class="d-flex flex-wrap align-items-center gap-2">
                    <span class="fw-semibold me-auto text-break">${escapeHtml(c.name)}</span>
                    <button class="btn btn-sm btn-outline-secondary cam-edit" data-id="${escapeHtml(c.id)}"><i class="fas fa-pen me-1"></i>Edit</button>
                    <button class="btn btn-sm btn-outline-danger cam-del" data-id="${escapeHtml(c.id)}" aria-label="Remove ${escapeHtml(c.name)}"><i class="fas fa-trash"></i></button>
                </div>
                <div class="small text-muted text-break">${escapeHtml(c.url)}</div>
                <div class="mt-1">${camBadges(c)}</div>
            </div>`).join('') : '<div class="list-group-item small text-muted">No cameras yet.</div>'}
        </div>`;
    const on = (id, fn) => el.querySelector('#' + id)?.addEventListener('click', fn);
    on('g2_save', async () => {
        try {
            await api('PUT', '/api/cameras/go2rtc', { url: el.querySelector('#g2_url').value.trim(), listen: el.querySelector('#g2_listen').value.trim() });
            window.toast?.success('Saved');
        } catch (e) { window.toast?.error(e.message); }
        renderSettings(root);
    });
    on('cam_new', () => openEditor(root, null));
    el.querySelectorAll('.cam-edit').forEach(b => b.addEventListener('click', () => openEditor(root, b.dataset.id)));
    el.querySelectorAll('.cam-del').forEach(b => b.addEventListener('click', async () => {
        const c = cameras.find(x => x.id === b.dataset.id);
        if (b.dataset.confirm !== '1') {        // two-step, no browser dialog
            b.dataset.confirm = '1';
            b.innerHTML = `Remove ${escapeHtml(c?.name)}?`;
            return;
        }
        try { await api('DELETE', `/api/cameras/${encodeURIComponent(b.dataset.id)}`); await refresh(root); }
        catch (e) { window.toast?.error(e.message); }
    }));
}

function openEditor(root, id) {
    const c = id ? cameras.find(x => x.id === id) : {};
    document.getElementById('camEditModal')?.remove();
    document.body.insertAdjacentHTML('beforeend', `
        <div class="modal fade" id="camEditModal" tabindex="-1" aria-labelledby="camEditTitle">
          <div class="modal-dialog modal-lg modal-dialog-scrollable modal-fullscreen-sm-down"><div class="modal-content">
            <div class="modal-header py-2">
              <h6 class="modal-title" id="camEditTitle"><i class="fas fa-video me-1"></i>${id ? escapeHtml(c.name) : 'Add a camera'}</h6>
              <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button></div>
            <div class="px-2 pt-1 border-bottom">
              <ul class="nav nav-underline nav-fill small flex-nowrap" role="tablist">
                ${TABS.map(([k, label], i) => `<li class="nav-item" role="presentation">
                  <button class="nav-link px-1 ${i ? '' : 'active'} text-nowrap" data-bs-toggle="tab" data-bs-target="#cf_tab_${k}" type="button" role="tab">${label}</button></li>`).join('')}
              </ul></div>
            <div class="modal-body"><div class="tab-content">
              ${TABS.map(([k, , pane], i) => `<div class="tab-pane ${i ? '' : 'show active'}" id="cf_tab_${k}" role="tabpanel">${pane(c)}</div>`).join('')}
            </div></div>
            <div class="modal-footer py-2">
              <button type="button" class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
              <button type="button" class="btn btn-sm btn-primary" id="cf_save">${id ? 'Save' : 'Add camera'}</button>
            </div>
          </div></div></div>`);
    const el = document.getElementById('camEditModal');
    zoneEditor = id ? mountZoneEditor(el.querySelector('#cf_zones'), id, c.detect?.zones || [],
                                      c.detect?.labels?.length ? c.detect.labels : Object.keys(OBJECTS)) : null;
    el.querySelector('#cf_last')?.addEventListener('click', () => showLastDetection(el.querySelector('#cf_last_view'), id));
    el.querySelector('#cam_discover')?.addEventListener('click', discover);
    el.querySelector('#cf_save').addEventListener('click', async ev => {
        ev.currentTarget.disabled = true;
        try {
            if (id) await api('PUT', `/api/cameras/${encodeURIComponent(id)}`, collectForm());
            else await api('POST', '/api/cameras', collectForm());
            window.toast?.success(id ? 'Saved' : 'Camera added');
            bootstrap.Modal.getInstance(el)?.hide();
            await refresh(root);
        } catch (e) { window.toast?.error(e.message); }
        const b = el.querySelector('#cf_save');
        if (b) b.disabled = false;
    });
    el.addEventListener('hidden.bs.modal', () => { zoneEditor = null; el.remove(); });
    new bootstrap.Modal(el).show();
}

async function discover(ev) {
    const btn = ev.currentTarget, out = document.getElementById('cam_found');
    btn.disabled = true;
    out.innerHTML = '<div class="small text-muted">Listening for cameras (3 s)…</div>';
    try {
        const r = await api('POST', '/api/cameras/discover');
        out.innerHTML = r.cameras.length ? `<div class="list-group">${r.cameras.map(f => `
            <button type="button" class="list-group-item list-group-item-action small cam-use" data-host="${escapeHtml(f.host)}"
                data-port="${escapeHtml(f.port)}" data-name="${escapeHtml(f.name || f.hardware || f.host)}" ${f.added ? 'disabled' : ''}>
                <strong>${escapeHtml(f.name || f.hardware || 'Camera')}</strong> ${escapeHtml(f.host)}
                ${f.added ? '<span class="badge bg-secondary ms-1">added</span>' : ''}</button>`).join('')}</div>
            <div class="small text-muted mt-1">Pick one, enter its username and password, then <em>Read streams</em>.</div>`
            : '<div class="small text-muted">None answered. Cameras on another VLAN won\'t; add them by URL.</div>';
        out.querySelectorAll('.cam-use').forEach(b => b.addEventListener('click', () => {
            document.getElementById('cf_name').value = b.dataset.name;
            document.getElementById('cf_ohost').value = b.dataset.host;
            document.getElementById('cf_oport').value = b.dataset.port;
            document.getElementById('cf_motion').checked = true;
            document.getElementById('cam_profiles').innerHTML =
                '<button type="button" class="btn btn-sm btn-outline-secondary" id="cam_probe">Read streams</button>';
            document.getElementById('cam_probe').addEventListener('click', probe);
        }));
    } catch (e) { out.innerHTML = `<div class="small text-danger">${escapeHtml(e.message)}</div>`; }
    btn.disabled = false;
}

function showLastDetection(box, id) {
    box.innerHTML = '';
    const img = new Image();
    img.className = 'img-fluid rounded';
    img.alt = 'Latest detection';
    img.onerror = () => { box.innerHTML = '<span class="small text-muted">Nothing detected on this camera yet.</span>'; };
    img.src = `/api/cameras/${encodeURIComponent(id)}/detection?t=${Date.now()}`;
    box.append(img);
}

async function probe() {
    const out = document.getElementById('cam_profiles');
    const v = id => document.getElementById(id).value.trim();
    out.innerHTML = '<div class="small text-muted">Asking the camera…</div>';
    try {
        const r = await api('POST', '/api/cameras/probe', {
            host: v('cf_ohost'), port: Number(v('cf_oport')) || 80, username: v('cf_user'), password: v('cf_pass'),
        });
        const ps = r.profiles.filter(p => p.uri);
        if (!r.events) document.getElementById('cf_motion').checked = false;
        out.innerHTML = ps.length ? `<div class="small mb-1">Streams${r.events ? '' : ' (this camera has no ONVIF events, so no motion)'}:</div>
            ${ps.map((p, i) => `<div class="form-check"><input class="form-check-input" type="radio" name="cf_prof" id="cf_prof${i}"
                value="${escapeHtml(p.uri)}" ${i === 0 ? 'checked' : ''}><label class="form-check-label small text-break" for="cf_prof${i}">
                ${escapeHtml(p.name)} ${p.width ? `${p.width}×${p.height}` : ''}</label></div>`).join('')}`
            : '<div class="small text-warning-emphasis">The camera listed no RTSP streams; enter the URL by hand.</div>';
        out.querySelectorAll('input[name=cf_prof]').forEach(r => r.addEventListener('change', () => {
            document.getElementById('cf_url').value = r.value;
        }));
        if (ps[0]) document.getElementById('cf_url').value = ps[0].uri;
    } catch (e) { out.innerHTML = `<div class="small text-danger">${escapeHtml(e.message)}</div>`; }
}

async function refresh(root) {
    try {
        const r = await api('GET', '/api/cameras');
        cameras = r.cameras;
        if (view === 'live') renderGrid(root, r.error); else lastError = r.error;
        if (view === 'settings') await renderSettings(root);
    } catch (e) {
        root.querySelector('#cam-grid').innerHTML = `<div class="col-12 text-danger small">${escapeHtml(e.message)}</div>`;
    }
}

async function show(root, next) {
    view = next;
    root.querySelectorAll('[data-cam-view]').forEach(b => {
        const on = b.dataset.camView === next;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', String(on));
    });
    for (const v of ['live', 'recordings', 'settings']) root.querySelector(`#cam-view-${v}`)?.classList.toggle('d-none', v !== next);
    // Streams run only while Live is showing.
    if (next === 'live') renderGrid(root); else stopAll();
    recordingsView?.stop();
    recordingsView = null;
    if (next === 'recordings') recordingsView = await renderRecordings(root.querySelector('#cam-view-recordings'), cameras);
    if (next === 'settings') await renderSettings(root);
}

function render(root) {
    if (!can('camera:read')) {
        root.innerHTML = '<div class="text-muted p-3">Your account can\'t view cameras. An admin can grant <code>camera:read</code>.</div>';
        return;
    }
    const tabs = [['live', 'fa-video', 'Live'], ['recordings', 'fa-film', 'Recordings'],
                  ...(isAdmin() ? [['settings', 'fa-gear', 'Settings']] : [])];
    root.innerHTML = `
        <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
            <h5 class="mb-0 me-auto"><i class="fas fa-video me-1"></i> Cameras</h5>
            <div class="btn-group btn-group-sm" role="tablist" aria-label="Cameras">
                ${tabs.map(([k, icon, label]) => `<button type="button" class="btn btn-outline-primary" role="tab" data-cam-view="${k}">
                    <i class="fas ${icon} me-1"></i>${label}</button>`).join('')}
            </div>
        </div>
        <div id="cam-error"></div>
        <div id="cam-view-live"><div class="row g-2" id="cam-grid"></div></div>
        <div id="cam-view-recordings" class="d-none"></div>
        <div id="cam-view-settings" class="d-none"></div>`;
    root.querySelectorAll('[data-cam-view]').forEach(b => b.addEventListener('click', () => show(root, b.dataset.camView)));
    refresh(root).then(() => show(root, view === 'settings' && !isAdmin() ? 'live' : view));
}

export function openCameraModal(id) {
    const c = cameras.find(x => x.id === id);
    document.getElementById('cameraModal')?.remove();
    document.body.insertAdjacentHTML('beforeend', `
        <div class="modal fade" id="cameraModal" tabindex="-1" aria-labelledby="cameraModalTitle">
          <div class="modal-dialog modal-xl modal-dialog-centered"><div class="modal-content">
            <div class="modal-header py-2"><h6 class="modal-title" id="cameraModalTitle"><i class="fas fa-video me-1"></i>
                ${escapeHtml(c?.name || 'Camera')}</h6>
              <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button></div>
            <div class="modal-body p-1"><div id="cameraModalView" class="ratio ratio-16x9 bg-black"><div></div></div></div>
          </div></div></div>`);
    const el = document.getElementById('cameraModal');
    let player = null;
    el.addEventListener('shown.bs.modal', () => {
        player = attachPlayer(document.querySelector('#cameraModalView > div'), id);
    });
    el.addEventListener('hidden.bs.modal', () => { player?.stop(); el.remove(); });
    new bootstrap.Modal(el).show();
}

export async function openCameraFromDevice(id) {
    if (!cameras.length) {
        try { cameras = (await api('GET', '/api/cameras')).cameras; } catch (_) { /* modal says unavailable */ }
    }
    openCameraModal(id);
}

export function initCamerasPage() {
    const tab = document.querySelector('[data-bs-target="#cameras"]');
    const root = document.getElementById('cameras-content');
    if (!tab || !root) return;
    tab.addEventListener('shown.bs.tab', () => render(root));
    tab.addEventListener('hidden.bs.tab', stopAll);
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) stopAll();
        else if (root.offsetParent) render(root);
    });
}
