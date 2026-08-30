/**
 * Settings -> Audio: OpenZone enablement, one-time Cast-console registration
 * for speaker sync (synchronised multi-speaker playback without a Google Home
 * group), and the Cast EQ proxy's LAN base URL.
 *
 * Saves only its own config slices (media.cast.sync + media.eq) so it cannot
 * clobber fields owned by other settings tabs. Sync groups themselves are built
 * in Media -> Group. See docs/speaker_sync.md.
 */
import { blockIfRestartForbidden, restartBlockedText, applyRestartGuard } from './restart-guard.js';

const log = zmmLog('speaker-sync');

let _cfg = { enabled: false, http_port: 8010, app_id: '', mic_device: '' };
let _eqCfg = { base_url: '' };

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function initSpeakerSyncTab() {
    const btn = document.querySelector('[data-bs-target="#settingsSpeakers"]');
    if (btn) btn.addEventListener('shown.bs.tab', render);
    window.speakerSyncSave = save;
    window.speakerSyncSaveRestart = saveAndRestart;
}

async function render() {
    const host = document.getElementById('settingsSpeakers');
    if (!host) return;
    let status = null, eqStatus = null;
    try {
        const cfgRes = await (await fetch('/api/config/structured')).json();
        const media = (cfgRes.config || {}).media || {};
        _cfg = Object.assign({ enabled: false, http_port: 8010, app_id: '', mic_device: '' },
                             (media.cast || {}).sync || {});
        _eqCfg = Object.assign({ base_url: '' }, media.eq || {});
        status = await (await fetch('/api/media/sync/status')).json();
        eqStatus = await (await fetch('/api/media/eq/status')).json();
    } catch (e) {
        host.innerHTML = `<div class="alert alert-danger">Could not load speaker settings: ${esc(e.message)}</div>`;
        return;
    }

    const receiverUrl = `http://${location.hostname}:${_cfg.http_port || 8010}/cast/sync_receiver.html`;
    const live = status && !status.error;          // service constructed = enabled at boot
    const statusBadge = live
        ? (status.configured
            ? '<span class="badge bg-success">listener running · custom receiver</span>'
            : '<span class="badge bg-success">listener running · built-in receiver mode</span>')
        : '<span class="badge bg-secondary">disabled (or not yet restarted)</span>';
    // Chirp-calibration mic badge: does the server see a capture device?
    const mic = status?.mic;
    const micBadge = !live ? '' : (mic?.available
        ? `<span class="badge bg-success ms-1" title="Inputs: ${esc((mic.inputs || []).join(', '))}"><i class="fas fa-microphone me-1"></i>mic: ${esc(mic.selected)}</span>`
        : `<span class="badge bg-warning text-dark ms-1" title="${esc(mic?.error || 'No capture device found')} — a mic plugged in after the container started needs a container restart to appear"><i class="fas fa-microphone-slash me-1"></i>no mic detected</span>`);
    const eqBadge = eqStatus?.available
        ? `<span class="badge bg-success">DSP ready</span>
           <div class="small text-muted mt-1">speakers fetch from
             <code>${esc(eqStatus.base_url)}</code>${eqStatus.auto ? ' (auto)' : ''}</div>`
        : `<span class="badge bg-warning text-dark">not ready</span>
           <div class="small text-muted mt-1">${esc(eqStatus?.reason || 'status unavailable')}</div>`;

    // render() rebuilds the whole host on every tab show — carry the active
    // pane across re-renders.
    const activeEl = host.querySelector('.tab-pane.active');
    const active = ['speakersReceiverPane', 'speakersEqPane'].includes(activeEl?.id)
        ? activeEl.id : 'speakersZonePane';

    host.innerHTML = `
    <ul class="nav nav-pills mb-3 zmm-icon-rail" id="speakersSubNav">
      <li class="nav-item d-md-none rail-toggle-item">
        <button class="nav-link rail-toggle" type="button" title="Toggle tab labels" aria-label="Toggle tab labels"
                onclick="this.closest('ul').classList.toggle('labels-expanded')">
          <i class="fas fa-text-width"></i>
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link${active === 'speakersZonePane' ? ' active' : ''}"
                data-bs-toggle="tab" data-bs-target="#speakersZonePane">
          <i class="zmm-openzone-icon me-1"></i> <span class="tab-label">OpenZone</span>
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link${active === 'speakersEqPane' ? ' active' : ''}"
                data-bs-toggle="tab" data-bs-target="#speakersEqPane">
          <i class="fas fa-sliders-h me-1"></i> <span class="tab-label">Cast EQ</span>
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link${active === 'speakersReceiverPane' ? ' active' : ''}"
                data-bs-toggle="tab" data-bs-target="#speakersReceiverPane">
          <i class="fas fa-list-check me-1"></i> <span class="tab-label">Receiver Registration</span>
        </button>
      </li>
    </ul>

    <div class="tab-content">

    <div class="tab-pane fade${active === 'speakersZonePane' ? ' show active' : ''}" id="speakersZonePane">
    <div class="card shadow-sm mb-3">
      <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
        <span class="fw-bold"><i class="zmm-openzone-icon me-1"></i> OpenZone</span>
        <div>
          <button class="btn btn-success btn-sm" onclick="speakerSyncSave()">
            <i class="fas fa-save me-1"></i> Save
          </button>
          <button class="btn btn-primary btn-sm ms-2" onclick="speakerSyncSaveRestart()" data-restart-control
                  title="Save and restart the service to apply changes now">
            <i class="fas fa-rotate me-1"></i> Save &amp; Restart
          </button>
        </div>
      </div>
      <div class="card-body">
        <p class="text-muted small mb-3">
          Play the same audio on several Google Cast speakers in sync <strong>without</strong>
          creating a group in Google Home. Works out of the box using each speaker's
          <em>built-in</em> receiver (no Cast console account needed) with automatic lag
          correction; a per-speaker trim (±ms) lets you align them by ear. Build the groups
          themselves under <strong>Media → Group</strong>. Optionally, registering a custom
          receiver (Receiver Registration tab) upgrades sync from ~tens of ms to sample-accurate.
        </p>
        <div class="row g-3 mb-3">
          <div class="col-md-2">
            <label class="form-label small fw-semibold">Enabled</label>
            <div class="form-check form-switch mt-1">
              <input class="form-check-input" type="checkbox" id="cfg_sync_enabled"
                     ${_cfg.enabled ? 'checked' : ''}>
            </div>
          </div>
          <div class="col-md-3">
            <label class="form-label small fw-semibold">HTTP Port</label>
            <input type="number" class="form-control" id="cfg_sync_port"
                   value="${Number(_cfg.http_port) || 8010}" min="1024" max="65535">
            <small class="text-muted">Plain-HTTP listener for the receiver page + audio
              WebSocket (Cast devices reject the app's self-signed HTTPS).</small>
          </div>
          <div class="col-md-4">
            <label class="form-label small fw-semibold">Sync Receiver App ID <span class="fw-normal text-muted">(optional)</span></label>
            <input type="text" class="form-control" id="cfg_sync_appid"
                   value="${esc(_cfg.app_id)}" placeholder="blank = built-in receiver mode">
            <small class="text-muted">Leave blank to use each speaker's built-in receiver.
              Fill from the Cast developer console (Receiver Registration tab) for sample-accurate sync.</small>
          </div>
          <div class="col-md-3">
            <label class="form-label small fw-semibold">Status</label>
            <div class="mt-2">${statusBadge} ${micBadge}</div>
          </div>
          <div class="col-md-4">
            <label class="form-label small fw-semibold">Mic Device <span class="fw-normal text-muted">(optional)</span></label>
            <input type="text" class="form-control" id="cfg_sync_mic"
                   value="${esc(_cfg.mic_device)}" placeholder="blank = system default input">
            <small class="text-muted">Capture device for chirp calibration — case-insensitive
              substring of the device name, e.g. "USB".</small>
          </div>
        </div>
        <div id="speakerSyncAlert"></div>
      </div>
      <div class="card-footer text-muted small">
        <i class="fas fa-info-circle me-1"></i> Changes take effect after a service restart.
      </div>
    </div>
    </div>

    <div class="tab-pane fade${active === 'speakersEqPane' ? ' show active' : ''}" id="speakersEqPane">
    <div class="card shadow-sm mb-3">
      <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
        <span class="fw-bold"><i class="fas fa-sliders-h me-1"></i> Cast EQ (server DSP)</span>
        <div>
          <button class="btn btn-success btn-sm" onclick="speakerSyncSave()">
            <i class="fas fa-save me-1"></i> Save
          </button>
          <button class="btn btn-primary btn-sm ms-2" onclick="speakerSyncSaveRestart()" data-restart-control
                  title="Save and restart the service to apply changes now">
            <i class="fas fa-rotate me-1"></i> Save &amp; Restart
          </button>
        </div>
      </div>
      <div class="card-body">
        <p class="text-muted small mb-3">
          Cast speakers expose no DSP API, so their 10-band EQ (Media tab → player
          <i class="fas fa-sliders-h"></i> button) is applied on the server: playback is routed
          through an ffmpeg → Rust biquad proxy and the speaker fetches the processed stream
          back over a plain-HTTP listener (Cast devices reject this app's self-signed
          HTTPS). The listener's address is auto-detected — <strong>no configuration
          needed</strong>.
        </p>
        <div class="row g-3 mb-2">
          <div class="col-md-5">
            <label class="form-label small fw-semibold">LAN Base URL <span class="fw-normal text-muted">(override, usually blank)</span></label>
            <input type="text" class="form-control" id="cfg_eq_baseurl"
                   value="${esc(_eqCfg.base_url)}"
                   placeholder="blank = auto-detect">
            <small class="text-muted">Only set this (<code>media.eq.base_url</code>) if
              auto-detection picks the wrong interface. Must be plain
              <code>http://</code> and reachable by the speaker.</small>
          </div>
          <div class="col-md-4">
            <label class="form-label small fw-semibold">Status</label>
            <div class="mt-2">${eqBadge}</div>
          </div>
        </div>
      </div>
      <div class="card-footer text-muted small">
        <i class="fas fa-info-circle me-1"></i> Changes take effect after a service restart.
      </div>
    </div>
    </div>

    <div class="tab-pane fade${active === 'speakersReceiverPane' ? ' show active' : ''}" id="speakersReceiverPane">
    <div class="card shadow-sm">
      <div class="card-header bg-light py-2">
        <span class="fw-bold"><i class="fas fa-list-check me-1"></i> Optional: custom-receiver registration (sample-accurate sync)</span>
      </div>
      <div class="card-body small">
        <p class="text-muted mb-2">Not required — sync works without it. Registration only
          buys tighter (sample-accurate) alignment via a custom Cast receiver.</p>
        <ol class="mb-2">
          <li class="mb-2">Enable OpenZone (previous tab), <em>Save &amp; Restart</em>, then check
            the listener answers on the LAN:
            <code>http://${esc(location.hostname)}:${Number(_cfg.http_port) || 8010}/health</code></li>
          <li class="mb-2">In the <a href="https://cast.google.com/publish" target="_blank"
              rel="noopener">Cast developer console <i class="fas fa-external-link-alt fa-xs"></i></a>
            (one-time $5 developer registration):
            <strong>Add new application → Custom Receiver</strong> and paste this URL
            (don't publish it):
            <div class="input-group input-group-sm mt-1" style="max-width: 480px">
              <input type="text" class="form-control" readonly value="${esc(receiverUrl)}"
                     id="syncReceiverUrl">
              <button class="btn btn-outline-secondary"
                      onclick="navigator.clipboard.writeText(document.getElementById('syncReceiverUrl').value)">
                <i class="far fa-copy"></i></button>
            </div>
            <span class="text-muted">Each test speaker's serial number must be registered for
              development in the same console (as for the lyrics receiver); reboot the speakers
              once after registering.</span></li>
          <li>Paste the generated <strong>Application ID</strong> into the App ID field on the
            OpenZone tab and <em>Save &amp; Restart</em>. Then build a sync group under
            <strong>Media → Group → OpenZone</strong> and start a test — drag each
            speaker's trim until the clicks land together.</li>
        </ol>
      </div>
    </div>
    </div>

    </div>`;
}

function collectSlice() {
    return {
        media: {
            cast: { sync: {
                enabled: document.getElementById('cfg_sync_enabled')?.checked ?? false,
                http_port: Number(document.getElementById('cfg_sync_port')?.value) || 8010,
                app_id: document.getElementById('cfg_sync_appid')?.value?.trim() || '',
                mic_device: document.getElementById('cfg_sync_mic')?.value?.trim() || '',
            } },
            eq: {
                base_url: document.getElementById('cfg_eq_baseurl')?.value?.trim() || '',
            },
        },
    };
}

function alertBox(kind, msg) {
    const el = document.getElementById('speakerSyncAlert');
    if (el) el.innerHTML = `<div class="alert alert-${kind} py-2 mb-0">${msg}</div>`;
}

async function save(silent = false) {
    try {
        const res = await fetch('/api/config/structured', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: collectSlice() }),
        });
        const data = await res.json();
        if (!data.success) { alertBox('danger', 'Save failed: ' + esc(data.error)); return false; }
        if (!silent) alertBox('success', 'Saved. Restart the service to apply.');
        return true;
    } catch (e) {
        alertBox('danger', 'Error saving: ' + esc(e.message));
        return false;
    }
}

async function saveAndRestart() {
    const blocked = await blockIfRestartForbidden();
    if (blocked) {
        alertBox('warning', restartBlockedText(blocked) +
            ' Your changes can be saved now and will apply at the next restart.');
        return;
    }
    if (!await save(true)) return;
    alertBox('warning', 'Saved — restarting the service. This page will reload shortly…');
    try {
        const res = await fetch('/api/system/restart', { method: 'POST' });
        if (res && res.status === 409) {
            const body = await res.json().catch(() => ({}));
            alertBox('warning', 'Saved, but the restart was refused: ' + restartBlockedText(body.reason));
            applyRestartGuard({ allowed: false, reason: body.reason });
            return;
        }
    } catch (e) { /* expected drop */ }
    setTimeout(() => window.location.reload(), 8000);
}
