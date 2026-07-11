/**
 * AC Unit Modal (WiFi — Gree / Midea local LAN)
 * Capability-aware controls driven by the `capabilities` block each unit
 * reports in /api/ac/units status. Reuses the shared #capModal shell.
 */

const log = zmmLog('ac-modal');

const EXTRA_LABELS = {
    turbo: 'Turbo', quiet: 'Quiet', xfan: 'X-Fan', light: 'Light',
    sleep: 'Sleep', anion: 'Ioniser', eco: 'Eco', display: 'Display',
    indirect_wind: 'Indirect wind',
};
const EXTRA_ICONS = {
    turbo: 'fa-rocket', quiet: 'fa-volume-mute', xfan: 'fa-wind',
    light: 'fa-lightbulb', sleep: 'fa-moon', anion: 'fa-leaf',
    eco: 'fa-seedling', display: 'fa-tv', indirect_wind: 'fa-random',
};
const MODE_ICONS = {
    auto: 'fa-magic', cool: 'fa-snowflake', dry: 'fa-tint',
    fan: 'fa-fan', heat: 'fa-sun',
};

let currentUnitId = null;

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export async function openAcModal(unitId) {
    const modalEl = document.getElementById('capModal');
    const body = document.getElementById('capModalBody');
    if (!modalEl || !body) return;
    currentUnitId = unitId;
    body.innerHTML = `<div class="text-center text-muted py-5">
        <i class="fas fa-spinner fa-spin me-2"></i>Contacting AC unit…</div>`;
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
    await refreshAcModal(unitId);
}

export async function refreshAcModal(unitId) {
    const body = document.getElementById('capModalBody');
    if (!body || unitId !== currentUnitId) return;
    try {
        const res = await fetch(`/api/ac/units/${encodeURIComponent(unitId)}/status`)
            .then(r => r.json());
        if (!res.success) throw new Error(res.error || 'status failed');
        renderAcModal(res.status);
    } catch (e) {
        log.error('AC modal status failed:', e);
        body.innerHTML = `<div class="alert alert-danger">${esc(e.message)}</div>`;
    }
}

async function acModalControl(changes) {
    const unitId = currentUnitId;
    const body = document.getElementById('capModalBody');
    body?.querySelectorAll('button, input').forEach(el => { el.disabled = true; });
    try {
        const res = await fetch(`/api/ac/units/${encodeURIComponent(unitId)}/control`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(changes),
        }).then(r => r.json());
        if (!res.success) throw new Error(res.error || 'control failed');
        if (unitId === currentUnitId) renderAcModal(res.status);
        // keep the settings pane's table in sync if it's on screen
        window.loadAcUnits?.();
    } catch (e) {
        window.toast?.error?.('AC control failed', e.message);
        await refreshAcModal(unitId);
    }
}

function renderAcModal(u) {
    const body = document.getElementById('capModalBody');
    if (!body) return;
    const caps = u.capabilities || {};
    const minC = caps.min_c ?? 16;
    const maxC = caps.max_c ?? 31;
    const online = u.online !== false;
    const fanActive = (u.extras?.turbo && (caps.fan || []).includes('turbo'))
        ? 'turbo' : (u.fan || '');

    const header = `
        <div class="mb-3 d-flex justify-content-between align-items-center">
            <div>
                <h5 class="mb-0">${esc(u.name || u.id)}</h5>
                <div class="text-muted small">
                    <span class="badge bg-info me-1">WiFi</span>
                    <span class="badge bg-secondary me-1">${esc(u.brand)}</span>
                    ${u.room_id ? `<span class="badge bg-light text-dark border">room: ${esc(u.room_id)}</span>` : ''}
                </div>
            </div>
            <div class="text-end">
                ${online
                    ? '<span class="badge bg-success">Online</span>'
                    : `<span class="badge bg-secondary" title="${esc(u.error || '')}">Offline</span>`}
                <div class="small text-muted mt-1">
                    ${u.current_c != null ? `<i class="fas fa-home me-1"></i>${Number(u.current_c).toFixed(1)}°C` : ''}
                    ${u.outdoor_c != null ? `<span class="ms-2"><i class="fas fa-cloud me-1"></i>${Number(u.outdoor_c).toFixed(1)}°C</span>` : ''}
                </div>
            </div>
        </div>`;

    if (!online) {
        body.innerHTML = header + `
            <div class="alert alert-warning small">
                ${esc(u.error || 'Unit is not reachable.')}
            </div>
            <button class="btn btn-outline-warning btn-sm" id="acmBind">
                <i class="fas fa-key me-1"></i>Bind / reconnect</button>`;
        body.querySelector('#acmBind')?.addEventListener('click', async () => {
            await window.acBind?.(u.id);
            await refreshAcModal(u.id);
        });
        return;
    }

    const modeButtons = (caps.modes || ['auto', 'cool', 'dry', 'fan', 'heat']).map(m => `
        <button class="btn btn-sm ${u.power && u.mode === m ? 'btn-primary' : 'btn-outline-secondary'}"
                data-ac-mode="${m}" ${u.power ? '' : 'disabled'}>
            <i class="fas ${MODE_ICONS[m] || 'fa-cog'} me-1"></i>${m}</button>`).join('');

    const fanButtons = (caps.fan || ['auto', 'low', 'medium', 'high', 'turbo']).map(f => `
        <button class="btn btn-sm ${fanActive === f ? 'btn-primary' : 'btn-outline-secondary'}"
                data-ac-fan="${f}" ${u.power ? '' : 'disabled'}>${f}</button>`).join('');

    const swingToggles = [
        caps.swing_v ? { key: 'swing_v', label: 'Vertical swing', icon: 'fa-arrows-alt-v', on: !!u.swing_v } : null,
        caps.swing_h ? { key: 'swing_h', label: 'Horizontal swing', icon: 'fa-arrows-alt-h', on: !!u.swing_h } : null,
    ].filter(Boolean);

    const extraToggles = (caps.extras || [])
        .filter(k => EXTRA_LABELS[k])
        .map(k => ({ key: k, label: EXTRA_LABELS[k], icon: EXTRA_ICONS[k], on: !!(u.extras || {})[k] }));

    const toggleHtml = t => `
        <button class="btn btn-sm ${t.on ? 'btn-primary' : 'btn-outline-secondary'}"
                data-ac-toggle="${t.key}" data-ac-on="${t.on ? 1 : 0}" ${u.power ? '' : 'disabled'}>
            <i class="fas ${t.icon} me-1"></i>${t.label}</button>`;

    body.innerHTML = header + `
        <div class="d-flex align-items-center gap-3 mb-3">
            <button class="btn ${u.power ? 'btn-danger' : 'btn-success'}" id="acmPower">
                <i class="fas fa-power-off me-1"></i>${u.power ? 'Turn off' : 'Turn on'}</button>
            <div class="input-group input-group-sm" style="width:170px" title="Target ${minC}–${maxC}°C">
                <button class="btn btn-outline-secondary" id="acmTempDown" ${u.power ? '' : 'disabled'}>−</button>
                <input type="number" class="form-control text-center" id="acmTemp"
                       step="0.5" min="${minC}" max="${maxC}" value="${u.target_c ?? 22}"
                       ${u.power ? '' : 'disabled'}>
                <span class="input-group-text">°C</span>
                <button class="btn btn-outline-secondary" id="acmTempUp" ${u.power ? '' : 'disabled'}>+</button>
            </div>
        </div>
        <div class="mb-3">
            <div class="small text-muted mb-1">Mode</div>
            <div class="btn-group flex-wrap" role="group">${modeButtons}</div>
        </div>
        <div class="mb-3">
            <div class="small text-muted mb-1">Fan</div>
            <div class="btn-group flex-wrap" role="group">${fanButtons}</div>
        </div>
        ${swingToggles.length ? `
        <div class="mb-3">
            <div class="small text-muted mb-1">Swing</div>
            <div class="d-flex flex-wrap gap-2">${swingToggles.map(toggleHtml).join('')}</div>
        </div>` : ''}
        ${extraToggles.length ? `
        <div class="mb-3">
            <div class="small text-muted mb-1">Features</div>
            <div class="d-flex flex-wrap gap-2">${extraToggles.map(toggleHtml).join('')}</div>
        </div>` : ''}
        <div id="acmTimerSection"></div>
        ${caps.source ? `<div class="text-muted" style="font-size:0.7rem">
            capabilities: ${esc(caps.source)}${caps.min_c != null ? ` · setpoint ${minC}–${maxC}°C` : ''}</div>` : ''}`;

    // ── wire events ──
    body.querySelector('#acmPower')?.addEventListener('click',
        () => acModalControl({ power: !u.power }));
    body.querySelectorAll('[data-ac-mode]').forEach(b => b.addEventListener('click',
        () => acModalControl({ mode: b.dataset.acMode })));
    body.querySelectorAll('[data-ac-fan]').forEach(b => b.addEventListener('click',
        () => acModalControl({ fan: b.dataset.acFan })));
    body.querySelectorAll('[data-ac-toggle]').forEach(b => b.addEventListener('click',
        () => acModalControl({ [b.dataset.acToggle]: b.dataset.acOn !== '1' })));

    const tempInput = body.querySelector('#acmTemp');
    const sendTemp = v => {
        const t = Math.min(maxC, Math.max(minC, v));
        if (Number.isFinite(t)) acModalControl({ target_c: t });
    };
    body.querySelector('#acmTempUp')?.addEventListener('click',
        () => sendTemp(parseFloat(tempInput.value) + 1));
    body.querySelector('#acmTempDown')?.addEventListener('click',
        () => sendTemp(parseFloat(tempInput.value) - 1));
    tempInput?.addEventListener('change',
        () => sendTemp(parseFloat(tempInput.value)));

    renderTimerSection(u);
}

// ── App-side timers (/api/ac/units/{id}/timer*) ──
// Kept separate so a timer API failure never breaks the controls above.

function describeTimerChanges(c) {
    if (c && c.power === false) return 'Turn off';
    if (c && c.power === true) return 'Turn on';
    return Object.entries(c || {}).map(([k, v]) => `${k}=${v}`).join(', ') || '?';
}

function timerCountdown(atSec) {
    const mins = Math.max(0, Math.round((atSec * 1000 - Date.now()) / 60000));
    if (mins >= 60) {
        const h = Math.floor(mins / 60);
        const m = mins % 60;
        return m ? `${h}h ${m}m` : `${h}h`;
    }
    return `${mins}m`;
}

async function renderTimerSection(u) {
    const el = document.getElementById('acmTimerSection');
    if (!el) return;
    let timers = [];
    try {
        const res = await fetch(`/api/ac/units/${encodeURIComponent(u.id)}/timers`)
            .then(r => r.json());
        if (res.success) timers = res.timers || [];
    } catch (e) {
        log.warn('AC timers load failed:', e);
        return;    // leave the section empty rather than break the modal
    }

    el.innerHTML = `
        <div class="mb-3">
            <div class="small text-muted mb-1">Timer</div>
            ${timers.map(t => `
                <div class="d-flex align-items-center gap-2 small mb-1">
                    <i class="fas fa-hourglass-half text-muted"></i>
                    <span>${esc(describeTimerChanges(t.changes))}
                          in <strong>${timerCountdown(t.at)}</strong></span>
                    <button class="btn btn-sm btn-link text-danger py-0 px-1"
                            data-ac-timer-cancel="${esc(t.id)}" title="Cancel timer">
                        <i class="fas fa-times"></i></button>
                </div>`).join('')}
            <div class="d-flex align-items-center gap-2">
                <select class="form-select form-select-sm" id="acmTimerAction" style="width:auto">
                    <option value="off">Turn off</option>
                    <option value="on">Turn on</option>
                </select>
                <span class="small text-muted">in</span>
                <select class="form-select form-select-sm" id="acmTimerWhen" style="width:auto">
                    <option value="30">30 min</option>
                    <option value="60">1 h</option>
                    <option value="120" selected>2 h</option>
                    <option value="240">4 h</option>
                    <option value="480">8 h</option>
                </select>
                <button class="btn btn-sm btn-outline-primary" id="acmTimerAdd">
                    <i class="fas fa-plus me-1"></i>Set</button>
            </div>
        </div>`;

    el.querySelector('#acmTimerAdd')?.addEventListener('click', async () => {
        const minutes = parseFloat(el.querySelector('#acmTimerWhen').value);
        const on = el.querySelector('#acmTimerAction').value === 'on';
        try {
            const res = await fetch(`/api/ac/units/${encodeURIComponent(u.id)}/timer`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ in_minutes: minutes, changes: { power: on } }),
            }).then(r => r.json());
            if (!res.success) throw new Error(res.error || 'timer failed');
        } catch (e) {
            window.toast?.error?.('Timer failed', e.message);
        }
        renderTimerSection(u);
    });
    el.querySelectorAll('[data-ac-timer-cancel]').forEach(b =>
        b.addEventListener('click', async () => {
            try {
                await fetch(`/api/ac/timers/${encodeURIComponent(b.dataset.acTimerCancel)}`,
                    { method: 'DELETE' });
            } catch (e) { /* refresh shows the truth either way */ }
            renderTimerSection(u);
        }));
}
