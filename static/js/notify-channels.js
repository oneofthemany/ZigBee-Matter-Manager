/* Settings -> Notifications: ntfy, Telegram, Signal, Pushover and email. Each user picks
   their own destinations; admins also set up the hub's servers and tokens.
   Backend: modules/notify_channels.py. See docs/notifications.md §Other channels. */

import { escapeHtml } from './utils.js';

const LABELS = {
    ntfy:     { name: 'ntfy',     icon: 'fa-bell' },
    telegram: { name: 'Telegram', icon: 'fa-paper-plane' },
    signal:   { name: 'Signal',   icon: 'fa-comment-dots' },
    pushover: { name: 'Pushover', icon: 'fa-mobile-screen-button' },
    email:    { name: 'Email',    icon: 'fa-envelope' },
};

let view = null;        // last GET /api/notify-channels
let linkPoll = null;

async function api(method, url, body) {
    const res = await fetch(url, {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    return data;
}

const isAdmin = () => !!window.zmmAuth?.hasScope?.('admin');

function randomTopic() {
    const b = new Uint8Array(12);
    crypto.getRandomValues(b);
    return 'zmm-' + Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
}

function channelRow(ch, s) {
    const on = s[ch].enabled ? 'checked' : '';
    let field = '';
    if (ch === 'ntfy') {
        const url = view.ntfy_server && s.ntfy.topic ? `${view.ntfy_server}/${s.ntfy.topic}` : '';
        field = `
            <div class="input-group input-group-sm">
                <input class="form-control" id="nc_ntfy_topic" placeholder="topic" maxlength="64"
                       value="${escapeHtml(s.ntfy.topic)}" autocomplete="off">
                <button class="btn btn-outline-secondary" type="button" id="nc_ntfy_gen">Generate</button>
            </div>
            <small class="text-muted d-block text-break">${url
                ? `Subscribe to <code>${escapeHtml(url)}</code> in the ntfy app.`
                : `Subscribe to this topic on <code>${escapeHtml(view.ntfy_server)}</code> in the ntfy app.`}</small>`;
    } else if (ch === 'telegram') {
        field = s.telegram.chat_id
            ? `<div class="small">Linked to <strong>${escapeHtml(s.telegram.chat_label || 'a chat')}</strong>
                 <button class="btn btn-link btn-sm p-0 ms-2 align-baseline" type="button" id="nc_tg_unlink">Unlink</button></div>`
            : `<button class="btn btn-outline-primary btn-sm" type="button" id="nc_tg_link">
                 <i class="fas fa-paper-plane me-1"></i>Link Telegram</button>
               <div id="nc_tg_status" class="small text-muted mt-1"></div>`;
    } else if (ch === 'signal') {
        field = s.signal.number
            ? `<div class="small">Verified <strong>${escapeHtml(s.signal.number)}</strong>
                 <button class="btn btn-link btn-sm p-0 ms-2 align-baseline" type="button" id="nc_sig_unlink">Remove</button></div>`
            : `<div class="input-group input-group-sm">
                 <input class="form-control" id="nc_sig_number" type="tel" inputmode="tel" placeholder="+447700900123"
                        autocomplete="tel">
                 <button class="btn btn-outline-primary" type="button" id="nc_sig_send">Send code</button>
               </div>
               <div id="nc_sig_confirm" class="input-group input-group-sm mt-2 d-none">
                 <input class="form-control" id="nc_sig_code" inputmode="numeric" maxlength="6"
                        placeholder="6-digit code" autocomplete="one-time-code">
                 <button class="btn btn-primary" type="button" id="nc_sig_ok">Verify</button>
               </div>
               <div id="nc_sig_status" class="small text-muted mt-1"></div>`;
    } else if (ch === 'pushover') {
        field = `<input class="form-control form-control-sm" id="nc_pushover_key" maxlength="30"
                        placeholder="Your Pushover user key" value="${escapeHtml(s.pushover.user_key)}" autocomplete="off">`;
    } else if (ch === 'email') {
        field = `<input class="form-control form-control-sm" id="nc_email_addr" type="email"
                        placeholder="you@example.com" value="${escapeHtml(s.email.address)}" autocomplete="email">`;
    }
    return `
        <div class="row g-2 align-items-center mb-3">
            <div class="col-12 col-md-3">
                <div class="form-check form-switch m-0">
                    <input class="form-check-input" type="checkbox" id="nc_${ch}_on" ${on}>
                    <label class="form-check-label fw-semibold" for="nc_${ch}_on">
                        <i class="fas ${LABELS[ch].icon} me-1"></i>${LABELS[ch].name}</label>
                </div>
            </div>
            <div class="col-12 col-md-9">${field}</div>
        </div>`;
}

function renderMine(el) {
    const s = view.settings;
    const avail = view.available;
    const missing = Object.keys(LABELS).filter(ch => !avail.includes(ch));
    el.innerHTML = `
        ${view.warnings.map(w => `<div class="alert alert-warning small py-2">${escapeHtml(w)}</div>`).join('')}
        ${avail.length ? avail.map(ch => channelRow(ch, s)).join('') : `
            <div class="text-muted small mb-2">No channels are set up on this hub yet.${isAdmin() ? ' Set them up below.' : ' Ask an admin.'}</div>`}
        ${avail.length && missing.length ? `<div class="small text-muted mb-2">Not set up on this hub:
            ${missing.map(ch => LABELS[ch].name).join(', ')}.</div>` : ''}
        ${avail.length ? `
        <div class="mb-3">
            <div class="small fw-semibold mb-1">Send</div>
            <div class="form-check">
                <input class="form-check-input" type="checkbox" id="nc_kind_rule" ${s.kinds.notification_rule ? 'checked' : ''}>
                <label class="form-check-label small" for="nc_kind_rule">Notification rules</label>
            </div>
            <div class="form-check">
                <input class="form-check-input" type="checkbox" id="nc_kind_msg" ${s.kinds.message_created ? 'checked' : ''}>
                <label class="form-check-label small" for="nc_kind_msg">Chat messages
                    <span class="text-muted">— the service can read them, unlike push</span></label>
            </div>
        </div>
        <div class="d-flex flex-wrap gap-2">
            <button class="btn btn-primary btn-sm" type="button" id="nc_save">Save</button>
            <button class="btn btn-outline-secondary btn-sm" type="button" id="nc_test">Send test</button>
        </div>
        <div id="nc_result" class="small mt-2"></div>` : ''}`;
    wireMine(el);
}

function collectMine() {
    const val = id => document.getElementById(id)?.value?.trim();
    const on = id => !!document.getElementById(id)?.checked;
    const body = { kinds: { notification_rule: on('nc_kind_rule'), message_created: on('nc_kind_msg') } };
    for (const ch of view.available) body[ch] = { enabled: on(`nc_${ch}_on`) };
    if (body.ntfy) body.ntfy.topic = val('nc_ntfy_topic') ?? '';
    if (body.pushover) body.pushover.user_key = val('nc_pushover_key') ?? '';
    if (body.email) body.email.address = val('nc_email_addr') ?? '';
    return body;
}

function resultLines(channels) {
    const entries = Object.entries(channels || {});
    if (!entries.length) return '<span class="text-muted">No channel is switched on and saved.</span>';
    return entries.map(([ch, r]) => r.ok
        ? `<div class="text-success"><i class="fas fa-check me-1"></i>${LABELS[ch]?.name || ch}: sent</div>`
        : `<div class="text-danger"><i class="fas fa-xmark me-1"></i>${LABELS[ch]?.name || ch}: ${escapeHtml(r.error || 'failed')}</div>`
    ).join('');
}

async function save(el, quiet) {
    try {
        view = await api('PUT', '/api/notify-channels', collectMine());
        renderMine(el);
        if (!quiet) window.toast?.success('Channels saved');
        return true;
    } catch (e) {
        window.toast?.error(`Couldn't save: ${e.message}`);
        return false;
    }
}

function wireMine(el) {
    const on = (id, fn) => document.getElementById(id)?.addEventListener('click', fn);
    on('nc_ntfy_gen', () => {
        document.getElementById('nc_ntfy_topic').value = randomTopic();
        document.getElementById('nc_ntfy_on').checked = true;
    });
    on('nc_save', () => save(el));
    on('nc_test', async ev => {
        const btn = ev.currentTarget;
        btn.disabled = true;
        // Test what's on screen, not what was last saved.
        if (await save(el, true)) {
            try {
                const r = await api('POST', '/api/notify-channels/test');
                document.getElementById('nc_result').innerHTML = resultLines(r.channels);
            } catch (e) {
                window.toast?.error(`Test failed: ${e.message}`);
            }
        }
        btn.disabled = false;
    });
    on('nc_tg_unlink', async () => {
        view = await api('PUT', '/api/notify-channels', { telegram: { unlink: true } }).catch(e => {
            window.toast?.error(e.message); return view;
        });
        renderMine(el);
    });
    on('nc_sig_unlink', async () => {
        view = await api('PUT', '/api/notify-channels', { signal: { unlink: true } }).catch(e => {
            window.toast?.error(e.message); return view;
        });
        renderMine(el);
    });
    on('nc_sig_send', async ev => {
        const btn = ev.currentTarget;
        const status = document.getElementById('nc_sig_status');
        btn.disabled = true;
        try {
            const r = await api('POST', '/api/notify-channels/signal/verify',
                                { number: document.getElementById('nc_sig_number').value.trim() });
            document.getElementById('nc_sig_confirm').classList.remove('d-none');
            status.textContent = `Code sent by Signal to ${r.number}. It's valid for 10 minutes.`;
            document.getElementById('nc_sig_code').focus();
        } catch (e) {
            status.textContent = e.message;
        }
        btn.disabled = false;
    });
    on('nc_sig_ok', async () => {
        const status = document.getElementById('nc_sig_status');
        try {
            const r = await api('POST', '/api/notify-channels/signal/confirm',
                                { code: document.getElementById('nc_sig_code').value.trim() });
            window.toast?.success(`Signal verified for ${r.number}`);
            view = await api('GET', '/api/notify-channels');
            renderMine(el);
        } catch (e) {
            status.textContent = e.message;
        }
    });
    on('nc_tg_link', async ev => {
        const btn = ev.currentTarget;
        const status = document.getElementById('nc_tg_status');
        btn.disabled = true;
        try {
            const r = await api('POST', '/api/notify-channels/telegram/link');
            window.open(r.url, '_blank', 'noopener');
            status.innerHTML = `Telegram should open on <strong>@${escapeHtml(r.bot)}</strong> — press <em>Start</em>.
                No app here? Send <code>/start ${escapeHtml(r.code)}</code> to <a href="${escapeHtml(r.url)}"
                target="_blank" rel="noopener">@${escapeHtml(r.bot)}</a>. Waiting…`;
            pollLink(el);
        } catch (e) {
            status.textContent = e.message;
            btn.disabled = false;
        }
    });
}

function pollLink(el) {
    clearInterval(linkPoll);
    const until = Date.now() + 10 * 60 * 1000;
    linkPoll = setInterval(async () => {
        if (Date.now() > until || !document.getElementById('nc_tg_status')) {
            clearInterval(linkPoll);
            return;
        }
        try {
            const r = await api('POST', '/api/notify-channels/telegram/check');
            if (r.linked) {
                clearInterval(linkPoll);
                window.toast?.success(`Telegram linked to ${r.chat_label || 'your chat'}`);
                view = await api('GET', '/api/notify-channels');
                renderMine(el);
            }
        } catch (e) {
            clearInterval(linkPoll);
            const s = document.getElementById('nc_tg_status');
            if (s) s.textContent = e.message;
        }
    }, 3000);
}

// Hub setup (admin)

function renderHub(el, hub) {
    const secret = (id, label, set, help = '') => `
        <div class="col-12 col-md-6">
            <label class="form-label small mb-1" for="${id}">${label}</label>
            <input class="form-control form-control-sm" type="password" id="${id}" autocomplete="new-password"
                   placeholder="${set ? '•••••• stored — blank keeps it' : ''}">
            ${set ? `<div class="form-check mt-1"><input class="form-check-input" type="checkbox" id="${id}_clear">
                <label class="form-check-label small" for="${id}_clear">Remove</label></div>` : ''}
            ${help ? `<small class="text-muted">${help}</small>` : ''}
        </div>`;
    const text = (id, label, value, attrs = '') => `
        <div class="col-12 col-md-6">
            <label class="form-label small mb-1" for="${id}">${label}</label>
            <input class="form-control form-control-sm" id="${id}" value="${escapeHtml(value ?? '')}" ${attrs}>
        </div>`;
    el.innerHTML = `
        <p class="small text-muted">Servers and tokens shared by everyone on this hub. Secrets are stored in
            <code>config/secrets.yaml</code> and never shown again.</p>
        <h6 class="mt-3"><i class="fas fa-bell me-1"></i>ntfy</h6>
        <div class="row g-2">
            ${text('nh_ntfy_server', 'Server', hub.ntfy_server, 'placeholder="https://ntfy.sh"')}
            ${secret('nh_ntfy_token', 'Access token (self-hosted servers with auth)', hub.ntfy_token_set)}
        </div>
        <h6 class="mt-3"><i class="fas fa-paper-plane me-1"></i>Telegram</h6>
        <div class="row g-2">
            ${secret('nh_telegram_bot_token', 'Bot token', hub.telegram_bot_token_set,
                     'Create a bot with @BotFather and paste its token.')}
        </div>
        <h6 class="mt-3"><i class="fas fa-comment-dots me-1"></i>Signal</h6>
        <div class="row g-2">
            ${text('nh_signal_api_url', 'signal-cli-rest-api URL', hub.signal_api_url, 'placeholder="http://192.168.1.1:8080"')}
            ${text('nh_signal_number', 'Hub\'s Signal number', hub.signal_number, 'type="tel" placeholder="+447700900123"')}
            <div class="col-12"><small class="text-muted">Signal has no bot API: run the signal-cli-rest-api
                container with a number registered or linked to it, and keep it on the LAN — it has no login of its own.</small></div>
        </div>
        <h6 class="mt-3"><i class="fas fa-mobile-screen-button me-1"></i>Pushover</h6>
        <div class="row g-2">
            ${secret('nh_pushover_app_token', 'Application API token', hub.pushover_app_token_set,
                     'From pushover.net → Create an Application.')}
        </div>
        <h6 class="mt-3"><i class="fas fa-envelope me-1"></i>Email (SMTP)</h6>
        <div class="row g-2">
            ${text('nh_smtp_host', 'Server', hub.smtp_host, 'placeholder="smtp.example.com"')}
            <div class="col-6 col-md-3">
                <label class="form-label small mb-1" for="nh_smtp_port">Port</label>
                <input class="form-control form-control-sm" type="number" id="nh_smtp_port" min="1" max="65535"
                       value="${escapeHtml(hub.smtp_port)}">
            </div>
            <div class="col-6 col-md-3">
                <label class="form-label small mb-1" for="nh_smtp_security">Security</label>
                <select class="form-select form-select-sm" id="nh_smtp_security">
                    ${['starttls', 'ssl', 'none'].map(v => `<option value="${v}" ${hub.smtp_security === v ? 'selected' : ''}>${
                        { starttls: 'STARTTLS', ssl: 'SSL/TLS', none: 'None' }[v]}</option>`).join('')}
                </select>
            </div>
            ${text('nh_smtp_from', 'From address', hub.smtp_from, 'type="email" placeholder="zmm@example.com"')}
            ${text('nh_smtp_username', 'Username', hub.smtp_username, 'autocomplete="off"')}
            ${secret('nh_smtp_password', 'Password', hub.smtp_password_set)}
        </div>
        <div class="d-flex flex-wrap gap-2 mt-3">
            <button class="btn btn-primary btn-sm" type="button" id="nh_save">Save hub setup</button>
        </div>`;

    document.getElementById('nh_save').addEventListener('click', async () => {
        const val = id => document.getElementById(id)?.value ?? '';
        const body = {
            ntfy_server: val('nh_ntfy_server').trim(),
            signal_api_url: val('nh_signal_api_url').trim(),
            signal_number: val('nh_signal_number').trim(),
            smtp_host: val('nh_smtp_host').trim(),
            smtp_port: Number(val('nh_smtp_port')) || 587,
            smtp_security: val('nh_smtp_security'),
            smtp_from: val('nh_smtp_from').trim(),
            smtp_username: val('nh_smtp_username').trim(),
        };
        for (const k of ['ntfy_token', 'telegram_bot_token', 'pushover_app_token', 'smtp_password']) {
            if (document.getElementById(`nh_${k}_clear`)?.checked) body[k] = null;
            else if (val(`nh_${k}`).trim()) body[k] = val(`nh_${k}`).trim();
        }
        try {
            const saved = await api('PUT', '/api/notify-channels/hub', body);
            renderHub(el, saved);
            window.toast?.success('Hub channels saved');
            await loadMine();
        } catch (e) {
            window.toast?.error(`Couldn't save: ${e.message}`);
        }
    });
}

async function loadMine() {
    const el = document.getElementById('notif-channels-panel');
    if (!el) return;
    try {
        view = await api('GET', '/api/notify-channels');
        renderMine(el);
    } catch (e) {
        el.innerHTML = `<div class="text-danger small">Couldn't load channels: ${escapeHtml(e.message)}</div>`;
    }
}

/** Card markup for the Notifications pane; call renderChannelCards() after inserting it. */
export function channelCardsHtml() {
    return `
        <div class="card shadow-sm mb-3">
            <div class="card-header bg-light py-2">
                <span class="fw-bold"><i class="fas fa-tower-broadcast me-1"></i> Other channels</span>
            </div>
            <div class="card-body">
                <p class="small text-muted mb-3">
                    Push needs the tunnel address; these work from a hub on the LAN.
                    They're yours alone — nobody else's notifications come here.
                </p>
                <div id="notif-channels-panel"><div class="text-muted small">Loading…</div></div>
            </div>
        </div>
        ${isAdmin() ? `
        <div class="card shadow-sm mb-3">
            <div class="card-header bg-light py-2">
                <span class="fw-bold"><i class="fas fa-server me-1"></i> Channel setup (admin)</span>
            </div>
            <div class="card-body" id="notif-channels-hub"><div class="text-muted small">Loading…</div></div>
        </div>` : ''}`;
}

export async function renderChannelCards() {
    await loadMine();
    const hubEl = document.getElementById('notif-channels-hub');
    if (hubEl) {
        try {
            renderHub(hubEl, await api('GET', '/api/notify-channels/hub'));
        } catch (e) {
            hubEl.innerHTML = `<div class="text-danger small">Couldn't load hub setup: ${escapeHtml(e.message)}</div>`;
        }
    }
}
