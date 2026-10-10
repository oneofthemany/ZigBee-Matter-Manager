/* Cameras tab: a live grid for anyone with camera:read; Manage (admin) for
   go2rtc, adding cameras by URL or ONVIF discovery, editing and removing.
   Backend: modules/cameras.py, modules/go2rtc.py — docs/cameras.md. */

import { escapeHtml } from './utils.js';
import { attachPlayer } from './camera-player.js';

let cameras = [];
const players = new Map();            // id -> player, only for cards on screen
let observer = null;

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

function renderGrid(root, error) {
    stopAll();
    const grid = root.querySelector('#cam-grid');
    const live = cameras.filter(c => c.enabled !== false);
    grid.innerHTML = cameras.length ? cameras.map(card).join('') : `
        <div class="col-12 text-muted small">No cameras yet.${isAdmin() ? ' Use <strong>Manage</strong> to add one.' : ''}</div>`;
    root.querySelector('#cam-error').innerHTML = error && isAdmin()
        ? `<div class="alert alert-warning small py-2">${escapeHtml(error)} — see Manage.</div>` : '';
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

function cameraForm(c = {}) {
    return `
        <div class="row g-2">
            <div class="col-12 col-md-4"><label class="form-label small mb-1" for="cf_name">Name</label>
                <input class="form-control form-control-sm" id="cf_name" maxlength="60" value="${escapeHtml(c.name || '')}"></div>
            <div class="col-12 col-md-8"><label class="form-label small mb-1" for="cf_url">Stream URL</label>
                <input class="form-control form-control-sm" id="cf_url" placeholder="rtsp://192.168.1.50:554/stream1"
                       value="${escapeHtml(c.url || '')}" autocomplete="off" spellcheck="false"></div>
            <div class="col-6 col-md-4"><label class="form-label small mb-1" for="cf_user">Username</label>
                <input class="form-control form-control-sm" id="cf_user" autocomplete="off" value="${escapeHtml(c.username || '')}"></div>
            <div class="col-6 col-md-4"><label class="form-label small mb-1" for="cf_pass">Password</label>
                <input class="form-control form-control-sm" id="cf_pass" type="password" autocomplete="new-password"
                       placeholder="${c.has_credentials ? 'stored — blank keeps it' : ''}"></div>
            <div class="col-8 col-md-3"><label class="form-label small mb-1" for="cf_ohost">ONVIF host <span class="text-muted">(for motion)</span></label>
                <input class="form-control form-control-sm" id="cf_ohost" value="${escapeHtml(c.onvif?.host || '')}" placeholder="optional"></div>
            <div class="col-4 col-md-1"><label class="form-label small mb-1" for="cf_oport">Port</label>
                <input class="form-control form-control-sm" id="cf_oport" type="number" min="1" max="65535" value="${escapeHtml(c.onvif?.port || 80)}"></div>
        </div>
        <div class="form-check form-switch mt-2">
            <input class="form-check-input" type="checkbox" id="cf_motion" ${c.motion ? 'checked' : ''}>
            <label class="form-check-label small" for="cf_motion">Use the camera's ONVIF motion events
                (they work in rules, notifications and alarm zones)</label>
        </div>
        <div class="form-check form-switch">
            <input class="form-check-input" type="checkbox" id="cf_enabled" ${c.enabled === false ? '' : 'checked'}>
            <label class="form-check-label small" for="cf_enabled">Enabled</label>
        </div>
        <div class="form-check form-switch mt-2">
            <input class="form-check-input" type="checkbox" id="cf_detect" ${c.detect?.enabled ? 'checked' : ''}>
            <label class="form-check-label small" for="cf_detect">Detect objects on this camera
                (needs object detection enabled in the ZMM Manager)</label>
        </div>
        <div class="row g-2 mt-0 align-items-end">
            <div class="col-12 col-md-5">
                ${Object.entries(OBJECTS).map(([k, [icon, label]]) => `<div class="form-check form-check-inline">
                    <input class="form-check-input cf-obj" type="checkbox" id="cf_obj_${k}" value="${k}"
                        ${(c.detect?.labels || Object.keys(OBJECTS)).includes(k) ? 'checked' : ''}>
                    <label class="form-check-label small" for="cf_obj_${k}"><i class="fas ${icon} me-1"></i>${label}</label></div>`).join('')}
            </div>
            <div class="col-8 col-md-5"><label class="form-label small mb-1" for="cf_durl">Low-resolution stream for detection</label>
                <input class="form-control form-control-sm" id="cf_durl" placeholder="optional — the camera's sub-stream"
                       value="${escapeHtml(c.detect?.url || '')}" autocomplete="off" spellcheck="false"></div>
            <div class="col-4 col-md-2"><label class="form-label small mb-1" for="cf_dconf">Sure by (%)</label>
                <input class="form-control form-control-sm" id="cf_dconf" type="number" min="30" max="95" step="5"
                       value="${escapeHtml(Math.round((c.detect?.threshold || 0.5) * 100))}"></div>
        </div>
        <div class="small text-muted mt-1">Left blank, detection shares the stream above: one connection to the camera
            however many people are watching. A sub-stream is a second connection, but far less work for the hub's CPU.</div>`;
}

function visionCard(v) {
    const up = !!v?.reachable;
    const where = { coral: 'Coral Edge TPU', cpu: 'CPU' }[v?.backend] || v?.backend;
    const badge = !up ? '<span class="badge bg-secondary">not running</span>'
        : !v.ready ? '<span class="badge bg-warning text-dark">getting the model…</span>'
        : `<span class="badge bg-success">running on ${escapeHtml(where)}</span>`;
    const cams = Object.entries(v?.cameras || {});
    return `<div class="card shadow-sm mb-3"><div class="card-body">
        <div class="d-flex flex-wrap align-items-center gap-2 mb-2">
            <strong>Object detection</strong> ${badge}
            <a class="btn btn-sm ${up ? 'btn-outline-secondary' : 'btn-primary'}" href="#" data-zmm-manager>
                <i class="fas fa-up-right-from-square me-1"></i>${up ? 'Manage' : 'Enable'} in ZMM Manager</a>
        </div>
        <p class="small text-muted mb-2">Reports <strong>person</strong>, <strong>vehicle</strong> and <strong>animal</strong>
            on each camera you switch it on for; they work in rules, notifications and alarm zones like any sensor.
            Everything runs on the hub.</p>
        ${v?.note ? `<div class="small text-warning-emphasis mb-2">${escapeHtml(v.note)}</div>` : ''}
        ${up && v.ready ? `<div class="small text-muted mb-1">${escapeHtml(v.inference_ms || 0)} ms per look.</div>` : ''}
        ${cams.map(([id, c]) => {
            const name = cameras.find(x => x.id === id)?.name || id;
            return `<div class="small">${c.online ? '<i class="fas fa-circle text-success me-1" style="font-size:.5rem;vertical-align:middle"></i>'
                : '<i class="fas fa-circle text-secondary me-1" style="font-size:.5rem;vertical-align:middle"></i>'}
                ${escapeHtml(name)} — ${c.online ? `watching, looked ${escapeHtml(c.looks)} times in ${escapeHtml(c.frames)} frames`
                    : `<span class="text-danger">${escapeHtml(c.error || 'connecting…')}</span>`}</div>`;
        }).join('')}
    </div></div>`;
}

function collectForm() {
    const v = id => document.getElementById(id)?.value?.trim() ?? '';
    const host = v('cf_ohost');
    return {
        name: v('cf_name'), url: v('cf_url'), username: v('cf_user'), password: v('cf_pass'),
        onvif: host ? { host, port: Number(v('cf_oport')) || 80 } : null,
        motion: document.getElementById('cf_motion').checked,
        enabled: document.getElementById('cf_enabled').checked,
        detect: {
            enabled: document.getElementById('cf_detect').checked,
            labels: [...document.querySelectorAll('.cf-obj:checked')].map(b => b.value),
            url: v('cf_durl'), threshold: (Number(v('cf_dconf')) || 50) / 100,
        },
    };
}

async function renderManage(root) {
    const el = root.querySelector('#cam-manage');
    el.innerHTML = '<div class="text-muted small">Loading…</div>';
    let g;
    try { g = await api('GET', '/api/cameras/go2rtc'); }
    catch (e) { el.innerHTML = `<div class="text-danger small">${escapeHtml(e.message)}</div>`; return; }
    const status = g.healthy ? '<span class="badge bg-success">running</span>'
        : '<span class="badge bg-secondary">not reachable</span>';
    let vis = null;
    try { vis = await api('GET', '/api/cameras/vision'); } catch { /* shown as not reachable */ }
    el.innerHTML = `
        <div class="card shadow-sm mb-3"><div class="card-body">
            <div class="d-flex flex-wrap align-items-center gap-2 mb-2">
                <strong>go2rtc</strong> ${status}
                <a class="btn btn-sm ${g.healthy ? 'btn-outline-secondary' : 'btn-primary'}" href="#" data-zmm-manager>
                    <i class="fas fa-up-right-from-square me-1"></i>${g.healthy ? 'Manage' : 'Enable'} in ZMM Manager</a>
            </div>
            <p class="small text-muted mb-2">The streaming sidecar. Enable, stop and update it in the
                <strong>ZMM Manager</strong> (Services → go2rtc), which also sets it to start at boot.
                Cameras are only ever reached through ZMM; go2rtc's own RTSP and WebRTC servers are off.</p>
            ${g.error ? `<div class="small text-danger mb-2">${escapeHtml(g.error)}</div>` : ''}
            <details><summary class="small">Address</summary>
                <div class="row g-2 mt-1">
                    <div class="col-12 col-md-6"><label class="form-label small mb-1" for="g2_url">ZMM reaches it at</label>
                        <input class="form-control form-control-sm" id="g2_url" value="${escapeHtml(g.url)}"></div>
                    <div class="col-12 col-md-6"><label class="form-label small mb-1" for="g2_listen">It listens on</label>
                        <input class="form-control form-control-sm" id="g2_listen" value="${escapeHtml(g.listen)}"></div>
                </div>
                <button class="btn btn-sm btn-outline-secondary mt-2" id="g2_save">Save and restart go2rtc</button>
            </details>
        </div></div>

        ${visionCard(vis)}

        <div class="card shadow-sm mb-3"><div class="card-body">
            <div class="d-flex flex-wrap align-items-center gap-2 mb-2">
                <strong>Add a camera</strong>
                <button class="btn btn-sm btn-outline-primary" id="cam_discover"><i class="fas fa-magnifying-glass me-1"></i>Find ONVIF cameras</button>
            </div>
            <div id="cam_found" class="mb-2"></div>
            <div id="cam_form">${cameraForm()}</div>
            <div id="cam_profiles" class="mt-2"></div>
            <button class="btn btn-sm btn-primary mt-2" id="cam_add">Add camera</button>
        </div></div>

        ${cameras.length ? `<div class="card shadow-sm mb-3"><div class="card-body">
            <strong>Cameras</strong>
            <ul class="list-group list-group-flush mt-2">
                ${cameras.map(c => `<li class="list-group-item px-0 d-flex flex-wrap align-items-center gap-2">
                    <span class="me-auto text-break">${escapeHtml(c.name)} <span class="text-muted small">${escapeHtml(c.url)}</span></span>
                    ${c.detect?.enabled ? `<button class="btn btn-sm btn-outline-secondary cam-last" data-id="${escapeHtml(c.id)}">Last detection</button>` : ''}
                    <button class="btn btn-sm btn-outline-secondary cam-edit" data-id="${escapeHtml(c.id)}">Edit</button>
                    <button class="btn btn-sm btn-outline-danger cam-del" data-id="${escapeHtml(c.id)}">Remove</button></li>`).join('')}
            </ul></div></div>` : ''}`;
    wireManage(root);
}

function wireManage(root) {
    const on = (id, fn) => document.getElementById(id)?.addEventListener('click', fn);
    on('g2_save', async () => {
        try {
            await api('PUT', '/api/cameras/go2rtc', {
                url: document.getElementById('g2_url').value.trim(),
                listen: document.getElementById('g2_listen').value.trim(),
            });
            window.toast?.success('Saved');
        } catch (e) { window.toast?.error(e.message); }
        renderManage(root);
    });
    on('cam_discover', async ev => {
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
                <div class="small text-muted mt-1">Pick one, enter its username and password below, then <em>Read streams</em>.</div>`
                : '<div class="small text-muted">None answered. Cameras on another VLAN won\'t; add them by URL.</div>';
            out.querySelectorAll('.cam-use').forEach(b => b.addEventListener('click', () => {
                document.getElementById('cf_name').value = b.dataset.name;
                document.getElementById('cf_ohost').value = b.dataset.host;
                document.getElementById('cf_oport').value = b.dataset.port;
                document.getElementById('cf_motion').checked = true;
                document.getElementById('cam_profiles').innerHTML =
                    '<button class="btn btn-sm btn-outline-secondary" id="cam_probe">Read streams</button>';
                document.getElementById('cam_probe').addEventListener('click', probe);
            }));
        } catch (e) { out.innerHTML = `<div class="small text-danger">${escapeHtml(e.message)}</div>`; }
        btn.disabled = false;
    });
    on('cam_add', async () => {
        try {
            await api('POST', '/api/cameras', collectForm());
            window.toast?.success('Camera added');
            await refresh(root);
        } catch (e) { window.toast?.error(e.message); }
    });
    root.querySelectorAll('.cam-edit').forEach(b => b.addEventListener('click', () => editCamera(root, b.dataset.id)));
    root.querySelectorAll('.cam-last').forEach(b => b.addEventListener('click', () => showLastDetection(b)));
    root.querySelectorAll('.cam-del').forEach(b => b.addEventListener('click', async () => {
        const c = cameras.find(x => x.id === b.dataset.id);
        if (b.dataset.confirm !== '1') {        // two-step, no browser dialog
            b.dataset.confirm = '1';
            b.textContent = `Remove ${c?.name}?`;
            return;
        }
        try { await api('DELETE', `/api/cameras/${encodeURIComponent(b.dataset.id)}`); await refresh(root); }
        catch (e) { window.toast?.error(e.message); }
    }));
}

function showLastDetection(btn) {
    const li = btn.closest('li');
    li.querySelector('.cam-last-view')?.remove();
    const box = document.createElement('div');
    box.className = 'cam-last-view w-100';
    const img = new Image();
    img.className = 'img-fluid rounded';
    img.alt = 'Latest detection';
    img.onerror = () => { box.innerHTML = '<span class="small text-muted">Nothing detected on this camera yet.</span>'; };
    img.src = `/api/cameras/${encodeURIComponent(btn.dataset.id)}/detection?t=${Date.now()}`;
    box.append(img);
    li.append(box);
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

function editCamera(root, id) {
    const c = cameras.find(x => x.id === id);
    const form = document.getElementById('cam_form');
    form.innerHTML = cameraForm(c);
    form.scrollIntoView({ behavior: 'smooth', block: 'center' });
    const btn = document.getElementById('cam_add');
    btn.textContent = `Save ${c.name}`;
    btn.replaceWith(btn.cloneNode(true));
    document.getElementById('cam_add').addEventListener('click', async () => {
        try {
            await api('PUT', `/api/cameras/${encodeURIComponent(id)}`, collectForm());
            window.toast?.success('Saved');
            await refresh(root);
        } catch (e) { window.toast?.error(e.message); }
    });
}

async function refresh(root) {
    try {
        const r = await api('GET', '/api/cameras');
        cameras = r.cameras;
        renderGrid(root, r.error);
        if (isAdmin() && !root.querySelector('#cam-manage').classList.contains('d-none')) await renderManage(root);
    } catch (e) {
        root.querySelector('#cam-grid').innerHTML = `<div class="col-12 text-danger small">${escapeHtml(e.message)}</div>`;
    }
}

function render(root) {
    if (!can('camera:read')) {
        root.innerHTML = '<div class="text-muted p-3">Your account can\'t view cameras. An admin can grant <code>camera:read</code>.</div>';
        return;
    }
    root.innerHTML = `
        <div class="d-flex align-items-center mb-2 gap-2">
            <h5 class="mb-0"><i class="fas fa-video me-1"></i> Cameras</h5>
            ${isAdmin() ? '<button class="btn btn-sm btn-outline-secondary ms-auto" id="cam-manage-btn" aria-expanded="false"><i class="fas fa-gear me-1"></i>Manage</button>' : ''}
        </div>
        <div id="cam-error"></div>
        <div id="cam-manage" class="d-none"></div>
        <div class="row g-2" id="cam-grid"></div>`;
    document.getElementById('cam-manage-btn')?.addEventListener('click', ev => {
        const m = root.querySelector('#cam-manage');
        const open = m.classList.toggle('d-none') === false;
        ev.currentTarget.setAttribute('aria-expanded', String(open));
        if (open) renderManage(root);
    });
    refresh(root);
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
