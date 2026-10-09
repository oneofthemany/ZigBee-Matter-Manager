/**
 * HomeKit TV modal — a remote for a paired HomeKit television (local HAP).
 * Controls render from the `capabilities` block, since TVs expose different
 * subsets of the Television service. Reuses the #capModal shell.
 */

const log = zmmLog('homekit-modal');

let currentDeviceId = null;

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const api = id => `/api/homekit/devices/${encodeURIComponent(id)}`;

export async function openHomekitModal(deviceId) {
    const modalEl = document.getElementById('capModal');
    const body = document.getElementById('capModalBody');
    if (!modalEl || !body) return;
    currentDeviceId = deviceId;
    body.innerHTML = `<div class="text-center text-muted py-5">
        <i class="fas fa-spinner fa-spin me-2"></i>Connecting to the TV…</div>`;
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
    await refreshHomekitModal(deviceId, 0);
}

export async function refreshHomekitModal(deviceId, maxAge = 0) {
    const body = document.getElementById('capModalBody');
    if (!body || deviceId !== currentDeviceId) return null;
    try {
        const res = await fetch(`${api(deviceId)}/status?max_age=${maxAge}`).then(r => r.json());
        if (!res.success) throw new Error(res.error || 'status failed');
        if (deviceId === currentDeviceId) renderHomekitModal(res.status);
        return res;
    } catch (e) {
        log.error('HomeKit modal status failed:', e);
        if (deviceId === currentDeviceId) {
            body.innerHTML = `<div class="alert alert-danger">${esc(e.message)}</div>`;
        }
        return null;
    }
}

// Remote keys fire-and-forget: re-rendering after each press would rebuild the
// d-pad under the user's finger. State-changing controls re-render.
async function homekitControl(changes, { render = true } = {}) {
    const deviceId = currentDeviceId;
    try {
        const res = await fetch(`${api(deviceId)}/control`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(changes),
        }).then(r => r.json());
        if (!res.success) throw new Error(res.error || 'control failed');
        if (render && deviceId === currentDeviceId) renderHomekitModal(res.status);
    } catch (e) {
        window.toast?.error?.(`TV control failed: ${e.message}`);
    }
}

function keyBtn(key, icon, title, extra = '') {
    return `<button class="btn btn-outline-secondary ${extra}" data-hk-key="${key}" title="${title}"
                    aria-label="${title}" style="min-width:3rem;min-height:3rem">
                <i class="fas ${icon}"></i></button>`;
}

function renderHomekitModal(d) {
    const body = document.getElementById('capModalBody');
    if (!body) return;
    const caps = d.capabilities || {};
    const on = !!d.power;

    const inputs = (d.inputs || []).map(i =>
        `<option value="${i.id}" ${i.id === d.input ? 'selected' : ''}>${esc(i.name)}</option>`).join('');

    // Grid d-pad: fixed 3×3 cells keep it square at phone width.
    const dpad = caps.remote ? `
        <div class="mb-3">
            <div class="mx-auto" style="display:grid;grid-template-columns:repeat(3,3.25rem);
                 grid-template-rows:repeat(3,3.25rem);gap:0.35rem;justify-content:center;width:max-content">
                <span></span>${keyBtn('up', 'fa-chevron-up', 'Up')}<span></span>
                ${keyBtn('left', 'fa-chevron-left', 'Left')}
                ${keyBtn('select', 'fa-circle', 'OK', 'btn-primary text-white')}
                ${keyBtn('right', 'fa-chevron-right', 'Right')}
                <span></span>${keyBtn('down', 'fa-chevron-down', 'Down')}<span></span>
            </div>
            <div class="d-flex flex-wrap justify-content-center gap-2 mt-2">
                ${keyBtn('back', 'fa-undo', 'Back')}
                ${keyBtn('exit', 'fa-home', 'Exit')}
                ${keyBtn('info', 'fa-info', 'Info')}
                ${keyBtn('play_pause', 'fa-play', 'Play / pause')}
            </div>
        </div>` : '';

    const volume = (caps.volume_step || caps.mute) ? `
        <div class="mb-3 d-flex justify-content-center align-items-center gap-2">
            ${caps.volume_step ? `<button class="btn btn-outline-secondary" data-hk-vol="down"
                title="Volume down" aria-label="Volume down" style="min-width:3rem;min-height:3rem">
                <i class="fas fa-volume-down"></i></button>` : ''}
            ${caps.mute ? `<button class="btn ${d.mute ? 'btn-warning' : 'btn-outline-secondary'}" id="hkmMute"
                title="${d.mute ? 'Unmute' : 'Mute'}" aria-label="Mute" style="min-width:3rem;min-height:3rem">
                <i class="fas ${d.mute ? 'fa-volume-mute' : 'fa-volume-off'}"></i></button>` : ''}
            ${caps.volume_step ? `<button class="btn btn-outline-secondary" data-hk-vol="up"
                title="Volume up" aria-label="Volume up" style="min-width:3rem;min-height:3rem">
                <i class="fas fa-volume-up"></i></button>` : ''}
        </div>` : '';

    body.innerHTML = `
        <div class="mb-3 d-flex justify-content-between align-items-center">
            <div>
                <h5 class="mb-0">${esc(d.name || d.id)}</h5>
                <div class="text-muted small">
                    <span class="badge bg-info me-1">HomeKit</span>
                    <span class="badge bg-secondary me-1">${esc(d.manufacturer)} ${esc(d.model)}</span>
                </div>
            </div>
            <div class="text-end">
                ${d.stale
                    ? `<span class="badge bg-warning text-dark" title="${esc(d.error || '')}">Last known</span>`
                    : `<span class="badge ${on ? 'bg-success' : 'bg-secondary'}">${on ? 'On' : 'Standby'}</span>`}
                <div><button class="btn btn-sm btn-link py-0" id="hkmRefresh" title="Refresh now">
                    <i class="fas fa-sync-alt"></i></button></div>
            </div>
        </div>
        ${d.stale ? `<div class="alert alert-warning small py-2">
            Couldn't reach the TV just now — showing the last state. ${esc(d.error || '')}</div>` : ''}
        ${caps.power ? `
        <div class="mb-3">
            <button class="btn ${on ? 'btn-danger' : 'btn-success'}" id="hkmPower">
                <i class="fas fa-power-off me-1"></i>${on ? 'Turn off' : 'Turn on'}</button>
        </div>` : ''}
        ${caps.input ? `
        <div class="mb-3">
            <label class="small text-muted mb-1" for="hkmInput"><i class="fas fa-sign-in-alt me-1"></i>Input</label>
            <select class="form-select" id="hkmInput" ${on ? '' : 'disabled'}>${inputs}</select>
        </div>` : ''}
        ${dpad}
        ${volume}
        <div class="text-muted" style="font-size:0.7rem">
            Controlled locally over HomeKit — no Apple account or cloud involved.</div>`;

    body.querySelector('#hkmRefresh')?.addEventListener('click', () => refreshHomekitModal(d.id, 0));
    body.querySelector('#hkmPower')?.addEventListener('click', () => homekitControl({ power: !on }));
    body.querySelector('#hkmInput')?.addEventListener('change',
        e => homekitControl({ input: Number(e.target.value) }));
    body.querySelectorAll('[data-hk-key]').forEach(b => b.addEventListener('click',
        () => homekitControl({ key: b.dataset.hkKey }, { render: false })));
    body.querySelectorAll('[data-hk-vol]').forEach(b => b.addEventListener('click',
        () => homekitControl({ volume_step: b.dataset.hkVol }, { render: false })));
    body.querySelector('#hkmMute')?.addEventListener('click', () => homekitControl({ mute: !d.mute }));
}
