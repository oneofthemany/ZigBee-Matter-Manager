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

function card(c) {
    const motion = c.motion ? '<span class="badge bg-warning text-dark ms-1"><i class="fas fa-person-running"></i> motion</span>' : '';
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
        </div>`;
}

function collectForm() {
    const v = id => document.getElementById(id)?.value?.trim() ?? '';
    const host = v('cf_ohost');
    return {
        name: v('cf_name'), url: v('cf_url'), username: v('cf_user'), password: v('cf_pass'),
        onvif: host ? { host, port: Number(v('cf_oport')) || 80 } : null,
        motion: document.getElementById('cf_motion').checked,
        enabled: document.getElementById('cf_enabled').checked,
    };
}

async function renderManage(root) {
    const el = root.querySelector('#cam-manage');
    el.innerHTML = '<div class="text-muted small">Loading…</div>';
    let g;
    try { g = await api('GET', '/api/cameras/go2rtc'); }
    catch (e) { el.innerHTML = `<div class="text-danger small">${escapeHtml(e.message)}</div>`; return; }
    const sc = g.sidecar || {};
    const status = g.healthy ? '<span class="badge bg-success">running</span>'
        : sc.installed ? '<span class="badge bg-warning text-dark">installed, not answering</span>'
        : '<span class="badge bg-secondary">not installed</span>';
    el.innerHTML = `
        <div class="card shadow-sm mb-3"><div class="card-body">
            <div class="d-flex flex-wrap align-items-center gap-2 mb-2">
                <strong>go2rtc</strong> ${status}
                ${g.healthy ? '' : `<button class="btn btn-sm btn-primary" id="g2_install" ${sc.socket ? '' : 'disabled'}>
                    ${sc.installed ? 'Start' : 'Install'} go2rtc</button>`}
            </div>
            <p class="small text-muted mb-2">The streaming sidecar (${escapeHtml(sc.image || '')}). Cameras are only ever
                reached through ZMM; go2rtc's own RTSP and WebRTC servers are switched off.
                ${sc.socket ? '' : '<br><span class="text-warning-emphasis">No container socket is mounted, so ZMM can\'t install it — see docs/cameras.md §Running go2rtc yourself.</span>'}</p>
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
                    <button class="btn btn-sm btn-outline-secondary cam-edit" data-id="${escapeHtml(c.id)}">Edit</button>
                    <button class="btn btn-sm btn-outline-danger cam-del" data-id="${escapeHtml(c.id)}">Remove</button></li>`).join('')}
            </ul></div></div>` : ''}`;
    wireManage(root);
}

function wireManage(root) {
    const on = (id, fn) => document.getElementById(id)?.addEventListener('click', fn);
    on('g2_install', async ev => {
        ev.currentTarget.disabled = true;
        ev.currentTarget.textContent = 'Installing… (pulling the image)';
        try { await api('POST', '/api/cameras/go2rtc/install'); window.toast?.success('go2rtc running'); }
        catch (e) { window.toast?.error(e.message); }
        renderManage(root);
    });
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
