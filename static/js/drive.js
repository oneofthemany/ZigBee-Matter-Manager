/* ============================================================
   ZMM — Drive tab: journeys + cheapest fuel nearby
   ============================================================
   Two halves, one page:
     - Journeys: trips recorded by the companion app's drive mode
       (car Bluetooth), with distance and speed statistics.
     - Fuel: cheapest stations near home or a typed postcode, by
       fuel type, with a Google Maps link per station.

   Same conventions as presence-settings.js: IIFE, window.init*
   entry point, string-built Bootstrap markup, no framework.
   ============================================================ */

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
        return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' }) +
               ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    }

    function placeName(p) {
        if (!p || p === 'away') return 'Away';
        if (p === 'home') return 'Home';
        return p.replace(/[_-]+/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
    }

    function getFuelPrefs() {
        try {
            var raw = localStorage.getItem(fuelPrefsKey);
            if (raw) return JSON.parse(raw);
        } catch (e) {}
        return { fuel: 'E10', postcode: '', radius: 8 };
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
    function render() {
        var host = document.getElementById(HOST_ID);
        if (!host) return;
        host.innerHTML =
            '<div class="row g-3">' +
              '<div class="col-lg-7">' + journeysCard() + '</div>' +
              '<div class="col-lg-5">' + fuelCard() + '</div>' +
            '</div>';
        bindJourneyHandlers();
        bindFuelHandlers();
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
            var rows = trips.map(function (t) {
                return '<tr class="drive-trip-row" data-trip="' + escape(t.trip_id) + '" style="cursor:pointer">' +
                  '<td class="small">' + fmtWhen(t.started_at) + '</td>' +
                  '<td class="small">' + escape(placeName(t.start_place)) +
                      ' <i class="fas fa-arrow-right text-muted mx-1"></i> ' +
                      escape(placeName(t.end_place)) + '</td>' +
                  '<td>' + fmtMiles(t.distance_m) + '</td>' +
                  '<td class="small">' + fmtDuration(t.duration_s) + '</td>' +
                  '<td>' + fmtMph(t.avg_speed_mps) + '</td>' +
                  '<td>' + fmtMph(t.max_speed_mps) + '</td>' +
                '</tr>' +
                '<tr class="d-none" id="trip-detail-' + escape(t.trip_id) + '">' +
                  '<td colspan="6" class="bg-light small p-3">' + tripDetail(t) + '</td>' +
                '</tr>';
            }).join('');
            body =
            '<div class="table-responsive">' +
              '<table class="table table-sm table-hover mb-0">' +
                '<thead class="table-light"><tr>' +
                  '<th>When</th><th>Route</th><th>Distance</th><th>Time</th>' +
                  '<th>Avg</th><th>Max</th>' +
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
        return '<div class="row g-2">' +
          '<div class="col-sm-3"><strong>Min speed</strong><br>' + fmtMph(t.min_speed_mps) + '</div>' +
          '<div class="col-sm-3"><strong>Speed σ</strong><br>' +
              (sd == null ? '—' : sd.toFixed(1) + ' mph') + '</div>' +
          '<div class="col-sm-3"><strong>Fixes</strong><br>' + (t.fix_count || '—') + '</div>' +
          '<div class="col-sm-3 text-end">' +
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
            '<div class="row g-2 align-items-end">' +
              '<div class="col-5">' +
                '<label class="form-label small fw-bold mb-1">Fuel</label>' +
                '<select class="form-select form-select-sm" id="fuel-type">' + opts + '</select>' +
              '</div>' +
              '<div class="col-4">' +
                '<label class="form-label small fw-bold mb-1">Postcode</label>' +
                '<input class="form-control form-control-sm" id="fuel-postcode" ' +
                  'placeholder="home" value="' + escape(prefs.postcode) + '" maxlength="10">' +
              '</div>' +
              '<div class="col-3">' +
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

        saveFuelPrefs({ fuel: fuel, postcode: postcode, radius: Number(radius) });

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
        } catch (e) {
            out.innerHTML = '<div class="alert alert-warning py-2 small mb-0">' +
                escape(e.message || String(e)) + '</div>';
        } finally {
            if (btn) btn.disabled = false;
        }
    }

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
