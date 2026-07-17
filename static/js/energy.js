/**
 * energy.js
 * Energy tab — Octopus Energy smart-meter data + local smart-plug breakdown.
 *
 * Consumes:
 *   GET /api/octopus/status
 *   GET /api/octopus/summary
 *   GET /api/octopus/consumption?fuel=&range=
 *   GET /api/octopus/rates?fuel=
 *   GET /api/octopus/breakdown?range=
 *
 * Integration:
 *   - `initEnergy()` called from main.js on DOMContentLoaded
 *   - Renders into <div id="energyDashboard">
 *   - Auto-refreshes every 5 min while the #energy tab is visible
 *   - Works with Octopus disabled: shows the plug breakdown (local DuckDB
 *     data) plus a pointer to Settings → APIs → Energy
 */

import { createChart } from './chart-utils.js';

const log = zmmLog('energy');

const REFRESH_MS = 5 * 60_000;

// Fixed hue per fuel (never reassigned): electricity=blue, gas=orange.
// Values match the app-wide chart palette in heating.js (contrast-verified
// against both body backgrounds).
function fuelColours() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    return isDark
        ? { electricity: '#60a5fa', gas: '#fb923c', muted: '#7e97a8' }
        : { electricity: '#1d4ed8', gas: '#c2410c', muted: '#53687a' };
}

// ============================================================================
// STATE
// ============================================================================
let energyTabActive = false;
let refreshTimer = null;
let charts = {};            // name → createChart wrapper
let currentRange = 'week';  // day | week | month
let currentMetric = 'kwh';  // kwh | cost
let consumptionCache = {};  // fuel → series (for metric re-render without refetch)
let ratesCache = null;
let breakdownCache = null;
let octopusEnabled = false;

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function disposeCharts() {
    Object.values(charts).forEach(c => c?.dispose());
    charts = {};
}

document.addEventListener('themechange', () => {
    // chart-utils re-themes axes; series colours from fuelColours() need a redraw
    if (energyTabActive) renderAllCharts();
});

// ============================================================================
// INITIALIZATION
// ============================================================================
export function initEnergy() {
    log.log('Initializing Energy Module…');

    const tabBtn = document.querySelector('button[data-bs-target="#energy"]');
    if (!tabBtn) {
        log.warn('Energy tab button not found');
        return;
    }
    tabBtn.addEventListener('shown.bs.tab', () => {
        energyTabActive = true;
        loadEnergyDashboard();
        startAutoRefresh();
    });
    tabBtn.addEventListener('hidden.bs.tab', () => {
        energyTabActive = false;
        stopAutoRefresh();
        disposeCharts();
    });
    if (tabBtn.classList.contains('active')) {
        energyTabActive = true;
        loadEnergyDashboard();
        startAutoRefresh();
    }
}

function startAutoRefresh() {
    stopAutoRefresh();
    refreshTimer = setInterval(() => {
        if (energyTabActive) loadEnergyDashboard({ silent: true });
    }, REFRESH_MS);
}

function stopAutoRefresh() {
    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
}

// ============================================================================
// DATA LOADING
// ============================================================================
async function fetchJson(url) {
    const res = await fetch(url);
    return res.json();
}

async function loadEnergyDashboard(opts = {}) {
    const root = document.getElementById('energyDashboard');
    if (!root) return;
    try {
        const statusRes = await fetchJson('/api/octopus/status');
        const status = statusRes.status || {};
        octopusEnabled = !!status.enabled;

        const wants = [fetchJson(`/api/octopus/breakdown?range=${currentRange}`)];
        if (octopusEnabled) {
            wants.push(
                fetchJson('/api/octopus/summary'),
                fetchJson(`/api/octopus/consumption?fuel=electricity&range=${currentRange}`),
                fetchJson(`/api/octopus/consumption?fuel=gas&range=${currentRange}`),
                fetchJson('/api/octopus/rates?fuel=electricity'),
            );
        }
        const [breakdown, summary, elec, gas, rates] = await Promise.all(wants);

        breakdownCache = breakdown?.success ? breakdown : null;
        consumptionCache = {
            electricity: elec?.success ? elec : null,
            gas: gas?.success ? gas : null,
        };
        ratesCache = rates?.success ? rates : null;

        disposeCharts();
        root.innerHTML = renderScaffold(status, summary?.success ? summary : null);
        renderAllCharts();
    } catch (e) {
        log.error('Energy dashboard load failed', e);
        if (!opts.silent) {
            root.innerHTML = `
              <div class="alert alert-warning m-3">
                <i class="fas fa-exclamation-triangle me-1"></i>
                Failed to load energy data: ${esc(e.message)}
              </div>`;
        }
    }
}

// ============================================================================
// SCAFFOLD / KPI ROW
// ============================================================================
function renderScaffold(status, summary) {
    const disabledBanner = octopusEnabled ? '' : `
      <div class="alert alert-info d-flex align-items-center gap-2 mb-3">
        <i class="fas fa-plug-circle-bolt fa-lg"></i>
        <div>
          <strong>Octopus Energy is not connected.</strong>
          Grid consumption, tariff rates and costs appear once the integration is enabled
          in <em>Settings → APIs → Energy</em>. Smart-plug readings below are collected locally.
        </div>
      </div>`;

    const errs = Object.entries(status.errors || {});
    const errBanner = (octopusEnabled && errs.length) ? `
      <div class="alert alert-warning py-2 small mb-3">
        <i class="fas fa-exclamation-triangle me-1"></i>
        ${errs.map(([k, v]) => `${esc(k)}: ${esc(v)}`).join(' · ')}
        — showing last stored data.
      </div>` : '';

    const kpis = octopusEnabled ? renderKpiRow(status, summary) : '';

    const octopusCards = octopusEnabled ? `
      <div class="card mb-3">
        <div class="card-header d-flex flex-wrap align-items-center gap-2">
          <span class="fw-semibold"><i class="fas fa-chart-column me-1"></i> Consumption</span>
          <div class="btn-group btn-group-sm ms-auto" role="group" aria-label="Range">
            ${['day', 'week', 'month'].map(r => `
              <button type="button" class="btn btn-outline-secondary ${currentRange === r ? 'active' : ''}"
                      onclick="window.energySetRange('${r}')">${r[0].toUpperCase() + r.slice(1)}</button>`).join('')}
          </div>
          <div class="btn-group btn-group-sm" role="group" aria-label="Metric">
            <button type="button" class="btn btn-outline-secondary ${currentMetric === 'kwh' ? 'active' : ''}"
                    onclick="window.energySetMetric('kwh')">kWh</button>
            <button type="button" class="btn btn-outline-secondary ${currentMetric === 'cost' ? 'active' : ''}"
                    onclick="window.energySetMetric('cost')">£</button>
          </div>
        </div>
        <div class="card-body">
          <div id="energyConsumptionChart" style="height: 320px;"></div>
        </div>
      </div>

      <div class="card mb-3">
        <div class="card-header fw-semibold">
          <i class="fas fa-wave-square me-1"></i> Electricity Unit Rate — today &amp; tomorrow
        </div>
        <div class="card-body">
          <div id="energyRatesChart" style="height: 260px;"></div>
        </div>
      </div>` : '';

    return `
      ${disabledBanner}
      ${errBanner}
      ${kpis}
      ${octopusCards}
      <div class="card mb-3">
        <div class="card-header fw-semibold">
          <i class="fas fa-plug me-1"></i> Where it goes — smart-plug breakdown
          <span class="text-muted small ms-2">(${esc(currentRange)})</span>
        </div>
        <div class="card-body">
          <div id="energyBreakdownChart" style="height: 300px;"></div>
        </div>
      </div>`;
}

function renderKpiRow(status, summary) {
    if (!summary) return '';
    const f = summary.fuels || {};

    const card = (title, icon, body, foot) => `
      <div class="col-6 col-lg-3">
        <div class="card h-100">
          <div class="card-body py-2">
            <div class="text-muted small">${icon} ${title}</div>
            <div class="fs-5 fw-semibold">${body}</div>
            ${foot ? `<div class="text-muted small">${foot}</div>` : ''}
          </div>
        </div>
      </div>`;

    const fuelCard = (fuel, label, icon) => {
        const d = f[fuel] || {};
        const day = d.latest_day;
        if (!day) return card(label, icon, '<span class="text-muted">no data yet</span>', '');
        const cost = day.cost_gbp !== null && day.cost_gbp !== undefined
            ? ` · £${day.cost_gbp.toFixed(2)}` : '';
        return card(label, icon,
            `${day.kwh ?? '—'} kWh${cost}`,
            `${esc(day.date)}${d.latest_data ? ' · meter data to ' + new Date(d.latest_data).toLocaleString() : ''}`);
    };

    const elecRate = f.electricity?.current_unit_rate_p;
    const gasRate = f.gas?.current_unit_rate_p;
    const rateBody = [
        elecRate !== null && elecRate !== undefined ? `⚡ ${elecRate.toFixed(2)}p` : null,
        gasRate !== null && gasRate !== undefined ? `🔥 ${gasRate.toFixed(2)}p` : null,
    ].filter(Boolean).join(' · ') || '<span class="text-muted">—</span>';

    const standing = [
        f.electricity?.standing_charge_p != null ? `⚡ ${f.electricity.standing_charge_p.toFixed(1)}p` : null,
        f.gas?.standing_charge_p != null ? `🔥 ${f.gas.standing_charge_p.toFixed(1)}p` : null,
    ].filter(Boolean).join(' · ') || '—';

    const agile = summary.tomorrow_agile_published
        ? '<span class="badge bg-success">Tomorrow’s rates in</span>'
        : (status.tariffs?.electricity?.is_agile
            ? '<span class="badge bg-secondary">Tomorrow ~16:00</span>' : '');

    return `
      <div class="row g-2 mb-3">
        ${fuelCard('electricity', 'Electricity — latest day', '⚡')}
        ${fuelCard('gas', 'Gas — latest day', '🔥')}
        ${card('Unit rates now', '<i class="fas fa-tag"></i>', rateBody, `standing: ${standing}`)}
        ${card('Tariff', '<i class="fas fa-file-signature"></i>',
            esc(status.tariffs?.electricity?.tariff_code || status.tariffs?.gas?.tariff_code || '—'),
            agile)}
      </div>`;
}

// ============================================================================
// CHARTS
// ============================================================================
function renderAllCharts() {
    if (octopusEnabled) {
        renderConsumptionChart();
        renderRatesChart();
    }
    renderBreakdownChart();
}

function fmtBucketLabel(ts, groupBy) {
    if (groupBy === 'halfhour') {
        return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    // 'YYYY-MM-DD' → short local label
    const d = new Date(ts + 'T00:00:00');
    return d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
}

function renderConsumptionChart() {
    const el = document.getElementById('energyConsumptionChart');
    if (!el) return;
    const colours = fuelColours();
    const elec = consumptionCache.electricity;
    const gas = consumptionCache.gas;
    const groupBy = elec?.group_by || gas?.group_by || 'day';

    // Union of bucket labels so both fuels align on one category axis
    const keys = [...new Set([
        ...(elec?.series || []).map(p => p.ts),
        ...(gas?.series || []).map(p => p.ts),
    ])].sort();
    if (!keys.length) {
        el.innerHTML = '<div class="text-muted text-center py-5">No consumption data yet — smart-meter readings lag by up to a day.</div>';
        return;
    }

    const pick = (resp, key) => (resp?.series || []).find(p => p.ts === key);
    const val = p => p == null ? null
        : (currentMetric === 'kwh' ? p.kwh : p.cost_gbp);

    const mkSeries = (name, resp, colour) => ({
        name,
        type: 'bar',
        barMaxWidth: 26,
        itemStyle: { color: colour, borderRadius: [4, 4, 0, 0] },
        data: keys.map(k => {
            const p = pick(resp, k);
            return { value: val(p), _kwh: p?.kwh, _cost: p?.cost_gbp };
        }),
    });

    charts.consumption?.dispose();
    charts.consumption = createChart(el);
    charts.consumption.setOption({
        grid: { left: 48, right: 16, top: 34, bottom: 42 },
        legend: { top: 0 },
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'shadow' },
            formatter: params => {
                const lines = params.map(p => {
                    const d = p.data || {};
                    const bits = [];
                    if (d._kwh != null) bits.push(`${d._kwh.toFixed(2)} kWh`);
                    if (d._cost != null) bits.push(`£${d._cost.toFixed(2)}`);
                    return `${p.marker} ${p.seriesName}: ${bits.join(' · ') || '—'}`;
                });
                return `<strong>${params[0]?.axisValueLabel ?? ''}</strong><br>${lines.join('<br>')}`;
            },
        },
        xAxis: {
            type: 'category',
            data: keys.map(k => fmtBucketLabel(k, groupBy)),
            axisLabel: { hideOverlap: true },
        },
        yAxis: {
            type: 'value',
            name: currentMetric === 'kwh' ? 'kWh' : '£',
            nameTextStyle: { align: 'left' },
        },
        series: [
            mkSeries('Electricity', elec, colours.electricity),
            mkSeries('Gas', gas, colours.gas),
        ],
    });
}

function renderRatesChart() {
    const el = document.getElementById('energyRatesChart');
    if (!el) return;
    const colours = fuelColours();
    const rates = ratesCache;
    const unitRates = rates?.unit_rates || [];
    if (!unitRates.length) {
        el.innerHTML = '<div class="text-muted text-center py-5">No rate data yet.</div>';
        return;
    }

    // Step line: one point per slot start (+ closing point for the last slot)
    const points = unitRates.map(r => [r.from, r.p_per_kwh]);
    const last = unitRates[unitRates.length - 1];
    if (last?.to) points.push([last.to, last.p_per_kwh]);

    const values = unitRates.map(r => r.p_per_kwh);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const isAgile = unitRates.length > 4;

    charts.rates?.dispose();
    charts.rates = createChart(el);
    charts.rates.setOption({
        grid: { left: 48, right: 16, top: 20, bottom: 42 },
        tooltip: {
            trigger: 'axis',
            formatter: params => {
                const p = params[0];
                if (!p) return '';
                return `${new Date(p.data[0]).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })}`
                    + `<br>${p.marker} ${Number(p.data[1]).toFixed(2)} p/kWh`;
            },
        },
        xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
        yAxis: {
            type: 'value',
            name: 'p/kWh',
            nameTextStyle: { align: 'left' },
            scale: true,
        },
        series: [{
            name: 'Unit rate',
            type: 'line',
            step: 'end',
            symbol: 'none',
            lineStyle: { width: 2, color: colours.electricity },
            itemStyle: { color: colours.electricity },
            data: points,
            markLine: {
                symbol: 'none',
                data: [
                    {
                        xAxis: rates.now,
                        label: { formatter: 'now' },
                        lineStyle: { type: 'solid', width: 2 },
                    },
                    ...(isAgile ? [{
                        yAxis: min,
                        label: { formatter: `min ${min.toFixed(1)}p`, position: 'insideEndTop' },
                        lineStyle: { type: 'dashed', opacity: 0.5 },
                    }, {
                        yAxis: max,
                        label: { formatter: `max ${max.toFixed(1)}p`, position: 'insideEndTop' },
                        lineStyle: { type: 'dashed', opacity: 0.5 },
                    }] : []),
                ],
            },
        }],
    });
}

function renderBreakdownChart() {
    const el = document.getElementById('energyBreakdownChart');
    if (!el) return;
    const colours = fuelColours();
    const b = breakdownCache;
    if (!b || (!b.devices?.length && b.grid_kwh == null)) {
        el.innerHTML = '<div class="text-muted text-center py-5">No smart-plug energy readings in this range.</div>';
        return;
    }

    // Top devices + "Other plugs" + "Rest of home" (grid minus all plugs).
    const MAX_BARS = 8;
    const devices = b.devices || [];
    const top = devices.slice(0, MAX_BARS);
    const otherKwh = devices.slice(MAX_BARS).reduce((s, d) => s + d.kwh, 0);

    const rows = top.map(d => ({ name: d.name, kwh: d.kwh, colour: colours.electricity }));
    if (otherKwh > 0) rows.push({ name: `Other plugs (${devices.length - MAX_BARS})`, kwh: otherKwh, colour: colours.electricity });
    if (b.unmetered_kwh != null && b.unmetered_kwh > 0) {
        rows.push({ name: 'Rest of home (grid − plugs)', kwh: b.unmetered_kwh, colour: colours.muted });
    }
    rows.reverse(); // horizontal bars read bottom-up

    charts.breakdown?.dispose();
    charts.breakdown = createChart(el);
    charts.breakdown.setOption({
        grid: { left: 8, right: 60, top: 8, bottom: 28, containLabel: true },
        tooltip: {
            trigger: 'item',
            formatter: p => `${esc(p.name)}: ${Number(p.value).toFixed(2)} kWh`,
        },
        xAxis: { type: 'value', name: 'kWh' },
        yAxis: {
            type: 'category',
            data: rows.map(r => r.name),
            axisLabel: { width: 180, overflow: 'truncate' },
        },
        series: [{
            type: 'bar',
            barMaxWidth: 20,
            data: rows.map(r => ({
                value: Math.round(r.kwh * 100) / 100,
                itemStyle: { color: r.colour, borderRadius: [0, 4, 4, 0] },
            })),
            label: {
                show: true,
                position: 'right',
                formatter: p => `${Number(p.value).toFixed(1)}`,
            },
        }],
    });
}

// ============================================================================
// CONTROLS (inline onclick handlers)
// ============================================================================
window.energySetRange = function(range) {
    if (!['day', 'week', 'month'].includes(range) || range === currentRange) return;
    currentRange = range;
    loadEnergyDashboard();
};

window.energySetMetric = function(metric) {
    if (!['kwh', 'cost'].includes(metric) || metric === currentMetric) return;
    currentMetric = metric;
    // Toggle button states without a full reload
    document.querySelectorAll('[aria-label="Metric"] .btn').forEach(btn => {
        btn.classList.toggle('active',
            (metric === 'kwh') === (btn.textContent.trim() === 'kWh'));
    });
    renderConsumptionChart();
};
