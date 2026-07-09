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
            <input type="text" class="form-control form-control-sm sig-filter"
                   placeholder="Filter by name / address…" style="width:220px">
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
}

// ---------------------------------------------------------------------------
// Device selection + streaming
// ---------------------------------------------------------------------------

async function _select(inst, ieee) {
    // Tear down any current stream first.
    await _stopStream(inst);
    inst.ieee = ieee;
    inst.rows = new Map();

    const isAll = ieee === ALL;
    inst.container.querySelector('.sig-col-device').style.display = isAll ? '' : 'none';

    if (!ieee) {
        _setLive(inst, false);
        _setInfo(inst, 'Pick a device to inspect its live signals.');
        return;
    }
    await _startStream(inst, ieee);
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
