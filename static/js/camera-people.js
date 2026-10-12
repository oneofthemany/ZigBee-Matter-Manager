/* Cameras → People: who face recognition knows. A person is a name, four
   posed photos, and any number of pictures picked from the cameras.
   Backend: vision/faces.py through /api/vision/faces — docs/vision.md §Faces. */

import { escapeHtml } from './utils.js';

const SLOTS = [
    ['front', 'fa-face-smile', 'Front', 'Looking straight at the camera'],
    ['left', 'fa-arrow-left', 'Left', 'Head turned to one side'],
    ['right', 'fa-arrow-right', 'Right', 'Head turned to the other side'],
    ['down', 'fa-arrow-down', 'Tilt', 'Head tipped down, as a camera above the door sees it'],
];
const POSE = { front: 'front', left: 'turned left', right: 'turned right', down: 'head down' };
const MAX_SIDE = 1600;

let state = { people: [], threshold: 0.363, ready: true, pid: null, tab: 'photos', found: null, cameras: [] };
let host = null;

async function api(method, url, body, raw) {
    const res = await fetch(url, {
        method,
        headers: raw ? { 'Content-Type': 'image/jpeg' } : body ? { 'Content-Type': 'application/json' } : undefined,
        body: raw || (body ? JSON.stringify(body) : undefined),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`);
    return data;
}

// Shrunk and re-encoded in the browser: a phone photo is 5 MB the hub doesn't
// need, and the copy carries no EXIF — where it was taken stays on the phone.
async function shrink(file) {
    try {
        const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
        const k = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
        const cv = document.createElement('canvas');
        cv.width = Math.round(bmp.width * k);
        cv.height = Math.round(bmp.height * k);
        cv.getContext('2d').drawImage(bmp, 0, 0, cv.width, cv.height);
        const blob = await new Promise(r => cv.toBlob(r, 'image/jpeg', 0.9));
        if (blob) return blob;
    } catch (_) { /* a format this browser can't draw: let the hub try */ }
    return file;
}

function pickFile() {
    return new Promise(resolve => {
        const input = document.createElement('input');
        input.type = 'file';
        input.accept = 'image/*';
        input.addEventListener('change', () => resolve(input.files[0] || null));
        input.click();
    });
}

const person = () => state.people.find(p => p.id === state.pid) || null;
const sampleUrl = (p, s) => `/api/vision/faces/${encodeURIComponent(p.id)}/samples/${encodeURIComponent(s.id)}?a=${s.added}`;

function agreeBadge(s) {
    if (s.agree == null) return '';
    const [cls, word, why] = s.agree >= 0.5 ? ['text-bg-success', 'strong', 'Looks like their other pictures']
        : s.agree >= state.threshold ? ['text-bg-secondary', 'ok', 'Looks enough like their other pictures']
        : ['text-bg-warning', 'weak', 'Doesn\'t look much like their other pictures — a poor photo, or someone else'];
    return `<span class="badge ${cls}" title="${why}">${word} ${s.agree.toFixed(2)}</span>`;
}

function slotTile(p, [slot, icon, label, hint]) {
    const s = p.samples.find(x => x.slot === slot);
    return `<div class="col-6 col-md-3"><div class="border rounded p-2 h-100 d-flex flex-column">
        <div class="small fw-semibold mb-1"><i class="fas ${icon} fa-fw me-1"></i>${label}</div>
        <div class="ratio ratio-1x1 bg-body-tertiary rounded mb-2 overflow-hidden">
            ${s ? `<img src="${sampleUrl(p, s)}" alt="${escapeHtml(p.name)}, ${label.toLowerCase()}" style="object-fit:cover">`
                : `<div class="d-flex align-items-center justify-content-center text-muted"><i class="fas ${icon} fa-2x opacity-25"></i></div>`}
        </div>
        ${s ? `<div class="mb-1">${agreeBadge(s)}</div>` : `<div class="small text-muted mb-1">${hint}</div>`}
        ${s?.note ? `<div class="small text-warning-emphasis mb-1">${escapeHtml(s.note)}</div>` : ''}
        <div class="mt-auto d-flex gap-1">
            <button type="button" class="btn btn-sm ${s ? 'btn-outline-secondary' : 'btn-primary'} flex-grow-1 pp-slot" data-slot="${slot}">
                <i class="fas fa-camera me-1"></i>${s ? 'Replace' : 'Add photo'}</button>
            ${s ? `<button type="button" class="btn btn-sm btn-outline-danger pp-del-sample" data-sid="${s.id}"
                aria-label="Remove the ${label.toLowerCase()} photo"><i class="fas fa-trash"></i></button>` : ''}
        </div></div></div>`;
}

function photosPane(p) {
    return `<div class="row g-2">${SLOTS.map(s => slotTile(p, s)).join('')}</div>
        <div class="small text-muted mt-2">One face per photo, in good light, close enough to fill a third of the picture.
            ${state.ready ? '' : 'The first photo takes up to a minute while the face models download.'}</div>`;
}

function foundCard(p, f) {
    const m = f.match;
    const who = f.error ? `<span class="text-warning-emphasis">${escapeHtml(f.error)}</span>`
        : !m ? 'Nobody enrolled yet'
        : m.match ? `Looks like <strong>${escapeHtml(m.name)}</strong> ${m.score.toFixed(2)}`
        : `No match <span class="text-muted">(nearest ${escapeHtml(m.name)} ${m.score.toFixed(2)})</span>`;
    return `<div class="col-6 col-md-3"><div class="border rounded p-2 h-100 d-flex flex-column">
        <div class="ratio ratio-1x1 rounded mb-2 overflow-hidden"><img src="${f.thumb}" alt="A face from the picture" style="object-fit:cover"></div>
        <div class="small mb-1">${who}</div>
        <div class="small text-muted mb-2">${POSE[f.pose] || ''}</div>
        <button type="button" class="btn btn-sm ${m?.match && m.id !== p.id ? 'btn-outline-secondary' : 'btn-primary'} mt-auto pp-take"
            data-face="${f.index}" ${f.error ? 'disabled' : ''}>
            This is ${escapeHtml(p.name)}</button></div></div>`;
}

function camerasPane(p) {
    const cams = state.cameras.filter(c => c.enabled !== false);
    const mine = p.samples.filter(s => s.source === 'camera');
    const found = state.found;
    return `
        <div class="d-flex flex-wrap gap-2 align-items-end mb-2">
            ${cams.length ? `<div class="flex-grow-1" style="min-width:10rem"><label class="form-label small mb-1" for="pp_cam">Camera</label>
                <select class="form-select form-select-sm" id="pp_cam">${cams.map(c =>
                    `<option value="${escapeHtml(c.id)}" ${c.id === state.cam ? 'selected' : ''}>${escapeHtml(c.name)}</option>`).join('')}</select></div>
                <button type="button" class="btn btn-sm btn-primary" id="pp_look"><i class="fas fa-eye me-1"></i>Look now</button>` : ''}
            <button type="button" class="btn btn-sm btn-outline-secondary" id="pp_upload"><i class="fas fa-upload me-1"></i>Use a saved picture</button>
        </div>
        <div class="small text-muted mb-2">Stand ${escapeHtml(p.name)} where the camera sees visitors, then <em>Look now</em>.
            Pictures from the real camera, by day and by night, teach it more than phone photos do.</div>
        ${found ? `<div class="row g-2 mb-3">${found.faces.length ? found.faces.map(f => foundCard(p, f)).join('')
            : '<div class="col-12 small text-muted">No face in that picture.</div>'}</div>` : ''}
        <div class="small fw-semibold mb-1">From cameras <span class="text-muted fw-normal">(${mine.length})</span></div>
        <div class="row g-2">${mine.length ? mine.map(s => `<div class="col-4 col-md-2"><div class="position-relative">
                <div class="ratio ratio-1x1 rounded overflow-hidden"><img src="${sampleUrl(p, s)}" alt="${escapeHtml(p.name)}, ${POSE[s.pose] || ''}" style="object-fit:cover"></div>
                <button type="button" class="btn btn-sm btn-danger position-absolute top-0 end-0 m-1 py-0 px-1 pp-del-sample" data-sid="${s.id}"
                    aria-label="Remove this picture"><i class="fas fa-xmark"></i></button>
                <div class="small mt-1">${agreeBadge(s)} <span class="text-muted">${POSE[s.pose] || ''}</span></div></div></div>`).join('')
            : '<div class="col-12 small text-muted">None yet.</div>'}</div>`;
}

function personPane(p) {
    const slots = SLOTS.filter(([k]) => p.samples.some(s => s.slot === k)).length;
    const near = p.nearest && p.nearest.score >= state.threshold ? p.nearest : null;
    const tabs = [['photos', `Photos <span class="text-muted">${slots}/4</span>`],
                  ['cameras', `From cameras <span class="text-muted">${p.samples.filter(s => s.source === 'camera').length}</span>`]];
    return `<div class="card shadow-sm"><div class="card-body">
        <div class="d-flex flex-wrap align-items-center gap-2 mb-2">
            <span class="fw-semibold fs-6 me-auto text-break" id="pp_name">${escapeHtml(p.name)}</span>
            <button type="button" class="btn btn-sm btn-outline-secondary" id="pp_rename"><i class="fas fa-pen me-1"></i>Rename</button>
            <button type="button" class="btn btn-sm btn-outline-danger" id="pp_delete" aria-label="Remove ${escapeHtml(p.name)}"><i class="fas fa-trash"></i></button>
        </div>
        ${near ? `<div class="alert alert-warning small py-2 mb-2"><i class="fas fa-triangle-exclamation me-1"></i>Could be mistaken for
            <strong>${escapeHtml(near.name)}</strong> (${near.score.toFixed(2)}). More pictures of both, from the cameras, help tell them apart.</div>` : ''}
        <ul class="nav nav-underline small mb-3" role="tablist">${tabs.map(([k, label]) => `<li class="nav-item" role="presentation">
            <button type="button" class="nav-link py-1 ${state.tab === k ? 'active' : ''}" role="tab" aria-selected="${state.tab === k}" data-pp-tab="${k}">${label}</button></li>`).join('')}</ul>
        ${state.tab === 'cameras' ? camerasPane(p) : photosPane(p)}
    </div></div>`;
}

function draw() {
    // Coming back from the phone's camera re-renders the Cameras tab under us.
    if (!host.isConnected) host = document.getElementById('cam-view-people') || host;
    const p = person();
    host.innerHTML = `
        <div class="d-flex align-items-center gap-2 mb-3">
            <div class="d-flex flex-nowrap gap-1 overflow-auto flex-grow-1 pb-1" role="tablist" aria-label="People">
                ${state.people.map(x => `<button type="button" role="tab" aria-selected="${x.id === state.pid}"
                    class="btn btn-sm text-nowrap ${x.id === state.pid ? 'btn-primary' : 'btn-outline-primary'} pp-person" data-pid="${x.id}">${escapeHtml(x.name)}</button>`).join('')}
            </div>
            <button type="button" class="btn btn-sm btn-outline-primary text-nowrap" id="pp_new"><i class="fas fa-user-plus me-1"></i>Add person</button>
        </div>
        <div id="pp_newform" class="d-none mb-3"><div class="input-group input-group-sm" style="max-width:24rem">
            <input class="form-control" id="pp_newname" maxlength="40" placeholder="Person's name" aria-label="Person's name">
            <button type="button" class="btn btn-primary" id="pp_create">Add</button></div></div>
        ${p ? personPane(p) : `<div class="text-muted small">Nobody is enrolled yet. Add a person, then four photos of them:
            front, left, right and head tipped down. Everything stays on the hub.</div>`}`;
    wire();
}

async function load(keep) {
    const r = await api('GET', '/api/vision/faces');
    state.people = r.people;
    state.threshold = r.threshold ?? state.threshold;
    state.ready = r.ready;
    if (!keep || !person()) state.pid = state.people[0]?.id || null;
}

async function act(fn, done) {
    try {
        await fn();
        await load(true);
        if (done) window.toast?.success(done);
    } catch (e) { window.toast?.error(e.message); }
    draw();
}

function busy(btn, text) {
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>${text}`;
}

function wire() {
    const p = person();
    const on = (sel, fn) => host.querySelectorAll(sel).forEach(b => b.addEventListener('click', ev => fn(ev.currentTarget)));
    on('.pp-person', b => { state.pid = b.dataset.pid; state.found = null; draw(); });
    on('[data-pp-tab]', b => { state.tab = b.dataset.ppTab; draw(); });
    on('#pp_new', () => {
        host.querySelector('#pp_newform').classList.remove('d-none');
        host.querySelector('#pp_newname').focus();
    });
    const create = () => {
        const name = host.querySelector('#pp_newname').value.trim();
        if (!name) return;
        act(async () => {
            const made = await api('POST', '/api/vision/faces', { name });
            state.pid = made.id;
            state.tab = 'photos';
            state.found = null;
        });
    };
    on('#pp_create', create);
    host.querySelector('#pp_newname')?.addEventListener('keydown', ev => { if (ev.key === 'Enter') create(); });
    if (!p) return;
    const base = `/api/vision/faces/${encodeURIComponent(p.id)}`;

    on('#pp_rename', () => {
        const span = host.querySelector('#pp_name');
        span.innerHTML = `<div class="input-group input-group-sm"><input class="form-control" id="pp_rename_in" maxlength="40"
            value="${escapeHtml(p.name)}" aria-label="Name"><button type="button" class="btn btn-primary" id="pp_rename_ok">Save</button></div>`;
        const input = span.querySelector('#pp_rename_in');
        const save = () => act(() => api('PATCH', base, { name: input.value.trim() }));
        span.querySelector('#pp_rename_ok').addEventListener('click', save);
        input.addEventListener('keydown', ev => { if (ev.key === 'Enter') save(); });
        input.focus();
    });
    on('#pp_delete', b => {
        if (b.dataset.confirm !== '1') {            // two-step, no browser dialog
            b.dataset.confirm = '1';
            b.innerHTML = `Remove ${escapeHtml(p.name)} and their pictures?`;
            return;
        }
        act(() => api('DELETE', base), 'Removed');
    });
    on('.pp-slot', async b => {
        const file = await pickFile();
        if (!file) return;
        busy(b, 'Checking…');
        act(async () => {
            const s = await api('PUT', `${base}/slot/${b.dataset.slot}`, null, await shrink(file));
            if (s.note) window.toast?.warning?.(s.note);
        });
    });
    on('.pp-del-sample', b => act(() => api('DELETE', `${base}/samples/${b.dataset.sid}`)));

    const show = async (btn, get) => {
        busy(btn, 'Looking…');
        try { state.found = await get(); } catch (e) { window.toast?.error(e.message); }
        draw();
    };
    host.querySelector('#pp_cam')?.addEventListener('change', ev => { state.cam = ev.target.value; });
    on('#pp_look', b => {
        state.cam = host.querySelector('#pp_cam').value;
        show(b, () => api('POST', `/api/vision/faces/detect/camera/${encodeURIComponent(state.cam)}`));
    });
    on('#pp_upload', async b => {
        const file = await pickFile();
        if (file) show(b, async () => api('POST', '/api/vision/faces/detect', null, await shrink(file)));
    });
    on('.pp-take', b => act(async () => {
        await api('POST', `${base}/samples`, { token: state.found.token, face: Number(b.dataset.face) });
        state.found = null;
    }, `Added to ${p.name}`));
}

export async function renderPeople(el, cameras) {
    host = el;
    state.cameras = cameras || [];
    state.found = null;
    el.innerHTML = '<div class="text-muted small">Loading…</div>';
    try {
        await load(true);
        draw();
    } catch (e) {
        el.innerHTML = `<div class="alert alert-warning small py-2">${escapeHtml(e.message)}</div>`;
    }
}
