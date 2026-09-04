/**
 * Device List Management
 * Handles device table rendering, updates, and state management
 */

import { state } from './state.js';
import { getTypeIcon, getLqiBadge, timeAgo } from './utils.js';
import { refreshModalState } from './device-modal.js';
import { openDeviceModal } from './device-modal.js';
import { reapplySort } from './table-utils.js';
import { dismissKnownDevices } from './join-progress.js';
import { confirmDialog } from './dialogs.js';


const log = zmmLog('devices');

/**
 * Adopt a fresh /api/devices payload into the cache.
 *
 * Every device, not just the ones a filter lets through: the cache is the state
 * source for Frames, the modal and the notification rules, none of which care
 * what the table is currently filtered to. Existing state is merged under the
 * new payload so transient keys the API doesn't carry survive a refresh.
 */
export function cacheDevices(devices) {
    for (const d of devices) {
        const known = state.deviceCache[d.ieee];
        if (known) d.state = { ...known.state, ...d.state };
        state.deviceCache[d.ieee] = d;
    }
}

/**
 * Check if a device has OTA cluster (0x0019) support
 */
function hasOTACluster(d) {
    if (!d.capabilities || !Array.isArray(d.capabilities)) return false;
    return d.capabilities.some(ep =>
        (ep.inputs || []).some(c => c.id === 0x0019) ||
        (ep.outputs || []).some(c => c.id === 0x0019)
    );
}

// In-flight /api/devices request, so overlapping callers share one fetch and
// one render. On a cold load main.js's init and the websocket's onopen both
// call this within a few hundred ms of each other, which rebuilt the whole
// tbody twice — the second rebuild being a visible flicker for no new data.
let _devicesInFlight = null;
// Fingerprint of the last payload we rendered, to skip a no-op re-render.
let _lastDevicesKey = null;

/**
 * Fetch all devices from API and render table.
 *
 * Concurrent calls are coalesced into the single in-flight request; a payload
 * identical to the one already on screen re-renders nothing.
 *
 * @param {boolean} [force=false] re-render even if the payload is unchanged
 *   (used after a filter/sort change that needs the rows rebuilt).
 */
export async function fetchAllDevices(force = false) {
    if (_devicesInFlight) return _devicesInFlight;

    _devicesInFlight = (async () => {
        try {
            log.log("Fetching all devices...");
            const res = await fetch('/api/devices');
            if (!res.ok) throw new Error(`API Error: ${res.status}`);
            const devices = await res.json();

            log.log(`Received ${devices.length} devices.`);
            state.devices = devices; // Update state
            // The cache used to be filled while rendering rows, which meant a
            // filtered table left everything else out of it — and Frames, which
            // reads state from the cache, showed those devices with no state at
            // all. Seed it from the payload instead, before any filter applies.
            cacheDevices(devices);

            const key = JSON.stringify(devices);
            if (force || key !== _lastDevicesKey) {
                _lastDevicesKey = key;
                renderDeviceTable();
                populateRouterList();
            } else {
                log.debug('Device payload unchanged — skipping re-render.');
            }

            try { dismissKnownDevices(state.devices); } catch(e) {}

        } catch (e) {
            log.error("Failed to fetch devices:", e);
            _lastDevicesKey = null;         // force a real render once it recovers
            const tbody = document.getElementById('deviceTableBody');
            if (tbody) tbody.innerHTML = `<tr><td colspan="10" class="text-center text-danger">Error loading devices: ${e.message}</td></tr>`;
        } finally {
            _devicesInFlight = null;
        }
    })();

    return _devicesInFlight;
}

export function renderDeviceTable() {
    const tbody = document.getElementById('deviceTableBody');
    const coordContainer = document.getElementById('coordinator-info');

    if (!tbody) return;

    tbody.innerHTML = '';
    tbody.removeAttribute('aria-busy');            // skeleton rows are gone now
    coordContainer?.removeAttribute('aria-busy');

    // Find Coordinator
    const coordinator = state.devices.find(d => d.type === 'Coordinator');
    let otherDevices = state.devices.filter(d => d.type !== 'Coordinator');

    // Apply tab filter if set
    if (state.deviceFilter) {
        otherDevices = otherDevices.filter(state.deviceFilter);
    }

    // Apply online/offline status filter
    if (state.statusFilter === 'online') {
        otherDevices = otherDevices.filter(d => d.available !== false);
    } else if (state.statusFilter === 'offline') {
        otherDevices = otherDevices.filter(d => d.available === false);
    }

    // Update device count badge
    const countBadge = document.getElementById('deviceCount');
    if (countBadge) {
        countBadge.textContent = otherDevices.length;
    }

    // 1. Render Coordinator Card
    if (coordinator && coordContainer) {
        coordContainer.innerHTML = `
            <div class="col-md-1 text-center">
                <i class="fas fa-broadcast-tower fa-2x text-primary"></i>
            </div>
            <div class="col-md-3">
                <h6 class="mb-0">Coordinator</h6>
                <small class="text-muted font-monospace">${coordinator.ieee}</small>
            </div>
            <div class="col-md-3">
                <span class="badge bg-light text-dark border">
                    <i class="fas fa-microchip"></i> ${coordinator.model || 'Unknown'}
                </span>
            </div>
            <div class="col-md-3">
                <span class="badge bg-light text-dark border">
                    <i class="fas fa-industry"></i> ${coordinator.manufacturer || 'Unknown'}
                </span>
            </div>
            <div class="col-md-2 text-end">
                <span class="badge bg-success">Online</span>
            </div>
            <div class="col-auto d-md-none coord-chevron">
                <i class="fas fa-chevron-down"></i>
            </div>
        `;
    } else if (coordContainer) {
        coordContainer.innerHTML = `<div class="col-12 text-center text-muted small">Coordinator not found</div>`;
    }

    // 2. Render Other Devices
    if (otherDevices.length === 0) {
        const filtered = state.deviceFilter || (state.statusFilter && state.statusFilter !== 'all');
        tbody.innerHTML = `<tr><td colspan="10" class="text-center text-muted">${
            filtered ? 'No devices match the current filter.' : 'No devices paired.'
        }</td></tr>`;
        return;
    }

    otherDevices.forEach(d => {
        if (!d.last_seen_ts) d.last_seen_ts = Date.now();

        const tr = document.createElement('tr');
        tr.dataset.ieee = d.ieee;

        // Quirk Badge Logic
        let quirkHtml = '';
        if (d.quirk && d.quirk !== 'None' && d.quirk !== 'NoneType') {
            const quirkName = d.quirk.split('.').pop();
            quirkHtml = `<span class="badge bg-info text-dark" style="font-size:0.65rem" title="${d.quirk}">${quirkName}</span>`;
        }

        // OTA Badge
        let otaHtml = '';
        if (hasOTACluster(d)) {
            otaHtml = `<span class="badge bg-warning text-dark" style="font-size:0.65rem" title="Firmware updatable (OTA cluster 0x0019)"><i class="fas fa-microchip"></i> OTA</span>`;
        }

        // Status Badge Logic
        let statusHtml = d.available !== false
            ? '<span class="badge bg-success me-1">Online</span>'
            : '<span class="badge bg-secondary me-1">Offline</span>';

        // Protocol badge
        if (d.protocol === 'matter') {
            statusHtml += '<span class="badge bg-info me-1">Matter</span>';
        }

        const isWifi = d.protocol === 'wifi';
        // AC units report power/mode/temp — show it where LQI would be
        const wifiStateHtml = isWifi && d.available !== false && d.state
            ? `<small class="text-muted">${d.state.power ? '⏻ ' + (d.state.mode || 'on') : 'off'}${
                  d.state.current_c != null ? ` · ${Number(d.state.current_c).toFixed(1)}°C` : ''}</small>`
            : '<small class="text-muted">—</small>';

        tr.innerHTML = `
            <td class="text-center align-middle" style="font-size: 1.2rem;" data-sort-value="${d.type || ''}">${getTypeIcon(d.type)}</td>
            <td class="align-middle">
                <div class="fw-bold text-primary" style="cursor:pointer" role="button" tabindex="0"
                     aria-label="Rename ${d.friendly_name}"
                     onclick="window.renamePrompt('${d.ieee}', '${d.friendly_name}')"
                     onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}">
                    ${d.friendly_name} <i class="fas fa-pen fa-xs text-muted ms-1" aria-hidden="true"></i>
                </div>
            </td>
            <td class="align-middle">
                <div class="font-monospace small text-muted">${
                    d.protocol === 'matter' || isWifi
                        ? (d.ip_addresses?.length ? d.ip_addresses[0] : (isWifi ? d.ieee : `Node ${d.state?.node_id || '?'}`))
                        : d.ieee
                }</div>
            </td>
            <td class="align-middle small" data-sort-value="${d.manufacturer || ''}">
                <div>${d.manufacturer || '?'}</div>
                ${quirkHtml} ${otaHtml}
            </td>
            <td class="align-middle small">
                <div>${d.model || '?'}</div>
            </td>
            <td class="device-lqi align-middle" data-sort-value="${d.lqi !== undefined ? d.lqi : ''}">${isWifi ? wifiStateHtml : getLqiBadge(d.lqi) + (d.rssi != null ? `<small class="text-muted d-block">${d.rssi} dBm</small>` : '')}</td>
            <td class="last-seen align-middle" data-ts="${d.last_seen_ts}" data-sort-value="${d.last_seen_ts}">${timeAgo(d.last_seen_ts)}</td>
             <td class="align-middle device-status-badges" data-sort-value="${d.available !== false ? 1 : 0}">
                 ${statusHtml}
             </td>
             <td class="align-middle">
                 <span class="badge ${d.protocol === 'matter' || isWifi ? 'bg-info' : 'bg-primary'}">${d.protocol === 'matter' ? 'Matter' : isWifi ? 'WiFi' : 'Zigbee'}</span>
             </td>
             <td class="align-middle text-end">
                <div class="btn-group btn-group-sm">
                    <button class="btn btn-outline-primary manage-btn" title="Details & Control">
                        <i class="fas fa-sliders-h"></i> Manage
                    </button>
                </div>
            </td>
        `;

        // Attach event listener for Manage button correctly
        const manageBtn = tr.querySelector('.manage-btn');
        if (manageBtn) {
            manageBtn.addEventListener('click', () => openDeviceModal(d)); // <--- Pass the object 'd'
        }
        tbody.appendChild(tr);
    });

    // Re-apply interview status badges after a full re-render
    try {
        if (typeof window.applyAllBadges === 'function') {
            window.applyAllBadges();
        }
    } catch (e) {
        // Non-fatal
    }

    // Restore the user's chosen column sort now the tbody is rebuilt
    reapplySort(tbody.closest('table'));

    // Swap the LQI/status cells to their final form in this same frame.
    // device-status.js also does this from a MutationObserver, but that only
    // helps once its observer is attached — on a cold load the table can be
    // rendered first, and then the rows visibly shrink when the enhancer
    // finally runs. The data-enhanced guard makes the duplicate call cheap.
    if (window._enhanceDeviceTable) {
        try { window._enhanceDeviceTable(); } catch (e) { log.debug('enhance skipped:', e); }
    }

    rememberColumnWidths(tbody.closest('table'));
}

/** localStorage key holding the last rendered device-table column widths. */
const COL_WIDTH_KEY = 'zbm-device-col-widths';

/**
 * Record the settled column widths so the next page load can seed the skeleton
 * with them (see the restore script in index.html).
 *
 * The table is table-layout:auto, so widths are derived from content — the
 * skeleton can only guess, and the guess being wrong is what makes every
 * column jump sideways when the real rows land. Widths from the previous
 * render of the same devices are a far better guess than any hardcoded one.
 */
function rememberColumnWidths(table) {
    if (!table || !table.tHead) return;
    try {
        const widths = [...table.tHead.rows[0].cells].map(c => Math.round(c.getBoundingClientRect().width));
        // A hidden tab measures as zero — never persist that.
        if (widths.some(w => w <= 0)) return;
        localStorage.setItem(COL_WIDTH_KEY, JSON.stringify(widths));
    } catch (e) { /* private mode / quota — the skeleton just guesses */ }
}

/**
 * Handle the online/offline status filter dropdown
 */
export function filterByStatus() {
    const select = document.getElementById('statusFilter');
    state.statusFilter = select ? select.value : 'all';
    renderDeviceTable();
}

/**
 * update states on device row as opposed to whole table render
 */

function updateDeviceRow(device) {
    const row = document.querySelector(`tr[data-ieee="${device.ieee}"]`);
    if (!row) {
        renderDeviceTable();
        return;
    }

    // Update last seen
    const lastSeenCell = row.querySelector('.last-seen');
    if (lastSeenCell && device.last_seen_ts) {
        lastSeenCell.dataset.ts = device.last_seen_ts;
        lastSeenCell.dataset.sortValue = device.last_seen_ts;
        lastSeenCell.innerText = timeAgo(device.last_seen_ts);
    }

    // Update signal (LQI badge + RSSI sub-text)
    const lqiCell = row.querySelector('.device-lqi');
    if (lqiCell && device.lqi !== undefined) {
        lqiCell.dataset.sortValue = device.lqi;
        lqiCell.innerHTML = getLqiBadge(device.lqi)
            + (device.rssi != null ? `<small class="text-muted d-block">${device.rssi} dBm</small>` : '');
    }

    // Mark for re-enhancement
    row.dataset.enhanced = 'false';

    // Directly trigger device-status.js enhancement
    if (window._enhanceDeviceTable) {
        window._enhanceDeviceTable();
    }
}


/**
 * Handle incoming WebSocket device update events
 */
export function handleDeviceUpdate(payload) {
    // DEBUG LOGGING: Log the payload as JSON
    //log.log("1. WebSocket Update Received:", payload.ieee, "\nPayload:", JSON.stringify(payload, null, 2));

    // 1. Find the device in the array
    const devIndex = state.devices.findIndex(d => d.ieee === payload.ieee);

    if (devIndex !== -1) {
        // 2. Update the device state in memory
        // We merge the new data into the existing state object to preserve existing keys
        state.devices[devIndex].state = { ...state.devices[devIndex].state, ...payload.data };
        // DEBUG LOGGING:
        //log.log("2. Current Open Device:", state.currentDeviceIeee);

        // Update metadata if present
        if (payload.data.last_seen) state.devices[devIndex].last_seen_ts = payload.data.last_seen;
        if (payload.data.available !== undefined) state.devices[devIndex].available = payload.data.available;
        if (payload.data.lqi !== undefined) state.devices[devIndex].lqi = payload.data.lqi;
        if (payload.data.rssi !== undefined) state.devices[devIndex].rssi = payload.data.rssi;

        // Update the cache as well
        state.deviceCache[payload.ieee] = state.devices[devIndex];

        try { dismissKnownDevices(state.devices); } catch(e) {}

        // Frames renders live values straight from the cache, so it only needs
        // to re-render the cells for this device.
        if (window.framesHandleDeviceUpdate) window.framesHandleDeviceUpdate(payload.ieee);

        // If availability changed while an online/offline filter is active,
        // the row may need to appear or disappear — re-render the whole table
        if (payload.data.available !== undefined && state.statusFilter !== 'all') {
            renderDeviceTable();
        } else {
            // Update only this device's row, not the entire table
            updateDeviceRow(state.devices[devIndex]);
        }

        // Update router list if device type changed or availability changed
        populateRouterList();
        // DEBUG LOGGING:
        //log.log("3. MATCH! Attempting to refresh modal...");

        // 4. Refresh the modal if it is open for THIS device
        //if (state.currentDeviceIeee === payload.ieee) {
        //    refreshModalState(state.devices[devIndex]); // Pass the updated object
        //}

        if (state.currentDeviceIeee === payload.ieee) {
            // DEBUG LOGGING:
            //log.log("3b. About to call refreshModalState, fn is:", typeof refreshModalState);
            try {
                refreshModalState(state.devices[devIndex]);
                log.log("3c. refreshModalState returned successfully");
            } catch (err) {
                log.error("3d. refreshModalState THREW:", err);
                log.error("    Stack:", err.stack);
                log.error("    Device:", state.devices[devIndex]);
            }
        }

    } else {
        // Device not found in list (maybe new join?), trigger full fetch
        log.log("Device not found in local list, fetching all...");
        fetchAllDevices();
    }
}

/**
 * Populate the "Pair via specific device" dropdown list
 * Targets <div id="routerList">
 */
function populateRouterList() {
    const listContainer = document.getElementById('routerList');
    if (!listContainer) return;

    // This runs on every device websocket update, which on a busy mesh is
    // several times a second. Rebuilding an open menu yanks the entries out
    // from under the pointer mid-click, so defer until it closes. Bootstrap
    // fires dropdown events on the toggle (a sibling of the menu), so the
    // listener goes on document and catches them as they bubble.
    const menu = listContainer.closest('.dropdown-menu');
    if (menu && menu.classList.contains('show')) {
        if (!listContainer._pendingRepopulate) {
            listContainer._pendingRepopulate = true;
            document.addEventListener('hidden.bs.dropdown', function onHidden() {
                if (menu.classList.contains('show')) return;   // a different dropdown
                document.removeEventListener('hidden.bs.dropdown', onHidden);
                listContainer._pendingRepopulate = false;
                populateRouterList();
            });
        }
        return;
    }

    // Clear current list
    listContainer.innerHTML = '';

    if (!state.devices || state.devices.length === 0) {
        listContainer.innerHTML = '<span class="dropdown-item disabled">No devices available</span>';
        return;
    }

    // Filter for Routers and Coordinator
    // Case-insensitive check for role/type
    const routers = state.devices.filter(d => {
        const type = (d.type || '').toLowerCase();
        // Also check if it's the coordinator based on IEEE if type is missing
        return type.includes('router') || type.includes('coordinator');
    });

    if (routers.length === 0) {
        listContainer.innerHTML = '<span class="dropdown-item disabled">No routers found</span>';
        return;
    }

    // Sort routers by name
    routers.sort((a, b) => {
        const nameA = a.friendly_name || a.ieee;
        const nameB = b.friendly_name || b.ieee;
        return nameA.localeCompare(nameB);
    });

    // Create dropdown items
    routers.forEach(router => {
        const name = router.friendly_name || router.ieee;
        const model = router.model ? ` <small class="text-muted">(${router.model})</small>` : '';

        const item = document.createElement('a');
        item.className = 'dropdown-item d-flex justify-content-between align-items-center cursor-pointer';
        item.href = '#'; // Prevent default anchor behavior
        item.innerHTML = `<span>${name}${model}</span>`;

        // Add click handler
        item.onclick = (e) => {
            e.preventDefault();
            enablePermitJoinDevice(router.ieee, name);
        };

        listContainer.appendChild(item);
    });
}

/**
 * Enable permit join on specific device
 */
window.enablePermitJoinDevice = async function(ieee, name) {
    if (!await confirmDialog({
        title: 'Enable pairing',
        message: `Enable pairing on ${name} for 120 seconds?`,
        confirmText: 'Enable'
    })) return;

    try {
        const response = await fetch('/api/permit_join', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                duration: 120,
                target_ieee: ieee
            })
        });

        const result = await response.json();

        if (result.success) {
            window.toast.success(`Pairing enabled on ${name}. LED on device may flash.`);
        } else {
            window.toast.error(`Failed: ${result.error}`);
        }

    } catch (error) {
        log.error("Permit join error:", error);
        window.toast.error("Failed to send request");
    }
};

export function removeDeviceRow(ieee) {
    state.devices = state.devices.filter(d => d.ieee !== ieee);
    renderDeviceTable();
}