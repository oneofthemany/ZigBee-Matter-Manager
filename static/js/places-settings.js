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
 * Location is confirmed on a map rather than typed. Coordinates are the one
 * field users cannot sanity-check by reading them back — a transposed digit
 * puts the shops in another county and the only symptom is an automation that
 * never fires. Tiles come through the hub's caching proxy, so choosing a place
 * does not announce it to a third-party tile server.
 *
 * Searching a postcode or town moves the map; it never sets the point. A postal
 * centroid names a district, not a doorstep, so dropping the pin there would
 * look precise while being wrong by a street or more. The search is for getting
 * across the country quickly, and the click is still what commits.
 * --------------------------------------------------------------------------
 */
(function () {
    'use strict';

    var HOST_ID = 'places-settings-host';
    var state = { places: [], max: 32, map: null, marker: null, circle: null,
                  geo: null };

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
        await loadGeo();
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
                : '') +
            locationDataSection();

        var add = document.getElementById('place-add-btn');
        if (add) add.onclick = function () { openEditor(null); };
        bindLocationData();

        host.querySelectorAll('[data-place-edit]').forEach(function (b) {
            b.onclick = function () { openEditor(b.getAttribute('data-place-edit')); };
        });
        host.querySelectorAll('[data-place-del]').forEach(function (b) {
            b.onclick = function () { remove(b.getAttribute('data-place-del')); };
        });
    }

    // ---- location data ----------------------------------------------------
    //
    // Postal datasets are downloaded per country so the search box has
    // something local to answer from. Managed here, beside the picker that
    // uses them, rather than in general settings — nothing else consults them.

    function fmtRows(n) {
        if (n == null) return '—';
        if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
        if (n >= 1000) return Math.round(n / 1000) + 'k';
        return String(n);
    }

    function locationDataSection() {
        var g = state.geo;
        if (!g) return '';

        // One badge per country+source pair: the two are complementary, so a
        // UK household normally runs both and needs to see and remove each.
        var installed = (g.installed || []).map(function (d) {
            return '<span class="badge bg-primary-subtle text-primary-emphasis ' +
                     'border border-primary-subtle me-1 mb-1">' +
                     esc(d.name) + ' <span class="opacity-75">' +
                     esc(d.source_label || d.source) + ' · ' +
                     fmtRows(d.row_count) + '</span> ' +
                     '<a href="#" class="text-danger text-decoration-none ms-1" ' +
                       'data-geo-del="' + esc(d.country) + '" ' +
                       'data-geo-src="' + esc(d.source) + '" title="Remove">&times;</a>' +
                   '</span>';
        }).join('');

        var countryOpts = (g.available || []).map(function (a) {
            return '<option value="' + esc(a.country) + '">' + esc(a.name) + '</option>';
        }).join('');

        var sourceOpts = (g.sources || []).map(function (s) {
            return '<option value="' + esc(s.id) + '" ' +
                     'data-countries="' + esc((s.countries || []).join(',')) + '" ' +
                     'data-note="' + esc(s.note || '') + '">' +
                     esc(s.label) +
                     (s.precision === 'unit' ? ' — exact' : ' — district') +
                   '</option>';
        }).join('');

        return '<hr class="my-3">' +
          '<div class="d-flex justify-content-between align-items-start flex-wrap gap-2">' +
            '<div>' +
              '<div class="fw-bold small"><i class="fas fa-database me-1"></i>Location data</div>' +
              '<div class="text-muted small">' +
                'Lets the location picker find a postcode or town by name. Stored on ' +
                'the hub, so searches are instant, work offline, and never leave the house.' +
              '</div>' +
            '</div>' +
            '<div class="d-flex align-items-center gap-1 flex-wrap">' +
              '<select class="form-select form-select-sm w-auto" id="geo-country">' +
                countryOpts + '</select>' +
              '<select class="form-select form-select-sm w-auto" id="geo-source">' +
                sourceOpts + '</select>' +
              '<button class="btn btn-sm btn-outline-primary" id="geo-install">' +
                '<i class="fas fa-download me-1"></i>Add</button>' +
            '</div>' +
          '</div>' +
          '<div class="form-text small mt-1" id="geo-source-note"></div>' +
          '<div class="mt-2">' +
            (installed || '<span class="text-muted small">No countries installed yet.</span>') +
          '</div>' +
          '<div class="form-check form-switch mt-2">' +
            '<input class="form-check-input" type="checkbox" id="geo-online"' +
              (g.online_fallback ? ' checked' : '') + '>' +
            '<label class="form-check-label small" for="geo-online">' +
              'Also search online for street addresses and businesses' +
              '<span class="text-muted"> — sends the typed search to OpenStreetMap. ' +
              'Only used when the local data has no answer.</span>' +
            '</label>' +
          '</div>' +
          // Licence conditions for the installed data, not a courtesy credit.
          ((g.installed || []).length
            ? '<div class="form-text small text-muted">' +
                (g.installed || []).map(function (d) { return d.attribution; })
                  .filter(function (a, i, all) { return a && all.indexOf(a) === i; })
                  .map(esc).join('<br>') +
              '</div>'
            : '') +
          '<div id="geo-status" class="form-text small" style="display:none"></div>';
    }

    function bindLocationData() {
        var status = document.getElementById('geo-status');
        function say(msg, cls) {
            if (!status) return;
            status.className = 'form-text small ' + (cls || 'text-muted');
            status.innerHTML = msg;
            status.style.display = msg ? '' : 'none';
        }

        var countrySel = document.getElementById('geo-country');
        var sourceSel = document.getElementById('geo-source');
        var note = document.getElementById('geo-source-note');

        // A source that covers only certain countries is disabled elsewhere,
        // rather than offered and then rejected by the server.
        function syncSources() {
            if (!countrySel || !sourceSel) return;
            var cc = countrySel.value;
            var firstEnabled = null;
            Array.prototype.forEach.call(sourceSel.options, function (o) {
                var only = (o.getAttribute('data-countries') || '').split(',')
                             .filter(Boolean);
                o.disabled = only.length > 0 && only.indexOf(cc) < 0;
                if (!o.disabled && firstEnabled === null) firstEnabled = o.value;
            });
            if (sourceSel.selectedOptions[0] && sourceSel.selectedOptions[0].disabled) {
                sourceSel.value = firstEnabled || '';
            }
            if (note) {
                var sel = sourceSel.selectedOptions[0];
                note.textContent = sel ? (sel.getAttribute('data-note') || '') : '';
            }
        }
        if (countrySel) countrySel.onchange = syncSources;
        if (sourceSel) sourceSel.onchange = syncSources;
        syncSources();

        var install = document.getElementById('geo-install');
        if (install) install.onclick = async function () {
            var cc = countrySel && countrySel.value;
            var src = sourceSel && sourceSel.value;
            if (!cc || !src) return;
            install.disabled = true;
            // Open Postcode Geo is ~100 MB and 1.7M rows, so this runs for a
            // minute or two. Worth narrating rather than leaving as a dead
            // button — and the warning is only shown for the big one.
            say('<i class="fas fa-spinner fa-spin me-1"></i>Downloading and ' +
                'indexing postal data… ' +
                (src === 'open_postcode_geo'
                    ? 'This one is large — it can take a couple of minutes.'
                    : ''));
            try {
                var r = await fetch('/api/geocode/datasets/' + encodeURIComponent(cc) +
                                    '?source=' + encodeURIComponent(src),
                                    { method: 'POST', credentials: 'same-origin' });
                var d = await r.json().catch(function () { return {}; });
                if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
                say('Added ' + esc(d.name || cc) + ' (' + esc(d.source_label || src) +
                    ') — ' + fmtRows(d.row_count) + ' postal codes.', 'text-success');
                await loadGeo();
                render();
            } catch (e) {
                say('Could not add that data: ' + esc(e.message || e), 'text-danger');
                install.disabled = false;
            }
        };

        document.querySelectorAll('[data-geo-del]').forEach(function (a) {
            a.onclick = async function (ev) {
                ev.preventDefault();
                var cc = a.getAttribute('data-geo-del');
                var src = a.getAttribute('data-geo-src');
                try {
                    await fetch('/api/geocode/datasets/' + encodeURIComponent(cc) +
                                '?source=' + encodeURIComponent(src),
                                { method: 'DELETE', credentials: 'same-origin' });
                    await loadGeo();
                    render();
                } catch (e) { say('Remove failed.', 'text-danger'); }
            };
        });

        var online = document.getElementById('geo-online');
        if (online) online.onchange = async function () {
            try {
                var r = await fetch('/api/geocode/settings', {
                    method: 'PUT', credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ online_fallback: online.checked }),
                });
                if (!r.ok) throw new Error('HTTP ' + r.status);
                var d = await r.json();
                state.geo.online_fallback = d.online_fallback;
                // Live but unsaved is a real state — a read-only config file
                // means this reverts at the next restart, and silently telling
                // the user it worked would make that look like a bug later.
                say(d.persisted
                    ? 'Saved to config.yaml.'
                    : 'Applied, but could not be written to config.yaml — it ' +
                      'will revert when the hub restarts.',
                    d.persisted ? 'text-success' : 'text-warning-emphasis');
            } catch (e) {
                online.checked = !online.checked;
                say('Could not change that setting.', 'text-danger');
            }
        };
    }

    async function loadGeo() {
        try {
            var r = await fetch('/api/geocode/datasets', { credentials: 'same-origin' });
            state.geo = r.ok ? await r.json() : null;
        } catch (e) { state.geo = null; }
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
                    // Search moves the map; the click still sets the point.
                    // Keeping those separate is deliberate — a postcode
                    // centroid is a district, not a doorstep, and dropping the
                    // pin on it would look precise while being wrong.
                    '<div class="input-group input-group-sm mb-2">' +
                      '<span class="input-group-text"><i class="fas fa-search"></i></span>' +
                      '<input id="pl-search" class="form-control" autocomplete="off" ' +
                        'placeholder="Postcode, ZIP, town — or paste 51.5074, -0.1278">' +
                      '<button type="button" class="btn btn-outline-secondary" id="pl-search-go">' +
                        'Find</button>' +
                    '</div>' +
                    '<div id="pl-results" class="list-group list-group-flush mb-2" ' +
                      'style="display:none;max-height:160px;overflow-y:auto"></div>' +
                    '<div id="pl-search-note" class="form-text small mb-2" style="display:none"></div>' +
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

        // ------------------------------------------------------------------
        // Place search
        // ------------------------------------------------------------------
        // Coordinates pasted straight in. Handled here rather than sent to the
        // server: it needs no lookup, and a coordinate someone typed is the one
        // search term worth never transmitting.
        var LATLON_RE = /^\s*(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$/;

        function searchNote(msg, cls) {
            var n = document.getElementById('pl-search-note');
            if (!msg) { n.style.display = 'none'; return; }
            n.className = 'form-text small mb-2 ' + (cls || 'text-muted');
            n.innerHTML = msg;
            n.style.display = '';
        }

        function showResults(list, attribution) {
            var host = document.getElementById('pl-results');
            if (!list.length) { host.style.display = 'none'; return; }
            host.innerHTML = list.map(function (r, i) {
                return '<button type="button" class="list-group-item list-group-item-action ' +
                         'py-1 px-2" data-result="' + i + '">' +
                         '<span class="fw-bold small">' + esc(r.label) + '</span>' +
                         (r.detail ? ' <span class="text-muted small">' +
                                     esc(r.detail) + '</span>' : '') +
                         '<span class="badge bg-light text-muted ms-1">' +
                           esc(r.kind) + '</span>' +
                       '</button>';
            }).join('');
            host.style.display = '';
            host.querySelectorAll('[data-result]').forEach(function (b) {
                b.onclick = function () {
                    var r = list[parseInt(b.getAttribute('data-result'), 10)];
                    // Zoom to what the match actually covers, so the whole of
                    // it stays on screen instead of dropping the user into one
                    // corner with no landmarks. A unit postcode is a street or
                    // two; `approximate` marks a centroid averaged over a whole
                    // district; a town is miles across.
                    var zoom = r.kind === 'town' ? 13 : (r.approximate ? 14 : 16);
                    if (state.map) state.map.setView([r.lat, r.lon], zoom);
                    host.style.display = 'none';
                    searchNote('Moved to <strong>' + esc(r.label) +
                               '</strong>. Click the map to set the exact point.' +
                               (attribution ? ' <span class="text-muted">· ' +
                                              esc(attribution) + '</span>' : ''));
                };
            });
        }

        async function runSearch() {
            var q = (document.getElementById('pl-search').value || '').trim();
            document.getElementById('pl-results').style.display = 'none';
            if (!q) { searchNote(''); return; }

            var m = LATLON_RE.exec(q);
            if (m) {
                var lat = parseFloat(m[1]), lon = parseFloat(m[2]);
                if (Math.abs(lat) > 90 || Math.abs(lon) > 180) {
                    searchNote('Those coordinates are out of range.', 'text-danger');
                    return;
                }
                setPoint(lat, lon);
                if (state.map) state.map.setView([lat, lon], 17);
                searchNote('Point set from coordinates.');
                return;
            }

            searchNote('<i class="fas fa-spinner fa-spin me-1"></i>Searching…');
            try {
                var r = await fetch('/api/geocode?q=' + encodeURIComponent(q) + '&limit=6',
                                    { credentials: 'same-origin' });
                if (!r.ok) throw new Error('HTTP ' + r.status);
                var d = await r.json();
                if (d.results && d.results.length) {
                    searchNote('');
                    showResults(d.results, d.attribution);
                } else if (!d.datasets_installed) {
                    // The difference between "not found" and "nothing to search"
                    // is the whole answer here, so it is spelled out.
                    searchNote('No location data installed yet — add your country ' +
                               'under <strong>Settings → Location data</strong>, ' +
                               'or click the map directly.', 'text-warning-emphasis');
                } else {
                    searchNote('Nothing found for “' + esc(q) + '”. Try a postcode ' +
                               'or a town name, or click the map.', 'text-muted');
                }
            } catch (e) {
                searchNote('Search unavailable — click the map to set the location.',
                           'text-muted');
            }
        }

        document.getElementById('pl-search-go').onclick = runSearch;
        document.getElementById('pl-search').onkeydown = function (ev) {
            // Submit-only, never per-keystroke: the upstream fallback's usage
            // policy forbids type-ahead, and Enter inside a modal would
            // otherwise submit the form.
            if (ev.key === 'Enter') { ev.preventDefault(); runSearch(); }
        };

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
