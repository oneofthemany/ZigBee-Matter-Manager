/**
 * Shelly / ESPHome device modal. Controls come from the device's own `controls`
 * list (what its channels can do), readings from its state, so one layout
 * serves both. Reuses the #capModal shell. Backend: modules/lan_devices.py.
 */

import { state } from '../state.js';
import { escapeHtml } from '../utils.js';

// Readings shown when present; a channel suffix (_1, _2) is shown as "· 1".
const READINGS = [
    ['power', 'Power', 'W'], ['energy', 'Energy', 'kWh'], ['voltage', 'Voltage', 'V'],
    ['current', 'Current', 'A'], ['temperature', 'Temperature', '°C'], ['humidity', 'Humidity', '%'],
    ['illuminance', 'Light', 'lx'], ['battery', 'Battery', '%'], ['co2', 'CO₂', 'ppm'],
    ['pm25', 'PM2.5', 'µg/m³'], ['device_temperature', 'Device temp', '°C'],
    ['occupancy', 'Motion', ''], ['contact', 'Contact', ''], ['water_leak', 'Leak', ''],
    ['cover_state', 'Cover', ''],
];

let openIeee = null;

function device() {
    return (state.devices || []).find(d => d.ieee === openIeee);
}

function fmt(key, v, unit) {
    if (key === 'contact') return v ? 'closed' : 'open';
    if (typeof v === 'boolean') return v ? 'yes' : 'no';
    if (typeof v === 'number') return `${Math.round(v * 100) / 100}${unit ? ' ' + unit : ''}`;
    return escapeHtml(v);
}

function readingsHtml(s) {
    const rows = [];
    for (const [key, label, unit] of READINGS) {
        for (const k of Object.keys(s).filter(k => k === key || new RegExp(`^${key}_\\d+$`).test(k)).sort()) {
            const ch = k === key ? '' : ` · ${k.slice(key.length + 1)}`;
            rows.push(`<div class="col-6 col-md-3"><div class="border rounded p-2 h-100">
                <div class="small text-muted">${label}${ch}</div>
                <div class="fw-semibold">${fmt(key, s[k], unit)}</div></div></div>`);
        }
    }
    return rows.length ? `<div class="row g-2 mb-3">${rows.join('')}</div>` : '';
}

function channelState(s, ep, multi) {
    const sfx = multi ? `_${ep}` : '';
    return { on: s[`on${sfx}`], level: s[`level${sfx}`], position: s[`position${sfx}`] };
}

function controlsHtml(d) {
    const controls = d.controls || [];
    if (!controls.length) return '<div class="text-muted small">This device reports readings only.</div>';
    const eps = [...new Set(controls.filter(c => c.endpoint_id).map(c => c.endpoint_id))];
    const multi = eps.length > 1;
    const groups = eps.map(ep => {
        const cs = controls.filter(c => c.endpoint_id === ep);
        const st = channelState(d.state || {}, ep, multi);
        // Controls are labelled "<channel name> On"; the name set on the device heads the group.
        const named = cs[0]?.label.replace(/ (On|Off|Toggle|Open|Close|Stop|Position|Brightness)$/, '');
        const heading = named && named !== cs[0].label ? named : `Channel ${ep}`;
        const title = multi ? `<div class="small fw-semibold mb-1">${escapeHtml(heading)}
            ${st.on === undefined ? '' : `<span class="badge ${st.on ? 'bg-success' : 'bg-secondary'} ms-1">${st.on ? 'on' : 'off'}</span>`}</div>` : '';
        const buttons = cs.filter(c => c.type !== 'slider').map(c => `
            <button type="button" class="btn btn-sm ${c.command === 'on' ? 'btn-success' : c.command === 'off' ? 'btn-secondary' : 'btn-outline-primary'} lan-cmd"
                data-cmd="${escapeHtml(c.command)}" data-ep="${ep}">${escapeHtml(c.label.replace(/^.* (?=(On|Off|Toggle|Open|Close|Stop)$)/, ''))}</button>`).join('');
        const sliders = cs.filter(c => c.type === 'slider').map(c => {
            const cur = c.command === 'brightness' ? st.level : c.command === 'position' ? st.position : undefined;
            const id = `lan_${c.command}_${ep}`;
            return `<label class="form-label small mb-0 mt-2" for="${id}">${escapeHtml(c.label)}
                    <span class="text-muted" id="${id}_v">${cur ?? ''}</span></label>
                <input type="range" class="form-range lan-slider" id="${id}" min="${c.min}" max="${c.max}"
                    value="${cur ?? c.min}" data-cmd="${escapeHtml(c.command)}" data-ep="${ep}">`;
        }).join('');
        return `<div class="mb-3">${title}<div class="d-flex flex-wrap gap-2">${buttons}</div>${sliders}</div>`;
    }).join('');
    const presses = controls.filter(c => c.command === 'press').map(c => `
        <button type="button" class="btn btn-sm btn-outline-secondary lan-cmd" data-cmd="press"
            data-value="${escapeHtml(c.value)}">${escapeHtml(c.label)}</button>`).join('');
    return groups + (presses ? `<div class="d-flex flex-wrap gap-2">${presses}</div>` : '');
}

function render() {
    const d = device();
    const body = document.getElementById('capModalBody');
    if (!d || !body) return;
    const title = document.querySelector('#capModal .modal-title');
    if (title) title.textContent = d.friendly_name;
    const online = d.available === false
        ? '<span class="badge bg-danger">offline</span>'
        : d.available ? '<span class="badge bg-success">online</span>' : '<span class="badge bg-secondary">connecting</span>';
    // Don't reset a slider being dragged.
    if (body.contains(document.activeElement) && document.activeElement.classList.contains('lan-slider')) return;
    body.innerHTML = `
        <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
            ${online}
            <span class="text-muted small">${escapeHtml(d.manufacturer)} ${escapeHtml(d.model || '')} · ${escapeHtml((d.ip_addresses || [])[0] || '')}</span>
        </div>
        ${readingsHtml(d.state || {})}
        ${controlsHtml(d)}`;
    body.querySelectorAll('.lan-cmd').forEach(b => b.addEventListener('click', async () => {
        b.disabled = true;
        await send(b.dataset.cmd, b.dataset.value ?? null, Number(b.dataset.ep) || null);
        b.disabled = false;
    }));
    body.querySelectorAll('.lan-slider').forEach(r => {
        r.addEventListener('input', () => { const v = document.getElementById(`${r.id}_v`); if (v) v.textContent = r.value; });
        r.addEventListener('change', () => send(r.dataset.cmd, Number(r.value), Number(r.dataset.ep) || null));
    });
}

async function send(command, value, endpoint) {
    try {
        const r = await fetch('/api/device/command', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee: openIeee, command, value, endpoint }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || data.success === false) window.toast?.error(data.error || data.detail || `Failed (${r.status})`);
    } catch (e) {
        window.toast?.error(e.message);
    }
}

function onUpdate(ev) {
    if (ev.detail?.ieee === openIeee) render();
}

export function openLanDeviceModal(ieee) {
    const modalEl = document.getElementById('capModal');
    if (!modalEl) return;
    openIeee = ieee;
    render();
    window.addEventListener('zmm-device-updated', onUpdate);
    modalEl.addEventListener('hidden.bs.modal', () => {
        window.removeEventListener('zmm-device-updated', onUpdate);
        openIeee = null;
    }, { once: true });
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
}
