/**
 * mesh.js
 * Enhanced mesh visualisation with connection table and packet statistics.
 * Topology graph is rendered with ECharts (force layout); see chart-utils.js.
 */

import { createChart } from './chart-utils.js';
import { reapplySort } from './table-utils.js';

const log = zmmLog('mesh');

// Module-level state
let dashboardMeshData = null;
let _meshChart = null;          // managed ECharts graph instance
let meshInitialised = false;
let labelsVisible = true;
let statsInterval = null;

/**
 * Initialise mesh module
 */
export function initMesh() {
    log.log('Mesh module initialised');

    const tabEl = document.querySelector('button[data-bs-target="#topology"]');
    if (tabEl) {
        tabEl.addEventListener('shown.bs.tab', function (event) {
            log.log('Mesh tab activated');
            setTimeout(() => {
                const container = document.querySelector('.mesh-topology-container');
                if (container && (container.children.length === 0 || container.innerHTML.trim() === "")) {
                    loadMeshTopology();
                }
            }, 50);
        });
    }
}

/**
 * Load mesh topology visualisation with connection table
 */
export async function loadMeshTopology() {
    const meshContainer = document.querySelector('.mesh-topology-container');
    if (!meshContainer) return;

    log.log('Loading mesh topology visualisation...');

    meshContainer.innerHTML = `
        <div class="text-center py-4">
            <div class="spinner-border text-primary" role="status">
                <span class="visually-hidden">Loading...</span>
            </div>
            <p class="text-muted mt-2">Discovering mesh topology...</p>
        </div>
    `;

    try {
            const response = await fetch('/api/network/simple-mesh');
            const data = await response.json();
            dashboardMeshData = data;

            // Build the full UI
            const meshContainer = document.querySelector('.mesh-topology-container');
            meshContainer.innerHTML = buildMeshUI();

            // Initialise graph and Tables
            initialiseMeshGraph(data);
            populateConnectionTable(data.connection_table || [], data.nodes || []);
            populatePacketStats(data.nodes || [], data.stats_summary || {});

            // Setup Tab Event Listeners for Auto-Refresh
            const statsTabBtn = document.querySelector('button[data-bs-target="#meshPacketStats"]');
            const otherTabs = document.querySelectorAll('button[data-bs-target="#meshVisualisation"], button[data-bs-target="#meshConnectionTable"]');

            if (statsTabBtn) {
                // When Packet Stats tab is shown, start polling every 2 seconds
                statsTabBtn.addEventListener('shown.bs.tab', () => {
                    refreshPacketStats(); // Immediate update
                    statsInterval = setInterval(refreshPacketStats, 2000);
                });

                // When leaving the tab, stop polling
                statsTabBtn.addEventListener('hidden.bs.tab', () => {
                    if (statsInterval) clearInterval(statsInterval);
                });
            }

    } catch (error) {
        log.error('Failed to load mesh topology:', error);
        meshContainer.innerHTML = `
            <div class="alert alert-danger m-3">
                <i class="fas fa-exclamation-triangle"></i>
                Failed to load mesh topology: ${error.message}
            </div>
        `;
    }
}

/**
 * Build the mesh UI with tabs
 */
function buildMeshUI() {
    return `
        <div class="mesh-wrapper">
            <!-- Controls -->
            <div class="mesh-controls p-2 bg-light border-bottom d-flex justify-content-between align-items-center">
                <div class="btn-group btn-group-sm">
                    <button class="btn btn-outline-primary" onclick="dashboardMeshRefresh()" title="Refresh topology">
                        <i class="fas fa-sync-alt"></i> Scan
                    </button>
                    <button class="btn btn-outline-secondary" onclick="dashboardMeshReset()" title="Reset view">
                        <i class="fas fa-expand"></i> Reset
                    </button>
                    <button class="btn btn-outline-secondary" onclick="dashboardMeshCenter()" title="Center view">
                        <i class="fas fa-crosshairs"></i> Center
                    </button>
                    <button class="btn btn-outline-secondary" onclick="toggleMeshLabels()" title="Toggle labels">
                        <i class="fas fa-tags"></i> Labels
                    </button>
                </div>
                <div class="mesh-legend small">
                    <span class="me-2"><i class="fas fa-square text-primary"></i> Coordinator</span>
                    <span class="me-2"><i class="fas fa-circle text-success"></i> Router</span>
                    <span class="me-2"><i class="fas fa-circle text-secondary"></i> End Device</span>
                    <span class="me-2 text-muted">|</span>
                    <span class="me-2"><i class="fas fa-circle" style="color:#dc3545"></i> Offline</span>
                    <span class="text-muted">|</span>
                    <span class="ms-2 signal-excellent">● &gt;200</span>
                    <span class="signal-good">● 150-200</span>
                    <span class="signal-fair">● 100-150</span>
                    <span class="signal-poor">● &lt;100</span>
                </div>
            </div>

            <!-- Sub-tabs for different views -->
            <ul class="nav nav-tabs px-2 pt-2" role="tablist">
                <li class="nav-item">
                    <button class="nav-link active" data-bs-toggle="tab" data-bs-target="#meshVisualisation">
                        <i class="fas fa-project-diagram"></i> Visualisation
                    </button>
                </li>
                <li class="nav-item">
                    <button class="nav-link" data-bs-toggle="tab" data-bs-target="#meshConnectionTable">
                        <i class="fas fa-table"></i> Connection Table
                    </button>
                </li>
                <li class="nav-item">
                    <button class="nav-link" data-bs-toggle="tab" data-bs-target="#meshPacketStats">
                        <i class="fas fa-chart-bar"></i> Packet Statistics
                    </button>
                </li>
            </ul>

            <!-- Tab content -->
            <div class="tab-content">
                <!-- Visualisation Tab -->
                <div class="tab-pane fade show active" id="meshVisualisation">
                    <div class="mesh-visualisation-wrapper" style="height: 1000px; position: relative; border: 1px solid #dee2e6; border-radius: 0 0 4px 4px; overflow: hidden;">
                        <div id="dashboard-mesh-graph" style="width: 100%; height: 100%;"></div>
                    </div>
                </div>

                <!-- Connection Table Tab -->
                <div class="tab-pane fade" id="meshConnectionTable">
                    <div class="p-3" style="max-height: 1000px; overflow-y: auto;">
                        <div class="table-responsive">
                            <table class="table table-sm table-striped table-hover tbl" id="connectionTable">
                                <thead class="sticky-top">
                                    <tr>
                                        <th>Source Device</th>
                                        <th>Role</th>
                                        <th>Connections</th>
                                    </tr>
                                </thead>
                                <tbody id="connectionTableBody"></tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <!-- Packet Statistics Tab -->
                <div class="tab-pane fade" id="meshPacketStats">
                    <div class="p-3">
                        <div class="row g-2 mb-3" id="packetStatsSummary"></div>

                        <div class="table-responsive" style="max-height: 1000px; overflow-y: auto;">
                            <table class="table table-sm table-striped table-hover tbl tbl-sortable" id="packetStatsTable">
                                <thead class="sticky-top">
                                    <tr>
                                        <th>Device</th>
                                        <th class="text-end" data-sort-type="number">LQI</th>
                                        <th class="text-end" data-sort-type="number">RSSI</th>
                                        <th class="text-end" data-sort-type="number">RX Packets</th>
                                        <th class="text-end" data-sort-type="number">TX Packets</th>
                                        <th class="text-end" data-sort-type="number">Total</th>
                                        <th class="text-end" data-sort-type="number">RX/min</th>
                                        <th class="text-end" data-sort-type="number">TX/min</th>
                                        <th class="text-end" data-sort-type="number">Errors</th>
                                        <th class="text-end" data-sort-type="number">Error %</th>
                                        <th data-sort-type="number">Load</th>
                                    </tr>
                                </thead>
                                <tbody id="packetStatsBody"></tbody>
                            </table>
                        </div>
                    </div>
                </div>

            </div>
        </div>
    `;
}

/**
 * Initialise the mesh topology graph (ECharts force layout).
 * Online devices participate in the force simulation with real links.
 * Offline devices are pinned in a grid (bottom-right) with no links.
 */
function initialiseMeshGraph(data) {
    const el = document.getElementById('dashboard-mesh-graph');
    if (!el) return;

    if (_meshChart) { _meshChart.dispose(); _meshChart = null; }
    _meshChart = createChart(el);

    const width = el.clientWidth || 800;

    const allNodes = data.nodes || [];
    const allLinks = data.links || [];
    const onlineIds = new Set(allNodes.filter(n => n.online).map(n => n.id));

    // Only keep links where BOTH source and target are online.
    const onlineLinks = allLinks.filter(l => {
        const srcId = typeof l.source === 'object' ? l.source.id : l.source;
        const tgtId = typeof l.target === 'object' ? l.target.id : l.target;
        return onlineIds.has(srcId) && onlineIds.has(tgtId);
    });

    const linkColor = (lqi) => {
        if (lqi >= 200) return '#00b894';
        if (lqi >= 150) return '#fdcb6e';
        if (lqi >= 100) return '#e17055';
        return '#d63031';
    };

    // Pinned grid positions for offline nodes (bottom-right corner).
    const offlineNodes = allNodes.filter(n => !n.online);
    const offCols = 3, offSpacing = 38;
    const offX0 = Math.max(80, width - 230), offY0 = 60;
    const offlinePos = new Map();
    offlineNodes.forEach((n, i) => {
        offlinePos.set(n.id, {
            x: offX0 + (i % offCols) * offSpacing,
            y: offY0 + Math.floor(i / offCols) * offSpacing,
        });
    });

    // Categories drive node colour + legend; offline nodes override per-node.
    const categories = [
        { name: 'Coordinator', itemStyle: { color: '#0d6efd' } },
        { name: 'Router',      itemStyle: { color: '#198754' } },
        { name: 'End Device',  itemStyle: { color: '#6c757d' } },
        { name: 'Offline',     itemStyle: { color: '#f8d7da' } },
    ];

    const nodes = allNodes.map(n => {
        const isCoord = n.role === 'Coordinator';
        const node = {
            id: n.id,
            name: n.friendly_name || n.id.slice(-8),
            symbol: isCoord ? 'rect' : 'circle',
            symbolSize: isCoord ? 22 : (n.online ? 18 : 14),
            category: !n.online ? 3 : isCoord ? 0 : n.role === 'Router' ? 1 : 2,
            itemStyle: n.online
                ? { borderColor: '#fff', borderWidth: 2 }
                : { color: '#f8d7da', borderColor: '#dc3545', borderWidth: 2, opacity: 0.85 },
            _info: n,   // full record, for the tooltip
        };
        if (!n.online) {
            const p = offlinePos.get(n.id);
            node.x = p.x;
            node.y = p.y;
            node.fixed = true;
            node.label = { color: '#999', fontSize: 9 };
        }
        return node;
    });

    const links = onlineLinks.map(l => ({
        source: typeof l.source === 'object' ? l.source.id : l.source,
        target: typeof l.target === 'object' ? l.target.id : l.target,
        lineStyle: {
            color: linkColor(l.lqi),
            width: Math.max(1, l.lqi / 50),
            opacity: 0.6,
        },
        _lqi: l.lqi,
    }));

    _meshChart.setOption({
        tooltip: {
            confine: true,
            formatter: (p) => {
                if (p.dataType === 'edge') {
                    return `LQI: <strong>${p.data._lqi}</strong>`;
                }
                const n = p.data._info || {};
                const s = n.packet_stats || {};
                return `<strong>${n.friendly_name || n.id}</strong><br/>`
                    + `Role: ${n.role}<br/>`
                    + `LQI: ${n.lqi}<br/>`
                    + `RSSI: ${n.rssi != null ? n.rssi + ' dBm' : '—'}<br/>`
                    + `NWK: ${n.network_address}<br/>`
                    + `Online: ${n.online ? 'Yes' : 'No'}<br/>`
                    + `RX: ${s.rx_packets || 0} · TX: ${s.tx_packets || 0}<br/>`
                    + `Rate: ${s.rx_rate || 0}/min`;
            },
        },
        legend: { show: false },
        series: [{
            type: 'graph',
            layout: 'force',
            roam: true,
            draggable: true,
            data: nodes,
            links,
            categories,
            force: { repulsion: 240, edgeLength: 110, gravity: 0.06, friction: 0.6 },
            edgeSymbol: ['none', 'arrow'],
            edgeSymbolSize: 7,
            label: {
                show: labelsVisible,
                position: 'right',
                fontSize: 11,
                formatter: (p) => p.data.name,
            },
            emphasis: { focus: 'adjacency', lineStyle: { width: 4 } },
        }],
    });

    meshInitialised = true;
}

/**
 * Populate the connection table with a Tree View
 * Now includes online/offline status for all devices
 */
function populateConnectionTable(connections, nodes) {
    const tbody = document.getElementById('connectionTableBody');
    if (!tbody) return;

    // Build online status + RSSI lookups from nodes
    const onlineMap = {};
    const rssiMap = {};
    (nodes || []).forEach(n => {
        onlineMap[n.id] = n.online;
        rssiMap[n.id] = n.rssi;
    });

    if (!connections || connections.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="4" class="text-center text-muted py-4">
                    <i class="fas fa-info-circle"></i> No connection data available.
                    Try running a topology scan.
                </td>
            </tr>
        `;
        return;
    }

    // Helpers
    const getLqiClass = (lqi) => {
        if (lqi >= 200) return 'signal-excellent';
        if (lqi >= 150) return 'signal-good';
        if (lqi >= 100) return 'signal-fair';
        if (lqi >= 50) return 'signal-poor';
        return 'signal-weak';
    };

    const getSignalBars = (lqi) => {
        const bars = Math.ceil(lqi / 51);
        let html = '';
        for (let i = 0; i < 5; i++) {
            const active = i < bars;
            html += `<span style="display:inline-block; width:4px; height:${8 + i * 3}px;
                     background:${active ? '#198754' : '#dee2e6'}; margin-right:1px;"></span>`;
        }
        return html;
    };

    const getStatusBadge = (ieee) => {
        const online = onlineMap[ieee];
        if (online === undefined) return '';
        return online
            ? '<span class="badge bg-success ms-1" style="font-size:0.6rem">Online</span>'
            : '<span class="badge bg-danger ms-1" style="font-size:0.6rem">Offline</span>';
    };

    // 1. Group connections by Source IEEE Address
    const grouped = {};
    connections.forEach(conn => {
        const sourceId = conn.source_ieee;
        if (!grouped[sourceId]) {
            grouped[sourceId] = {
                name: conn.source_name,
                ieee: conn.source_ieee,
                role: conn.source_role,
                online: onlineMap[conn.source_ieee] !== false,
                targets: []
            };
        }
        grouped[sourceId].targets.push(conn);
    });

    // 2. Build HTML — online sources first, then offline
    let html = '';

    const sortedGroups = Object.values(grouped).sort((a, b) => {
        // Online first, then alphabetical
        if (a.online !== b.online) return a.online ? -1 : 1;
        return (a.name || '').localeCompare(b.name || '');
    });

    sortedGroups.forEach((group, index) => {
        const collapseId = `conn-collapse-${index}`;
        const rowOpacity = group.online ? '' : 'opacity: 0.6;';

        // Parent Row (Source Device)
        html += `
            <tr style="cursor: pointer; ${rowOpacity}">
                <td data-bs-toggle="collapse" data-bs-target="#${collapseId}" aria-expanded="false" class="d-flex align-items-center border-0">
                    <i class="fas fa-chevron-right me-3 transition-icon text-muted"></i>
                    <div>
                        <span class="fw-medium">${escapeHtml(group.name)}</span>${getStatusBadge(group.ieee)}
                        <small class="text-muted d-block">${group.ieee.slice(-8)}</small>
                    </div>
                </td>
                <td class="align-middle">
                    <span class="badge ${getRoleBadgeClass(group.role)}">${group.role}</span>
                </td>
                <td class="align-middle">
                    <span class="badge bg-light text-dark border">${group.targets.length} Neighbors</span>
                </td>
            </tr>
        `;

        // Child Row (Expanded Details)
        html += `
            <tr>
                <td colspan="3" class="child-row-cell">
                    <div class="collapse" id="${collapseId}">
                        <div class="p-3 bg-light border-bottom shadow-inset">
                            <table class="table table-sm table-bordered nested-connection-table mb-0">
                                <thead>
                                    <tr>
                                        <th>Target Device</th>
                                        <th>Role</th>
                                        <th>Status</th>
                                        <th>Relationship</th>
                                        <th>LQI</th>
                                        <th title="Device RSSI as received by the coordinator">RSSI</th>
                                        <th>Signal</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${group.targets.map(t => {
                                        const tOnline = onlineMap[t.target_ieee] !== false;
                                        const tOpacity = tOnline ? '' : 'opacity: 0.6;';
                                        return `
                                        <tr style="${tOpacity}">
                                            <td>
                                                <span class="fw-medium">${escapeHtml(t.target_name)}</span>
                                                <small class="text-muted d-block">${t.target_ieee.slice(-8)}</small>
                                            </td>
                                            <td><span class="badge ${getRoleBadgeClass(t.target_role)}">${t.target_role}</span></td>
                                            <td>${getStatusBadge(t.target_ieee)}</td>
                                            <td><span class="badge bg-secondary">${t.relationship}</span></td>
                                            <td class="${getLqiClass(t.lqi)} fw-bold">${t.lqi}</td>
                                            <td>${rssiMap[t.target_ieee] != null ? `${rssiMap[t.target_ieee]} dBm` : '—'}</td>
                                            <td>${getSignalBars(t.lqi)}</td>
                                        </tr>
                                    `}).join('')}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </td>
            </tr>
        `;
    });

    tbody.innerHTML = html;
}

/**
 * Silently refresh packet statistics without redrawing the whole UI
 */
async function refreshPacketStats() {
    // specific check to ensure we don't run if the tab isn't actually visible
    const statsTab = document.getElementById('meshPacketStats');
    if (!statsTab || !statsTab.classList.contains('active')) return;

    try {
        const response = await fetch('/api/network/simple-mesh');
        const data = await response.json();

        // Update global data ref
        dashboardMeshData = data;
        addMobileLabelsToPacketStats();

        // Only update the stats table and summary
        populatePacketStats(data.nodes || [], data.stats_summary || {});
    } catch (error) {
        log.error("Silent stats refresh failed:", error);
    }
}

/**
 * Populate packet statistics
 */
function populatePacketStats(nodes, summary) {
    // CONFIGURATION
    // Define the "Red Line" for network traffic.
    // 5 Packets Per Second (300/min) is generally considered high for a single ZigBee device.
    const PPS_THRESHOLD = 5;
    const PPM_THRESHOLD = PPS_THRESHOLD * 60; // Convert to Per Minute for calculation

    // Summary cards
    const onlineCount = nodes.filter(n => n.online).length;
    const offlineCount = nodes.filter(n => !n.online).length;
    const summaryContainer = document.getElementById('packetStatsSummary');
    if (summaryContainer) {
        summaryContainer.innerHTML = `
            <div class="col-md-2">
                <div class="card text-center">
                    <div class="card-body py-2">
                        <div class="small text-muted">Devices</div>
                        <div class="h5 mb-0">${summary.total_devices || nodes.length}</div>
                        <div class="small">
                            <span class="text-success">${onlineCount} online</span>
                            ${offlineCount > 0 ? `<span class="text-danger ms-1">${offlineCount} offline</span>` : ''}
                        </div>
                    </div>
                </div>
            </div>
            <div class="col-md-2">
                <div class="card text-center">
                    <div class="card-body py-2">
                        <div class="small text-muted">Total RX</div>
                        <div class="h5 mb-0">${formatNumber(summary.total_rx_packets || 0)}</div>
                    </div>
                </div>
            </div>
            <div class="col-md-2">
                <div class="card text-center">
                    <div class="card-body py-2">
                        <div class="small text-muted">Total TX</div>
                        <div class="h5 mb-0">${formatNumber(summary.total_tx_packets || 0)}</div>
                    </div>
                </div>
            </div>
            <div class="col-md-2">
                <div class="card text-center">
                    <div class="card-body py-2">
                        <div class="small text-muted">Errors</div>
                        <div class="h5 mb-0 ${summary.total_errors > 0 ? 'text-danger' : ''}">${summary.total_errors || 0}</div>
                    </div>
                </div>
            </div>
            <div class="col-md-2">
                <div class="card text-center">
                    <div class="card-body py-2">
                        <div class="small text-muted">Avg/Device</div>
                        <div class="h5 mb-0">${summary.avg_packets_per_device || 0}</div>
                    </div>
                </div>
            </div>
            <div class="col-md-2">
                <div class="card text-center">
                    <div class="card-body py-2">
                        <div class="small text-muted">Uptime</div>
                        <div class="h5 mb-0">${formatUptime(summary.uptime_seconds || 0)}</div>
                    </div>
                </div>
            </div>
        `;
    }

    // Device stats table
    const tbody = document.getElementById('packetStatsBody');
    if (!tbody) return;

    // Sort: online first (by activity), then offline (by activity)
    const sortedNodes = [...nodes].sort((a, b) => {
        // Online devices first
        if (a.online !== b.online) return a.online ? -1 : 1;
        // Then by current activity rate
        const aRate = (a.packet_stats?.rx_rate || 0) + (a.packet_stats?.tx_rate || 0);
        const bRate = (b.packet_stats?.rx_rate || 0) + (b.packet_stats?.tx_rate || 0);
        return bRate - aRate;
    });

    tbody.innerHTML = sortedNodes.map(node => {
        const stats = node.packet_stats || {};
        const isOffline = !node.online;
        const rowStyle = isOffline ? 'opacity: 0.6;' : '';
        const statusBadge = isOffline
            ? '<span class="badge bg-danger ms-1" style="font-size:0.6rem">Offline</span>'
            : '<span class="badge bg-success ms-1" style="font-size:0.6rem">Online</span>';

        // Calculate current activity: RX Rate + TX Rate (Packets Per Minute)
        const currentPpm = (stats.rx_rate || 0) + (stats.tx_rate || 0);

        // Calculate % Load against our defined Threshold
        // If currentPpm = 300 and Threshold = 300, we are at 100% load
        const loadPercent = Math.round((currentPpm / PPM_THRESHOLD) * 100);

        // Cap visual bar at 100% so it doesn't overflow, but allow logic to see higher
        const visualPercent = Math.min(loadPercent, 100);

        return `
            <tr style="${rowStyle}">
                <td data-sort-value="${escapeHtml(node.friendly_name || '')}">
                    <span class="fw-medium">${escapeHtml(node.friendly_name)}</span>${statusBadge}
                    <small class="text-muted d-block">${node.ieee_address.slice(-8)}</small>
                </td>
                <td class="text-end" data-sort-value="${node.lqi || 0}">${node.lqi || 0}</td>
                <td class="text-end" data-sort-value="${node.rssi ?? -999}">${node.rssi != null ? `${node.rssi} dBm` : '—'}</td>
                <td class="text-end" data-sort-value="${stats.rx_packets || 0}">${formatNumber(stats.rx_packets || 0)}</td>
                <td class="text-end" data-sort-value="${stats.tx_packets || 0}">${formatNumber(stats.tx_packets || 0)}</td>
                <td class="text-end fw-bold" data-sort-value="${stats.total_packets || 0}">${formatNumber(stats.total_packets || 0)}</td>
                <td class="text-end" data-sort-value="${stats.rx_rate || 0}">${stats.rx_rate || 0}</td>
                <td class="text-end" data-sort-value="${stats.tx_rate || 0}">${stats.tx_rate || 0}</td>
                <td class="text-end ${stats.errors > 0 ? 'text-danger' : ''}" data-sort-value="${stats.errors || 0}">${stats.errors || 0}</td>
                <td class="text-end ${stats.error_rate > 5 ? 'text-danger' : ''}" data-sort-value="${stats.error_rate || 0}">${stats.error_rate || 0}%</td>
                <td style="width: 100px; vertical-align: middle;" data-sort-value="${loadPercent}">
                    <div class="progress" style="height: 8px;" title="Current: ${currentPpm} PPM / Threshold: ${PPM_THRESHOLD} PPM">
                        <div class="progress-bar ${getLoadBarClass(loadPercent)}"
                             style="width: ${visualPercent}%"></div>
                    </div>
                </td>
            </tr>
        `;
    }).join('');

    // Keep the user's chosen column sort across the 2s auto-refresh.
    reapplySort(document.getElementById('packetStatsTable'));
}

function addMobileLabelsToPacketStats() {
    const table = document.querySelector('#meshPacketStats table');
    if (!table) return;

    // Get header texts
    const headers = [];
    table.querySelectorAll('thead th').forEach(th => {
        headers.push(th.textContent.trim());
    });

    // Add data-label to each td
    table.querySelectorAll('tbody tr').forEach(row => {
        row.querySelectorAll('td').forEach((td, i) => {
            if (headers[i]) {
                td.setAttribute('data-label', headers[i]);
            }
        });
    });
}

// Helper functions
function getRoleBadgeClass(role) {
    switch (role) {
        case 'Coordinator': return 'bg-primary';
        case 'Router': return 'bg-success';
        default: return 'bg-secondary';
    }
}

function getLoadBarClass(percent) {
    if (percent >= 80) return 'bg-danger';
    if (percent >= 50) return 'bg-warning';
    return 'bg-success';
}

function formatNumber(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return n.toString();
}

function formatUptime(seconds) {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
    return `${Math.round(seconds / 86400)}d`;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

export async function dashboardMeshRefresh() {
    const scanBtn = document.querySelector('.mesh-controls .btn-outline-primary');
    if (scanBtn) {
        scanBtn.disabled = true;
        scanBtn.innerHTML = '<i class="fas fa-sync-alt fa-spin"></i> Scanning...';
    }

    try {
        // 1. Trigger the actual topology scan
        const scanRes = await fetch('/api/network/scan', { method: 'POST' });
        const scanData = await scanRes.json();

        if (!scanData.success) {
            log.error('Scan failed:', scanData.error);
            return;
        }

        // 2. Poll scan status until complete (max 60s)
        let attempts = 0;
        while (attempts < 30) {
            await new Promise(r => setTimeout(r, 2000));
            const statusRes = await fetch('/api/network/scan/status');
            const status = await statusRes.json();
            if (!status.in_progress) break;
            attempts++;
        }

        // 3. Reload the visualisation with fresh data
        await loadMeshTopology();

    } catch (error) {
        log.error('Mesh refresh failed:', error);
    } finally {
        if (scanBtn) {
            scanBtn.disabled = false;
            scanBtn.innerHTML = '<i class="fas fa-sync-alt"></i> Scan';
        }
    }
}

// Reset and Center both restore the initial zoom/pan and re-run the layout.
export function dashboardMeshReset() {
    if (dashboardMeshData) initialiseMeshGraph(dashboardMeshData);
}

export function dashboardMeshCenter() {
    if (dashboardMeshData) initialiseMeshGraph(dashboardMeshData);
}

export function toggleMeshLabels() {
    labelsVisible = !labelsVisible;
    if (_meshChart) {
        _meshChart.instance().setOption({ series: [{ label: { show: labelsVisible } }] });
    }
}