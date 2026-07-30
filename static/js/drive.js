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
        disposeTripMaps();              // and so are the trip maps

        var tabs = [
            { id: 'journeys', icon: 'fa-route', label: 'Journeys' },
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
              pane('fuel', fuelCard()) +
              pane('history', historyCard()) +
              pane('apiary', apiaryCard()) +
            '</div>';

        bindJourneyHandlers();
        bindFuelHandlers();
        bindHistoryHandlers();

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

    // ----------------------------------------------------------
    // Apiary pane — hosts places-settings.js (moved from Settings →
    // Presence). That module owns everything inside
    // #places-settings-host; this pane just provides the card.
    // ----------------------------------------------------------
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
                  '<td class="small">' + fmtWhen(t.started_at) + '</td>' +
                  '<td class="small">' + escape(placeName(t.start_place)) +
                      ' <i class="fas fa-arrow-right text-muted mx-1"></i> ' +
                      escape(placeName(t.end_place)) + '</td>' +
                  '<td>' + fmtMiles(t.distance_m) + '</td>' +
                  '<td class="small d-none d-sm-table-cell">' + fmtDuration(t.duration_s) + '</td>' +
                  '<td>' + fmtMph(t.avg_speed_mps) + '</td>' +
                  '<td class="d-none d-sm-table-cell">' + fmtMph(t.max_speed_mps) + '</td>' +
                  '<td class="d-none d-md-table-cell fw-bold">' +
                      fmtScore(t.smoothness_score) + '</td>' +
                '</tr>' +
                '<tr class="d-none" id="trip-detail-' + escape(t.trip_id) + '">' +
                  '<td colspan="7" class="bg-light small p-3">' + tripDetail(t) + '</td>' +
                '</tr>';
            }).join('');
            body =
            '<div class="table-responsive">' +
              '<table class="table table-sm table-hover mb-0">' +
                '<thead class="table-light"><tr>' +
                  '<th>When</th><th>Route</th><th>Distance</th>' +
                  '<th class="d-none d-sm-table-cell">Time</th>' +
                  '<th>Avg</th><th class="d-none d-sm-table-cell">Max</th>' +
                  '<th class="d-none d-md-table-cell">Style</th>' +
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
        '</div>' +
        behaviourDetail(t) +
        // Filled in on first expand — see loadTripDetail. The track and events
        // are the only parts of a trip not already in the list response, and
        // fetching every trip's up front would be a hundred requests to render
        // a table.
        '<div id="trip-events-' + escape(t.trip_id) + '" class="mt-2"></div>' +
        '<div id="trip-map-wrap-' + escape(t.trip_id) + '" class="mt-2"></div>';
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
          cell('Harsh events', counts) +
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
          cell('Climb', t.climb_m == null ? '—' : Math.round(t.climb_m) + ' m') +
        '</div>';
    }

    // Red / amber / green thresholds for acceleration, m/s².
    //
    // Red is the phone's own event threshold (MotionSampler.EVENT_ENTER_MPS2):
    // above it the sampler logged a discrete event, so the map agrees with the
    // event list by construction rather than by coincidence. Amber is the
    // approach to it — firm but not logged — which is the band worth showing a
    // driver, because it is where a habit is visible before it becomes an
    // event. Change these together with the phone's constant or the two
    // stories stop matching.
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

    /** Worst acceleration seen in the window ending at a track point. */
    function pointSeverity(p) {
        if (!p) return null;
        var lon = p.long_peak_mps2 == null ? null : Math.abs(p.long_peak_mps2);
        var lat = p.lat_peak_mps2 == null ? null : Math.abs(p.lat_peak_mps2);
        if (lon == null && lat == null) return null;
        return Math.max(lon || 0, lat || 0);
    }

    function ragBand(sev) {
        if (sev == null) return 'none';
        if (sev >= RAG_RED) return 'red';
        if (sev >= RAG_AMBER) return 'amber';
        return 'green';
    }

    /**
     * Fetch and render one trip's events, coaching note and map.
     *
     * Runs once per trip per page load; the rendered markup and the Leaflet
     * instance are left in place so collapsing and re-expanding a row costs
     * nothing.
     */
    async function loadTripDetail(tripId) {
        var host = document.getElementById('trip-events-' + tripId);
        if (!host) return;
        if (host.dataset.loaded) {
            // Leaflet measures the container when it is created. If that
            // happened while the row was collapsed the map is sized to zero
            // and renders as grey; re-measuring on every expand is the
            // documented fix and costs nothing when the size is unchanged.
            var existing = mapRegistry[tripId];
            if (existing) setTimeout(function () { existing.invalidateSize(); }, 0);
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
                       ' <span class="opacity-75">@ ' + into + ' min</span>' +
                     '</span>';
          }).join('') +
          '</div>' +
          coachingNote(evs);
    }

    /**
     * One sentence on what to work on, from whichever event kind dominates.
     *
     * The counts alone tell a driver what happened; this is the part that says
     * what to do about it, which is the whole point of showing them at all.
     * Withheld below three events — two hard stops on one trip is traffic, not
     * a habit, and advice given on that evidence teaches drivers to distrust
     * the rest of it.
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

    // Live Leaflet instances, keyed by trip. Kept so an expand can re-measure
    // one rather than build a second on top of it.
    var mapRegistry = {};

    /**
     * Tear down every trip map before the host's innerHTML is replaced.
     *
     * Leaflet attaches listeners to window and document, not only to its
     * container, so dropping the container's markup leaves those live and the
     * instance uncollectable. Every refresh, delete or sub-tab switch
     * re-renders, so leaking one map per expanded row adds up over a session.
     */
    function disposeTripMaps() {
        Object.keys(mapRegistry).forEach(function (id) {
            try { mapRegistry[id].remove(); } catch (e) { /* already gone */ }
        });
        mapRegistry = {};
    }

    /**
     * Draw the route, coloured green/amber/red by how it was driven, with a
     * pin at every logged event.
     *
     * The track is admin-only on the API (coordinates are a tighter privacy
     * boundary than behaviour — see journey_routes.py), so a non-admin gets
     * the events and the summary and simply no map.
     */
    function renderTripMap(tripId, trip) {
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

        var mapId = 'trip-map-' + tripId;
        wrap.innerHTML =
          '<div class="d-flex justify-content-between align-items-center mb-1">' +
            '<strong>Route</strong>' +
            '<span class="small">' +
              legendSwatch('green', 'Smooth') + legendSwatch('amber', 'Firm') +
              legendSwatch('red', 'Harsh') +
            '</span>' +
          '</div>' +
          '<div id="' + mapId + '" style="height:320px" class="rounded border"></div>';

        var map = L.map(mapId, { scrollWheelZoom: false });
        mapRegistry[tripId] = map;
        L.tileLayer('/api/map/tiles/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" ' +
                         'target="_blank" rel="noopener">OpenStreetMap</a> contributors',
        }).addTo(map);

        // One polyline per segment rather than one per trip: the colour is a
        // property of the stretch of road, and a single line can only carry
        // one. A drive is a few hundred segments, which Leaflet handles
        // comfortably.
        var bounds = [];
        for (var i = 1; i < track.length; i++) {
            var a = track[i - 1], b = track[i];
            var band = ragBand(pointSeverity(b));
            L.polyline([[a.lat, a.lon], [b.lat, b.lon]], {
                color: RAG_COLOURS[band],
                weight: band === 'green' ? 4 : 6,
                opacity: band === 'none' ? 0.5 : 0.9,
            }).addTo(map);
            bounds.push([a.lat, a.lon]);
        }
        bounds.push([track[track.length - 1].lat, track[track.length - 1].lon]);

        // Start and end, so the direction of travel is never ambiguous.
        L.circleMarker([track[0].lat, track[0].lon], {
            radius: 6, color: '#fff', weight: 2,
            fillColor: '#198754', fillOpacity: 1,
        }).addTo(map).bindPopup('Start');
        var last = track[track.length - 1];
        L.circleMarker([last.lat, last.lon], {
            radius: 6, color: '#fff', weight: 2,
            fillColor: '#212529', fillOpacity: 1,
        }).addTo(map).bindPopup('End');

        addEventMarkers(map, trip, track);

        map.fitBounds(bounds, { padding: [20, 20] });
        // Same reason as the re-expand path: the container may still be
        // hidden when Leaflet first measures it.
        setTimeout(function () { map.invalidateSize(); map.fitBounds(bounds, { padding: [20, 20] }); }, 60);
    }

    function legendSwatch(band, label) {
        return '<span class="ms-2 text-nowrap">' +
                 '<span style="display:inline-block;width:14px;height:4px;' +
                   'background:' + RAG_COLOURS[band] + ';vertical-align:middle"></span> ' +
                 '<span class="text-muted">' + label + '</span></span>';
    }

    /**
     * Pin each event to where the car was when it happened.
     *
     * Events carry their own timestamp but no position — the phone detects
     * them between fixes — so the position is the nearest track point in time.
     * At the 10 s drive cadence that is within a few car lengths, which is
     * ample for "this junction" and is the honest resolution to claim.
     */
    function addEventMarkers(map, trip, track) {
        (trip.events || []).forEach(function (e) {
            if (e.ts == null) return;
            var best = null, bestGap = Infinity;
            for (var i = 0; i < track.length; i++) {
                var gap = Math.abs(track[i].ts - e.ts);
                if (gap < bestGap) { bestGap = gap; best = track[i]; }
            }
            // Further from any fix than the gap the closer tolerates means the
            // track has a hole here and the position would be a guess.
            if (!best || bestGap > 60) return;

            var k = eventKinds[e.kind] || eventKinds.harsh;
            var colour = e.kind === 'brake' ? RAG_COLOURS.red : RAG_COLOURS.amber;
            var into = Math.max(0, Math.round((e.ts - (trip.started_at || e.ts)) / 60));
            L.circleMarker([best.lat, best.lon], {
                radius: 8, color: '#fff', weight: 2,
                fillColor: colour, fillOpacity: 1,
            }).addTo(map).bindPopup(
                '<strong>' + escape(k.label) + '</strong><br>' +
                fmtG(e.peak_mps2) + ' peak · ' +
                (e.duration_s == null ? '' : e.duration_s.toFixed(1) + ' s · ') +
                into + ' min into the trip'
            );
        });
    }

    function bindJourneyHandlers() {
        var refresh = document.getElementById('drive-refresh');
        if (refresh) refresh.onclick = async function () {
            await fetchJourneys();
            render();
        };

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
