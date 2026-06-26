/**
 * Device History Tab
 * Location: static/js/modal/history.js
 *
 * Renders time-series charts for attribute changes stored in DuckDB.
 * Data source: /api/telemetry/device/{ieee}/...
 */

import { createChart } from '../chart-utils.js';

// Live ECharts instance for the numeric view; disposed/recreated each refresh.
let _histChart = null;

const HOURS_OPTIONS = [
    { v: 1,   label: '1h'  },
    { v: 6,   label: '6h'  },
    { v: 24,  label: '24h' },
    { v: 72,  label: '3d'  },
    { v: 168, label: '7d'  },
];

export function renderHistoryTab(device) {
    return `
        <div class="mb-3 d-flex gap-2 align-items-center flex-wrap">
            <label class="small text-muted mb-0">Attribute</label>
            <select class="form-select form-select-sm" id="hist-attr" style="width:auto"></select>
            <label class="small text-muted mb-0 ms-2">Range</label>
            <select class="form-select form-select-sm" id="hist-hours" style="width:auto">
                ${HOURS_OPTIONS.map(o =>
                    `<option value="${o.v}" ${o.v === 24 ? 'selected' : ''}>${o.label}</option>`
                ).join('')}
            </select>
            <button class="btn btn-sm btn-outline-secondary" id="hist-refresh">
                <i class="fas fa-sync-alt"></i>
            </button>
            <span class="ms-auto small text-muted" id="hist-meta"></span>
        </div>
        <div id="hist-chart-wrap">
            <div class="text-muted small text-center py-4">Loading history…</div>
        </div>
        <div id="hist-raw" class="mt-3"></div>
    `;
}

export async function initHistoryTab(ieee) {
    const attrSel   = document.getElementById('hist-attr');
    const hoursSel  = document.getElementById('hist-hours');
    const refreshBtn = document.getElementById('hist-refresh');
    if (!attrSel || !hoursSel) return;

    // Populate attribute list
    try {
        const res = await fetch(`/api/telemetry/device/${ieee}/attributes?hours=168`);
        const json = await res.json();
        const attrs = (json.success && json.attributes) ? json.attributes : [];
        if (!attrs.length) {
            document.getElementById('hist-chart-wrap').innerHTML =
                '<div class="text-muted small text-center py-4">' +
                'No history recorded yet — data accumulates as the device reports.' +
                '</div>';
            return;
        }
        attrSel.innerHTML = attrs.map(a =>
            `<option value="${a}">${a}</option>`
        ).join('');
    } catch (e) {
        document.getElementById('hist-chart-wrap').innerHTML =
            `<div class="text-danger small py-4">Failed to load attributes: ${e.message}</div>`;
        return;
    }

    const refresh = () => _refreshHistoryChart(ieee);
    attrSel.addEventListener('change', refresh);
    hoursSel.addEventListener('change', refresh);
    refreshBtn.addEventListener('click', refresh);
    refresh();
}

async function _refreshHistoryChart(ieee) {
    const attr = document.getElementById('hist-attr')?.value;
    const hours = parseInt(document.getElementById('hist-hours')?.value || '24');
    if (!attr) return;

    const bucket = hours <= 1 ? 1 : hours <= 6 ? 2 : hours <= 24 ? 5 : hours <= 72 ? 15 : 30;

    const wrap = document.getElementById('hist-chart-wrap');
    wrap.innerHTML = '<div class="text-muted small text-center py-4">Loading…</div>';

    try {
        const res = await fetch(
            `/api/telemetry/device/${ieee}/history?attribute=${encodeURIComponent(attr)}&hours=${hours}&bucket=${bucket}`
        );
        const json = await res.json();
        if (!json.success || !json.data?.length) {
            wrap.innerHTML = '<div class="text-muted small text-center py-4">No data in this range.</div>';
            document.getElementById('hist-meta').textContent = '';
            return;
        }
        _buildHistChart(json.data, attr);
        const total = json.data.reduce((s, r) => s + (r.samples || 0), 0);
        document.getElementById('hist-meta').textContent =
            `${total} samples · ${bucket}m buckets`;
    } catch (e) {
        wrap.innerHTML = `<div class="text-danger small py-4">Query failed: ${e.message}</div>`;
    }
}

function _buildHistChart(data, attr) {
    const wrap = document.getElementById('hist-chart-wrap');
    if (!wrap) return;

    // Any rebuild starts fresh — drop the previous chart so we don't leak
    // instances when the attribute/range changes or we switch view modes.
    if (_histChart) { _histChart.dispose(); _histChart = null; }

    const numeric = data.some(r => r.avg !== null && r.avg !== undefined);

    if (!numeric) {
        // Non-numeric: show timeline table of state changes
        wrap.innerHTML = `
            <div class="table-responsive" style="max-height:400px">
                <table class="table table-sm table-striped">
                    <thead><tr><th>Time</th><th>${attr}</th><th class="text-end">Samples</th></tr></thead>
                    <tbody>
                        ${data.map(r => `
                            <tr>
                                <td class="small font-monospace">${new Date(r.ts).toLocaleString()}</td>
                                <td class="small">${r.last_str ?? ''}</td>
                                <td class="small text-end text-muted">${r.samples}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        `;
        return;
    }

    // Numeric: ECharts line with a min/max confidence band behind the average.
    wrap.innerHTML = '<div id="hist-chart-canvas" style="height:240px"></div>';
    _histChart = createChart(document.getElementById('hist-chart-canvas'));

    // Sample counts keyed by timestamp, for the tooltip.
    const samplesByTs = {};
    data.forEach(d => { samplesByTs[new Date(d.ts).getTime()] = d.samples; });

    const avgData = data.map(d => [new Date(d.ts).getTime(), d.avg]);
    // Confidence band via the stacked-area trick: a transparent baseline at
    // `min`, then `max - min` stacked on top with a filled area.
    const minData  = data.map(d => [new Date(d.ts).getTime(), d.min]);
    const bandData = data.map(d => [new Date(d.ts).getTime(), (d.max ?? d.avg) - (d.min ?? d.avg)]);

    _histChart.setOption({
        animationDuration: 500,
        grid: { top: 16, right: 14, bottom: 24, left: 52 },
        tooltip: {
            trigger: 'axis',
            formatter: (params) => {
                const p = params.find(x => x.seriesName === attr) || params[0];
                const ts = p.value[0];
                const when = new Date(ts).toLocaleString();
                const n = samplesByTs[ts];
                const avg = p.value[1];
                return `${when}<br/><strong>${attr}: ${avg == null ? '—' : Number(avg).toFixed(2)}</strong>`
                    + (n != null ? `<br/><span style="opacity:.7">${n} samples</span>` : '');
            },
        },
        xAxis: { type: 'time', axisLabel: { fontSize: 9, hideOverlap: true } },
        yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 9 } },
        series: [
            // Invisible baseline for the band.
            {
                name: '_min',
                type: 'line',
                stack: 'band',
                data: minData,
                symbol: 'none',
                lineStyle: { opacity: 0 },
                silent: true,
                tooltip: { show: false },
                z: 1,
            },
            // The band itself (height = max - min).
            {
                name: '_band',
                type: 'line',
                stack: 'band',
                data: bandData,
                symbol: 'none',
                lineStyle: { opacity: 0 },
                areaStyle: { color: '#4a90e2', opacity: 0.15 },
                silent: true,
                tooltip: { show: false },
                z: 1,
            },
            // The average line + points on top.
            {
                name: attr,
                type: 'line',
                data: avgData,
                smooth: true,
                showSymbol: data.length < 60,
                symbolSize: 5,
                lineStyle: { width: 1.5, color: '#4a90e2' },
                itemStyle: { color: '#4a90e2', borderColor: '#fff', borderWidth: 1 },
                z: 2,
            },
        ],
    });
}