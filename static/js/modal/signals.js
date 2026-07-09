/**
 * Signal Inspector — universal, device-agnostic live signal view.
 * Location: static/js/modal/signals.js
 *
 * Every raw signal a device emits — ZCL attribute reports, cluster commands,
 * Tuya datapoints, Matter attributes and derived state keys — in one live
 * table, whatever the vendor or cluster. This is the surface the future
 * learn-by-demonstration flow plugs into: press a button / turn a knob and
 * watch which signal reacts — that's the address you map.
 *
 * Mounted in two places from one implementation:
 *   • the per-device modal (pinned to the open device), and
 *   • the Debug tab's "Signal Inspector" sub-tab (with a device picker).
 *
 * Usage:
 *   const inspector = createSignalInspector(containerEl, { ieee, showPicker });
 *   inspector.setDevice('00:12:...');   // switch device (picker mode)
 *   inspector.destroy();                // stop streaming + detach
 *
 * Data source: /api/signals/{ieee}  (ieee may be the literal 'all')
 * plus live `signal_inspector_update` WebSocket events.
 */

import { state } from '../state.js';

// Live instances, so the WebSocket dispatcher can fan updates to every mount.
const _instances = new Set();

const SOURCE_LABELS = {
    zcl_attr:    { label: 'ZCL attr',  cls: 'bg-primary'   },
    zcl_cmd:     { label: 'command',   cls: 'bg-warning text-dark' },
    dp:          { label: 'datapoint', cls: 'bg-info text-dark' },
    matter_attr: { label: 'matter',    cls: 'bg-success'   },
    state:       { label: 'state',     cls: 'bg-secondary' },
};

const ALL = 'all';

// ---------------------------------------------------------------------------
// Public factory
// ---------------------------------------------------------------------------

export function createSignalInspector(container, opts = {}) {
    if (!container) return { destroy() {}, setDevice() {} };

    const inst = {
        container,
        ieee: opts.ieee ?? null,       // null = nothing selected yet
        showPicker: !!opts.showPicker,
        rows: new Map(),               // rowKey -> signal
        tick: null,
        lastPulse: null,
        streaming: null,               // ieee currently streamed server-side
        learn: { active: false, timer: null, changes: [], mapped: new Set() },
        mappedView: { open: false, list: [] },
    };

    container.innerHTML = _template(inst);
    _wire(inst);
    _instances.add(inst);

    if (inst.ieee) _select(inst, inst.ieee);
    else _setInfo(inst, 'Pick a device to inspect its live signals.');

    return {
        destroy: () => _destroy(inst),
        setDevice: (ieee) => _select(inst, ieee),
    };
}

/** Live update dispatched from websocket.js on `signal_inspector_update`. */
export function handleSignalUpdate(payload) {
    if (!payload || !payload.signal) return;
    for (const inst of _instances) _applyUpdate(inst, payload);
}

// ---------------------------------------------------------------------------
// Template
// ---------------------------------------------------------------------------

function _template(inst) {
    const picker = inst.showPicker ? `
        <div class="mb-2 d-flex gap-2 align-items-center flex-wrap">
            <label class="small text-muted mb-0">Device</label>
            <select class="form-select form-select-sm sig-device" style="width:auto;max-width:320px">
                <option value="">— select a device —</option>
                ${_deviceOptions()}
            </select>
            <div class="form-check form-check-inline mb-0 ms-1">
                <input class="form-check-input sig-watch-all" type="checkbox" id="sig-watch-${inst._id = (inst._id || Math.round(performance.now()))}">
                <label class="form-check-label small text-muted" for="sig-watch-${inst._id}">Watch all</label>
            </div>
        </div>` : '';

    return `
        ${picker}
        <div class="mb-2 d-flex gap-2 align-items-center flex-wrap">
            <span class="badge bg-danger sig-live" style="display:none">
                <i class="fas fa-circle fa-xs"></i> LIVE
            </span>
            <button class="btn btn-sm btn-primary sig-learn-btn" style="display:none"
                    title="Demonstrate a control on the device and map what changed">
                <i class="fas fa-graduation-cap"></i> Learn a control
            </button>
            <button class="btn btn-sm btn-outline-secondary sig-mapped-btn" style="display:none"
                    title="Review, rename and remove what you've mapped">
                <i class="fas fa-list-check"></i> Mapped <span class="badge bg-secondary sig-mapped-count" style="display:none"></span>
            </button>
            <input type="text" class="form-control form-control-sm sig-filter"
                   placeholder="Filter by name / address…" style="width:200px">
            <div class="form-check form-check-inline mb-0 ms-1">
                <input class="form-check-input sig-changed-only" type="checkbox">
                <label class="form-check-label small text-muted">Recently changed</label>
            </div>
            <button class="btn btn-sm btn-outline-secondary ms-auto sig-clear"
                    title="Clear recorded signals — use before demonstrating a control">
                <i class="fas fa-eraser"></i> Reset
            </button>
            <span class="small text-muted sig-meta"></span>
        </div>
        <div class="sig-learn card border-primary mb-2" style="display:none"></div>
        <div class="sig-mapped card border-secondary mb-2" style="display:none"></div>
        <div class="small text-muted mb-2">
            Interact with the device — press a button, turn a knob, change a setting —
            and watch which signal reacts. That's the address you map.
        </div>
        <div class="table-responsive" style="max-height:440px">
            <table class="table table-sm table-hover tbl mb-0" style="font-size:.82rem">
                <thead style="position:sticky;top:0;z-index:1">
                    <tr>
                        <th class="sig-col-device" style="display:none">Device</th>
                        <th>Signal</th>
                        <th>Source</th>
                        <th>Address</th>
                        <th class="text-end">Value</th>
                        <th class="text-end" title="Updates per minute">Rate</th>
                        <th class="text-end" title="Total updates observed">#</th>
                        <th class="text-end">Age</th>
                    </tr>
                </thead>
                <tbody class="sig-tbody">
                    <tr><td colspan="8" class="text-muted text-center py-4">—</td></tr>
                </tbody>
            </table>
        </div>
    `;
}

function _deviceOptions() {
    const devs = (state.devices || [])
        .map(d => ({ ieee: d.ieee, name: d.friendly_name || d.ieee }))
        .filter(d => d.ieee)
        .sort((a, b) => a.name.localeCompare(b.name));
    return devs.map(d =>
        `<option value="${_esc(d.ieee)}">${_esc(d.name)}</option>`).join('');
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function _wire(inst) {
    const $ = (sel) => inst.container.querySelector(sel);
    $('.sig-filter')?.addEventListener('input', () => _repaint(inst));
    $('.sig-changed-only')?.addEventListener('change', () => _repaint(inst));
    $('.sig-clear')?.addEventListener('click', () => _clear(inst));

    const dev = $('.sig-device');
    if (dev) {
        dev.addEventListener('change', () => {
            const watch = $('.sig-watch-all');
            if (watch) watch.checked = false;
            _select(inst, dev.value || null);
        });
    }
    const watch = $('.sig-watch-all');
    if (watch) {
        watch.addEventListener('change', () => {
            if (watch.checked) {
                if (dev) dev.value = '';
                _select(inst, ALL);
            } else {
                _select(inst, null);
            }
        });
    }
    $('.sig-learn-btn')?.addEventListener('click', () => _learnStart(inst));
    $('.sig-mapped-btn')?.addEventListener('click', () => _mappedToggle(inst));
}

// ---------------------------------------------------------------------------
// Device selection + streaming
// ---------------------------------------------------------------------------

async function _select(inst, ieee) {
    // Tear down any current stream + learn session first.
    _learnStop(inst);
    await _stopStream(inst);
    inst.ieee = ieee;
    inst.rows = new Map();

    const isAll = ieee === ALL;
    inst.container.querySelector('.sig-col-device').style.display = isAll ? '' : 'none';

    // Learn / Mapped only make sense for a single, specific device.
    const specific = ieee && !isAll;
    const learnBtn = inst.container.querySelector('.sig-learn-btn');
    if (learnBtn) learnBtn.style.display = specific ? '' : 'none';
    const mappedBtn = inst.container.querySelector('.sig-mapped-btn');
    if (mappedBtn) mappedBtn.style.display = specific ? '' : 'none';
    _mappedClose(inst);

    if (!ieee) {
        _setLive(inst, false);
        _setInfo(inst, 'Pick a device to inspect its live signals.');
        return;
    }
    await _startStream(inst, ieee);
    if (specific) _refreshMappedCount(inst);
}

async function _startStream(inst, ieee) {
    _setInfo(inst, 'Starting inspection…');
    try {
        const res = await fetch(`/api/signals/${encodeURIComponent(ieee)}/start`, { method: 'POST' });
        const json = await res.json();
        if (!json.success) { _setInfo(inst, json.error || 'Could not start inspection'); return; }
        inst.streaming = ieee;
        (json.signals || []).forEach(s => inst.rows.set(_rowKey(inst, s), s));
        _setLive(inst, true);
        _repaint(inst);
        if (inst.tick) clearInterval(inst.tick);
        inst.tick = setInterval(() => _repaint(inst), 1000);
    } catch (e) {
        _setInfo(inst, `Failed to start: ${e.message}`);
    }
}

async function _stopStream(inst) {
    if (inst.tick) { clearInterval(inst.tick); inst.tick = null; }
    _setLive(inst, false);
    const ieee = inst.streaming;
    inst.streaming = null;
    if (!ieee) return;
    try {
        await fetch(`/api/signals/${encodeURIComponent(ieee)}/stop`, { method: 'POST' });
    } catch (_) { /* ignore */ }
}

function _destroy(inst) {
    _learnStop(inst);
    _stopStream(inst);
    _instances.delete(inst);
}

async function _clear(inst) {
    const ieee = inst.streaming || inst.ieee;
    if (ieee) {
        try { await fetch(`/api/signals/${encodeURIComponent(ieee)}/clear`, { method: 'POST' }); }
        catch (_) { /* ignore */ }
    }
    inst.rows.clear();
    _repaint(inst);
}

// ---------------------------------------------------------------------------
// Live updates + rendering
// ---------------------------------------------------------------------------

function _applyUpdate(inst, payload) {
    if (!inst.streaming) return;
    const isAll = inst.ieee === ALL;
    if (!isAll && payload.ieee !== inst.ieee) return;
    const sig = { ...payload.signal, ieee: payload.ieee };
    const key = _rowKey(inst, sig);
    inst.rows.set(key, sig);
    inst.lastPulse = key;
    _repaint(inst);
}

function _rowKey(inst, sig) {
    return (inst.ieee === ALL) ? `${sig.ieee}::${sig.key}` : sig.key;
}

function _repaint(inst) {
    const tb = inst.container.querySelector('.sig-tbody');
    if (!tb) return;
    const isAll = inst.ieee === ALL;

    const filter = (inst.container.querySelector('.sig-filter')?.value || '').toLowerCase();
    const changedOnly = inst.container.querySelector('.sig-changed-only')?.checked;

    let rows = [...inst.rows.values()];
    if (filter) {
        rows = rows.filter(s =>
            (s.name || '').toLowerCase().includes(filter) ||
            (s.address || '').toLowerCase().includes(filter));
    }
    if (changedOnly) rows = rows.filter(s => (s.since_change_s ?? 999) < 15);
    rows.sort((a, b) => (a.last_seen < b.last_seen ? 1 : -1));

    const meta = inst.container.querySelector('.sig-meta');
    if (meta) meta.textContent = `${inst.rows.size} signal${inst.rows.size === 1 ? '' : 's'}`;

    if (!rows.length) {
        _setInfo(inst, inst.rows.size
            ? 'No signals match the filter.'
            : 'No signals yet — interact with the device to make it report.');
        return;
    }

    tb.innerHTML = rows.map(s => {
        const src = SOURCE_LABELS[s.source] || { label: s.source, cls: 'bg-secondary' };
        const fresh = (s.since_change_s ?? 999) < 2;
        const recent = (s.age_s ?? 999) < 3;
        const pulse = (_rowKey(inst, s) === inst.lastPulse) ? 'sig-pulse' : '';
        const devCell = isAll
            ? `<td class="small text-truncate" style="max-width:130px">${_esc(_deviceName(s.ieee))}</td>`
            : '';
        return `
            <tr class="${pulse}">
                ${devCell}
                <td class="font-monospace">${_esc(s.name)}</td>
                <td><span class="badge ${src.cls}" style="font-weight:500">${src.label}</span></td>
                <td class="font-monospace text-muted small">${_esc(s.address)}</td>
                <td class="text-end font-monospace ${fresh ? 'fw-bold text-success' : ''}">${_fmtVal(s.value)}</td>
                <td class="text-end text-muted">${s.rate_per_min ? s.rate_per_min.toFixed(1) : '·'}</td>
                <td class="text-end text-muted">${s.count}</td>
                <td class="text-end small ${recent ? 'text-success' : 'text-muted'}">${_fmtAge(s.age_s)}</td>
            </tr>`;
    }).join('');
    inst.lastPulse = null;
}

// ---------------------------------------------------------------------------
// Learn-by-demonstration
// ---------------------------------------------------------------------------

// Value-bearing sources that resolve to a device state key we can map.
const MAPPABLE_SOURCES = new Set(['state', 'dp', 'zcl_attr', 'matter_attr']);
const DEVICE_CLASSES = [
    '', 'temperature', 'humidity', 'battery', 'illuminance', 'power', 'energy',
    'voltage', 'current', 'pressure', 'occupancy', 'motion', 'contact',
    'door', 'window', 'signal_strength',
];

function _learnPanel(inst) { return inst.container.querySelector('.sig-learn'); }

function _learnStart(inst) {
    if (!inst.ieee || inst.ieee === ALL) return;
    inst.learn.active = true;
    inst.learn.changes = [];
    _learnPanel(inst).style.display = '';
    _renderLearnStep(inst, 'ready');
}

function _learnStop(inst) {
    if (inst.learn) {
        if (inst.learn.timer) { clearInterval(inst.learn.timer); inst.learn.timer = null; }
        inst.learn.active = false;
    }
    const panel = _learnPanel(inst);
    if (panel) { panel.style.display = 'none'; panel.innerHTML = ''; }
}

async function _learnBaseline(inst) {
    _renderLearnStep(inst, 'capturing');
    try {
        const res = await fetch(`/api/signals/${encodeURIComponent(inst.ieee)}/learn/baseline`, { method: 'POST' });
        const json = await res.json();
        if (!json.success) { _renderLearnStep(inst, 'ready', json.error); return; }
        _renderLearnStep(inst, 'demonstrate');
        if (inst.learn.timer) clearInterval(inst.learn.timer);
        inst.learn.timer = setInterval(() => _learnPoll(inst), 1500);
        _learnPoll(inst);
    } catch (e) {
        _renderLearnStep(inst, 'ready', e.message);
    }
}

async function _learnPoll(inst) {
    if (!inst.learn.active) return;
    try {
        const res = await fetch(`/api/signals/${encodeURIComponent(inst.ieee)}/learn/diff`);
        const json = await res.json();
        if (json.success) { inst.learn.changes = json.changes || []; _renderChanges(inst); }
    } catch (_) { /* ignore */ }
}

function _renderLearnStep(inst, step, err) {
    const panel = _learnPanel(inst);
    if (!panel) return;
    const errHtml = err ? `<div class="alert alert-warning py-1 px-2 small mb-2">${_esc(err)}</div>` : '';

    if (step === 'ready') {
        panel.innerHTML = `
            <div class="card-body p-2">
                <div class="d-flex justify-content-between align-items-center mb-1">
                    <strong><i class="fas fa-graduation-cap"></i> Learn a control</strong>
                    <button class="btn btn-sm btn-outline-secondary sig-learn-done">Cancel</button>
                </div>
                ${errHtml}
                <div class="small text-muted mb-2">
                    We'll snapshot the device now, then you operate it — press the button,
                    turn the knob, change the setting. We'll show exactly what changed so you can name it.
                </div>
                <button class="btn btn-sm btn-primary sig-learn-baseline">
                    <i class="fas fa-camera"></i> Start — capture baseline
                </button>
            </div>`;
    } else if (step === 'capturing') {
        panel.innerHTML = `<div class="card-body p-2 small text-muted">
            <i class="fas fa-spinner fa-spin"></i> Capturing baseline…</div>`;
    } else if (step === 'demonstrate') {
        panel.innerHTML = `
            <div class="card-body p-2">
                <div class="d-flex justify-content-between align-items-center mb-1">
                    <strong><i class="fas fa-hand-pointer"></i> Operate the device now</strong>
                    <button class="btn btn-sm btn-outline-secondary sig-learn-done">Done</button>
                </div>
                <div class="small text-muted mb-2">
                    Press the button / turn the knob / change the setting. Then click a
                    changed signal below to give it a friendly name.
                </div>
                <div class="sig-learn-changes"></div>
                <div class="sig-learn-form"></div>
            </div>`;
        _renderChanges(inst);
    }

    panel.querySelector('.sig-learn-done')?.addEventListener('click', () => _learnStop(inst));
    panel.querySelector('.sig-learn-baseline')?.addEventListener('click', () => _learnBaseline(inst));
}

function _renderChanges(inst) {
    const wrap = inst.container.querySelector('.sig-learn-changes');
    if (!wrap) return;
    const changes = inst.learn.changes;

    if (!changes.length) {
        wrap.innerHTML = `<div class="small text-muted fst-italic">Waiting for a reaction…</div>`;
        return;
    }

    const badge = { changed: 'bg-success', new: 'bg-primary', repeated: 'bg-secondary' };
    wrap.innerHTML = changes.map((c, i) => {
        const isCmd = c.source === 'zcl_cmd';
        const mappable = MAPPABLE_SOURCES.has(c.source) || isCmd;
        const mapped = _isMapped(inst, c);
        const before = (c.baseline_value === null || c.baseline_value === undefined)
            ? '—' : _fmtVal(c.baseline_value);
        let cta;
        if (mapped) cta = '<span class="badge bg-success"><i class="fas fa-check"></i> mapped</span>';
        else if (isCmd) cta = '<button class="btn btn-sm btn-outline-warning py-0 sig-map-btn">Name action</button>';
        else if (mappable) cta = '<button class="btn btn-sm btn-outline-primary py-0 sig-map-btn">Name it</button>';
        else cta = '';
        return `
            <div class="d-flex align-items-center gap-2 py-1 border-bottom sig-change-row"
                 data-idx="${i}" style="cursor:${mappable ? 'pointer' : 'default'}">
                <span class="badge ${badge[c.change] || 'bg-secondary'}" style="min-width:64px">${c.change}</span>
                <span class="font-monospace small">${_esc(c.name)}</span>
                <span class="text-muted small">${_esc(c.address)}</span>
                <span class="ms-auto font-monospace small">${before} → <strong>${_fmtVal(c.value)}</strong></span>
                ${cta}
            </div>`;
    }).join('');

    wrap.querySelectorAll('.sig-change-row').forEach(row => {
        row.addEventListener('click', () => {
            const c = changes[parseInt(row.dataset.idx, 10)];
            if (!c || _isMapped(inst, c)) return;
            if (c.source === 'zcl_cmd') _openActionForm(inst, c);
            else if (MAPPABLE_SOURCES.has(c.source)) _openLabelForm(inst, c);
        });
    });
}

// A command can carry several payload-specific mappings (single/double/hold),
// so check both the exact-payload key and the any-payload (wildcard) key.
function _isMapped(inst, c) {
    const m = inst.learn.mapped;
    if (c.source === 'zcl_cmd') {
        const base = `cmd:${c.endpoint}/${_hexToInt(c.cluster)}/${c.item}`;
        if (m.has(base)) return true;
        return !!(c.arg_disc && m.has(`${base}/${c.arg_disc}`));
    }
    return m.has(c.name);
}

function _hexToInt(v) {
    if (typeof v === 'number') return v;
    const s = String(v);
    return s.startsWith('0x') ? parseInt(s, 16) : parseInt(s, 10);
}

function _openActionForm(inst, change) {
    const form = inst.container.querySelector('.sig-learn-form');
    if (!form) return;
    const hasPayload = !!change.arg_disc;
    const payloadHint = hasPayload
        ? `<div class="form-check mb-1">
               <input class="form-check-input sig-a-match" type="checkbox" id="sig-match" checked>
               <label class="form-check-label small" for="sig-match">
                   Only this exact press
                   <span class="text-muted font-monospace">${_esc(change.arg_summary || change.arg_disc)}</span>
               </label>
           </div>
           <div class="small text-muted mb-1">Distinguishes e.g. single / double / hold on the same command.</div>`
        : '';
    form.innerHTML = `
        <div class="card card-body p-2 mt-2 bg-body-tertiary">
            <div class="small mb-2">Name the action for <span class="font-monospace">${_esc(change.name)}</span>
                <span class="text-muted">(${_esc(change.address)})</span></div>
            ${payloadHint}
            <div class="d-flex align-items-center gap-2 flex-wrap">
                <input type="text" class="form-control form-control-sm sig-a-name"
                       placeholder="e.g. button_single" style="max-width:240px">
                <span class="small text-muted">→ published as <span class="font-monospace">action</span></span>
                <button class="btn btn-sm btn-outline-secondary ms-auto sig-a-cancel">Cancel</button>
                <button class="btn btn-sm btn-primary sig-a-save">Save action</button>
            </div>
            <div class="small text-muted mt-1">
                Automations can trigger on <span class="font-monospace">action = &lt;name&gt;</span>.
                Press the button again afterwards to confirm it fires.
            </div>
        </div>`;
    form.querySelector('.sig-a-name')?.focus();
    form.querySelector('.sig-a-cancel')?.addEventListener('click', () => { form.innerHTML = ''; });
    form.querySelector('.sig-a-save')?.addEventListener('click', () => _saveActionMapping(inst, change, form));
}

async function _saveActionMapping(inst, change, form) {
    const nameEl = form.querySelector('.sig-a-name');
    const name = nameEl.value.trim();
    if (!name) { nameEl.classList.add('is-invalid'); return; }
    const matchArgs = !!form.querySelector('.sig-a-match')?.checked;
    const btn = form.querySelector('.sig-a-save');
    btn.disabled = true; btn.textContent = 'Saving…';
    try {
        const res = await fetch(`/api/signals/${encodeURIComponent(inst.ieee)}/learn/map`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                command: true, endpoint: change.endpoint,
                cluster: change.cluster, item: change.item, friendly_name: name,
                match_args: matchArgs, arg_disc: change.arg_disc || '',
            }),
        });
        const json = await res.json();
        if (!json.success) { _toast('error', json.error || 'Failed'); btn.disabled = false; btn.textContent = 'Save action'; return; }
        // Track the exact key the backend stored so the row reflects it.
        if (json.raw_key) inst.learn.mapped.add(json.raw_key);
        _toast('success', `Action “${name}” mapped`);
        form.innerHTML = '';
        _renderChanges(inst);
        _refreshMappedCount(inst);
    } catch (e) {
        _toast('error', e.message);
        btn.disabled = false; btn.textContent = 'Save action';
    }
}

function _openLabelForm(inst, change) {
    const form = inst.container.querySelector('.sig-learn-form');
    if (!form) return;
    const guessName = /^(dp_\d+|attr_[0-9a-f]|0x|cluster_)/i.test(change.name) ? '' : change.name;

    form.innerHTML = `
        <div class="card card-body p-2 mt-2 bg-body-tertiary">
            <div class="small mb-2">Map <span class="font-monospace">${_esc(change.name)}</span>
                <span class="text-muted">(${_esc(change.address)})</span></div>
            <div class="row g-2">
                <div class="col-md-4">
                    <label class="form-label small mb-0">Friendly name</label>
                    <input type="text" class="form-control form-control-sm sig-f-name" value="${_esc(guessName)}" placeholder="e.g. heating_setpoint">
                </div>
                <div class="col-md-2">
                    <label class="form-label small mb-0">Unit</label>
                    <input type="text" class="form-control form-control-sm sig-f-unit" placeholder="°C">
                </div>
                <div class="col-md-2">
                    <label class="form-label small mb-0" title="Raw value is divided by this">Divide by</label>
                    <input type="number" step="any" class="form-control form-control-sm sig-f-scale" value="1">
                </div>
                <div class="col-md-4">
                    <label class="form-label small mb-0">Device class</label>
                    <select class="form-select form-select-sm sig-f-class">
                        ${DEVICE_CLASSES.map(d => `<option value="${d}">${d || '(none)'}</option>`).join('')}
                    </select>
                </div>
            </div>
            <div class="d-flex align-items-center gap-2 mt-2">
                <div class="form-check mb-0">
                    <input class="form-check-input sig-f-invert" type="checkbox" id="sig-invert">
                    <label class="form-check-label small" for="sig-invert">Invert (0↔1 boolean)</label>
                </div>
                <div class="ms-auto small text-muted sig-f-preview"></div>
                <button class="btn btn-sm btn-outline-secondary sig-f-cancel">Cancel</button>
                <button class="btn btn-sm btn-primary sig-f-save">Save mapping</button>
            </div>
        </div>`;

    const nameEl = form.querySelector('.sig-f-name');
    nameEl?.focus();
    const preview = () => {
        const scale = parseFloat(form.querySelector('.sig-f-scale').value) || 1;
        const raw = Number(change.value);
        const el = form.querySelector('.sig-f-preview');
        if (!isNaN(raw) && scale && scale !== 1) el.textContent = `preview: ${change.value} → ${(raw / scale)}`;
        else el.textContent = '';
    };
    form.querySelector('.sig-f-scale')?.addEventListener('input', preview);

    form.querySelector('.sig-f-cancel')?.addEventListener('click', () => { form.innerHTML = ''; });
    form.querySelector('.sig-f-save')?.addEventListener('click', () => _saveMapping(inst, change, form));
}

async function _saveMapping(inst, change, form) {
    const friendly = form.querySelector('.sig-f-name').value.trim();
    if (!friendly) { form.querySelector('.sig-f-name').classList.add('is-invalid'); return; }
    const body = {
        state_key:    change.name,
        friendly_name: friendly,
        unit:         form.querySelector('.sig-f-unit').value.trim(),
        scale:        parseFloat(form.querySelector('.sig-f-scale').value) || 1,
        device_class: form.querySelector('.sig-f-class').value,
        invert:       form.querySelector('.sig-f-invert').checked,
    };
    const saveBtn = form.querySelector('.sig-f-save');
    saveBtn.disabled = true; saveBtn.textContent = 'Saving…';
    try {
        const res = await fetch(`/api/signals/${encodeURIComponent(inst.ieee)}/learn/map`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        const json = await res.json();
        if (!json.success) { _toast('error', json.error || 'Mapping failed'); saveBtn.disabled = false; saveBtn.textContent = 'Save mapping'; return; }
        inst.learn.mapped.add(change.name);
        _toast('success', `Mapped “${friendly}”`);
        form.innerHTML = '';
        _renderChanges(inst);
        _refreshMappedCount(inst);
    } catch (e) {
        _toast('error', e.message);
        saveBtn.disabled = false; saveBtn.textContent = 'Save mapping';
    }
}

function _toast(type, msg) {
    try { if (window.toast && window.toast[type]) window.toast[type](msg); } catch (_) { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Mapped-signals management
// ---------------------------------------------------------------------------

function _mappedPanel(inst) { return inst.container.querySelector('.sig-mapped'); }

function _mappedClose(inst) {
    if (inst.mappedView) inst.mappedView.open = false;
    const p = _mappedPanel(inst);
    if (p) { p.style.display = 'none'; p.innerHTML = ''; }
}

function _mappedToggle(inst) {
    if (inst.mappedView.open) { _mappedClose(inst); return; }
    _mappedLoad(inst);
}

function _updateMappedCount(inst, n) {
    const b = inst.container.querySelector('.sig-mapped-count');
    if (b) { b.textContent = n; b.style.display = n ? '' : 'none'; }
}

async function _refreshMappedCount(inst) {
    if (!inst.ieee || inst.ieee === ALL) return;
    try {
        const res = await fetch(`/api/signals/${encodeURIComponent(inst.ieee)}/mappings`);
        const j = await res.json();
        inst.mappedView.list = j.mappings || [];
        _updateMappedCount(inst, j.count || 0);
        if (inst.mappedView.open) _mappedRender(inst);
    } catch (_) { /* ignore */ }
}

async function _mappedLoad(inst) {
    const p = _mappedPanel(inst);
    if (!p) return;
    p.style.display = ''; inst.mappedView.open = true;
    p.innerHTML = `<div class="card-body p-2 small text-muted"><i class="fas fa-spinner fa-spin"></i> Loading…</div>`;
    try {
        const res = await fetch(`/api/signals/${encodeURIComponent(inst.ieee)}/mappings`);
        const j = await res.json();
        inst.mappedView.list = j.mappings || [];
        _updateMappedCount(inst, j.count || 0);
        _mappedRender(inst);
    } catch (e) {
        p.innerHTML = `<div class="card-body p-2 small text-danger">Failed: ${_esc(e.message)}</div>`;
    }
}

function _mappedRender(inst) {
    const p = _mappedPanel(inst);
    if (!p) return;
    const list = inst.mappedView.list;
    const rows = list.length
        ? list.map((e, i) => _mappedRow(e, i)).join('')
        : `<div class="small text-muted fst-italic px-1 py-2">Nothing mapped yet — use “Learn a control”.</div>`;
    p.innerHTML = `
        <div class="card-body p-2">
            <div class="d-flex justify-content-between align-items-center mb-2">
                <strong><i class="fas fa-list-check"></i> Mapped signals (${list.length})</strong>
                <button class="btn btn-sm btn-outline-secondary sig-m-close">Close</button>
            </div>
            <div class="sig-m-rows">${rows}</div>
        </div>`;
    p.querySelector('.sig-m-close')?.addEventListener('click', () => _mappedClose(inst));
    p.querySelectorAll('.sig-m-row').forEach(row => {
        const i = parseInt(row.dataset.idx, 10);
        row.querySelector('.sig-m-edit')?.addEventListener('click', () => _mappedEdit(inst, i));
        row.querySelector('.sig-m-del')?.addEventListener('click', () => _mappedRemove(inst, i));
    });
}

function _mappedRow(e, i) {
    const kindBadge = { value: 'bg-primary', action: 'bg-warning text-dark', attribute: 'bg-info text-dark' }[e.kind] || 'bg-secondary';
    const detail = e.kind === 'action'
        ? `<span class="text-muted small">${_esc(e.label || '')}</span>`
        : `<span class="text-muted small">from <span class="font-monospace">${_esc(e.source_key || e.label || '')}</span>`
            + `${e.unit ? ` · ${_esc(e.unit)}` : ''}`
            + `${(e.scale && e.scale != 1) ? ` · ÷${_esc(String(e.scale))}` : ''}`
            + `${(e.current !== null && e.current !== undefined) ? ` · now <strong>${_esc(String(e.current))}</strong>` : ''}</span>`;
    return `
        <div class="d-flex align-items-center gap-2 py-1 border-bottom sig-m-row" data-idx="${i}">
            <span class="badge ${kindBadge}">${_esc(e.kind)}</span>
            <span class="font-monospace">${_esc(e.friendly_name)}</span>
            ${detail}
            <span class="ms-auto"></span>
            <button class="btn btn-sm btn-outline-secondary py-0 sig-m-edit">Edit</button>
            <button class="btn btn-sm btn-outline-danger py-0 sig-m-del">Remove</button>
        </div>
        <div class="sig-m-editrow" data-idx="${i}"></div>`;
}

function _mappedEdit(inst, i) {
    const e = inst.mappedView.list[i];
    const holder = _mappedPanel(inst).querySelector(`.sig-m-editrow[data-idx="${i}"]`);
    if (!holder) return;
    if (holder.innerHTML) { holder.innerHTML = ''; return; }   // toggle closed
    const isValue = e.kind !== 'action';
    holder.innerHTML = `
        <div class="card card-body p-2 my-1 bg-body-tertiary">
            <div class="row g-2">
                <div class="col-md-4">
                    <label class="form-label small mb-0">Name</label>
                    <input class="form-control form-control-sm sig-me-name" value="${_esc(e.friendly_name)}">
                </div>
                ${isValue ? `
                <div class="col-md-2">
                    <label class="form-label small mb-0">Unit</label>
                    <input class="form-control form-control-sm sig-me-unit" value="${_esc(e.unit || '')}">
                </div>
                <div class="col-md-2">
                    <label class="form-label small mb-0">Divide by</label>
                    <input type="number" step="any" class="form-control form-control-sm sig-me-scale" value="${_esc(String(e.scale || 1))}">
                </div>
                <div class="col-md-4">
                    <label class="form-label small mb-0">Device class</label>
                    <select class="form-select form-select-sm sig-me-class">
                        ${DEVICE_CLASSES.map(d => `<option value="${d}" ${d === e.device_class ? 'selected' : ''}>${d || '(none)'}</option>`).join('')}
                    </select>
                </div>` : ''}
            </div>
            <div class="d-flex align-items-center gap-2 mt-2">
                ${isValue ? `<div class="form-check mb-0">
                    <input class="form-check-input sig-me-invert" type="checkbox" ${e.invert ? 'checked' : ''} id="sig-me-inv-${i}">
                    <label class="form-check-label small" for="sig-me-inv-${i}">Invert</label>
                </div>` : ''}
                <button class="btn btn-sm btn-outline-secondary ms-auto sig-me-cancel">Cancel</button>
                <button class="btn btn-sm btn-primary sig-me-save">Save</button>
            </div>
        </div>`;
    holder.querySelector('.sig-me-cancel')?.addEventListener('click', () => { holder.innerHTML = ''; });
    holder.querySelector('.sig-me-save')?.addEventListener('click', () => _mappedSaveEdit(inst, i, holder));
}

async function _mappedSaveEdit(inst, i, holder) {
    const e = inst.mappedView.list[i];
    const nameEl = holder.querySelector('.sig-me-name');
    const name = nameEl.value.trim();
    if (!name) { nameEl.classList.add('is-invalid'); return; }
    const body = { raw_key: e.raw_key, friendly_name: name };
    if (e.kind !== 'action') {
        body.unit = holder.querySelector('.sig-me-unit')?.value.trim() || '';
        body.scale = parseFloat(holder.querySelector('.sig-me-scale')?.value) || 1;
        body.device_class = holder.querySelector('.sig-me-class')?.value || '';
        body.invert = !!holder.querySelector('.sig-me-invert')?.checked;
    }
    const btn = holder.querySelector('.sig-me-save');
    btn.disabled = true; btn.textContent = 'Saving…';
    try {
        const res = await fetch(`/api/signals/${encodeURIComponent(inst.ieee)}/mappings/update`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        const j = await res.json();
        if (!j.success) { _toast('error', j.error || 'Failed'); btn.disabled = false; btn.textContent = 'Save'; return; }
        _toast('success', 'Mapping updated');
        _mappedLoad(inst);
    } catch (err) {
        _toast('error', err.message); btn.disabled = false; btn.textContent = 'Save';
    }
}

async function _mappedRemove(inst, i) {
    const e = inst.mappedView.list[i];
    if (!window.confirm(`Remove mapping “${e.friendly_name}”?`)) return;
    try {
        const res = await fetch(`/api/signals/${encodeURIComponent(inst.ieee)}/learn/unmap`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ raw_key: e.raw_key }),
        });
        const j = await res.json();
        if (!j.success) { _toast('error', j.error || 'Failed'); return; }
        _toast('success', 'Mapping removed');
        _mappedLoad(inst);
    } catch (err) {
        _toast('error', err.message);
    }
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function _setLive(inst, on) {
    const badge = inst.container.querySelector('.sig-live');
    if (badge) badge.style.display = on ? '' : 'none';
}

function _setInfo(inst, msg) {
    const tb = inst.container.querySelector('.sig-tbody');
    if (tb) tb.innerHTML = `<tr><td colspan="8" class="text-muted text-center py-4">${_esc(msg)}</td></tr>`;
}

function _deviceName(ieee) {
    const d = (state.devices || []).find(x => x.ieee === ieee)
        || state.deviceCache?.[ieee];
    return d?.friendly_name || ieee;
}

function _fmtVal(v) {
    if (v === null || v === undefined) return '<span class="text-muted">—</span>';
    if (typeof v === 'object') v = JSON.stringify(v);
    const s = String(v);
    return _esc(s.length > 48 ? s.slice(0, 48) + '…' : s);
}

function _fmtAge(sec) {
    if (sec === null || sec === undefined) return '—';
    if (sec < 1) return 'now';
    if (sec < 60) return `${Math.round(sec)}s`;
    if (sec < 3600) return `${Math.round(sec / 60)}m`;
    return `${Math.round(sec / 3600)}h`;
}

function _esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
