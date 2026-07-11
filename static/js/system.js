/**
 * System & Configuration Management
 * Handles config.yaml editing and system restart
 */

import { state } from './state.js';
import { confirmDialog } from './dialogs.js';

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
    if (!await confirmDialog({
        title: 'Restart service',
        message: 'Restart the service now?',
        detail: 'The app will be unavailable for a short while.',
        confirmText: 'Restart',
        variant: 'danger'
    })) return;

    state.isRestarting = true;
    await fetch('/api/system/restart', { method: 'POST' });
    setTimeout(() => location.reload(), 15000);
}

/**
 * SSL Status
 */

export async function loadSSLStatus() {
    const r = await fetch('/api/ssl/status');
    const d = await r.json();
    // #sslToggle lives in the dynamically rendered Config sub-tabs — it may
    // not be in the DOM yet on early calls.
    const toggle = document.getElementById('sslToggle');
    if (toggle) toggle.checked = d.enabled;
}

export async function toggleSSL(enabled) {
    const r = await fetch('/api/ssl/toggle', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ enabled })
    });
    const d = await r.json();

    if (!d.success) {
        window.toast.error('SSL toggle failed: ' + d.error);
        // Revert toggle
        document.getElementById('sslToggle').checked = !enabled;
        return;
    }

    // Tell the user what happened, with extra detail when a cert was generated.
    if (enabled) {
        if (d.cert_action === 'generated') {
            window.toast.success(
                `HTTPS enabled. A new self-signed certificate was generated at ` +
                `${d.cert_path}.\n` +
                `You will need to re-trust it in your browser the next time you ` +
                `connect.\n` +
                `${d.message || 'Restart the service to apply.'}`,
                { duration: 10000 }
            );
        } else if (d.cert_action === 'preserved') {
            window.toast.success(
                `HTTPS enabled using existing certificate at ${d.cert_path}.\n` +
                `${d.message || 'Restart the service to apply.'}`
            );
        } else {
            window.toast.success(d.message || 'HTTPS enabled. Restart the service to apply.');
        }
    } else {
        window.toast.success(d.message || 'HTTPS disabled. Restart the service to apply.');
    }
}