/* Settings -> Config -> Scheduled backups (admin): when, where, how many to
   keep, and the passphrase. Backend: modules/backup.py — docs/backups.md. */

import { escapeHtml } from './utils.js';

let cfg = null;

async function api(method, url, body) {
    const res = await fetch(url, {
        method, headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`);
    return data;
}

function ago(ts) {
    if (!ts) return 'never';
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 5400) return `${Math.round(s / 60)} min ago`;
    if (s < 172800) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} days ago`;
}

function statusHtml(c) {
    const st = c.status || {};
    if (!c.enabled && !st.last_attempt) return '<span class="text-muted">Off. Nothing has been backed up automatically.</span>';
    const next = c.next_run ? ` Next: ${escapeHtml(new Date(c.next_run * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' }))}.` : '';
    if (st.last_error) {
        return `<span class="text-danger"><i class="fas fa-triangle-exclamation me-1"></i>Last run failed: ${escapeHtml(st.last_error)}</span>
            <div class="text-muted">Last good backup: ${escapeHtml(ago(st.last_success))}.${next}</div>`;
    }
    if (!st.last_success) return `<span class="text-muted">Not run yet.${next}</span>`;
    return `<span class="text-success"><i class="fas fa-check me-1"></i>Last backup ${escapeHtml(ago(st.last_success))}</span>
        <span class="text-muted">— ${escapeHtml(st.last_file || '')}, ${((st.last_size || 0) / 1048576).toFixed(1)} MB,
        to <span class="text-break">${escapeHtml(st.target || '')}</span>${st.encrypted ? ', encrypted' : ''}.${next}</span>`;
}

function field(id, label, value, attrs = '', col = 'col-12 col-md-6') {
    return `<div class="${col}"><label class="form-label small mb-1" for="${id}">${label}</label>
        <input class="form-control form-control-sm" id="${id}" value="${escapeHtml(value ?? '')}" ${attrs}></div>`;
}

function secret(id, label, isSet, help = '') {
    return `<div class="col-12 col-md-6"><label class="form-label small mb-1" for="${id}">${label}</label>
        <input class="form-control form-control-sm" id="${id}" type="password" autocomplete="new-password"
               placeholder="${isSet ? 'stored — blank keeps it' : ''}">
        ${help ? `<small class="text-muted">${help}</small>` : ''}</div>`;
}

function targetFields(c) {
    const t = c.target;
    if (t.type === 'webdav') {
        return field('bs_url', 'WebDAV folder URL', t.url, 'placeholder="https://nas.example/remote.php/dav/files/me/zmm"', 'col-12')
            + field('bs_username', 'Username', t.username, 'autocomplete="off"')
            + secret('bs_webdav_password', 'Password', c.webdav_password_set, 'An app password, where the server offers one.');
    }
    if (t.type === 's3') {
        return field('bs_endpoint', 'Endpoint', t.endpoint, 'placeholder="https://s3.eu-west-2.amazonaws.com"')
            + field('bs_bucket', 'Bucket', t.bucket)
            + field('bs_region', 'Region', t.region, 'placeholder="us-east-1"', 'col-6 col-md-3')
            + field('bs_prefix', 'Folder in the bucket', t.prefix, 'placeholder="optional"', 'col-6 col-md-3')
            + field('bs_access_key', 'Access key', t.access_key, 'autocomplete="off"')
            + secret('bs_s3_secret_key', 'Secret key', c.s3_secret_key_set);
    }
    return field('bs_path', 'Folder', t.path, 'placeholder="data/backups"', 'col-12')
        + `<div class="col-12"><small class="text-muted">On the hub. The default is on the hub's own disk — it survives a
            broken container, not a dead disk. For a NAS, mount its share on the host inside ZMM's data folder and
            point this at it.</small></div>`;
}

function render(host) {
    const c = cfg;
    host.innerHTML = `
    <h6 class="text-uppercase text-muted fw-bold mb-3 mt-2 small">
      <i class="fas fa-clock-rotate-left me-1"></i> Scheduled backups
    </h6>
    <div class="card mb-4"><div class="card-body">
        <div class="small mb-3" id="bs_status" role="status">${statusHtml(c)}</div>
        <div class="form-check form-switch mb-3">
            <input class="form-check-input" type="checkbox" id="bs_enabled" ${c.enabled ? 'checked' : ''}>
            <label class="form-check-label" for="bs_enabled">Back up every night</label>
        </div>
        <div class="row g-2">
            <div class="col-6 col-md-3"><label class="form-label small mb-1" for="bs_time">At</label>
                <input class="form-control form-control-sm" id="bs_time" type="time" value="${escapeHtml(c.time)}"></div>
            <div class="col-6 col-md-3"><label class="form-label small mb-1" for="bs_keep_daily">Keep daily</label>
                <input class="form-control form-control-sm" id="bs_keep_daily" type="number" min="1" max="60" value="${escapeHtml(c.keep_daily)}"></div>
            <div class="col-6 col-md-3"><label class="form-label small mb-1" for="bs_keep_weekly">Keep weekly</label>
                <input class="form-control form-control-sm" id="bs_keep_weekly" type="number" min="0" max="52" value="${escapeHtml(c.keep_weekly)}"></div>
            <div class="col-6 col-md-3"><label class="form-label small mb-1" for="bs_type">Send to</label>
                <select class="form-select form-select-sm" id="bs_type">
                    ${[['local', 'A folder'], ['webdav', 'WebDAV'], ['s3', 'S3-compatible']].map(([v, l]) =>
                        `<option value="${v}" ${c.target.type === v ? 'selected' : ''}>${l}</option>`).join('')}
                </select></div>
            ${targetFields(c)}
        </div>
        <div class="form-check mt-3">
            <input class="form-check-input" type="checkbox" id="bs_telemetry" ${c.include_telemetry ? 'checked' : ''}>
            <label class="form-check-label small" for="bs_telemetry">Include history (telemetry and energy databases) — much larger</label>
        </div>
        <div class="form-check mt-1">
            <input class="form-check-input" type="checkbox" id="bs_encrypt" ${c.encrypt ? 'checked' : ''}>
            <label class="form-check-label small" for="bs_encrypt">Encrypt with a passphrase</label>
        </div>
        <div class="row g-2 mt-1">
            ${secret('bs_passphrase', 'Passphrase', c.passphrase_set,
                     'A backup holds logins, password hashes and the network key. <strong>Without this passphrase an encrypted backup can\'t be restored</strong> — keep it somewhere that isn\'t this hub.')}
        </div>
        <div class="d-flex flex-wrap gap-2 mt-3">
            <button type="button" class="btn btn-primary btn-sm" id="bs_save">Save</button>
            <button type="button" class="btn btn-outline-secondary btn-sm" id="bs_test">Test destination</button>
            <button type="button" class="btn btn-outline-secondary btn-sm" id="bs_run">Back up now</button>
        </div>
        <div class="small mt-2" id="bs_msg" role="status"></div>
    </div></div>`;
    wire(host);
}

function collect() {
    const v = id => document.getElementById(id)?.value?.trim();
    const on = id => !!document.getElementById(id)?.checked;
    const body = {
        enabled: on('bs_enabled'), time: v('bs_time'), keep_daily: Number(v('bs_keep_daily')),
        keep_weekly: Number(v('bs_keep_weekly')), include_telemetry: on('bs_telemetry'), encrypt: on('bs_encrypt'),
        target: { type: v('bs_type') },
    };
    for (const k of ['path', 'url', 'username', 'endpoint', 'bucket', 'region', 'prefix', 'access_key']) {
        if (document.getElementById(`bs_${k}`)) body.target[k] = v(`bs_${k}`);
    }
    for (const k of ['passphrase', 'webdav_password', 's3_secret_key']) {
        if (v(`bs_${k}`)) body[k] = v(`bs_${k}`);
    }
    return body;
}

function say(text, cls = 'text-danger') {
    const el = document.getElementById('bs_msg');
    if (el) { el.className = `small mt-2 ${cls}`; el.textContent = text; }
}

async function save(host, quiet) {
    try {
        cfg = await api('PUT', '/api/backup/schedule', collect());
        render(host);
        if (!quiet) say('Saved.', 'text-success');
        return true;
    } catch (e) { say(e.message); return false; }
}

function wire(host) {
    const on = (id, ev, fn) => document.getElementById(id)?.addEventListener(ev, fn);
    // Switching destination shows its fields; what was typed for the others is kept on the hub.
    on('bs_type', 'change', () => { cfg = { ...cfg, target: { ...cfg.target, type: document.getElementById('bs_type').value } }; render(host); });
    on('bs_save', 'click', () => save(host));
    on('bs_test', 'click', async ev => {
        const btn = ev.currentTarget;
        btn.disabled = true;
        // Test what's on screen, not what was last saved.
        if (await save(host, true)) {
            say('Writing a test file…', 'text-muted');
            try {
                const r = await api('POST', '/api/backup/schedule/test');
                say(r.success ? `Works: wrote, listed and removed a test file in ${r.target}.` : r.error,
                    r.success ? 'text-success' : 'text-danger');
            } catch (e) { say(e.message); }
        }
        document.getElementById('bs_test').disabled = false;
    });
    on('bs_run', 'click', async ev => {
        const btn = ev.currentTarget;
        btn.disabled = true;
        if (await save(host, true)) {
            say('Backing up…', 'text-muted');
            try {
                const r = await api('POST', '/api/backup/schedule/run');
                cfg = await api('GET', '/api/backup/schedule');
                render(host);
                say(r.success ? 'Backup done.' : `Backup failed: ${r.status.last_error}`, r.success ? 'text-success' : 'text-danger');
            } catch (e) { say(e.message); }
        }
        const b = document.getElementById('bs_run');
        if (b) b.disabled = false;
    });
}

export async function initBackupSchedule(host) {
    if (!host || !window.zmmAuth?.hasScope?.('admin')) return;
    try {
        cfg = await api('GET', '/api/backup/schedule');
        render(host);
    } catch (e) {
        host.innerHTML = `<div class="text-danger small mb-3">Couldn't load the backup schedule: ${escapeHtml(e.message)}</div>`;
    }
}
