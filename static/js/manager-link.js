/**
 * Links to the ZMM Manager (the recovery sidecar on :8001).
 *
 * The manager is reached at this page's host on its own port, which fails from
 * the tunnel, behind a proxy, or before its certificate is accepted — and a
 * timed-out tab reads as "the manager is down". So a click first asks the hub
 * whether the manager is up (/api/system/manager), then whether this browser
 * can reach it, and explains which of the two is the problem.
 *
 * Any element with data-zmm-manager opens it; classic scripts can call
 * window.zbmOpenManager().
 */

import { confirmDialog } from './dialogs.js';

const PROBE_TIMEOUT_MS = 2500;   // stays inside the browser's popup allowance after a click

export function managerUrlHere(port = 8001) {
    return `${location.protocol}//${location.hostname}:${port}/`;
}

/** True when a connection succeeds; no-cors, since the manager sends no CORS headers. */
async function reachable(url) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), PROBE_TIMEOUT_MS);
    try {
        await fetch(url + 'healthz', { mode: 'no-cors', cache: 'no-store', signal: ctl.signal });
        return true;
    } catch (e) {
        return false;
    } finally {
        clearTimeout(timer);
    }
}

async function managerInfo() {
    try {
        const r = await fetch('/api/system/manager', { cache: 'no-store' });
        return r.ok ? await r.json() : null;
    } catch (e) {
        return null;
    }
}

export async function openManager() {
    const info = await managerInfo();
    const here = managerUrlHere(info?.port || 8001);

    if (info && !info.up) {
        if (await confirmDialog({
            title: 'ZMM Manager isn\'t responding',
            message: `The hub checked and nothing is answering on port ${info.port}.`,
            detail: 'It normally restarts on its own within a minute. If it stays down, check the '
                + 'zigbee-matter-manager-manager service on the hub (for example in Cockpit).',
            confirmText: 'Try anyway',
        })) window.open(here, '_blank', 'noopener');
        return;
    }

    if (await reachable(here)) {
        window.open(here, '_blank', 'noopener');
        return;
    }

    const lan = info?.lan_url && info.lan_url !== here ? info.lan_url : null;
    const running = info ? 'The manager is running on the hub, but this browser' : 'This browser';
    if (await confirmDialog({
        title: 'Can\'t reach the ZMM Manager from here',
        message: `${running} couldn't connect to ${here}`,
        detail: 'Usually one of: you\'re away from the home network (the manager isn\'t published through '
            + 'the tunnel); a proxy, VPN or proxy extension is sending this browser\'s traffic elsewhere; '
            + 'or the browser hasn\'t accepted the manager\'s certificate yet, which opening it once fixes.'
            + (lan ? ` On your home network it's at ${lan}` : ''),
        confirmText: lan ? 'Open LAN address' : 'Open anyway',
    })) window.open(lan || here, '_blank', 'noopener');
}

document.addEventListener('click', e => {
    const link = e.target.closest('[data-zmm-manager]');
    if (!link || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;   // let middle/ctrl-click open the raw link
    e.preventDefault();
    openManager();
});

window.zbmOpenManager = openManager;
