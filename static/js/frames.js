/**
 * Frames — dynamically-generated dashboards.
 *
 * Structure comes from /api/frames/auto; live values come from
 * state.deviceCache, which the websocket keeps current, so a state change
 * re-renders one cell without refetching the layout. Zigbee-only for now.
 * Terminology and the group-command conventions: docs/frames.md.
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

// state helpers (match the conventions in modal/control.js)

/**
 * The live device behind a cell.
 *
 * deviceCache is the fast path, but on the dashboard it is filled as a side
 * effect of rendering the device table — so a table filtered to one tab leaves
 * every other device missing from it, and a frame full of cells with no state.
 * Falling back to state.devices (and adopting what it finds) means a cell shows
 * what the device is doing regardless of what the table happens to be showing.
 */
function devOf(cell) {
    if (!cell?.ieee) return null;
    const cached = state.deviceCache?.[cell.ieee];
    if (cached) return cached;

    const dev = (state.devices || []).find(d => d.ieee === cell.ieee);
    if (dev && state.deviceCache) state.deviceCache[cell.ieee] = dev;
    return dev || null;
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

/**
 * Colour temperature in kelvin, endpoint-aware.
 *
 * The device reports mireds (ZCL's unit), the slider and the `color_temp`
 * command both speak kelvin, and the two are reciprocals — the same conversion
 * modal/control.js does. 2700K is the fallback a warm-white bulb starts at.
 */
function colorTempKelvinOf(cell, ep = 1) {
    const s = stateOf(cell);
    const mireds = s[`color_temp_${ep}`] || (ep === 1 ? s.color_temp : null);
    if (!mireds) return 2700;
    return Math.min(CT_MAX_K, Math.max(CT_MIN_K, Math.round(1000000 / mireds)));
}

/** The swatch colour, from reported hue/saturation on the ZCL 0-254 scale. */
function colorHexOf(cell, ep = 1) {
    const s = stateOf(cell);
    const hue = s[`hue_${ep}`] ?? s.hue ?? s.color_hue ?? 0;
    const sat = s[`saturation_${ep}`] ?? s.saturation ?? s.color_saturation ?? 254;
    return hsToHex(Math.round((hue / 254) * 360), Math.round((sat / 254) * 100));
}

// colour conversion — a local copy because utils.js is a dashboard module and
// the standalone /frames page deliberately doesn't load it (frames-page.js).

function hsToHex(h, s) {
    const sat = s / 100;
    const a = sat * 0.5;
    const f = n => {
        const k = (n + h / 30) % 12;
        const v = 0.5 - a * Math.max(Math.min(k - 3, 9 - k, 1), -1);
        return Math.round(255 * v).toString(16).padStart(2, '0');
    };
    return `#${f(0)}${f(8)}${f(4)}`;
}

function hexToHS(hex) {
    const r = parseInt(hex.slice(1, 3), 16) / 255;
    const g = parseInt(hex.slice(3, 5), 16) / 255;
    const b = parseInt(hex.slice(5, 7), 16) / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
    const s = max === 0 ? 0 : d / max;
    let h = 0;
    if (d !== 0) {
        if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
        else if (max === g) h = (b - r) / d + 2;
        else h = (r - g) / d + 4;
        h /= 6;
    }
    return { h: Math.round(h * 360), s: Math.round(s * 100) };
}

// ── group cells (a Zigbee group's own control tile, see resolve_group_cell) ─

/**
 * Aggregate readouts for a group cell, reusing the per-device helpers above
 * against each member — a group cell has no ieee of its own to look up.
 */
function groupIsOn(cell) {
    return (cell.members || []).some(ieee => isOn({ ieee }));
}

function groupBrightnessPct(cell) {
    const vals = (cell.members || []).map(ieee => brightnessOf({ ieee }));
    return vals.length ? Math.round(vals.reduce((a, b) => a + b, 0) / vals.length) : 0;
}

function groupPositionPct(cell) {
    const vals = (cell.members || []).map(ieee => positionOf({ ieee }));
    return vals.length ? Math.round(vals.reduce((a, b) => a + b, 0) / vals.length) : 50;
}

/** First member that reports a colour temperature — an average of kelvin is meaningless. */
function groupColorTempKelvin(cell) {
    for (const ieee of cell.members || []) {
        const s = state.deviceCache?.[ieee]?.state || {};
        if (s.color_temp) return colorTempKelvinOf({ ieee });
    }
    return 2700;
}

/** Likewise for colour: show a member's, not a blend of everyone's. */
function groupColorHex(cell) {
    for (const ieee of cell.members || []) {
        const s = state.deviceCache?.[ieee]?.state || {};
        if (s.hue !== undefined || s.color_hue !== undefined) return colorHexOf({ ieee });
    }
    return '#ffffff';
}

// readout formatting

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

// controls

/** Kelvin range offered for tunable white — the same span modal/control.js uses. */
const CT_MIN_K = 2000;
const CT_MAX_K = 6500;

/**
 * The endpoints to render a control row for.
 *
 * `cell.features` is the union across endpoints, so it can say "this thing
 * switches" but never "it switches twice" — a two-gang socket and a one-gang
 * socket have an identical list. `cell.endpoints` is what tells them apart.
 * A payload from an older backend has none; treat the flat list as a single
 * endpoint 1 so the cell still renders rather than going blank.
 */
function endpointsOf(cell) {
    const eps = cell.endpoints;
    if (Array.isArray(eps) && eps.length) return eps;
    return [{ id: 1, type: cell.kind, features: cell.features || [] }];
}

function toggleHtml(cell, epId, showEp) {
    const on = isOn(cell, epId);
    // A tile is too narrow for "Switch (EP2)", so the gang shows as a number —
    // with the modal's wording on hover, since a bare digit needs explaining.
    return `
        <button class="btn btn-sm ${on ? 'btn-warning' : 'btn-outline-secondary'} flex-grow-1"
                onclick="window.frameCommand('${esc(cell.ieee)}', 'toggle', null, ${epId})"
                ${showEp ? `title="Endpoint ${epId}"` : ''}
                aria-label="${showEp ? `Endpoint ${epId}: ` : ''}${on ? 'On' : 'Off'}"
                aria-pressed="${on}">
            <i class="fas fa-power-off"></i> ${showEp ? `${epId} · ` : ''}${on ? 'On' : 'Off'}
        </button>`;
}

function coverHtml(cell, epId) {
    const ieee = esc(cell.ieee);
    return `
        <div class="btn-group btn-group-sm flex-grow-1" role="group" aria-label="Cover controls">
            <button class="btn btn-outline-secondary" onclick="window.frameCommand('${ieee}', 'open', null, ${epId})" title="Open">
                <i class="fas fa-arrow-up"></i>
            </button>
            <button class="btn btn-outline-secondary" onclick="window.frameCommand('${ieee}', 'stop', null, ${epId})" title="Stop">
                <i class="fas fa-stop"></i>
            </button>
            <button class="btn btn-outline-secondary" onclick="window.frameCommand('${ieee}', 'close', null, ${epId})" title="Close">
                <i class="fas fa-arrow-down"></i>
            </button>
        </div>`;
}

function setpointHtml(cell, epId) {
    const sp = setpointOf(cell);
    const ieee = esc(cell.ieee);
    return `
        <div class="cell-setpoint flex-grow-1">
            <button class="btn btn-sm btn-outline-secondary" onclick="window.frameSetpoint('${ieee}', -0.5, ${epId})"
                    aria-label="Decrease setpoint" ${sp === null ? 'disabled' : ''}>
                <i class="fas fa-minus"></i>
            </button>
            <span class="cell-setpoint-value" data-setpoint>${sp === null ? '—' : sp + '°'}</span>
            <button class="btn btn-sm btn-outline-secondary" onclick="window.frameSetpoint('${ieee}', 0.5, ${epId})"
                    aria-label="Increase setpoint" ${sp === null ? 'disabled' : ''}>
                <i class="fas fa-plus"></i>
            </button>
        </div>`;
}

/**
 * A slider with its live value beside it.
 *
 * `oninput` moves the number while the finger is down and `onchange` sends on
 * release — one command per gesture, not one per pixel, which is what keeps a
 * drag from flooding the radio.
 */
function sliderHtml({ icon, min, max, value, unit, label, css, onInputTarget, onChange }) {
    return `
        <div class="cell-slider-row">
            <i class="fas ${icon}" aria-hidden="true"></i>
            <input type="range" class="cell-slider" min="${min}" max="${max}" value="${value}"
                   aria-label="${esc(label)}" ${css ? `style="${css}"` : ''}
                   oninput="window.frameSliderInput(this, '${onInputTarget}', '${unit}')"
                   onchange="${onChange}">
            <span class="cell-slider-value" data-slider-value="${onInputTarget}">${value}${unit}</span>
        </div>`;
}

/** Every control one endpoint offers, in the order the device modal shows them. */
function endpointControlsHtml(cell, ep, showLabel) {
    const f = ep.features || [];
    const ieee = esc(cell.ieee);
    const epId = ep.id;
    const key = `${cell.ieee}-${epId}`;
    let buttons = '';
    let rows = '';

    if (f.includes('toggle')) buttons += toggleHtml(cell, epId, showLabel);
    if (f.includes('open') || f.includes('close')) buttons += coverHtml(cell, epId);
    if (f.includes('setpoint')) buttons += setpointHtml(cell, epId);

    if (f.includes('brightness')) {
        rows += sliderHtml({
            icon: 'fa-sun', min: 0, max: 100, value: brightnessOf(cell, epId), unit: '%',
            label: 'Brightness', onInputTarget: `bri-${key}`,
            onChange: `window.frameCommand('${ieee}', 'brightness', this.value, ${epId})`,
        });
    }

    if (f.includes('color_temp')) {
        rows += sliderHtml({
            icon: 'fa-temperature-half', min: CT_MIN_K, max: CT_MAX_K, value: colorTempKelvinOf(cell, epId), unit: 'K',
            label: 'Colour temperature', onInputTarget: `ct-${key}`,
            css: 'background: linear-gradient(to right, #ffae00, #ffead1, #fff, #d1eaff, #99ccff);',
            onChange: `window.frameCommand('${ieee}', 'color_temp', this.value, ${epId})`,
        });
    }

    if (f.includes('position')) {
        rows += sliderHtml({
            icon: 'fa-arrows-up-down', min: 0, max: 100, value: positionOf(cell), unit: '%',
            label: 'Position', onInputTarget: `pos-${key}`,
            onChange: `window.frameCommand('${ieee}', 'position', this.value, ${epId})`,
        });
    }

    if (f.includes('color')) {
        rows += `
            <div class="cell-slider-row">
                <i class="fas fa-palette" aria-hidden="true"></i>
                <input type="color" class="cell-swatch" value="${colorHexOf(cell, epId)}"
                       aria-label="Colour"
                       onchange="window.frameColor('${ieee}', this.value, ${epId})">
            </div>`;
    }

    if (!buttons && !rows) return '';
    return `<div class="cell-ep">${buttons ? `<div class="cell-controls">${buttons}</div>` : ''}${rows}</div>`;
}

function controlsHtml(cell) {
    const eps = endpointsOf(cell);
    // Label the toggles only when there is more than one gang to tell apart.
    const showLabel = eps.filter(e => (e.features || []).includes('toggle')).length > 1;
    return eps.map(ep => endpointControlsHtml(cell, ep, showLabel)).join('');
}

/** Lit if anything on the device is on — one gang of two still counts. */
function cellIsActive(cell) {
    if (cell.is_group) return (cell.features || []).includes('toggle') && groupIsOn(cell);
    return endpointsOf(cell).some(ep => (ep.features || []).includes('toggle') && isOn(cell, ep.id));
}

/**
 * Controls for a group's own tile — same layout as controlsHtml, but every
 * action goes through frameGroupCommand (POST /api/groups/{id}/control)
 * instead of the single-device command path, and reads/writes are aggregated
 * across cell.members instead of one device's state. A group is one control
 * surface by definition, so there are no endpoint rows here.
 */
function groupControlsHtml(cell) {
    const f = cell.features || [];
    const gid = cell.group_id;
    const key = `g${gid}`;
    let buttons = '';
    let rows = '';

    if (f.includes('toggle')) {
        const on = groupIsOn(cell);
        buttons += `
            <button class="btn btn-sm ${on ? 'btn-warning' : 'btn-outline-secondary'} flex-grow-1"
                    onclick="window.frameGroupCommand(${gid}, 'toggle', ${!on})"
                    aria-pressed="${on}">
                <i class="fas fa-power-off"></i> ${on ? 'On' : 'Off'}
            </button>`;
    }

    if (f.includes('open') || f.includes('close')) {
        buttons += `
            <div class="btn-group btn-group-sm flex-grow-1" role="group" aria-label="Group cover controls">
                <button class="btn btn-outline-secondary" onclick="window.frameGroupCommand(${gid}, 'open')" title="Open">
                    <i class="fas fa-arrow-up"></i>
                </button>
                <button class="btn btn-outline-secondary" onclick="window.frameGroupCommand(${gid}, 'stop')" title="Stop">
                    <i class="fas fa-stop"></i>
                </button>
                <button class="btn btn-outline-secondary" onclick="window.frameGroupCommand(${gid}, 'close')" title="Close">
                    <i class="fas fa-arrow-down"></i>
                </button>
            </div>`;
    }

    if (f.includes('brightness')) {
        rows += sliderHtml({
            icon: 'fa-sun', min: 0, max: 100, value: groupBrightnessPct(cell), unit: '%',
            label: 'Brightness', onInputTarget: `bri-${key}`,
            onChange: `window.frameGroupCommand(${gid}, 'brightness', this.value)`,
        });
    }

    if (f.includes('color_temp')) {
        rows += sliderHtml({
            icon: 'fa-temperature-half', min: CT_MIN_K, max: CT_MAX_K, value: groupColorTempKelvin(cell), unit: 'K',
            label: 'Colour temperature', onInputTarget: `ct-${key}`,
            css: 'background: linear-gradient(to right, #ffae00, #ffead1, #fff, #d1eaff, #99ccff);',
            onChange: `window.frameGroupCommand(${gid}, 'color_temp', this.value)`,
        });
    }

    if (f.includes('position')) {
        rows += sliderHtml({
            icon: 'fa-arrows-up-down', min: 0, max: 100, value: groupPositionPct(cell), unit: '%',
            label: 'Position', onInputTarget: `pos-${key}`,
            onChange: `window.frameGroupCommand(${gid}, 'position', this.value)`,
        });
    }

    if (f.includes('color')) {
        rows += `
            <div class="cell-slider-row">
                <i class="fas fa-palette" aria-hidden="true"></i>
                <input type="color" class="cell-swatch" value="${groupColorHex(cell)}"
                       aria-label="Colour"
                       onchange="window.frameGroupColor(${gid}, this.value)">
            </div>`;
    }

    return (buttons ? `<div class="cell-controls">${buttons}</div>` : '') + rows;
}


// cells

function cellIcon(cell) {
    if (cell.is_group) return 'fa-layer-group';
    if (cell.kind === 'sensor' && cell.readouts?.length) {
        return READOUT_ICONS[cell.readouts[0].kind] || KIND_ICONS.sensor;
    }
    return KIND_ICONS[cell.kind] || KIND_ICONS.unknown;
}

function cellInnerHtml(cell) {
    if (cell.is_group) {
        const memberCount = (cell.members || []).length;
        const controls = groupControlsHtml(cell);
        const body = controls || `<div class="readout is-pending">No controls available</div>`;
        return `
            <div class="cell-head">
                <span class="cell-icon"><i class="fas ${cellIcon(cell)}"></i></span>
                <span class="cell-name" title="${esc(cell.name)}">${esc(cell.name)}</span>
                <span class="cell-badges" title="${memberCount} device${memberCount === 1 ? '' : 's'} in this group">
                    <i class="fas fa-layer-group"></i>${memberCount}
                </span>
            </div>
            <div class="cell-body">${body}</div>
        `;
    }

    const dev = devOf(cell);
    // Prefer the live name — a rename shouldn't need a frame refetch.
    const name = dev?.friendly_name || cell.name;

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
    const active = cellIsActive(cell);
    const cls = [
        'frame-cell',
        cell.is_group ? 'is-group' : '',
        cell.available ? '' : 'is-offline',
        active ? 'is-active' : '',
    ].filter(Boolean).join(' ');

    return `<div class="${cls}" data-ieee="${esc(cell.ieee)}"
                 data-group-id="${cell.is_group ? esc(String(cell.group_id)) : ''}" data-kind="${esc(cell.kind)}"
                 title="${cell.available ? '' : 'Device unreachable'}">
                ${cellInnerHtml(cell)}
            </div>`;
}

// frame

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

/**
 * The cell whose slider is currently under a finger, if any.
 *
 * A range input on a touch screen never takes focus, so focus alone can't tell
 * us a drag is in progress — a websocket update arriving mid-drag would rebuild
 * the cell and snatch the slider away. Tracking the pointer covers both.
 */
let draggingCell = null;

function markDragging(el) {
    draggingCell = el.closest('.frame-cell');
}

function endDragging() {
    draggingCell = null;
}

/** Don't yank a slider out from under a finger mid-drag. */
function sliderBeingDragged(el) {
    if (draggingCell && el.contains(draggingCell)) return true;
    if (el === draggingCell) return true;
    return el.contains(document.activeElement) &&
        document.activeElement.matches('input[type="range"]');
}

/** Re-render only the cells for one device. Called on every websocket update. */
export function framesHandleDeviceUpdate(ieee) {
    if (!frame) return;
    const allCells = frame.groups.flatMap(g => g.cells);

    const cell = allCells.find(c => !c.is_group && c.ieee === ieee);
    if (cell) {
        const dev = devOf(cell);
        if (dev) cell.available = !!dev.available;
        for (const el of document.querySelectorAll(`.frame-cell[data-ieee="${CSS.escape(ieee)}"]`)) {
            if (sliderBeingDragged(el)) continue;
            el.classList.toggle('is-offline', !cell.available);
            el.classList.toggle('is-active', cellIsActive(cell));
            el.innerHTML = cellInnerHtml(cell);
        }
    }

    // A group has no websocket event of its own — its aggregate on/off,
    // brightness and position can still change because one of its member
    // devices just did, so refresh every group tile this ieee belongs to.
    for (const gc of allCells.filter(c => c.is_group && c.members?.includes(ieee))) {
        for (const el of document.querySelectorAll(`.frame-cell[data-group-id="${CSS.escape(String(gc.group_id))}"]`)) {
            if (sliderBeingDragged(el)) continue;
            el.classList.toggle('is-active', cellIsActive(gc));
            el.innerHTML = cellInnerHtml(gc);
        }
    }
}

export function setSplit(next) {
    if (next !== 'chamber' && next !== 'type') return;
    split = next;
    current = { type: 'auto', split: next };
    syncSelect();
    return loadFrame();
}

// frame selector

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

// builder

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

/** The sections this frame can produce — what a tab is allowed to hold. */
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

// quick actions

/**
 * The state keys a command is expected to produce.
 *
 * Same keys as actions.js:optimisticDeltaFor, deliberately — the websocket echo
 * then lands on top of the guess without the tile visibly flipping back and
 * forth. Returns null for a command with no predictable effect.
 */
function optimisticDelta(command, value, ep) {
    const suffix = `_${ep}`;
    const d = {};

    // Gang 2 turning on must not light gang 1's toggle, so the unsuffixed keys
    // are only written for endpoint 1 — the same rule handlers/general.py
    // applies when it reports the change back.
    const setOn = on => {
        d[`on${suffix}`] = on;
        d[`state${suffix}`] = on ? 'ON' : 'OFF';
        if (ep === 1) {
            d.on = on;
            d.state = on ? 'ON' : 'OFF';
        }
    };

    switch (command) {
        case 'on':
        case 'off':
            setOn(command === 'on');
            return d;
        case 'brightness': {
            const pct = Number(value);
            d.brightness = pct;
            d[`brightness${suffix}`] = pct;
            // A level command implies the light comes on — the modal's slider
            // behaves the same way, so the toggle must not lag behind it.
            setOn(pct > 0);
            return d;
        }
        case 'color_temp':
            // Kelvin in, mireds stored — the reciprocal, as everywhere else.
            d.color_temp = Math.round(1000000 / Number(value));
            d[`color_temp${suffix}`] = d.color_temp;
            return d;
        case 'hs_color': {
            const [h, s] = value || [];
            if (h === undefined) return null;
            d.hue = Math.round((h / 360) * 254);
            d.saturation = Math.round((s / 100) * 254);
            return d;
        }
        case 'position':
            d.position = Number(value);
            return d;
        case 'open':
            d.position = 100;
            return d;
        case 'close':
            d.position = 0;
            return d;
        case 'temperature':
            d.occupied_heating_setpoint = Number(value);
            d.heating_setpoint = Number(value);
            d.target_temp = Number(value);
            return d;
        default:
            return null;
    }
}

/**
 * Show a command's expected result now, and hand back an undo.
 *
 * /api/device/command doesn't answer until the radio has, which on a retrying
 * device is seconds — long enough that a tile updated only on the reply reads
 * as a dropped tap. So the cache is written first and the reply only gets to
 * correct it.
 */
function applyOptimistic(ieee, command, value, ep) {
    const dev = state.deviceCache?.[ieee];
    if (!dev) return () => {};

    const cmd = command === 'toggle' ? (isOn({ ieee }, ep) ? 'off' : 'on') : command;
    const delta = optimisticDelta(cmd, value, ep);
    if (!delta) return () => {};

    dev.state = dev.state || {};
    const before = {};
    for (const k of Object.keys(delta)) before[k] = dev.state[k];
    Object.assign(dev.state, delta);
    framesHandleDeviceUpdate(ieee);

    return () => {
        for (const [k, v] of Object.entries(before)) {
            if (v === undefined) delete dev.state[k];
            else dev.state[k] = v;
        }
        framesHandleDeviceUpdate(ieee);
    };
}

/**
 * Route a cell action through the shared command path.
 *
 * The optimistic update is applied before the request, not after it, and rolled
 * back if the command is refused. sendCommand applies its own copy on success
 * (writing the same keys), and the websocket echo lands on top of both.
 */
export async function frameCommand(ieee, command, value = null, endpoint = null) {
    if (!window.sendCommand) return;
    const ep = endpoint || 1;
    const v = value === null ? null : (Array.isArray(value) || isNaN(value) ? value : Number(value));

    const undo = applyOptimistic(ieee, command, v, ep);
    const res = await window.sendCommand(ieee, command, v, endpoint);
    // A transport that reports nothing can't be second-guessed; leave the
    // optimistic value up and let the websocket correct it.
    if (res && res.success === false) undo();
    else framesHandleDeviceUpdate(ieee);
}

/** Send the picked colour as hue/saturation, the units the command path wants. */
export function frameColor(ieee, hex, endpoint = null) {
    const { h, s } = hexToHS(hex);
    return frameCommand(ieee, 'hs_color', [h, s], endpoint);
}

export function frameSetpoint(ieee, delta, endpoint = null) {
    const cell = frame?.groups.flatMap(g => g.cells).find(c => c.ieee === ieee);
    if (!cell) return;
    const current = setpointOf(cell);
    if (current === null) return;

    return frameCommand(ieee, 'temperature', Math.round((current + delta) * 2) / 2, endpoint);
}

/**
 * Move a slider's value label while the finger is down.
 *
 * The command itself waits for `change` — one per gesture rather than one per
 * pixel — so this is what makes the drag feel connected to anything.
 */
export function frameSliderInput(el, target, unit) {
    markDragging(el);
    const out = el.parentElement?.querySelector(`[data-slider-value="${target}"]`);
    if (out) out.textContent = `${el.value}${unit}`;
}

/**
 * Route a group cell's quick action through POST /api/groups/{id}/control — a
 * different command surface from a device's, with its own unit conventions
 * (raw 0-254 brightness, mired colour temperature, un-inverted position).
 * Members report their new state over the websocket, so no optimistic update.
 * See docs/frames.md.
 */
export async function frameGroupCommand(groupId, action, value = null) {
    let body;
    switch (action) {
        case 'toggle': body = { state: value ? 'ON' : 'OFF' }; break;
        case 'brightness': body = { brightness: Math.round((Number(value) / 100) * 254) }; break;
        case 'color_temp': body = { color_temp: Math.round(1000000 / Number(value)) }; break;
        case 'hs_color': body = { hs_color: value }; break;
        case 'open': body = { cover_state: 'OPEN' }; break;
        case 'close': body = { cover_state: 'CLOSE' }; break;
        case 'stop': body = { cover_state: 'STOP' }; break;
        case 'position': body = { position: 100 - Number(value) }; break;
        default: return;
    }

    try {
        const res = await fetch(`/api/groups/${groupId}/control`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
    } catch (e) {
        window.toast.error('Group command failed: ' + e.message);
    }
}

export function frameGroupColor(groupId, hex) {
    const { h, s } = hexToHS(hex);
    return frameGroupCommand(groupId, 'hs_color', [h, s]);
}


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

    // Delegated because cells are rebuilt on every state change; the listener
    // has to outlive the elements it guards.
    document.addEventListener('pointerdown', e => {
        if (e.target.matches?.('.cell-slider')) markDragging(e.target);
    });
    document.addEventListener('pointerup', endDragging);
    document.addEventListener('pointercancel', endDragging);

    syncSelect();
}
