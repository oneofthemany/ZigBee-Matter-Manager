/* Settings -> APIs -> Speakers: Espressif speakers on USB serial — log, Wi-Fi
   (Improv), info, backup, flash. On the hub through /api/esp-serial; on this
   computer through Web Serial (log and Wi-Fi only). docs/esp-serial.md. */

import { escapeHtml } from './utils.js';

const API = '/api/esp-serial';
const ESPRESSIF = 0x303a;
const TABS = [
    ['connect', 'Connect', 'fa-plug'], ['log', 'Log', 'fa-terminal'], ['wifi', 'Wi-Fi', 'fa-wifi'],
    ['device', 'Device & backup', 'fa-microchip'], ['flash', 'Flash & restore', 'fa-bolt'],
];
const RESTART_NOTE = `These actions restart the speaker: it drops off Wi-Fi and stops playing for
    about ten seconds. ESPHome counts restarts that happen before a boot has run for a minute; after
    10 in a row it starts in <strong>safe mode</strong> — no voice or audio, waiting for an update —
    until it is restarted again. ZMM therefore allows one restart per speaker every 2 minutes.`;

const st = { mode: 'hub', port: null, ports: [], seq: 0, timer: null, browser: null, logLines: [] };

async function api(method, url, body, raw) {
    const res = await fetch(url, {
        method,
        headers: raw ? { 'Content-Type': 'application/octet-stream' } : body ? { 'Content-Type': 'application/json' } : undefined,
        body: raw || (body ? JSON.stringify(body) : undefined),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`);
    return data;
}

const $ = id => document.getElementById(id);
const say = (id, text, cls = 'text-danger') => { const el = $(id); if (el) { el.className = `small mt-2 ${cls}`; el.textContent = text; } };

export function espSerialSectionHtml() {
    const webSerial = 'serial' in navigator && window.isSecureContext;
    return `
        <p class="small text-muted">Local control and recovery for ESP32 speakers such as the Home Assistant
            Voice PE, over USB. Only Espressif devices are offered; the Zigbee radio never is.</p>
        <div class="btn-group btn-group-sm mb-3" role="group">
            <input type="radio" class="btn-check" name="esps_mode" id="esps_mode_hub" value="hub" checked>
            <label class="btn btn-outline-primary" for="esps_mode_hub"><i class="fas fa-server me-1"></i>Plugged into the ZMM hub</label>
            <input type="radio" class="btn-check" name="esps_mode" id="esps_mode_browser" value="browser" ${webSerial ? '' : 'disabled'}>
            <label class="btn btn-outline-primary" for="esps_mode_browser" title="${webSerial ? '' : 'Needs Chrome or Edge over HTTPS'}">
                <i class="fas fa-laptop me-1"></i>Plugged into this computer</label>
        </div>
        <ul class="nav nav-pills mb-3 zmm-icon-rail" role="tablist">
            ${TABS.map(([id, label, icon], i) => `<li class="nav-item"><button class="nav-link ${i ? '' : 'active'}"
                data-bs-toggle="pill" data-bs-target="#esps_pane_${id}" type="button" id="esps_tab_${id}">
                <i class="fas ${icon} me-1"></i>${label}</button></li>`).join('')}
        </ul>
        <div class="tab-content">
            <div class="tab-pane fade show active" id="esps_pane_connect">
                <div id="esps_ports"></div>
                <button type="button" class="btn btn-sm btn-outline-secondary mt-2" id="esps_refresh">
                    <i class="fas fa-rotate me-1"></i>Look again</button>
                <div id="esps_connect_msg" class="small mt-2"></div>
            </div>
            <div class="tab-pane fade" id="esps_pane_log">
                <div class="d-flex gap-2 mb-2">
                    <button type="button" class="btn btn-sm btn-primary" id="esps_log_start">Start</button>
                    <button type="button" class="btn btn-sm btn-outline-secondary" id="esps_log_stop">Stop</button>
                    <button type="button" class="btn btn-sm btn-link" id="esps_log_clear">Clear</button>
                    <span class="small text-muted ms-auto align-self-center" id="esps_log_state"></span>
                </div>
                <pre id="esps_log" class="border rounded p-2 small mb-0" style="height:22rem;overflow:auto;white-space:pre-wrap"></pre>
                <small class="text-muted">Reading the log never restarts the speaker.</small>
            </div>
            <div class="tab-pane fade" id="esps_pane_wifi">
                <button type="button" class="btn btn-sm btn-outline-primary mb-2" id="esps_wifi_check">Check Wi-Fi state</button>
                <div id="esps_wifi_state" class="small mb-3"></div>
                <div class="row g-2">
                    <div class="col-12 col-md-5"><label class="form-label small mb-1" for="esps_ssid">Network name</label>
                        <input class="form-control form-control-sm" id="esps_ssid" maxlength="32" autocomplete="off"></div>
                    <div class="col-12 col-md-5"><label class="form-label small mb-1" for="esps_pw">Password</label>
                        <input class="form-control form-control-sm" id="esps_pw" type="password" maxlength="64" autocomplete="new-password"></div>
                    <div class="col-12 col-md-2 d-flex align-items-end">
                        <button type="button" class="btn btn-sm btn-primary w-100" id="esps_wifi_send">Send</button></div>
                </div>
                <small class="text-muted">Sent to the speaker over USB (Improv); ZMM does not keep the password.</small>
                <div id="esps_wifi_msg" class="small mt-2"></div>
            </div>
            <div class="tab-pane fade" id="esps_pane_device">
                <div class="esps-hub-only"></div>
                <div class="alert alert-warning small">${RESTART_NOTE}</div>
                <div class="form-check mb-2"><input class="form-check-input" type="checkbox" id="esps_ack">
                    <label class="form-check-label small" for="esps_ack">I understand the speaker will restart</label></div>
                <div class="d-flex flex-wrap gap-2 mb-2">
                    <button type="button" class="btn btn-sm btn-outline-primary esps-restart" id="esps_info" disabled>Read device info</button>
                    <button type="button" class="btn btn-sm btn-outline-primary esps-restart" id="esps_backup" disabled>Full backup</button>
                </div>
                <details class="small mb-2"><summary>Recovery</summary>
                    <div class="form-check mt-1"><input class="form-check-input" type="checkbox" id="esps_override">
                        <label class="form-check-label" for="esps_override">Ignore the 2-minute wait — only for a speaker that is already failing</label></div>
                </details>
                <div id="esps_device_msg" class="small"></div>
                <div id="esps_device_out"></div>
                <h6 class="mt-3">Backups</h6>
                <div id="esps_backups" class="small"></div>
                <small class="text-muted">Backups hold the speaker's saved settings, including its Wi-Fi password.</small>
            </div>
            <div class="tab-pane fade" id="esps_pane_flash">
                <div class="esps-hub-only"></div>
                <div class="alert alert-danger small">Flashing replaces the firmware on the speaker. ZMM refuses
                    unless this speaker has a full backup, so you can always restore it. ${RESTART_NOTE}</div>
                <label class="form-label small mb-1" for="esps_upload">Add a factory or full-flash image (.bin)</label>
                <input type="file" class="form-control form-control-sm mb-2" id="esps_upload" accept=".bin">
                <div id="esps_sources" class="small mb-2"></div>
                <div class="row g-2 align-items-end">
                    <div class="col-12 col-md-5"><label class="form-label small mb-1" for="esps_confirm" id="esps_confirm_label">Type the code to confirm</label>
                        <input class="form-control form-control-sm" id="esps_confirm" autocomplete="off" spellcheck="false"></div>
                    <div class="col-12 col-md-4"><div class="form-check"><input class="form-check-input" type="checkbox" id="esps_flash_ack">
                        <label class="form-check-label small" for="esps_flash_ack">I understand the speaker will restart</label></div></div>
                    <div class="col-12 col-md-3"><button type="button" class="btn btn-sm btn-danger w-100" id="esps_flash" disabled>Flash</button></div>
                </div>
                <div id="esps_flash_msg" class="small mt-2"></div>
            </div>
        </div>`;
}

// ── Hub ──────────────────────────────────────────────────────────────────────

async function renderPorts() {
    const el = $('esps_ports');
    if (st.mode === 'browser') {
        el.innerHTML = `<button type="button" class="btn btn-sm btn-primary" id="esps_pick">Choose the speaker…</button>
            <div class="small text-muted mt-1">${st.browser ? 'Connected through this browser.' : 'Your browser asks which USB device to use.'}</div>`;
        $('esps_pick').addEventListener('click', pickBrowserPort);
        return;
    }
    let data;
    try { data = await api('GET', `${API}/ports`); }
    catch (e) { el.innerHTML = `<div class="text-danger small">${escapeHtml(e.message)}</div>`; return; }
    st.ports = data.ports;
    if (st.port && !st.ports.some(p => p.name === st.port)) st.port = null;
    if (!st.port && st.ports.length === 1) st.port = st.ports[0].name;
    el.innerHTML = (st.ports.length ? `<div class="list-group">${st.ports.map(p => `
        <label class="list-group-item d-flex flex-wrap align-items-center gap-2">
            <input class="form-check-input esps-port" type="radio" name="esps_port" value="${escapeHtml(p.name)}" ${p.name === st.port ? 'checked' : ''}>
            <span class="me-auto"><strong>${escapeHtml(p.product || 'Espressif device')}</strong>
                <span class="text-muted">${escapeHtml(p.path)} · ${escapeHtml(p.serial || 'no serial')}</span></span>
            ${p.busy ? '<span class="badge bg-warning text-dark">busy</span>' : ''}
            ${p.cooldown_s ? `<span class="badge bg-secondary">restart in ${p.cooldown_s} s</span>` : ''}
        </label>`).join('')}</div>`
        : `<div class="text-muted small">No speaker on the hub's USB. A speaker plugged in after ZMM started
            needs a container restart to appear.</div>`)
        + (data.hidden ? `<div class="small text-muted mt-1">${data.hidden} other serial device(s) hidden, such as the Zigbee radio.</div>` : '');
    el.querySelectorAll('.esps-port').forEach(r => r.addEventListener('change', () => { st.port = r.value; resetLog(); }));
}

function needPort(msgId) {
    if (st.mode === 'hub' ? st.port : st.browser) return true;
    say(msgId, 'Choose a speaker on the Connect tab first.');
    return false;
}

function resetLog() { st.seq = 0; st.logLines = []; const el = $('esps_log'); if (el) el.textContent = ''; }

function appendLog(lines) {
    const el = $('esps_log');
    if (!el || !lines.length) return;
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 8;
    st.logLines.push(...lines);
    if (st.logLines.length > 2000) st.logLines.splice(0, st.logLines.length - 2000);
    el.textContent = st.logLines.join('\n');
    if (atBottom) el.scrollTop = el.scrollHeight;
}

async function pollLog() {
    if (!st.port) return;
    try {
        const r = await api('GET', `${API}/ports/${encodeURIComponent(st.port)}/log?after=${st.seq}`);
        appendLog(r.lines.map(l => l.text));
        st.seq = r.seq;
        $('esps_log_state').textContent = r.error ? r.error : r.paused ? 'Paused while an operation uses the port'
            : r.resets ? `${r.resets} restart(s) seen` : 'Listening';
    } catch (e) { $('esps_log_state').textContent = e.message; }
}

async function runJob(path, body, msgId, render) {
    say(msgId, 'Working… the speaker restarts once.', 'text-muted');
    let job;
    try { job = await api('POST', `${API}/ports/${encodeURIComponent(st.port)}/${path}`, body); }
    catch (e) { say(msgId, e.message); return; }
    while (job.state === 'running') {
        await new Promise(r => setTimeout(r, 1500));
        try { job = await api('GET', `${API}/jobs/${job.id}`); } catch (e) { say(msgId, e.message); return; }
    }
    if (job.state === 'failed') { say(msgId, job.error); return; }
    say(msgId, restartSummary(job.result.restart), job.result.restart.booted_from_flash === false ? 'text-danger' : 'text-success');
    render?.(job.result);
    renderPorts();
    renderLibrary();
}

function restartSummary(r) {
    if (r.booted_from_flash === false) return 'The speaker restarted into its download mode and is not running its firmware. Unplug it and plug it back in.';
    const count = r.boot_attempts == null ? 'its restart count was not seen'
        : `ESPHome counts ${r.boot_attempts} unfinished boot(s) of ${r.safe_mode_default_attempts}`;
    return `Done. The speaker restarted and ${count}${r.safe_mode_active ? ' — it is in SAFE MODE' : ''}.`;
}

function renderInfo(v) {
    const sec = v.security, yn = b => b == null ? 'unknown' : b ? 'on' : 'off';
    $('esps_device_out').innerHTML = `
        <table class="table table-sm small mt-2"><tbody>
            <tr><th>Chip</th><td>${escapeHtml(v.chip.type)} ${escapeHtml(v.chip.revision || '')} · ${escapeHtml(v.chip.features.join(', '))}</td></tr>
            <tr><th>MAC</th><td>${escapeHtml(v.chip.mac)}</td></tr>
            <tr><th>Flash</th><td>${escapeHtml(v.flash.detected_size || 'unknown')}</td></tr>
            <tr><th>Secure Boot</th><td>${yn(sec.secure_boot_enabled)}</td></tr>
            <tr><th>Flash Encryption</th><td>${yn(sec.flash_encryption_enabled)}</td></tr>
        </tbody></table>
        ${v.partition_table ? `<table class="table table-sm small"><thead><tr><th>Partition</th><th>Type</th><th>Offset</th><th>Size</th></tr></thead><tbody>
            ${v.partition_table.entries.map(p => `<tr><td>${escapeHtml(p.label)}</td><td>${p.type}/${p.subtype}</td>
                <td>0x${p.offset.toString(16)}</td><td>${Math.round(p.size / 1024)} KB</td></tr>`).join('')}
        </tbody></table><div class="small text-muted">Table checksum: ${v.partition_table.md5}</div>` : ''}`;
}

async function renderLibrary() {
    let lib;
    try { lib = await api('GET', `${API}/library`); } catch { return; }
    const port = st.ports.find(p => p.name === st.port);
    const mine = lib.backups.filter(b => !port || b.port_key === port.key);
    $('esps_backups').innerHTML = mine.length ? mine.map(b => `<div>${escapeHtml(b.created_at)} · ${escapeHtml(b.mac)} ·
        ${Math.round(b.bytes / 1048576)} MB · <code>${b.sha256.slice(0, 12)}</code>
        <a href="${API}/backups/${encodeURIComponent(b.id)}/download">download</a></div>`).join('')
        : '<span class="text-muted">None for this speaker yet.</span>';
    const sources = [...lib.images.map(i => ({ ref: `image:${i.id}`, label: `${i.name} (${Math.round(i.bytes / 1024)} KB)`, del: i.id })),
                     ...mine.map(b => ({ ref: `backup:${b.id}`, label: `Backup ${b.created_at}` }))];
    $('esps_sources').innerHTML = sources.length ? sources.map(s => `<div class="form-check">
        <input class="form-check-input esps-src" type="radio" name="esps_src" id="src_${escapeHtml(s.ref)}" value="${escapeHtml(s.ref)}">
        <label class="form-check-label" for="src_${escapeHtml(s.ref)}">${escapeHtml(s.label)}</label>
        ${s.del ? `<button type="button" class="btn btn-sm btn-link text-danger p-0 ms-2 esps-del" data-id="${escapeHtml(s.del)}">remove</button>` : ''}</div>`).join('')
        : '<span class="text-muted">Add an image, or take a backup to restore.</span>';
    document.querySelectorAll('.esps-src').forEach(r => r.addEventListener('change', updateFlashButton));
    document.querySelectorAll('.esps-del').forEach(b => b.addEventListener('click', async () => {
        try { await api('DELETE', `${API}/images/${encodeURIComponent(b.dataset.id)}`); renderLibrary(); }
        catch (e) { window.toast?.error(e.message); }
    }));
    const code = port ? (port.serial || port.name).replace(/[^0-9A-Za-z]/g, '').slice(-6).toUpperCase() : '';
    $('esps_confirm_label').textContent = code ? `Type ${code} to confirm` : 'Type the code to confirm';
    updateFlashButton();
}

function updateFlashButton() {
    const src = document.querySelector('.esps-src:checked');
    $('esps_flash').disabled = st.mode !== 'hub' || !st.port || !src || !$('esps_flash_ack').checked || !$('esps_confirm').value.trim();
}

// ── Browser (Web Serial) ─────────────────────────────────────────────────────

const IMPROV = [...new TextEncoder().encode('IMPROV')];

function improvPacket(type, data) {
    const body = [...IMPROV, 1, type, data.length, ...data];
    return new Uint8Array([...body, body.reduce((s, b) => (s + b) & 255, 0), 10]);
}
const improvRpc = (cmd, data = []) => improvPacket(3, [cmd, data.length, ...data]);

function parseImprov(buf) {
    const packets = [];
    let i = indexOf(buf, IMPROV);
    while (i !== -1 && buf.length - i >= 9) {
        const end = i + 9 + buf[i + 8];
        if (buf.length <= end) break;
        if (buf.slice(i, end).reduce((s, b) => (s + b) & 255, 0) === buf[end]) packets.push([buf[i + 7], buf.slice(i + 9, end)]);
        buf = buf.slice(end + 1);
        i = indexOf(buf, IMPROV);
    }
    return [packets, i !== -1 ? buf.slice(i) : buf.slice(-5)];
}

function indexOf(buf, needle) {
    outer: for (let i = 0; i <= buf.length - needle.length; i++) {
        for (let j = 0; j < needle.length; j++) if (buf[i + j] !== needle[j]) continue outer;
        return i;
    }
    return -1;
}

function rpcStrings(d) {
    const out = [];
    for (let j = 2; j < 2 + d[1] && j < d.length; j += 1 + d[j]) out.push(new TextDecoder().decode(new Uint8Array(d.slice(j + 1, j + 1 + d[j]))));
    return out;
}

async function pickBrowserPort() {
    try {
        const port = await navigator.serial.requestPort({ filters: [{ usbVendorId: ESPRESSIF }] });
        // open() raises DTR and RTS together, which does not reset; they are never changed after.
        await port.open({ baudRate: 115200 });
        st.browser = { port, buf: [], waiters: [], text: '', reading: true };
        readBrowser(st.browser);
        resetLog();
        renderPorts();
    } catch (e) { say('esps_connect_msg', e.message); }
}

async function readBrowser(b) {
    const decoder = new TextDecoder();
    while (b.reading && b.port.readable) {
        const reader = b.port.readable.getReader();
        b.reader = reader;
        try {
            for (;;) {
                const { value, done } = await reader.read();
                if (done) break;
                b.text += decoder.decode(value, { stream: true });
                const parts = b.text.split(/\r?\n|\r/);
                b.text = parts.pop();
                if (b.logging) appendLog(parts.filter(l => l.trim()).map(l => l.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '')));
                b.buf.push(...value);
                const [packets, rest] = parseImprov(b.buf);
                b.buf = rest;
                for (const p of packets) b.waiters.forEach(w => w(p));
            }
        } catch { /* unplugged */ } finally { reader.releaseLock(); }
        if (!b.reading) break;
    }
}

function browserImprov(packet, want, timeoutMs) {
    const b = st.browser;
    return new Promise((resolve, reject) => {
        const done = (fn, v) => { clearTimeout(t); b.waiters = b.waiters.filter(w => w !== waiter); fn(v); };
        const waiter = ([type, data]) => {
            if (type === 2 && data[0]) return done(reject, new Error(`The speaker reported error ${data[0]}`));
            const found = want(type, data);
            if (found !== undefined) done(resolve, found);
        };
        const t = setTimeout(() => done(reject, new Error('No Improv answer: the firmware may not include improv_serial')), timeoutMs);
        b.waiters.push(waiter);
        const writer = b.port.writable.getWriter();
        writer.write(packet).finally(() => writer.releaseLock());
    });
}

// ── Wiring ───────────────────────────────────────────────────────────────────

async function wifiCheck() {
    if (!needPort('esps_wifi_msg')) return;
    say('esps_wifi_msg', 'Asking the speaker…', 'text-muted');
    try {
        let r;
        if (st.mode === 'hub') r = await api('POST', `${API}/ports/${encodeURIComponent(st.port)}/improv/status`);
        else {
            const states = { 1: 'stopped', 2: 'ready', 3: 'provisioning', 4: 'provisioned' };
            const state = await browserImprov(improvRpc(2), (t, d) => (t === 1 ? states[d[0]] : undefined), 3000);
            const s = await browserImprov(improvRpc(3), (t, d) => (t === 4 && d[0] === 3 ? rpcStrings(d) : undefined), 3000);
            r = { state, firmware: s[0], version: s[1], chip: s[2], device_name: s[3] };
        }
        $('esps_wifi_state').innerHTML = `<strong>${r.state === 'provisioned' ? 'Has Wi-Fi settings' : escapeHtml(r.state)}</strong>
            · ${escapeHtml(r.device_name || '')} · ${escapeHtml(r.firmware || '')} ${escapeHtml(r.version || '')}`;
        say('esps_wifi_msg', '', 'text-muted');
    } catch (e) { say('esps_wifi_msg', e.message); }
}

async function wifiSend() {
    if (!needPort('esps_wifi_msg')) return;
    const ssid = $('esps_ssid').value, password = $('esps_pw').value;
    say('esps_wifi_msg', 'Sending — the speaker tries to join (up to 45 s)…', 'text-muted');
    try {
        if (st.mode === 'hub') await api('POST', `${API}/ports/${encodeURIComponent(st.port)}/improv/wifi`, { ssid, password });
        else {
            const enc = new TextEncoder(), s = [...enc.encode(ssid)], p = [...enc.encode(password)];
            await browserImprov(improvRpc(1, [s.length, ...s, p.length, ...p]), (t, d) => (t === 4 && d[0] === 1 ? true : undefined), 45000);
        }
        $('esps_pw').value = '';
        say('esps_wifi_msg', 'Joined. It will appear on the network shortly.', 'text-success');
    } catch (e) { say('esps_wifi_msg', e.message); }
}

function setMode(mode) {
    st.mode = mode;
    clearInterval(st.timer);
    resetLog();
    document.querySelectorAll('.esps-hub-only').forEach(el => {
        el.innerHTML = mode === 'hub' ? '' : `<div class="alert alert-info small">Info, backup and flashing need the
            speaker plugged into the ZMM hub. From this computer you can read its log and set its Wi-Fi.</div>`;
    });
    renderPorts();
    updateFlashButton();
}

export function loadEspSerialSection() {
    const on = (id, ev, fn) => $(id)?.addEventListener(ev, fn);
    document.querySelectorAll('input[name="esps_mode"]').forEach(r => r.addEventListener('change', () => setMode(r.value)));
    on('esps_refresh', 'click', renderPorts);
    on('esps_log_start', 'click', () => {
        if (!needPort('esps_connect_msg')) return;
        if (st.mode === 'hub') { clearInterval(st.timer); pollLog(); st.timer = setInterval(pollLog, 700); }
        else { st.browser.logging = true; $('esps_log_state').textContent = 'Listening'; }
    });
    on('esps_log_stop', 'click', async () => {
        clearInterval(st.timer);
        if (st.browser) st.browser.logging = false;
        if (st.mode === 'hub' && st.port) await api('POST', `${API}/ports/${encodeURIComponent(st.port)}/log/stop`).catch(() => {});
        $('esps_log_state').textContent = 'Stopped';
    });
    on('esps_log_clear', 'click', resetLog);
    on('esps_wifi_check', 'click', wifiCheck);
    on('esps_wifi_send', 'click', wifiSend);
    on('esps_ack', 'change', () => document.querySelectorAll('.esps-restart').forEach(b => { b.disabled = !$('esps_ack').checked || st.mode !== 'hub'; }));
    const flags = () => ({ acknowledge_restart: $('esps_ack').checked, override_cooldown: $('esps_override').checked });
    on('esps_info', 'click', () => needPort('esps_device_msg') && runJob('info', flags(), 'esps_device_msg', renderInfo));
    on('esps_backup', 'click', () => needPort('esps_device_msg') && runJob('backup', flags(), 'esps_device_msg'));
    on('esps_upload', 'change', async ev => {
        const file = ev.target.files[0];
        if (!file) return;
        try { await api('POST', `${API}/images?name=${encodeURIComponent(file.name)}`, null, await file.arrayBuffer()); renderLibrary(); say('esps_flash_msg', 'Image added.', 'text-success'); }
        catch (e) { say('esps_flash_msg', e.message); }
        ev.target.value = '';
    });
    on('esps_flash_ack', 'change', updateFlashButton);
    on('esps_confirm', 'input', updateFlashButton);
    on('esps_flash', 'click', () => runJob('flash', {
        source: document.querySelector('.esps-src:checked')?.value, confirm: $('esps_confirm').value,
        acknowledge_restart: $('esps_flash_ack').checked, override_cooldown: $('esps_override').checked,
    }, 'esps_flash_msg'));
    setMode('hub');
    renderLibrary();
}
