/**
 * app-alerts.js
 * --------------------------------------------------------------------------
 * Application alert center (backend-originated problems).
 *
 * Backed by /api/alerts (modules/app_alerts.py). Live alerts arrive over
 * the WebSocket as `app_alert` events — websocket.js re-dispatches them as
 * a `zmm-app-alert` CustomEvent which this module consumes.
 *
 * Distinct from notifications.js (user-defined device-event rules): these
 * are errors/warnings the application itself raises — disabled automations,
 * repeated device write failures, subsystem errors — so problems surface
 * in the UI instead of dying quietly in the log.
 * --------------------------------------------------------------------------
 */

const SEV_ICON = {
    error:   'fa-exclamation-circle text-danger',
    warning: 'fa-exclamation-triangle text-warning',
    info:    'fa-info-circle text-info',
};

let alerts = [];
let panelOpen = false;

function timeAgo(ts) {
    const s = Math.max(0, (Date.now() / 1000) - ts);
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str ?? '';
    return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Navbar bell + badge
// ---------------------------------------------------------------------------

function createBell() {
    if (document.getElementById('zbm-alerts-bell')) return;
    const navbar = document.querySelector('.navbar .d-flex.align-items-center.gap-3');
    if (!navbar) return;

    const btn = document.createElement('button');
    btn.id = 'zbm-alerts-bell';
    btn.className = 'btn btn-sm btn-outline-light border-0';
    btn.title = 'Application alerts';
    btn.style.cssText = 'font-size:1rem;padding:0.25rem 0.5rem;opacity:0.8;position:relative;';
    btn.innerHTML =
        '<i class="fas fa-exclamation-triangle"></i>' +
        '<span id="zbm-alerts-badge" class="badge rounded-pill bg-danger" ' +
        'style="position:absolute;top:-2px;right:-4px;font-size:0.6rem;display:none;"></span>';
    btn.onmouseenter = function () { this.style.opacity = '1'; };
    btn.onmouseleave = function () { this.style.opacity = '0.8'; };
    btn.addEventListener('click', togglePanel);

    // Sit next to the notification-rules bell if it exists
    const notifBell = document.getElementById('zbm-notif-bell');
    if (notifBell) {
        navbar.insertBefore(btn, notifBell);
    } else {
        const themeBtn = document.getElementById('themeToggleBtn');
        if (themeBtn) navbar.insertBefore(btn, themeBtn);
        else navbar.appendChild(btn);
    }
}

function updateBadge() {
    const badge = document.getElementById('zbm-alerts-badge');
    if (!badge) return;
    const n = alerts.length;
    badge.textContent = n > 99 ? '99+' : String(n);
    badge.style.display = n > 0 ? 'inline-block' : 'none';
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

function togglePanel() {
    panelOpen ? closePanel() : openPanel();
}

function closePanel() {
    document.getElementById('zbm-alerts-panel')?.remove();
    panelOpen = false;
}

function openPanel() {
    closePanel();
    panelOpen = true;

    const panel = document.createElement('div');
    panel.id = 'zbm-alerts-panel';
    panel.className = 'card shadow';
    panel.style.cssText =
        'position:fixed;top:56px;right:12px;width:min(420px,calc(100vw - 24px));' +
        'max-height:70vh;z-index:1055;display:flex;flex-direction:column;';
    document.body.appendChild(panel);
    renderPanel();

    // Click-away close (deferred so this click doesn't immediately close it)
    setTimeout(() => {
        document.addEventListener('click', onDocClick);
    }, 0);
}

function onDocClick(e) {
    const panel = document.getElementById('zbm-alerts-panel');
    const bell = document.getElementById('zbm-alerts-bell');
    if (!panel) {
        document.removeEventListener('click', onDocClick);
        return;
    }
    if (!panel.contains(e.target) && !bell?.contains(e.target)) {
        closePanel();
        document.removeEventListener('click', onDocClick);
    }
}

function renderPanel() {
    const panel = document.getElementById('zbm-alerts-panel');
    if (!panel) return;

    const rows = alerts.length === 0
        ? '<div class="text-muted text-center py-4"><i class="fas fa-check-circle me-1"></i>No active alerts</div>'
        : alerts.map((a) => `
            <div class="d-flex align-items-start gap-2 border-bottom px-3 py-2" data-alert-id="${a.id}">
                <i class="fas ${SEV_ICON[a.severity] || SEV_ICON.error} mt-1"></i>
                <div class="flex-grow-1" style="min-width:0;">
                    <div class="fw-semibold" style="font-size:0.85rem;">${escapeHtml(a.title)}
                        ${a.count > 1 ? `<span class="badge bg-secondary ms-1">×${a.count}</span>` : ''}
                    </div>
                    <div class="text-muted" style="font-size:0.78rem;word-break:break-word;">${escapeHtml(a.message)}</div>
                    <div class="text-muted" style="font-size:0.7rem;">${escapeHtml(a.source)} · ${timeAgo(a.last_seen || a.ts)}</div>
                </div>
                <button class="btn btn-sm btn-outline-secondary border-0 zbm-alert-dismiss" title="Dismiss">
                    <i class="fas fa-times"></i>
                </button>
            </div>`).join('');

    panel.innerHTML = `
        <div class="card-header d-flex align-items-center justify-content-between py-2">
            <span><i class="fas fa-exclamation-triangle me-2"></i>Application Alerts</span>
            <button id="zbm-alerts-clear" class="btn btn-sm btn-outline-secondary" ${alerts.length ? '' : 'disabled'}>
                Clear all
            </button>
        </div>
        <div style="overflow-y:auto;">${rows}</div>`;

    panel.querySelectorAll('.zbm-alert-dismiss').forEach((btn) => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const id = btn.closest('[data-alert-id]')?.dataset.alertId;
            if (!id) return;
            await fetch(`/api/alerts/${id}/dismiss`, { method: 'POST' }).catch(() => {});
            alerts = alerts.filter((a) => a.id !== id);
            updateBadge();
            renderPanel();
        });
    });

    panel.querySelector('#zbm-alerts-clear')?.addEventListener('click', async () => {
        await fetch('/api/alerts/clear', { method: 'POST' }).catch(() => {});
        alerts = [];
        updateBadge();
        renderPanel();
    });
}

// ---------------------------------------------------------------------------
// Data flow
// ---------------------------------------------------------------------------

async function fetchAlerts() {
    try {
        const r = await fetch('/api/alerts');
        const data = await r.json();
        alerts = data.alerts || [];
        updateBadge();
        if (panelOpen) renderPanel();
    } catch (e) { /* backend not up yet — badge stays hidden */ }
}

function onLiveAlert(e) {
    const a = e.detail;
    if (!a || !a.id) return;
    const idx = alerts.findIndex((x) => x.id === a.id);
    if (idx >= 0) alerts[idx] = a;
    else alerts.unshift(a);
    updateBadge();
    if (panelOpen) renderPanel();
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

export function initAppAlerts() {
    const boot = () => {
        createBell();
        fetchAlerts();
    };
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
    window.addEventListener('zmm-app-alert', onLiveAlert);
}
