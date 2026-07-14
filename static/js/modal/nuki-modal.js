/**
 * Nuki Lock Modal (bridge-paired locks; pseudo-ieee nuki_<id>)
 * Opened from the device list via device-modal.js when the entry carries
 * nuki_lock_id. Matter-commissioned locks use the standard device modal.
 * Two tabs: Control (live state + actions) and Automation — the latter
 * reuses the full rule builder from ./automation.js, so lock rules
 * (time-based lock/unlock, locked-state conditions) work exactly like
 * any other device's.
 * Reuses the shared #capModal shell like ac-modal.js.
 */

import { renderAutomationTab, initAutomationTab } from './automation.js';

const log = zmmLog('nuki-modal');

let currentLockId = null;

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const STATE_BADGES = {
    'locked': 'bg-success',
    'unlocked': 'bg-warning text-dark',
    'unlatched': 'bg-warning text-dark',
    'motor blocked': 'bg-danger',
};

function _lockIeee(l) {
    return `nuki_${l.nuki_id}`;
}

export async function openNukiModal(lockId) {
    const modalEl = document.getElementById('capModal');
    const body = document.getElementById('capModalBody');
    if (!modalEl || !body) return;
    currentLockId = lockId;
    body.innerHTML = `<div class="text-center text-muted py-5">
        <i class="fas fa-spinner fa-spin me-2"></i>Contacting lock…</div>`;
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
    // Render instantly from the app-side cache (persisted across restarts),
    // then refresh live in the background if what we showed wasn't fresh.
    const res = await refreshNukiModal(lockId, 86400);
    const lock = res?.locks?.find(l => l.id === lockId);
    if (lock?.via === 'bridge' && (res.age_sec > 5 || lock.cached)) {
        refreshNukiModal(lockId);
    }
}

async function refreshNukiModal(lockId, maxAge = 0) {
    const body = document.getElementById('capModalBody');
    if (!body || lockId !== currentLockId) return null;
    try {
        const r = await fetch(`/api/security/nuki/locks?max_age=${maxAge}`);
        const res = await r.json();
        if (!res.success) throw new Error(res.error || 'status failed');
        const lock = (res.locks || []).find(l => l.id === lockId);
        if (!lock) throw new Error('Lock not found — is the bridge reachable?');
        renderNukiModal(lock);
        return res;
    } catch (e) {
        log.error('Nuki modal status failed:', e);
        body.innerHTML = `<div class="alert alert-danger">${esc(e.message)}</div>`;
        return null;
    }
}

async function nukiModalAction(action) {
    const lockId = currentLockId;
    const pane = document.getElementById('nukiCtlPane');
    pane?.querySelectorAll('button').forEach(el => { el.disabled = true; });
    const hint = document.getElementById('nukiModalHint');
    if (hint) hint.innerHTML = `<i class="fas fa-spinner fa-spin me-1"></i> Sending ${esc(action)}…`;
    try {
        const r = await fetch(`/api/security/nuki/locks/${encodeURIComponent(lockId)}/action`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action }),
        });
        const res = await r.json();
        if (!res.success) throw new Error(res.error || `${action} failed`);
        window.toast?.success?.('Nuki', `${action} sent`);
    } catch (e) {
        window.toast?.error?.('Nuki action failed', e.message);
    }
    await refreshNukiModal(lockId);
    // keep the settings pane's table in sync if it's on screen
    window.loadNukiLocks?.();
}
window.nukiModalAction = nukiModalAction;

function renderNukiModal(l) {
    const body = document.getElementById('capModalBody');
    if (!body || l.id !== currentLockId) return;

    // Build the tabbed shell once per modal open; later refreshes only
    // repaint the Control pane so the Automation tab keeps its state.
    if (!document.getElementById('nukiModalShell')) {
        const ieee = _lockIeee(l);
        body.innerHTML = `
        <div id="nukiModalShell">
          <div class="d-flex justify-content-between align-items-center mb-3">
            <div>
              <h5 class="mb-0"><i class="fas fa-lock me-2"></i>${esc(l.name)}</h5>
              <div class="text-muted small">${esc(l.device_type_name || 'Smart Lock')}
                ${l.firmware ? ' · fw ' + esc(l.firmware) : ''}</div>
            </div>
            <span class="badge bg-primary">via ${esc(l.via)}</span>
          </div>
          <ul class="nav nav-tabs mb-3">
            <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab"
                data-bs-target="#nukiTabControl">Control</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab"
                data-bs-target="#nukiTabAutomation">Automation</button></li>
          </ul>
          <div class="tab-content">
            <div class="tab-pane fade show active" id="nukiTabControl">
              <div id="nukiCtlPane"></div>
            </div>
            <div class="tab-pane fade" id="nukiTabAutomation">
              ${renderAutomationTab({ ieee })}
            </div>
          </div>
        </div>`;
        initAutomationTab(ieee);
    }

    const pane = document.getElementById('nukiCtlPane');
    if (!pane) return;
    const stateName = l.state_name || 'unknown';
    const badgeCls = STATE_BADGES[stateName.toLowerCase()] || 'bg-secondary';
    const actions = [
        { action: 'lock', label: 'Lock', icon: 'fa-lock', btn: 'success' },
        { action: 'unlock', label: 'Unlock', icon: 'fa-lock-open', btn: 'warning' },
        { action: 'unlatch', label: 'Unlatch', icon: 'fa-door-open', btn: 'danger',
          title: 'Also pulls the latch — the door swings open' },
        { action: 'lock_n_go', label: "Lock 'n' Go", icon: 'fa-walking', btn: 'primary',
          title: 'Unlocks now, relocks automatically after the Nuki-configured delay' },
    ];
    pane.innerHTML = `
    <div class="row g-3 mb-3 text-center">
      <div class="col-4">
        <div class="border rounded p-3">
          <div class="small text-muted mb-1">State</div>
          <span class="badge ${badgeCls} fs-6">${esc(stateName)}</span>
        </div>
      </div>
      <div class="col-4">
        <div class="border rounded p-3">
          <div class="small text-muted mb-1">Battery</div>
          <div class="fw-semibold">
            ${l.battery_critical
                ? '<span class="text-danger"><i class="fas fa-battery-empty me-1"></i>critical</span>'
                : '<i class="fas fa-battery-three-quarters me-1 text-success"></i>ok'}
            ${l.battery_charge != null ? ` · ${esc(l.battery_charge)}%` : ''}
          </div>
        </div>
      </div>
      <div class="col-4">
        <div class="border rounded p-3">
          <div class="small text-muted mb-1">Door Sensor</div>
          <div class="fw-semibold">${esc(l.door_state || 'n/a')}</div>
        </div>
      </div>
    </div>

    <div class="d-flex gap-2 flex-wrap justify-content-center mb-2">
      ${actions.map(a => `
        <button class="btn btn-outline-${a.btn}" ${l.available === false ? 'disabled' : ''}
                ${a.title ? `title="${esc(a.title)}"` : ''}
                onclick="window.nukiModalAction('${esc(a.action)}')">
          <i class="fas ${a.icon} me-1"></i> ${esc(a.label)}
        </button>`).join('')}
    </div>
    <div id="nukiModalHint" class="text-center text-muted small">
      ${l.cached
        ? '<i class="fas fa-history me-1"></i>cached state — refreshing…'
        : (l.last_updated ? 'Lock state as of ' + esc(l.last_updated) : '')}
    </div>
    `;
}
