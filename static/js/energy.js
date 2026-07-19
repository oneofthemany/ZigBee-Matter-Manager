/**
 * energy.js
 * Energy tab — Octopus Energy smart-meter data + local smart-plug breakdown.
 *
 * Consumes:
 *   GET /api/octopus/status
 *   GET /api/octopus/summary
 *   GET /api/octopus/consumption?fuel=&range=
 *   GET /api/octopus/rates?fuel=
 *   GET /api/octopus/insights
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

// Categorical palette for per-socket series — same fixed order as the
// heating charts; assigned by rank once per load, never re-cycled.
function socketPalette() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    return isDark
        ? ['#60a5fa', '#4ade80', '#fbbf24', '#a78bfa', '#2dd4bf', '#f472b6']
        : ['#1d4ed8', '#15803d', '#b45309', '#6d28d9', '#0e7490', '#be185d'];
}

// ============================================================================
// STATE
// ============================================================================
let energyTabActive = false;
let refreshTimer = null;
let charts = {};            // name → createChart wrapper
let currentRange = 'week';  // day | week | month
let currentMetric = 'kwh';  // kwh | cost
let customDates = null;     // {from, to} 'YYYY-MM-DD' — calendar search mode
let breakdownView = 'daily'; // daily (stacked) | totals
let consumptionCache = {};  // fuel → series (for metric re-render without refetch)
let ratesCache = null;
let breakdownCache = null;
let telemetryCache = null;  // Home Mini live demand samples
let insightsCache = null;   // percentiles / trend / rate position / recommendations
let octopusEnabled = false;

// Price-band colours (diverging: cheap=blue pole, typical=neutral midpoint,
// peak=warm pole). Validated for CVD separation + contrast against both card
// surfaces (dataviz six-checks); band names always appear as text too.
function bandColours() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    return isDark
        ? { cheap: '#3b82f6', typical: '#64748b', peak: '#ea580c' }
        : { cheap: '#2563eb', typical: '#64748b', peak: '#ea580c' };
}

// Band thresholds over the displayed rate window (terciles): cheap ≤ lo,
// peak ≥ hi. Shared by the header chips and the chart so they never disagree.
function rateBands() {
    const vals = (ratesCache?.unit_rates || []).map(r => r.p_per_kwh)
        .filter(v => v != null).sort((a, b) => a - b);
    if (vals.length < 6) return null;
    const q = p => vals[Math.min(vals.length - 1, Math.floor(vals.length * p))];
    return { lo: q(1 / 3), hi: q(2 / 3) };
}

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
// Never lets a single slow/failed endpoint pin the tab on the loading
// placeholder: bounded by a timeout, resolves null on any failure, and the
// callers all degrade per-card on null.
async function fetchJson(url, timeoutMs = 20_000) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
        const res = await fetch(url, { signal: ctrl.signal });
        return await res.json();
    } catch (e) {
        log.warn(`fetch failed: ${url}`, e);
        return null;
    } finally {
        clearTimeout(timer);
    }
}

async function loadEnergyDashboard(opts = {}) {
    const root = document.getElementById('energyDashboard');
    if (!root) return;
    try {
        const statusRes = await fetchJson('/api/octopus/status');
        if (!statusRes) {
            // App unreachable or still starting (common right after a
            // reboot) — say so and retry rather than sit on the placeholder.
            root.innerHTML = `
              <div class="alert alert-warning m-3">
                <i class="fas fa-hourglass-half me-1"></i>
                Energy data is not responding yet — the app may still be
                starting up. Retrying automatically…
              </div>`;
            setTimeout(() => {
                if (energyTabActive) loadEnergyDashboard({ silent: true });
            }, 10_000);
            return;
        }
        const status = statusRes.status || {};
        octopusEnabled = !!status.enabled;

        const consQuery = customDates
            ? `date_from=${customDates.from}&date_to=${customDates.to}`
            : `range=${currentRange}`;
        const wants = [fetchJson(`/api/octopus/breakdown?range=${currentRange}`)];
        if (octopusEnabled) {
            wants.push(
                fetchJson('/api/octopus/summary'),
                fetchJson(`/api/octopus/consumption?fuel=electricity&${consQuery}`),
                fetchJson(`/api/octopus/consumption?fuel=gas&${consQuery}`),
                fetchJson('/api/octopus/rates?fuel=electricity'),
                fetchJson('/api/octopus/telemetry'),
                fetchJson('/api/octopus/insights'),
            );
        }
        const [breakdown, summary, elec, gas, rates, telemetry, insights] = await Promise.all(wants);

        breakdownCache = breakdown?.success ? breakdown : null;
        consumptionCache = {
            electricity: elec?.success ? elec : null,
            gas: gas?.success ? gas : null,
        };
        ratesCache = rates?.success ? rates : null;
        telemetryCache = telemetry?.success && telemetry.enabled ? telemetry : null;
        insightsCache = insights?.success ? insights : null;

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
        <i class="fas fa-plug fa-lg"></i>
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

    const latestW = telemetryCache?.latest?.demand_w;
    const liveCard = (octopusEnabled && telemetryCache) ? `
      <div class="card mb-3">
        <div class="card-header d-flex flex-wrap align-items-center gap-2">
          <span class="fw-semibold"><i class="fas fa-gauge-high me-1"></i> Live demand — Home Mini</span>
          ${latestW != null ? `<span class="badge bg-success ms-1">${Math.round(latestW)} W now</span>` : ''}
          <span class="text-muted small ms-auto">sampled every ${telemetryCache.poll_minutes} min</span>
        </div>
        <div class="card-body">
          ${telemetryCache.series?.length
            ? '<div id="energyLiveChart" style="height: 220px;"></div>'
            : `<div class="text-muted text-center py-4">Waiting for the first Home Mini sample${telemetryCache.error ? ` — ${esc(telemetryCache.error)}` : ' (up to a few minutes after startup)'}…</div>`}
        </div>
      </div>` : '';

    const octopusCards = octopusEnabled ? `
      ${liveCard}
      <div class="card mb-3">
        <div class="card-header d-flex flex-wrap align-items-center gap-2">
          <span class="fw-semibold"><i class="fas fa-chart-column me-1"></i> Consumption</span>
          <div class="btn-group btn-group-sm ms-auto" role="group" aria-label="Range">
            ${['day', 'week', 'month'].map(r => `
              <button type="button" class="btn btn-outline-secondary ${!customDates && currentRange === r ? 'active' : ''}"
                      onclick="window.energySetRange('${r}')">${r[0].toUpperCase() + r.slice(1)}</button>`).join('')}
          </div>
          <div class="input-group input-group-sm" style="width: auto;" aria-label="Calendar search">
            <input type="date" class="form-control" id="energyDateFrom" value="${customDates?.from ?? ''}" title="From">
            <input type="date" class="form-control" id="energyDateTo" value="${customDates?.to ?? ''}" title="To">
            <button class="btn btn-outline-secondary ${customDates ? 'active' : ''}" title="Show this date range"
                    onclick="window.energyApplyDates()"><i class="fas fa-calendar-day"></i></button>
            ${customDates ? `<button class="btn btn-outline-secondary" title="Back to rolling ranges"
                    onclick="window.energyClearDates()"><i class="fas fa-times"></i></button>` : ''}
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

      ${renderRatesCard(status, summary)}
      ${renderInsightsCard()}` : '';

    return `
      ${disabledBanner}
      ${errBanner}
      ${kpis}
      ${octopusCards}
      <div class="card mb-3">
        <div class="card-header d-flex flex-wrap align-items-center gap-2">
          <span class="fw-semibold"><i class="fas fa-plug me-1"></i> Where it goes — energy-reporting sockets</span>
          <span class="text-muted small">(${esc(currentRange)})</span>
          <div class="btn-group btn-group-sm ms-auto" role="group" aria-label="Breakdown view">
            <button type="button" class="btn btn-outline-secondary ${breakdownView === 'daily' ? 'active' : ''}"
                    onclick="window.energySetBreakdownView('daily')">Daily stack</button>
            <button type="button" class="btn btn-outline-secondary ${breakdownView === 'totals' ? 'active' : ''}"
                    onclick="window.energySetBreakdownView('totals')">Totals</button>
          </div>
        </div>
        <div class="card-body">
          <div id="energyBreakdownChart" style="height: 300px;"></div>
          ${renderSocketsTable()}
        </div>
      </div>
      ${renderTipsCard()}`;
}

function renderKpiRow(status, summary) {
    if (!summary) return '';
    const f = summary.fuels || {};
    const colours = fuelColours();

    const card = (title, icon, body, foot, accent) => `
      <div class="col-6 col-lg-3">
        <div class="card h-100"${accent ? ` style="border-left:3px solid ${accent}"` : ''}>
          <div class="card-body py-2">
            <div class="text-muted small">${icon} ${title}</div>
            <div class="fs-5 fw-semibold" style="font-variant-numeric: tabular-nums">${body}</div>
            ${foot ? `<div class="text-muted small">${foot}</div>` : ''}
          </div>
        </div>
      </div>`;

    const fuelCard = (fuel, label, icon) => {
        const d = f[fuel] || {};
        const day = d.latest_day;
        if (!day) return card(label, icon, '<span class="text-muted">no data yet</span>', '', colours[fuel]);
        const cost = day.cost_gbp !== null && day.cost_gbp !== undefined
            ? ` · £${day.cost_gbp.toFixed(2)}` : '';
        return card(label, icon,
            `${day.kwh ?? '—'} kWh${cost}`,
            `${esc(day.date)}${d.latest_data ? ' · meter data to ' + new Date(d.latest_data).toLocaleString() : ''}`,
            colours[fuel]);
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

function ordinal(n) {
    const s = ['th', 'st', 'nd', 'rd'][(n % 100 > 10 && n % 100 < 14) ? 0 : Math.min(n % 10, 4)] || 'th';
    return `${n}${s}`;
}

const swatch = colour =>
    `<span style="width:10px;height:10px;border-radius:3px;background:${colour};display:inline-block;flex-shrink:0"></span>`;

/**
 * Unit-rate card, adaptive to the tariff's shape:
 *  - flat tariff → one text line (a 260px chart of a flat line says nothing)
 *  - varying tariff (Agile/E7) → now→next strip + half-hourly bars coloured
 *    by price band (terciles of the visible window)
 */
function renderRatesCard(status, summary) {
    const unitRates = ratesCache?.unit_rates || [];
    if (!unitRates.length) return '';

    const rc = insightsCache?.rate_context;
    const distinct = new Set(unitRates.map(r => r.p_per_kwh)).size;
    const isAgile = rc ? rc.is_agile : distinct > 4;
    const standing = ratesCache?.standing_charge_p;

    const fmtT = iso => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const next = rc?.next_change;
    const strip = rc?.current_p != null ? `
        <span class="fs-5 fw-semibold">${rc.current_p.toFixed(2)}p</span><span class="text-muted small">/kWh now</span>
        ${next ? `<span class="text-muted small"><i class="fas fa-arrow-right-long mx-1"></i>${next.p.toFixed(2)}p at ${fmtT(next.at)}</span>` : ''}` : '';

    if (!isAgile) {
        const rate = rc?.current_p ?? unitRates[unitRates.length - 1]?.p_per_kwh;
        return `
      <div class="card mb-3">
        <div class="card-body py-2 d-flex flex-wrap align-items-baseline gap-2">
          <span class="text-muted small"><i class="fas fa-tag me-1"></i> Electricity unit rate</span>
          ${strip || `<span class="fs-5 fw-semibold">${rate != null ? rate.toFixed(2) : '—'}p</span><span class="text-muted small">/kWh</span>`}
          ${!next && distinct === 1 ? '<span class="text-muted small">· same price all day</span>' : ''}
          ${standing != null ? `<span class="text-muted small ms-auto">standing ${standing.toFixed(1)}p/day</span>` : ''}
        </div>
      </div>`;
    }

    const bands = rateBands();
    const bc = bandColours();
    const chip = (colour, label) =>
        `<span class="d-inline-flex align-items-center gap-1 small text-muted">${swatch(colour)}${label}</span>`;
    const agileBadge = summary?.tomorrow_agile_published
        ? '<span class="badge bg-success">Tomorrow’s rates in</span>'
        : '<span class="badge bg-secondary">Tomorrow ~16:00</span>';

    return `
      <div class="card mb-3">
        <div class="card-header d-flex flex-wrap align-items-center gap-2">
          <span class="fw-semibold"><i class="fas fa-wave-square me-1"></i> Electricity Unit Rate — today &amp; tomorrow</span>
          ${agileBadge}
          <span class="ms-auto d-flex align-items-baseline gap-1">${strip}</span>
        </div>
        <div class="card-body">
          ${bands ? `
          <div class="d-flex flex-wrap align-items-center gap-3 mb-1">
            ${chip(bc.cheap, `cheap ≤ ${bands.lo.toFixed(1)}p`)}
            ${chip(bc.typical, 'typical')}
            ${chip(bc.peak, `peak ≥ ${bands.hi.toFixed(1)}p`)}
            ${rc?.percentile_today != null
                ? `<span class="small text-muted ms-auto">now is cheaper than ${100 - rc.percentile_today}% of today's slots</span>` : ''}
          </div>` : ''}
          <div id="energyRatesChart" style="height: 240px;"></div>
        </div>
      </div>`;
}

/**
 * Analysis card: where each fuel's latest full day sits in its own 30-day
 * distribution (p10–p90 band + median + latest marker), weekly trend, base
 * load / Agile-timing chips, and the server's rule-based recommendations.
 */
function renderInsightsCard() {
    const ins = insightsCache;
    if (!ins) return '';
    const elecS = ins.fuels?.electricity;
    const gasS = ins.fuels?.gas;
    const recs = ins.recommendations || [];
    if (!elecS && !gasS && !recs.length) return '';

    const colours = fuelColours();
    const fuelLine = (s, label, colour) => {
        if (!s) return '';
        const ld = s.latest_day;
        const trend = s.week_trend_pct;
        return `
          <div class="d-flex align-items-center flex-wrap gap-2">
            ${swatch(colour)}
            <span class="fw-semibold small">${label}</span>
            <span class="small">${ld.kwh} kWh on ${esc(ld.date)}${ld.cost_gbp != null ? ` · £${ld.cost_gbp.toFixed(2)}` : ''}</span>
            <span class="small text-muted">— ${ordinal(ld.percentile)} percentile of your last ${s.days_analysed} days</span>
            ${trend != null ? `<span class="small text-muted"><i class="fas fa-arrow-trend-${trend >= 0 ? 'up' : 'down'} me-1"></i>${trend >= 0 ? '+' : ''}${trend}% wk-on-wk</span>` : ''}
          </div>`;
    };

    const bl = ins.base_load;
    const timing = ins.timing;
    const chips = [
        bl ? `<span class="small text-muted"><i class="fas fa-moon me-1"></i>base load ~${bl.w} W (${bl.kwh_day} kWh/day${bl.cost_month_gbp != null ? ` · £${bl.cost_month_gbp.toFixed(0)}/mo` : ''})</span>` : '',
        timing?.saving_pct != null
            ? `<span class="small text-muted"><i class="fas fa-clock me-1"></i>Agile timing ${timing.saving_pct >= 0 ? 'saves' : 'costs'} you ${Math.abs(timing.saving_pct)}% vs flat usage</span>` : '',
    ].filter(Boolean);

    const recsHtml = recs.length ? `
        <div class="mt-2 pt-2 border-top">
          ${recs.map(t => `
            <div class="d-flex gap-2 py-2">
              <i class="fas fa-${esc(t.icon || 'lightbulb')} mt-1 text-warning"></i>
              <div>
                <div class="fw-semibold small">${esc(t.title)}</div>
                <div class="text-muted small">${esc(t.detail)}</div>
              </div>
            </div>`).join('')}
        </div>` : '';

    return `
      <div class="card mb-3">
        <div class="card-header fw-semibold">
          <i class="fas fa-magnifying-glass-chart me-1"></i> Analysis — your last 30 days
        </div>
        <div class="card-body">
          <div class="d-flex flex-column gap-1 mb-2">
            ${fuelLine(elecS, 'Electricity', colours.electricity)}
            ${fuelLine(gasS, 'Gas', colours.gas)}
          </div>
          ${(elecS || gasS) ? `
          <div id="energyInsightsChart" style="height: ${elecS && gasS ? 120 : 90}px;"></div>
          <div class="text-muted small">Shaded band = middle 80% of your daily usage · <strong>|</strong> median · <strong>●</strong> latest full day</div>` : ''}
          ${chips.length ? `<div class="d-flex flex-wrap gap-3 mt-2">${chips.join('')}</div>` : ''}
          ${recsHtml}
        </div>
      </div>`;
}

// ============================================================================
// CHARTS
// ============================================================================
function renderAllCharts() {
    if (octopusEnabled) {
        renderLiveChart();
        renderConsumptionChart();
        renderRatesChart();
        renderInsightsChart();
    }
    renderBreakdownChart();
}

function renderLiveChart() {
    const el = document.getElementById('energyLiveChart');
    if (!el || !telemetryCache?.series?.length) return;
    const colours = fuelColours();
    const points = telemetryCache.series
        .filter(s => s.demand_w != null)
        .map(s => [s.ts, Math.round(s.demand_w)]);
    if (!points.length) return;

    charts.live?.dispose();
    charts.live = createChart(el);
    charts.live.setOption({
        grid: { left: 56, right: 16, top: 20, bottom: 32 },
        tooltip: {
            trigger: 'axis',
            formatter: params => {
                const p = params[0];
                if (!p) return '';
                return `${new Date(p.data[0]).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
                    + `<br>${p.marker} ${p.data[1]} W`;
            },
        },
        xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
        yAxis: {
            type: 'value',
            name: 'W',
            nameTextStyle: { align: 'left' },
            min: 0,
        },
        series: [{
            name: 'Demand',
            type: 'line',
            symbol: 'none',
            smooth: 0.2,
            lineStyle: { width: 2, color: colours.electricity },
            itemStyle: { color: colours.electricity },
            areaStyle: { opacity: 0.12, color: colours.electricity },
            data: points,
        }],
    });
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
    // Only exists in Agile mode — the flat-tariff card renders no chart div.
    const el = document.getElementById('energyRatesChart');
    if (!el) return;
    const unitRates = ratesCache?.unit_rates || [];
    const bands = rateBands();
    if (!unitRates.length || !bands) {
        el.innerHTML = '<div class="text-muted text-center py-5">No rate data yet.</div>';
        return;
    }

    const bc = bandColours();
    const bandOf = p => p <= bands.lo ? 'cheap' : (p >= bands.hi ? 'peak' : 'typical');
    const nowMs = new Date(ratesCache.now).getTime();
    const nowIdx = unitRates.findIndex(r =>
        new Date(r.from).getTime() <= nowMs && (!r.to || nowMs < new Date(r.to).getTime()));

    charts.rates?.dispose();
    charts.rates = createChart(el);
    charts.rates.setOption({
        grid: { left: 48, right: 16, top: 30, bottom: 32 },
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'shadow' },
            formatter: params => {
                const p = params[0];
                const r = p ? unitRates[p.dataIndex] : null;
                if (!r) return '';
                const t0 = new Date(r.from);
                const t1 = r.to ? new Date(r.to) : null;
                return `${t0.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })}`
                    + `${t1 ? '–' + t1.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''}`
                    + `<br>${p.marker} ${r.p_per_kwh.toFixed(2)} p/kWh · ${bandOf(r.p_per_kwh)}`;
            },
        },
        xAxis: {
            type: 'category',
            data: unitRates.map(r => r.from),
            axisTick: { show: false },
            axisLabel: {
                // Label every 6h; midnight becomes the day name so the
                // today/tomorrow boundary is obvious without a legend.
                interval: i => {
                    const d = new Date(unitRates[i].from);
                    return d.getMinutes() === 0 && d.getHours() % 6 === 0;
                },
                formatter: iso => {
                    const d = new Date(iso);
                    return d.getHours() === 0
                        ? d.toLocaleDateString([], { weekday: 'short' })
                        : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                },
            },
        },
        yAxis: {
            type: 'value',
            name: 'p/kWh',
            nameTextStyle: { align: 'left' },
        },
        series: [{
            name: 'Unit rate',
            type: 'bar',
            barCategoryGap: '25%',
            data: unitRates.map(r => ({
                value: r.p_per_kwh,
                itemStyle: {
                    color: bc[bandOf(r.p_per_kwh)],
                    borderRadius: r.p_per_kwh >= 0 ? [2, 2, 0, 0] : [0, 0, 2, 2],
                },
            })),
            markLine: {
                symbol: 'none',
                data: nowIdx >= 0 ? [{
                    xAxis: nowIdx,
                    label: { formatter: 'now' },
                    lineStyle: { type: 'solid', width: 2 },
                }] : [],
            },
        }],
    });
}

function renderInsightsChart() {
    const el = document.getElementById('energyInsightsChart');
    if (!el || !insightsCache) return;
    const colours = fuelColours();
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    const surface = isDark ? '#101e2b' : '#ffffff';

    const rows = [];
    for (const [fuel, label] of [['electricity', 'Electricity'], ['gas', 'Gas']]) {
        const s = insightsCache.fuels?.[fuel];
        if (s) rows.push({ label, s, colour: colours[fuel] });
    }
    if (!rows.length) return;

    const fmt = p => {
        const r = rows[p.dataIndex ?? (Array.isArray(p.value) ? p.value[1] : 0)];
        if (!r) return '';
        return `<strong>${r.label}</strong> — daily kWh, last ${r.s.days_analysed} days`
            + `<br>p10 ${r.s.p10} · median ${r.s.p50} · p90 ${r.s.p90}`
            + `<br>● latest full day: ${r.s.latest_day.kwh} (${ordinal(r.s.latest_day.percentile)} pct)`;
    };

    charts.insights?.dispose();
    charts.insights = createChart(el);
    charts.insights.setOption({
        grid: { left: 8, right: 64, top: 4, bottom: 22, containLabel: true },
        tooltip: { trigger: 'item', formatter: fmt },
        xAxis: { type: 'value', name: 'kWh/day', nameGap: 8 },
        yAxis: { type: 'category', data: rows.map(r => r.label), axisTick: { show: false } },
        series: [
            {   // invisible offset so the band starts at p10
                type: 'bar', stack: 'band', barWidth: 14, silent: true,
                itemStyle: { color: 'transparent' },
                data: rows.map(r => r.s.p10),
            },
            {   // p10–p90 band
                type: 'bar', stack: 'band', barWidth: 14,
                data: rows.map(r => ({
                    value: Math.max(0, +(r.s.p90 - r.s.p10).toFixed(2)),
                    itemStyle: { color: r.colour, opacity: 0.25, borderRadius: 4 },
                })),
            },
            {   // median tick
                type: 'scatter', symbol: 'rect', symbolSize: [3, 20],
                data: rows.map((r, i) => ({
                    value: [r.s.p50, i],
                    itemStyle: { color: r.colour },
                })),
            },
            {   // latest full day, with a surface ring so it reads over the band
                type: 'scatter', symbolSize: 11,
                data: rows.map((r, i) => ({
                    value: [r.s.latest_day.kwh, i],
                    itemStyle: { color: r.colour, borderColor: surface, borderWidth: 2 },
                })),
            },
        ],
    });
}

function renderSocketsTable() {
    const b = breakdownCache;
    const sockets = b?.sockets || [];
    if (!sockets.length) return '';
    const hasCost = sockets.some(s => s.cost_gbp != null);
    const rows = [...sockets]
        .sort((a, z) => (z.cost_gbp ?? z.kwh) - (a.cost_gbp ?? a.kwh))
        .map(s => `
          <tr>
            <td>${esc(s.name)}</td>
            <td class="text-end">${s.power_w != null ? Math.round(s.power_w) + ' W' : '<span class="text-muted">—</span>'}</td>
            <td class="text-end">${s.kwh.toFixed(2)}</td>
            ${hasCost ? `<td class="text-end">${s.cost_gbp != null ? '£' + s.cost_gbp.toFixed(2) : '—'}</td>` : ''}
          </tr>`).join('');
    return `
      <div class="table-responsive mt-3">
        <table class="table table-sm small align-middle mb-0">
          <thead><tr>
            <th>Socket</th><th class="text-end">Now</th>
            <th class="text-end">kWh (${esc(currentRange)})</th>
            ${hasCost ? '<th class="text-end">Est. cost</th>' : ''}
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
        ${hasCost ? `<div class="text-muted small mt-1">Costs estimated at the current unit rate (${breakdownCache.rate_p?.toFixed(2)}p/kWh), sorted most-expensive first.</div>` : ''}
      </div>`;
}

function renderTipsCard() {
    const tips = breakdownCache?.tips || [];
    if (!tips.length) return '';
    return `
      <div class="card mb-3">
        <div class="card-header fw-semibold"><i class="fas fa-lightbulb me-1"></i> Saving opportunities</div>
        <div class="card-body py-2">
          ${tips.map(t => `
            <div class="d-flex gap-2 py-2 border-bottom-0">
              <i class="fas fa-${esc(t.icon || 'lightbulb')} mt-1 text-warning"></i>
              <div>
                <div class="fw-semibold small">${esc(t.title)}</div>
                <div class="text-muted small">${esc(t.detail)}</div>
              </div>
            </div>`).join('')}
        </div>
      </div>`;
}

function renderBreakdownChart() {
    const el = document.getElementById('energyBreakdownChart');
    if (!el) return;
    const b = breakdownCache;
    if (!b || (!b.sockets?.length && b.grid_kwh == null)) {
        el.innerHTML = '<div class="text-muted text-center py-5">No socket energy readings in this range.</div>';
        return;
    }
    if (breakdownView === 'daily' && b.series?.days?.length) {
        renderBreakdownDaily(el, b);
    } else {
        renderBreakdownTotals(el, b);
    }
}

function renderBreakdownTotals(el, b) {
    const colours = fuelColours();
    const sockets = (b.sockets || []).filter(s => s.kwh > 0);
    const MAX_BARS = 8;
    const top = sockets.slice(0, MAX_BARS);
    const otherKwh = sockets.slice(MAX_BARS).reduce((s, d) => s + d.kwh, 0);

    const rows = top.map(d => ({ name: d.name, kwh: d.kwh, colour: colours.electricity }));
    if (otherKwh > 0) rows.push({ name: `Other sockets (${sockets.length - MAX_BARS})`, kwh: otherKwh, colour: colours.electricity });
    if (b.unmetered_kwh != null && b.unmetered_kwh > 0) {
        rows.push({ name: 'Rest of home (grid − sockets)', kwh: b.unmetered_kwh, colour: colours.muted });
    }
    rows.reverse(); // horizontal bars read bottom-up

    charts.breakdown?.dispose();
    charts.breakdown = createChart(el);
    charts.breakdown.setOption({
        grid: { left: 8, right: 60, top: 8, bottom: 28, containLabel: true },
        tooltip: {
            trigger: 'item',
            formatter: p => {
                const cost = b.rate_p != null
                    ? ` · £${(p.value * b.rate_p / 100).toFixed(2)}` : '';
                return `${esc(p.name)}: ${Number(p.value).toFixed(2)} kWh${cost}`;
            },
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

function renderBreakdownDaily(el, b) {
    const palette = socketPalette();
    const muted = fuelColours().muted;
    const days = b.series.days;
    const labels = days.map(d => fmtBucketLabel(d, 'day'));

    const mkStack = (name, data, colour) => ({
        name,
        type: 'bar',
        stack: 'home',
        barMaxWidth: 30,
        itemStyle: { color: colour, borderWidth: 1, borderColor: 'transparent' },
        data,
    });
    const series = b.series.stacks.map((s, i) =>
        mkStack(s.name, s.kwh, palette[i % palette.length]));
    if (b.series.rest.some(v => v != null && v > 0)) {
        series.push(mkStack('Rest of home', b.series.rest, muted));
    }

    charts.breakdown?.dispose();
    charts.breakdown = createChart(el);
    charts.breakdown.setOption({
        grid: { left: 48, right: 16, top: 34, bottom: 42 },
        legend: { top: 0, type: 'scroll' },
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'shadow' },
            formatter: params => {
                const lines = params
                    .filter(p => p.value != null && p.value > 0)
                    .map(p => {
                        const cost = b.rate_p != null
                            ? ` · £${(p.value * b.rate_p / 100).toFixed(2)}` : '';
                        return `${p.marker} ${esc(p.seriesName)}: ${Number(p.value).toFixed(2)} kWh${cost}`;
                    });
                const total = params.reduce((s, p) => s + (p.value || 0), 0);
                return `<strong>${params[0]?.axisValueLabel ?? ''}</strong> — ${total.toFixed(2)} kWh total<br>${lines.join('<br>')}`;
            },
        },
        xAxis: { type: 'category', data: labels, axisLabel: { hideOverlap: true } },
        yAxis: { type: 'value', name: 'kWh', nameTextStyle: { align: 'left' } },
        series,
    });
}

// ============================================================================
// CONTROLS (inline onclick handlers)
// ============================================================================
window.energySetRange = function(range) {
    if (!['day', 'week', 'month'].includes(range)) return;
    if (range === currentRange && !customDates) return;
    currentRange = range;
    customDates = null;
    loadEnergyDashboard();
};

window.energyApplyDates = function() {
    const from = document.getElementById('energyDateFrom')?.value;
    const to = document.getElementById('energyDateTo')?.value || from;
    if (!from) return;
    customDates = from <= to ? { from, to } : { from: to, to: from };
    loadEnergyDashboard();
};

window.energyClearDates = function() {
    customDates = null;
    loadEnergyDashboard();
};

window.energySetBreakdownView = function(view) {
    if (!['daily', 'totals'].includes(view) || view === breakdownView) return;
    breakdownView = view;
    document.querySelectorAll('[aria-label="Breakdown view"] .btn').forEach(btn => {
        btn.classList.toggle('active',
            (view === 'daily') === (btn.textContent.trim() === 'Daily stack'));
    });
    renderBreakdownChart();
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
