/* Drive tab — journeys, cheapest fuel nearby, and fuel price history.
   An ES module (unlike presence-settings.js) because the chart uses the shared
   chart-utils/ECharts layer; still exposes window.initDriveTab for main.js.
   See docs/journeys.md. */

import { createChart } from './chart-utils.js';
import { whileVisible } from './utils.js';
import { LAYERS, layerColour, findStops, findPeaks, nearestIndex,
         terrainOf, terrainSummary, terrainRuns } from './drive-track.js';

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
    var drivers = [];
    var board = null;
    // Which driver the Journeys pane is scoped to; '' is everyone. Held here
    // rather than in the URL so switching sub-tabs doesn't reset it.
    var driverFilter = '';
    var fuelTypes = { E10: 'Petrol (E10)', E5: 'Premium petrol (E5)',
                      B7: 'Diesel (B7)', SDV: 'Super diesel (SDV)' };
    var fuelPrefsKey = 'zbm-drive-fuel-prefs';

    // How the active region quotes a price. Replaced by whatever the API
    // reports; these defaults are the UK's, and only ever show before the
    // first response lands. Prices on the wire are always in the major
    // currency unit — display_scale decides whether that is how they are shown.
    var fuelUnits = { currency: 'GBP', symbol: '\u00a3', volume: 'L',
                      distance: 'km', display_scale: 'minor', decimals: 3 };
    var fuelAttribution = '';
    var fuelDefaultGrade = '';
    // False for a region that publishes area averages rather than
    // forecourts. Changes what the card asks for as well as what it shows.
    var fuelStationLevel = true;

    // Suffix for a price quoted in a currency's minor unit. Only regions that
    // price that way need an entry; anywhere else shows the major unit instead.
    var MINOR_SUFFIX = { GBP: 'p' };

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

    // Acceleration reads better in g than m/s² — "0.45 g" is a quantity
    // drivers have a feel for — but the stored unit is kept alongside it
    // rather than hidden, because that is what the API returns.
    function fmtG(mps2) {
        if (mps2 == null) return '—';
        return (mps2 / 9.80665).toFixed(2) + ' g';
    }

    // Null means "this trip had no motion sensing", which must never render as
    // a perfect score. Every behaviour formatter here returns an em dash for
    // null and only ever styles a real number.
    function scoreClass(s) {
        if (s == null) return 'text-muted';
        if (s >= 85) return 'text-success';
        if (s >= 60) return 'text-warning';
        return 'text-danger';
    }

    function fmtScore(s) {
        if (s == null) return '<span class="text-muted">—</span>';
        return '<span class="' + scoreClass(s) + '">' + Math.round(s) + '</span>';
    }

    var eventKinds = {
        brake:  { label: 'Harsh braking',      icon: 'fa-hand',            cls: 'danger'  },
        accel:  { label: 'Harsh acceleration', icon: 'fa-gauge-high',      cls: 'warning' },
        corner: { label: 'Hard cornering',     icon: 'fa-arrows-turn-right', cls: 'warning' },
        // Detected before the phone had worked out which way the car faces,
        // so it is a real excursion that simply cannot be attributed.
        harsh:  { label: 'Harsh manoeuvre',    icon: 'fa-triangle-exclamation', cls: 'secondary' }
    };

    function placeName(p) {
        if (!p || p === 'away') return 'Away';
        if (p === 'home') return 'Home';
        return p.replace(/[_-]+/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
    }

    function getFuelPrefs() {
        var base = { fuel: fuelDefaultGrade || 'E10', postcode: '',
                     radius: 8, historyDays: 30 };
        var prefs = base;
        try {
            var raw = localStorage.getItem(fuelPrefsKey);
            if (raw) prefs = Object.assign({}, base, JSON.parse(raw));
        } catch (e) {}
        // A grade saved under one region need not exist in another — B7 means
        // nothing in Germany. Falling back to the region's default beats
        // rendering a select with nothing selected.
        if (!Object.prototype.hasOwnProperty.call(fuelTypes, prefs.fuel)) {
            prefs.fuel = base.fuel in fuelTypes
                ? base.fuel : (Object.keys(fuelTypes)[0] || base.fuel);
        }
        return prefs;
    }

    function saveFuelPrefs(p) {
        try { localStorage.setItem(fuelPrefsKey, JSON.stringify(p)); } catch (e) {}
    }

    // Data
    async function fetchJourneys() {
        // Scoping the trips and the tiles to the same driver is the whole
        // point of the filter: an unfiltered stat row above a filtered table
        // is two different stories on one page.
        var scope = driverFilter ? '&driver_id=' + encodeURIComponent(driverFilter) : '';
        try {
            var r = await fetch('/api/journeys?limit=100' + scope,
                                { credentials: 'same-origin' });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            trips = (await r.json()).trips || [];
        } catch (e) {
            log.warn('journeys fetch failed', e);
            trips = [];
        }
        try {
            var r2 = await fetch('/api/journeys/stats' + (scope ? '?' + scope.slice(1) : ''),
                                 { credentials: 'same-origin' });
            if (r2.ok) stats = await r2.json();
        } catch (e) { stats = null; }
        await fetchDrivers();
    }

    async function fetchDrivers() {
        try {
            var r = await fetch('/api/journeys/drivers', { credentials: 'same-origin' });
            if (r.ok) drivers = (await r.json()).drivers || [];
        } catch (e) { log.warn('drivers fetch failed', e); }
        try {
            var r2 = await fetch('/api/journeys/leaderboard', { credentials: 'same-origin' });
            if (r2.ok) board = await r2.json();
        } catch (e) { board = null; }
    }

    function driverById(id) {
        for (var i = 0; i < drivers.length; i++) {
            if (drivers[i].driver_id === id) return drivers[i];
        }
        return null;
    }

    function driverName(id) {
        var d = driverById(id);
        return d ? d.name : null;
    }

    // Fallback ring colour for a driver saved without one, stable per id so a
    // driver keeps the same colour between renders and between sessions.
    var DRIVER_COLOURS = ['#1f77b4', '#d62728', '#2ca02c', '#9467bd',
                          '#ff7f0e', '#17becf', '#8c564b', '#e377c2'];

    function driverColour(id) {
        var d = driverById(id);
        if (d && d.colour) return d.colour;
        var h = 0;
        for (var i = 0; i < String(id).length; i++) h = (h * 31 + String(id).charCodeAt(i)) >>> 0;
        return DRIVER_COLOURS[h % DRIVER_COLOURS.length];
    }

    // Grades, units and attribution all belong to the region, so they are
    // fetched together rather than assumed. /api/fuel/status carries the units;
    // /api/fuel/types alone would leave the first render formatting in pence
    // for a country that has never used them.
    async function fetchFuelTypes() {
        try {
            var r = await fetch('/api/fuel/status', { credentials: 'same-origin' });
            if (!r.ok) return;
            var st = await r.json();
            fuelTypes = st.fuel_types || fuelTypes;
            if (st.units) fuelUnits = st.units;
            fuelAttribution = st.attribution || '';
            fuelDefaultGrade = st.default_grade || '';
            if (typeof st.station_level === 'boolean') fuelStationLevel = st.station_level;
        } catch (e) { /* keep defaults */ }
    }

    // Render — page skeleton
    // Which sub-tab is open; survives re-renders (refresh, delete, search)
    // so redrawing the data doesn't bounce the user back to Journeys.
    var activePane = 'journeys';

    function render() {
        var host = document.getElementById(HOST_ID);
        if (!host) return;
        disposeHistoryChart();          // the old canvas is about to be wiped
        disposeTripMaps();              // and so are the trip maps

        var tabs = [
            { id: 'journeys', icon: 'fa-route', label: 'Journeys' },
            { id: 'drivers', icon: 'fa-trophy', label: 'Drivers' },
            { id: 'fuel', icon: 'fa-gas-pump', label: 'Fuel' },
            { id: 'history', icon: 'fa-chart-line', label: 'Price History' },
            // Named places (the apiary) live here rather than in Settings →
            // Presence: journeys name their endpoints from it, so the list
            // of places belongs beside the trips it labels.
            { id: 'apiary', icon: 'fa-map-location-dot', label: 'Apiary' },
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
              pane('drivers', driversCard()) +
              pane('fuel', fuelCard()) +
              pane('history', historyCard()) +
              pane('apiary', apiaryCard()) +
            '</div>';

        bindJourneyHandlers();
        bindDriverHandlers();
        bindFuelHandlers();
        bindHistoryHandlers();
        renderLive();

        host.querySelectorAll('[data-drive-pane]').forEach(function (btn) {
            btn.addEventListener('shown.bs.tab', function () {
                activePane = btn.getAttribute('data-drive-pane');
                // The chart can only measure itself in a visible pane, so it
                // draws on first show rather than at render time. Same story
                // for the apiary's map picker.
                if (activePane === 'history') renderHistoryChart();
                if (activePane === 'apiary') initApiary();
            });
        });
        if (activePane === 'history') renderHistoryChart();
        if (activePane === 'apiary') initApiary();
    }

    // Apiary pane — hosts places-settings.js (moved from Settings →
    // Presence). That module owns everything inside
    // #places-settings-host; this pane just provides the card.
    function apiaryCard() {
        return '<div class="card shadow-sm">' +
          '<div class="card-header bg-light py-2">' +
            '<span class="fw-bold"><i class="fas fa-map-location-dot me-1"></i> Apiary</span>' +
          '</div>' +
          '<div class="card-body" id="places-settings-host">' +
            '<div class="text-center text-muted py-4">' +
              '<i class="fas fa-spinner fa-spin"></i> Loading apiary...</div>' +
          '</div>' +
        '</div>';
    }

    function initApiary() {
        if (window.initPlacesSettings) window.initPlacesSettings();
        else log.warn('places-settings.js not loaded; apiary pane is empty');
    }

    // Journeys card
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

            // Second row only when at least one trip carried motion data.
            // Drawing it from all-null columns would present "no accelerometer"
            // as flawless driving.
            if (stats.measured_trip_count > 0) {
                tiles +=
                '<div class="row g-2 mb-3">' +
                  statTile('Driving style', fmtScore(stats.smoothness_score),
                           'over ' + stats.measured_trip_count + ' measured trip' +
                           (stats.measured_trip_count === 1 ? '' : 's')) +
                  statTile('Harsh events', stats.harsh_event_count == null ? '—'
                           : stats.harsh_event_count,
                           (stats.harsh_brake_count || 0) + ' brake / ' +
                           (stats.harsh_accel_count || 0) + ' accel / ' +
                           (stats.harsh_corner_count || 0) + ' corner') +
                  statTile('Peak braking', fmtG(stats.max_brake_mps2)) +
                  statTile('Peak cornering', fmtG(stats.max_lat_mps2)) +
                '</div>';
            }
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
                  '<td class="small">' + fmtWhen(t.started_at) +
                      unconfirmedMark(t) + '</td>' +
                  '<td class="small">' + escape(placeName(t.start_place)) +
                      ' <i class="fas fa-arrow-right text-muted mx-1"></i> ' +
                      escape(placeName(t.end_place)) + '</td>' +
                  '<td class="small d-none d-md-table-cell">' + driverCell(t) + '</td>' +
                  '<td>' + fmtMiles(t.distance_m) + '</td>' +
                  '<td class="small d-none d-sm-table-cell">' + fmtDuration(t.duration_s) + '</td>' +
                  '<td>' + fmtMph(t.avg_speed_mps) + '</td>' +
                  '<td class="d-none d-sm-table-cell">' + fmtMph(t.max_speed_mps) + '</td>' +
                  '<td class="d-none d-md-table-cell fw-bold">' +
                      fmtScore(t.smoothness_score) + '</td>' +
                '</tr>' +
                '<tr class="d-none" id="trip-detail-' + escape(t.trip_id) + '">' +
                  '<td colspan="8" class="bg-light small p-3">' + tripDetail(t) + '</td>' +
                '</tr>';
            }).join('');
            body =
            '<div class="table-responsive">' +
              '<table class="table table-sm table-hover mb-0">' +
                '<thead class="table-light"><tr>' +
                  '<th>When</th><th>Route</th>' +
                  '<th class="d-none d-md-table-cell">Driver</th>' +
                  '<th>Distance</th>' +
                  '<th class="d-none d-sm-table-cell">Time</th>' +
                  '<th>Avg</th><th class="d-none d-sm-table-cell">Max</th>' +
                  '<th class="d-none d-md-table-cell">Style</th>' +
                '</tr></thead><tbody>' + rows + '</tbody>' +
              '</table>' +
            '</div>';
        }

        // Only worth a filter once there is more than one driver to filter to.
        var filter = drivers.length > 1
            ? '<select class="form-select form-select-sm w-auto me-2" id="drive-driver-filter">' +
                '<option value=""' + (driverFilter ? '' : ' selected') + '>All drivers</option>' +
                drivers.map(function (d) {
                    return '<option value="' + escape(d.driver_id) + '"' +
                           (d.driver_id === driverFilter ? ' selected' : '') + '>' +
                           escape(d.name) + '</option>';
                }).join('') +
              '</select>'
            : '';

        return '<div class="card shadow-sm h-100">' +
          '<div class="card-header bg-light py-2 d-flex justify-content-between align-items-center">' +
            '<span class="fw-bold"><i class="fas fa-route me-1"></i> Journeys</span>' +
            '<div class="d-flex align-items-center">' + filter +
              '<button class="btn btn-sm btn-outline-secondary" id="drive-refresh">' +
                '<i class="fas fa-rotate"></i></button>' +
            '</div>' +
          '</div>' +
          '<div class="card-body"><div id="drive-live"></div>' + tiles + body + '</div>' +
        '</div>';
    }

    // A trip attributed by inference rather than by someone saying so. Marked
    // on every screen size, unlike the Driver column: a score presented without
    // the caveat that the hub guessed who earned it is the bias this whole
    // feature exists to remove, only harder to spot.
    function unconfirmedMark(t) {
        if (!t.driver_id || t.confidence === 'high') return '';
        return ' <i class="fas fa-circle-question text-warning" ' +
               'title="Attributed automatically — tap to confirm who drove"></i>';
    }

    function driverCell(t) {
        if (!t.driver_id) {
            return '<span class="text-muted fst-italic">Unassigned</span>';
        }
        var name = driverName(t.driver_id) || t.driver_id;
        return driverDot(t.driver_id) + escape(name);
    }

    /**
     * The "who drove?" control. Always present, not only on low-confidence
     * trips: correcting a confident wrong guess is where the score is most
     * misleading, so it must not be the hardest one to fix.
     */
    function driverPicker(t) {
        if (!drivers.length) {
            return '<div class="col-12 col-sm-6"><strong>Driver</strong><br>' +
                   '<span class="text-muted">Add a driver on the Drivers tab to ' +
                   'attribute journeys.</span></div>';
        }
        var opts = '<option value=""' + (t.driver_id ? '' : ' selected') + '>— unassigned —</option>' +
            drivers.map(function (d) {
                return '<option value="' + escape(d.driver_id) + '"' +
                       (d.driver_id === t.driver_id ? ' selected' : '') + '>' +
                       escape(d.name) + '</option>';
            }).join('');

        var why = '';
        if (t.driver_id && t.confidence !== 'high') {
            why = '<div class="small text-warning-emphasis mt-1">' +
                  '<i class="fas fa-circle-question me-1"></i>' +
                  (t.attribution === 'copresence'
                      ? 'Another tracked phone was in the car for this drive, so this is a guess.'
                      : 'Attributed from journey history rather than observed.') +
                  '</div>';
        }

        return '<div class="col-12 col-sm-6"><strong>Driver</strong><br>' +
                 '<select class="form-select form-select-sm w-auto d-inline-block" ' +
                   'data-trip-driver="' + escape(t.trip_id) + '">' + opts + '</select>' +
                 why +
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
        '</div>' +
        '<hr class="my-2"><div class="row g-2">' + driverPicker(t) + '</div>' +
        behaviourDetail(t) +
        // Filled in on first expand — see loadTripDetail. The track and events
        // are the only parts of a trip not already in the list response, and
        // fetching every trip's up front would be a hundred requests to render
        // a table.
        '<div id="trip-events-' + escape(t.trip_id) + '" class="mt-2"></div>' +
        '<div id="trip-map-wrap-' + escape(t.trip_id) + '" class="mt-2"></div>';
    }

    // The counts are what happened; the score takes each event less the part
    // the hill did (journeys.md §Slope weighting). Said only when they differ.
    function slopeScored(t) {
        var w = t.weighted_event_count;
        if (w == null || t.harsh_event_count == null || w > t.harsh_event_count - 0.05) return '';
        return '<br><span class="text-muted">scored as ' + w.toFixed(1) + ' after slope</span>';
    }

    function slopeForgiven(e) {
        return e.slope_weight != null && e.slope_weight < 0.995;
    }

    /**
     * The inertial half of a trip. Absent entirely when the phone had no
     * motion sensing, rather than shown as a row of dashes: a trip recorded
     * before this existed is not a trip with nothing to report.
     */
    function behaviourDetail(t) {
        if (!t.motion_fix_count) return '';

        function cell(label, value, cls) {
            return '<div class="col-6 col-sm-3 col-lg-2">' +
                     '<strong>' + label + '</strong><br>' +
                     '<span class="' + (cls || '') + '">' + value + '</span>' +
                   '</div>';
        }

        var counts =
            (t.harsh_brake_count || 0) + ' <span class="text-muted">brake</span> · ' +
            (t.harsh_accel_count || 0) + ' <span class="text-muted">accel</span> · ' +
            (t.harsh_corner_count || 0) + ' <span class="text-muted">corner</span>';

        return '<hr class="my-2">' +
        '<div class="row g-2">' +
          cell('Driving style', fmtScore(t.smoothness_score) +
               '<span class="text-muted"> / 100</span>', 'fw-bold') +
          cell('Harsh events', counts + slopeScored(t)) +
          cell('Peak braking', fmtG(t.max_brake_mps2)) +
          cell('Peak acceleration', fmtG(t.max_accel_mps2)) +
          cell('Peak cornering', fmtG(t.max_lat_mps2)) +
          // RMS vertical acceleration: a smooth A-road sits near 0.5 m/s²,
          // a broken urban surface several times that.
          cell('Road roughness', t.roughness_mps2 == null ? '—'
               : t.roughness_mps2.toFixed(2) + ' m/s²') +
          cell('Stops', t.stop_count == null ? '—' : t.stop_count,
               '') +
          cell('Idling', t.idle_s == null ? '—' : fmtDuration(t.idle_s)) +
          // Kept apart rather than netted: a return trip nets to zero, which
          // says nothing about the road it was driven on.
          cell('Climb', t.climb_m == null ? '—' : '↑ ' + Math.round(t.climb_m) + ' m') +
          cell('Descent', t.descent_m == null ? '—'
               : '↓ ' + Math.round(t.descent_m) + ' m') +
        '</div>';
    }

    // Red is the phone's own event threshold (MotionSampler.EVENT_ENTER_MPS2),
    // so the map agrees with the event list by construction. Amber is the
    // approach to it. Change these with the phone's constant or the two stories
    // stop matching.
    var RAG_RED = 3.5;
    var RAG_AMBER = 2.5;

    var RAG_COLOURS = {
        green: '#2e7d32',
        amber: '#ed6c02',
        red:   '#d32f2f',
        // No motion data for that stretch — grey, never green. An unmeasured
        // road must not read as a well-driven one.
        none:  '#7a8894'
    };

    // Worst horizontal acceleration in the window ending at a track point.
    // horiz_peak needs no forward axis, so it spans the whole drive; the
    // long/lat pair is its calibration-gated fallback for older trips.
    function pointSeverity(p) {
        if (!p) return null;
        if (p.horiz_peak_mps2 != null) return Math.abs(p.horiz_peak_mps2);
        var lon = p.long_peak_mps2 == null ? null : Math.abs(p.long_peak_mps2);
        var lat = p.lat_peak_mps2 == null ? null : Math.abs(p.lat_peak_mps2);
        if (lon == null && lat == null) return null;
        return Math.max(lon || 0, lat || 0);
    }

    // Peak acceleration from logged events, keyed by the segment holding them.
    // Events are never calibration-gated, so this bands stretches of older
    // trips that would otherwise be grey under their own pins.
    function eventSeverityBySegment(trip, track) {
        var out = {};
        (trip.events || []).forEach(function (e) {
            if (e.ts == null || e.peak_mps2 == null) return;
            for (var i = 1; i < track.length; i++) {
                if (e.ts > track[i - 1].ts && e.ts <= track[i].ts) {
                    if (out[i] == null || e.peak_mps2 > out[i]) out[i] = e.peak_mps2;
                    break;
                }
            }
        });
        return out;
    }

    function ragBand(sev) {
        if (sev == null) return 'none';
        if (sev >= RAG_RED) return 'red';
        if (sev >= RAG_AMBER) return 'amber';
        return 'green';
    }

    /**
     * Fetch and render one trip's events, coaching note and map. Runs once per
     * trip per page load; the markup and Leaflet instance are left in place.
     */
    async function loadTripDetail(tripId) {
        var host = document.getElementById('trip-events-' + tripId);
        if (!host) return;
        if (host.dataset.loaded) {
            // Leaflet measures the container when it is created. If that
            // happened while the row was collapsed the map is sized to zero
            // and renders as grey; re-measuring on every expand is the
            // documented fix and costs nothing when the size is unchanged.
            var existing = tripViews[tripId];
            if (existing) setTimeout(function () {
                existing.map.invalidateSize();
                if (existing.chart) existing.chart.resize();
            }, 0);
            return;
        }
        host.dataset.loaded = '1';

        var trip;
        try {
            var r = await fetch('/api/journeys/' + encodeURIComponent(tripId),
                                { credentials: 'same-origin' });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            trip = await r.json();
        } catch (e) {
            log.warn('trip detail fetch failed', e);
            // Leave the panel empty rather than showing an error: the summary
            // above it is complete on its own, and this is detail.
            return;
        }

        renderEvents(host, trip);
        renderTripMap(tripId, trip);
    }

    function renderEvents(host, trip) {
        var evs = trip.events || [];
        if (!evs.length) return;

        var start = trip.started_at || (evs[0] && evs[0].ts) || 0;
        host.innerHTML =
          '<strong>Events</strong>' +
          '<div class="d-flex flex-wrap gap-1 mt-1">' +
          evs.map(function (e) {
              var k = eventKinds[e.kind] || eventKinds.harsh;
              var into = Math.max(0, Math.round((e.ts - start) / 60));
              return '<span class="badge bg-' + k.cls + '-subtle text-' + k.cls +
                     '-emphasis border border-' + k.cls + '-subtle">' +
                       '<i class="fas ' + k.icon + ' me-1"></i>' +
                       escape(k.label) + ' ' + fmtG(e.peak_mps2) +
                       ' <span class="opacity-75">@ ' + into + ' min' +
                       (slopeForgiven(e) ? ' · counts ' + Math.round(e.slope_weight * 100) + '%' : '') +
                       '</span>' +
                     '</span>';
          }).join('') +
          '</div>' +
          coachingNote(evs);
    }

    /**
     * One sentence on what to work on, from whichever event kind dominates.
     * Withheld below three events — two hard stops is traffic, not a habit, and
     * advice on that evidence teaches drivers to distrust the rest.
     */
    function coachingNote(evs) {
        if (evs.length < 3) return '';
        var tally = {};
        evs.forEach(function (e) { tally[e.kind] = (tally[e.kind] || 0) + 1; });

        var top = null;
        Object.keys(tally).forEach(function (k) {
            if (!top || tally[k] > tally[top]) top = k;
        });
        // No clear majority: reporting a tie as "your main issue is X" would be
        // inventing a pattern out of a coin toss.
        if (!top || tally[top] * 2 <= evs.length) return '';

        var advice = {
            brake: 'Hard braking is usually a following-distance problem — ' +
                   'arriving at a situation with more room turns most of these ' +
                   'into gentle ones.',
            accel: 'Hard acceleration costs fuel for time you get back at the ' +
                   'next red light. Easing onto the throttle is the single ' +
                   'cheapest change here.',
            corner: 'Hard cornering means speed carried into the turn rather ' +
                    'than shed before it. Braking earlier, in a straight line, ' +
                    'settles the car through the bend.',
            harsh:  'These were detected before the phone had worked out which ' +
                    'way the car faces — a longer drive will classify them.'
        }[top];
        if (!advice) return '';

        var k = eventKinds[top] || eventKinds.harsh;
        return '<div class="alert alert-light border mt-2 mb-0 py-2 px-3 small">' +
                 '<i class="fas fa-lightbulb text-warning me-1"></i>' +
                 '<strong>' + tally[top] + ' of ' + evs.length + '</strong> were ' +
                 escape(k.label.toLowerCase()) + '. ' + advice +
               '</div>';
    }

    // Per-trip map state, keyed by trip: the Leaflet map, its timeline chart
    // and what is drawn on them. Kept so an expand can re-measure one rather
    // than build a second on top of it.
    var tripViews = {};

    /**
     * Tear down every trip map before the host's innerHTML is replaced.
     *
     * Leaflet attaches listeners to window and document, not only to its
     * container, so dropping the container's markup leaves those live and the
     * instance uncollectable. Every refresh, delete or sub-tab switch
     * re-renders, so leaking one map per expanded row adds up over a session.
     * The timeline chart holds a ResizeObserver and goes the same way.
     */
    function disposeTripMaps() {
        Object.keys(tripViews).forEach(function (id) {
            var v = tripViews[id];
            try { v.map.remove(); } catch (e) { /* already gone */ }
            if (v.chart) v.chart.dispose();
        });
        tripViews = {};
    }

    // What the route is coloured by. 'style' is the RAG banding above; the
    // rest are drive-track.js LAYERS. One choice for every trip, remembered,
    // so comparing two journeys doesn't mean re-picking it on each.
    var mapLayerKey = 'zbm-drive-map-layer';
    var STYLE_LAYER = { label: 'Style', icon: 'fa-car-side' };

    function getMapLayer() {
        try {
            var v = localStorage.getItem(mapLayerKey);
            if (v === 'style' || LAYERS[v]) return v;
        } catch (e) {}
        return 'style';
    }

    function minutesInto(view, ts) {
        return Math.max(0, Math.round((ts - view.start) / 60));
    }

    // Value behind track point i on the active layer: m/s² severity for
    // 'style', the layer's display unit otherwise. Null is "not measured".
    function layerValue(view, i) {
        var layer = LAYERS[view.layer];
        if (layer) return layer.value(view.track[i]);
        var sev = pointSeverity(view.track[i]);
        var ev = view.evSeg[i];
        return ev != null && (sev == null || ev > sev) ? ev : sev;
    }

    function fmtLayerValue(view, v) {
        if (v == null) return 'No data';
        var layer = LAYERS[view.layer];
        return layer ? v.toFixed(layer.decimals) + ' ' + layer.unit : fmtG(v);
    }

    // Blue down, orange up — the Hills ramp's two ends, so the slope reads the
    // same on the timeline, in the breakdown and on the map.
    var TERRAIN = {
        up:    { label: 'Uphill',   icon: 'fa-arrow-trend-up',   colour: LAYERS.gradient.ramp[2] },
        level: { label: 'Level',    icon: 'fa-arrow-right-long', colour: LAYERS.gradient.ramp[1] },
        down:  { label: 'Downhill', icon: 'fa-arrow-trend-down', colour: LAYERS.gradient.ramp[0] }
    };

    // Which way the road was going at a fix, for hover text on the other
    // layers. Empty on Hills, where the value already says it, and wherever
    // the gradient is unknown.
    function slopeNote(view, p) {
        var s = view.layer === 'gradient' ? '' : slopeText(p);
        return s ? ' · ' + s : '';
    }

    function slopeText(p) {
        var kind = terrainOf(p);
        if (!kind) return '';
        if (kind === 'level') return 'level';
        return (kind === 'up' ? '↑ ' : '↓ ') +
               Math.abs(p.gradient_pct).toFixed(1) + '% ' + (kind === 'up' ? 'climb' : 'descent');
    }

    // On a pin, the slope is context for the figure: braking on a descent is
    // a different event from the same braking on the level.
    function slopeLine(p) {
        var s = slopeText(p);
        return s ? '<br><span style="opacity:.75">Road: ' + s + '</span>' : '';
    }

    /**
     * How the trip's speed, braking and acceleration split across uphill,
     * level and downhill road. Absent without a barometer: three dashes would
     * read as a flat drive.
     */
    function terrainBreakdown(view) {
        var t = terrainSummary(view.track);
        if (!t.up && !t.level && !t.down) return '';

        var harsh = {};
        (view.trip.events || []).forEach(function (e) {
            var kind = e.ts == null ? null
                : terrainOf(view.track[nearestIndex(view.track, e.ts)]);
            if (kind) harsh[kind] = (harsh[kind] || 0) + 1;
        });
        function peaks(c, n) {
            if (c.max_brake_mps2 == null && c.max_accel_mps2 == null && !n) return '';
            return '<br><span class="text-nowrap"><span class="text-muted">Brake</span> ' +
                     fmtG(c.max_brake_mps2) + '</span> ' +
                   '<span class="text-nowrap"><span class="text-muted">Accel</span> ' +
                     fmtG(c.max_accel_mps2) + '</span>' +
                   (n ? '<br><span class="text-danger text-nowrap">' + n + ' harsh event' +
                        (n === 1 ? '' : 's') + '</span>' : '');
        }
        var cells = ['up', 'level', 'down'].map(function (k) {
            var c = t[k], m = TERRAIN[k];
            return '<div class="col-4">' +
                     '<span class="text-nowrap" style="color:' + m.colour + '">' +
                       '<i class="fas ' + m.icon + ' me-1"></i><strong>' + m.label + '</strong></span><br>' +
                     (c ? fmtMph(c.avg_speed_mps) + '<br><span class="text-muted">' +
                          fmtMiles(c.distance_m) + '</span>' + peaks(c, harsh[k])
                        : '<span class="text-muted">—</span>') +
                   '</div>';
        }).join('');
        return '<div class="mt-2"><strong>By terrain</strong>' +
                 '<div class="row g-2 mt-0">' + cells + '</div>' +
                 (t.unknown_s >= 60
                   ? '<div class="text-muted mt-1" style="font-size:.75rem">' +
                     fmtDuration(t.unknown_s) + ' not classified — no gradient reading ' +
                     '(below about 11 mph, or no barometer).</div>'
                   : '') +
               '</div>';
    }

    /**
     * Draw the route with a selectable colouring — driving style, speed, road
     * surface or gradient — pins for events, peaks and stops, and a timeline
     * of the same quantity underneath. docs/journeys.md §Map layers.
     *
     * The track is admin-only on the API (coordinates are a tighter privacy
     * boundary than behaviour — see journey_routes.py), so a non-admin gets
     * the events and the summary and simply no map.
     */
    function renderTripMap(tripId, trip, live) {
        var wrap = document.getElementById('trip-map-wrap-' + tripId);
        if (!wrap) return;

        var track = (trip.track || []).filter(function (p) {
            return p.lat != null && p.lon != null;
        });
        if (track.length < 2) return;

        if (typeof L === 'undefined') {
            wrap.innerHTML = '<div class="text-muted small">Map library not loaded.</div>';
            return;
        }

        var active = getMapLayer();
        var buttons = ['style'].concat(Object.keys(LAYERS)).map(function (id) {
            var l = LAYERS[id] || STYLE_LAYER;
            return '<button type="button" class="btn btn-outline-secondary' +
                     (id === active ? ' active' : '') + '" data-map-layer="' + id + '">' +
                     '<i class="fas ' + l.icon + ' me-1"></i>' + l.label + '</button>';
        }).join('');

        var mapId = 'trip-map-' + tripId;
        // flex-wrap throughout: at phone width the picker drops under the
        // heading and the legend under the picker rather than overflowing.
        wrap.innerHTML =
          '<div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-1">' +
            '<strong>Route</strong>' +
            '<div class="btn-group btn-group-sm" role="group" aria-label="Colour the route by">' +
              buttons + '</div>' +
          '</div>' +
          '<div class="small mb-1" data-map-legend></div>' +
          '<div id="' + mapId + '" style="height:320px" class="rounded border"></div>' +
          '<div class="d-flex flex-wrap gap-1 mt-2" data-map-toggles></div>' +
          '<div class="mt-2 d-flex flex-wrap justify-content-between align-items-baseline gap-2">' +
            '<strong data-map-chart-title></strong>' +
            '<span class="small d-none" data-map-chart-key>' +
              ['up', 'down'].map(function (k) {
                  return '<span class="text-nowrap ms-2"><span style="display:inline-block;width:12px;' +
                         'height:10px;opacity:.35;vertical-align:middle;background:' + TERRAIN[k].colour +
                         '"></span> <span class="text-muted">' + TERRAIN[k].label + '</span></span>';
              }).join('') +
            '</span></div><div>' +
            '<div data-map-chart style="height:130px"></div></div>' +
          '<div data-map-terrain></div>';

        var map = L.map(mapId, { scrollWheelZoom: false });
        L.tileLayer('/api/map/tiles/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" ' +
                         'target="_blank" rel="noopener">OpenStreetMap</a> contributors',
        }).addTo(map);
        // Pins get their own pane above the route: the route is redrawn on
        // every layer switch, and in a shared pane it would land on top.
        map.createPane('tripPins').style.zIndex = 450;
        // Stops are the most numerous pin and the least urgent, so they sit
        // under events rather than in the default marker pane above them.
        map.createPane('tripStops').style.zIndex = 440;

        var view = tripViews[tripId] = {
            map: map, wrap: wrap, trip: trip, track: track,
            start: trip.started_at || track[0].ts,
            layer: active,
            evSeg: eventSeverityBySegment(trip, track),
            segs: L.layerGroup().addTo(map),
            groups: {}, hidden: {}, cursor: null, chart: null,
            // Live only: keep the car in view until the user moves the map.
            now: null, follow: !!live,
        };

        // Start and end, so the direction of travel is never ambiguous.
        L.circleMarker([track[0].lat, track[0].lon], {
            radius: 6, color: '#fff', weight: 2, pane: 'tripPins',
            fillColor: '#198754', fillOpacity: 1,
        }).addTo(map).bindPopup('Start');
        var last = track[track.length - 1];
        if (live) {
            view.now = L.circleMarker([last.lat, last.lon], {
                radius: 9, color: '#fff', weight: 3, pane: 'tripPins',
                fillColor: '#0d6efd', fillOpacity: 1,
            }).addTo(map).bindPopup('Now');
            map.on('dragstart', function () { view.follow = false; renderMarkerToggles(view); });
        } else {
            L.circleMarker([last.lat, last.lon], {
                radius: 6, color: '#fff', weight: 2, pane: 'tripPins',
                fillColor: '#212529', fillOpacity: 1,
            }).addTo(map).bindPopup('End');
        }

        drawPins(view);

        wrap.querySelectorAll('[data-map-layer]').forEach(function (btn) {
            btn.onclick = function () {
                view.layer = btn.getAttribute('data-map-layer');
                try { localStorage.setItem(mapLayerKey, view.layer); } catch (e) {}
                wrap.querySelectorAll('[data-map-layer]').forEach(function (b) {
                    b.classList.toggle('active', b === btn);
                });
                drawTripLayer(view);
            };
        });

        drawTripLayer(view);
        bindChartCursor(view);

        var bounds = track.map(function (p) { return [p.lat, p.lon]; });
        map.fitBounds(bounds, { padding: [20, 20] });
        // Same reason as the re-expand path: the container may still be
        // hidden when Leaflet first measures it.
        setTimeout(function () { map.invalidateSize(); map.fitBounds(bounds, { padding: [20, 20] }); }, 60);
    }

    // Everything derived from the track alone. Rebuilt whole on a live update:
    // a peak moves when a harder one is set, so appending would go stale.
    function drawPins(view) {
        Object.keys(view.groups).forEach(function (id) { view.map.removeLayer(view.groups[id]); });
        view.wrap.querySelector('[data-map-terrain]').innerHTML = terrainBreakdown(view);
        view.groups.events = eventMarkers(view);
        view.groups.peaks = peakMarkers(view);
        view.groups.stops = stopMarkers(view);
        renderMarkerToggles(view);
    }

    /** Fold a newer copy of a live trip into its existing map and timeline. */
    function updateLiveView(view, trip) {
        var track = (trip.track || []).filter(function (p) {
            return p.lat != null && p.lon != null;
        });
        if (track.length < 2) return;
        view.trip = trip;
        view.track = track;
        view.evSeg = eventSeverityBySegment(trip, track);
        drawPins(view);
        drawTripLayer(view);
        var last = track[track.length - 1];
        view.now.setLatLng([last.lat, last.lon]);
        if (view.follow) view.map.panTo([last.lat, last.lon]);
    }

    // Everything that depends on the active layer: route, legend, timeline.
    function drawTripLayer(view) {
        view.wrap.querySelector('[data-map-legend]').innerHTML = mapLegend(view);
        drawSegments(view);
        renderTripChart(view);
    }

    // One polyline per segment rather than one per trip: the colour is a
    // property of the stretch of road, and a single line can only carry one.
    // A drive is a few hundred segments, which Leaflet handles comfortably.
    function drawSegments(view) {
        var layer = LAYERS[view.layer];
        var domain = layer && layer.domain(view.track);
        view.segs.clearLayers();
        for (var i = 1; i < view.track.length; i++) {
            var a = view.track[i - 1], b = view.track[i];
            var v = layerValue(view, i);
            var band = layer ? null : ragBand(v);
            L.polyline([[a.lat, a.lon], [b.lat, b.lon]], {
                color: layer ? layerColour(layer, domain, v) : RAG_COLOURS[band],
                weight: band === 'green' ? 4 : layer ? 5 : 6,
                opacity: v == null ? 0.5 : 0.9,
            }).bindTooltip(fmtLayerValue(view, v) + slopeNote(view, b) + ' · ' +
                           minutesInto(view, b.ts) + ' min in',
                           { sticky: true })
              .addTo(view.segs);
        }
    }

    function legendSwatch(band, label) {
        return '<span class="text-nowrap">' +
                 '<span style="display:inline-block;width:14px;height:4px;' +
                   'background:' + RAG_COLOURS[band] + ';vertical-align:middle"></span> ' +
                 '<span class="text-muted">' + label + '</span></span>';
    }

    // Swatches for the banded style layer; a gradient bar with the trip's own
    // scale for a continuous one. "No data" is on both — grey is never a value.
    function mapLegend(view) {
        var layer = LAYERS[view.layer];
        var body;
        if (!layer) {
            body = legendSwatch('green', 'Smooth') + legendSwatch('amber', 'Firm') +
                   legendSwatch('red', 'Harsh');
        } else {
            var d = layer.domain(view.track);
            var ticks = view.layer === 'gradient'
                ? ['↓ ' + d[1] + '%', 'level', '↑ ' + d[1] + '%']
                : [d[0], (d[0] + d[1]) / 2, d[1] + ' ' + layer.unit];
            body =
              '<span style="flex:1 1 9rem;max-width:18rem">' +
                '<span style="display:block;height:8px;border-radius:4px;background:' +
                  'linear-gradient(to right,' + layer.ramp.join(',') + ')"></span>' +
                '<span class="d-flex justify-content-between text-muted" style="font-size:.75rem">' +
                  ticks.map(function (t) { return '<span>' + t + '</span>'; }).join('') +
                '</span>' +
              '</span>';
        }
        return '<div class="d-flex flex-wrap align-items-center justify-content-end gap-3">' +
                 body + legendSwatch('none', 'No data') + '</div>';
    }

    function pinIcon(icon, colour, size) {
        size = size || 22;
        return L.divIcon({
            className: '', iconSize: [size, size], iconAnchor: [size / 2, size / 2],
            popupAnchor: [0, -size / 2],
            html: '<span style="display:flex;align-items:center;justify-content:center;' +
                    'width:' + size + 'px;height:' + size + 'px;border-radius:50%;background:#fff;' +
                    'border:2px solid ' + colour + ';color:' + colour + ';' +
                    'font-size:' + size / 2 + 'px;' +
                    'box-shadow:0 1px 3px rgba(0,0,0,.4)">' +
                    '<i class="fas ' + icon + '"></i></span>'
        });
    }

    /**
     * Pin each event to where the car was when it happened.
     *
     * Events carry their own timestamp but no position — the phone detects
     * them between fixes — so the position is the nearest track point in time.
     * At the 10 s drive cadence that is within a few car lengths, which is
     * ample for "this junction" and is the honest resolution to claim.
     */
    function eventMarkers(view) {
        var group = L.layerGroup(), track = view.track;
        (view.trip.events || []).forEach(function (e) {
            if (e.ts == null) return;
            var best = track[nearestIndex(track, e.ts)];
            // Further from any fix than the gap the closer tolerates means the
            // track has a hole here and the position would be a guess.
            if (!best || Math.abs(best.ts - e.ts) > 60) return;

            var k = eventKinds[e.kind] || eventKinds.harsh;
            var colour = e.kind === 'brake' ? RAG_COLOURS.red : RAG_COLOURS.amber;
            L.circleMarker([best.lat, best.lon], {
                radius: 8, color: '#fff', weight: 2, pane: 'tripPins',
                fillColor: colour, fillOpacity: 1,
            }).addTo(group).bindPopup(
                '<strong>' + escape(k.label) + '</strong><br>' +
                fmtG(e.peak_mps2) + ' peak · ' +
                (e.duration_s == null ? '' : e.duration_s.toFixed(1) + ' s · ') +
                minutesInto(view, e.ts) + ' min into the trip' + slopeLine(best) +
                (slopeForgiven(e)
                    ? '<br>Counts ' + Math.round(e.slope_weight * 100) + '% toward the score — ' +
                      'the ' + (e.gradient_pct > 0 ? 'climb' : 'descent') + ' did the rest'
                    : '')
            );
        });
        return group;
    }

    // Where the trip's peak braking / acceleration / cornering figures were
    // set. Shown however gentle they are: on a smooth drive there are no
    // events to pin, and "where was my hardest stop" still has an answer.
    function peakMarkers(view) {
        var group = L.layerGroup();
        var peaks = findPeaks(view.track);
        [['brake', 'Peak braking', RAG_COLOURS.red],
         ['accel', 'Peak acceleration', RAG_COLOURS.amber],
         ['corner', 'Peak cornering', RAG_COLOURS.amber]].forEach(function (k) {
            var pk = peaks[k[0]];
            if (!pk) return;
            L.marker([pk.point.lat, pk.point.lon], { icon: pinIcon(eventKinds[k[0]].icon, k[2]) })
                .addTo(group).bindPopup(
                    '<strong>' + k[1] + '</strong><br>' + fmtG(pk.value) + ' · ' +
                    minutesInto(view, pk.point.ts) + ' min into the trip' + slopeLine(pk.point));
        });
        return group;
    }

    // fmtDuration rounds to minutes, which turns most junction waits into "0 min".
    function fmtIdle(s) {
        if (!s) return 'Stopped briefly';
        return 'Idled for ' + (s < 90 ? Math.round(s) + ' s' : fmtDuration(s));
    }

    function stopMarkers(view) {
        var group = L.layerGroup();
        findStops(view.track).forEach(function (s) {
            L.marker([s.lat, s.lon], { icon: pinIcon('fa-pause', '#495057', 16),
                                       pane: 'tripStops' })
                .addTo(group).bindPopup(
                    '<strong>Stop</strong><br>' + fmtIdle(s.idle_s) + ' · ' +
                    minutesInto(view, s.ts) + ' min into the trip');
        });
        return group;
    }

    var MARKER_GROUPS = [
        { id: 'events', label: 'Harsh events', icon: 'fa-triangle-exclamation' },
        { id: 'peaks', label: 'Peaks', icon: 'fa-arrow-up-wide-short' },
        { id: 'stops', label: 'Stops', icon: 'fa-pause' },
    ];

    // A chip per non-empty pin group, all on to begin with. Nine stops and a
    // handful of events on a short route is clutter someone has to be able
    // to clear.
    function renderMarkerToggles(view) {
        var host = view.wrap.querySelector('[data-map-toggles]');
        host.innerHTML = MARKER_GROUPS.map(function (g) {
            var n = view.groups[g.id].getLayers().length;
            if (!n) return '';
            var on = !view.hidden[g.id];
            if (on) view.groups[g.id].addTo(view.map);
            return '<button type="button" class="btn btn-sm btn-outline-secondary' +
                     (on ? ' active' : '') + '" ' +
                     'aria-pressed="' + on + '" data-map-toggle="' + g.id + '">' +
                     '<i class="fas ' + g.icon + ' me-1"></i>' + g.label +
                     ' <span class="badge text-bg-secondary">' + n + '</span></button>';
        }).join('') + (view.now
            ? '<button type="button" class="btn btn-sm btn-outline-primary' +
              (view.follow ? ' active' : '') + '" aria-pressed="' + view.follow + '" data-map-follow>' +
              '<i class="fas fa-location-crosshairs me-1"></i>Follow</button>'
            : '');
        var follow = host.querySelector('[data-map-follow]');
        if (follow) follow.onclick = function () {
            view.follow = !view.follow;
            if (view.follow) view.map.panTo(view.now.getLatLng());
            renderMarkerToggles(view);
        };
        host.querySelectorAll('[data-map-toggle]').forEach(function (btn) {
            btn.onclick = function () {
                var id = btn.getAttribute('data-map-toggle');
                var group = view.groups[id];
                var on = !view.map.hasLayer(group);
                view.hidden[id] = !on;
                if (on) group.addTo(view.map); else view.map.removeLayer(group);
                btn.classList.toggle('active', on);
                btn.setAttribute('aria-pressed', String(on));
            };
        });
    }

    var G = 9.80665;

    /**
     * The active layer against time into the trip, in the map's own colours,
     * so a stretch of line on one is recognisably the same stretch on the other.
     */
    function renderTripChart(view) {
        var el = view.wrap.querySelector('[data-map-chart]');
        if (!view.chart) view.chart = createChart(el);
        if (!view.chart) return;

        var layer = LAYERS[view.layer];
        var domain = layer && layer.domain(view.track);
        view.wrap.querySelector('[data-map-chart-title]').textContent = layer
            ? layer.title + ' (' + layer.unit + ')'
            : 'Acceleration over the journey (g)';

        var data = view.track.map(function (p, i) {
            var v = layerValue(view, i);
            return [(p.ts - view.start) / 60, v == null || layer ? v : v / G];
        });

        // Climbs and descents shaded behind the line, so a dip in speed can be
        // read against the hill that caused it. Redundant on Hills itself.
        var shading = view.layer === 'gradient' ? [] : terrainRuns(view.track).map(function (r) {
            return [{ xAxis: (r.from - view.start) / 60,
                      itemStyle: { color: TERRAIN[r.kind].colour, opacity: 0.13 } },
                    { xAxis: (r.to - view.start) / 60 }];
        });
        view.wrap.querySelector('[data-map-chart-key]').classList.toggle('d-none', !shading.length);

        view.chart.setOption({
            animation: false,
            grid: { left: 40, right: 24, top: 10, bottom: 24 },
            tooltip: {
                trigger: 'axis',
                formatter: function (params) {
                    var d = params[0].data;
                    var v = d[1] == null ? null : layer ? d[1] : d[1] * G;
                    return Math.round(d[0]) + ' min · ' + fmtLayerValue(view, v) +
                           slopeNote(view, view.track[params[0].dataIndex]);
                },
            },
            xAxis: {
                type: 'value', min: 0, max: 'dataMax',
                axisLabel: { formatter: function (v) { return Math.round(v) + ' min'; } },
            },
            yAxis: layer
                ? { type: 'value', min: domain[0], max: domain[1],
                    interval: (domain[1] - domain[0]) / 2 }
                : { type: 'value', min: 0, splitNumber: 2 },
            visualMap: layer
                ? { show: false, type: 'continuous', dimension: 1,
                    min: domain[0], max: domain[1], inRange: { color: layer.ramp } }
                : { show: false, type: 'piecewise', dimension: 1, pieces: [
                      { lt: RAG_AMBER / G, color: RAG_COLOURS.green },
                      { gte: RAG_AMBER / G, lt: RAG_RED / G, color: RAG_COLOURS.amber },
                      { gte: RAG_RED / G, color: RAG_COLOURS.red } ] },
            series: [{
                type: 'line', data: data, showSymbol: false,
                lineStyle: { width: 2 }, areaStyle: { opacity: 0.18 },
                markArea: { silent: true, data: shading },
            }],
        });
    }

    /**
     * Pointing at the timeline marks that moment on the map.
     *
     * Bound to the container, not the ECharts instance: chart-utils re-inits
     * the instance on a theme change, which would drop instance listeners.
     */
    function bindChartCursor(view) {
        var el = view.wrap.querySelector('[data-map-chart]');

        function move(ev) {
            if (!view.chart) return;
            var r = el.getBoundingClientRect(), pt;
            try {
                pt = view.chart.instance().convertFromPixel(
                    { gridIndex: 0 }, [ev.clientX - r.left, ev.clientY - r.top]);
            } catch (e) { return; }
            var p = pt && view.track[nearestIndex(view.track, view.start + pt[0] * 60)];
            if (!p) return;
            if (!view.cursor) {
                view.cursor = L.circleMarker([p.lat, p.lon], {
                    radius: 7, color: '#fff', weight: 3, pane: 'tripPins',
                    fillColor: '#0d6efd', fillOpacity: 1, interactive: false,
                });
            }
            view.cursor.setLatLng([p.lat, p.lon]).addTo(view.map);
            if (!view.map.getBounds().contains([p.lat, p.lon])) view.map.panTo([p.lat, p.lon]);
        }

        el.addEventListener('pointermove', move);
        el.addEventListener('pointerdown', move);
        el.addEventListener('pointerleave', function (ev) {
            // A finger lifting is also a leave; keep the mark so a tap sticks.
            if (ev.pointerType === 'mouse' && view.cursor) view.map.removeLayer(view.cursor);
        });
    }

    // Live — drives in progress
    //
    // Polled rather than pushed: the phone reports every ten seconds, so a
    // poll at that cadence is as live as the data, and needs no socket to
    // re-establish when the hub restarts mid-drive.
    var LIVE_POLL_MS = 10000;
    // Longer than this since the last fix and the feed, not the car, has stopped.
    var LIVE_STALE_S = 45;
    var live = [];

    async function fetchLive() {
        try {
            var r = await fetch('/api/journeys/live', { credentials: 'same-origin' });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return (await r.json()).trips || [];
        } catch (e) {
            log.warn('live journeys fetch failed', e);
            return null;
        }
    }

    async function pollLive() {
        var host = document.getElementById('drive-live');
        // offsetParent is null while the Drive tab or the Journeys pane is hidden.
        if (!host || host.offsetParent === null) return;
        var next = await fetchLive();
        // A failed poll keeps the last picture; blanking it would read as "arrived".
        if (!next) return;
        // A card changing which phone it follows is not a drive ending.
        var ended = live.some(function (t) {
            return !next.some(function (n) {
                return n.trip_id === t.trip_id ||
                       (n.duplicate_trip_ids || []).indexOf(t.trip_id) !== -1;
            });
        });
        live = next;
        if (ended) {
            // The finished drive is now a closed trip; the table needs it.
            await fetchJourneys();
            render();
        } else {
            renderLive();
        }
    }

    function userName(id) {
        for (var i = 0; i < presenceUsers.length; i++) {
            if (presenceUsers[i].user_id === id) return presenceUsers[i].display_name || id;
        }
        return id;
    }

    function liveWho(t) {
        var others = t.also_recorded_by || [];
        // Two phones in the car: name the occupants, not a driver. Nothing
        // the hub can see says which of them is at the wheel.
        if (others.length) return [t.user_id].concat(others).map(userName).join(' & ');
        return (t.driver_id && driverName(t.driver_id)) || userName(t.user_id);
    }

    function liveTiles(t) {
        var age = Date.now() / 1000 - t.last_fix_at;
        var stale = age > LIVE_STALE_S
            ? '<div class="small text-warning-emphasis mb-2">' +
              '<i class="fas fa-signal me-1"></i>No fix for ' +
              (age < 90 ? Math.round(age) + ' s' : fmtDuration(age)) +
              ' — the phone may have lost signal. Showing the last known position.</div>'
            : '';
        var slope = slopeText(t);
        return stale +
          '<div class="row g-2">' +
            statTile('Speed now', fmtMph(t.speed_mps)) +
            // Unknown is a dash, never "level": no barometer is not a flat road.
            statTile('Road now', slope ? slope.replace(/^./, function (c) { return c.toUpperCase(); }) : '—') +
            statTile('Distance', fmtMiles(t.distance_m)) +
            statTile('Time', fmtDuration(t.last_fix_at - t.started_at)) +
          '</div>';
    }

    /**
     * Draw a card per drive in progress above the journeys table, from `live`.
     *
     * Cards are kept and updated in place rather than rebuilt, so the map
     * under each keeps its zoom, layer and pin toggles between polls.
     */
    function renderLive() {
        var host = document.getElementById('drive-live');
        if (!host) return;

        Array.prototype.slice.call(host.children).forEach(function (card) {
            var id = card.getAttribute('data-live-trip');
            if (live.some(function (t) { return t.trip_id === id; })) return;
            var v = tripViews[id];
            if (v) {
                try { v.map.remove(); } catch (e) { /* already gone */ }
                if (v.chart) v.chart.dispose();
                delete tripViews[id];
            }
            card.remove();
        });

        live.forEach(function (t) {
            var card = host.querySelector('[data-live-trip="' + t.trip_id + '"]');
            if (!card) {
                card = document.createElement('div');
                card.className = 'border border-primary-subtle rounded p-2 p-sm-3 mb-3 small';
                card.setAttribute('data-live-trip', t.trip_id);
                card.innerHTML =
                  '<div class="d-flex flex-wrap align-items-center gap-2 mb-2">' +
                    '<i class="fas fa-circle fa-beat-fade text-danger" style="font-size:.6rem"></i>' +
                    '<strong>Driving now</strong>' +
                    '<span data-live-who></span>' +
                  '</div>' +
                  '<div data-live-tiles></div>' +
                  '<div id="trip-map-wrap-' + escape(t.trip_id) + '" class="mt-2"></div>';
                host.appendChild(card);
            }
            card.querySelector('[data-live-who]').innerHTML =
                '<span class="text-muted">' + escape(liveWho(t)) + ' · since ' +
                new Date(t.started_at * 1000).toLocaleTimeString(undefined,
                    { hour: '2-digit', minute: '2-digit' }) + '</span>';
            card.querySelector('[data-live-tiles]').innerHTML = liveTiles(t);
            loadLiveTrack(t.trip_id);
        });
    }

    // The track is admin-only, so for anyone else this fetch carries none and
    // the card stays at its tiles — the same boundary as a finished trip.
    async function loadLiveTrack(tripId) {
        var trip;
        try {
            var r = await fetch('/api/journeys/' + encodeURIComponent(tripId),
                                { credentials: 'same-origin' });
            if (!r.ok) return;
            trip = await r.json();
        } catch (e) { return; }
        if (!trip.track) return;
        var view = tripViews[tripId];
        // A view whose markup a re-render has since replaced is not this card's.
        if (view && view.wrap.isConnected) updateLiveView(view, trip);
        else renderTripMap(tripId, trip, true);
    }

    function bindJourneyHandlers() {
        var refresh = document.getElementById('drive-refresh');
        if (refresh) refresh.onclick = async function () {
            await fetchJourneys();
            render();
        };

        var filter = document.getElementById('drive-driver-filter');
        if (filter) filter.onchange = async function () {
            driverFilter = filter.value;
            await fetchJourneys();
            render();
        };

        document.querySelectorAll('[data-trip-driver]').forEach(function (sel) {
            // The select sits inside the expandable detail row, whose parent
            // toggles on click — without this, choosing a driver collapses the
            // row out from under the pointer.
            sel.onclick = function (ev) { ev.stopPropagation(); };
            sel.onchange = async function (ev) {
                ev.stopPropagation();
                var id = sel.getAttribute('data-trip-driver');
                try {
                    var r = await fetch('/api/journeys/' + encodeURIComponent(id) + '/driver', {
                        method: 'PUT', credentials: 'same-origin',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ driver_id: sel.value || null })
                    });
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    await fetchJourneys();
                    render();
                    if (window.toast) {
                        window.toast.success(sel.value
                            ? 'Journey attributed to ' + (driverName(sel.value) || sel.value)
                            : 'Journey unassigned');
                    }
                } catch (e) {
                    if (window.toast) window.toast.error('Could not set driver: ' + (e.message || e));
                }
            };
        });

        document.querySelectorAll('.drive-trip-row').forEach(function (row) {
            row.onclick = function () {
                var id = row.getAttribute('data-trip');
                var d = document.getElementById('trip-detail-' + id);
                if (!d) return;
                d.classList.toggle('d-none');
                if (!d.classList.contains('d-none')) loadTripDetail(id);
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

    // Drivers card — leaderboard over the roster
    //
    // The scores this ranks are only as good as the attribution behind them,
    // so the unranked tail and the unattributed total are shown as part of the
    // table rather than tucked away: a leaderboard that silently omits a third
    // of the driving reads as more authoritative than it has any right to.
    var presenceUsers = [];

    async function fetchPresenceUsers() {
        try {
            var r = await fetch('/api/presence/users', { credentials: 'same-origin' });
            if (r.ok) presenceUsers = (await r.json()).users || [];
        } catch (e) { presenceUsers = []; }
    }

    function medal(rank) {
        if (rank === 1) return '<i class="fas fa-trophy" style="color:#d4af37"></i>';
        if (rank === 2) return '<i class="fas fa-medal" style="color:#9fa6ad"></i>';
        if (rank === 3) return '<i class="fas fa-medal" style="color:#b0703c"></i>';
        return '<span class="text-muted">' + rank + '</span>';
    }

    function driverDot(id) {
        return '<span class="d-inline-block rounded-circle me-2 align-middle" ' +
               'style="width:.7rem;height:.7rem;background:' + escape(driverColour(id)) + '"></span>';
    }

    function leaderboardTable() {
        if (!board || !board.drivers || !board.drivers.length) {
            return '<div class="text-center text-muted py-4">' +
                     'No drivers yet.<br><span class="small">Add one below, and link it ' +
                     'to a presence user to claim that phone\'s journeys automatically.</span>' +
                   '</div>';
        }

        var minMi = miles(board.min_distance_m);
        var rows = board.drivers.map(function (d) {
            var unranked = !d.qualified;
            return '<tr' + (unranked ? ' class="opacity-75"' : '') + '>' +
              '<td class="text-center">' +
                  (d.rank ? medal(d.rank) : '<span class="text-muted">—</span>') + '</td>' +
              '<td>' + driverDot(d.driver_id) + escape(d.name) +
                  (d.active ? '' : ' <span class="badge bg-secondary-subtle text-secondary-emphasis">inactive</span>') +
                  (d.unconfirmed_trip_count
                      ? ' <span class="badge bg-warning-subtle text-warning-emphasis border border-warning-subtle" ' +
                        'title="Journeys attributed automatically but not confirmed">' +
                        '<i class="fas fa-circle-question me-1"></i>' + d.unconfirmed_trip_count +
                        '</span>'
                      : '') +
              '</td>' +
              '<td class="fw-bold fs-6">' +
                  (unranked
                      ? '<span class="text-muted small">not enough data</span>'
                      : fmtScore(d.smoothness_score)) + '</td>' +
              '<td>' + (d.trip_count || 0) + '</td>' +
              '<td>' + fmtMiles(d.total_distance_m) + '</td>' +
              '<td class="d-none d-sm-table-cell">' +
                  (d.events_per_100km == null ? '—' : d.events_per_100km) + '</td>' +
              '<td class="d-none d-md-table-cell">' + fmtMph(d.top_speed_mps) + '</td>' +
            '</tr>';
        }).join('');

        var notes = '<div class="small text-muted mt-2">' +
            '<i class="fas fa-circle-info me-1"></i>' +
            'Ranked on distance-weighted driving style, over at least ' +
            (minMi == null ? '—' : Math.round(minMi)) + ' mi of measured driving.';
        if (board.unattributed_trip_count) {
            notes += ' <strong>' + board.unattributed_trip_count + '</strong> journey' +
                     (board.unattributed_trip_count === 1 ? '' : 's') + ' (' +
                     fmtMiles(board.unattributed_distance_m) +
                     ') not yet attributed to a driver.';
        }
        notes += '</div>';

        return '<div class="table-responsive">' +
          '<table class="table table-sm table-hover mb-0">' +
            '<thead class="table-light"><tr>' +
              '<th class="text-center" style="width:3rem">#</th>' +
              '<th>Driver</th><th>Style</th><th>Trips</th><th>Distance</th>' +
              '<th class="d-none d-sm-table-cell" title="Harsh events per 100 km">Events/100km</th>' +
              '<th class="d-none d-md-table-cell">Top speed</th>' +
            '</tr></thead><tbody>' + rows + '</tbody></table></div>' + notes;
    }

    function userOptions(selected) {
        return '<option value=""' + (selected ? '' : ' selected') +
                 '>— no linked phone —</option>' +
            presenceUsers.map(function (u) {
                return '<option value="' + escape(u.user_id) + '"' +
                       (u.user_id === selected ? ' selected' : '') + '>' +
                       escape(u.display_name || u.user_id) + '</option>';
            }).join('');
    }

    function rosterEditor() {
        var rows = drivers.map(function (d) {
            return '<tr data-driver-row="' + escape(d.driver_id) + '">' +
              '<td>' + driverDot(d.driver_id) +
                  '<input class="form-control form-control-sm d-inline-block" ' +
                    'style="width:9rem" data-dfield="name" value="' + escape(d.name) + '">' +
              '</td>' +
              '<td><select class="form-select form-select-sm" data-dfield="user_id" ' +
                    'style="width:11rem">' + userOptions(d.user_id) + '</select></td>' +
              '<td><input type="color" class="form-control form-control-color form-control-sm" ' +
                    'data-dfield="colour" value="' + escape(driverColour(d.driver_id)) + '"></td>' +
              '<td class="text-center"><div class="form-check form-switch d-inline-block">' +
                  '<input class="form-check-input" type="checkbox" data-dfield="active"' +
                  (d.active ? ' checked' : '') + '></div></td>' +
              '<td class="text-end text-nowrap">' +
                '<button class="btn btn-sm btn-outline-primary me-1" data-save-driver="' +
                    escape(d.driver_id) + '"><i class="fas fa-check"></i></button>' +
                '<button class="btn btn-sm btn-outline-danger" data-del-driver="' +
                    escape(d.driver_id) + '"><i class="fas fa-trash"></i></button>' +
              '</td>' +
            '</tr>';
        }).join('');

        return '<hr class="my-3">' +
          '<h6 class="fw-bold"><i class="fas fa-users me-1"></i> Roster</h6>' +
          '<p class="small text-muted">' +
            'Linking a driver to a presence user attributes that phone\'s journeys to them ' +
            'automatically, including ones already recorded. Where two linked phones travel ' +
            'together the hub records one journey and marks it for confirmation, because ' +
            'nothing it can see says which of the two was driving.' +
          '</p>' +
          (drivers.length
            ? '<div class="table-responsive"><table class="table table-sm align-middle mb-2">' +
                '<thead class="table-light"><tr><th>Name</th><th>Linked phone</th>' +
                '<th>Colour</th><th class="text-center">Active</th><th></th></tr></thead>' +
                '<tbody>' + rows + '</tbody></table></div>'
            : '') +
          '<div class="row g-2 align-items-end">' +
            '<div class="col-6 col-sm-3">' +
              '<label class="form-label small mb-1">Name</label>' +
              '<input class="form-control form-control-sm" id="new-driver-name" placeholder="Kate">' +
            '</div>' +
            '<div class="col-6 col-sm-3">' +
              '<label class="form-label small mb-1">Linked phone</label>' +
              '<select class="form-select form-select-sm" id="new-driver-user">' +
                  userOptions(null) + '</select>' +
            '</div>' +
            '<div class="col-auto">' +
              '<button class="btn btn-sm btn-primary" id="add-driver">' +
                '<i class="fas fa-plus me-1"></i>Add driver</button>' +
            '</div>' +
          '</div>';
    }

    function driversCard() {
        return '<div class="card shadow-sm h-100">' +
          '<div class="card-header bg-light py-2 d-flex justify-content-between align-items-center">' +
            '<span class="fw-bold"><i class="fas fa-trophy me-1"></i> Drivers</span>' +
            '<button class="btn btn-sm btn-outline-secondary" id="drivers-refresh">' +
              '<i class="fas fa-rotate"></i></button>' +
          '</div>' +
          '<div class="card-body">' + leaderboardTable() + rosterEditor() + '</div>' +
        '</div>';
    }

    async function saveDriver(body) {
        var r = await fetch('/api/journeys/drivers', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    }

    function bindDriverHandlers() {
        var refresh = document.getElementById('drivers-refresh');
        if (refresh) refresh.onclick = async function () {
            await fetchJourneys();
            render();
        };

        var add = document.getElementById('add-driver');
        if (add) add.onclick = async function () {
            var name = (document.getElementById('new-driver-name') || {}).value || '';
            name = name.trim();
            if (!name) {
                if (window.toast) window.toast.error('Give the driver a name');
                return;
            }
            // Slug from the name; the id is internal and never shown, so it
            // only has to be stable and unique, not pretty.
            var id = name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '')
                         .slice(0, 32) || 'driver_' + Date.now().toString(36).slice(-6);
            var userId = (document.getElementById('new-driver-user') || {}).value || null;
            try {
                var res = await saveDriver({
                    driver_id: id, name: name, user_id: userId || null,
                    colour: driverColour(id), active: true
                });
                await fetchJourneys();
                render();
                if (window.toast) {
                    window.toast.success(res.claimed
                        ? 'Driver added — claimed ' + res.claimed + ' existing journey' +
                          (res.claimed === 1 ? '' : 's')
                        : 'Driver added');
                }
            } catch (e) {
                if (window.toast) window.toast.error('Could not add driver: ' + (e.message || e));
            }
        };

        document.querySelectorAll('[data-save-driver]').forEach(function (btn) {
            btn.onclick = async function () {
                var id = btn.getAttribute('data-save-driver');
                var row = document.querySelector('[data-driver-row="' + id + '"]');
                if (!row) return;
                function field(n) { return row.querySelector('[data-dfield="' + n + '"]'); }
                try {
                    await saveDriver({
                        driver_id: id,
                        name: field('name').value.trim(),
                        user_id: field('user_id').value || null,
                        colour: field('colour').value,
                        active: field('active').checked
                    });
                    await fetchJourneys();
                    render();
                    if (window.toast) window.toast.success('Driver saved');
                } catch (e) {
                    if (window.toast) window.toast.error('Save failed: ' + (e.message || e));
                }
            };
        });

        document.querySelectorAll('[data-del-driver]').forEach(function (btn) {
            btn.onclick = async function () {
                var id = btn.getAttribute('data-del-driver');
                var d = driverById(id);
                if (window.zbmConfirm && !await window.zbmConfirm({
                    title: 'Remove driver',
                    message: 'Remove ' + ((d && d.name) || id) +
                             ' from the roster? Their journeys are kept but become ' +
                             'unattributed.',
                    confirmText: 'Remove', variant: 'danger'
                })) return;
                try {
                    var r = await fetch('/api/journeys/drivers/' + encodeURIComponent(id),
                                        { method: 'DELETE', credentials: 'same-origin' });
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    if (driverFilter === id) driverFilter = '';
                    await fetchJourneys();
                    render();
                    if (window.toast) window.toast.success('Driver removed');
                } catch (e) {
                    if (window.toast) window.toast.error('Remove failed: ' + (e.message || e));
                }
            };
        });
    }

    // Fuel card
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
            // phone width leaves the place field about six characters wide.
            '<div class="row g-2 align-items-end">' +
              '<div class="col-12 col-sm-5">' +
                '<label class="form-label small fw-bold mb-1">Fuel</label>' +
                '<select class="form-select form-select-sm" id="fuel-type">' + opts + '</select>' +
              '</div>' +
              '<div class="col-6 col-sm-4">' +
                // Not "Postcode": outside the UK the useful search is often a
                // town or street, and the geocoder takes either.
                '<label class="form-label small fw-bold mb-1">Place</label>' +
                '<input class="form-control form-control-sm" id="fuel-postcode" ' +
                  'placeholder="home" value="' + escape(prefs.postcode) + '" maxlength="120" ' +
                  'autocomplete="postal-code">' +
              '</div>' +
              // A radius means nothing when the answer is one figure for a
              // whole state, so the slider is left out rather than shown
              // doing nothing.
              (fuelStationLevel
                ? '<div class="col-6 col-sm-3">' +
                    '<label class="form-label small fw-bold mb-1">' +
                      'Within <span id="fuel-radius-label">' +
                        escape(fuelRadiusLabel(Number(prefs.radius))) + '</span></label>' +
                    '<input type="range" class="form-range" id="fuel-radius" ' +
                      'min="2" max="40" step="1" value="' + prefs.radius + '">' +
                  '</div>'
                : '') +
            '</div>' +
            '<div class="d-grid mt-2">' +
              '<button class="btn btn-sm btn-primary" id="fuel-search">' +
                '<i class="fas fa-magnifying-glass me-1"></i> ' +
                (fuelStationLevel ? 'Find cheapest' : 'Show average') + '</button>' +
            '</div>' +
            '<div class="small text-muted mt-1" id="fuel-note">' +
              (fuelStationLevel
                ? 'Leave blank to search around home. '
                : 'Leave blank to use the hub\'s location. This region publishes ' +
                  'an area average rather than individual stations. ') +
              escape(fuelAttribution || 'Prices come from published open data.') +
            '</div>' +
            '<div class="mt-3" id="fuel-results"></div>' +
          '</div>' +
        '</div>';
    }

    function bindFuelHandlers() {
        var radius = document.getElementById('fuel-radius');
        var radiusLabel = document.getElementById('fuel-radius-label');
        if (radius && radiusLabel) {
            radius.oninput = function () {
                radiusLabel.textContent = fuelRadiusLabel(Number(radius.value));
            };
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
            '<span class="small">(the first search of the day loads the feed; can take ~20 s)</span></div>';
        if (btn) btn.disabled = true;

        var qs = '?fuel=' + encodeURIComponent(fuel) +
                 '&radius_km=' + encodeURIComponent(radius) + '&limit=10' +
                 (postcode ? '&q=' + encodeURIComponent(postcode) : '');
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

    // Price-history chart
    // The snapshots fuel_history.py records at every search, drawn as a
    // daily MEDIAN line over a shaded MIN–MAX band. One measure, one axis
    // (price per litre over time); single series so the card title is the legend.
    //
    // Colour: the same blue the Energy tab uses for its "cheap" pole,
    // validated (dataviz six-checks) against both card surfaces.
    var historyChart = null;

    // Pence to one decimal — the way a forecourt sign quotes it, and the way
    // the price is actually set. Prices are stored per-litre in pounds, so the
    // conversion happens here rather than in the data: history rows already on
    // disk are in pounds and must keep meaning the same thing.
    //
    function minorSuffix(u) { return MINOR_SUFFIX[(u || fuelUnits).currency] || 'c'; }

    function volumeLabel(u) {
        return (u || fuelUnits).volume === 'gal_us' ? 'gal' : 'L';
    }

    var _priceFmt = null, _priceFmtKey = '';
    function priceFormatter(u) {
        var key = u.currency + '/' + u.decimals;
        if (_priceFmtKey !== key) {
            _priceFmtKey = key;
            try {
                _priceFmt = new Intl.NumberFormat(undefined, {
                    style: 'currency', currency: u.currency,
                    minimumFractionDigits: u.decimals,
                    maximumFractionDigits: u.decimals });
            } catch (e) {
                // An unknown currency code throws rather than degrading, and a
                // price is too important to drop over a formatting detail.
                _priceFmt = null;
            }
        }
        return _priceFmt;
    }

    // A price, in the region's own terms. `value` is always in the major
    // currency unit.
    //
    // The decimal is not decoration. Pump prices are set to a tenth of the
    // smallest unit and in practice always end in .9, so dropping it rounds
    // 159.9 to 160 — dearer than the station charges, and identical for two
    // stations a penny apart.
    function fuelPrice(value, u) {
        u = u || fuelUnits;
        if (value == null || isNaN(value)) return '—';
        if (u.display_scale === 'minor') {
            return (value * 100).toFixed(1) + minorSuffix(u);
        }
        var f = priceFormatter(u);
        return f ? f.format(value)
                 : (u.symbol || '') + Number(value).toFixed(u.decimals);
    }

    // A gap between two prices. No currency symbol: it always sits beside a
    // formatted price that already carries one.
    function fuelDelta(value, u) {
        u = u || fuelUnits;
        return u.display_scale === 'minor'
            ? (value * 100).toFixed(1) + minorSuffix(u)
            : Number(value).toFixed(u.decimals);
    }

    // Bare numbers for a chart axis, in whichever scale is being displayed.
    function fuelAxisLabel(value, u) {
        u = u || fuelUnits;
        return u.display_scale === 'minor'
            ? (value * 100).toFixed(1)
            : Number(value).toFixed(u.decimals);
    }

    function fuelPriceHeader(u) {
        u = u || fuelUnits;
        var unit = u.display_scale === 'minor' ? minorSuffix(u)
                                               : (u.currency || '');
        return 'Price/' + volumeLabel(u) + ' (' + unit + ')';
    }

    // Distances are kilometres on the wire everywhere; only the display varies.
    function fuelDistance(km, u) {
        u = u || fuelUnits;
        if (km == null) return '—';
        return u.distance === 'mi'
            ? (km / 1.609344).toFixed(1) + ' mi'
            : km.toFixed(1) + ' km';
    }

    function fuelRadiusLabel(km, u) {
        u = u || fuelUnits;
        return u.distance === 'mi'
            ? Math.round(km / 1.609344) + ' mi'
            : km + ' km';
    }

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
                        'Median ' + fuelPrice(s.median) + '<br>' +
                        'Range ' + fuelPrice(s.min) + ' – ' + fuelPrice(s.max) + '<br>' +
                        s.stations + ' station(s)';
                },
            },
            xAxis: { type: 'category', data: daysAxis, boundaryGap: false },
            yAxis: {
                type: 'value', scale: true,
                axisLabel: { formatter: function (v) { return fuelAxisLabel(v); } },
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
                  '<strong>' + fuelPrice(h.cheapest_seen.price, h.units) + '</strong> — ' +
                  escape(h.cheapest_seen.brand || '?') + ' ' +
                  escape(h.cheapest_seen.postcode || '') +
                  ' (' + escape(h.cheapest_seen.day) + ')' +
                '</div>';
        } catch (e) { /* history is a bonus, never an error */ }
    }

    // An area average is not a station, and must not be dressed up as one.
    // The US is the only region like this — no free station-level feed exists
    // there — so instead of a cheapest-first table it gets one figure, named
    // for the area it covers and dated, because it can be a week old.
    function renderFuelAverage(out, data) {
        var s = (data.stations || [])[0];
        if (!s) {
            out.innerHTML = '<div class="text-center text-muted py-3">' +
                'No average published for ' + escape(data.fuel_label || data.fuel) +
                ' in this area.</div>';
            return;
        }
        var alternatives = Object.keys(s.prices || {})
            .filter(function (k) { return k !== data.fuel; })
            .map(function (k) {
                return '<span class="me-3">' + escape(fuelTypes[k] || k) + ' ' +
                       '<strong>' + fuelPrice(s.prices[k]) + '</strong></span>';
            }).join('');

        out.innerHTML =
          '<div class="card border-0 bg-body-tertiary">' +
            '<div class="card-body text-center py-4">' +
              '<div class="small text-muted mb-1">Average price in ' +
                escape(s.brand || 'your area') + '</div>' +
              '<div class="display-6 fw-bold">' + fuelPrice(s.price) + '</div>' +
              '<div class="small text-muted">' +
                escape(data.fuel_label || data.fuel) + ' per ' + volumeLabel() +
                (s.last_updated
                    ? ' — week ending ' + escape(s.last_updated) : '') +
              '</div>' +
              (alternatives
                  ? '<div class="small mt-3">' + alternatives + '</div>' : '') +
            '</div>' +
          '</div>' +
          '<div class="small text-muted mt-2">' +
            'This is a published average, not a station. No station-level price ' +
            'feed exists for this region, so there is nowhere to navigate to and ' +
            'the figure can be up to a week old.' +
          '</div>' +
          '<div id="fuel-trend"></div>';
    }

    function renderFuelResults(out, data) {
        // The response is what the region actually answered with, so it wins
        // over whatever was cached when the tab was first opened.
        if (data.units) fuelUnits = data.units;
        if (data.attribution) fuelAttribution = data.attribution;
        if (data.station_level === false) { renderFuelAverage(out, data); return; }
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
              '<td class="text-nowrap">' + fuelPrice(s.price) +
                (i > 0 && delta > 0.001
                    ? '<br><span class="small text-muted">+' + fuelDelta(delta) + '</span>'
                    : '<br><span class="small text-success fw-bold">cheapest</span>') +
              '</td>' +
              '<td class="small text-nowrap">' + fuelDistance(s.distance_km) + '</td>' +
              '<td class="text-nowrap">' +
                '<a class="btn btn-sm btn-outline-primary" target="_blank" rel="noopener" ' +
                   'href="' + escape(s.maps_url) + '" title="Open in Google Maps">' +
                  '<i class="fas fa-map-location-dot me-1"></i>' + escape(s.postcode || 'Map') +
                '</a>' +
              '</td>' +
            '</tr>';
        }).join('');

        var centre = data.centre || {};
        // 'place' covers a postcode or a place name — outside the UK the
        // search box is not postcode-shaped, so the wording cannot be either.
        var centreNote = centre.source === 'home' ? 'around home'
                       : centre.source === 'place' ? 'around that location'
                       : 'around the given point';
        out.innerHTML =
          '<div class="small text-muted mb-1">' +
            data.count + ' station(s) with ' + escape(data.fuel_label || data.fuel) +
            ' ' + centreNote + ' — cheapest first. Tap a station for directions.' +
          '</div>' +
          '<div class="table-responsive">' +
            '<table class="table table-sm table-hover align-middle mb-0">' +
              '<thead class="table-light"><tr>' +
                '<th>Station</th><th>' + escape(fuelPriceHeader()) + '</th><th>Dist</th><th>Maps</th>' +
              '</tr></thead><tbody>' + rows + '</tbody>' +
            '</table>' +
          '</div>' +
          '<div id="fuel-trend"></div>';
    }

    // Public init
    var initialised = false;

    window.initDriveTab = async function () {
        // Re-fetch on every tab open (cheap), but only build fuel types once.
        if (!initialised) {
            initialised = true;
            await fetchFuelTypes();
            setInterval(whileVisible(pollLive), LIVE_POLL_MS);
        }
        // Presence users only feed the roster's "linked phone" dropdown, so
        // they are refreshed alongside the journeys rather than cached: a user
        // added in Settings should be linkable without a reload.
        var fetched = await Promise.all([fetchJourneys(), fetchPresenceUsers(), fetchLive()]);
        if (fetched[2]) live = fetched[2];
        render();
    };
})();
