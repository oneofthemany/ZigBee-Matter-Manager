/**
 * places-settings.js
 * --------------------------------------------------------------------------
 * Settings → Presence → Apiary.
 *
 * The apiary is where the hive's foragers go: shared named locations beyond
 * home ("the shops", "school"). Each gives presence users a `place` attribute
 * that automations can test.
 *
 * Naming note: "apiary" is the user-facing term, matching Hive/Frame/Chamber/
 * Cell elsewhere. The API and the automation attribute stay `place` — a rule
 * reading `place == the_shops` is clearer than `apiary == the_shops`, and
 * renaming a shipped attribute would silently break existing rules.
 *
 * Location is picked on a map rather than typed. Coordinates are the one field
 * users cannot sanity-check by reading them back — a transposed digit puts the
 * shops in another county and the only symptom is an automation that never
 * fires. Tiles come through the hub's caching proxy, so choosing a place does
 * not announce it to a third-party tile server.
 * --------------------------------------------------------------------------
 */
(function () {
    'use strict';

    var HOST_ID = 'places-settings-host';
    var state = { places: [], max: 32, map: null, marker: null, circle: null };

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function isAdmin() {
        var a = window.zmmAuth;
        return !!(a && a.hasScope && a.hasScope('admin'));
    }

    async function load() {
        try {
            var r = await fetch('/api/places', {
                credentials: 'same-origin', cache: 'no-store',
            });
            if (!r.ok) { state.places = []; render(); return; }
            var j = await r.json();
            state.places = j.places || [];
            state.max = j.max || 32;
        } catch (e) {
            state.places = [];
        }
        render();
    }

    // ---- list -------------------------------------------------------------

    function render() {
        var host = document.getElementById(HOST_ID);
        if (!host) return;

        if (!isAdmin()) {
            host.innerHTML = '<div class="text-muted small">' +
                'The apiary is managed by an administrator.</div>';
            return;
        }

        var rows = state.places.length
            ? state.places.map(function (p) {
                return '<tr>' +
                    '<td><i class="fas fa-' + esc(p.icon || 'map-marker-alt') + ' me-2 text-muted"></i>' +
                        '<strong>' + esc(p.name) + '</strong>' +
                        '<div class="text-muted small"><code>' + esc(p.id) + '</code></div></td>' +
                    '<td class="text-muted small">' +
                        esc(Number(p.lat).toFixed(5)) + ', ' + esc(Number(p.lon).toFixed(5)) +
                    '</td>' +
                    '<td class="text-muted small">' + esc(Math.round(p.radius_m)) + ' m</td>' +
                    '<td>' + (p.enabled === false
                        ? '<span class="badge bg-secondary">off</span>'
                        : '<span class="badge bg-success">on</span>') + '</td>' +
                    '<td class="text-end">' +
                        '<button class="btn btn-sm btn-outline-secondary me-1" ' +
                            'data-place-edit="' + esc(p.id) + '">Edit</button>' +
                        '<button class="btn btn-sm btn-outline-danger" ' +
                            'data-place-del="' + esc(p.id) + '">Delete</button>' +
                    '</td>' +
                '</tr>';
            }).join('')
            : '<tr><td colspan="5" class="text-muted small py-3">' +
              'Your apiary is empty. Add a location to use <code>place</code> in ' +
              'automations — for example, notify someone when a family member ' +
              'reaches the shops.' +
              '</td></tr>';

        host.innerHTML =
            '<div class="d-flex justify-content-between align-items-center mb-2">' +
              '<div class="text-muted small">' +
                'Where your foragers go — shared locations beyond home. Each ' +
                'gives presence users a <code>place</code> value automations can test.' +
              '</div>' +
              '<button class="btn btn-sm btn-primary" id="place-add-btn"' +
                (state.places.length >= state.max ? ' disabled' : '') + '>' +
                '<i class="fas fa-plus"></i> Add location</button>' +
            '</div>' +
            '<div class="table-responsive"><table class="table table-sm align-middle mb-0">' +
              '<thead><tr class="text-muted small">' +
                '<th>Name</th><th>Coordinates</th><th>Radius</th><th>State</th><th></th>' +
              '</tr></thead><tbody>' + rows + '</tbody>' +
            '</table></div>' +
            (state.places.length >= state.max
                ? '<div class="form-text text-warning">Maximum ' + state.max + ' places reached.</div>'
                : '');

        var add = document.getElementById('place-add-btn');
        if (add) add.onclick = function () { openEditor(null); };

        host.querySelectorAll('[data-place-edit]').forEach(function (b) {
            b.onclick = function () { openEditor(b.getAttribute('data-place-edit')); };
        });
        host.querySelectorAll('[data-place-del]').forEach(function (b) {
            b.onclick = function () { remove(b.getAttribute('data-place-del')); };
        });
    }

    async function remove(id) {
        var p = state.places.filter(function (x) { return x.id === id; })[0];
        var ok = window.zbmConfirm
            ? await window.zbmConfirm({
                title: 'Delete location?',
                body: 'Delete "' + (p ? p.name : id) + '"?\n\n' +
                      'Automations that test this place keep their condition but ' +
                      'will never match again — they are not edited for you.',
                okText: 'Delete', danger: true,
              })
            : confirm('Delete "' + (p ? p.name : id) + '"?');
        if (!ok) return;

        var r = await fetch('/api/places/' + encodeURIComponent(id), {
            method: 'DELETE', credentials: 'same-origin',
        });
        if (!r.ok && window.toast) window.toast.error('Delete failed');
        await load();
    }

    // ---- editor -----------------------------------------------------------

    function openEditor(id) {
        var p = id
            ? state.places.filter(function (x) { return x.id === id; })[0]
            : null;
        var existing = !!p;
        p = p || { name: '', lat: null, lon: null, radius_m: 150, enabled: true,
                   icon: 'map-marker-alt' };

        var prev = document.getElementById('placeEditModal');
        if (prev) prev.remove();

        document.body.insertAdjacentHTML('beforeend',
        '<div class="modal fade" id="placeEditModal" tabindex="-1">' +
          '<div class="modal-dialog modal-lg">' +
            '<div class="modal-content">' +
              '<div class="modal-header py-2">' +
                '<h6 class="modal-title">' + (existing ? 'Edit' : 'Add') + ' apiary location</h6>' +
                '<button type="button" class="btn-close" data-bs-dismiss="modal"></button>' +
              '</div>' +
              '<div class="modal-body">' +
                '<div class="row g-3">' +
                  '<div class="col-md-6">' +
                    '<label class="form-label small fw-bold">Name</label>' +
                    '<input id="pl-name" class="form-control form-control-sm" ' +
                      'value="' + esc(p.name) + '" placeholder="The Shops">' +
                    '<div class="form-text small">' +
                      (existing
                        ? 'Id <code>' + esc(p.id) + '</code> is fixed — automations reference it.'
                        : 'The id is derived from the name and cannot be changed later.') +
                    '</div>' +
                  '</div>' +
                  '<div class="col-md-3">' +
                    '<label class="form-label small fw-bold">Radius (m)</label>' +
                    '<input id="pl-radius" type="number" min="1" ' +
                      'class="form-control form-control-sm" value="' + esc(p.radius_m) + '">' +
                  '</div>' +
                  '<div class="col-md-3 d-flex align-items-end">' +
                    '<div class="form-check">' +
                      '<input id="pl-enabled" type="checkbox" class="form-check-input"' +
                        (p.enabled !== false ? ' checked' : '') + '>' +
                      '<label class="form-check-label small" for="pl-enabled">Enabled</label>' +
                    '</div>' +
                  '</div>' +
                  '<div class="col-12">' +
                    '<label class="form-label small fw-bold">Location</label>' +
                    '<div id="pl-map" style="height:300px;background:#f2f2f2;border-radius:.375rem"></div>' +
                    '<div class="d-flex justify-content-between align-items-center mt-1">' +
                      '<span class="form-text small mb-0" id="pl-coords">' +
                        (p.lat != null
                          ? Number(p.lat).toFixed(5) + ', ' + Number(p.lon).toFixed(5)
                          : 'Click the map to set the location') +
                      '</span>' +
                      '<button type="button" class="btn btn-sm btn-outline-primary" id="pl-here">' +
                        '<i class="fas fa-crosshairs"></i> Use my location</button>' +
                    '</div>' +
                  '</div>' +
                '</div>' +
                '<div id="pl-error" class="alert alert-danger mt-3" style="display:none"></div>' +
              '</div>' +
              '<div class="modal-footer py-2">' +
                '<button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">Cancel</button>' +
                '<button type="button" class="btn btn-primary btn-sm" id="pl-save">Save</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>');

        var el = document.getElementById('placeEditModal');
        var modal = new bootstrap.Modal(el);
        var picked = { lat: p.lat, lon: p.lon };

        el.addEventListener('shown.bs.modal', function () {
            // Leaflet sizes itself on init, so it has to be visible first —
            // initialising a hidden container yields a grey box.
            initPicker(picked, Number(p.radius_m) || 150);
        });
        el.addEventListener('hidden.bs.modal', function () {
            if (state.map) { state.map.remove(); state.map = null; }
            state.marker = state.circle = null;
            el.remove();
        });

        document.getElementById('pl-save').onclick = async function () {
            var err = document.getElementById('pl-error');
            err.style.display = 'none';

            var name = document.getElementById('pl-name').value.trim();
            var radius = parseFloat(document.getElementById('pl-radius').value);
            if (!name) { showErr('Give the location a name.'); return; }
            if (picked.lat == null || picked.lon == null) {
                showErr('Set a location — click the map or use your current position.');
                return;
            }
            if (!(radius > 0)) { showErr('Radius must be greater than zero.'); return; }

            var body = {
                name: name, lat: picked.lat, lon: picked.lon,
                radius_m: radius,
                enabled: document.getElementById('pl-enabled').checked,
                icon: p.icon || 'map-marker-alt',
            };
            // Preserve the id when editing: automations reference it, so
            // regenerating it from a renamed place would silently orphan them.
            if (existing) body.id = p.id;

            var r = await fetch('/api/places', {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!r.ok) {
                var e = await r.json().catch(function () { return {}; });
                showErr(e.detail || 'Save failed');
                return;
            }
            modal.hide();
            await load();

            function showErr(m) { err.textContent = m; err.style.display = ''; }
        };

        function showErr(m) {
            var err = document.getElementById('pl-error');
            err.textContent = m; err.style.display = '';
        }

        document.getElementById('pl-here').onclick = function () {
            if (!navigator.geolocation) { showErr('This browser has no geolocation.'); return; }
            navigator.geolocation.getCurrentPosition(function (pos) {
                setPoint(pos.coords.latitude, pos.coords.longitude);
                if (state.map) state.map.setView([pos.coords.latitude, pos.coords.longitude], 16);
            }, function (e) {
                showErr('Could not get your location: ' + (e.message || e.code));
            }, { enableHighAccuracy: true, timeout: 10000 });
        };

        function setPoint(lat, lon) {
            picked.lat = lat; picked.lon = lon;
            document.getElementById('pl-coords').textContent =
                lat.toFixed(5) + ', ' + lon.toFixed(5);
            if (!state.map) return;
            var r = parseFloat(document.getElementById('pl-radius').value) || 150;
            if (state.marker) state.map.removeLayer(state.marker);
            if (state.circle) state.map.removeLayer(state.circle);
            state.marker = L.circleMarker([lat, lon], {
                radius: 6, color: '#0d6efd', fillColor: '#0d6efd', fillOpacity: 0.9,
            }).addTo(state.map);
            state.circle = L.circle([lat, lon], {
                radius: r, color: '#0d6efd', fillColor: '#0d6efd',
                fillOpacity: 0.08, weight: 1,
            }).addTo(state.map);
        }

        function initPicker(pt, radius) {
            if (typeof L === 'undefined') {
                document.getElementById('pl-map').innerHTML =
                    '<div class="p-3 text-muted small">Map library not loaded.</div>';
                return;
            }
            var centre = (pt.lat != null) ? [pt.lat, pt.lon] : homeGuess();
            state.map = L.map('pl-map').setView(centre, pt.lat != null ? 15 : 12);
            L.tileLayer('/api/map/tiles/{z}/{x}/{y}.png', {
                maxZoom: 19,
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" ' +
                             'target="_blank" rel="noopener">OpenStreetMap</a> contributors',
            }).addTo(state.map);

            if (pt.lat != null) setPoint(pt.lat, pt.lon);
            state.map.on('click', function (e) { setPoint(e.latlng.lat, e.latlng.lng); });

            // Keep the radius ring honest while it is being typed.
            document.getElementById('pl-radius').oninput = function () {
                if (picked.lat != null) setPoint(picked.lat, picked.lon);
            };
        }

        modal.show();
    }

    /** Centre new places near a known home, rather than mid-ocean. */
    function homeGuess() {
        var u = (window.state && window.state.presenceUsers) || [];
        for (var i = 0; i < u.length; i++) {
            if (u[i].home_lat != null) return [u[i].home_lat, u[i].home_lon];
        }
        return [51.5, -0.12];
    }

    window.initPlacesSettings = function () {
        if (!document.getElementById(HOST_ID)) return;
        load();
    };
})();
