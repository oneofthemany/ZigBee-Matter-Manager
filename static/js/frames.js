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
        const res = await fetch(`/api/frames/auto?split=${encodeURIComponent(split)}`);
        const data = await res.json();
        if (!data.success) throw new Error(data.error || 'failed to build frame');
        frame = data;
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
        grid.innerHTML = `<div class="frame-empty">
            <i class="fas fa-border-none fa-2x mb-2"></i>
            <div>No devices to show yet.</div>
            <div class="small mt-1">Assign devices to chambers from the Devices tab.</div>
        </div>`;
        return;
    }

    grid.innerHTML = frame.groups.map(g => `
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
    document.querySelectorAll('[data-frames-split]').forEach(b => {
        const on = b.dataset.framesSplit === split;
        b.classList.toggle('btn-warning', on);
        b.classList.toggle('btn-outline-secondary', !on);
        b.setAttribute('aria-pressed', on);
    });
    loadFrame();
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
    const tab = document.querySelector('[data-bs-target="#frames"]');
    if (!tab) return;

    tab.addEventListener('shown.bs.tab', () => loadFrame());

    document.querySelectorAll('[data-frames-split]').forEach(btn => {
        btn.addEventListener('click', () => setSplit(btn.dataset.framesSplit));
    });

    document.getElementById('framesRefresh')?.addEventListener('click', () => loadFrame());
}
