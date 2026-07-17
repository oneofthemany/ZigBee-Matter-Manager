/**
 * Frames page bootstrap — the standalone mobile front end (/frames).
 *
 * This page deliberately does NOT reuse the dashboard's plumbing:
 *
 *   websocket.js imports 13 modules (devices.js, logging.js, editor.js,
 *   mqtt-explorer.js, the modal set...) and actions.js imports device-modal.js,
 *   which drags in control.js (79KB) and automation.js (88KB). Importing either
 *   would pull the entire admin dashboard onto a phone and defeat the point of
 *   a separate surface.
 *
 * So this file provides the three things frames.js actually needs, and nothing
 * else:
 *
 *   1. state.deviceCache / state.devices — from one /api/devices fetch
 *   2. live updates — a minimal websocket that only listens for device_updated
 *   3. window.sendCommand — frames.js depends on that CONTRACT, not on actions.js
 *
 * Total cost: this file, frames.js, chambers-free. The dashboard is untouched.
 */

import { state } from './state.js';
import { initFrames, loadFrame, loadSavedFrames, framesHandleDeviceUpdate } from './frames.js';

const log = zmmLog('frames-page');

let socket = null;
let reconnectDelay = 1000;

// ── devices ─────────────────────────────────────────────────────────

async function loadDevices() {
    const res = await fetch('/api/devices');
    const devices = await res.json();
    if (!Array.isArray(devices)) throw new Error('unexpected /api/devices response');
    state.devices = devices;
    state.deviceCache = Object.fromEntries(devices.map(d => [d.ieee, d]));
    return devices;
}

// ── commands ────────────────────────────────────────────────────────

/**
 * Minimal sendCommand, matching actions.js's contract:
 *   sendCommand(ieee, command, value?, endpoint?)
 *
 * Applies the same optimistic toggle flip actions.js does, so a tap feels
 * instant instead of waiting for the websocket echo.
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

        const dev = state.deviceCache[ieee];
        if (dev) {
            dev.state = dev.state || {};
            const ep = endpoint || 1;
            if (command === 'toggle') {
                const key = dev.state[`on_${ep}`] !== undefined ? `on_${ep}` : 'on';
                const now = dev.state[key] === true || dev.state[key] === 'ON' || dev.state[key] === 1;
                dev.state[key] = !now;
            } else if (command === 'on' || command === 'off') {
                const key = dev.state[`on_${ep}`] !== undefined ? `on_${ep}` : 'on';
                dev.state[key] = command === 'on';
            } else if (command === 'brightness') {
                dev.state[dev.state[`brightness_${ep}`] !== undefined ? `brightness_${ep}` : 'brightness'] = Number(value);
            } else if (command === 'position') {
                dev.state.position = Number(value);
            } else if (command === 'temperature') {
                dev.state.occupied_heating_setpoint = Number(value);
            }
            framesHandleDeviceUpdate(ieee);
        }
        return data;
    } catch (e) {
        window.toast.error('Command failed: ' + e.message);
        // Re-render from unchanged state so the control snaps back rather than
        // showing a change that never happened.
        framesHandleDeviceUpdate(ieee);
        return { success: false, error: e.message };
    }
};

// ── live updates ────────────────────────────────────────────────────

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

// ── init ────────────────────────────────────────────────────────────

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
