/**
 * main.js
 * Application entry point
 * * Imports and initializes all sub-modules.
 * Exposes necessary functions to the global window object for HTML onclick events.
 */

import { state } from './state.js';
import { updateLastSeenTimes } from './utils.js';
import { initWS } from './websocket.js';
import { fetchAllDevices } from './devices.js';
import { initGroups } from './groups.js';

import {
    openDeviceModal,
    saveConfig,
    getDeviceStateHtml
} from './device-modal.js';
import {
    filterLogs,
    clearLogs,
    toggleDebug,
    toggleVerboseLogging,
    viewDebugPackets,
    refreshDebugPackets,
    clearDebugFilters,
    downloadDebugLog
} from './logging.js';
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
    startTouchlinkScan
} from './actions.js';
import {
    initMesh,
    loadMeshTopology,
    dashboardMeshRefresh,
    dashboardMeshReset,
    dashboardMeshCenter,
    toggleMeshLabels
} from './mesh.js';

// ============================================================================
// EXPOSE FUNCTIONS GLOBALLY
// ============================================================================

// Device management
window.openDeviceModal = openDeviceModal;
window.renamePrompt = renamePrompt;
window.fetchAllDevices = fetchAllDevices;
window.getDeviceStateHtml = getDeviceStateHtml;

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

// Logging & Debug
window.filterLogs = filterLogs;
window.clearLogs = clearLogs;
window.toggleDebug = toggleDebug;
window.toggleVerboseLogging = toggleVerboseLogging;
window.viewDebugPackets = viewDebugPackets;
window.refreshDebugPackets = refreshDebugPackets;
window.clearDebugFilters = clearDebugFilters;
window.downloadDebugLog = downloadDebugLog;

// System & Config
window.loadConfigYaml = loadConfigYaml;
window.saveConfigYaml = saveConfigYaml;
window.restartSystem = restartSystem;

// Mesh Topology
window.loadMeshTopology = loadMeshTopology;
window.dashboardMeshRefresh = dashboardMeshRefresh;
window.dashboardMeshReset = dashboardMeshReset;
window.dashboardMeshCenter = dashboardMeshCenter;
window.toggleMeshLabels = toggleMeshLabels;

window.state = state;

window.bindDevices = bindDevices;


// ============================================================================
// APPLICATION INITIALIZATION
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
    // Initialize WebSocket connection
    initWS();

    // Start update interval for "last seen" times
    setInterval(updateLastSeenTimes, 1000);

    // Initialize Mesh Tab listener
    initMesh();


    // Initialize Groups
    initGroups();

    // Initial fetch
    fetchAllDevices();

    // Check if pairing is currently active (Persistence)
    if(typeof checkPairingStatus === 'function') checkPairingStatus();

    // Initialize Settings Tab listener
    const settingsTab = document.querySelector('button[data-bs-target="#settings"]');
    if(settingsTab) {
        settingsTab.addEventListener('click', loadConfigYaml);
    }

    console.log("Zigbee Gateway Frontend Initialized");
});