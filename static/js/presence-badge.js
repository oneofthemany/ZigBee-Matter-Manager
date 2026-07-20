/* Presence badge + map modal.
 *
 * A count in the header ("2 home"), and a map on click showing where each
 * tracked user is.
 *
 * Tiles are proxied through /api/map/tiles rather than fetched from a public
 * tile server. That keeps the coordinates being viewed between the browser and
 * this hub, and means the map still works over a tunnel or VPN where the
 * client may have no direct internet access.
 */
(function () {
    'use strict';

    var POLL_MS = 60000;          // presence changes on the order of minutes
    var state = { users: [], map: null, markers: [], timer: null };

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function canSee() {
        var a = window.zmmAuth;
        return !!(a && a.whoami() &&
                  (a.hasScope('admin') || a.hasScope('presence:read')));
    }

    async function fetchUsers() {
        try {
            var r = await fetch('/api/presence/users', {
                credentials: 'same-origin', cache: 'no-store',
            });
            if (!r.ok) return false;
            var j = await r.json();
            state.users = (j.users || []).filter(function (u) { return u.enabled !== false; });
            return true;
        } catch (e) {
            return false;
        }
    }

    function presenceOf(u) {
        return (u.state && u.state.presence) || 'unknown';
    }

    // ---- badge ------------------------------------------------------------

    function renderBadge() {
        var host = document.getElementById('presence-badge-host');
        if (!host) return;

        if (!canSee() || !state.users.length) { host.innerHTML = ''; return; }

        var home = state.users.filter(function (u) { return presenceOf(u) === 'home'; }).length;
        var unknown = state.users.filter(function (u) { return presenceOf(u) === 'unknown'; }).length;

        // Amber when nobody's state is trustworthy — a "0 home" that actually
        // means "we don't know" would otherwise read as everyone being out.
        var cls = home > 0 ? 'bg-success' : (unknown === state.users.length ? 'bg-warning text-dark' : 'bg-secondary');
        var label = (unknown === state.users.length && state.users.length)
            ? 'presence unknown'
            : home + ' home';

        host.innerHTML =
            '<button class="btn btn-sm btn-link p-0 border-0" id="presence-badge-btn" ' +
                'title="Show where everyone is" aria-label="Presence: ' + esc(label) + '. Show where everyone is.">' +
              '<span class="badge ' + cls + '">' +
                '<i class="fas fa-location-dot"></i><span class="ms-1 d-none d-sm-inline">' + esc(label) + '</span>' +
              '</span>' +
            '</button>';

        var btn = document.getElementById('presence-badge-btn');
        if (btn) btn.onclick = openModal;
    }

    // ---- modal ------------------------------------------------------------

    function stateBadge(p) {
        if (p === 'home') return '<span class="badge bg-success">home</span>';
        if (p === 'away') return '<span class="badge bg-secondary">away</span>';
        return '<span class="badge bg-warning text-dark">unknown</span>';
    }

    function ago(ts) {
        if (!ts) return 'never';
        var s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
        if (s < 90) return s + 's ago';
        if (s < 5400) return Math.round(s / 60) + ' min ago';
        if (s < 172800) return Math.round(s / 3600) + ' h ago';
        return Math.round(s / 86400) + ' d ago';
    }

    function openModal() {
        var prev = document.getElementById('presenceMapModal');
        if (prev) prev.remove();

        document.body.insertAdjacentHTML('beforeend',
        '<div class="modal fade" id="presenceMapModal" tabindex="-1">' +
          '<div class="modal-dialog modal-lg modal-dialog-centered">' +
            '<div class="modal-content">' +
              '<div class="modal-header py-2">' +
                '<h6 class="modal-title"><i class="fas fa-location-dot me-1"></i> Where is everyone?</h6>' +
                '<button type="button" class="btn-close" data-bs-dismiss="modal"></button>' +
              '</div>' +
              '<div class="modal-body p-0">' +
                '<div id="presence-map" style="height:360px;background:#f2f2f2"></div>' +
                '<div id="presence-list" class="p-2 small"></div>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>');

        var el = document.getElementById('presenceMapModal');
        var modal = new bootstrap.Modal(el);

        el.addEventListener('shown.bs.modal', function () {
            // Leaflet measures its container on init, so it must be visible
            // first — initialising while hidden yields a zero-size map that
            // renders as a grey box until something forces a resize.
            initMap();
        });
        el.addEventListener('hidden.bs.modal', function () {
            if (state.map) { state.map.remove(); state.map = null; }
            el.remove();
        });

        renderList();
        modal.show();
    }

    function renderList() {
        var host = document.getElementById('presence-list');
        if (!host) return;
        if (!state.users.length) {
            host.innerHTML = '<div class="text-muted">No presence users configured.</div>';
            return;
        }
        host.innerHTML = state.users.map(function (u) {
            var p = presenceOf(u);
            var d = u.state && u.state.distance_m;
            return '<div class="d-flex justify-content-between align-items-center py-1 border-bottom">' +
                '<span><strong>' + esc(u.display_name || u.user_id) + '</strong> ' + stateBadge(p) + '</span>' +
                '<span class="text-muted">' +
                  (d != null ? Math.round(d) + ' m from home · ' : '') +
                  esc(ago(u.state && u.state.last_update)) +
                '</span>' +
            '</div>';
        }).join('');
    }

    function initMap() {
        if (typeof L === 'undefined') {
            document.getElementById('presence-map').innerHTML =
                '<div class="p-3 text-muted small">Map library not loaded.</div>';
            return;
        }

        var withPos = state.users.filter(function (u) {
            return u.state && u.state.lat != null && u.state.lon != null;
        });
        var home = state.users.filter(function (u) {
            return u.home_lat != null && u.home_lon != null;
        })[0];

        var centre = withPos.length
            ? [withPos[0].state.lat, withPos[0].state.lon]
            : (home ? [home.home_lat, home.home_lon] : [51.5, -0.1]);

        state.map = L.map('presence-map', { attributionControl: true })
            .setView(centre, withPos.length || home ? 14 : 5);

        L.tileLayer('/api/map/tiles/{z}/{x}/{y}.png', {
            maxZoom: 19,
            // Attribution is a licence condition of using OSM data, not
            // decoration — it stays even though the tiles are proxied.
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" ' +
                         'target="_blank" rel="noopener">OpenStreetMap</a> contributors',
        }).addTo(state.map);

        if (home) {
            L.circle([home.home_lat, home.home_lon], {
                radius: home.radius_m || 100,
                color: '#198754', fillColor: '#198754', fillOpacity: 0.08, weight: 1,
            }).addTo(state.map).bindPopup('Home geofence');
        }

        var bounds = [];
        withPos.forEach(function (u) {
            var p = presenceOf(u);
            var colour = p === 'home' ? '#198754' : (p === 'away' ? '#6c757d' : '#ffc107');
            // CircleMarker, not the default pin: Leaflet's marker icons are
            // separate image files, and vectors avoid vendoring those assets
            // entirely.
            L.circleMarker([u.state.lat, u.state.lon], {
                radius: 8, color: colour, fillColor: colour, fillOpacity: 0.85, weight: 2,
            }).addTo(state.map)
              .bindPopup('<strong>' + esc(u.display_name || u.user_id) + '</strong><br>' +
                         esc(p) + ' · ' + esc(ago(u.state.last_update)));
            bounds.push([u.state.lat, u.state.lon]);
        });

        if (home) bounds.push([home.home_lat, home.home_lon]);
        if (bounds.length > 1) state.map.fitBounds(bounds, { padding: [40, 40] });

        if (!withPos.length) {
            var note = L.control({ position: 'topright' });
            note.onAdd = function () {
                var d = L.DomUtil.create('div', 'leaflet-bar');
                d.style.cssText = 'background:#fff;padding:6px 10px;font-size:12px';
                d.textContent = 'No live positions reported yet';
                return d;
            };
            note.addTo(state.map);
        }
    }

    // ---- lifecycle --------------------------------------------------------

    async function tick() {
        if (!canSee()) { renderBadge(); return; }
        var ok = await fetchUsers();
        if (ok) {
            renderBadge();
            if (state.map) renderList();
        }
    }

    window.initPresenceBadge = function () {
        if (state.timer) return;          // once per page, not per tab click
        tick();
        state.timer = setInterval(function () {
            // Polling a hidden tab wakes the hub for nothing.
            if (!document.hidden) tick();
        }, POLL_MS);
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) tick();
        });
        if (window.zmmAuth && window.zmmAuth.onChange) {
            window.zmmAuth.onChange(function () { tick(); });
        }
    };
})();
