/**
 * Blueair purifier / humidifier modal (cloud account API).
 * Controls are driven by the `capabilities` block in each device's status, so
 * both device generations render from one layout. Reuses the #capModal shell.
 */

const log = zmmLog('blueair-modal');

const TOGGLES = [
    { key: 'auto', label: 'Auto', icon: 'fa-magic' },
    { key: 'night_mode', label: 'Night', icon: 'fa-moon' },
    { key: 'child_lock', label: 'Child lock', icon: 'fa-child' },
    { key: 'germ_shield', label: 'Germ shield', icon: 'fa-shield-virus' },
];

const READINGS = [
    { key: 'pm2_5', label: 'PM2.5', unit: 'µg/m³' },
    { key: 'pm10', label: 'PM10', unit: 'µg/m³' },
    { key: 'pm1', label: 'PM1', unit: 'µg/m³' },
    { key: 'voc', label: 'VOC', unit: '' },
    { key: 'co2', label: 'CO₂', unit: 'ppm' },
    { key: 'temperature_c', label: 'Temp', unit: '°C' },
    { key: 'humidity_pct', label: 'Humidity', unit: '%' },
];

let currentDeviceId = null;

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export async function openBlueairModal(deviceId) {
    const modalEl = document.getElementById('capModal');
    const body = document.getElementById('capModalBody');
    if (!modalEl || !body) return;
    currentDeviceId = deviceId;
    body.innerHTML = `<div class="text-center text-muted py-5">
        <i class="fas fa-spinner fa-spin me-2"></i>Contacting Blueair…</div>`;
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
    // Cached status first (every live read is a cloud round trip), then a
    // fresh one in the background.
    const res = await refreshBlueairModal(deviceId, 86400);
    if (res) refreshBlueairModal(deviceId, 0);
}

export async function refreshBlueairModal(deviceId, maxAge = 0) {
    const body = document.getElementById('capModalBody');
    if (!body || deviceId !== currentDeviceId) return null;
    try {
        const res = await fetch(`/api/blueair/devices/${encodeURIComponent(deviceId)}/status?max_age=${maxAge}`)
            .then(r => r.json());
        if (!res.success) throw new Error(res.error || 'status failed');
        if (deviceId === currentDeviceId) renderBlueairModal(res.status);
        return res;
    } catch (e) {
        log.error('Blueair modal status failed:', e);
        if (deviceId === currentDeviceId) {
            body.innerHTML = `<div class="alert alert-danger">${esc(e.message)}</div>`;
        }
        return null;
    }
}

async function blueairControl(changes) {
    const deviceId = currentDeviceId;
    const body = document.getElementById('capModalBody');
    body?.querySelectorAll('button, input').forEach(el => { el.disabled = true; });
    try {
        const res = await fetch(`/api/blueair/devices/${encodeURIComponent(deviceId)}/control`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(changes),
        }).then(r => r.json());
        if (!res.success) throw new Error(res.error || 'control failed');
        if (deviceId === currentDeviceId) renderBlueairModal(res.status);
    } catch (e) {
        window.toast?.error?.('Blueair control failed', e.message);
        await refreshBlueairModal(deviceId);
    }
}

function renderBlueairModal(d) {
    const body = document.getElementById('capModalBody');
    if (!body) return;
    const caps = d.capabilities || {};
    const on = !!d.power;

    const readings = READINGS.filter(r => d[r.key] != null).map(r => `
        <div class="border rounded px-2 py-1 text-center" style="min-width:5.5rem">
            <div class="small text-muted">${r.label}</div>
            <div class="fw-semibold">${esc(d[r.key])}<span class="small text-muted ms-1">${r.unit}</span></div>
        </div>`).join('');

    let filter = '';
    if (d.filter_usage_pct != null) {
        const pct = Math.max(0, Math.min(100, Number(d.filter_usage_pct)));
        const bar = pct >= 90 ? 'bg-danger' : pct >= 70 ? 'bg-warning' : 'bg-success';
        filter = `
            <div class="small text-muted mb-1">Filter used</div>
            <div class="progress mb-3" style="height:0.9rem" title="${pct}% used">
                <div class="progress-bar ${bar}" style="width:${pct}%">${pct}%</div>
            </div>`;
    } else if (d.filter_expired != null) {
        filter = `<div class="mb-3 small">${d.filter_expired
            ? '<span class="badge bg-danger">Filter needs replacing</span>'
            : '<span class="badge bg-success">Filter OK</span>'}</div>`;
    }

    const toggles = TOGGLES.filter(t => caps[t.key]).map(t => {
        const active = !!d[t.key];
        return `<button class="btn btn-sm ${active ? 'btn-primary' : 'btn-outline-secondary'}"
                        data-ba-toggle="${t.key}" data-ba-on="${active ? 1 : 0}"
                        ${on || t.key === 'child_lock' ? '' : 'disabled'}>
                    <i class="fas ${t.icon} me-1"></i>${t.label}</button>`;
    }).join('');

    const fanPct = d.fan_speed_pct ?? 0;
    const brightMax = caps.brightness_max || 100;

    body.innerHTML = `
        <div class="mb-3 d-flex justify-content-between align-items-center">
            <div>
                <h5 class="mb-0">${esc(d.name || d.id)}</h5>
                <div class="text-muted small">
                    <span class="badge bg-info me-1">Cloud</span>
                    <span class="badge bg-secondary me-1">${esc(d.model)}</span>
                </div>
            </div>
            <div class="text-end">
                ${d.stale
                    ? `<span class="badge bg-warning text-dark" title="${esc(d.error || '')}">Last known</span>`
                    : '<span class="badge bg-success">Online</span>'}
                <div><button class="btn btn-sm btn-link py-0" id="bamRefresh" title="Refresh now">
                    <i class="fas fa-sync-alt"></i></button></div>
            </div>
        </div>
        ${d.stale ? `<div class="alert alert-warning small py-2">
            Couldn't reach Blueair just now — showing the last reading. ${esc(d.error || '')}</div>` : ''}
        ${readings ? `<div class="d-flex flex-wrap gap-2 mb-3">${readings}</div>` : ''}
        ${filter}
        ${caps.power ? `
        <div class="mb-3">
            <button class="btn ${on ? 'btn-danger' : 'btn-success'}" id="bamPower">
                <i class="fas fa-power-off me-1"></i>${on ? 'Turn off' : 'Turn on'}</button>
        </div>` : ''}
        ${caps.fan_speed ? `
        <div class="mb-3">
            <label class="small text-muted mb-1 d-flex justify-content-between" for="bamFan">
                <span><i class="fas fa-fan me-1"></i>Fan speed${d.auto ? ' (auto — moving this switches auto off)' : ''}</span>
                <span id="bamFanLbl">${fanPct}%</span></label>
            <input type="range" class="form-range" id="bamFan" min="0" max="100" step="1"
                   value="${fanPct}" ${on ? '' : 'disabled'}>
        </div>` : ''}
        ${toggles ? `
        <div class="mb-3">
            <div class="small text-muted mb-1">Features</div>
            <div class="d-flex flex-wrap gap-2">${toggles}</div>
        </div>` : ''}
        ${caps.brightness ? `
        <div class="mb-3">
            <label class="small text-muted mb-1 d-flex justify-content-between" for="bamBright">
                <span><i class="fas fa-lightbulb me-1"></i>Display brightness</span>
                <span id="bamBrightLbl">${d.brightness ?? 0}</span></label>
            <input type="range" class="form-range" id="bamBright" min="0" max="${brightMax}" step="1"
                   value="${d.brightness ?? 0}">
        </div>` : ''}
        <div class="text-muted" style="font-size:0.7rem">
            Controlled through the Blueair cloud — changes take a few seconds to reach the device.</div>`;

    body.querySelector('#bamRefresh')?.addEventListener('click',
        () => refreshBlueairModal(d.id, 0));
    body.querySelector('#bamPower')?.addEventListener('click',
        () => blueairControl({ power: !on }));
    body.querySelectorAll('[data-ba-toggle]').forEach(b => b.addEventListener('click',
        () => blueairControl({ [b.dataset.baToggle]: b.dataset.baOn !== '1' })));

    const fan = body.querySelector('#bamFan');
    fan?.addEventListener('input', () => { body.querySelector('#bamFanLbl').textContent = `${fan.value}%`; });
    fan?.addEventListener('change', () => {
        const changes = { fan_speed_pct: Number(fan.value) };
        // A manual speed only sticks with auto mode off.
        if (d.auto && caps.auto) changes.auto = false;
        blueairControl(changes);
    });

    const bright = body.querySelector('#bamBright');
    bright?.addEventListener('input', () => { body.querySelector('#bamBrightLbl').textContent = bright.value; });
    bright?.addEventListener('change', () => blueairControl({ brightness: Number(bright.value) }));
}
