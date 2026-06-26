/**
 * System Telemetry Tab
 * Location: static/js/system-telemetry.js
 *
 * Fluid real-time system monitoring with:
 *   - Surgical DOM updates (no innerHTML rebuilds during refresh)
 *   - CSS transitions on gauge bars and colours
 *   - ECharts time-series history chart (legend toggle + axis tooltip built in)
 *   - Staggered refresh: gauges 5s, chart 30s, DB stats 60s
 */

import { state } from './state.js';
import { createChart } from './chart-utils.js';

let _gaugeTimer = null;
let _chartTimer = null;
let _dbTimer = null;
let _chart = null;
let _chartData = [];

const SERIES = [
    { key: 'cpu_percent', label: 'CPU %', color: '#0d6efd', id: 'cpu' },
    { key: 'mem_percent', label: 'Memory %', color: '#198754', id: 'mem' },
    { key: 'cpu_temp',    label: 'CPU Temp', color: '#dc3545', id: 'temp' },
];

// ============================================================================
// INIT
// ============================================================================

export function initSystemTab() {
    const tab = document.querySelector('button[data-bs-target="#system"]');
    if (tab) {
        tab.addEventListener('shown.bs.tab', () => _startTab());
        tab.addEventListener('hidden.bs.tab', () => _stopTab());
    }
}

function _startTab() {
    const container = document.getElementById('system-content');
    if (!container) return;

    // Only build skeleton once
    if (!document.getElementById('sys-gauges')) {
        container.innerHTML = _renderSkeleton();
    }

    // Initial fetch
    _refreshGauges();
    _refreshChart();
    _refreshDbStats();

    // Staggered intervals
    _stopTab();
    _gaugeTimer = setInterval(_refreshGauges, 5000);
    _chartTimer = setInterval(_refreshChart, 30000);
    _dbTimer    = setInterval(_refreshDbStats, 60000);
}

function _stopTab() {
    if (_gaugeTimer) { clearInterval(_gaugeTimer); _gaugeTimer = null; }
    if (_chartTimer) { clearInterval(_chartTimer); _chartTimer = null; }
    if (_dbTimer)    { clearInterval(_dbTimer);    _dbTimer = null; }
}

// ============================================================================
// SKELETON (rendered once)
// ============================================================================

function _renderSkeleton() {
    return `
    <style>
        .sys-bar { transition: width 0.8s ease, background-color 0.5s ease; }
        .sys-val { transition: color 0.5s ease; }
    </style>

    <!-- Gauges -->
    <div class="row g-3 mb-3" id="sys-gauges">
        ${_gaugeCard('cpu',  'CPU',         'microchip',        80, 95)}
        ${_gaugeCard('mem',  'Memory',      'memory',           80, 90)}
        ${_gaugeCard('temp', 'Temperature', 'thermometer-half', 75, 85)}
        ${_gaugeCard('disk', 'Disk',        'hdd',              85, 95)}
        ${_gaugeCard('proc', 'Process',     'cogs',             0,  0)}
        ${_gaugeCard('load', 'Load / Uptime','tachometer-alt',  0,  0)}
    </div>

    <!-- Alerts -->
    <div id="sys-alerts" class="mb-3"></div>

    <!-- History Chart -->
    <div class="card mb-3">
        <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
            <strong class="small"><i class="fas fa-chart-line me-1"></i> System History</strong>
            <div class="d-flex gap-2 align-items-center">
                <select class="form-select form-select-sm" id="sys-history-hours" style="width:auto" onchange="window._sysRefreshChart()">
                    <option value="1" selected>Last 1h</option>
                    <option value="6">Last 6h</option>
                    <option value="24">Last 24h</option>
                    <option value="72">Last 3 days</option>
                    <option value="168">Last 7 days</option>
                </select>
                <button class="btn btn-sm btn-outline-secondary" onclick="window._sysRefreshChart()"><i class="fas fa-sync-alt"></i></button>
            </div>
        </div>
        <div class="card-body p-2">
            <div id="sys-history-chart" style="height:240px">
                <div class="text-muted small text-center py-4"><i class="fas fa-spinner fa-spin"></i> Loading history...</div>
            </div>
        </div>
    </div>

    <!-- DB Stats -->
    <div class="card">
        <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
            <strong class="small"><i class="fas fa-database me-1"></i> Telemetry Database</strong>
            <button class="btn btn-sm btn-outline-danger" onclick="window._sysPrune()" title="Prune old data">
                <i class="fas fa-broom me-1"></i> Prune
            </button>
        </div>
        <div class="card-body py-2" id="sys-db-stats">
            <span class="text-muted small"><i class="fas fa-spinner fa-spin"></i></span>
        </div>
    </div>`;
}

function _gaugeCard(id, label, icon, warn, crit) {
    return `
    <div class="col-md-2 col-sm-4 col-6">
        <div class="card h-100">
            <div class="card-body py-2 px-2">
                <div class="d-flex justify-content-between align-items-center mb-1">
                    <span class="text-muted small"><i class="fas fa-${icon} me-1"></i>${label}</span>
                    <span id="sys-val-${id}" class="fw-bold sys-val" style="font-size:1.1rem">—</span>
                </div>
                ${(warn > 0) ? `<div class="progress" style="height:4px">
                    <div id="sys-bar-${id}" class="progress-bar sys-bar bg-success" role="progressbar" style="width:0%"></div>
                </div>` : ''}
                <div id="sys-sub-${id}" class="text-muted mt-1" style="font-size:0.68rem;line-height:1.2"></div>
            </div>
        </div>
    </div>`;
}

// ============================================================================
// GAUGE UPDATES (surgical — textContent + attribute only)
// ============================================================================

async function _refreshGauges() {
    try {
        const res = await fetch('/api/telemetry/system/current');
        const d = await res.json();
        if (!d || d.error) return;
        _updateGauges(d);
    } catch (e) { /* silent */ }
}

function _updateGauges(d) {
    // CPU
    _setVal('cpu', d.cpu_percent != null ? `${d.cpu_percent.toFixed(0)}%` : '—');
    _setBar('cpu', d.cpu_percent, 80, 95);
    _setSub('cpu', d.cpu_freq ? `${d.cpu_freq.toFixed(0)} MHz` : '');

    // Memory
    const memGB = d.mem_used ? (d.mem_used / 1073741824).toFixed(1) : '?';
    const memTotGB = d.mem_total ? (d.mem_total / 1073741824).toFixed(1) : '?';
    _setVal('mem', d.mem_percent != null ? `${d.mem_percent.toFixed(0)}%` : '—');
    _setBar('mem', d.mem_percent, 80, 90);
    _setSub('mem', `${memGB} / ${memTotGB} GB`);

    // Temperature
    const parts = [];
    if (d.cpu_temp != null) parts.push(`CPU ${d.cpu_temp.toFixed(0)}°C`);
    if (d.gpu_temp != null) parts.push(`GPU ${d.gpu_temp.toFixed(0)}°C`);
    _setVal('temp', d.cpu_temp != null ? `${d.cpu_temp.toFixed(0)}°C` : '—');
    _setBar('temp', d.cpu_temp, 75, 85);
    _setSub('temp', parts.join(' · ') || 'No sensors');

    // Disk
    const dskGB = d.disk_used ? (d.disk_used / 1073741824).toFixed(1) : '?';
    const dskTotGB = d.disk_total ? (d.disk_total / 1073741824).toFixed(1) : '?';
    _setVal('disk', d.disk_percent != null ? `${d.disk_percent.toFixed(0)}%` : '—');
    _setBar('disk', d.disk_percent, 85, 95);
    _setSub('disk', `${dskGB} / ${dskTotGB} GB`);

    // Process (no bar)
    const rss = d.process_rss ? (d.process_rss / 1048576).toFixed(0) : '?';
    _setVal('proc', `${rss} MB`);
    _setSub('proc', d.process_threads ? `${d.process_threads} threads` : '');

    // Load + Uptime (no bar)
    _setVal('load', d.load_1m != null ? d.load_1m.toFixed(2) : '—');
    let loadSub = '';
    if (d.load_5m != null) loadSub = `5m: ${d.load_5m.toFixed(2)} · 15m: ${d.load_15m?.toFixed(2) || '?'}`;
    if (d.uptime_secs) {
        const days = Math.floor(d.uptime_secs / 86400);
        const hrs = Math.floor((d.uptime_secs % 86400) / 3600);
        const up = days > 0 ? `${days}d ${hrs}h` : `${hrs}h`;
        loadSub = `uptime ${up}` + (loadSub ? ` · ${loadSub}` : '');
    }
    _setSub('load', loadSub);

    // Alerts
    _updateAlerts(d.active_alerts);
}

function _setVal(id, text) {
    const el = document.getElementById(`sys-val-${id}`);
    if (el && el.textContent !== text) el.textContent = text;
}

function _setSub(id, text) {
    const el = document.getElementById(`sys-sub-${id}`);
    if (el && el.textContent !== text) el.textContent = text;
}

function _setBar(id, value, warn, crit) {
    const el = document.getElementById(`sys-bar-${id}`);
    if (!el || value == null) return;
    const pct = Math.min(Math.max(value, 0), 100);
    el.style.width = `${pct}%`;
    el.classList.remove('bg-success', 'bg-warning', 'bg-danger');
    if (value >= crit) el.classList.add('bg-danger');
    else if (value >= warn) el.classList.add('bg-warning');
    else el.classList.add('bg-success');

    // Value text colour
    const valEl = document.getElementById(`sys-val-${id}`);
    if (valEl) {
        valEl.classList.remove('text-success', 'text-warning', 'text-danger');
        if (value >= crit) valEl.classList.add('text-danger');
        else if (value >= warn) valEl.classList.add('text-warning');
        else valEl.classList.add('text-success');
    }
}

function _updateAlerts(alerts) {
    const el = document.getElementById('sys-alerts');
    if (!el) return;
    if (!alerts || Object.keys(alerts).length === 0) {
        if (el.innerHTML !== '') el.innerHTML = '';
        return;
    }
    const html = Object.entries(alerts).map(([m, s]) => {
        const cls = s === 'critical' ? 'danger' : 'warning';
        const ico = s === 'critical' ? 'exclamation-circle' : 'exclamation-triangle';
        return `<span class="badge bg-${cls} me-1"><i class="fas fa-${ico} me-1"></i>${m}: ${s}</span>`;
    }).join('');
    const newHtml = `<div class="alert alert-warning py-2 small mb-0"><i class="fas fa-bell me-1"></i> Active: ${html}</div>`;
    if (el.innerHTML !== newHtml) el.innerHTML = newHtml;
}

// ============================================================================
// HISTORY CHART — ECharts time-series, full option rebuilt on each refresh
// ============================================================================

async function _refreshChart() {
    const hours = parseInt(document.getElementById('sys-history-hours')?.value || '1');
    const bucket = hours <= 1 ? 1 : hours <= 6 ? 2 : hours <= 24 ? 5 : 15;

    try {
        const res = await fetch(`/api/telemetry/system/history?hours=${hours}&bucket=${bucket}`);
        const json = await res.json();
        if (!json.success || !json.data?.length) {
            if (!_chart) {
                const el = document.getElementById('sys-history-chart');
                if (el) el.innerHTML = '<div class="text-muted small text-center py-4">No history yet — collecting every 30s.</div>';
            }
            return;
        }
        _chartData = json.data;
        _renderChart();
    } catch (e) { /* silent */ }
}

function _renderChart() {
    const el = document.getElementById('sys-history-chart');
    if (!el || !_chartData.length) return;

    // First data after a (re)build of the skeleton: clear the loader and init.
    if (!_chart) {
        el.innerHTML = '';
        _chart = createChart(el);
    }

    const series = SERIES.map(s => ({
        name: s.label,
        type: 'line',
        showSymbol: false,
        smooth: true,
        sampling: 'lttb',
        lineStyle: { width: 1.5, color: s.color },
        itemStyle: { color: s.color },
        data: _chartData
            .filter(d => d[s.key] != null)
            .map(d => [new Date(d.ts).getTime(), d[s.key]]),
    }));

    _chart.setOption({
        animationDuration: 600,
        grid: { top: 32, right: 12, bottom: 22, left: 38 },
        legend: {
            top: 0,
            itemHeight: 8,
            itemWidth: 14,
            textStyle: { fontSize: 11 },
            data: SERIES.map(s => s.label),
        },
        tooltip: {
            trigger: 'axis',
            valueFormatter: v => (v == null ? '—' : Number(v).toFixed(1)),
        },
        xAxis: {
            type: 'time',
            axisLabel: { fontSize: 9, hideOverlap: true },
        },
        yAxis: {
            type: 'value',
            min: 0,
            max: 100,
            splitNumber: 4,
            axisLabel: { fontSize: 9 },
        },
        series,
    });
}

// ============================================================================
// DB STATS (low-frequency, innerHTML is fine here)
// ============================================================================

async function _refreshDbStats() {
    try {
        const res = await fetch('/api/telemetry/db/stats');
        const data = await res.json();
        if (!data.success) return;

        const el = document.getElementById('sys-db-stats');
        if (!el) return;

        const tables = ['system_metrics', 'packet_stats', 'device_states', 'spectrum_scans'];
        const badges = tables.map(t => {
            const count = data[t] || 0;
            const label = t.replace(/_/g, ' ');
            return `<span class="badge bg-light text-dark border me-2">${label}: <strong>${count.toLocaleString()}</strong></span>`;
        }).join('');

        const newHtml = `<div class="d-flex flex-wrap align-items-center gap-1">${badges}` +
            `<span class="badge bg-info text-white ms-2">${data.file_size_mb || 0} MB</span></div>`;

        if (el.innerHTML !== newHtml) el.innerHTML = newHtml;
    } catch (e) { /* silent */ }
}

// ============================================================================
// ACTIONS
// ============================================================================

async function _sysPrune() {
    if (!confirm('Prune telemetry data older than 7 days?')) return;
    try {
        const res = await fetch('/api/telemetry/db/prune', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            await _refreshDbStats();
        }
    } catch (e) {
        alert('Prune failed: ' + e.message);
    }
}

// ============================================================================
// WINDOW HANDLERS
// ============================================================================

window._sysRefreshChart = _refreshChart;
window._sysPrune = _sysPrune;