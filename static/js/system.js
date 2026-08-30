/**
 * System & Configuration Management
 * Handles config.yaml editing and system restart
 */

import { state } from './state.js';
import { confirmDialog } from './dialogs.js';
import { blockIfRestartForbidden, restartBlockedText, applyRestartGuard } from './restart-guard.js';

/**
 * Load configuration YAML into editor
 */
export async function loadConfigYaml() {
    const editor = document.getElementById('configEditor');
    if (!editor) return;

    try {
        const res = await fetch('/api/config');
        const data = await res.json();
        if (data.success) editor.value = data.content;
    } catch (e) {
        // Silent fail
    }
}

/**
 * Save configuration YAML
 */
export async function saveConfigYaml() {
    const editor = document.getElementById('configEditor');
    if (!editor) return;
    if (!await confirmDialog({
        title: 'Save configuration',
        message: 'Save config.yaml?',
        confirmText: 'Save'
    })) return;

    await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: editor.value })
    });
    window.toast.success("Saved");
}

/**
 * Restart the Zigbee service
 */
export async function restartSystem() {
    const blocked = await blockIfRestartForbidden();
    if (blocked) {
        window.toast.warning(restartBlockedText(blocked), { timeout: 12000 });
        return;
    }

    if (!await confirmDialog({
        title: 'Restart service',
        message: 'Restart the service now?',
        detail: 'The app will be unavailable for a short while.',
        confirmText: 'Restart',
        variant: 'danger'
    })) return;

    const res = await fetch('/api/system/restart', { method: 'POST' });
    if (res && res.status === 409) {
        // A swap began between the pre-check and this call.
        const body = await res.json().catch(() => ({}));
        applyRestartGuard({ allowed: false, reason: body.reason });
        window.toast.warning(restartBlockedText(body.reason), { timeout: 12000 });
        return;
    }
    state.isRestarting = true;
    setTimeout(() => location.reload(), 15000);
}

// HTTPS is always on (self-signed cert auto-generated at boot by
// modules/ssl_bootstrap.py) — there is deliberately no HTTP option, so the
// old SSL toggle UI and its /api/ssl/* endpoints are gone.