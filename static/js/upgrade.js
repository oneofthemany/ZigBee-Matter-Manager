/**
 * upgrade.js
 * Settings tab — Upgrade Manager card
 *
 * Self-contained module. Add to main.js:
 *   import { initUpgrade } from './upgrade.js';
 *   initUpgrade();
 *
 * Requires a <div id="upgradeCardMount"></div> in the Settings tab
 * (see index.html snippet).
 */

import { confirmDialog } from './dialogs.js';

const log = zmmLog('upgrade');

let _pollTimer = null;
let _pollMs = 0;
let _logPollTimer = null;
let _lastState = null;
let _lastPayload = null;   // last /status body — skip re-render when unchanged
let _managerToken = null;  // fetched once; only changes when the sidecar regenerates it
// Versions from the last status render — fed to the DeLorean-bee time-travel
// overlay (deploy-animation.js) as the swap's "time circuits" readout.
let _versions = { present: '', destination: '' };

// Poll fast only while an upgrade is actually in flight; idle status
// almost never changes, and WS 'upgrade_*' events trigger an immediate
// refresh anyway.
const POLL_ACTIVE_MS = 5000;
const POLL_IDLE_MS = 30000;
const ACTIVE_STATES = ['checking', 'building', 'swapping', 'rolling_back', 'ready_to_swap'];

// ============================================================================
// INIT
// ============================================================================

export function initUpgrade() {
    // Render when the Settings tab is shown
    const tab = document.querySelector('button[data-bs-target="#settings"]');
    if (tab) {
        tab.addEventListener('shown.bs.tab', () => {
            renderUpgradeCard();
            refreshUpgradeStatus();
            startPolling();
        });
        // Stop polling when leaving the tab
        tab.addEventListener('hidden.bs.tab', stopPolling);
    }

    // Hook WebSocket messages if the global bus exists
    if (typeof window !== 'undefined') {
        window.addEventListener('zmm-ws-message', (ev) => {
            const msg = ev.detail;
            if (!msg || !msg.type) return;
            if (msg.type === 'upgrade_available' || msg.type === 'upgrade_status') {
                refreshUpgradeStatus();
            }
        });
    }
}

function startPolling(ms = POLL_ACTIVE_MS) {
    if (_pollTimer && _pollMs === ms) return;
    if (_pollTimer) clearInterval(_pollTimer);
    _pollMs = ms;
    _pollTimer = setInterval(refreshUpgradeStatus, ms);
}

function stopPolling() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; _pollMs = 0; }
    if (_logPollTimer) { clearInterval(_logPollTimer); _logPollTimer = null; }
}

// ============================================================================
// RENDER THE STATIC CARD SHELL
// ============================================================================

function renderUpgradeCard() {
    const mount = document.getElementById('upgradeCardMount');
    if (!mount) return;
    if (mount.dataset.rendered === 'true') return;

    mount.innerHTML = `
      <!-- Upgrade sub-nav — one pane per section instead of a stacked scroll -->
      <ul class="nav nav-pills mb-3 zmm-icon-rail" id="upgradeSubNav">
        <li class="nav-item d-md-none rail-toggle-item">
          <button class="nav-link rail-toggle" type="button" title="Toggle tab labels" aria-label="Toggle tab labels"
                  onclick="this.closest('ul').classList.toggle('labels-expanded')">
            <i class="fas fa-text-width"></i>
          </button>
        </li>
        <li class="nav-item">
          <button class="nav-link active" data-bs-toggle="tab" data-bs-target="#upgradeAppPane">
            <i class="fas fa-cloud-arrow-down me-1"></i> <span class="tab-label">Application Upgrade</span>
          </button>
        </li>
        <li class="nav-item">
          <button class="nav-link" data-bs-toggle="tab" data-bs-target="#upgradeSettingsPane">
            <i class="fas fa-gear me-1"></i> <span class="tab-label">Settings</span>
          </button>
        </li>
        <li class="nav-item">
          <button class="nav-link" data-bs-toggle="tab" data-bs-target="#upgradeDepsPane">
            <i class="fab fa-python me-1"></i> <span class="tab-label">Python Dependencies</span>
          </button>
        </li>
        <li class="nav-item">
          <button class="nav-link" data-bs-toggle="tab" data-bs-target="#upgradeRustPane">
            <i class="fab fa-rust me-1"></i> <span class="tab-label">Rust Components</span>
          </button>
        </li>
      </ul>

      <div class="tab-content">

        <div class="tab-pane fade show active" id="upgradeAppPane">
          <div class="card shadow-sm mb-3" id="upgradeCard">
            <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
              <span class="fw-bold"><i class="fas fa-cloud-arrow-down me-1"></i> Application Upgrade</span>
              <button class="btn btn-outline-primary btn-sm" id="upgradeCheckBtn">
                <i class="fas fa-sync-alt me-1"></i> Check now
              </button>
            </div>
            <div class="card-body" id="upgradeCardBody">
              <div class="text-muted small"><i class="fas fa-spinner fa-spin me-1"></i> Loading...</div>
            </div>
            <div class="card-footer text-muted small" id="upgradeCardFooter"></div>
          </div>
        </div>

        <div class="tab-pane fade" id="upgradeSettingsPane">
          <div class="card shadow-sm mb-3" id="upgradeSettingsCard">
            <div class="card-header bg-light py-2">
              <span class="fw-bold"><i class="fas fa-gear me-1"></i> Upgrade Settings</span>
            </div>
            <div class="card-body" id="upgradeSettingsBody">
              <div class="text-muted small"><i class="fas fa-spinner fa-spin me-1"></i> Loading...</div>
            </div>
          </div>
        </div>

        <div class="tab-pane fade" id="upgradeDepsPane">
          <!-- Python dependencies card (recovery / feature testing) -->
          <div class="card shadow-sm mb-3" id="depsCard">
            <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
              <span class="fw-bold"><i class="fab fa-python me-1"></i> Python Dependencies</span>
              <button class="btn btn-outline-secondary btn-sm" id="depsRefreshBtn">
                <i class="fas fa-sync-alt me-1"></i> Refresh
              </button>
            </div>
            <div class="card-body" id="depsCardBody">
              <div class="text-muted small"><i class="fas fa-spinner fa-spin me-1"></i> Loading...</div>
            </div>
            <div class="card-footer text-muted small">
              Installs go into the <strong>running container only</strong> — the next
              upgrade rebuilds from <code>requirements.lock</code>. Use this to recover a
              missing dependency or trial a package without upgrading.
            </div>
          </div>
        </div>

        <div class="tab-pane fade" id="upgradeRustPane">
          <div class="card shadow-sm mb-3" id="rustCard">
            <div class="card-header bg-light py-2">
              <span class="fw-bold"><i class="fab fa-rust me-1"></i> Rust Components</span>
            </div>
            <div class="card-body" id="rustCardBody">
              <div class="text-muted small"><i class="fas fa-spinner fa-spin me-1"></i> Loading...</div>
            </div>
            <div class="card-footer text-muted small">
              The toggle sets what the <strong>next upgrade build</strong> bakes into the
              image — it cannot add components to the container that is already running.
            </div>
          </div>
        </div>

      </div>

      <!-- Build log modal -->
      <div class="modal fade" id="upgradeLogModal" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog modal-lg modal-dialog-scrollable">
          <div class="modal-content">
            <div class="modal-header">
              <h5 class="modal-title"><i class="fas fa-terminal me-1"></i> Build Log</h5>
              <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body p-0">
              <pre id="upgradeLogPre" class="m-0 p-3 small text-monospace"
                   style="background:#0b1021;color:#c9d1d9;max-height:60vh;overflow:auto;"></pre>
            </div>
            <div class="modal-footer">
              <div class="form-check me-auto">
                <input class="form-check-input" type="checkbox" id="upgradeLogAutoScroll" checked>
                <label class="form-check-label small" for="upgradeLogAutoScroll">Auto-scroll</label>
              </div>
              <button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">Close</button>
            </div>
          </div>
        </div>
      </div>
    `;

    // Hook buttons
    document.getElementById('upgradeCheckBtn').addEventListener('click', () => checkForUpdates(true));
    document.getElementById('depsRefreshBtn').addEventListener('click', () => loadDependencies());

    // pip queries are not free — fetch the dependency table on first visit
    // to its pane, not on every Settings→Upgrade render.
    document.querySelector('button[data-bs-target="#upgradeDepsPane"]')
        .addEventListener('shown.bs.tab', () => {
            const body = document.getElementById('depsCardBody');
            if (body && !body.dataset.loaded) {
                body.dataset.loaded = 'true';
                loadDependencies();
            }
        });

    // Rust pane: fetch fresh on every visit — the state is tiny and the
    // marker may have been flipped from the host side.
    document.querySelector('button[data-bs-target="#upgradeRustPane"]')
        .addEventListener('shown.bs.tab', () => loadRustComponents());

    mount.dataset.rendered = 'true';
}

// ============================================================================
// RUST COMPONENTS (native wheels baked in at image build time)
// ============================================================================

async function loadRustComponents() {
    const body = document.getElementById('rustCardBody');
    if (!body) return;
    let data;
    try {
        data = await (await fetch('/api/upgrade/rust')).json();
    } catch (e) {
        data = null;
    }
    if (!data || !data.success) {
        body.innerHTML = `<div class="alert alert-warning small mb-0">
            Could not read Rust components state${data && data.error ? ': ' + escapeHtml(data.error) : ''}.</div>`;
        return;
    }
    const inst = data.installed || {};
    const on = !!data.build_enabled;
    // A component enabled for builds but absent from this image = arrives
    // with the next upgrade; present while builds are off = will drop out.
    const row = (label, desc, present) => `
        <div class="d-flex align-items-center gap-2 py-1 border-bottom">
          <span class="badge ${present ? 'bg-success' : 'bg-secondary'}" style="min-width:5.5em">
            ${present ? 'installed' : 'not in image'}</span>
          <div class="small"><strong>${label}</strong>
            <span class="text-muted">— ${desc}</span>
            ${!present && on ? '<span class="text-info ms-1">(included in the next upgrade build)</span>' : ''}
            ${present && !on ? '<span class="text-warning ms-1">(will be dropped by the next upgrade build)</span>' : ''}
          </div>
        </div>`;
    body.innerHTML = `
      <div class="form-check form-switch mb-2">
        <input class="form-check-input" type="checkbox" id="upgRustEnabled" ${on ? 'checked' : ''}>
        <label class="form-check-label" for="upgRustEnabled">
          <strong>Build Rust components into upgrade images</strong>
          <div class="small text-muted">Adds the Rust toolchain to the image build — expect it to
            take roughly 5&ndash;15 minutes longer. The running app is untouched until you upgrade.</div>
        </label>
      </div>
      <div class="mb-2">
        ${row('Telemetry appender <code>zmm_telemetry</code>',
              'fast native DuckDB writes for device telemetry (Python fallback otherwise)',
              !!inst.telemetry)}
        ${row('Cast EQ DSP <code>zmm_eq</code>',
              'live 10-band equaliser for Cast speakers (Media tab); without it Cast EQ is unavailable',
              !!inst.eq_dsp)}
        ${row('ffmpeg', 'stream decoder used by the Cast EQ (part of the base image on current builds)',
              !!inst.ffmpeg)}
      </div>`;
    document.getElementById('upgRustEnabled').addEventListener('change', async (e) => {
        const enabled = e.target.checked;
        e.target.disabled = true;
        try {
            const res = await fetch('/api/upgrade/rust', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled }),
            });
            const r = await res.json();
            if (!r.success) throw new Error(r.error || 'save failed');
            toast('success', enabled
                ? 'Rust components will be included in the next upgrade build'
                : 'Rust components will be left out of the next upgrade build');
        } catch (err) {
            toast('danger', 'Could not save: ' + err.message);
        }
        loadRustComponents();   // re-render badges + hints from saved state
    });
}

// ============================================================================
// PYTHON DEPENDENCIES (recovery / feature testing)
// ============================================================================

function _depsEsc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function loadDependencies() {
    const body = document.getElementById('depsCardBody');
    if (!body) return;
    try {
        const res = await fetch('/api/system/dependencies').then(r => r.json());
        if (!res.success) throw new Error(res.error || 'failed');
        renderDependencies(res);
    } catch (e) {
        body.innerHTML = `<div class="text-danger small">Failed to load dependencies: ${_depsEsc(e.message)}</div>`;
    }
}

function renderDependencies(data) {
    const body = document.getElementById('depsCardBody');
    if (!body) return;
    const pkgs = data.packages || [];
    const missing = pkgs.filter(p => p.missing);

    const rows = pkgs.map(p => `
        <tr>
          <td class="font-monospace">${_depsEsc(p.name)}</td>
          <td class="font-monospace text-muted">${_depsEsc(p.spec)}</td>
          <td>${p.missing
              ? '<span class="badge bg-danger">missing</span>'
              : `<span class="badge bg-success">${_depsEsc(p.installed)}</span>`}</td>
        </tr>`).join('');

    body.innerHTML = `
      <div class="d-flex align-items-center gap-2 mb-2 small">
        <span class="text-muted">Python ${_depsEsc(data.python)} ·
          ${pkgs.length} packages in requirements.txt ·</span>
        ${missing.length
            ? `<span class="text-danger fw-semibold">${missing.length} missing</span>`
            : '<span class="text-success fw-semibold">all installed</span>'}
      </div>
      <div style="max-height:220px;overflow:auto" class="mb-2 border rounded">
        <table class="table table-sm small mb-0">
          <thead><tr class="text-muted"><th>Package</th><th>Required</th><th>Installed</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div class="d-flex flex-wrap align-items-center gap-2">
        ${missing.length ? `
        <button class="btn btn-warning btn-sm" id="depsInstallMissingBtn">
          <i class="fas fa-download me-1"></i> Install ${missing.length} missing
        </button>` : ''}
        <div class="input-group input-group-sm" style="max-width:340px">
          <input type="text" class="form-control font-monospace" id="depsCustomSpec"
                 placeholder="package, package==1.2, package>=1.0">
          <button class="btn btn-outline-primary" id="depsInstallCustomBtn">
            <i class="fas fa-plus me-1"></i> Install
          </button>
        </div>
      </div>
      <pre id="depsOutput" class="small mt-2 mb-0 p-2 border rounded d-none"
           style="max-height:200px;overflow:auto;white-space:pre-wrap"></pre>`;

    document.getElementById('depsInstallMissingBtn')?.addEventListener('click',
        () => installDependencies({ missing: true }));
    document.getElementById('depsInstallCustomBtn')?.addEventListener('click', () => {
        const spec = document.getElementById('depsCustomSpec')?.value?.trim();
        if (spec) installDependencies({ packages: [spec] });
    });
    document.getElementById('depsCustomSpec')?.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') document.getElementById('depsInstallCustomBtn')?.click();
    });
}

async function installDependencies(payload) {
    const body = document.getElementById('depsCardBody');
    const out = document.getElementById('depsOutput');
    body?.querySelectorAll('button, input').forEach(el => { el.disabled = true; });
    if (out) {
        out.classList.remove('d-none');
        out.textContent = 'Running pip install — this can take a few minutes…';
    }
    try {
        const res = await fetch('/api/system/dependencies/install', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        }).then(r => r.json());
        if (out) out.textContent = (res.output || res.error || '(no output)')
            + (res.note ? `\n\n${res.note}` : '');
        if (res.success) {
            window.toast?.success?.('Dependencies', 'pip install finished.');
        } else {
            window.toast?.error?.('Install failed', res.error || 'see output');
        }
    } catch (e) {
        if (out) out.textContent = `Request failed: ${e.message}`;
        window.toast?.error?.('Install failed', e.message);
    }
    // Re-render the table (fresh installed versions) but keep the output pane.
    const keptOutput = out?.textContent;
    await loadDependencies();
    const newOut = document.getElementById('depsOutput');
    if (newOut && keptOutput) {
        newOut.classList.remove('d-none');
        newOut.textContent = keptOutput;
    }
}

// ============================================================================
// REFRESH STATUS + RENDER
// ============================================================================

async function refreshUpgradeStatus() {
    // Don't fetch while the page is in a background browser tab;
    // the next visible poll (or WS event) catches up.
    if (document.hidden) return;
    try {
        const res = await fetch('/api/upgrade/status');

        if (res.status === 401) {
            stopPolling();
            return;
        }

        const data = await res.json();
        if (!data || !data.success) return;

        // Re-render (and re-resolve the manager token) only when the
        // payload actually changed — keeps user edits in the settings
        // form alive between polls.
        const payload = JSON.stringify(data);
        if (payload !== _lastPayload) {
            _lastPayload = payload;
            renderBody(data);
            renderSettings(data);
        }

        // Manage log polling based on state
        const state = data.upgrade_state;
        if (state === 'building' && !_logPollTimer) {
            _logPollTimer = setInterval(refreshBuildLog, 2000);
        } else if (state !== 'building' && _logPollTimer) {
            clearInterval(_logPollTimer);
            _logPollTimer = null;
        }

        // Surface state transitions as toasts
        if (_lastState && _lastState !== state) {
            if (state === 'ready_to_swap') {
                toast('success', 'New image is built and ready to swap');
            } else if (state === 'idle' && _lastState === 'swapping') {
                toast('success', 'Upgrade complete');
            } else if (state === 'failed') {
                toast('danger', 'Upgrade failed: ' + (data.error || 'unknown error'));
            }
        }
        _lastState = state;

        // Retier the poll: fast while an upgrade is in flight, slow at idle.
        if (_pollTimer) {
            startPolling(ACTIVE_STATES.includes(state) ? POLL_ACTIVE_MS : POLL_IDLE_MS);
        }
    } catch (e) {
        // Silent — the tab may not be visible
    }
}

function renderBody(data) {
    const body = document.getElementById('upgradeCardBody');
    const footer = document.getElementById('upgradeCardFooter');
    if (!body) return;

    const {
        current_version, latest_available, update_available,
        previous_version, previous_image_tag,
        notes, url, last_check,
        upgrade_state, progress_percent, current_step, error,
        architecture, watcher_installed
    } = data;

    _versions = {
        present: current_version || '',
        destination: data.target_version || latest_available || '',
    };

    const stateBadge = renderStateBadge(upgrade_state);

    let watcherBanner = '';
    if (!watcher_installed) {
        watcherBanner = `
          <div class="alert alert-warning small mb-3">
            <i class="fas fa-triangle-exclamation me-1"></i>
            <strong>Upgrade watcher not installed.</strong>
            Run this on the host to enable in-app upgrades:
            <pre class="mb-0 mt-2 small">curl -fsSL https://raw.githubusercontent.com/oneofthemany/ZigBee-Matter-Manager/main/scripts/install_watcher.sh | bash</pre>
          </div>
        `;
    }

    // Warn that an image-based upgrade will discard live in-container edits.
    let liveEditsBanner = '';
    const liveEditCount = Number(data.live_edit_count || 0);
    if (liveEditCount > 0) {
        liveEditsBanner = `
          <div class="alert alert-warning small mb-3">
            <i class="fas fa-triangle-exclamation me-1"></i>
            <strong>${liveEditCount} live-edited file${liveEditCount === 1 ? '' : 's'} not in any release.</strong>
            Upgrading rebuilds from the published version and will <strong>discard</strong> these in-app edits.
            <button class="btn btn-link btn-sm p-0 ms-1 align-baseline" onclick="window.showLiveEdits()">View files</button>
            <span class="text-muted">·</span>
            <button class="btn btn-link btn-sm p-0 align-baseline" onclick="window.exportLiveEdits()">Export as .zip</button>
          </div>
        `;
    }

    let progressHtml = '';
    if (upgrade_state && upgrade_state !== 'idle') {
        const pct = Number(progress_percent || 0);
        const isFailed = upgrade_state === 'failed';
        progressHtml = `
          <div class="mb-3">
            <div class="d-flex justify-content-between small mb-1">
              <span><strong>${escapeHtml(current_step || upgrade_state)}</strong></span>
              <span>${pct}%</span>
            </div>
            <div class="progress" style="height:6px;">
              <div class="progress-bar ${isFailed ? 'bg-danger' : ''}"
                   role="progressbar" style="width:${pct}%"
                   aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"></div>
            </div>
            ${error ? `<div class="text-danger small mt-2"><i class="fas fa-circle-exclamation me-1"></i> ${escapeHtml(error)}</div>` : ''}
            <div class="mt-2 d-flex flex-wrap gap-1">
              <button class="btn btn-outline-secondary btn-sm" onclick="window.showUpgradeLog()">
                <i class="fas fa-terminal me-1"></i> View log
              </button>
              ${upgrade_state === 'building' ? `
                <button class="btn btn-outline-danger btn-sm" onclick="window.cancelUpgrade()">
                  <i class="fas fa-ban me-1"></i> Cancel
                </button>` : ''}
              ${isFailed ? `
                <button class="btn btn-outline-secondary btn-sm" onclick="window.dismissFailedUpgrade()">
                  <i class="fas fa-xmark me-1"></i> Dismiss
                </button>` : ''}
            </div>
          </div>
        `;
    }

    let actionHtml = '';
    if (upgrade_state === 'idle' || !upgrade_state) {
        if (update_available) {
            actionHtml = `
              <div class="alert alert-info mb-3">
                <div class="d-flex justify-content-between align-items-start">
                  <div>
                    <div class="fw-bold mb-1">
                      <i class="fas fa-arrow-up-from-bracket me-1"></i>
                      Version ${escapeHtml(latest_available)} is available
                    </div>
                    ${url ? `<a href="${escapeAttr(url)}" target="_blank" rel="noopener" class="small">Release notes on GitHub <i class="fas fa-external-link-alt ms-1"></i></a>` : ''}
                  </div>
                  <button class="btn btn-primary btn-sm" onclick="window.startUpgradeBuild('${escapeAttr(latest_available)}')"
                          ${!watcher_installed ? 'disabled' : ''}>
                    <i class="fas fa-hammer me-1"></i> Build
                  </button>
                </div>
                ${notes ? `<details class="mt-2"><summary class="small">Release notes</summary>
                  <pre class="small mt-2 mb-0" style="white-space:pre-wrap;max-height:200px;overflow:auto;">${escapeHtml(notes)}</pre>
                </details>` : ''}
              </div>
            `;
        } else {
            actionHtml = `
              <div class="text-success small mb-2">
                <i class="fas fa-check-circle me-1"></i> You're running the latest version.
              </div>
            `;
        }
    } else if (upgrade_state === 'ready_to_swap') {
        actionHtml = `
          <div class="alert alert-success mb-3">
            <div class="fw-bold mb-2">
              <i class="fas fa-check-circle me-1"></i> Image for v${escapeHtml(data.host_status?.target_version || latest_available || '')} is ready
            </div>
            <p class="mb-2 small">
              Swapping takes about 2-3 mins. You'll be disconnected briefly,
              then the page will reload automatically.
            </p>
            <button class="btn btn-success btn-sm" onclick="window.startUpgradeSwap()">
              <i class="fas fa-arrow-right-arrow-left me-1"></i> Swap now
            </button>
            <button class="btn btn-outline-secondary btn-sm ms-1" onclick="window.cancelUpgrade()">
              <i class="fas fa-xmark me-1"></i> Discard built image
            </button>
          </div>
        `;
    }

    // Rollback moved to the ZMM Manager (:8001) so it works even when this
    // app is down. Keep a pointer instead of a duplicate button.
    const managerUrl = `${location.protocol}//${location.hostname}:8001`;
    const rollbackHtml = `
      <div class="border-top pt-3 mt-3 small text-muted">
        <i class="fas fa-rotate-left me-1"></i>
        Rollback (to any retained version) and image retention are managed from the
        <a href="${managerUrl}" target="_blank" rel="noopener">ZMM Manager</a>${
          previous_version ? ` — previous version: <code>${escapeHtml(previous_version)}</code>` : ''}.
      </div>
    `;

    body.innerHTML = `
      ${watcherBanner}
      ${liveEditsBanner}
      <div class="d-flex justify-content-between align-items-center mb-3">
        <div>
          <div class="small text-muted">Current version</div>
          <div class="fs-5 fw-bold"><code>v${escapeHtml(current_version || 'unknown')}</code> <small class="text-muted">(${escapeHtml(architecture || '')})</small></div>
        </div>
        <div class="text-end">
          ${stateBadge}
        </div>
      </div>
      ${progressHtml}
      ${actionHtml}
      ${rollbackHtml}
    `;

    if (footer) {
        const lastCheckStr = last_check ? new Date(last_check).toLocaleString() : 'never';
        footer.innerHTML = `<i class="fas fa-clock me-1"></i> Last check: ${lastCheckStr}`;
    }
}

function renderStateBadge(state) {
    const map = {
        idle:          { cls: 'bg-success',   label: 'Idle',         icon: 'fa-check' },
        checking:      { cls: 'bg-info',      label: 'Checking',     icon: 'fa-satellite-dish' },
        building:      { cls: 'bg-primary',   label: 'Building',     icon: 'fa-hammer' },
        ready_to_swap: { cls: 'bg-success',   label: 'Ready',        icon: 'fa-circle-check' },
        swapping:      { cls: 'bg-warning text-dark', label: 'Swapping', icon: 'fa-arrow-right-arrow-left' },
        rolling_back:  { cls: 'bg-warning text-dark', label: 'Rolling back', icon: 'fa-rotate-left' },
        failed:        { cls: 'bg-danger',    label: 'Failed',       icon: 'fa-circle-exclamation' },
    };
    const m = map[state] || map.idle;
    return `<span class="badge ${m.cls}"><i class="fas ${m.icon} me-1"></i> ${m.label}</span>`;
}

// ============================================================================
// SETTINGS SECTION
// ============================================================================

function renderSettings(data) {
    const body = document.getElementById('upgradeSettingsBody');
    if (!body) return;

    const auto = !!data.auto_update;
    const win = data.auto_update_window || {};
    // Legacy "stable" behaved like today's "patch" (every release offered)
    const rawChannel = data.channel || 'patch';
    const channel = rawChannel === 'stable' ? 'patch' : rawChannel;
    const retention = data.retention_count || 2;
    const repo = data.repo || 'oneofthemany/ZigBee-Matter-Manager';

    body.innerHTML = `
      <div class="row g-3">
        <div class="col-md-6">
          <div class="form-check form-switch">
            <input class="form-check-input" type="checkbox" id="upgAutoUpdate" ${auto ? 'checked' : ''}>
            <label class="form-check-label" for="upgAutoUpdate">
              <strong>Auto-update</strong>
              <div class="small text-muted">Automatically install updates during the quiet window</div>
            </label>
          </div>
        </div>
        <div class="col-md-6">
          <label class="form-label small">Release channel</label>
          <select class="form-select form-select-sm" id="upgChannel">
            <option value="major" ${channel === 'major' ? 'selected' : ''}>Stable &mdash; major milestones only (e.g. 07.2026)</option>
            <option value="minor" ${channel === 'minor' ? 'selected' : ''}>Stable &mdash; minor &amp; major (e.g. 20.07.2026)</option>
            <option value="patch" ${channel === 'patch' ? 'selected' : ''}>Stable &mdash; every dated release (e.g. 20.01.07.2026)</option>
            <option value="prerelease" ${channel === 'prerelease' ? 'selected' : ''}>Bleeding edge &mdash; includes pre-release builds</option>
          </select>
          <div class="small text-muted mt-1">Versions are dated (day.release.month.year) &mdash; fewer parts means a bigger release. This sets how big a release has to be before you're notified; bleeding edge also includes builds flagged pre-release on GitHub.</div>
        </div>

        <div class="col-md-6">
          <label class="form-label small">Quiet window start</label>
          <input type="time" class="form-control form-control-sm" id="upgWindowStart"
                 value="${escapeAttr(win.start || '03:00')}">
        </div>
        <div class="col-md-6">
          <label class="form-label small">Quiet window end</label>
          <input type="time" class="form-control form-control-sm" id="upgWindowEnd"
                 value="${escapeAttr(win.end || '05:00')}">
        </div>

        <div class="col-md-6">
          <label class="form-label small">GitHub repository</label>
          <input type="text" class="form-control form-control-sm" id="upgRepo"
                 value="${escapeAttr(repo)}" placeholder="owner/repo">
        </div>
      </div>
      <div class="mt-3 d-flex gap-2">
        <button class="btn btn-primary btn-sm" onclick="window.saveUpgradeSettings()">
          <i class="fas fa-save me-1"></i> Save settings
        </button>
      </div>
      <div class="mt-3 small text-muted border-top pt-2">
        <i class="fas fa-broom me-1"></i>
        Image retention (currently keep last <code>${retention}</code>) and pruning are
        managed from the
        <a href="${location.protocol}//${location.hostname}:8001" target="_blank" rel="noopener">ZMM Manager</a>.
        Manager action token: <code id="upgMgrToken">&hellip;</code>
      </div>
      <div id="upgradeSettingsAlert" class="alert mt-3 small" style="display:none;"></div>
    `;
    loadManagerToken();
}

async function loadManagerToken() {
    const el = document.getElementById('upgMgrToken');
    if (!el) return;
    // The token is static for the life of the sidecar — one fetch is enough.
    if (_managerToken) {
        el.textContent = _managerToken;
        return;
    }
    try {
        const res = await fetch('/api/upgrade/manager-token');
        const data = await res.json();
        if (data.success && data.token) {
            _managerToken = data.token;
            el.textContent = data.token;
        } else {
            el.textContent = 'available after next upgrade';
        }
    } catch (_) {
        el.textContent = 'unavailable';
    }
}

// ============================================================================
// ACTIONS
// ============================================================================

async function checkForUpdates(force = true) {
    const btn = document.getElementById('upgradeCheckBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> Checking...'; }
    try {
        const res = await fetch('/api/upgrade/check?force=' + (force ? 'true' : 'false'), { method: 'POST' });
        const data = await res.json();
        if (!data.success) toast('danger', data.error || 'Check failed');
        await refreshUpgradeStatus();
    } catch (e) {
        toast('danger', 'Check failed: ' + e.message);
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-sync-alt me-1"></i> Check now'; }
    }
}

// Returns true if it's OK to proceed. If live in-container edits exist that the
// image-based op would discard, lists them and requires explicit confirmation.
// Detection failure never blocks the operation.
async function confirmDiscardLiveEdits(verb) {
    let info;
    try {
        const res = await fetch('/api/upgrade/live-edits');
        info = await res.json();
    } catch (e) {
        return true;
    }
    if (!info || !info.count) return true;

    const files = (info.files || []).map(f => '  • ' + f.path);
    const shown = files.slice(0, 15).join('\n');
    const more = files.length > 15 ? `\n  …and ${files.length - 15} more` : '';
    const exactNote = info.exact
        ? ''
        : '\n\n(Exact paths unavailable — counts inferred from editor backups.)';
    return confirmDialog({
        title: 'Live edits will be lost',
        message: `${info.count} live-edited file(s) will be PERMANENTLY LOST if you ${verb}.`,
        detail:
            `${shown}${more}${exactNote}\n\n` +
            `These in-app edits are not in any release. To keep them, cancel and use ` +
            `"Export as .zip" on the upgrade card first.`,
        confirmText: 'Discard & continue',
        variant: 'danger'
    });
}

window.exportLiveEdits = async function () {
    try {
        const res = await fetch('/api/upgrade/live-edits/export');
        if (!res.ok) {
            const j = await res.json().catch(() => ({}));
            toast('warning', j.error || 'Nothing to export');
            return;
        }
        const blob = await res.blob();
        const cd = res.headers.get('Content-Disposition') || '';
        const m = cd.match(/filename="([^"]+)"/);
        const name = m ? m[1] : 'zmm-live-edits.zip';
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        toast('success', 'Live edits exported — ' + name);
    } catch (e) {
        toast('danger', 'Export failed: ' + e.message);
    }
};

window.showLiveEdits = async function () {
    try {
        const res = await fetch('/api/upgrade/live-edits');
        const info = await res.json();
        if (!info || !info.count) { toast('info', 'No live edits detected.'); return; }
        const files = (info.files || [])
            .map(f => `• ${f.path}${f.status && f.status !== 'edited' ? ` (${f.status})` : ''}`)
            .join('\n');
        const exactNote = info.exact ? '' : '\n\n(Paths inferred from editor backups.)';
        window.toast.info(`${info.count} live-edited file(s) an upgrade would discard:\n\n${files}${exactNote}`);
    } catch (e) {
        toast('danger', 'Could not load live edits: ' + e.message);
    }
};

async function startBuild(version) {
    if (!version) return;
    if (!await confirmDialog({
        title: 'Build image',
        message: `Build image for v${version}?`,
        detail: 'This takes a few moments. The current app stays running during the build.',
        confirmText: 'Build'
    })) return;
    const res = await fetch('/api/upgrade/build', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version })
    });
    const data = await res.json();
    if (data.success) {
        toast('success', data.message || 'Build started');
    } else if (res.status === 409 && (data.error || data.message || '').toLowerCase().includes('progress')) {
        // Stuck lock — offer to force-clear
        if (await confirmDialog({
            title: 'Upgrade already in progress?',
            message: 'The system says another upgrade is in progress, but if you believe nothing is actually running ' +
                '(e.g. the watcher service crashed), you can force-clear the stale lock.',
            confirmText: 'Clear lock',
            variant: 'danger'
        })) {
            await clearLock();
        }
    } else {
        toast('danger', data.error || data.message || 'Build failed to start');
    }
    refreshUpgradeStatus();
}

async function clearLock() {
    try {
        const res = await fetch('/api/upgrade/clear-lock', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            toast('success', data.message || 'Lock cleared — try Build again');
        } else {
            toast('danger', data.error || 'Could not clear lock');
        }
    } catch (e) {
        toast('danger', 'Clear-lock request failed: ' + e.message);
    }
    refreshUpgradeStatus();
}

async function dismissFailedUpgrade() {
    try {
        const res = await fetch('/api/upgrade/reset-status', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        const data = await res.json();
        if (!data.success) {
            toast('warning', data.error || 'Could not dismiss');
        }
    } catch (e) {
        toast('danger', 'Dismiss failed: ' + e.message);
    }
    // Reset the local state-tracking so the next transition is detected fresh
    _lastState = null;
    refreshUpgradeStatus();
}

async function startSwap() {
    if (!(await confirmDiscardLiveEdits('swap to the new image'))) return;
    if (!await confirmDialog({
        title: 'Swap container',
        message: 'Swap to the new container?',
        detail: 'You will be disconnected for a few minutes. The page will reload automatically.',
        confirmText: 'Swap',
        variant: 'danger'
    })) return;
    const res = await fetch('/api/upgrade/swap', { method: 'POST' });
    const data = await res.json();
    if (data.success) {
        // Time to hit 88 MPH: full-screen DeLorean-bee overlay with the
        // version jump on its time circuits. Falls back to a plain toast
        // if deploy-animation.js didn't load.
        let tt = null;
        if (typeof window.showTimeTravelWait === 'function') {
            tt = window.showTimeTravelWait(_versions);
        } else {
            toast('info', 'Swap in progress — reloading when ready...');
        }
        // Poll for health after the API call likely drops
        waitForHealth(tt);
    } else {
        toast('danger', data.error || data.message || 'Swap failed');
    }
}

function waitForHealth(tt) {
    let attempts = 0;
    const iv = setInterval(async () => {
        attempts++;
        try {
            const r = await fetch('/api/system/health', { cache: 'no-store' });
            if (r.ok) {
                clearInterval(iv);
                // Accelerate to 88 and reload at the white-flash peak;
                // without the overlay, keep the old quiet reload.
                if (tt) tt.complete(() => location.reload());
                else setTimeout(() => location.reload(), 1500);
                return;
            }
        } catch (_) { /* expected during swap */ }
        if (attempts > 150) {
            clearInterval(iv);
            if (tt) tt.abort();
            toast('danger', 'Server did not come back within 5 minutes. Check the host logs.');
        }
    }, 2000);
}

async function cancelUpgrade() {
    if (!await confirmDialog({
        title: 'Cancel operation',
        message: 'Cancel the in-progress operation?',
        confirmText: 'Cancel operation',
        cancelText: 'Keep running',
        variant: 'danger'
    })) return;
    const res = await fetch('/api/upgrade/cancel', { method: 'POST' });
    const data = await res.json();
    if (data.success) toast('warning', 'Cancel requested');
    else toast('danger', data.error || data.message || 'Cancel failed');
    refreshUpgradeStatus();
}

async function saveUpgradeSettings() {
    const body = {
        auto_update: document.getElementById('upgAutoUpdate').checked,
        channel: document.getElementById('upgChannel').value,
        auto_update_window: {
            start: document.getElementById('upgWindowStart').value || '03:00',
            end:   document.getElementById('upgWindowEnd').value || '05:00',
        },
        repo: document.getElementById('upgRepo').value.trim() || undefined,
    };
    const res = await fetch('/api/upgrade/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const data = await res.json();
    const alert = document.getElementById('upgradeSettingsAlert');
    if (data.success) {
        alert.className = 'alert alert-success small';
        alert.textContent = 'Settings saved.';
    } else {
        alert.className = 'alert alert-danger small';
        alert.textContent = data.error || 'Save failed';
    }
    alert.style.display = 'block';
    setTimeout(() => { alert.style.display = 'none'; }, 5000);
}

// ============================================================================
// LOG VIEWER
// ============================================================================

async function showLog() {
    const modalEl = document.getElementById('upgradeLogModal');
    if (!modalEl) return;
    const modal = new window.bootstrap.Modal(modalEl);
    await refreshBuildLog();
    modal.show();
}

async function refreshBuildLog() {
    try {
        const res = await fetch('/api/upgrade/log?lines=500');
        const data = await res.json();
        if (!data.success) return;
        const pre = document.getElementById('upgradeLogPre');
        if (!pre) return;
        pre.textContent = (data.lines || []).join('\n') || '(no output yet)';
        const autoScroll = document.getElementById('upgradeLogAutoScroll');
        if (autoScroll?.checked) {
            pre.scrollTop = pre.scrollHeight;
        }
    } catch (e) { /* ignore */ }
}

// ============================================================================
// EXPORT / GLOBALS
// ============================================================================

if (typeof window !== 'undefined') {
    window.startUpgradeBuild = startBuild;
    window.startUpgradeSwap = startSwap;
    window.cancelUpgrade = cancelUpgrade;
    window.saveUpgradeSettings = saveUpgradeSettings;
    window.showUpgradeLog = showLog;
    window.dismissFailedUpgrade = dismissFailedUpgrade;
}

// ============================================================================
// UTILITIES
// ============================================================================

function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function escapeAttr(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function toast(type, msg) {
    const map = { danger: 'error', success: 'success', warning: 'warning', info: 'info' };
    const fn = window.toast && window.toast[map[type] || 'info'];
    if (fn) fn(msg);
    else if (type === 'danger') log.error(msg);
    else log.log(msg);
}