/**
 * Frames page bootstrap — the standalone mobile front end (/frames).
 *
 * Deliberately does NOT reuse the dashboard's plumbing: importing websocket.js
 * or actions.js would pull the entire admin dashboard onto a phone. Provides
 * only what frames.js needs — the device cache, a minimal device_updated
 * websocket, and window.sendCommand. See docs/structure.md.
 */

import { state } from './state.js';
import {
    initFrames, loadFrame, loadSavedFrames, framesHandleDeviceUpdate, setFrameTab,
    openFrameBuilder, saveFrame, deleteCurrentFrame, frameCommand, frameSetpoint,
    frameColor, frameSliderInput, frameGroupCommand, frameGroupColor,
    frameSetHidden, frameSetGroupMembersHidden, frameToggleVisibilityEdit,
    frameToggleChamber, frameToggleKind, frameToggleDevice, frameMoveDevice,
    frameAddTab, frameRenameTab, frameRemoveTab, frameMoveTab, frameToggleTabGroup,
} from './frames.js';

// frames.js renders inline onclick= handlers, so its callbacks must be globals
// here exactly as main.js does for the dashboard.
Object.assign(window, {
    setFrameTab, openFrameBuilder, saveFrame, deleteCurrentFrame,
    frameCommand, frameSetpoint, frameColor, frameSliderInput,
    frameGroupCommand, frameGroupColor,
    frameSetHidden, frameSetGroupMembersHidden, frameToggleVisibilityEdit,
    frameToggleChamber, frameToggleKind, frameToggleDevice, frameMoveDevice,
    frameAddTab, frameRenameTab, frameRemoveTab, frameMoveTab, frameToggleTabGroup,
});

const log = zmmLog('frames-page');

let socket = null;
let reconnectDelay = 1000;

// devices

async function loadDevices() {
    const res = await fetch('/api/devices');
    const devices = await res.json();
    if (!Array.isArray(devices)) throw new Error('unexpected /api/devices response');
    state.devices = devices;
    state.deviceCache = Object.fromEntries(devices.map(d => [d.ieee, d]));
    return devices;
}

// commands

/**
 * Minimal sendCommand, matching actions.js's contract:
 *   sendCommand(ieee, command, value?, endpoint?)
 *
 * Transport only. frames.js applies the optimistic update before the call and
 * rolls it back on the failure this reports, so a tap shows its result now
 * rather than after the radio round-trip.
 */
window.sendCommand = async function sendCommand(ieee, command, value = null, endpoint = null) {
    const body = { ieee, command, value };
    if (endpoint) body.endpoint = endpoint;

    try {
        const res = await fetch('/api/device/command', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!data.success) throw new Error(data.error || 'command failed');
        return data;
    } catch (e) {
        window.toast.error('Command failed: ' + e.message);
        return { success: false, error: e.message };
    }
};

// live updates

function setConnected(ok) {
    const el = document.getElementById('framesConnection');
    if (!el) return;
    el.className = ok ? 'frames-conn is-up' : 'frames-conn is-down';
    el.title = ok ? 'Live' : 'Reconnecting…';
}

/**
 * A websocket that only cares about device state.
 *
 * The server sends { type, payload } — see routes/websocket_routes.py. Every
 * message type other than device_updated is ignored here on purpose; this page
 * has no logs, no packet flow, no pairing UI.
 */
function initWS() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${protocol}//${window.location.host}/ws`);

    socket.onopen = () => {
        reconnectDelay = 1000;
        setConnected(true);
    };

    socket.onmessage = ev => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }

        if (msg.type === 'device_updated' && msg.payload?.ieee) {
            const { ieee, data } = msg.payload;
            const dev = state.deviceCache[ieee];
            if (!dev) return;
            dev.state = { ...dev.state, ...data };
            if (data.available !== undefined) dev.available = data.available;
            if (data.last_seen) dev.last_seen_ts = data.last_seen;
            framesHandleDeviceUpdate(ieee);
        } else if (msg.type === 'device_list' && Array.isArray(msg.data)) {
            state.devices = msg.data;
            state.deviceCache = Object.fromEntries(msg.data.map(d => [d.ieee, d]));
            loadFrame();
        }
    };

    socket.onclose = () => {
        setConnected(false);
        // Back off to 30s: a phone in a pocket shouldn't hammer the hub.
        setTimeout(initWS, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 30000);
    };

    socket.onerror = () => setConnected(false);
}


async function start() {
    try {
        await loadDevices();
    } catch (e) {
        log.error('failed to load devices:', e.message);
        const grid = document.getElementById('framesGrid');
        if (grid) {
            grid.innerHTML = `<div class="frame-empty">
                <i class="fas fa-triangle-exclamation fa-2x mb-2"></i>
                <div>Could not reach the hive.</div>
            </div>`;
        }
        return;
    }

    initFrames();
    await loadSavedFrames();
    await loadFrame();
    initWS();
    // Header presence badge — same gating as the manager (main.js).
    if (window.initPresenceBadge) window.initPresenceBadge();
}

document.addEventListener('DOMContentLoaded', () => {
    // Same auth gate as the dashboard: zmmAuth.onChange fires immediately if a
    // principal already exists, and again after login.
    if (window.zmmAuth) {
        let started = false;
        window.zmmAuth.onChange(principal => {
            if (principal && !started) {
                started = true;
                start();
            }
        });
    } else {
        start();
    }
});
