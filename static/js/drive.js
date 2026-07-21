/* ============================================================
   ZMM — Drive tab: journeys + cheapest fuel nearby + price history
   ============================================================
   Three pieces, one page:
     - Journeys: trips recorded by the companion app's drive mode
       (car Bluetooth), with distance and speed statistics.
     - Fuel: cheapest stations near home or a typed postcode, by
       fuel type, with a Google Maps link per station.
     - Price history chart: the snapshots fuel_history.py records at
       every search, drawn as a daily median line over a min–max band.

   An ES module (unlike presence-settings.js) because the chart goes
   through the shared chart-utils/ECharts layer; still exposes
   window.initDriveTab for main.js's tab listener.
   ============================================================ */

import { createChart } from './chart-utils.js';

(function () {
    'use strict';

    var HOST_ID = 'drive-host';

    // UK display units; storage is metric (m, m/s).
    var MI = 1609.344;
    function mph(mps)  { return mps == null ? null : mps * 2.23694; }
    function miles(m)  { return m == null ? null : m / MI; }

    var log = (window.zmmLog && window.zmmLog('drive')) || console;

    var trips = [];
    var stats = null;
    var fuelTypes = { E10: 'Petrol (E10)', E5: 'Premium petrol (E5)',
                      B7: 'Diesel (B7)', SDV: 'Super diesel (SDV)' };
    var fuelPrefsKey = 'zbm-drive-fuel-prefs';

    // ----------------------------------------------------------
    // Helpers
    // ----------------------------------------------------------
    function escape(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function fmtMiles(m) {
        var mi = miles(m);
        return mi == null ? '—' : mi.toFixed(1) + ' mi';
    }

    function fmtMph(mps) {
        var v = mph(mps);
        return v == null ? '—' : Math.round(v) + ' mph';
    }

    function fmtDuration(s) {
        if (s == null) return '—';
        var mins = Math.round(s / 60);
        if (mins < 60) return mins + ' min';
        return Math.floor(mins / 60) + ' h ' + (mins % 60) + ' min';
    }

    function fmtWhen(ts) {
        if (!ts) return '—';
        var d = new Date(ts * 1000);
        // Weekday only from sm up: at 390 px the full form wraps the When
        // cell to three lines and doubles every row's height.
        return '<span class="d-none d-sm-inline">' +
               d.toLocaleDateString(undefined, { weekday: 'short' }) + ' </span>' +
               '<span class="text-nowrap">' +
               d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) +
               '</span> <span class="text-nowrap">' +
               d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }) +
               '</span>';
    }

    function placeName(p) {
        if (!p || p === 'away') return 'Away';
        if (p === 'home') return 'Home';
        return p.replace(/[_-]+/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
    }

    function getFuelPrefs() {
        var base = { fuel: 'E10', postcode: '', radius: 8, historyDays: 30 };
        try {
            var raw = localStorage.getItem(fuelPrefsKey);
            if (raw) return Object.assign(base, JSON.parse(raw));
        } catch (e) {}
        return base;
    }

    function saveFuelPrefs(p) {
        try { localStorage.setItem(fuelPrefsKey, JSON.stringify(p)); } catch (e) {}
    }

    // ----------------------------------------------------------
    // Data
    // ----------------------------------------------------------
    async function fetchJourneys() {
        try {
            var r = await fetch('/api/journeys?limit=100', { credentials: 'same-origin' });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            trips = (await r.json()).trips || [];
        } catch (e) {
            log.warn('journeys fetch failed', e);
            trips = [];
        }
        try {
            var r2 = await fetch('/api/journeys/stats', { credentials: 'same-origin' });
            if (r2.ok) stats = await r2.json();
        } catch (e) { stats = null; }
    }

    async function fetchFuelTypes() {
        try {
            var r = await fetch('/api/fuel/types', { credentials: 'same-origin' });
            if (r.ok) fuelTypes = (await r.json()).fuel_types || fuelTypes;
        } catch (e) { /* keep defaults */ }
    }

    // ----------------------------------------------------------
    // Render — page skeleton
    // ----------------------------------------------------------
    // Which sub-tab is open; survives re-renders (refresh, delete, search)
    // so redrawing the data doesn't bounce the user back to Journeys.
    var activePane = 'journeys';

    function render() {
        var host = document.getElementById(HOST_ID);
        if (!host) return;
        disposeHistoryChart();          // the old canvas is about to be wiped

        var tabs = [
            { id: 'journeys', icon: 'fa-route', label: 'Journeys' },
            { id: 'fuel', icon: 'fa-gas-pump', label: 'Fuel' },
            { id: 'history', icon: 'fa-chart-line', label: 'Price History' },
        ];
        var nav = tabs.map(function (t) {
            return '<li class="nav-item">' +
              '<button class="nav-link' + (t.id === activePane ? ' active' : '') + '" ' +
                'data-bs-toggle="tab" data-bs-target="#drivePane-' + t.id + '" ' +
                'data-drive-pane="' + t.id + '" type="button">' +
                '<i class="fas ' + t.icon + ' me-1"></i> ' +
                '<span class="tab-label">' + t.label + '</span></button>' +
            '</li>';
        }).join('');

        function pane(id, html) {
            return '<div class="tab-pane fade' +
                (id === activePane ? ' show active' : '') +
                '" id="drivePane-' + id + '">' + html + '</div>';
        }

        // zmm-icon-rail: the app-wide sub-tab pattern — icon-only on phones
        // with a sticky text-width toggle that reveals the labels (CSS in
        // mobile.css); full labels on desktop. Same as remote-access,
        // settings, upgrade, speaker-sync.
        host.innerHTML =
            '<ul class="nav nav-pills mb-3 zmm-icon-rail">' +
              '<li class="nav-item d-md-none rail-toggle-item">' +
                '<button class="nav-link rail-toggle" type="button" title="Toggle tab labels" ' +
                  'aria-label="Toggle tab labels" ' +
                  'onclick="this.closest(\'ul\').classList.toggle(\'labels-expanded\')">' +
                  '<i class="fas fa-text-width"></i></button></li>' +
              nav + '</ul>' +
            '<div class="tab-content">' +
              pane('journeys', journeysCard()) +
              pane('fuel', fuelCard()) +
              pane('history', historyCard()) +
            '</div>';

        bindJourneyHandlers();
        bindFuelHandlers();
        bindHistoryHandlers();

        host.querySelectorAll('[data-drive-pane]').forEach(function (btn) {
            btn.addEventListener('shown.bs.tab', function () {
                activePane = btn.getAttribute('data-drive-pane');
                // The chart can only measure itself in a visible pane, so it
                // draws on first show rather than at render time.
                if (activePane === 'history') renderHistoryChart();
            });
        });
        if (activePane === 'history') renderHistoryChart();
    }

    // ----------------------------------------------------------
    // Journeys card
    // ----------------------------------------------------------
    function statTile(label, value, sub) {
        return '<div class="col-6 col-md-3">' +
            '<div class="border rounded p-2 text-center h-100">' +
              '<div class="fs-5 fw-bold">' + value + '</div>' +
              '<div class="small text-muted">' + label +
                (sub ? '<br><span class="text-muted">' + sub + '</span>' : '') +
              '</div>' +
            '</div></div>';
    }

    function journeysCard() {
        var tiles = '';
        if (stats && stats.trip_count > 0) {
            tiles =
            '<div class="row g-2 mb-3">' +
              statTile('Trips', stats.trip_count) +
              statTile('Distance', fmtMiles(stats.total_distance_m)) +
              statTile('Avg speed', fmtMph(stats.overall_avg_speed_mps), 'distance / time') +
              statTile('Top speed', fmtMph(stats.top_speed_mps)) +
            '</div>';
        }

        var body;
        if (!trips.length) {
            body =
            '<div class="text-center text-muted py-4">' +
              'No journeys recorded yet.<br>' +
              '<span class="small">Enable <strong>journey recording</strong> for a presence user in ' +
              'Settings → Presence, choose a car Bluetooth device in the companion app, ' +
              'and drives will appear here automatically.</span>' +
            '</div>';
        } else {
            // Time and Max hide on phones (d-none d-sm-table-cell): four
            // columns fit a 390 px screen, six don't, and the expandable
            // detail row still carries everything hidden.
            var rows = trips.map(function (t) {
                return '<tr class="drive-trip-row" data-trip="' + escape(t.trip_id) + '" style="cursor:pointer">' +
                  '<td class="small">' + fmtWhen(t.started_at) + '</td>' +
                  '<td class="small">' + escape(placeName(t.start_place)) +
                      ' <i class="fas fa-arrow-right text-muted mx-1"></i> ' +
                      escape(placeName(t.end_place)) + '</td>' +
                  '<td>' + fmtMiles(t.distance_m) + '</td>' +
                  '<td class="small d-none d-sm-table-cell">' + fmtDuration(t.duration_s) + '</td>' +
                  '<td>' + fmtMph(t.avg_speed_mps) + '</td>' +
                  '<td class="d-none d-sm-table-cell">' + fmtMph(t.max_speed_mps) + '</td>' +
                '</tr>' +
                '<tr class="d-none" id="trip-detail-' + escape(t.trip_id) + '">' +
                  '<td colspan="6" class="bg-light small p-3">' + tripDetail(t) + '</td>' +
                '</tr>';
            }).join('');
            body =
            '<div class="table-responsive">' +
              '<table class="table table-sm table-hover mb-0">' +
                '<thead class="table-light"><tr>' +
                  '<th>When</th><th>Route</th><th>Distance</th>' +
                  '<th class="d-none d-sm-table-cell">Time</th>' +
                  '<th>Avg</th><th class="d-none d-sm-table-cell">Max</th>' +
                '</tr></thead><tbody>' + rows + '</tbody>' +
              '</table>' +
            '</div>';
        }

        return '<div class="card shadow-sm h-100">' +
          '<div class="card-header bg-light py-2 d-flex justify-content-between align-items-center">' +
            '<span class="fw-bold"><i class="fas fa-route me-1"></i> Journeys</span>' +
            '<button class="btn btn-sm btn-outline-secondary" id="drive-refresh">' +
              '<i class="fas fa-rotate"></i></button>' +
          '</div>' +
          '<div class="card-body">' + tiles + body + '</div>' +
        '</div>';
    }

    function tripDetail(t) {
        var sd = mph(t.stddev_speed_mps);
        // Duration and max repeat here because their table columns are
        // hidden on phones; on wider screens the repetition is harmless.
        return '<div class="row g-2">' +
          '<div class="col-6 col-sm-2"><strong>Time</strong><br>' + fmtDuration(t.duration_s) + '</div>' +
          '<div class="col-6 col-sm-2"><strong>Max speed</strong><br>' + fmtMph(t.max_speed_mps) + '</div>' +
          '<div class="col-6 col-sm-2"><strong>Min speed</strong><br>' + fmtMph(t.min_speed_mps) + '</div>' +
          '<div class="col-6 col-sm-2"><strong>Speed σ</strong><br>' +
              (sd == null ? '—' : sd.toFixed(1) + ' mph') + '</div>' +
          '<div class="col-6 col-sm-2"><strong>Fixes</strong><br>' + (t.fix_count || '—') + '</div>' +
          '<div class="col-6 col-sm-2 text-end align-self-end">' +
            '<button class="btn btn-sm btn-outline-danger" data-del-trip="' + escape(t.trip_id) + '">' +
              '<i class="fas fa-trash me-1"></i>Delete</button>' +
          '</div>' +
        '</div>';
    }

    function bindJourneyHandlers() {
        var refresh = document.getElementById('drive-refresh');
        if (refresh) refresh.onclick = async function () {
            await fetchJourneys();
            render();
        };

        document.querySelectorAll('.drive-trip-row').forEach(function (row) {
            row.onclick = function () {
                var d = document.getElementById('trip-detail-' + row.getAttribute('data-trip'));
                if (d) d.classList.toggle('d-none');
            };
        });

        document.querySelectorAll('[data-del-trip]').forEach(function (btn) {
            btn.onclick = async function (ev) {
                ev.stopPropagation();
                var id = btn.getAttribute('data-del-trip');
                if (window.zbmConfirm && !await window.zbmConfirm({
                    title: 'Delete journey',
                    message: 'Delete this journey and its track?',
                    confirmText: 'Delete', variant: 'danger'
                })) return;
                try {
                    var r = await fetch('/api/journeys/' + encodeURIComponent(id),
                                        { method: 'DELETE', credentials: 'same-origin' });
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    await fetchJourneys();
                    render();
                    if (window.toast) window.toast.success('Journey deleted');
                } catch (e) {
                    if (window.toast) window.toast.error('Delete failed: ' + (e.message || e));
                }
            };
        });
    }

    // ----------------------------------------------------------
    // Fuel card
    // ----------------------------------------------------------
    function fuelCard() {
        var prefs = getFuelPrefs();
        var opts = Object.keys(fuelTypes).map(function (k) {
            return '<option value="' + escape(k) + '"' +
                   (k === prefs.fuel ? ' selected' : '') + '>' +
                   escape(fuelTypes[k]) + '</option>';
        }).join('');

        return '<div class="card shadow-sm h-100">' +
          '<div class="card-header bg-light py-2">' +
            '<span class="fw-bold"><i class="fas fa-gas-pump me-1"></i> Cheapest Fuel Nearby</span>' +
          '</div>' +
          '<div class="card-body">' +
            // Stacks to full-width rows below 576 px; a three-across form at
            // phone width leaves the postcode field about six characters wide.
            '<div class="row g-2 align-items-end">' +
              '<div class="col-12 col-sm-5">' +
                '<label class="form-label small fw-bold mb-1">Fuel</label>' +
                '<select class="form-select form-select-sm" id="fuel-type">' + opts + '</select>' +
              '</div>' +
              '<div class="col-6 col-sm-4">' +
                '<label class="form-label small fw-bold mb-1">Postcode</label>' +
                '<input class="form-control form-control-sm" id="fuel-postcode" ' +
                  'placeholder="home" value="' + escape(prefs.postcode) + '" maxlength="10" ' +
                  'autocapitalize="characters" autocomplete="postal-code">' +
              '</div>' +
              '<div class="col-6 col-sm-3">' +
                '<label class="form-label small fw-bold mb-1">' +
                  'Within <span id="fuel-radius-label">' + prefs.radius + '</span> km</label>' +
                '<input type="range" class="form-range" id="fuel-radius" ' +
                  'min="2" max="40" step="1" value="' + prefs.radius + '">' +
              '</div>' +
            '</div>' +
            '<div class="d-grid mt-2">' +
              '<button class="btn btn-sm btn-primary" id="fuel-search">' +
                '<i class="fas fa-magnifying-glass me-1"></i> Find cheapest</button>' +
            '</div>' +
            '<div class="small text-muted mt-1" id="fuel-note">' +
              'Leave postcode blank to search around home. ' +
              'Prices come from the UK retailer open-data scheme (most update daily).' +
            '</div>' +
            '<div class="mt-3" id="fuel-results"></div>' +
          '</div>' +
        '</div>';
    }

    function bindFuelHandlers() {
        var radius = document.getElementById('fuel-radius');
        var radiusLabel = document.getElementById('fuel-radius-label');
        if (radius && radiusLabel) {
            radius.oninput = function () { radiusLabel.textContent = radius.value; };
        }
        var btn = document.getElementById('fuel-search');
        if (btn) btn.onclick = searchFuel;
        var pc = document.getElementById('fuel-postcode');
        if (pc) pc.onkeydown = function (ev) { if (ev.key === 'Enter') searchFuel(); };
    }

    async function searchFuel() {
        var fuel = (document.getElementById('fuel-type') || {}).value || 'E10';
        var postcode = ((document.getElementById('fuel-postcode') || {}).value || '').trim();
        var radius = (document.getElementById('fuel-radius') || {}).value || 8;
        var out = document.getElementById('fuel-results');
        var btn = document.getElementById('fuel-search');
        if (!out) return;

        saveFuelPrefs(Object.assign(getFuelPrefs(),
            { fuel: fuel, postcode: postcode, radius: Number(radius) }));

        out.innerHTML = '<div class="text-center text-muted py-3">' +
            '<i class="fas fa-spinner fa-spin"></i> Fetching prices… ' +
            '<span class="small">(first search loads all retailer feeds; can take ~20 s)</span></div>';
        if (btn) btn.disabled = true;

        var qs = '?fuel=' + encodeURIComponent(fuel) +
                 '&radius_km=' + encodeURIComponent(radius) + '&limit=10' +
                 (postcode ? '&postcode=' + encodeURIComponent(postcode) : '');
        try {
            var r = await fetch('/api/fuel/nearby' + qs, { credentials: 'same-origin' });
            var data = await r.json().catch(function () { return {}; });
            if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
            renderFuelResults(out, data);
            loadFuelTrend(fuel);
            // The search just recorded new rows, but only redraw if the
            // History pane is actually visible — a hidden pane has no
            // dimensions to draw into, and it re-renders on show anyway.
            if (activePane === 'history') renderHistoryChart();
        } catch (e) {
            out.innerHTML = '<div class="alert alert-warning py-2 small mb-0">' +
                escape(e.message || String(e)) + '</div>';
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    // ----------------------------------------------------------
    // Price-history chart
    // ----------------------------------------------------------
    // The snapshots fuel_history.py records at every search, drawn as a
    // daily MEDIAN line over a shaded MIN–MAX band. One measure, one axis
    // (£/L over time); single series so the card title is the legend.
    //
    // Colour: the same blue the Energy tab uses for its "cheap" pole,
    // validated (dataviz six-checks) against both card surfaces.
    var historyChart = null;

    function priceLineColour() {
        return document.documentElement.getAttribute('data-theme') === 'dark'
            ? '#3b82f6' : '#2563eb';
    }

    function disposeHistoryChart() {
        if (historyChart) { historyChart.dispose(); historyChart = null; }
    }

    function historyCard() {
        var prefs = getFuelPrefs();
        var days = [7, 30, 90].map(function (d) {
            return '<button type="button" class="btn btn-sm ' +
                (d === prefs.historyDays ? 'btn-secondary' : 'btn-outline-secondary') +
                '" data-hist-days="' + d + '">' + d + 'd</button>';
        }).join('');
        return '<div class="card shadow-sm">' +
          '<div class="card-header bg-light py-2 d-flex justify-content-between align-items-center flex-wrap gap-2">' +
            '<span class="fw-bold"><i class="fas fa-chart-line me-1"></i> ' +
              'Price History — <span id="fuel-hist-label">' +
              escape(fuelTypes[prefs.fuel] || prefs.fuel) + '</span>, daily median</span>' +
            '<div class="btn-group" role="group" aria-label="History window">' + days + '</div>' +
          '</div>' +
          '<div class="card-body">' +
            '<div id="fuel-history-chart" style="height:260px"></div>' +
            '<div class="small text-muted mt-1">Shaded band is the cheapest-to-dearest ' +
              'spread across recorded stations each day. History grows as you search — ' +
              'every query is snapshotted.</div>' +
          '</div>' +
        '</div>';
    }

    function bindHistoryHandlers() {
        document.querySelectorAll('[data-hist-days]').forEach(function (btn) {
            btn.onclick = function () {
                var p = getFuelPrefs();
                p.historyDays = Number(btn.getAttribute('data-hist-days'));
                saveFuelPrefs(p);
                document.querySelectorAll('[data-hist-days]').forEach(function (b) {
                    b.className = 'btn btn-sm ' +
                        (b === btn ? 'btn-secondary' : 'btn-outline-secondary');
                });
                renderHistoryChart();
            };
        });
    }

    async function renderHistoryChart() {
        var el = document.getElementById('fuel-history-chart');
        if (!el) return;
        var prefs = getFuelPrefs();
        var label = document.getElementById('fuel-hist-label');
        if (label) label.textContent = fuelTypes[prefs.fuel] || prefs.fuel;

        var h = null;
        try {
            var r = await fetch('/api/fuel/history?fuel=' + encodeURIComponent(prefs.fuel) +
                                '&days=' + prefs.historyDays, { credentials: 'same-origin' });
            if (r.ok) h = await r.json();
        } catch (e) { /* falls through to empty state */ }

        if (!h || !h.series || h.series.length < 2) {
            disposeHistoryChart();
            el.innerHTML = '<div class="text-center text-muted py-5 small">' +
                'Not enough history yet — the chart appears after prices have been ' +
                'recorded on two or more days. Search above to start recording.</div>';
            return;
        }

        // Clear the empty-state text only when first creating the chart:
        // wiping innerHTML with a live instance orphans its canvas (the
        // instance keeps rendering into a detached node — blank chart).
        if (!historyChart) {
            el.innerHTML = '';
            historyChart = createChart(el);
        }

        var daysAxis = h.series.map(function (s) { return s.day; });
        var minS = h.series.map(function (s) { return s.min; });
        // The band is drawn as stacked areas: an invisible base at MIN, then
        // a fill of (MAX - MIN) on top. deltas keeps the fill honest.
        var bandDelta = h.series.map(function (s) { return s.max - s.min; });
        var medianS = h.series.map(function (s) { return s.median; });
        var colour = priceLineColour();

        historyChart.setOption({
            grid: { left: 48, right: 16, top: 24, bottom: 28 },
            tooltip: {
                trigger: 'axis',
                axisPointer: { type: 'line' },
                formatter: function (params) {
                    var i = params[0].dataIndex;
                    var s = h.series[i];
                    return '<strong>' + s.day + '</strong><br>' +
                        'Median £' + s.median.toFixed(2) + '<br>' +
                        'Range £' + s.min.toFixed(2) + ' – £' + s.max.toFixed(2) + '<br>' +
                        s.stations + ' station(s)';
                },
            },
            xAxis: { type: 'category', data: daysAxis, boundaryGap: false },
            yAxis: {
                type: 'value', scale: true,
                axisLabel: { formatter: function (v) { return '£' + v.toFixed(2); } },
            },
            series: [
                { name: 'min', type: 'line', data: minS, stack: 'band',
                  lineStyle: { opacity: 0 }, symbol: 'none', silent: true,
                  tooltip: { show: false } },
                { name: 'range', type: 'line', data: bandDelta, stack: 'band',
                  lineStyle: { opacity: 0 }, symbol: 'none', silent: true,
                  areaStyle: { color: colour, opacity: 0.14 },
                  tooltip: { show: false } },
                { name: 'median', type: 'line', data: medianS,
                  lineStyle: { width: 2, color: colour },
                  itemStyle: { color: colour },
                  symbol: 'circle', symbolSize: 5, showSymbol: h.series.length <= 31 },
            ],
        });
    }

    // Series colour is theme-dependent; chart-utils re-themes the axes but
    // replays the same option, so redraw with the new colour ourselves.
    document.addEventListener('themechange', function () {
        if (historyChart && activePane === 'history') renderHistoryChart();
    });

    // Historical context under the results — the hub snapshots every query
    // into its own history DB, so this gets richer the more you search.
    async function loadFuelTrend(fuel) {
        var host = document.getElementById('fuel-trend');
        if (!host) return;
        try {
            var r = await fetch('/api/fuel/history?fuel=' + encodeURIComponent(fuel) + '&days=30',
                                { credentials: 'same-origin' });
            if (!r.ok) return;
            var h = await r.json();
            if (!h.series || h.series.length < 2 || !h.cheapest_seen) return;
            var today = h.series[h.series.length - 1];
            var first = h.series[0];
            var dir = today.min > first.min ? 'up' : today.min < first.min ? 'down' : 'flat';
            var arrow = dir === 'up' ? '<i class="fas fa-arrow-trend-up text-danger"></i>'
                      : dir === 'down' ? '<i class="fas fa-arrow-trend-down text-success"></i>'
                      : '<i class="fas fa-arrows-left-right text-muted"></i>';
            host.innerHTML =
                '<div class="small text-muted border-top pt-2 mt-2">' +
                  arrow + ' Cheapest seen in ' + h.series.length + ' day(s) of history: ' +
                  '<strong>£' + h.cheapest_seen.price.toFixed(2) + '</strong> — ' +
                  escape(h.cheapest_seen.brand || '?') + ' ' +
                  escape(h.cheapest_seen.postcode || '') +
                  ' (' + escape(h.cheapest_seen.day) + ')' +
                '</div>';
        } catch (e) { /* history is a bonus, never an error */ }
    }

    function renderFuelResults(out, data) {
        if (!data.stations || !data.stations.length) {
            out.innerHTML = '<div class="text-center text-muted py-3">' +
                'No stations selling ' + escape(data.fuel_label || data.fuel) +
                ' within ' + data.radius_km + ' km.</div>';
            return;
        }
        var cheapest = data.stations[0].price;
        var rows = data.stations.map(function (s, i) {
            var delta = s.price - cheapest;
            return '<tr' + (i === 0 ? ' class="table-success"' : '') + '>' +
              '<td class="small">' +
                '<strong>' + escape(s.brand || '?') + '</strong><br>' +
                '<span class="text-muted">' + escape(s.address || '') + '</span>' +
              '</td>' +
              '<td class="text-nowrap">£' + s.price.toFixed(2) +
                (i > 0 && delta > 0.001
                    ? '<br><span class="small text-muted">+' + Math.round(delta * 100) + 'p</span>'
                    : '<br><span class="small text-success fw-bold">cheapest</span>') +
              '</td>' +
              '<td class="small text-nowrap">' +
                (s.distance_km != null ? s.distance_km.toFixed(1) + ' km' : '—') +
              '</td>' +
              '<td class="text-nowrap">' +
                '<a class="btn btn-sm btn-outline-primary" target="_blank" rel="noopener" ' +
                   'href="' + escape(s.maps_url) + '" title="Open in Google Maps">' +
                  '<i class="fas fa-map-location-dot me-1"></i>' + escape(s.postcode || 'Map') +
                '</a>' +
              '</td>' +
            '</tr>';
        }).join('');

        var centre = data.centre || {};
        var centreNote = centre.source === 'home' ? 'around home'
                       : centre.source === 'postcode' ? 'around that postcode'
                       : 'around the given point';
        out.innerHTML =
          '<div class="small text-muted mb-1">' +
            data.count + ' station(s) with ' + escape(data.fuel_label || data.fuel) +
            ' ' + centreNote + ' — cheapest first. Tap the postcode for directions.' +
          '</div>' +
          '<div class="table-responsive">' +
            '<table class="table table-sm table-hover align-middle mb-0">' +
              '<thead class="table-light"><tr>' +
                '<th>Station</th><th>Price/L</th><th>Dist</th><th>Maps</th>' +
              '</tr></thead><tbody>' + rows + '</tbody>' +
            '</table>' +
          '</div>' +
          '<div id="fuel-trend"></div>';
    }

    // ----------------------------------------------------------
    // Public init
    // ----------------------------------------------------------
    var initialised = false;

    window.initDriveTab = async function () {
        // Re-fetch on every tab open (cheap), but only build fuel types once.
        if (!initialised) {
            initialised = true;
            await fetchFuelTypes();
        }
        await fetchJourneys();
        render();
    };
})();
