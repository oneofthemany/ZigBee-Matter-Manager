/**
 * main.js
 * Application entry point
 * * Imports and initializes all sub-modules.
 * Exposes necessary functions to the global window object for HTML onclick events.
 */

import { state } from './state.js';
import { updateLastSeenTimes } from './utils.js';
import { initWS } from './websocket.js';
import { fetchAllDevices, filterByStatus } from './devices.js';
import { initGroups } from './groups.js';
import { initMQTTExplorer, handleMQTTMessage, startMQTTStats, stopMQTTStats } from './mqtt-explorer.js';
import { initEditor, getEditorInstance } from './editor.js';
import { initOtbr, loadOtbrStatus } from './otbr.js';
let editorInitialised = false;

import { initSettings } from './settings.js';
initSettings();

import { initMedia } from './media.js';
initMedia();

import { initAISettingsTab } from './ai-automations.js';
initAISettingsTab();

import { initNotifications } from './notifications.js';
initNotifications();

import { initAppAlerts } from './app-alerts.js';
initAppAlerts();

import { initUpgrade } from './upgrade.js';
initUpgrade();

import {
    loadTabs,
    filterByTab,
    openTabManager,
    createNewTab,
    deleteTab,
    manageTabDevices,
    toggleDeviceInTab
} from './tabs.js';

import {
    initAutomationsPage,
    loadAutomationsPage
} from './automations-page.js';

import {
    initZones,
    recalibrateZone,
    deleteZone,
    viewZoneDetails
} from './zones.js';

import { initHeating } from './heating.js';

import { initSystemTab } from './system-telemetry.js';
import { initTables } from './table-utils.js';

import {
    openDeviceModal,
    saveConfig,
    getDeviceStateHtml
} from './device-modal.js';
import { openAcModal } from './ac-modal.js';
import {
    filterLogs,
    clearLogs,
    toggleDebug,
    toggleVerboseLogging,
    viewDebugPackets,
    refreshDebugPackets,
    exportDebugPackets,
    clearDebugFilters,
    downloadDebugLog,
    showConsoleLogSettings
} from './logging.js';
import { initPacketFlow } from './packet-flow.js';
import { createSignalInspector } from './modal/signals.js';
import {
    loadConfigYaml,
    saveConfigYaml,
    restartSystem,
    loadSSLStatus,
    toggleSSL
} from './system.js';
import {
    sendCommand,
    adjustSetpoint,
    doAction,
    renamePrompt,
    togglePairing,
    permitJoinVia,
    checkPairingStatus,
    bindDevices,
    startTouchlinkScan,
    openBannedModal,
    handleUnbanClick,
    cleanupOrphans,
    matterCommission,
    checkMatterStatus
} from './actions.js';
import {
    initMesh,
    loadMeshTopology,
    dashboardMeshRefresh,
    dashboardMeshReset,
    dashboardMeshCenter,
    toggleMeshLabels
} from './mesh.js';

import { renderOTATab, handleOTAProgress } from './modal/ota.js';
import {
    loadInterviewStatusPending,
    updateInterviewBadge,
    applyAllBadges,
} from './interview-status-badge.js';
import {
    startRetryInterview,
    deleteAndRepair,
} from './modal/device-settings.js';

const log = zmmLog('main');

// ============================================================================
// EXPOSE FUNCTIONS GLOBALLY
// ============================================================================

// Device management
window.openDeviceModal = openDeviceModal;
window.openAcModal = openAcModal;
window.renamePrompt = renamePrompt;
window.fetchAllDevices = fetchAllDevices;
window.filterByStatus = filterByStatus;
window.getDeviceStateHtml = getDeviceStateHtml;
window.cleanupOrphans = cleanupOrphans;

// Interview status (badge + modal actions)
window.startRetryInterview = startRetryInterview;
window.deleteAndRepair = deleteAndRepair;
window.applyAllBadges = applyAllBadges;
window.updateInterviewBadge = updateInterviewBadge;

// Device actions
window.sendCommand = sendCommand;
window.adjustSetpoint = adjustSetpoint;
window.doAction = doAction;
window.saveConfig = saveConfig;

// Pairing
window.togglePairing = togglePairing;
window.permitJoinVia = permitJoinVia;
window.checkPairingStatus = checkPairingStatus;
window.startTouchlinkScan = startTouchlinkScan;
window.bindDevices = bindDevices;

// Matter
window.matterCommission = matterCommission;
window.checkMatterStatus = checkMatterStatus;

// Logging & Debug
window.filterLogs = filterLogs;
window.clearLogs = clearLogs;
window.toggleDebug = toggleDebug;
window.toggleVerboseLogging = toggleVerboseLogging;
window.viewDebugPackets = viewDebugPackets;
window.refreshDebugPackets = refreshDebugPackets;
window.exportDebugPackets = exportDebugPackets;
window.clearDebugFilters = clearDebugFilters;
window.downloadDebugLog = downloadDebugLog;
window.showConsoleLogSettings = showConsoleLogSettings;

// System & Config
window.loadConfigYaml = loadConfigYaml;
window.saveConfigYaml = saveConfigYaml;
window.restartSystem = restartSystem;
window.loadSSLStatus = loadSSLStatus;
window.toggleSSL = toggleSSL;

window.applyManualChannel = function() {
    const ch = parseInt(document.getElementById('manualChannelSelect').value);
    const sel = document.getElementById('cfg_channel');
    if (sel) sel.value = ch;
    document.getElementById('spectrumStatus').innerHTML =
        `<span class="text-primary">Channel ${ch} set in config. Click Save on the Configuration tab.</span>`;
};

// Mesh Topology
window.loadMeshTopology = loadMeshTopology;
window.dashboardMeshRefresh = dashboardMeshRefresh;
window.dashboardMeshReset = dashboardMeshReset;
window.dashboardMeshCenter = dashboardMeshCenter;
window.toggleMeshLabels = toggleMeshLabels;

window.state = state;

// Device Stuff
window.bindDevices = bindDevices;
window.openBannedModal = openBannedModal;
window.handleUnbanClick = handleUnbanClick;

// Automations Page
window.loadAutomationsPage = loadAutomationsPage;

// OTA
window.renderOTATab = renderOTATab;

window.otaCheckAll = async function() {
    const btn = document.getElementById('otaCheckAllBtn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Checking...';
    }
    try {
        const resp = await fetch('/api/ota/check-all');
        const data = await resp.json();
        const count = data.devices_with_updates || 0;
        if (count > 0) {
            let msg = `${count} device(s) have firmware updates available:\n\n`;
            for (const [ieee, info] of Object.entries(data.updates || {})) {
                const dev = state.deviceCache[ieee];
                const name = dev ? dev.friendly_name : ieee;
                msg += `• ${name}: ${info.current_version} → ${info.new_version}\n`;
            }
            msg += '\nOpen each device\'s OTA tab to install.';
            window.toast.info(msg, { duration: 10000 });
        } else {
            window.toast.success('All devices are up to date — no firmware updates available.');
        }
    } catch (e) {
        window.toast.error('OTA check failed: ' + e.message);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-microchip"></i> Check OTA';
        }
    }
};

// Zones Management
window.recalibrateZone = recalibrateZone;
window.deleteZone = deleteZone;
window.viewZoneDetails = viewZoneDetails;

// Tabs Management
window.loadTabs = loadTabs;
window.filterByTab = filterByTab;
window.openTabManager = openTabManager;
window.createNewTab = createNewTab;
window.deleteTab = deleteTab;
window.manageTabDevices = manageTabDevices;
window.toggleDeviceInTab = toggleDeviceInTab;

// ============================================================================
// APPLICATION INITIALISATION
// ============================================================================

/**
 * Tab accessibility enhancer — gives every Bootstrap tablist proper
 * role/aria wiring (tab, tabpanel, aria-controls, aria-labelledby).
 * Runs at init and again whenever a tab is shown, so dynamically
 * rendered tablists (mesh sub-tabs, zone modal) get covered too.
 */
function enhanceTabA11y() {
    document.querySelectorAll('.nav-tabs, .nav-pills').forEach(list => {
        if (!list.getAttribute('role')) list.setAttribute('role', 'tablist');
        list.querySelectorAll('[data-bs-toggle="tab"]').forEach(btn => {
            const target = btn.getAttribute('data-bs-target');
            if (!target || !target.startsWith('#')) return;
            btn.setAttribute('role', 'tab');
            if (!btn.id) btn.id = 'tab-' + target.slice(1);
            btn.setAttribute('aria-controls', target.slice(1));
            btn.setAttribute('aria-selected', btn.classList.contains('active') ? 'true' : 'false');
            btn.closest('li')?.setAttribute('role', 'presentation');
            const pane = document.querySelector(target);
            if (pane) {
                pane.setAttribute('role', 'tabpanel');
                pane.setAttribute('aria-labelledby', btn.id);
            }
        });
    });
}

document.addEventListener('DOMContentLoaded', () => {

    /**
     * Idempotent dashboard initialiser. Only fires once a valid principal is
     * available — gated by zmmAuth.onChange below. Safe to call multiple times;
     * the state._dashboardInited guard prevents re-running.
     *
     * On first run with no admin user yet, the setup wizard is shown by
     * setup-wizard.js and this function never runs (no principal). Once the
     * wizard's submitAccount() creates the admin and we refresh the auth
     * principal, onChange fires and the dashboard inits cleanly.
     */
    function initDashboard() {
        if (state._dashboardInited) return;
        state._dashboardInited = true;

        // WebSocket
        if (!state.socket) initWS();

        // ARIA roles for all tablists, refreshed as lazily-built ones appear
        enhanceTabA11y();
        document.addEventListener('shown.bs.tab', enhanceTabA11y);

        // Live "last seen" tick
        setInterval(updateLastSeenTimes, 1000);

        // Module inits
        initTables();          // delegated table sort/filter (once)
        initOtbr();
        loadTabs();
        initMesh();
        initGroups();
        initMQTTExplorer();
        initAutomationsPage();
        initZones();
        initHeating();
        initSystemTab();
        initPacketFlow();

        // Initial data fetches
        fetchAllDevices();
        loadInterviewStatusPending();
        if (typeof checkPairingStatus === 'function') checkPairingStatus();
        checkMatterStatus();

        // Settings tab listener
        const settingsTab = document.querySelector('button[data-bs-target="#settings"]');
        if (settingsTab) {
            settingsTab.addEventListener('click', () => {
                loadConfigYaml();
                loadSSLStatus();

                if (window.initMyAccount) window.initMyAccount();
                if (window.initAuthSettings) window.initAuthSettings();
                if (window.initPresenceSettings) window.initPresenceSettings();
                if (window.initRemoteAccess) window.initRemoteAccess();
            });
        }

        // Topology tab listener
        const topologyTab = document.querySelector('button[data-bs-target="#topology"]');
        if (topologyTab) {
            topologyTab.addEventListener('shown.bs.tab', () => {
                // Ensures the D3 force simulation or SVG scales correctly once
                // the container is actually visible.
                if (typeof loadMeshTopology === 'function') {
                    loadMeshTopology();
                }
            });
        }

        // Spectrum tab listener — auto-load historical interference on first show
        const spectrumTab = document.querySelector('[data-bs-target="#settingsSpectrum"]');
        if (spectrumTab) {
            spectrumTab.addEventListener('shown.bs.tab', () => {
                if (typeof window.loadSpectrumHistory === 'function') {
                    window.loadSpectrumHistory();
                }
            });
        }

        // Network tab relay — Bootstrap only fires shown.bs.tab on the button
        // that was activated, so the already-active nested sub-tab (Thread /
        // Spectrum / Security) never gets its event when the parent Network
        // tab is shown. Re-dispatch on the active sub-tab so its lazy-loader runs.
        const networkTab = document.querySelector('[data-bs-target="#settingsNetwork"]');
        if (networkTab) {
            networkTab.addEventListener('shown.bs.tab', () => {
                const active = document.querySelector('#networkSubNav .nav-link.active');
                if (active) active.dispatchEvent(new Event('shown.bs.tab'));
            });
        }

        // MQTT Explorer tab listener
        const mqttTabTrigger = document.querySelector('button[data-bs-target="#mqtt-explorer"], a[data-bs-target="#mqtt-explorer"]');
        if (mqttTabTrigger) {
            mqttTabTrigger.addEventListener('shown.bs.tab', () => {
                startMQTTStats();
            });
            mqttTabTrigger.addEventListener('hidden.bs.tab', () => {
                stopMQTTStats();
            });
        }

        // ── Debug tab: sub-tabs (Logs / Signal Inspector / Packets) ──
        // Parent-tab relay: Bootstrap only fires shown.bs.tab on the button
        // that was activated, so the already-active sub-tab never gets its
        // event when the parent Debug tab is re-shown. Re-dispatch on it.
        const debugTab = document.querySelector('[data-bs-target="#debug"]');
        if (debugTab) {
            debugTab.addEventListener('shown.bs.tab', () => {
                const active = document.querySelector('#debugSubNav .nav-link.active');
                if (active) active.dispatchEvent(new Event('shown.bs.tab'));
            });
        }

        // Signal Inspector sub-tab — mount on show (device picker), destroy on hide.
        const debugSignalsTab = document.querySelector('[data-bs-target="#debugSignals"]');
        if (debugSignalsTab) {
            debugSignalsTab.addEventListener('shown.bs.tab', () => {
                const mount = document.getElementById('debug-signals-mount');
                if (!mount) return;
                if (window._debugInspector) window._debugInspector.destroy();
                window._debugInspector = createSignalInspector(mount, { ieee: null, showPicker: true });
            });
            debugSignalsTab.addEventListener('hidden.bs.tab', () => {
                if (window._debugInspector) { window._debugInspector.destroy(); window._debugInspector = null; }
            });
        }

        // Packets sub-tab — populate + refresh the (now inline) packet view.
        const debugPacketsTab = document.querySelector('[data-bs-target="#debugPackets"]');
        if (debugPacketsTab) {
            debugPacketsTab.addEventListener('shown.bs.tab', () => {
                try { viewDebugPackets(); } catch (e) { /* ignore */ }
            });
        }

        // Editor tab listener
        document.querySelector('[data-bs-target="#editor-tab"]')?.addEventListener('shown.bs.tab', () => {
            if (!editorInitialised) {
                initEditor();
                editorInitialised = true;
            } else {
                const inst = getEditorInstance();
                if (inst) inst.layout();
            }
        });

        // Test-recovery banner lives in test-banner.js — a standalone classic
        // script — so it still renders when a test deploy breaks this module
        // graph. Do not re-add it here.

        log.log("Zigbee Matter Manager Frontend Initialised");
    }

    // Gate the dashboard on authentication. zmmAuth.onChange fires
    // immediately if state.ready is already true (re-attaches), and again
    // every time the principal changes (login / logout / refresh).
    if (window.zmmAuth) {
        window.zmmAuth.onChange(function (principal) {
            if (principal) {
                initDashboard();
            }
        });
    } else {
        // auth.js failed to load — fall back to old unconditional behaviour
        initDashboard();
    }
});

// Initial fetch of devices needing interview attention
//loadInterviewStatusPending();