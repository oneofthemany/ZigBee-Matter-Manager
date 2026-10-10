/* Settings -> Notifications: per-device, per-event rules with cooldowns, time
   windows and condition logic. Rules live on the hub (modules/notification_rules.py),
   which evaluates them and pushes to the owner, so they fire with ZMM closed.
   This module is the editor plus in-page delivery. See docs/notifications.md. */

import { state } from './state.js';
import { escapeHtml, timeAgo } from './utils.js';
import { channelCardsHtml, renderChannelCards } from './notify-channels.js';

const log = zmmLog('notifications');

// Rules this browser kept before they moved to the hub; imported once, then cleared.
const LEGACY_RULES_KEY = 'zbm-notification-rules';

/**
 * Editor catalogue. Keys and labels mirror TRIGGERS in
 * modules/notification_rules.py, which does the matching; tests/notifications
 * checks the two agree.
 */
const TRIGGERS = {
    motion_detected:     { label: 'Motion detected',                   icon: 'fa-running',              category: 'motion' },
    motion_cleared:      { label: 'Motion cleared',                    icon: 'fa-shield-alt',           category: 'motion' },
    contact_opened:      { label: 'Door / window opened',              icon: 'fa-door-open',            category: 'contact' },
    contact_closed:      { label: 'Door / window closed',              icon: 'fa-door-closed',          category: 'contact' },
    water_leak:          { label: 'Water leak detected',               icon: 'fa-tint',                 category: 'safety' },
    smoke:               { label: 'Smoke detected',                    icon: 'fa-fire',                 category: 'safety' },
    vibration:           { label: 'Vibration / tamper',                icon: 'fa-bolt',                 category: 'safety' },
    button_pressed:      { label: 'Button pressed',                    icon: 'fa-hand-pointer',         category: 'control' },
    low_battery:         { label: 'Low battery (< 15%)',               icon: 'fa-battery-quarter',      category: 'maintenance' },
    offline:             { label: 'Device went offline',               icon: 'fa-plug',                 category: 'maintenance' },
    online:              { label: 'Device came online',                icon: 'fa-plug-circle-bolt',     category: 'maintenance' },
    temp_target_reached: { label: 'Heating target reached',            icon: 'fa-thermometer-half',     category: 'heating' },
    temp_above:          { label: 'Temperature rises above threshold', icon: 'fa-temperature-high',     category: 'heating', needsThreshold: true },
    temp_below:          { label: 'Temperature drops below threshold', icon: 'fa-temperature-low',      category: 'heating', needsThreshold: true },
    person_detected:     { label: 'Person seen on camera',             icon: 'fa-person',               category: 'camera' },
    vehicle_detected:    { label: 'Vehicle seen on camera',            icon: 'fa-car-side',             category: 'camera' },
    animal_detected:     { label: 'Animal seen on camera',             icon: 'fa-paw',                  category: 'camera' },
    valve_alarm:         { label: 'Valve alarm (TRV)',                 icon: 'fa-exclamation-triangle', category: 'heating' },
    window_open_trv:     { label: 'Window-open detected (TRV)',        icon: 'fa-window-maximize',      category: 'heating' },
};

// Sensible defaults for rule cooldowns
const COOLDOWN_OPTIONS = [
    { value: 0,  label: 'No cooldown' },
    { value: 1,  label: '1 minute' },
    { value: 5,  label: '5 minutes' },
    { value: 15, label: '15 minutes' },
    { value: 60, label: '1 hour' },
];

// Persistence — the hub's /api/notification-rules, cached for rendering

let rulesCache = [];

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

async function refreshRules() {
    rulesCache = (await api('GET', '/api/notification-rules')).rules || [];
    return rulesCache;
}

async function saveRule(rule) {
    return rule.id
        ? api('PUT', `/api/notification-rules/${encodeURIComponent(rule.id)}`, rule)
        : api('POST', '/api/notification-rules', rule);
}

let importing = null;

/** Upload rules this browser kept locally, once; they then run on the hub. */
function importLegacyRules() {
    // onChange can fire twice in quick succession; one upload, not two copies.
    importing ??= doImportLegacyRules().finally(() => { importing = null; });
    return importing;
}

async function doImportLegacyRules() {
    let legacy;
    try { legacy = JSON.parse(localStorage.getItem(LEGACY_RULES_KEY) || 'null'); } catch (e) { return; }
    if (!Array.isArray(legacy) || legacy.length === 0) return;
    try {
        const { imported, errors } = await api('POST', '/api/notification-rules/import', { rules: legacy });
        localStorage.removeItem(LEGACY_RULES_KEY);
        if (imported && window.toast) {
            window.toast.success(`Moved ${imported} notification rule${imported === 1 ? '' : 's'} to the hub — they now work with ZMM closed.`);
        }
        if (errors.length) log.warn('[notifications] rules not imported:', errors);
    } catch (e) {
        log.warn('[notifications] legacy import failed; will retry next load', e);
    }
}

// In-page delivery

async function hasPushSubscription() {
    try {
        if (!window.isSecureContext || !navigator.serviceWorker) return false;
        const reg = await navigator.serviceWorker.getRegistration();
        return !!(reg && await reg.pushManager.getSubscription());
    } catch (e) {
        return false;
    }
}

/**
 * A rule fired on the hub. With push on this browser the system notification
 * arrives that way, so the page only adds a toast while visible; without push
 * (e.g. the LAN address) the page raises it itself, as before.
 */
async function handleRuleFired(p) {
    if (await hasPushSubscription()) {
        if (document.visibilityState === 'visible' && window.toast) window.toast.info(`${p.title}: ${p.body}`);
        return;
    }
    const send = window.zbmSendNotification;
    // Falsy means the master toggle is off, not a failure — still show it somewhere.
    const delivered = typeof send === 'function' && send(p.title, p.body, p.tag, { persistent: !!p.persistent });
    if (!delivered && window.toast) window.toast.info(`${p.title}: ${p.body}`);
}

// UI rendering

function getAllDevices() {
    const cache = (window.state && window.state.deviceCache) || {};
    return Object.values(cache)
        .filter(d => d && d.ieee)
        .sort((a, b) => (a.friendly_name || '').localeCompare(b.friendly_name || ''));
}

function renderRulesList() {
    const container = document.getElementById('notifRulesList');
    if (!container) return;

    const rules = rulesCache;
    if (rules.length === 0) {
        container.innerHTML = `
            <div class="text-center text-muted py-5">
                <i class="fas fa-bell-slash fa-2x mb-2 d-block"></i>
                <div>No notification rules yet.</div>
                <div class="small">Click <strong>Add Rule</strong> to get notified about motion, doors, leaks, heating events and more.</div>
            </div>
        `;
        return;
    }

    container.innerHTML = rules.map(rule => {
        const trigger = TRIGGERS[rule.trigger];
        const triggerLabel = trigger ? trigger.label : rule.trigger;
        const icon = trigger ? trigger.icon : 'fa-bell';
        const enabled = rule.enabled !== false;

        let scopeText = 'All devices';
        if (rule.scope === 'devices') {
            const n = (rule.devices || []).length;
            scopeText = `${n} device${n === 1 ? '' : 's'}`;
        } else if (rule.scope === 'tab') {
            scopeText = `Tab: ${rule.tab}`;
        }

        let extras = '';
        if (rule.threshold != null && rule.threshold !== '') {
            extras += `<span class="badge bg-light text-dark border me-1">Threshold: ${escapeHtml(rule.threshold)}</span>`;
        }
        if (rule.timeFrom && rule.timeTo) {
            extras += `<span class="badge bg-light text-dark border me-1"><i class="far fa-clock me-1"></i>${escapeHtml(rule.timeFrom)}–${escapeHtml(rule.timeTo)}</span>`;
        }
        if (rule.cooldownMinutes) {
            extras += `<span class="badge bg-light text-dark border me-1">Cooldown ${rule.cooldownMinutes}m</span>`;
        }

        return `
            <div class="card notif-rule-card mb-2 ${enabled ? '' : 'opacity-50'}" data-rule-id="${escapeHtml(rule.id)}">
                <div class="card-body py-2 px-3">
                    <div class="d-flex flex-wrap flex-sm-nowrap align-items-center gap-2">
                        <i class="fas ${icon} fa-fw text-primary"></i>
                        <div class="flex-grow-1 min-w-0">
                            <div class="fw-semibold text-truncate">${escapeHtml(rule.title || triggerLabel)}</div>
                            <div class="small text-muted">
                                ${escapeHtml(triggerLabel)} · ${escapeHtml(scopeText)}
                            </div>
                            <div class="mt-1">${extras}</div>
                            ${lastFiredLine(rule.last_fired)}
                        </div>
                        <div class="notif-rule-actions d-flex align-items-center gap-2">
                        <div class="form-check form-switch m-0">
                            <input class="form-check-input" type="checkbox" data-action="toggle" ${enabled ? 'checked' : ''} aria-label="Rule enabled">
                        </div>
                        <button class="btn btn-sm btn-outline-primary" data-action="test" title="Send test" aria-label="Send a test notification">
                            <i class="fas fa-paper-plane"></i>
                        </button>
                        <button class="btn btn-sm btn-outline-secondary" data-action="edit" title="Edit" aria-label="Edit rule">
                            <i class="fas fa-pen"></i>
                        </button>
                        <button class="btn btn-sm btn-outline-danger" data-action="delete" title="Delete" aria-label="Delete rule">
                            <i class="fas fa-trash"></i>
                        </button>
                        </div>
                    </div>
                </div>
            </div>
        `;
    }).join('');

    // Wire up per-card actions
    container.querySelectorAll('[data-rule-id]').forEach(card => {
        const id = card.dataset.ruleId;
        card.querySelector('[data-action="toggle"]').addEventListener('change', async (ev) => {
            const r = rulesCache.find(x => x.id === id);
            if (!r) return;
            try {
                await saveRule({ ...r, enabled: ev.target.checked });
                await refreshRules();
            } catch (e) {
                window.toast?.error(`Couldn't update rule: ${e.message}`);
            }
            renderRulesList();
        });
        card.querySelector('[data-action="test"]').addEventListener('click', ev => sendTest(id, ev.currentTarget));
        card.querySelector('[data-action="edit"]').addEventListener('click', () => openRuleEditor(id));
        card.querySelector('[data-action="delete"]').addEventListener('click', async () => {
            if (!await window.zbmConfirm({
                title: 'Delete rule',
                message: 'Delete this notification rule?',
                confirmText: 'Delete',
                variant: 'danger'
            })) return;
            try {
                await api('DELETE', `/api/notification-rules/${encodeURIComponent(id)}`);
                await refreshRules();
            } catch (e) {
                window.toast?.error(`Couldn't delete rule: ${e.message}`);
            }
            renderRulesList();
        });
    });
}

// Last fired / test

function lastFiredLine(last) {
    if (!last) return '<div class="small text-muted mt-1">Hasn\'t fired yet.</div>';
    const at = last.at * 1000;
    return `<div class="small text-muted mt-1" title="${escapeHtml(new Date(at).toLocaleString())}">
        <i class="fas fa-clock-rotate-left me-1"></i>Last fired ${escapeHtml(timeAgo(at))} — ${escapeHtml(last.body)}</div>`;
}

/** Say where a test went, so a missing phone notification has an explanation. */
function testOutcome(r) {
    const n = (count, what) => `${count} ${what}${count === 1 ? '' : 's'}`;
    const chans = Object.entries(r.channels || {});
    const okChans = chans.filter(([, c]) => c.ok).map(([ch]) => ch);
    const badChans = chans.filter(([, c]) => !c.ok).map(([ch]) => ch);
    const chanNote = (okChans.length ? ` Also sent via ${okChans.join(', ')}.` : '')
        + (badChans.length ? ` Failed: ${badChans.join(', ')} — see Other channels.` : '');
    const [kind, text] = pushOutcome(r, n, okChans.length > 0);
    return [badChans.length && kind === 'success' ? 'warning' : kind, text + chanNote];
}

function pushOutcome(r, n, viaChannels) {
    if (!('sent' in r) && !r.no_subscriptions) return ['info', 'Test sent.'];
    if (r.no_subscriptions && viaChannels) {
        return ['success', 'Test sent (no device has web push enabled).'];
    }
    if (r.no_subscriptions) {
        return ['warning', 'Test sent, but no phone or browser has push enabled for your account, so it only shows on open ZMM pages. '
            + 'Enable push under "Delivery on this device".'];
    }
    if (r.sent) {
        const failed = r.failed ? ` (${n(r.failed, 'device')} failed)` : '';
        return ['success', `Test pushed to ${n(r.sent, 'device')}${failed}.`];
    }
    return ['error', `Test push failed on ${n(r.failed || 0, 'device')}. Check "Delivery on this device".`];
}

async function sendTest(id, btn) {
    btn.disabled = true;
    try {
        const r = await api('POST', `/api/notification-rules/${encodeURIComponent(id)}/test`);
        const [kind, text] = testOutcome(r);
        window.toast?.[kind](text);
    } catch (e) {
        window.toast?.error(`Couldn't send test: ${e.message}`);
    } finally {
        btn.disabled = false;
    }
}

// Rule editor modal

async function openRuleEditor(ruleId) {
    const rule = ruleId
        ? rulesCache.find(r => r.id === ruleId)
        : {
            enabled: true,
            trigger: 'motion_detected',
            scope: 'all',
            devices: [],
            cooldownMinutes: 5,
        };
    if (!rule) return;

    // Remove any existing editor
    document.getElementById('notifRuleEditorModal')?.remove();

    const triggersByCategory = {};
    Object.entries(TRIGGERS).forEach(([key, t]) => {
        if (!triggersByCategory[t.category]) triggersByCategory[t.category] = [];
        triggersByCategory[t.category].push({ key, ...t });
    });

    const categoryLabels = {
        motion:      'Motion',
        contact:     'Doors & Windows',
        safety:      'Safety',
        control:     'Buttons & Controls',
        maintenance: 'Maintenance',
        heating:     'Heating',
        camera:      'Cameras',
    };

    const triggerOptions = Object.entries(triggersByCategory)
        .map(([cat, items]) => `
            <optgroup label="${escapeHtml(categoryLabels[cat] || cat)}">
                ${items.map(t => `
                    <option value="${t.key}" ${t.key === rule.trigger ? 'selected' : ''}>${escapeHtml(t.label)}</option>
                `).join('')}
            </optgroup>
        `).join('');

    const devices = getAllDevices();
    const deviceCheckboxes = devices.map(d => {
        const checked = (rule.devices || []).includes(d.ieee) ? 'checked' : '';
        // Searchable haystack — friendly name + ieee suffix, lowercased once
        const haystack = `${d.friendly_name || ''} ${d.ieee || ''}`.toLowerCase();
        return `
            <label class="list-group-item d-flex align-items-center gap-2 notif-device-item"
                   data-haystack="${escapeHtml(haystack)}">
                <input class="form-check-input m-0" type="checkbox" value="${escapeHtml(d.ieee)}" ${checked}>
                <span class="flex-grow-1 text-truncate">${escapeHtml(d.friendly_name || d.ieee)}</span>
                <small class="text-muted">${escapeHtml(d.protocol || 'zigbee')}</small>
            </label>
        `;
    }).join('');

    let tabs = {};
    try { tabs = await api('GET', '/api/tabs'); } catch (e) { log.warn('[notifications] tabs unavailable', e); }
    // Empty for an account that can't view cameras: the picker just isn't shown.
    let cams = [];
    try { cams = (await api('GET', '/api/cameras')).cameras || []; } catch (e) { /* no camera:read */ }
    const tabOptions = Object.keys(tabs).map(t =>
        `<option value="${escapeHtml(t)}" ${rule.tab === t ? 'selected' : ''}>${escapeHtml(t)}</option>`
    ).join('');

    const html = `
        <div class="modal fade" id="notifRuleEditorModal" tabindex="-1">
            <div class="modal-dialog modal-lg">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">
                            <i class="fas fa-bell me-2"></i>${ruleId ? 'Edit' : 'Add'} Notification Rule
                        </h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">

                        <div class="mb-3">
                            <label class="form-label fw-bold">When this happens</label>
                            <select class="form-select" id="notifRuleTrigger">${triggerOptions}</select>
                        </div>

                        <div class="mb-3" id="notifRuleThresholdWrap" style="display:none;">
                            <label class="form-label fw-bold">Threshold (°C)</label>
                            <input type="number" step="0.1" class="form-control" id="notifRuleThreshold"
                                   value="${escapeHtml(rule.threshold ?? '')}" placeholder="e.g. 5">
                            <small class="text-muted">The rule fires when the temperature crosses this value.</small>
                        </div>

                        <div class="mb-3">
                            <label class="form-label fw-bold">For which devices</label>
                            <div class="btn-group w-100" role="group">
                                <input type="radio" class="btn-check" name="notifRuleScope" id="scopeAll" value="all" ${rule.scope === 'all' ? 'checked' : ''}>
                                <label class="btn btn-outline-primary" for="scopeAll">All devices</label>

                                <input type="radio" class="btn-check" name="notifRuleScope" id="scopeDevices" value="devices" ${rule.scope === 'devices' ? 'checked' : ''}>
                                <label class="btn btn-outline-primary" for="scopeDevices">Selected</label>

                                <input type="radio" class="btn-check" name="notifRuleScope" id="scopeTab" value="tab" ${rule.scope === 'tab' ? 'checked' : ''} ${Object.keys(tabs).length === 0 ? 'disabled' : ''}>
                                <label class="btn btn-outline-primary" for="scopeTab">Device tab</label>
                            </div>

                            <div id="notifRuleDevicesWrap" class="mt-2" style="display:${rule.scope === 'devices' ? 'block' : 'none'};">
                                <input type="search" class="form-control form-control-sm mb-2" id="notifRuleDeviceFilter" placeholder="Search devices...">
                                <div class="list-group notif-device-list" id="notifRuleDevices" style="max-height: 240px; overflow-y: auto;">
                                    ${deviceCheckboxes || '<div class="text-muted small p-2">No devices loaded yet.</div>'}
                                </div>
                            </div>

                            <div id="notifRuleTabWrap" class="mt-2" style="display:${rule.scope === 'tab' ? 'block' : 'none'};">
                                <select class="form-select" id="notifRuleTab">
                                    <option value="">-- Choose a tab --</option>
                                    ${tabOptions}
                                </select>
                            </div>
                        </div>

                        <div class="row g-2 mb-3">
                            <div class="col-6">
                                <label class="form-label fw-bold">Only between</label>
                                <input type="time" class="form-control" id="notifRuleTimeFrom" value="${escapeHtml(rule.timeFrom ?? '')}">
                            </div>
                            <div class="col-6">
                                <label class="form-label fw-bold">and</label>
                                <input type="time" class="form-control" id="notifRuleTimeTo" value="${escapeHtml(rule.timeTo ?? '')}">
                            </div>
                            <small class="text-muted ps-2">Leave blank to fire any time of day.</small>
                        </div>

                        <div class="mb-3">
                            <label class="form-label fw-bold">Cooldown</label>
                            <select class="form-select" id="notifRuleCooldown">
                                ${COOLDOWN_OPTIONS.map(o => `
                                    <option value="${o.value}" ${Number(rule.cooldownMinutes) === o.value ? 'selected' : ''}>${o.label}</option>
                                `).join('')}
                            </select>
                            <small class="text-muted">Don't re-notify the same device within this window.</small>
                        </div>

                        <div class="mb-3">
                            <label class="form-label fw-bold">Notification title <span class="text-muted small">(optional)</span></label>
                            <input type="text" class="form-control" id="notifRuleTitle" value="${escapeHtml(rule.title ?? '')}" placeholder="Default: trigger name">
                        </div>

                        <div class="mb-3">
                            <label class="form-label fw-bold">Notification message <span class="text-muted small">(optional)</span></label>
                            <input type="text" class="form-control" id="notifRuleMessage" value="${escapeHtml(rule.message ?? '')}" placeholder="Use {device} for the device name">
                        </div>

                        ${cams.length ? `<div class="mb-3">
                            <label class="form-label fw-bold" for="notifRuleCamera">Attach a snapshot from</label>
                            <select class="form-select" id="notifRuleCamera">
                                <option value="">The camera it fired on, if any</option>
                                ${cams.map(c => `<option value="${escapeHtml(c.id)}" ${c.id === rule.camera ? 'selected' : ''}>${escapeHtml(c.name)}</option>`).join('')}
                            </select>
                            <small class="text-muted">A door opening can send the porch camera's view. Pictures go to ntfy,
                                Telegram, Signal, Pushover and email once you turn on <strong>Camera snapshots</strong>
                                under Other channels — not to browser push.</small>
                        </div>` : ''}

                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                        <button type="button" class="btn btn-primary" id="notifRuleSave">
                            <i class="fas fa-save me-1"></i>Save Rule
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', html);
    const modalEl = document.getElementById('notifRuleEditorModal');
    const modal = new bootstrap.Modal(modalEl);

    // Show/hide threshold field per trigger
    function syncThresholdVisibility() {
        const sel = document.getElementById('notifRuleTrigger').value;
        const t = TRIGGERS[sel];
        document.getElementById('notifRuleThresholdWrap').style.display =
            (t && t.needsThreshold) ? 'block' : 'none';
    }
    document.getElementById('notifRuleTrigger').addEventListener('change', syncThresholdVisibility);
    syncThresholdVisibility();

    // Scope radio handling
    modalEl.querySelectorAll('input[name="notifRuleScope"]').forEach(radio => {
        radio.addEventListener('change', () => {
            const v = modalEl.querySelector('input[name="notifRuleScope"]:checked').value;
            document.getElementById('notifRuleDevicesWrap').style.display = v === 'devices' ? 'block' : 'none';
            document.getElementById('notifRuleTabWrap').style.display     = v === 'tab'     ? 'block' : 'none';
        });
    });

    // Live device filter — matches against pre-built haystack (name + ieee).
    // Uses the `hidden` attribute rather than style.display so we don't fight
    // Bootstrap's flexbox display rules on .list-group-item.
    const filterInput = document.getElementById('notifRuleDeviceFilter');
    function applyDeviceFilter() {
        const q = (filterInput.value || '').trim().toLowerCase();
        modalEl.querySelectorAll('#notifRuleDevices .notif-device-item').forEach(lbl => {
            const hay = lbl.dataset.haystack || '';
            lbl.hidden = q && !hay.includes(q);
        });
    }
    if (filterInput) {
        filterInput.addEventListener('input', applyDeviceFilter);
        // `search` event fires on the × clear button in type=search inputs
        filterInput.addEventListener('search', applyDeviceFilter);
    }

    // Save handler
    document.getElementById('notifRuleSave').addEventListener('click', async () => {
        const triggerKey = document.getElementById('notifRuleTrigger').value;
        const trigger = TRIGGERS[triggerKey];
        const scope = modalEl.querySelector('input[name="notifRuleScope"]:checked').value;

        const selectedDevices = Array.from(
            modalEl.querySelectorAll('#notifRuleDevices input[type="checkbox"]:checked')
        ).map(cb => cb.value);

        const updated = {
            ...rule,
            trigger: triggerKey,
            scope,
            devices: scope === 'devices' ? selectedDevices : [],
            tab: scope === 'tab' ? document.getElementById('notifRuleTab').value : null,
            timeFrom: document.getElementById('notifRuleTimeFrom').value || null,
            timeTo:   document.getElementById('notifRuleTimeTo').value   || null,
            cooldownMinutes: Number(document.getElementById('notifRuleCooldown').value) || 0,
            title:   document.getElementById('notifRuleTitle').value.trim()   || null,
            message: document.getElementById('notifRuleMessage').value.trim() || null,
            camera:  document.getElementById('notifRuleCamera')?.value || null,
            threshold: trigger?.needsThreshold
                ? document.getElementById('notifRuleThreshold').value
                : undefined,
        };

        // Validation
        if (scope === 'devices' && updated.devices.length === 0) {
            window.toast.warning('Pick at least one device, or change the scope to "All devices".');
            return;
        }
        if (scope === 'tab' && !updated.tab) {
            window.toast.warning('Pick a device tab.');
            return;
        }
        if (trigger?.needsThreshold && (updated.threshold === '' || updated.threshold === undefined)) {
            window.toast.warning('This trigger requires a threshold value.');
            return;
        }

        try {
            await saveRule(updated);
            await refreshRules();
        } catch (e) {
            window.toast.error(`Couldn't save rule: ${e.message}`);
            return;
        }

        modal.hide();
        renderRulesList();
        window.toast?.success(rule.id ? 'Rule updated' : 'Rule added');
    });

    modalEl.addEventListener('hidden.bs.modal', () => modalEl.remove());
    modal.show();
}

// Sub-tab init

function renderNotificationsPane() {
    const pane = document.getElementById('settingsNotifications');
    if (!pane || pane.dataset.rendered === '1') return;

    pane.innerHTML = `
        <div class="card shadow-sm mb-3">
            <div class="card-header bg-light py-2">
                <span class="fw-bold"><i class="fas fa-mobile-screen me-1"></i> Delivery on this device</span>
            </div>
            <div class="card-body">
                <p class="small text-muted mb-2">
                    Whether ZMM can ping <strong>this</strong> phone/browser — including
                    chat messages and automation messages — with the app closed.
                    "Test (server push)" proves the whole hub → push-service → device chain.
                </p>
                <div id="notif-push-panel"></div>
            </div>
        </div>
        ${channelCardsHtml()}
        <div class="card shadow-sm">
            <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
                <span class="fw-bold"><i class="fas fa-bell me-1"></i> Notification Rules</span>
                <button class="btn btn-sm btn-primary" id="notifAddRuleBtn">
                    <i class="fas fa-plus me-1"></i> Add Rule
                </button>
            </div>
            <div class="card-body">
                <div class="alert alert-info small mb-3" id="notifGlobalStatus">
                    <i class="fas fa-info-circle me-1"></i>
                    Rules run on the hub and notify you on every device where push is
                    enabled above — even with ZMM closed. Pages you have open show them too.
                </div>
                <div id="notifRulesList"></div>
            </div>
        </div>
    `;

    document.getElementById('notifAddRuleBtn').addEventListener('click', () => openRuleEditor(null));

    // Shared push panel from pwa.js (same one as the navbar bell modal).
    if (window.zbmRenderPushPanel) {
        window.zbmRenderPushPanel(document.getElementById('notif-push-panel'));
    } else {
        const h = document.getElementById('notif-push-panel');
        if (h) h.innerHTML = '<div class="text-muted small">Push panel unavailable (pwa.js not loaded).</div>';
    }

    pane.dataset.rendered = '1';
    renderChannelCards();
    refreshRules()
        .catch(e => window.toast?.error(`Couldn't load notification rules: ${e.message}`))
        .finally(renderRulesList);
}

export function initNotifications() {
    // Hook the Settings → Notifications sub-tab
    const tabBtn = document.querySelector('[data-bs-target="#settingsNotifications"]');
    if (tabBtn) {
        tabBtn.addEventListener('shown.bs.tab', renderNotificationsPane);
    }

    // Re-render the rule list when the device cache changes (so new devices
    // appear in the picker without a reload).
    document.addEventListener('zbm:devices-updated', () => {
        // Cheap: only refresh the list view, not the editor modal
        if (document.getElementById('notifRulesList')) {
            renderRulesList();
        }
    });

    window.zbmHandleRuleFired = handleRuleFired;

    // Signed in is when the import can be attributed to someone.
    window.zmmAuth?.onChange(principal => { if (principal) importLegacyRules(); });
}