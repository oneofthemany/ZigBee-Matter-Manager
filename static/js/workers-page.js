/**
 * Workers (Automations → Workers sub-tab)
 * Location: static/js/workers-page.js
 *
 * Household state a person or a rule sets, and every rule can read: a holiday
 * flag, the mode the house is in, a countdown, a tally, when something last
 * happened, a shared number. The server owns what each type means — this page
 * builds its form and its controls from /api/workers/types, so adding a type
 * on the server does not need a change here.
 *
 * Each card is also the manual actuation surface: the same control that shows
 * a worker's value is the one that sets it.
 */

import { showToast, withBusy } from './utils.js';
import { confirmDialog } from './dialogs.js';

const log = zmmLog('workers-page');

let types = {};
let workers = [];
let usage = {};
let loaded = false;
// Which worker the modal is editing, or null when it is creating one.
let editingId = null;

function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function canEdit() {
    const a = window.zmmAuth;
    return !!(a && a.hasScope && (a.hasScope('admin') || a.hasScope('automation:write')));
}

async function api(path, options = {}) {
    const res = await fetch(path, {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
        ...options,
    });
    if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch { /* keep status */ }
        throw new Error(detail);
    }
    return res.status === 204 ? null : res.json();
}

// LOAD

export function initWorkersPage() {
    const tab = document.querySelector('button[data-bs-target="#automationWorkers"]');
    if (tab) tab.addEventListener('shown.bs.tab', () => loadWorkersPage());

    // Returning to the Automations tab reloads whichever sub-tab is open. The
    // rules page hooks the same event for itself; without this, coming back to
    // an already-open Workers sub-tab would show whatever it last rendered.
    const outer = document.querySelector('button[data-bs-target="#automations"]');
    if (outer) {
        outer.addEventListener('shown.bs.tab', () => {
            if (document.querySelector('#automationWorkers.active')) loadWorkersPage();
        });
    }

    // A worker set from a rule, the app or another browser lands here rather
    // than on the next reload, because the page doubles as the control panel:
    // a switch that shows a stale position is worse than no switch.
    window.addEventListener('zmm-worker-updated', (ev) => applyLiveUpdate(ev.detail));
}

export async function loadWorkersPage() {
    const host = document.getElementById('workers-content');
    if (!host) return;
    if (!loaded) {
        host.innerHTML = `<div class="text-center text-muted py-4">
            <i class="fas fa-spinner fa-spin"></i> Loading workers...</div>`;
    }
    try {
        const data = await api('/api/workers');
        types = data.types || {};
        workers = data.workers || [];
        try {
            usage = (await api('/api/workers/usage')).usage || {};
        } catch { usage = {}; }
        loaded = true;
        render();
    } catch (e) {
        log.error('Failed to load workers', e);
        host.innerHTML = `<div class="alert alert-warning">
            Could not load workers: ${esc(e.message)}</div>`;
    }
}

function applyLiveUpdate(payload) {
    if (!payload || !loaded) return;
    const worker = workers.find(w => w.id === payload.id);
    if (!worker) return;
    worker.state = payload.state || worker.state;
    worker.display = payload.display ?? worker.display;
    const card = document.querySelector(`[data-worker-value="${CSS.escape(worker.id)}"]`);
    if (card) {
        card.textContent = worker.display;
        // A brief highlight, so a value that changed while you were looking at
        // it does not just silently differ from what you last read.
        card.classList.remove('worker-flash');
        void card.offsetWidth;
        card.classList.add('worker-flash');
    } else {
        render();
    }
    syncControls(worker);
}

// RENDER

function render() {
    const host = document.getElementById('workers-content');
    if (!host) return;

    const editable = canEdit();
    const header = `
        <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
            <div>
                <h5 class="mb-0"><i class="fas fa-helmet-safety text-warning"></i> Workers</h5>
                <div class="text-muted small">
                    State you set, that automations read. ${workers.length} worker${workers.length === 1 ? '' : 's'}.
                </div>
            </div>
            <div class="ms-auto">
                ${editable ? `<button class="btn btn-primary btn-sm" id="worker-new-btn">
                    <i class="fas fa-plus"></i> New worker</button>` : ''}
            </div>
        </div>`;

    const body = workers.length
        ? `<div class="row g-3">${workers.map(cardHtml).join('')}</div>`
        : emptyState(editable);

    host.innerHTML = header + body;

    ['worker-new-btn', 'worker-empty-new'].forEach(id => {
        const btn = document.getElementById(id);
        if (btn) btn.addEventListener('click', () => openEditor(null));
    });
    host.querySelectorAll('[data-worker-action]').forEach(bindAction);
    workers.forEach(syncControls);
}

function emptyState(editable) {
    // The empty state carries the explanation, because a worker is the one
    // thing on this page nobody has seen elsewhere in the app.
    const examples = Object.entries(types).map(([key, spec]) => `
        <div class="col-md-6 col-lg-4">
            <div class="d-flex gap-2 align-items-start">
                <i class="fas fa-${esc(spec.icon)} text-warning mt-1"></i>
                <div>
                    <div class="fw-semibold">${esc(spec.label)}</div>
                    <div class="text-muted small">${esc(spec.summary)}</div>
                </div>
            </div>
        </div>`).join('');
    return `
        <div class="card border-0 shadow-sm">
            <div class="card-body">
                <h6 class="mb-1">No workers yet</h6>
                <p class="text-muted small mb-3">
                    An automation can only react to something a device did. A worker is the
                    other half: a value you set — holiday mode, the mode the house is in, a
                    countdown — that any rule can then test, and that any rule can set.
                </p>
                <div class="row g-3">${examples}</div>
                ${editable ? `<button class="btn btn-primary btn-sm mt-3" id="worker-empty-new">
                    <i class="fas fa-plus"></i> Create the first one</button>` : ''}
            </div>
        </div>`;
}

function cardHtml(w) {
    const spec = types[w.type] || {};
    const used = usage[w.id] || { triggers: [], targets: [] };
    const ruleCount = new Set([...used.triggers, ...used.targets]).size;
    const editable = canEdit();
    const off = w.type === 'timer' || w.type === 'boolean'
        ? w.state.value !== 'on' : false;

    return `
    <div class="col-md-6 col-xl-4">
      <div class="card h-100 shadow-sm ${w.enabled ? '' : 'opacity-50'}">
        <div class="card-body d-flex flex-column">
          <div class="d-flex align-items-start gap-2 mb-2">
            <i class="fas fa-${esc(w.icon || spec.icon || 'cube')} fa-lg text-warning mt-1"></i>
            <div class="flex-grow-1 min-w-0">
              <div class="fw-semibold text-truncate">${esc(w.name)}</div>
              <div class="text-muted small">
                <span class="badge bg-light text-dark border">${esc(spec.label || w.type)}</span>
                <code class="ms-1 small">${esc(w.ieee)}</code>
              </div>
            </div>
            ${editable ? `
            <div class="dropdown">
              <button class="btn btn-sm btn-link text-muted p-1" data-bs-toggle="dropdown" aria-label="Worker menu">
                <i class="fas fa-ellipsis-v"></i></button>
              <ul class="dropdown-menu dropdown-menu-end">
                <li><button class="dropdown-item" data-worker-action="edit" data-id="${esc(w.id)}">
                    <i class="fas fa-pen fa-fw"></i> Edit</button></li>
                <li><button class="dropdown-item text-danger" data-worker-action="delete" data-id="${esc(w.id)}">
                    <i class="fas fa-trash fa-fw"></i> Delete</button></li>
              </ul>
            </div>` : ''}
          </div>

          ${w.description ? `<div class="text-muted small mb-2">${esc(w.description)}</div>` : ''}

          <div class="d-flex align-items-baseline gap-2 mb-2">
            <span class="fs-4 fw-semibold ${off ? 'text-muted' : 'text-body'}"
                  data-worker-value="${esc(w.id)}">${esc(w.display)}</span>
          </div>

          <div class="mt-auto">${controlsHtml(w)}</div>

          <div class="text-muted small mt-2">
            ${ruleCount
                ? `<i class="fas fa-link"></i> used by ${ruleCount} rule${ruleCount === 1 ? '' : 's'}`
                : '<i class="fas fa-unlink"></i> no rules use this yet'}
          </div>
        </div>
      </div>
    </div>`;
}

/** The per-type control. This is the manual actuation the feature exists for. */
function controlsHtml(w) {
    const id = esc(w.id);
    const btn = (cmd, label, cls = 'btn-outline-secondary', extra = '') =>
        `<button class="btn btn-sm ${cls}" data-worker-action="cmd" data-id="${id}"
                 data-cmd="${cmd}" ${extra}>${label}</button>`;

    switch (w.type) {
        case 'boolean':
            return `<div class="form-check form-switch">
                <input class="form-check-input" type="checkbox" role="switch"
                       data-worker-action="switch" data-id="${id}"
                       ${w.state.value === 'on' ? 'checked' : ''}>
                <label class="form-check-label small text-muted">On</label>
            </div>`;

        case 'mode':
            return `<select class="form-select form-select-sm"
                            data-worker-action="mode" data-id="${id}">
                ${(w.options || []).map(o => `<option value="${esc(o)}"
                    ${o === w.state.value ? 'selected' : ''}>${esc(o)}</option>`).join('')}
            </select>`;

        case 'timer': {
            const mins = Math.max(1, Math.round((w.default_seconds || 3600) / 60));
            return `<div class="input-group input-group-sm">
                <input type="number" class="form-control" min="1" max="10080"
                       value="${mins}" data-worker-input="${id}" aria-label="Minutes">
                <span class="input-group-text">min</span>
                ${btn('start', 'Start', 'btn-outline-primary')}
                ${btn('cancel', 'Cancel')}
            </div>`;
        }

        case 'counter':
            return `<div class="btn-group btn-group-sm w-100">
                ${btn('decrement', '<i class="fas fa-minus"></i>')}
                ${btn('increment', '<i class="fas fa-plus"></i>')}
                ${btn('reset', 'Reset')}
            </div>`;

        case 'marker':
            return `<div class="btn-group btn-group-sm w-100">
                ${btn('mark', '<i class="fas fa-check"></i> Mark now', 'btn-outline-primary')}
                ${btn('reset', 'Clear')}
            </div>`;

        case 'number': {
            const s = w.state.value ?? 0;
            return `<div class="input-group input-group-sm">
                <input type="number" class="form-control" value="${esc(s)}"
                       min="${esc(w.min)}" max="${esc(w.max)}" step="${esc(w.step)}"
                       data-worker-input="${id}" aria-label="${esc(w.name)}">
                ${w.unit ? `<span class="input-group-text">${esc(w.unit)}</span>` : ''}
                ${btn('set', 'Set', 'btn-outline-primary')}
            </div>`;
        }
        default:
            return '';
    }
}

/** Push live state back into the controls, so a rule's change moves them too. */
function syncControls(w) {
    const sel = v => document.querySelector(
        `[data-worker-action="${v}"][data-id="${CSS.escape(w.id)}"]`);
    if (w.type === 'boolean') {
        const el = sel('switch');
        if (el) el.checked = w.state.value === 'on';
    } else if (w.type === 'mode') {
        const el = sel('mode');
        if (el && el.value !== w.state.value) el.value = w.state.value;
    } else if (w.type === 'number') {
        const el = document.querySelector(`[data-worker-input="${CSS.escape(w.id)}"]`);
        // Never overwrite a number somebody is part-way through typing.
        if (el && document.activeElement !== el) el.value = w.state.value;
    }
}

// ACTIONS

function bindAction(el) {
    const action = el.dataset.workerAction;
    const id = el.dataset.id;

    if (action === 'switch') {
        el.addEventListener('change', () => send(id, el.checked ? 'on' : 'off'));
    } else if (action === 'mode') {
        el.addEventListener('change', () => send(id, 'set', el.value));
    } else if (action === 'cmd') {
        el.addEventListener('click', () => withBusy(el, async () => {
            const input = document.querySelector(`[data-worker-input="${CSS.escape(id)}"]`);
            const w = workers.find(x => x.id === id);
            let value = input ? Number(input.value) : null;
            // A timer's control is in minutes because that is how people say it;
            // the API is in seconds because that is what the engine counts.
            if (w && w.type === 'timer' && el.dataset.cmd === 'start') value *= 60;
            await send(id, el.dataset.cmd, input ? value : null);
        }));
    } else if (action === 'edit') {
        el.addEventListener('click', () => openEditor(id));
    } else if (action === 'delete') {
        el.addEventListener('click', () => removeWorker(id));
    }
}

async function send(id, command, value = null) {
    try {
        const res = await api(`/api/workers/${encodeURIComponent(id)}/command`, {
            method: 'POST', body: JSON.stringify({ command, value }),
        });
        const w = workers.find(x => x.id === id);
        if (w && res.state) w.state = res.state;
        await loadWorkersPage();
    } catch (e) {
        showToast(e.message, 'danger');
        await loadWorkersPage();       // put the control back where it was
    }
}

async function removeWorker(id) {
    const w = workers.find(x => x.id === id);
    const used = usage[id] || { triggers: [], targets: [] };
    const count = new Set([...used.triggers, ...used.targets]).size;
    const ok = await confirmDialog({
        title: `Delete ${w ? w.name : id}?`,
        message: count
            ? `${count} rule${count === 1 ? '' : 's'} still use this worker. ` +
              `${count === 1 ? 'It' : 'They'} will stop working until you edit ${count === 1 ? 'it' : 'them'}.`
            : 'Nothing uses this worker.',
        confirmText: 'Delete', variant: 'danger',
    });
    if (!ok) return;
    try {
        await api(`/api/workers/${encodeURIComponent(id)}`, { method: 'DELETE' });
        showToast('Worker deleted', 'success');
        await loadWorkersPage();
    } catch (e) {
        showToast(e.message, 'danger');
    }
}

// EDITOR
//
// The form is generated from the server's type catalogue rather than written
// out per type, so the six types cannot drift apart in the UI and a seventh
// would need no code here.

function ensureModal() {
    let el = document.getElementById('worker-editor-modal');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'worker-editor-modal';
    el.className = 'modal fade';
    el.tabIndex = -1;
    el.innerHTML = `
      <div class="modal-dialog modal-dialog-centered modal-lg">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title"><i class="fas fa-helmet-safety text-warning"></i>
                <span id="worker-modal-title">New worker</span></h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
          </div>
          <div class="modal-body" id="worker-modal-body"></div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
            <button type="button" class="btn btn-primary" id="worker-save-btn">Save</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(el);
    return el;
}

function openEditor(id) {
    editingId = id;
    const w = id ? workers.find(x => x.id === id) : null;
    const el = ensureModal();
    document.getElementById('worker-modal-title').textContent =
        w ? `Edit ${w.name}` : 'New worker';

    const body = document.getElementById('worker-modal-body');
    body.innerHTML = `
        <div class="mb-3">
            <label class="form-label">Name</label>
            <input class="form-control" id="worker-f-name" maxlength="48"
                   value="${esc(w ? w.name : '')}" placeholder="Holiday mode">
        </div>
        <div class="mb-3">
            <label class="form-label">Description <span class="text-muted small">(optional)</span></label>
            <input class="form-control" id="worker-f-description" maxlength="200"
                   value="${esc(w ? (w.description || '') : '')}"
                   placeholder="What this is for">
        </div>
        <div class="mb-3">
            <label class="form-label">Type</label>
            ${w ? `<div class="form-control-plaintext">
                     <i class="fas fa-${esc(types[w.type].icon)} text-warning"></i>
                     ${esc(types[w.type].label)}
                     <span class="text-muted small ms-2">A worker's type cannot change —
                       create a new one instead.</span>
                   </div>`
                : `<div class="row g-2" id="worker-type-picker">
                     ${Object.entries(types).map(([key, spec], i) => `
                       <div class="col-md-6">
                         <label class="border rounded p-2 h-100 d-flex gap-2 worker-type-option ${i === 0 ? 'border-primary' : ''}">
                           <input class="form-check-input mt-1" type="radio" name="worker-type"
                                  value="${esc(key)}" ${i === 0 ? 'checked' : ''}>
                           <span>
                             <span class="fw-semibold">
                               <i class="fas fa-${esc(spec.icon)} text-warning"></i> ${esc(spec.label)}</span>
                             <span class="d-block text-muted small">${esc(spec.summary)}</span>
                           </span>
                         </label>
                       </div>`).join('')}
                   </div>`}
        </div>
        <hr>
        <div id="worker-type-fields"></div>
        <div class="form-check mt-3">
            <input class="form-check-input" type="checkbox" id="worker-f-enabled"
                   ${!w || w.enabled ? 'checked' : ''}>
            <label class="form-check-label" for="worker-f-enabled">
                Enabled <span class="text-muted small">— a disabled worker leaves
                the registry, and rules using it stop firing.</span></label>
        </div>`;

    const renderFields = () => renderTypeFields(currentType(w), w);
    if (!w) {
        body.querySelectorAll('input[name="worker-type"]').forEach(radio => {
            radio.addEventListener('change', () => {
                body.querySelectorAll('.worker-type-option')
                    .forEach(l => l.classList.toggle('border-primary',
                                                     l.contains(document.querySelector('input[name="worker-type"]:checked'))));
                renderFields();
            });
        });
    }
    renderFields();

    const save = document.getElementById('worker-save-btn');
    save.replaceWith(save.cloneNode(true));   // drop the previous handler
    document.getElementById('worker-save-btn')
        .addEventListener('click', () => saveWorker());

    bootstrap.Modal.getOrCreateInstance(el).show();
}

function currentType(w) {
    if (w) return w.type;
    const checked = document.querySelector('input[name="worker-type"]:checked');
    return checked ? checked.value : Object.keys(types)[0];
}

function renderTypeFields(typeKey, w) {
    const host = document.getElementById('worker-type-fields');
    const spec = types[typeKey];
    if (!host || !spec) return;

    const val = key => {
        if (w && w[key] !== undefined && w[key] !== null) return w[key];
        const f = spec.fields.find(x => x.key === key);
        return f ? f.default : '';
    };

    host.innerHTML = spec.fields.map(f => {
        const id = `worker-f-${f.key}`;
        const v = val(f.key);
        if (f.type === 'bool') {
            return `<div class="form-check mb-2">
                <input class="form-check-input" type="checkbox" id="${id}" ${v ? 'checked' : ''}>
                <label class="form-check-label" for="${id}">${esc(f.label)}</label></div>`;
        }
        if (f.type === 'list') {
            const items = Array.isArray(v) ? v : (f.default || []);
            return `<div class="mb-3"><label class="form-label" for="${id}">${esc(f.label)}</label>
                <input class="form-control" id="${id}" value="${esc(items.join(', '))}"
                       placeholder="home, away, night">
                <div class="form-text">Comma separated. Exactly one is ever true at a time —
                    that is what a mode gives you that separate flags cannot.</div></div>`;
        }
        if (f.type === 'select') {
            return `<div class="mb-3"><label class="form-label" for="${id}">${esc(f.label)}</label>
                <select class="form-select" id="${id}">${(f.options || []).map(o =>
                    `<option value="${esc(o)}" ${String(v) === String(o) ? 'selected' : ''}>${esc(o)}</option>`).join('')}
                </select></div>`;
        }
        if (f.type === 'option_ref') {
            // Filled from the options field above, which the user may still be
            // typing, so it is resolved at save time rather than pinned here.
            return '';
        }
        const inputType = f.type === 'time' ? 'time' : (f.type === 'number' ? 'number' : 'text');
        const bounds = f.type === 'number'
            ? `${f.min !== undefined ? `min="${f.min}"` : ''} ${f.max !== undefined ? `max="${f.max}"` : ''}`
            : '';
        return `<div class="mb-3"><label class="form-label" for="${id}">${esc(f.label)}</label>
            <input class="form-control" type="${inputType}" id="${id}" ${bounds}
                   value="${v === null || v === undefined ? '' : esc(v)}"></div>`;
    }).join('');
}

function readField(key, kind) {
    const el = document.getElementById(`worker-f-${key}`);
    if (!el) return undefined;
    if (kind === 'bool') return el.checked;
    if (kind === 'number') return el.value === '' ? undefined : Number(el.value);
    if (kind === 'list') {
        return el.value.split(',').map(s => s.trim()).filter(Boolean);
    }
    return el.value.trim();
}

async function saveWorker() {
    const w = editingId ? workers.find(x => x.id === editingId) : null;
    const typeKey = currentType(w);
    const spec = types[typeKey];

    const payload = {
        name: readField('name') || '',
        description: document.getElementById('worker-f-description').value.trim(),
        enabled: document.getElementById('worker-f-enabled').checked,
    };
    if (!editingId) payload.type = typeKey;
    if (!payload.name) { showToast('A worker needs a name', 'warning'); return; }

    spec.fields.forEach(f => {
        if (f.type === 'option_ref') return;
        const v = readField(f.key, f.type);
        if (v !== undefined) payload[f.key] = v;
    });
    // `initial` for a mode is the first option unless the worker already has a
    // value worth keeping — update() carries the live one across regardless.
    if (typeKey === 'mode' && payload.options && payload.options.length) {
        payload.initial = w && payload.options.includes(w.state.value)
            ? w.state.value : payload.options[0];
    }

    const btn = document.getElementById('worker-save-btn');
    await withBusy(btn, async () => {
        try {
            if (editingId) {
                await api(`/api/workers/${encodeURIComponent(editingId)}`,
                          { method: 'PUT', body: JSON.stringify(payload) });
            } else {
                await api('/api/workers', { method: 'POST', body: JSON.stringify(payload) });
            }
            bootstrap.Modal.getOrCreateInstance(
                document.getElementById('worker-editor-modal')).hide();
            showToast(editingId ? 'Worker updated' : 'Worker created', 'success');
            await loadWorkersPage();
        } catch (e) {
            showToast(e.message, 'danger');
        }
    });
}
