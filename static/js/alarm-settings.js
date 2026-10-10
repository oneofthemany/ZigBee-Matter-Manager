/* Settings -> Security: house mode and alarm setup (admin).
   Backend: modules/house_mode.py, modules/alarm.py — docs/house-mode-and-alarm.md. */

import { state } from './state.js';
import { escapeHtml } from './utils.js';

const MODES = ['home', 'away', 'night'];
let cfg = null, house = null, users = [], workers = [];

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

const isAdmin = () => !!window.zmmAuth?.hasScope?.('admin');
const name = d => d.friendly_name || d.ieee;

// Anything reporting contact, motion or tamper can be a zone.
function sensorDevices() {
    return (state.devices || []).filter(d => {
        const s = d.state || {};
        return ['contact', 'occupancy', 'motion', 'presence', 'tamper'].some(k => k in s);
    }).sort((a, b) => name(a).localeCompare(name(b)));
}

// Sirens are switched on and off, so anything with on/off state.
function switchDevices() {
    return (state.devices || []).filter(d => 'state' in (d.state || {}) && d.type !== 'Coordinator')
        .sort((a, b) => name(a).localeCompare(name(b)));
}

function houseCard() {
    const modeWorkers = workers.filter(w => w.type === 'mode');
    return `
    <div class="card shadow-sm mb-3">
        <div class="card-header bg-light py-2"><span class="fw-bold"><i class="fas fa-house me-1"></i> House mode</span></div>
        <div class="card-body">
            <p class="small text-muted">House mode is a Mode worker (Automations → Workers) that the header
                switch, presence and the alarm all share. Rules read and set it like any worker.</p>
            <div class="row g-2 align-items-end">
                <div class="col-12 col-md-5">
                    <label class="form-label small mb-1" for="hm_worker">Worker</label>
                    <select class="form-select form-select-sm" id="hm_worker">
                        <option value="">— none —</option>
                        ${modeWorkers.map(w => `<option value="${escapeHtml(w.id)}" ${w.id === house.worker ? 'selected' : ''}>
                            ${escapeHtml(w.name)} (${escapeHtml((w.options || []).join(', '))})</option>`).join('')}
                    </select>
                </div>
                <div class="col-12 col-md-auto">
                    ${house.configured ? '' : '<button class="btn btn-outline-primary btn-sm" id="hm_create">Create one</button>'}
                </div>
            </div>
            <div class="form-check form-switch mt-3">
                <input class="form-check-input" type="checkbox" id="hm_follow" ${house.follow_presence ? 'checked' : ''}>
                <label class="form-check-label small" for="hm_follow">Follow presence — <strong>away</strong> when
                    everyone has been out for
                    <input type="number" class="form-control form-control-sm d-inline-block mx-1" id="hm_after"
                           min="0" max="240" value="${escapeHtml(house.away_after_minutes)}" style="width:5rem"> min
                    (only from home), <strong>home</strong> when someone arrives (from away or holiday).</label>
            </div>
            <button class="btn btn-primary btn-sm mt-3" id="hm_save">Save house mode</button>
        </div>
    </div>`;
}

function alarmCard() {
    const zones = new Map(cfg.zones.map(z => [z.ieee, z]));
    const sensors = sensorDevices();
    // Configured zones whose device isn't in the list (offline/removed) still show.
    for (const z of cfg.zones) if (!sensors.some(d => d.ieee === z.ieee)) sensors.push({ ieee: z.ieee, friendly_name: z.ieee });
    const zoneRows = sensors.map(d => {
        const z = zones.get(d.ieee);
        const id = escapeHtml(d.ieee);
        return `<tr data-ieee="${id}">
            <td class="small text-break">${escapeHtml(name(d))}</td>
            ${MODES.map(m => `<td class="text-center"><input class="form-check-input al-zone" type="checkbox" data-mode="${m}"
                aria-label="${escapeHtml(name(d))} armed ${m}" ${z && z.modes.includes(m) ? 'checked' : ''}></td>`).join('')}
            <td class="text-center"><input class="form-check-input al-entry" type="checkbox"
                aria-label="${escapeHtml(name(d))} uses the entry delay" ${z && z.entry ? 'checked' : ''}></td>
        </tr>`;
    }).join('');
    const sirens = switchDevices();
    for (const i of cfg.sirens) if (!sirens.some(d => d.ieee === i)) sirens.push({ ieee: i, friendly_name: i });
    const num = (id, label, value, max) => `
        <div class="col-6 col-md-3">
            <label class="form-label small mb-1" for="${id}">${label}</label>
            <input type="number" class="form-control form-control-sm" id="${id}" min="0" max="${max}" value="${escapeHtml(value)}">
        </div>`;
    const flag = (id, label, on) => `
        <div class="form-check form-switch">
            <input class="form-check-input" type="checkbox" id="${id}" ${on ? 'checked' : ''}>
            <label class="form-check-label small" for="${id}">${label}</label>
        </div>`;
    return `
    <div class="card shadow-sm mb-3">
        <div class="card-header bg-light py-2"><span class="fw-bold"><i class="fas fa-shield-halved me-1"></i> Alarm</span></div>
        <div class="card-body">
            <h6>Zones</h6>
            <p class="small text-muted mb-2">Tick the armed modes each sensor watches. <em>Entry</em> sensors (the front
                door) give the entry delay to disarm; the rest go off at once.</p>
            ${sensors.length ? `<div class="table-responsive"><table class="table table-sm align-middle mb-3">
                <thead><tr><th>Sensor</th>${MODES.map(m => `<th class="text-center small">${m}</th>`).join('')}
                <th class="text-center small">Entry</th></tr></thead>
                <tbody id="al_zones">${zoneRows}</tbody></table></div>`
                : '<div class="small text-muted mb-3">No contact, motion or tamper sensors found.</div>'}
            <h6>Timing</h6>
            <div class="row g-2 mb-3">
                ${num('al_exit_away', 'Exit delay, away (s)', cfg.exit_delay_s.away, 600)}
                ${num('al_exit_home', 'Exit delay, home (s)', cfg.exit_delay_s.home, 600)}
                ${num('al_exit_night', 'Exit delay, night (s)', cfg.exit_delay_s.night, 600)}
                ${num('al_entry', 'Entry delay (s)', cfg.entry_delay_s, 600)}
                ${num('al_siren', 'Siren time (min)', cfg.siren_minutes, 30)}
            </div>
            <h6>Sirens</h6>
            <p class="small text-muted mb-1">Switched on while the alarm sounds, off when it stops or is disarmed.</p>
            <select class="form-select form-select-sm mb-3" id="al_sirens" multiple size="${Math.min(6, Math.max(3, sirens.length))}">
                ${sirens.map(d => `<option value="${escapeHtml(d.ieee)}" ${cfg.sirens.includes(d.ieee) ? 'selected' : ''}>${escapeHtml(name(d))}</option>`).join('')}
            </select>
            <h6>Who is alerted</h6>
            <div class="mb-1 small text-muted">Push and Other channels, urgent. None ticked = everyone.</div>
            <div class="d-flex flex-wrap gap-3 mb-3" id="al_users">
                ${users.map(u => `<div class="form-check"><input class="form-check-input" type="checkbox" id="al_u_${escapeHtml(u)}"
                    value="${escapeHtml(u)}" ${cfg.notify_users.includes(u) ? 'checked' : ''}>
                    <label class="form-check-label small" for="al_u_${escapeHtml(u)}">${escapeHtml(u)}${cfg.pins_set.includes(u) ? '' : ' <span class="text-muted">(no PIN)</span>'}</label></div>`).join('')}
            </div>
            <h6>Rules</h6>
            ${flag('al_follow', 'House mode <strong>away</strong>/<strong>holiday</strong> or <strong>night</strong> arms the alarm (it never disarms it)', cfg.follow_house_mode)}
            ${flag('al_setmode', 'Arming and disarming set the house mode', cfg.set_house_mode)}
            ${flag('al_armpin', 'Arming needs a PIN too', cfg.arm_requires_pin)}
            ${flag('al_autodisarm', '<span class="text-danger">Let automations disarm</span> — anyone who can edit rules could then switch the alarm off', cfg.allow_automation_disarm)}
            <button class="btn btn-primary btn-sm mt-3" id="al_save">Save alarm</button>
            ${cfg.pins_set.length ? `<h6 class="mt-4">PINs</h6><div class="small text-muted mb-1">For a forgotten PIN: clear it and the person sets a new one from the header.</div>
                <div class="d-flex flex-wrap gap-2">${cfg.pins_set.map(u => `<button class="btn btn-outline-danger btn-sm al-clear" data-user="${escapeHtml(u)}">
                    Clear ${escapeHtml(u)}</button>`).join('')}</div>` : ''}
        </div>
    </div>`;
}

function collectAlarm() {
    const zones = [];
    document.querySelectorAll('#al_zones tr').forEach(tr => {
        const modes = [...tr.querySelectorAll('.al-zone')].filter(c => c.checked).map(c => c.dataset.mode);
        if (modes.length) zones.push({ ieee: tr.dataset.ieee, modes, entry: tr.querySelector('.al-entry').checked });
    });
    const n = id => Number(document.getElementById(id).value) || 0;
    const on = id => document.getElementById(id).checked;
    return {
        zones,
        sirens: [...document.getElementById('al_sirens').selectedOptions].map(o => o.value),
        exit_delay_s: { away: n('al_exit_away'), home: n('al_exit_home'), night: n('al_exit_night') },
        entry_delay_s: n('al_entry'),
        siren_minutes: n('al_siren'),
        notify_users: [...document.querySelectorAll('#al_users input:checked')].map(c => c.value),
        follow_house_mode: on('al_follow'),
        set_house_mode: on('al_setmode'),
        arm_requires_pin: on('al_armpin'),
        allow_automation_disarm: on('al_autodisarm'),
    };
}

function render(host) {
    host.innerHTML = houseCard() + alarmCard();
    const click = (id, fn) => document.getElementById(id)?.addEventListener('click', fn);
    click('hm_create', async () => {
        try { house = await api('POST', '/api/house/mode/config/create-worker'); await load(host); window.toast?.success('House mode created'); }
        catch (e) { window.toast?.error(e.message); }
    });
    click('hm_save', async () => {
        try {
            house = await api('PUT', '/api/house/mode/config', {
                worker: document.getElementById('hm_worker').value,
                follow_presence: document.getElementById('hm_follow').checked,
                away_after_minutes: Number(document.getElementById('hm_after').value) || 0,
            });
            window.toast?.success('House mode saved');
        } catch (e) { window.toast?.error(`Couldn't save: ${e.message}`); }
    });
    click('al_save', async () => {
        try { cfg = await api('PUT', '/api/alarm/config', collectAlarm()); render(host); window.toast?.success('Alarm saved'); }
        catch (e) { window.toast?.error(`Couldn't save: ${e.message}`); }
    });
    host.querySelectorAll('.al-clear').forEach(b => b.addEventListener('click', async () => {
        try { await api('DELETE', `/api/alarm/pin/${encodeURIComponent(b.dataset.user)}`); await load(host); }
        catch (e) { window.toast?.error(e.message); }
    }));
}

async function load(host) {
    try {
        [cfg, house, users, workers] = await Promise.all([
            api('GET', '/api/alarm/config'),
            api('GET', '/api/house/mode'),
            api('GET', '/api/auth/users').then(r => (r.users || []).map(u => u.username)).catch(() => []),
            api('GET', '/api/workers').then(r => r.workers || r || []).catch(() => []),
        ]);
        render(host);
    } catch (e) {
        host.innerHTML = `<div class="alert alert-danger small">Couldn't load alarm setup: ${escapeHtml(e.message)}</div>`;
    }
}

export function initAlarmSettings() {
    const tab = document.querySelector('[data-bs-target="#settingsSecurity"]');
    tab?.addEventListener('shown.bs.tab', () => {
        const host = document.getElementById('alarm-settings-host');
        if (host && isAdmin()) load(host);
    });
}
