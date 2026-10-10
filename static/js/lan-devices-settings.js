/* Settings -> APIs -> Shelly / ESPHome: find, add, edit and remove devices.
   One module for both; the kinds differ only in their credential fields.
   Backend: routes/lan_device_routes.py — docs/wifi-devices.md. */

import { escapeHtml } from './utils.js';

const KINDS = {
    shelly: {
        label: 'Shelly', port: 80, api: '/api/shelly',
        intro: 'Shelly relays, dimmers, plugs, covers and meters on their local API — no Shelly cloud. '
            + 'Gen2+ devices push changes; Gen1 is polled every few seconds.',
        fields: [{ id: 'password', label: 'Password', type: 'password',
                   help: 'Only if you set one in the Shelly app or web page (user admin).' }],
        discoverNote: 'Shellys announce themselves on the LAN; Gen1 ones only if mDNS is on in their settings.',
    },
    esphome: {
        label: 'ESPHome', port: 6053, api: '/api/esphome',
        intro: 'ESPHome devices over the native API — the same encrypted, push-based connection Home '
            + 'Assistant uses. Switches, lights, covers, sensors and buttons are all picked up.',
        fields: [
            { id: 'encryption_key', label: 'Encryption key', type: 'password',
              help: 'The <code>api: encryption: key:</code> from the device\'s YAML (44 characters).' },
            { id: 'password', label: 'API password (old firmware)', type: 'password', help: '' },
        ],
        discoverNote: 'Finds devices announcing _esphomelib on the hub\'s network.',
    },
};

async function api(method, url, body) {
    const res = await fetch(url, {
        method, headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`);
    return data;
}

function form(kind, d = {}) {
    const k = KINDS[kind];
    return `
        <div class="row g-2">
            <div class="col-12 col-md-4"><label class="form-label small mb-1" for="${kind}_host">Address</label>
                <input class="form-control form-control-sm" id="${kind}_host" placeholder="192.168.1.60"
                       value="${escapeHtml(d.host || '')}" autocomplete="off" spellcheck="false"></div>
            <div class="col-4 col-md-2"><label class="form-label small mb-1" for="${kind}_port">Port</label>
                <input class="form-control form-control-sm" id="${kind}_port" type="number" min="1" max="65535"
                       value="${escapeHtml(d.port || k.port)}"></div>
            <div class="col-8 col-md-6"><label class="form-label small mb-1" for="${kind}_name">Name <span class="text-muted">(optional)</span></label>
                <input class="form-control form-control-sm" id="${kind}_name" maxlength="60" value="${escapeHtml(d.name || '')}"></div>
            ${k.fields.map(f => `<div class="col-12 col-md-6">
                <label class="form-label small mb-1" for="${kind}_${f.id}">${f.label}</label>
                <input class="form-control form-control-sm" id="${kind}_${f.id}" type="${f.type}" autocomplete="new-password"
                       placeholder="${d.has_credentials ? 'stored — blank keeps it' : ''}">
                ${f.help ? `<small class="text-muted">${f.help}</small>` : ''}</div>`).join('')}
        </div>`;
}

function collect(kind) {
    const v = id => document.getElementById(`${kind}_${id}`)?.value?.trim() ?? '';
    const body = { host: v('host'), port: Number(v('port')) || KINDS[kind].port };
    if (v('name')) body.name = v('name');
    for (const f of KINDS[kind].fields) if (v(f.id)) body[f.id] = v(f.id);
    return body;
}

export function lanSectionHtml(kind) {
    const k = KINDS[kind];
    return `
        <p class="small text-muted">${k.intro} Added devices appear in the device list and work in rules,
            notifications, alarm zones and Frames like any other device.</p>
        <div id="${kind}_list" class="mb-3"><div class="text-muted small">Loading…</div></div>
        <div class="card"><div class="card-body">
            <div class="d-flex flex-wrap align-items-center gap-2 mb-2">
                <strong id="${kind}_form_title">Add a ${k.label} device</strong>
                <button type="button" class="btn btn-sm btn-outline-primary" id="${kind}_discover">
                    <i class="fas fa-magnifying-glass me-1"></i>Find on the network</button>
            </div>
            <div id="${kind}_found" class="mb-2"></div>
            <div id="${kind}_form">${form(kind)}</div>
            <div class="d-flex flex-wrap gap-2 mt-2">
                <button type="button" class="btn btn-sm btn-primary" id="${kind}_save">Add</button>
                <button type="button" class="btn btn-sm btn-link d-none" id="${kind}_cancel">Cancel</button>
            </div>
            <div id="${kind}_msg" class="small mt-2" role="status"></div>
        </div></div>`;
}

let editing = {};

function say(kind, text, cls = 'text-danger') {
    const el = document.getElementById(`${kind}_msg`);
    if (el) { el.className = `small mt-2 ${cls}`; el.textContent = text; }
}

function resetForm(kind) {
    editing[kind] = null;
    document.getElementById(`${kind}_form`).innerHTML = form(kind);
    document.getElementById(`${kind}_form_title`).textContent = `Add a ${KINDS[kind].label} device`;
    document.getElementById(`${kind}_save`).textContent = 'Add';
    document.getElementById(`${kind}_cancel`).classList.add('d-none');
}

async function renderList(kind) {
    const el = document.getElementById(`${kind}_list`);
    if (!el) return;
    let devices = [];
    try { devices = (await api('GET', KINDS[kind].api)).devices; }
    catch (e) { el.innerHTML = `<div class="text-danger small">${escapeHtml(e.message)}</div>`; return; }
    el.innerHTML = devices.length ? `<ul class="list-group">${devices.map(d => `
        <li class="list-group-item d-flex flex-wrap align-items-center gap-2">
            <span class="badge ${d.online ? 'bg-success' : d.online === false ? 'bg-danger' : 'bg-secondary'}">
                ${d.online ? 'online' : d.online === false ? 'offline' : '…'}</span>
            <span class="me-auto text-break"><strong>${escapeHtml(d.name)}</strong>
                <span class="text-muted small">${escapeHtml(d.model || '')} · ${escapeHtml(d.host)}</span>
                ${d.online === false && d.error ? `<div class="small text-danger">${escapeHtml(d.error)}</div>` : ''}</span>
            <button type="button" class="btn btn-sm btn-outline-secondary lan-edit" data-id="${escapeHtml(d.id)}">Edit</button>
            <button type="button" class="btn btn-sm btn-outline-danger lan-del" data-id="${escapeHtml(d.id)}">Remove</button>
        </li>`).join('')}</ul>` : '<div class="text-muted small">None added yet.</div>';
    el.querySelectorAll('.lan-edit').forEach(b => b.addEventListener('click', () => {
        const d = devices.find(x => x.id === b.dataset.id);
        editing[kind] = d.id;
        document.getElementById(`${kind}_form`).innerHTML = form(kind, d);
        document.getElementById(`${kind}_form_title`).textContent = `Edit ${d.name}`;
        document.getElementById(`${kind}_save`).textContent = 'Save';
        document.getElementById(`${kind}_cancel`).classList.remove('d-none');
        document.getElementById(`${kind}_form`).scrollIntoView({ behavior: 'smooth', block: 'center' });
    }));
    el.querySelectorAll('.lan-del').forEach(b => b.addEventListener('click', async () => {
        if (b.dataset.confirm !== '1') { b.dataset.confirm = '1'; b.textContent = 'Remove?'; return; }
        try { await api('DELETE', `${KINDS[kind].api}/${encodeURIComponent(b.dataset.id)}`); renderList(kind); }
        catch (e) { window.toast?.error(e.message); }
    }));
}

export function loadLanSection(kind) {
    renderList(kind);
    const on = (id, fn) => document.getElementById(`${kind}_${id}`)?.addEventListener('click', fn);
    on('cancel', () => resetForm(kind));
    on('save', async ev => {
        const btn = ev.currentTarget;
        btn.disabled = true;
        say(kind, editing[kind] ? 'Saving…' : 'Contacting the device…', 'text-muted');
        try {
            if (editing[kind]) await api('PUT', `${KINDS[kind].api}/${encodeURIComponent(editing[kind])}`, collect(kind));
            else await api('POST', KINDS[kind].api, collect(kind));
            say(kind, editing[kind] ? 'Saved.' : 'Added — it\'s in the device list now.', 'text-success');
            resetForm(kind);
            renderList(kind);
        } catch (e) { say(kind, e.message); }
        btn.disabled = false;
    });
    on('discover', async ev => {
        const btn = ev.currentTarget, out = document.getElementById(`${kind}_found`);
        btn.disabled = true;
        out.innerHTML = '<div class="small text-muted">Listening (3 s)…</div>';
        try {
            const found = (await api('POST', `${KINDS[kind].api}/discover`)).devices;
            out.innerHTML = found.length ? `<div class="list-group">${found.map(f => `
                <button type="button" class="list-group-item list-group-item-action small lan-use"
                    data-host="${escapeHtml(f.host)}" data-port="${escapeHtml(f.port)}" data-name="${escapeHtml(f.name)}"
                    ${f.added ? 'disabled' : ''}><strong>${escapeHtml(f.name)}</strong> ${escapeHtml(f.host)}
                    ${f.encrypted ? '<span class="badge bg-info text-dark ms-1">needs key</span>' : ''}
                    ${f.added ? '<span class="badge bg-secondary ms-1">added</span>' : ''}</button>`).join('')}</div>`
                : `<div class="small text-muted">None answered. ${KINDS[kind].discoverNote} You can add by address.</div>`;
            out.querySelectorAll('.lan-use').forEach(b => b.addEventListener('click', () => {
                document.getElementById(`${kind}_host`).value = b.dataset.host;
                document.getElementById(`${kind}_port`).value = b.dataset.port;
                document.getElementById(`${kind}_name`).value = b.dataset.name;
                document.getElementById(`${kind}_${KINDS[kind].fields[0].id}`).focus();
            }));
        } catch (e) { out.innerHTML = `<div class="small text-danger">${escapeHtml(e.message)}</div>`; }
        btn.disabled = false;
    });
}
