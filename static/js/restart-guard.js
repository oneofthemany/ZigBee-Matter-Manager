/**
 * Client half of the restart guard.
 *
 * The server refuses a restart outright while an upgrade is being verified
 * (modules/restart_guard.py) — this only makes that refusal visible before the
 * user commits, by disabling the restart controls and saying why. It is an
 * affordance, never the enforcement: every restart endpoint re-checks.
 *
 * Controls opt in with `data-restart-control`, so a new restart button is
 * covered by adding the attribute rather than by editing this file.
 */

const ENDPOINT = '/api/system/restart-allowed';
const POLL_MS = 10000;

let _pollTimer = null;
let _last = { allowed: true, reason: null };

/** Ask the server. Network failures resolve to "allowed" — the server still enforces. */
export async function fetchRestartStatus() {
    try {
        const res = await fetch(ENDPOINT, { cache: 'no-store' });
        if (!res.ok) return { allowed: true, reason: null };
        const data = await res.json();
        _last = { allowed: data.allowed !== false, reason: data.reason || null };
    } catch (e) {
        _last = { allowed: true, reason: null };
    }
    return _last;
}

export function lastRestartStatus() {
    return _last;
}

function humaniseWait(seconds) {
    if (!seconds || seconds < 60) return `about ${Math.max(15, seconds | 0)} seconds`;
    const mins = Math.round(seconds / 60);
    return `about ${mins} minute${mins === 1 ? '' : 's'}`;
}

/** One line fit for a toast or an alert box. */
export function restartBlockedText(reason) {
    if (!reason) return 'A restart is not possible right now.';
    return `${reason.message} Try again in ${humaniseWait(reason.retry_after_s)}.`;
}

/** Reflect the current state onto every opted-in control. */
export function applyRestartGuard(status = _last) {
    const blocked = status && status.allowed === false;
    const reason = status && status.reason;
    document.querySelectorAll('[data-restart-control]').forEach(el => {
        el.disabled = !!blocked;
        el.classList.toggle('disabled', !!blocked);
        if (blocked) {
            if (!el.dataset.restartTitle) el.dataset.restartTitle = el.title || '';
            el.title = restartBlockedText(reason);
        } else if (el.dataset.restartTitle !== undefined) {
            el.title = el.dataset.restartTitle;
            delete el.dataset.restartTitle;
        }
    });

    const banner = document.getElementById('restartGuardBanner');
    if (banner) {
        banner.classList.toggle('d-none', !blocked);
        if (blocked) {
            banner.innerHTML =
                '<i class="fas fa-shield-halved me-2"></i>' +
                `<strong>Restart temporarily unavailable.</strong> ${escapeHtml(restartBlockedText(reason))}`;
        }
    }
    return !blocked;
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

/**
 * Re-check before acting. Returns the reason to refuse, or null to proceed.
 * Always hits the network: the poll may be up to POLL_MS out of date, and a
 * swap can begin in that gap.
 */
export async function blockIfRestartForbidden() {
    const status = await fetchRestartStatus();
    applyRestartGuard(status);
    return status.allowed === false ? status.reason : null;
}

/** Start polling. Idempotent, so tabs can call it on every activation. */
export function startRestartGuardWatch() {
    if (_pollTimer) return;
    const tick = async () => applyRestartGuard(await fetchRestartStatus());
    tick();
    _pollTimer = setInterval(tick, POLL_MS);
}

export function stopRestartGuardWatch() {
    if (!_pollTimer) return;
    clearInterval(_pollTimer);
    _pollTimer = null;
}
