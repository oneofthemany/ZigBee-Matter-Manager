/**
 * main.js
 * Application entry point
 * * Imports and initializes all sub-modules.
 * Exposes necessary functions to the global window object for HTML onclick events.
 */

import { state } from './state.js';
import './manager-link.js';      // data-zmm-manager links
import { updateLastSeenTimes, whileVisible } from './utils.js';
import { initWS } from './websocket.js';
import { fetchAllDevices, filterByStatus, renderLastDevices, initDeviceSortPref } from './devices.js';
import { initTabRouting } from './tab-routing.js';
import { initMQTTExplorer, handleMQTTMessage, startMQTTStats, stopMQTTStats } from './mqtt-explorer.js';
import { lazyTab } from './tab-loader.js';
let editor = null;   // editor.js, once its tab has been opened
let editorInitialised = false;

// App-wide, so they start with the page rather than with a tab.
import { initNotifications } from './notifications.js';
initNotifications();
import { initAppAlerts } from './app-alerts.js';
initAppAlerts();

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
    initChambers,
    loadChambers,
    openChamberManager,
    createChamber,
    renameChamber,
    deleteChamber,
    assignChamberDevices,
    saveChamberDevices,
    assignDeviceChamber,
    showChamberMerge,
    mergeChambers,
    chooseChamber
} from './chambers.js';

import { initTables } from './table-utils.js';

import {
    openDeviceModal,
    saveConfig,
    getDeviceStateHtml
} from './device-modal.js';
import { openAcModal } from './modal/ac-modal.js';
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
    restartSystem
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

// EXPOSE FUNCTIONS GLOBALLY

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

window.applyManualChannel = function() {
    const ch = parseInt(document.getElementById('manualChannelSelect').value);
    const sel = document.getElementById('cfg_channel');
    if (sel) sel.value = ch;
    document.getElementById('spectrumStatus').innerHTML =
        `<span class="text-primary">Channel ${ch} set in config. Click Save on the Configuration tab.</span>`;
};

window.state = state;

// Device Stuff
window.bindDevices = bindDevices;
window.openBannedModal = openBannedModal;
window.handleUnbanClick = handleUnbanClick;

// OTA
window.renderOTATab = renderOTATab;

window.otaCheckAll = async function() {
    const btn = document.getElementById('otaCheckAllBtn');
    const saved = btn?.innerHTML;
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin fa-fw me-2"></i>Checking...';
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
            btn.innerHTML = saved;
        }
    }
};

// LAZY TABS — each loads on its tab's first open (tab-loader.js). `globals` are
// the window functions this page's own markup calls, stubbed until then.

lazyTab('settings', async () => {
    const [settings, upgrade, alarm, otbr, speakers, ai] = await Promise.all([
        import('./settings.js'), import('./upgrade.js'), import('./alarm-settings.js'),
        import('./otbr.js'), import('./speaker-sync.js'), import('./ai-automations.js'),
    ]);
    settings.initSettings();
    upgrade.initUpgrade();
    alarm.initAlarmSettings();
    otbr.initOtbr();
    speakers.initSpeakerSyncTab();
    ai.initAISettingsTab();
}, { globals: ['saveSettingsConfig', 'saveSettingsConfigAndRestart', 'loadSpectrumHistory',
               'runSpectrumScan', 'autoSelectChannel'] });

lazyTab('media', async () => (await import('./media.js')).initMedia(),
    { globals: ['mediaSetSource', 'mediaSearch', 'mediaPane', 'mediaSetKaraoke', 'mediaRefresh'] });

lazyTab('heating', async () => (await import('./heating.js')).initHeating());
lazyTab('energy', async () => (await import('./energy.js')).initEnergy());
lazyTab('cameras', async () => (await import('./cameras-page.js')).initCamerasPage());
lazyTab('system', async () => (await import('./system-telemetry.js')).initSystemTab());
// drive.js exposes window.initDriveTab, which the drive listener below calls.
lazyTab('drive', () => import('./drive.js'));
// The editor listener below starts it, on the replayed first show.
lazyTab('editor-tab', async () => { editor = await import('./editor.js'); });

// initGroups attaches its tab listener after its first fetch, so it is awaited.
lazyTab('groups', async () => (await import('./groups.js')).initGroups(),
    { globals: ['resetGroupCreation', 'onBaseDeviceSelected', 'deleteCurrentGroup',
                'createGroup', 'addDeviceToGroup'] });

lazyTab('zones', async () => {
    const z = await import('./zones.js');
    Object.assign(window, {
        recalibrateZone: z.recalibrateZone, deleteZone: z.deleteZone, viewZoneDetails: z.viewZoneDetails,
    });
    z.initZones();
});

lazyTab('topology', async () => {
    const m = await import('./mesh.js');
    Object.assign(window, {
        loadMeshTopology: m.loadMeshTopology, dashboardMeshRefresh: m.dashboardMeshRefresh,
        dashboardMeshReset: m.dashboardMeshReset, dashboardMeshCenter: m.dashboardMeshCenter,
        toggleMeshLabels: m.toggleMeshLabels,
    });
    m.initMesh();
    // Reload on every show, so the force layout sizes against a visible container.
    document.querySelector('button[data-bs-target="#topology"]')
        ?.addEventListener('shown.bs.tab', () => m.loadMeshTopology());
});

lazyTab('automations', async () => {
    const [page, workers, suggestions] = await Promise.all([
        import('./automations-page.js'), import('./workers-page.js'), import('./swarm-suggestions.js'),
    ]);
    window.loadAutomationsPage = page.loadAutomationsPage;
    page.initAutomationsPage();
    workers.initWorkersPage();
    suggestions.initSuggestionsPage();
}, { globals: ['loadAutomationsPage'] });

// Frames cells call these from their own markup, so none is needed before load.
// framesHandleDeviceUpdate is deliberately absent until then: devices.js skips
// a missing hook rather than loading Frames on every device update.
lazyTab('frames', async () => {
    const f = await import('./frames.js');
    Object.assign(window, {
        loadFrame: f.loadFrame, setFramesSplit: f.setSplit, selectFrame: f.selectFrame,
        frameCommand: f.frameCommand, frameSetpoint: f.frameSetpoint, frameColor: f.frameColor,
        frameSliderInput: f.frameSliderInput, frameGroupCommand: f.frameGroupCommand,
        frameGroupColor: f.frameGroupColor, frameSetHidden: f.frameSetHidden,
        frameSetGroupMembersHidden: f.frameSetGroupMembersHidden,
        frameToggleVisibilityEdit: f.frameToggleVisibilityEdit, openFrameBuilder: f.openFrameBuilder,
        saveFrame: f.saveFrame, deleteCurrentFrame: f.deleteCurrentFrame,
        frameToggleChamber: f.frameToggleChamber, frameToggleKind: f.frameToggleKind,
        frameToggleDevice: f.frameToggleDevice, frameMoveDevice: f.frameMoveDevice,
        setFrameTab: f.setFrameTab, frameAddTab: f.frameAddTab, frameRenameTab: f.frameRenameTab,
        frameRemoveTab: f.frameRemoveTab, frameMoveTab: f.frameMoveTab,
        frameToggleTabGroup: f.frameToggleTabGroup,
        framesHandleDeviceUpdate: f.framesHandleDeviceUpdate,
    });
    f.initFrames();
});

// Chambers (Frames room registry)
window.openChamberManager = openChamberManager;
window.createChamber = createChamber;
window.renameChamber = renameChamber;
window.deleteChamber = deleteChamber;
window.assignChamberDevices = assignChamberDevices;
window.saveChamberDevices = saveChamberDevices;
window.assignDeviceChamber = assignDeviceChamber;
window.loadChambers = loadChambers;
window.showChamberMerge = showChamberMerge;
window.mergeChambers = mergeChambers;
window.zbmChooseChamber = chooseChamber;

// Tabs Management
window.loadTabs = loadTabs;
window.filterByTab = filterByTab;
window.openTabManager = openTabManager;
window.createNewTab = createNewTab;
window.deleteTab = deleteTab;
window.manageTabDevices = manageTabDevices;
window.toggleDeviceInTab = toggleDeviceInTab;

// APPLICATION INITIALISATION

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

        // First, so it heads the browser's six-connection queue: the module
        // inits below each fire their own requests.
        renderLastDevices();
        fetchAllDevices();
        initDeviceSortPref();

        // WebSocket
        if (!state.socket) initWS();

        // ARIA roles for all tablists, refreshed as lazily-built ones appear
        enhanceTabA11y();
        document.addEventListener('shown.bs.tab', enhanceTabA11y);

        // Live "last seen" tick
        setInterval(whileVisible(updateLastSeenTimes), 1000);

        // Module inits
        // Only what the dashboard or every tab needs; the rest load with their
        // tab (LAZY TABS above).
        initTables();          // delegated table sort/filter (once)
        loadTabs();
        initChambers();
        initMQTTExplorer();
        initPacketFlow();

        // Initial data fetches
        loadInterviewStatusPending();
        if (typeof checkPairingStatus === 'function') checkPairingStatus();
        checkMatterStatus();
        // Header presence badge — page-level, not tied to the Settings tab.
        if (window.initPresenceBadge) window.initPresenceBadge();
        if (window.initHouseBadge) window.initHouseBadge();
        if (window.initMessagesUI) window.initMessagesUI();
        // Fallback poll — the matter_status websocket event is the primary
        // signal; this catches missed events (ws reconnects, backend restarts)
        setInterval(whileVisible(checkMatterStatus), 60000);

        // Drive tab listener — journeys + fuel prices load on first open
        const driveTab = document.querySelector('button[data-bs-target="#drive"]');
        if (driveTab) {
            driveTab.addEventListener('shown.bs.tab', () => {
                if (window.initDriveTab) window.initDriveTab();
            });
        }

        // Settings tab listener
        const settingsTab = document.querySelector('button[data-bs-target="#settings"]');
        if (settingsTab) {
            settingsTab.addEventListener('click', () => {
                loadConfigYaml();

                if (window.initMyAccount) window.initMyAccount();
                if (window.initAuthSettings) window.initAuthSettings();
                if (window.initPresenceSettings) window.initPresenceSettings();
                // Apiary moved to the Drive tab; drive.js initialises it there.
                if (window.initRemoteAccess) window.initRemoteAccess();
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

        // Debug tab: sub-tabs (Logs / Signal Inspector / Packets)
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
        // The first show arrives before editor.js does; tab-loader replays it.
        document.querySelector('[data-bs-target="#editor-tab"]')?.addEventListener('shown.bs.tab', () => {
            if (!editor) return;
            if (!editorInitialised) {
                editor.initEditor();
                editorInitialised = true;
            } else {
                const inst = editor.getEditorInstance();
                if (inst) inst.layout();
            }
        });

        // Test-recovery banner lives in test-banner.js — a standalone classic
        // script — so it still renders when a test deploy breaks this module
        // graph. Do not re-add it here.

        initTabRouting({ onReturn: fetchAllDevices });

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