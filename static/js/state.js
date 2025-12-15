/**
 * Shared Application State
 * Central store for all global state variables
 */

export const state = {
    socket: null,
    allLogs: [],
    devices: [],
    currentDeviceIeee: null,
    deviceCache: {},
    debugEnabled: false,
    verboseLogging: false,
    isRestarting: false,
    pairingInterval: null, // Track the timer ID
    tableSortInitialised: false // Track table sort initialisation
};
