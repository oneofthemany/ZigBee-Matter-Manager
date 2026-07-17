/**
 * Frames — dynamically-generated dashboards.
 *
 * Hive = the install. Frame = a dashboard. Chamber = a room. Cell = a device tile.
 *
 * Structure (which cell kind, which quick actions, which readouts) comes from
 * /api/frames/auto. Live VALUES come from state.deviceCache, which the existing
 * websocket already keeps current — so a state change re-renders one cell and
 * never refetches the layout.
 *
 * Quick actions go through window.sendCommand (actions.js), which already does
 * the optimistic update. Frames does not open a second command path.
 *
 * Zigbee-only for now — the backend excludes everything else.
 */

import { state } from './state.js';

const log = zmmLog('frames');

let frame = null;
let split = 'chamber';
let loading = false;

/** What's on screen: an auto layout, or a saved frame. */
let current = { type: 'auto', split: 'chamber' };
let savedFrames = [];
let allChambers = [];
let allKinds = [];

/** Builder working copy — only live while the modal is open. */
let draft = null;

/** Active frame tab id, or null when the frame has no tabs. */
let activeTab = null;

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

const KIND_ICONS = {
    light: 'fa-lightbulb',
    switch: 'fa-plug',
    cover: 'fa-arrows-up-down',
    climate: 'fa-temperature-half',
    lock: 'fa-lock',
    sensor: 'fa-wave-square',
    unknown: 'fa-circle-question',
};

const READOUT_ICONS = {
    contact: 'fa-door-closed',
    motion: 'fa-person-running',
    leak: 'fa-droplet',
    smoke: 'fa-fire',
    vibration: 'fa-wave-square',
    temperature: 'fa-temperature-half',
    humidity: 'fa-droplet',
    illuminance: 'fa-sun',
    pressure: 'fa-gauge',
    co2: 'fa-wind',
    power: 'fa-bolt',
    energy: 'fa-gauge-high',
};

// ── state helpers (match the conventions in modal/control.js) ────────

function devOf(cell) {
    return state.deviceCache?.[cell.ieee] || null;
}

function stateOf(cell) {
    return devOf(cell)?.state || {};
}

/** on/off, endpoint-aware. Mirrors modal/control.js:355. */
function isOn(cell, ep = 1) {
    const s = stateOf(cell);
    let v = s[`on_${ep}`];
    if (v === undefined && ep === 1) v = s.on;
    if (v !== undefined) return v === true || v === 1 || v === 'ON';
    if (ep === 1 && s.state !== undefined) return s.state === 'ON' || s.state === true;
    return false;
}

function brightnessOf(cell, ep = 1) {
    const s = stateOf(cell);
    const v = s[`brightness_${ep}`] !== undefined ? s[`brightness_${ep}`] : (ep === 1 ? s.brightness : undefined);
    return typeof v === 'number' ? Math.round(v) : 0;
}

function positionOf(cell) {
    const s = stateOf(cell);
    return typeof s.position === 'number' ? Math.round(s.position) : 50;
}

function setpointOf(cell) {
    const s = stateOf(cell);
    const v = s.occupied_heating_setpoint ?? s.heating_setpoint ?? s.target_temp;
    return typeof v === 'number' ? v : null;
}

// ── readout formatting ──────────────────────────────────────────────

const NUM_UNITS = {
    temperature: '°C',
    humidity: '%',
    illuminance: ' lx',
    pressure: ' hPa',
    co2: ' ppm',
    power: ' W',
    energy: ' kWh',
};

/**
 * Render one readout.
 *
 * Contact is the subtle one: `contact: true` means CLOSED, and `is_open` is its
 * inverse — so the semantics live on the KEY, not the kind. Same expression as
 * device-status.js:393 and handlers/security.py:171.
 */
function readoutHtml(cell, r) {
    const s = stateOf(cell);
    const icon = READOUT_ICONS[r.kind] || 'fa-circle-info';

    // No key, or nothing reported yet: say so rather than invent a value.
    if (!r.key || s[r.key] === undefined || s[r.key] === null) {
        return `<span class="readout is-pending" title="Not reported yet">
                    <i class="fas ${icon}"></i>${esc(r.kind)} —
                </span>`;
    }

    const v = s[r.key];

    if (r.binary) {
        let open, label, cls;
        switch (r.kind) {
            case 'contact':
                // `contact` true = closed; `is_open` true = open.
                open = r.key === 'is_open' ? v === true : v === false;
                label = open ? 'Open' : 'Closed';
                cls = open ? 'state-active' : 'state-idle';
                return `<span class="readout ${cls}">
                            <i class="fas ${open ? 'fa-door-open' : 'fa-door-closed'}"></i>${label}
                        </span>`;
            case 'motion':
                label = v ? 'Motion' : 'Clear';
                cls = v ? 'state-active' : 'state-idle';
                break;
            case 'leak':
                label = v ? 'Leak' : 'Dry';
                cls = v ? 'state-alarm' : 'state-idle';
                break;
            case 'smoke':
                label = v ? 'Smoke' : 'Clear';
                cls = v ? 'state-alarm' : 'state-idle';
                break;
            case 'vibration':
                label = v ? 'Vibration' : 'Still';
                cls = v ? 'state-active' : 'state-idle';
                break;
            default:
                label = v ? 'Yes' : 'No';
                cls = v ? 'state-active' : 'state-idle';
        }
        return `<span class="readout ${cls}"><i class="fas ${icon}"></i>${label}</span>`;
    }

    const num = typeof v === 'number' ? (Math.round(v * 10) / 10) : v;
    const unit = NUM_UNITS[r.kind] || '';
    return `<span class="readout"><i class="fas ${icon}"></i>${esc(num)}${unit}</span>`;
}

// ── controls ────────────────────────────────────────────────────────

function controlsHtml(cell) {
    const f = cell.features || [];
    const ieee = esc(cell.ieee);
    let html = '';

    if (f.includes('toggle')) {
        const on = isOn(cell);
        html += `
            <button class="btn btn-sm ${on ? 'btn-warning' : 'btn-outline-secondary'} flex-grow-1"
                    onclick="window.frameCommand('${ieee}', 'toggle')"
                    aria-pressed="${on}">
                <i class="fas fa-power-off"></i> ${on ? 'On' : 'Off'}
            </button>`;
    }

    if (f.includes('open') || f.includes('close')) {
        html += `
            <div class="btn-group btn-group-sm flex-grow-1" role="group" aria-label="Cover controls">
                <button class="btn btn-outline-secondary" onclick="window.frameCommand('${ieee}', 'open')" title="Open">
                    <i class="fas fa-arrow-up"></i>
                </button>
                <button class="btn btn-outline-secondary" onclick="window.frameCommand('${ieee}', 'stop')" title="Stop">
                    <i class="fas fa-stop"></i>
                </button>
                <button class="btn btn-outline-secondary" onclick="window.frameCommand('${ieee}', 'close')" title="Close">
                    <i class="fas fa-arrow-down"></i>
                </button>
            </div>`;
    }

    if (f.includes('setpoint')) {
        const sp = setpointOf(cell);
        html += `
            <div class="cell-setpoint flex-grow-1">
                <button class="btn btn-sm btn-outline-secondary" onclick="window.frameSetpoint('${ieee}', -0.5)"
                        aria-label="Decrease setpoint" ${sp === null ? 'disabled' : ''}>
                    <i class="fas fa-minus"></i>
                </button>
                <span class="cell-setpoint-value" data-setpoint>${sp === null ? '—' : sp + '°'}</span>
                <button class="btn btn-sm btn-outline-secondary" onclick="window.frameSetpoint('${ieee}', 0.5)"
                        aria-label="Increase setpoint" ${sp === null ? 'disabled' : ''}>
                    <i class="fas fa-plus"></i>
                </button>
            </div>`;
    }

    let sliders = '';
    if (f.includes('brightness')) {
        sliders += `
            <input type="range" class="form-range cell-slider" min="0" max="100" value="${brightnessOf(cell)}"
                   aria-label="Brightness"
                   onchange="window.frameCommand('${ieee}', 'brightness', this.value)">`;
    }
    if (f.includes('position')) {
        sliders += `
            <input type="range" class="form-range cell-slider" min="0" max="100" value="${positionOf(cell)}"
                   aria-label="Position"
                   onchange="window.frameCommand('${ieee}', 'position', this.value)">`;
    }

    return (html ? `<div class="cell-controls">${html}</div>` : '') + sliders;
}

// ── cells ───────────────────────────────────────────────────────────

function cellIcon(cell) {
    if (cell.kind === 'sensor' && cell.readouts?.length) {
        return READOUT_ICONS[cell.readouts[0].kind] || KIND_ICONS.sensor;
    }
    return KIND_ICONS[cell.kind] || KIND_ICONS.unknown;
}

function cellInnerHtml(cell) {
    const dev = devOf(cell);
    // Prefer the live name — a rename shouldn't need a frame refetch.
    const name = dev?.friendly_name || cell.name;
    const active = (cell.features || []).includes('toggle') && isOn(cell);

    const badges = (cell.badges || []).map(b => {
        if (b === 'battery') {
            const lvl = stateOf(cell).battery;
            return `<i class="fas fa-battery-half" title="Battery${typeof lvl === 'number' ? `: ${lvl}%` : ''}"></i>`;
        }
        if (b === 'power') return '<i class="fas fa-bolt" title="Power monitoring"></i>';
        return '';
    }).join('');

    const readouts = (cell.readouts || []).map(r => readoutHtml(cell, r)).join('');
    const controls = controlsHtml(cell);

    // `unknown` still renders: name + last seen. A device you can't see is
    // worse than one you can't use.
    const body = controls || readouts
        ? `${readouts ? `<div class="cell-readouts">${readouts}</div>` : ''}${controls}`
        : `<div class="readout is-pending">No controls or readings</div>`;

    return `
        <div class="cell-head">
            <span class="cell-icon"><i class="fas ${cellIcon(cell)}"></i></span>
            <span class="cell-name" title="${esc(name)}">${esc(name)}</span>
            ${badges ? `<span class="cell-badges">${badges}</span>` : ''}
        </div>
        <div class="cell-body">${body}</div>
    `;
}

function cellHtml(cell) {
    const active = (cell.features || []).includes('toggle') && isOn(cell);
    const cls = [
        'frame-cell',
        cell.available ? '' : 'is-offline',
        active ? 'is-active' : '',
    ].filter(Boolean).join(' ');

    return `<div class="${cls}" data-ieee="${esc(cell.ieee)}" data-kind="${esc(cell.kind)}"
                 title="${cell.available ? '' : 'Device unreachable'}">
                ${cellInnerHtml(cell)}
            </div>`;
}

// ── frame ───────────────────────────────────────────────────────────

export async function loadFrame() {
    if (loading) return;
    loading = true;
    try {
        const url = current.type === 'saved'
            ? `/api/frames/${encodeURIComponent(current.id)}`
            : `/api/frames/auto?split=${encodeURIComponent(current.split)}`;
        const res = await fetch(url);
        const data = await res.json();
        if (!data.success) throw new Error(data.error || 'failed to build frame');
        frame = data;
        split = data.split || split;
        // Keep the open tab across a refresh if it still exists, so a websocket
        // reload doesn't bounce you back to the first tab mid-tap.
        const tabs = frame.tabs || [];
        activeTab = tabs.some(t => t.id === activeTab) ? activeTab : (tabs[0]?.id ?? null);
        renderFrame();
    } catch (e) {
        log.warn('failed to load frame:', e.message);
        const grid = document.getElementById('framesGrid');
        if (grid) {
            grid.innerHTML = `<div class="frame-empty">
                <i class="fas fa-triangle-exclamation fa-2x mb-2"></i>
                <div>Could not build the frame: ${esc(e.message)}</div>
            </div>`;
        }
    } finally {
        loading = false;
    }
}

export function renderFrame() {
    const grid = document.getElementById('framesGrid');
    if (!grid || !frame) return;

    if (!frame.groups?.length) {
        // A saved frame that filters everything out is a different problem from
        // a hive with nothing assigned yet — say which one it is.
        const custom = current.type === 'saved';
        grid.innerHTML = `<div class="frame-empty">
            <i class="fas fa-border-none fa-2x mb-2"></i>
            <div>${custom ? 'This frame has nothing in it.' : 'No devices to show yet.'}</div>
            <div class="small mt-1">${custom
                ? 'Edit the frame to widen what it includes.'
                : 'Assign devices to chambers from the Devices tab.'}</div>
        </div>`;
        return;
    }

    const tabs = frame.tabs || [];
    // Only the active tab's groups are rendered — the point of tabs is to not
    // scroll past everything else.
    const shown = tabs.length
        ? (tabs.find(t => t.id === activeTab)?.groups || []).map(k => frame.groups.find(g => g.key === k)).filter(Boolean)
        : frame.groups;

    const tabBar = tabs.length ? `
        <div class="frame-tabs" role="tablist" aria-label="Frame sections">
            ${tabs.map(t => {
                const on = t.id === activeTab;
                const count = t.groups.reduce(
                    (n, k) => n + (frame.groups.find(g => g.key === k)?.cells.length || 0), 0);
                return `<button class="frame-tab ${on ? 'is-active' : ''}" role="tab"
                                aria-selected="${on}" data-frame-tab="${esc(t.id)}"
                                onclick="window.setFrameTab('${esc(t.id)}')">
                            ${esc(t.name)} <span class="frame-tab-count">${count}</span>
                        </button>`;
            }).join('')}
        </div>` : '';

    grid.innerHTML = tabBar + shown.map(g => `
        <section class="frame-group" data-group="${esc(g.key)}">
            <div class="frame-group-head">
                <h6 class="frame-group-title">${esc(g.label)}</h6>
                <span class="frame-group-count">${g.cells.length}</span>
            </div>
            <div class="frame-comb">
                ${g.cells.map(cellHtml).join('')}
            </div>
        </section>
    `).join('');
}

export function setFrameTab(id) {
    if (!frame?.tabs?.some(t => t.id === id)) return;
    activeTab = id;
    renderFrame();
}

/** Re-render only the cells for one device. Called on every websocket update. */
export function framesHandleDeviceUpdate(ieee) {
    if (!frame) return;
    const cells = document.querySelectorAll(`.frame-cell[data-ieee="${CSS.escape(ieee)}"]`);
    if (!cells.length) return;

    const cell = frame.groups.flatMap(g => g.cells).find(c => c.ieee === ieee);
    if (!cell) return;

    const dev = devOf(cell);
    if (dev) cell.available = !!dev.available;

    for (const el of cells) {
        // Don't yank a slider out from under a finger mid-drag.
        if (el.contains(document.activeElement) &&
            document.activeElement.matches('input[type="range"]')) continue;
        el.classList.toggle('is-offline', !cell.available);
        el.classList.toggle('is-active', (cell.features || []).includes('toggle') && isOn(cell));
        el.innerHTML = cellInnerHtml(cell);
    }
}

export function setSplit(next) {
    if (next !== 'chamber' && next !== 'type') return;
    split = next;
    current = { type: 'auto', split: next };
    syncSelect();
    return loadFrame();
}

// ── frame selector ──────────────────────────────────────────────────

function selectValue() {
    return current.type === 'saved' ? `saved:${current.id}` : `auto:${current.split}`;
}

function syncSelect() {
    const sel = document.getElementById('framesSelect');
    if (sel) sel.value = selectValue();
    // Auto frames aren't editable — they're derived, not authored.
    const isSaved = current.type === 'saved';
    document.getElementById('framesEdit')?.toggleAttribute('disabled', !isSaved);
    document.getElementById('framesDelete')?.toggleAttribute('disabled', !isSaved);
}

export async function loadSavedFrames() {
    try {
        const data = await (await fetch('/api/frames')).json();
        savedFrames = data.success ? (data.frames || []) : [];
    } catch (e) {
        log.warn('failed to load saved frames:', e.message);
        savedFrames = [];
    }
    renderFrameSelect();
    return savedFrames;
}

function renderFrameSelect() {
    const sel = document.getElementById('framesSelect');
    if (!sel) return;
    sel.innerHTML = `
        <optgroup label="Automatic">
            <option value="auto:chamber">By chamber</option>
            <option value="auto:type">By device type</option>
        </optgroup>
        ${savedFrames.length ? `<optgroup label="Saved frames">${
            savedFrames.map(f => `<option value="saved:${esc(f.id)}">${esc(f.name)}</option>`).join('')
        }</optgroup>` : ''}
    `;
    syncSelect();
}

export function selectFrame(value) {
    const [type, rest] = String(value || '').split(':');
    if (type === 'saved' && rest) current = { type: 'saved', id: rest };
    else current = { type: 'auto', split: rest === 'type' ? 'type' : 'chamber' };
    syncSelect();
    return loadFrame();
}

// ── builder ─────────────────────────────────────────────────────────

/**
 * Open the frame builder.
 *
 * With no id, starts from what's on screen — if you're looking at "By type" and
 * hit New, the draft starts as "By type". Building from the thing you were
 * already looking at beats starting from a blank slate.
 */
export async function openFrameBuilder(id = null) {
    const [chRes, kRes, cRes] = await Promise.all([
        fetch('/api/chambers').then(r => r.json()).catch(() => ({})),
        fetch('/api/frames/kinds').then(r => r.json()).catch(() => ({})),
        fetch('/api/frames/cells').then(r => r.json()).catch(() => ({})),
    ]);
    allChambers = chRes.chambers || [];
    allKinds = kRes.kinds || [];
    const cells = cRes.cells || [];

    const existing = id ? savedFrames.find(f => f.id === id) : null;
    draft = existing
        ? JSON.parse(JSON.stringify(existing))
        : { id: null, name: '', split: current.type === 'auto' ? current.split : 'chamber',
            chambers: [], kinds: [], devices: [], order: [], tabs: [] };
    draft.tabs = draft.tabs || [];
    draft._cells = cells;

    document.getElementById('frameBuilderModal')?.remove();
    document.body.insertAdjacentHTML('beforeend', `
        <div class="modal fade" id="frameBuilderModal" tabindex="-1" aria-labelledby="frameBuilderTitle" aria-hidden="true">
            <div class="modal-dialog modal-lg modal-dialog-scrollable">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title" id="frameBuilderTitle">
                            <i class="fas fa-border-all"></i> ${existing ? 'Edit frame' : 'New frame'}
                        </h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                    </div>
                    <div class="modal-body">
                        <div class="row g-2 mb-3">
                            <div class="col-md-7">
                                <label class="form-label small fw-bold" for="frameName">Name</label>
                                <input type="text" class="form-control form-control-sm" id="frameName"
                                       value="${esc(draft.name)}" placeholder="e.g. Downstairs, Evening">
                            </div>
                            <div class="col-md-5">
                                <label class="form-label small fw-bold" for="frameSplit">Group by</label>
                                <select class="form-select form-select-sm" id="frameSplit">
                                    <option value="chamber" ${draft.split === 'chamber' ? 'selected' : ''}>Chamber</option>
                                    <option value="type" ${draft.split === 'type' ? 'selected' : ''}>Device type</option>
                                </select>
                            </div>
                        </div>
                        <div class="text-muted small mb-3">
                            Leave a section empty to include everything in it.
                        </div>
                        <div class="row g-3">
                            <div class="col-md-6">
                                <label class="form-label small fw-bold">Chambers</label>
                                <div id="frameChambers" class="border rounded p-2" style="max-height:150px;overflow:auto"></div>
                            </div>
                            <div class="col-md-6">
                                <label class="form-label small fw-bold">Device types</label>
                                <div id="frameKinds" class="border rounded p-2" style="max-height:150px;overflow:auto"></div>
                            </div>
                        </div>
                        <hr>
                        <div class="d-flex align-items-center mb-1">
                            <label class="form-label small fw-bold mb-0">Tabs</label>
                            <button class="btn btn-sm btn-outline-secondary py-0 px-2 ms-auto"
                                    onclick="window.frameAddTab()">
                                <i class="fas fa-plus"></i> Add tab
                            </button>
                        </div>
                        <div class="text-muted small mb-2" id="frameTabsHint"></div>
                        <div id="frameTabs" class="mb-3"></div>

                        <hr>
                        <label class="form-label small fw-bold">Specific devices</label>
                        <div class="text-muted small mb-2">
                            Pick devices to pin this frame to exactly those. Picked devices can be
                            arranged; everything else keeps its natural order.
                        </div>
                        <input type="search" id="frameDeviceSearch" class="form-control form-control-sm mb-2"
                               placeholder="Filter devices…" aria-label="Filter devices">
                        <div class="row g-2">
                            <div class="col-md-6">
                                <div id="frameDevices" class="border rounded p-2" style="max-height:200px;overflow:auto"></div>
                            </div>
                            <div class="col-md-6">
                                <div class="small text-muted mb-1">Arrangement</div>
                                <div id="frameOrder" class="border rounded p-2" style="max-height:200px;overflow:auto"></div>
                            </div>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Cancel</button>
                        <button class="btn btn-sm btn-primary" id="frameSave" onclick="window.saveFrame()">Save</button>
                    </div>
                </div>
            </div>
        </div>
    `);

    renderBuilder();

    document.getElementById('frameSplit').addEventListener('change', e => {
        draft.split = e.target.value;
        // Chamber ids and cell kinds are different group vocabularies, so tab
        // assignments can't survive a split change. Clear them rather than
        // leave tabs silently holding keys that will never match.
        if (draft.tabs.some(t => t.groups.length)) {
            draft.tabs.forEach(t => { t.groups = []; });
            window.toast.info('Tab contents cleared — chambers and device types group differently');
        }
        renderTabsEditor();
    });
    document.getElementById('frameName').addEventListener('input', e => { draft.name = e.target.value; });
    document.getElementById('frameDeviceSearch').addEventListener('input', e => {
        const q = e.target.value.trim().toLowerCase();
        document.querySelectorAll('#frameDevices label').forEach(el => {
            el.classList.toggle('d-none', q && !el.dataset.name.includes(q));
        });
    });

    new bootstrap.Modal(document.getElementById('frameBuilderModal')).show();
}

function checkList(items, picked, handler) {
    if (!items.length) return '<div class="text-muted small">None available.</div>';
    return items.map(i => `
        <label class="d-block small" data-name="${esc(i.label.toLowerCase())}">
            <input class="form-check-input me-1" type="checkbox" value="${esc(i.value)}"
                   ${picked.includes(i.value) ? 'checked' : ''}
                   onchange="${handler}('${esc(i.value)}', this.checked)">
            ${esc(i.label)}
        </label>`).join('');
}

function renderBuilder() {
    document.getElementById('frameChambers').innerHTML = checkList(
        allChambers.map(c => ({ value: c.id, label: c.name })), draft.chambers, 'window.frameToggleChamber');
    document.getElementById('frameKinds').innerHTML = checkList(
        allKinds.map(k => ({ value: k.kind, label: k.label })), draft.kinds, 'window.frameToggleKind');
    document.getElementById('frameDevices').innerHTML = checkList(
        draft._cells.map(c => ({ value: c.ieee, label: c.name })), draft.devices, 'window.frameToggleDevice');
    renderTabsEditor();
    renderOrderList();
}

/** The groups this frame can produce — what a tab is allowed to hold. */
function availableGroups() {
    if (draft.split === 'type') {
        const wanted = draft.kinds.length ? draft.kinds : allKinds.map(k => k.kind);
        return allKinds.filter(k => wanted.includes(k.kind)).map(k => ({ value: k.kind, label: k.label }));
    }
    const wanted = draft.chambers.length ? draft.chambers : allChambers.map(c => c.id);
    const out = allChambers.filter(c => wanted.includes(c.id)).map(c => ({ value: c.id, label: c.name }));
    // Unassigned is a real group you can put in a tab, not a special case.
    if (!draft.chambers.length) out.push({ value: '__unassigned__', label: 'Unassigned' });
    return out;
}

function renderTabsEditor() {
    const hint = document.getElementById('frameTabsHint');
    const el = document.getElementById('frameTabs');
    if (!el || !hint) return;

    hint.textContent = draft.tabs.length
        ? 'Groups you don\'t place land in the first tab.'
        : draft.split === 'chamber'
            ? 'No tabs: sections are grouped by floor automatically, if your floor plan has levels.'
            : 'No tabs: every section is shown in one list.';

    if (!draft.tabs.length) { el.innerHTML = ''; return; }

    const groups = availableGroups();
    el.innerHTML = draft.tabs.map((t, i) => `
        <div class="card mb-2" data-tab-id="${esc(t.id)}">
            <div class="card-body py-2">
                <div class="d-flex align-items-center gap-1 mb-2">
                    <input type="text" class="form-control form-control-sm" value="${esc(t.name)}"
                           aria-label="Tab name" placeholder="Tab name"
                           onchange="window.frameRenameTab('${esc(t.id)}', this.value)">
                    <button class="btn btn-sm btn-outline-secondary py-0 px-1" ${i === 0 ? 'disabled' : ''}
                            onclick="window.frameMoveTab('${esc(t.id)}', -1)" aria-label="Move tab left">
                        <i class="fas fa-chevron-up"></i>
                    </button>
                    <button class="btn btn-sm btn-outline-secondary py-0 px-1"
                            ${i === draft.tabs.length - 1 ? 'disabled' : ''}
                            onclick="window.frameMoveTab('${esc(t.id)}', 1)" aria-label="Move tab right">
                        <i class="fas fa-chevron-down"></i>
                    </button>
                    <button class="btn btn-sm btn-outline-danger py-0 px-1"
                            onclick="window.frameRemoveTab('${esc(t.id)}')" aria-label="Remove tab">
                        <i class="fas fa-trash"></i>
                    </button>
                </div>
                <div class="d-flex flex-wrap gap-2">
                    ${groups.map(g => `
                        <label class="small">
                            <input class="form-check-input me-1" type="checkbox"
                                   ${t.groups.includes(g.value) ? 'checked' : ''}
                                   onchange="window.frameToggleTabGroup('${esc(t.id)}', '${esc(g.value)}', this.checked)">
                            ${esc(g.label)}
                        </label>`).join('')}
                </div>
            </div>
        </div>`).join('');
}

export function frameAddTab() {
    const n = draft.tabs.length + 1;
    draft.tabs.push({ id: `tab_${Date.now().toString(36)}`, name: `Tab ${n}`, groups: [] });
    renderTabsEditor();
}

export function frameRenameTab(id, name) {
    const t = draft.tabs.find(x => x.id === id);
    if (t) t.name = name;
}

export function frameRemoveTab(id) {
    draft.tabs = draft.tabs.filter(t => t.id !== id);
    renderTabsEditor();
}

export function frameMoveTab(id, delta) {
    const i = draft.tabs.findIndex(t => t.id === id);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= draft.tabs.length) return;
    [draft.tabs[i], draft.tabs[j]] = [draft.tabs[j], draft.tabs[i]];
    renderTabsEditor();
}

export function frameToggleTabGroup(tabId, group, on) {
    const t = draft.tabs.find(x => x.id === tabId);
    if (!t) return;
    if (on) {
        // A group lives in exactly one tab — claiming it here removes it elsewhere,
        // rather than silently rendering it in whichever tab happens to win.
        for (const other of draft.tabs) other.groups = other.groups.filter(g => g !== group);
        t.groups.push(group);
    } else {
        t.groups = t.groups.filter(g => g !== group);
    }
    renderTabsEditor();
}

function renderOrderList() {
    const el = document.getElementById('frameOrder');
    if (!el) return;
    if (!draft.devices.length) {
        el.innerHTML = '<div class="text-muted small">Pick devices to arrange them.</div>';
        return;
    }
    const nameOf = ieee => draft._cells.find(c => c.ieee === ieee)?.name || ieee;
    el.innerHTML = orderedPicks().map((ieee, i, arr) => `
        <div class="d-flex align-items-center gap-1 small mb-1" data-order-ieee="${esc(ieee)}">
            <span class="flex-grow-1 text-truncate">${esc(nameOf(ieee))}</span>
            <button class="btn btn-sm btn-outline-secondary py-0 px-1" ${i === 0 ? 'disabled' : ''}
                    onclick="window.frameMoveDevice('${esc(ieee)}', -1)" aria-label="Move up">
                <i class="fas fa-chevron-up"></i>
            </button>
            <button class="btn btn-sm btn-outline-secondary py-0 px-1" ${i === arr.length - 1 ? 'disabled' : ''}
                    onclick="window.frameMoveDevice('${esc(ieee)}', 1)" aria-label="Move down">
                <i class="fas fa-chevron-down"></i>
            </button>
        </div>`).join('');
}

/** Picked devices in saved order, with any not-yet-ordered picks appended. */
function orderedPicks() {
    const picked = new Set(draft.devices);
    const out = draft.order.filter(i => picked.has(i));
    for (const ieee of draft.devices) if (!out.includes(ieee)) out.push(ieee);
    return out;
}

export function frameToggleChamber(id, on) {
    draft.chambers = on ? [...draft.chambers, id] : draft.chambers.filter(x => x !== id);
    // Which chambers are in play decides what a tab can hold.
    renderTabsEditor();
}

export function frameToggleKind(kind, on) {
    draft.kinds = on ? [...draft.kinds, kind] : draft.kinds.filter(x => x !== kind);
    renderTabsEditor();
}

export function frameToggleDevice(ieee, on) {
    draft.devices = on ? [...draft.devices, ieee] : draft.devices.filter(x => x !== ieee);
    if (!on) draft.order = draft.order.filter(x => x !== ieee);
    renderOrderList();
}

export function frameMoveDevice(ieee, delta) {
    const order = orderedPicks();
    const i = order.indexOf(ieee);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= order.length) return;
    [order[i], order[j]] = [order[j], order[i]];
    draft.order = order;
    renderOrderList();
}

export async function saveFrame() {
    if (!draft) return;
    const name = (draft.name || '').trim();
    if (!name) {
        window.toast.error('Give the frame a name');
        document.getElementById('frameName')?.focus();
        return;
    }

    const btn = document.getElementById('frameSave');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Saving…'; }
    try {
        const body = {
            name,
            split: draft.split,
            chambers: draft.chambers,
            kinds: draft.kinds,
            devices: draft.devices,
            order: orderedPicks(),
            tabs: draft.tabs,
        };
        // Only send id when editing — a new frame derives its id from the name.
        if (draft.id) body.id = draft.id;

        const data = await (await fetch('/api/frames', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        })).json();
        if (!data.success) throw new Error(data.error || 'save failed');

        savedFrames = data.frames || savedFrames;
        renderFrameSelect();
        bootstrap.Modal.getInstance(document.getElementById('frameBuilderModal'))?.hide();
        draft = null;
        window.toast.success(`Frame "${data.frame.name}" saved`);
        await selectFrame(`saved:${data.frame.id}`);
    } catch (e) {
        window.toast.error('Could not save frame: ' + e.message);
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = 'Save'; }
    }
}

export async function deleteCurrentFrame() {
    if (current.type !== 'saved') return;
    const f = savedFrames.find(x => x.id === current.id);
    const ok = await window.zbmConfirm({
        title: 'Delete frame',
        message: `Delete "${f?.name || current.id}"? The devices in it are not affected.`,
        confirmText: 'Delete',
        variant: 'danger',
    });
    if (!ok) return;

    try {
        const data = await (await fetch(`/api/frames/${encodeURIComponent(current.id)}`, {
            method: 'DELETE',
        })).json();
        if (!data.success) throw new Error(data.error || 'delete failed');
        savedFrames = data.frames || [];
        renderFrameSelect();
        window.toast.success('Frame deleted');
        await selectFrame('auto:chamber');
    } catch (e) {
        window.toast.error('Could not delete frame: ' + e.message);
    }
}

// ── quick actions ───────────────────────────────────────────────────

/**
 * Route a cell action through the shared command path.
 *
 * sendCommand already applies the optimistic update to state.devices; the
 * websocket echo then lands via framesHandleDeviceUpdate.
 */
export async function frameCommand(ieee, command, value = null) {
    if (!window.sendCommand) return;
    const v = value === null ? null : (isNaN(value) ? value : Number(value));
    await window.sendCommand(ieee, command, v);
    framesHandleDeviceUpdate(ieee);
}

export async function frameSetpoint(ieee, delta) {
    const cell = frame?.groups.flatMap(g => g.cells).find(c => c.ieee === ieee);
    if (!cell) return;
    const current = setpointOf(cell);
    if (current === null) return;

    const next = Math.round((current + delta) * 2) / 2;
    // Optimistic: the stepper should track the finger, not the round trip.
    const el = document.querySelector(`.frame-cell[data-ieee="${CSS.escape(ieee)}"] [data-setpoint]`);
    if (el) el.textContent = `${next}°`;

    await window.sendCommand(ieee, 'temperature', next);
}

// ── init ────────────────────────────────────────────────────────────

export function initFrames() {
    // The dashboard renders Frames inside a tab and only loads it when shown.
    // The standalone /frames page has no tab — everything below still wires up.
    const tab = document.querySelector('[data-bs-target="#frames"]');
    tab?.addEventListener('shown.bs.tab', async () => {
        await loadSavedFrames();
        loadFrame();
    });

    document.getElementById('framesSelect')?.addEventListener('change', e => selectFrame(e.target.value));
    document.getElementById('framesNew')?.addEventListener('click', () => openFrameBuilder());
    document.getElementById('framesEdit')?.addEventListener('click', () => {
        if (current.type === 'saved') openFrameBuilder(current.id);
    });
    document.getElementById('framesDelete')?.addEventListener('click', () => deleteCurrentFrame());
    document.getElementById('framesRefresh')?.addEventListener('click', () => loadFrame());

    syncSelect();
}
