/**
 * zones.js
 * Frontend logic for Presence Detection Zones
 */

import { showToast } from './utils.js'; // Assuming you have a utils file, or remove if not

// ============================================================================
// STATE
// ============================================================================
let zonesData = new Map();
let deviceListCache = [];

// ============================================================================
// INITIALIZATION
// ============================================================================
export function initZones() {
    console.log("Initializing Zones Module...");

    // Initial Fetch
    fetchZones();

    // Bind Tab Click to Refresh
    const zonesTabBtn = document.querySelector('button[data-bs-target="#zones"]');
    if (zonesTabBtn) {
        zonesTabBtn.addEventListener('click', () => {
            fetchZones();
        });
    }

    // Bind Refresh Button
    document.getElementById('btn-refresh-zones')?.addEventListener('click', fetchZones);

    // Bind Create Zone Button
    document.getElementById('btn-create-zone')?.addEventListener('click', openCreateZoneModal);

    // Bind Save Zone Button
    document.getElementById('btn-save-zone')?.addEventListener('click', handleCreateZoneSubmit);

    // Search filter for create modal
    document.getElementById('zone-device-search')?.addEventListener('keyup', (e) => {
        filterDeviceList(e.target.value);
    });
}

// ============================================================================
// API CALLS
// ============================================================================
async function fetchZones() {
    try {
        const response = await fetch('/api/zones');
        if (!response.ok) throw new Error("Failed to fetch zones");
        const zones = await response.json();

        zonesData.clear();
        zones.forEach(zone => zonesData.set(zone.name, zone));

        renderZonesGrid();
    } catch (error) {
        console.error("Error fetching zones:", error);
        document.getElementById('zones-container').innerHTML =
            `<div class="col-12 text-center text-danger">Failed to load zones: ${error.message}</div>`;
    }
}

async function fetchDevicesForModal() {
    try {
        const response = await fetch('/api/devices');
        const devices = await response.json();
        // Filter for routers or devices capable of neighbors usually,
        // but for now allow all except basic battery sensors if desired.
        deviceListCache = devices;
        renderDeviceList(devices);
    } catch (error) {
        console.error("Error fetching devices:", error);
    }
}

// ============================================================================
// RENDERING
// ============================================================================
function renderZonesGrid() {
    const container = document.getElementById('zones-container');
    container.innerHTML = '';

    if (zonesData.size === 0) {
        container.innerHTML = `
            <div class="col-12 text-center text-muted py-5">
                <i class="bi bi-inbox fs-1"></i>
                <p class="mt-2">No zones created yet.</p>
            </div>`;
        return;
    }

    zonesData.forEach(zone => {
        const card = createZoneCard(zone);
        container.appendChild(card);
    });
}

function createZoneCard(zone) {
    const col = document.createElement('div');
    col.className = 'col-md-6 col-lg-4 mb-4';

    const stateColors = {
        'occupied': 'success',
        'vacant': 'secondary',
        'calibrating': 'warning'
    };
    const stateColor = stateColors[zone.state] || 'primary';
    const isOccupied = zone.state === 'occupied';

    col.innerHTML = `
        <div class="card h-100 shadow-sm border-${isOccupied ? 'success' : 'light'}">
            <div class="card-header bg-transparent d-flex justify-content-between align-items-center">
                <h5 class="mb-0 text-truncate" title="${zone.name}">${zone.name}</h5>
                <span class="badge bg-${stateColor} text-uppercase">${zone.state}</span>
            </div>
            <div class="card-body">
                <div class="d-flex justify-content-between mb-2">
                    <span class="text-muted">Devices:</span>
                    <strong>${zone.device_count}</strong>
                </div>
                <div class="d-flex justify-content-between mb-3">
                    <span class="text-muted">Links Tracked:</span>
                    <strong>${zone.link_count}</strong>
                </div>

                ${zone.occupied_since ?
                    `<div class="alert alert-success py-1 small mb-3">
                        <i class="bi bi-clock-history"></i> Since ${new Date(zone.occupied_since * 1000).toLocaleTimeString()}
                    </div>` : ''
                }
            </div>
            <div class="card-footer bg-transparent border-top-0 d-flex gap-2">
                <button class="btn btn-sm btn-outline-primary flex-grow-1" onclick="window.viewZoneDetails('${zone.name}')">
                    <i class="bi bi-graph-up"></i> Details
                </button>
                <button class="btn btn-sm btn-outline-warning" onclick="window.recalibrateZone('${zone.name}')" title="Recalibrate">
                    <i class="bi bi-arrow-clockwise"></i>
                </button>
                <button class="btn btn-sm btn-outline-danger" onclick="window.deleteZone('${zone.name}')" title="Delete">
                    <i class="bi bi-trash"></i>
                </button>
            </div>
        </div>
    `;
    return col;
}

// ============================================================================
// ACTIONS (Exposed to Window)
// ============================================================================

export async function recalibrateZone(zoneName) {
    if (!confirm(`Force recalibration for ${zoneName}?`)) return;
    try {
        await fetch(`/api/zones/${zoneName}/recalibrate`, { method: 'POST' });
        fetchZones(); // Refresh UI
    } catch (e) {
        alert("Recalibration failed: " + e.message);
    }
}

export async function deleteZone(zoneName) {
    if (!confirm(`Are you sure you want to delete zone "${zoneName}"?`)) return;
    try {
        await fetch(`/api/zones/${zoneName}`, { method: 'DELETE' });
        fetchZones(); // Refresh UI
    } catch (e) {
        alert("Delete failed: " + e.message);
    }
}

export function viewZoneDetails(zoneName) {
    const zone = zonesData.get(zoneName);
    if (!zone) return;

    document.getElementById('zone-details-title').innerText = zone.name;
    document.getElementById('zone-details-state').innerText = zone.state;

    const container = document.getElementById('zone-links-container');
    container.innerHTML = '';

    // Render links table
    if (zone.links && Object.keys(zone.links).length > 0) {
        const table = document.createElement('table');
        table.className = 'table table-sm table-striped small';
        table.innerHTML = `
            <thead>
                <tr>
                    <th>Link</th>
                    <th>RSSI</th>
                    <th>Baseline</th>
                    <th>Dev (σ)</th>
                </tr>
            </thead>
            <tbody>
                ${Object.entries(zone.links).map(([key, link]) => {
                    const dev = link.deviation ? link.deviation.toFixed(2) : '-';
                    const baseline = link.baseline_mean ? link.baseline_mean.toFixed(1) : '-';
                    const isTriggered = link.deviation > zone.config.deviation_threshold;
                    return `
                        <tr class="${isTriggered ? 'table-danger fw-bold' : ''}">
                            <td class="text-truncate" style="max-width: 150px;" title="${key}">${key}</td>
                            <td>${link.last_rssi}</td>
                            <td>${baseline}</td>
                            <td>${dev}</td>
                        </tr>
                    `;
                }).join('')}
            </tbody>
        `;
        container.appendChild(table);
    } else {
        container.innerHTML = '<p class="text-muted">No link data available yet. Wait for calibration.</p>';
    }

    const modal = new bootstrap.Modal(document.getElementById('zoneDetailsModal'));
    modal.show();
}

// ============================================================================
// CREATE ZONE LOGIC
// ============================================================================

function openCreateZoneModal() {
    document.getElementById('zone-name-input').value = '';
    selectedDevices.clear();
    updateSelectedCount();
    fetchDevicesForModal(); // Load devices

    const modal = new bootstrap.Modal(document.getElementById('createZoneModal'));
    modal.show();
}

const selectedDevices = new Set();

function renderDeviceList(devices) {
    const list = document.getElementById('zone-device-list');
    list.innerHTML = '';

    devices.forEach(device => {
        const item = document.createElement('a');
        item.className = 'list-group-item list-group-item-action d-flex justify-content-between align-items-center';
        item.style.cursor = 'pointer';
        item.innerHTML = `
            <div>
                <strong>${device.friendly_name || device.ieee}</strong><br>
                <small class="text-muted">${device.model} (${device.type})</small>
            </div>
            <input class="form-check-input" type="checkbox" ${selectedDevices.has(device.ieee) ? 'checked' : ''}>
        `;

        item.onclick = (e) => {
            e.preventDefault();
            if (selectedDevices.has(device.ieee)) {
                selectedDevices.delete(device.ieee);
                item.querySelector('input').checked = false;
                item.classList.remove('active');
            } else {
                selectedDevices.add(device.ieee);
                item.querySelector('input').checked = true;
                item.classList.add('active');
            }
            updateSelectedCount();
        };
        list.appendChild(item);
    });
}

function filterDeviceList(query) {
    const lowerQuery = query.toLowerCase();
    const filtered = deviceListCache.filter(d =>
        (d.friendly_name && d.friendly_name.toLowerCase().includes(lowerQuery)) ||
        d.ieee.toLowerCase().includes(lowerQuery)
    );
    renderDeviceList(filtered);
}

function updateSelectedCount() {
    document.getElementById('zone-selected-count').innerText = selectedDevices.size;
}

async function handleCreateZoneSubmit() {
    const name = document.getElementById('zone-name-input').value.trim();
    if (!name) return alert("Please enter a zone name");
    if (selectedDevices.size < 2) return alert("Select at least 2 devices");

    const payload = {
        name: name,
        device_ieees: Array.from(selectedDevices),
        deviation_threshold: parseFloat(document.getElementById('zone-deviation-threshold').value),
        variance_threshold: parseFloat(document.getElementById('zone-variance-threshold').value),
        min_links_triggered: parseInt(document.getElementById('zone-min-links').value),
        calibration_time: parseInt(document.getElementById('zone-calibration-time').value),
        clear_delay: parseInt(document.getElementById('zone-clear-delay').value)
    };

    try {
        const response = await fetch('/api/zones', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (!response.ok) throw new Error((await response.json()).detail || "Failed to create zone");

        // Close modal and refresh
        const modalEl = document.getElementById('createZoneModal');
        const modal = bootstrap.Modal.getInstance(modalEl);
        modal.hide();

        fetchZones();
    } catch (e) {
        alert("Error: " + e.message);
    }
}