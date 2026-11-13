/**
 * Device History Tab
 * Location: static/js/modal/history.js
 *
 * Renders time-series charts for attribute changes stored in DuckDB, and
 * (Events view) the device's logbook lines, each of which opens the same
 * trace as the Debug tab's live log.
 * Data source: /api/telemetry/device/{ieee}/..., /api/logbook/events?ieee=
 */

import { createChart } from '../chart-utils.js';
import { state } from '../state.js';
import { escapeHtml } from '../utils.js';
import { openTrace, stamp } from '../logging.js';

// Metadata keys never recorded by the backend (mirrors telemetry_collector
// SKIP_ATTRS) — excluded when merging current state keys into the dropdown.
const SKIP_ATTRS = new Set(['manufacturer', 'model', 'power_source', 'last_seen', 'available']);

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
            <div class="btn-group btn-group-sm me-2" role="group" id="hist-view" aria-label="History view">
                <button type="button" class="btn btn-outline-secondary active" data-view="chart" aria-pressed="true">
                    <i class="fas fa-chart-line me-1"></i>Chart</button>
                <button type="button" class="btn btn-outline-secondary" data-view="events" aria-pressed="false">
                    <i class="fas fa-list me-1"></i>Events</button>
            </div>
            <label class="small text-muted mb-0">Attribute</label>
            <select class="form-select form-select-sm" id="hist-attr" style="width:auto"></select>
            <label class="small text-muted mb-0 ms-2">Range</label>
            <select class="form-select form-select-sm" id="hist-hours" style="width:auto">
                ${HOURS_OPTIONS.map(o =>
                    `<option value="${o.v}" ${o.v === 24 ? 'selected' : ''}>${o.label}</option>`
                ).join('')}
            </select>
            <div class="form-check mb-0 ms-2 d-none" id="hist-only-attr-wrap">
                <input class="form-check-input" type="checkbox" id="hist-only-attr">
                <label class="form-check-label small" for="hist-only-attr">Only this attribute</label>
            </div>
            <button class="btn btn-sm btn-outline-secondary" id="hist-refresh">
                <i class="fas fa-sync-alt"></i>
            </button>
            <span class="ms-auto small text-muted" id="hist-meta"></span>
        </div>
        <div id="hist-chart-wrap">
            <div class="text-muted small text-center py-4">Loading history…</div>
        </div>
        <div id="hist-events-wrap" class="d-none"></div>
        <div id="hist-raw" class="mt-3"></div>
    `;
}

export async function initHistoryTab(ieee) {
    const attrSel   = document.getElementById('hist-attr');
    const hoursSel  = document.getElementById('hist-hours');
    const refreshBtn = document.getElementById('hist-refresh');
    if (!attrSel || !hoursSel) return;

    // Chart | Events. Wired before the attribute list loads, so the events
    // view works for a device with nothing in telemetry.
    _view = 'chart';
    _ev = { ieee, rows: [], more: false, token: 0 };
    const refresh = () => (_view === 'events' ? _loadEvents(ieee) : _refreshHistoryChart(ieee));
    document.getElementById('hist-view')?.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-view]');
        if (!btn || btn.dataset.view === _view) return;
        _view = btn.dataset.view;
        document.querySelectorAll('#hist-view [data-view]').forEach(b => {
            const on = b.dataset.view === _view;
            b.classList.toggle('active', on);
            b.setAttribute('aria-pressed', String(on));
        });
        document.getElementById('hist-chart-wrap')?.classList.toggle('d-none', _view !== 'chart');
        document.getElementById('hist-events-wrap')?.classList.toggle('d-none', _view !== 'events');
        document.getElementById('hist-only-attr-wrap')?.classList.toggle('d-none', _view !== 'events');
        document.getElementById('hist-meta').textContent = '';
        refresh();
    });
    document.getElementById('hist-only-attr')?.addEventListener('change', _renderEvents);
    hoursSel.addEventListener('change', refresh);
    refreshBtn.addEventListener('click', refresh);
    // A different attribute redraws the chart, or refilters the events.
    attrSel.addEventListener('change', () => (_view === 'events' ? _renderEvents() : _refreshHistoryChart(ieee)));

    // Populate attribute list: everything recorded within retention (30d),
    // merged with the device's current state keys so attributes that only
    // gain rows via the hourly keep-alive snapshot are selectable at once.
    try {
        const res = await fetch(`/api/telemetry/device/${ieee}/attributes?hours=720`);
        const json = await res.json();
        const recorded = (json.success && json.attributes) ? json.attributes : [];
        const stateAttrs = Object.keys(state.deviceCache?.[ieee]?.state || {})
            .filter(k => !SKIP_ATTRS.has(k) && !k.endsWith('_raw') && !k.startsWith('attr_'));
        const attrs = [...new Set([...recorded, ...stateAttrs])].sort();
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

    refresh();
}

// ── Events view ──────────────────────────────────────────────────────────

let _view = 'chart';
let _ev = { ieee: null, rows: [], more: false, token: 0 };
const EV_PAGE = 200;        // rows per /api/logbook/events call
const EV_PAGES = 5;         // calls per load before offering "Load earlier"
const LEVEL_CLASS = { ERROR: 'text-danger', WARNING: 'text-warning', INFO: 'text-success', DEBUG: 'text-info' };

/** Fetch the device's logbook lines within the selected Range (fresh, or older ones). */
async function _loadEvents(ieee, earlier = false) {
    const wrap = document.getElementById('hist-events-wrap');
    if (!wrap) return;
    const hours = parseInt(document.getElementById('hist-hours')?.value || '24');
    const since = Date.now() / 1000 - hours * 3600;
    const token = ++_ev.token;
    if (!earlier) {
        _ev.rows = [];
        _ev.more = false;
        wrap.innerHTML = '<div class="text-muted small text-center py-4">Loading…</div>';
    }
    let before = earlier && _ev.rows.length ? _ev.rows[_ev.rows.length - 1].ts : null;
    const got = [];
    let more = false;
    try {
        for (let page = 0; page < EV_PAGES; page++) {
            const res = await fetch(`/api/logbook/events?ieee=${encodeURIComponent(ieee)}&limit=${EV_PAGE}`
                + (before ? `&before=${before}` : ''));
            if (res.status === 401 || res.status === 403) throw new Error('You need system:read access to see events.');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const rows = (await res.json()).events || [];
            const inRange = rows.filter(r => r.ts >= since);
            got.push(...inRange);
            // Done when the hub has no more, or this page already reached past the range.
            if (rows.length < EV_PAGE || inRange.length < rows.length) { more = false; break; }
            before = rows[rows.length - 1].ts;
            more = true;
        }
    } catch (e) {
        if (token !== _ev.token) return;
        wrap.innerHTML = `<div class="text-danger small py-4">Failed to load events: ${escapeHtml(e.message)}</div>`;
        return;
    }
    if (token !== _ev.token) return;   // a newer load (range change, refresh) took over
    const have = new Set(_ev.rows.map(r => r.id));
    _ev.rows = _ev.rows.concat(got.filter(r => !have.has(r.id))).sort((a, b) => b.ts - a.ts);
    _ev.more = more;
    _renderEvents();
}

function _renderEvents() {
    const wrap = document.getElementById('hist-events-wrap');
    if (!wrap || _view !== 'events') return;
    const attr = (document.getElementById('hist-attr')?.value || '').toLowerCase();
    const only = attr && document.getElementById('hist-only-attr')?.checked;
    // Same match as the Debug tab's attribute filter: the line's attribute, or its text.
    const rows = only ? _ev.rows.filter(r => (r.attribute || '').toLowerCase().includes(attr)
        || (r.message || '').toLowerCase().includes(attr)) : _ev.rows;
    document.getElementById('hist-meta').textContent =
        `${rows.length}${_ev.more ? '+' : ''} events${only ? ` · ${attr}` : ''}`;
    const earlier = _ev.more
        ? '<div class="text-center py-2"><button type="button" class="btn btn-sm btn-outline-secondary" data-hist-earlier>Load earlier</button></div>'
        : '';
    if (!rows.length) {
        wrap.innerHTML = `<div class="text-muted small text-center py-4">No events in this range${only ? ' for this attribute' : ''}.</div>${earlier}`;
    } else {
        wrap.innerHTML = `<div class="font-monospace small border rounded" style="max-height:400px;overflow-y:auto">${rows.map(r => {
            const lvl = r.level || 'INFO';
            return `<div class="border-bottom px-2 py-1 d-flex gap-2" data-eid="${escapeHtml(r.id)}" role="button" tabindex="0" title="Trace this event" style="cursor:pointer">`
                + `<span class="text-muted text-nowrap">${escapeHtml(stamp(r.ts))}</span>`
                + `<span class="fw-bold ${LEVEL_CLASS[lvl] || ''}">${escapeHtml(lvl)}</span>`
                + `<span class="flex-grow-1 text-break">${escapeHtml(r.message || '')}</span>`
                + '<i class="fas fa-chevron-right text-muted align-self-center"></i></div>';
        }).join('')}${earlier}</div>`;
    }
    if (!wrap.dataset.wired) {
        wrap.dataset.wired = '1';
        const go = (e) => {
            if (e.target.closest('[data-hist-earlier]')) { _loadEvents(_ev.ieee, true); return; }
            const row = e.target.closest('[data-eid]');
            // Selecting text to copy isn't a click on the line.
            if (row && !String(window.getSelection()).length) openTrace(row.dataset.eid);
        };
        wrap.addEventListener('click', go);
        wrap.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(e); });
    }
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
                <table class="table table-sm table-striped tbl tbl-sortable">
                    <thead><tr><th>Time</th><th>${attr}</th><th class="text-end" data-sort-type="number">Samples</th></tr></thead>
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
                    + (n > 0 ? `<br/><span style="opacity:.7">${n} samples</span>`
                             : `<br/><span style="opacity:.7">carried forward</span>`);
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