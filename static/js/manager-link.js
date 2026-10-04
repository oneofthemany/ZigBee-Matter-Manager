/**
 * Links to the ZMM Manager (the recovery sidecar on :8001).
 *
 * The manager is only ever on the hub's private network — it isn't published
 * through the tunnel. So on a public hostname a link must point at the hub's
 * LAN address (from /api/system/manager), never at this page's host; on a
 * private hostname this page's host already reaches it. The hub also reports
 * whether the manager is up, so "down" can be told apart from "wrong network".
 *
 * Any element with data-zmm-manager opens it; classic scripts can call
 * window.zbmOpenManager().
 */

import { confirmDialog } from './dialogs.js';

const DEFAULT_PORT = 8001;
let info = null;          // last /api/system/manager answer

// RFC 1918 / loopback / link-local / unique-local addresses and LAN-only names.
const PRIVATE_HOST = /^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|\[?::1\]?$|\[?f[cd][0-9a-f]{2}:|\[?fe80:)|\.(local|lan|home|internal|home\.arpa)$/i;

export function isPrivateHost(hostname) {
    // A single-label name ("rocky") only resolves on the local network.
    return PRIVATE_HOST.test(hostname) || !hostname.replace(/^\[|\]$/g, '').includes('.') && !hostname.includes(':');
}

/** The manager's address for a page at loc, or null when it can't be known. */
export function pickManagerUrl(loc, managerInfo) {
    const port = managerInfo?.port || DEFAULT_PORT;
    if (isPrivateHost(loc.hostname)) return `${loc.protocol}//${loc.hostname}:${port}/`;
    return managerInfo?.lan_url || null;
}

export function managerUrl() {
    return pickManagerUrl(location, info);
}

async function refreshInfo() {
    try {
        const r = await fetch('/api/system/manager', { cache: 'no-store' });
        if (r.ok) info = await r.json();
    } catch (e) { /* keep the last answer */ }
    // Links rendered before the answer arrived get the private address now.
    const url = managerUrl();
    if (url) document.querySelectorAll('a[data-zmm-manager]').forEach(a => { a.href = url; });
    return info;
}

export async function openManager() {
    await refreshInfo();
    const url = managerUrl();

    if (info && !info.up) {
        if (await confirmDialog({
            title: 'ZMM Manager isn\'t responding',
            message: `The hub checked and nothing is answering on port ${info.port}.`,
            detail: 'It normally restarts on its own within a minute. If it stays down, check the '
                + 'zigbee-matter-manager-manager service on the hub (for example in Cockpit).',
            confirmText: url ? 'Try anyway' : 'OK',
        }) && url) window.open(url, '_blank', 'noopener');
        return;
    }

    if (!url) {
        await confirmDialog({
            title: 'ZMM Manager is on your home network only',
            message: 'It isn\'t published through the public address, and the hub couldn\'t report its LAN address.',
            detail: `Open it from a device on your home network at https://<hub's LAN IP>:${info?.port || DEFAULT_PORT}/`,
            confirmText: 'OK',
        });
        return;
    }
    window.open(url, '_blank', 'noopener');
}

document.addEventListener('click', e => {
    const link = e.target.closest('[data-zmm-manager]');
    if (!link || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;   // middle/ctrl-click use the href
    e.preventDefault();
    openManager();
});

// Signed in is when /api/system/manager will answer.
window.zmmAuth?.onChange(p => { if (p) refreshInfo(); });
window.zbmOpenManager = openManager;
