/**
 * speaker-sync.js — Settings → Speakers tab.
 *
 * Enablement + one-time Cast-console registration for the speaker-sync
 * feature (synchronised multi-speaker playback WITHOUT a Google-Home group;
 * server: modules/media/cast_sync.py). Saves only its own config slice
 * (media.cast.sync) via /api/config/structured, so it can't clobber fields
 * owned by the other settings tabs. Sync groups themselves are built in
 * Media → Group (speaker-sync sub-tab).
 */
const log = zmmLog('speaker-sync');

let _cfg = { enabled: false, http_port: 8010, app_id: '' };

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
    let status = null;
    try {
        const cfgRes = await (await fetch('/api/config/structured')).json();
        const media = (cfgRes.config || {}).media || {};
        _cfg = Object.assign({ enabled: false, http_port: 8010, app_id: '' },
                             (media.cast || {}).sync || {});
        status = await (await fetch('/api/media/sync/status')).json();
    } catch (e) {
        host.innerHTML = `<div class="alert alert-danger">Could not load speaker settings: ${esc(e.message)}</div>`;
        return;
    }

    const receiverUrl = `http://${location.hostname}:${_cfg.http_port || 8010}/cast/sync_receiver.html`;
    const live = status && !status.error;          // service constructed = enabled at boot
    const statusBadge = live
        ? (status.configured
            ? '<span class="badge bg-success">listener running · receiver registered</span>'
            : '<span class="badge bg-warning text-dark">listener running · App ID missing</span>')
        : '<span class="badge bg-secondary">disabled (or not yet restarted)</span>';

    host.innerHTML = `
    <div class="card shadow-sm mb-3">
      <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
        <span class="fw-bold"><i class="fas fa-volume-up me-1"></i> Speaker Sync</span>
        <div>
          <button class="btn btn-success btn-sm" onclick="speakerSyncSave()">
            <i class="fas fa-save me-1"></i> Save
          </button>
          <button class="btn btn-primary btn-sm ms-2" onclick="speakerSyncSaveRestart()"
                  title="Save and restart the service to apply changes now">
            <i class="fas fa-rotate me-1"></i> Save &amp; Restart
          </button>
        </div>
      </div>
      <div class="card-body">
        <p class="text-muted small mb-3">
          Play the same audio on several Google Cast speakers in sync <strong>without</strong>
          creating a group in Google Home. ZigBee Manager streams a server-clocked feed to a
          custom Cast receiver on each speaker; a per-speaker trim (±ms) lets you align them
          by ear. Build the groups themselves under <strong>Media → Group</strong>.
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
            <label class="form-label small fw-semibold">Sync Receiver App ID</label>
            <input type="text" class="form-control" id="cfg_sync_appid"
                   value="${esc(_cfg.app_id)}" placeholder="e.g. AB12CD34 (blank = not registered)">
            <small class="text-muted">From the Cast developer console (step 2 below).</small>
          </div>
          <div class="col-md-3">
            <label class="form-label small fw-semibold">Status</label>
            <div class="mt-2">${statusBadge}</div>
          </div>
        </div>
        <div id="speakerSyncAlert"></div>
      </div>
      <div class="card-footer text-muted small">
        <i class="fas fa-info-circle me-1"></i> Changes take effect after a service restart.
      </div>
    </div>

    <div class="card shadow-sm">
      <div class="card-header bg-light py-2">
        <span class="fw-bold"><i class="fas fa-list-check me-1"></i> One-time registration</span>
      </div>
      <div class="card-body small">
        <ol class="mb-2">
          <li class="mb-2">Enable above, <em>Save &amp; Restart</em>, then check the listener
            answers on the LAN:
            <code>http://${esc(location.hostname)}:${Number(_cfg.http_port) || 8010}/health</code></li>
          <li class="mb-2">In the <a href="https://cast.google.com/publish" target="_blank"
              rel="noopener">Cast developer console <i class="fas fa-external-link-alt fa-xs"></i></a>:
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
          <li>Paste the generated <strong>Application ID</strong> into the field above and
            <em>Save &amp; Restart</em>. Then build a sync group under
            <strong>Media → Group → Speaker sync</strong> and start a test — drag each
            speaker's trim until the clicks land together.</li>
        </ol>
      </div>
    </div>`;
}

function collectSlice() {
    return {
        media: { cast: { sync: {
            enabled: document.getElementById('cfg_sync_enabled')?.checked ?? false,
            http_port: Number(document.getElementById('cfg_sync_port')?.value) || 8010,
            app_id: document.getElementById('cfg_sync_appid')?.value?.trim() || '',
        } } },
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
    if (!await save(true)) return;
    alertBox('warning', 'Saved — restarting the service. This page will reload shortly…');
    try { await fetch('/api/system/restart', { method: 'POST' }); } catch (e) { /* expected drop */ }
    setTimeout(() => window.location.reload(), 8000);
}
