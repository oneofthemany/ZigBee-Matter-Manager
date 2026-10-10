/* House mode + alarm badge and panel.
 *
 * A badge in the header (house mode, and a shield when the alarm is set up);
 * a modal on click to change the mode, arm, or disarm with a PIN. A modal, not
 * a dropdown: the phone tab rail's mask clips dropdowns.
 * Backend: modules/house_mode.py, modules/alarm.py — docs/house-mode-and-alarm.md.
 */
(function () {
    'use strict';

    var POLL_MS = 30000;
    var s = { mode: null, alarm: null, timer: null, tick: null, openList: null };

    function esc(v) {
        return String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    function can(scope) {
        var a = window.zmmAuth;
        return !!(a && a.whoami() && (a.hasScope('admin') || a.hasScope(scope)));
    }
    async function api(method, url, body) {
        var r = await fetch(url, {
            method: method, credentials: 'same-origin', cache: 'no-store',
            headers: body ? { 'Content-Type': 'application/json' } : undefined,
            body: body ? JSON.stringify(body) : undefined,
        });
        var j = await r.json().catch(function () { return {}; });
        if (!r.ok) {
            var err = new Error(typeof j.detail === 'string' ? j.detail
                : (j.detail && j.detail.error) || ('HTTP ' + r.status));
            err.status = r.status;
            err.detail = j.detail;
            throw err;
        }
        return j;
    }

    var ALARM = {
        disarmed:    { label: 'Disarmed',     cls: 'bg-secondary', icon: 'fa-shield' },
        arming:      { label: 'Arming',       cls: 'bg-warning text-dark', icon: 'fa-shield-halved' },
        armed_home:  { label: 'Armed (home)', cls: 'bg-success', icon: 'fa-shield-halved' },
        armed_away:  { label: 'Armed (away)', cls: 'bg-success', icon: 'fa-shield-halved' },
        armed_night: { label: 'Armed (night)', cls: 'bg-success', icon: 'fa-shield-halved' },
        pending:     { label: 'Entry delay',  cls: 'bg-warning text-dark', icon: 'fa-triangle-exclamation' },
        triggered:   { label: 'ALARM',        cls: 'bg-danger', icon: 'fa-bell' },
    };
    var MODE_ICON = { home: 'fa-house', away: 'fa-person-walking-luggage', night: 'fa-moon', holiday: 'fa-plane' };

    async function refresh() {
        s.mode = can('device:read') ? await api('GET', '/api/house/mode').catch(function () { return null; }) : null;
        s.alarm = can('security:read') ? await api('GET', '/api/alarm').catch(function () { return null; }) : null;
        renderBadge();
        renderModal();
    }

    function alarmInUse() {
        return !!(s.alarm && (s.alarm.zones.length || s.alarm.state !== 'disarmed'));
    }

    function renderBadge() {
        var host = document.getElementById('house-badge-host');
        if (!host) return;
        var showMode = s.mode && s.mode.configured;
        if (!showMode && !alarmInUse()) { host.innerHTML = ''; return; }
        var html = '';
        if (showMode) {
            var m = (s.mode.mode || '').toLowerCase();
            html += '<span class="badge bg-info text-dark"><i class="fas ' + (MODE_ICON[m] || 'fa-house') + '"></i>' +
                    '<span class="ms-1 d-none d-sm-inline">' + esc(s.mode.mode) + '</span></span>';
        }
        if (alarmInUse()) {
            var a = ALARM[s.alarm.state] || ALARM.disarmed;
            html += '<span class="badge ' + a.cls + (showMode ? ' ms-1' : '') + '"><i class="fas ' + a.icon + '"></i>' +
                    '<span class="ms-1 d-none d-md-inline">' + esc(a.label) + '</span></span>';
        }
        var label = (showMode ? 'House mode ' + s.mode.mode + '. ' : '') +
                    (alarmInUse() ? 'Alarm ' + (ALARM[s.alarm.state] || {}).label + '. ' : '');
        host.innerHTML = '<button class="btn btn-sm btn-link p-0 border-0" id="house-badge-btn" ' +
            'title="House mode and alarm" aria-label="' + esc(label) + 'Open house controls.">' + html + '</button>';
        document.getElementById('house-badge-btn').addEventListener('click', openModal);
    }

    // modal

    function modeSection() {
        if (!s.mode) return '';
        if (!s.mode.configured) {
            return can('admin')
                ? '<div class="mb-3"><div class="small text-muted mb-2">No house mode yet.</div>' +
                  '<button class="btn btn-sm btn-outline-primary" id="hb-create">Create house mode</button></div>'
                : '';
        }
        var cur = (s.mode.mode || '').toLowerCase();
        var btns = s.mode.options.map(function (o) {
            var on = o.toLowerCase() === cur;
            return '<button class="btn btn-sm ' + (on ? 'btn-info' : 'btn-outline-info') + ' hb-mode" data-mode="' + esc(o) + '"' +
                   (can('device:write') ? '' : ' disabled') + ' aria-pressed="' + on + '">' +
                   '<i class="fas ' + (MODE_ICON[o.toLowerCase()] || 'fa-circle') + ' me-1"></i>' + esc(o) + '</button>';
        }).join('');
        return '<h6 class="mb-2">House mode</h6><div class="d-flex flex-wrap gap-2 mb-1">' + btns + '</div>' +
               (s.mode.follow_presence ? '<div class="small text-muted mb-3">Follows presence: away after ' +
                   esc(s.mode.away_after_minutes) + ' min with nobody home, home when someone arrives.</div>'
                   : '<div class="mb-3"></div>');
    }

    function alarmSection() {
        var a = s.alarm;
        if (!a) return '';
        if (!a.zones.length && a.state === 'disarmed') {
            return '<h6 class="mb-2">Alarm</h6><div class="small text-muted">Not set up' +
                   (can('admin') ? ' — Settings → Security → Alarm.' : '.') + '</div>';
        }
        var st = ALARM[a.state] || ALARM.disarmed;
        var left = a.seconds_left != null && (a.state === 'arming' || a.state === 'pending')
            ? ' <span class="small" id="hb-left">' + a.seconds_left + 's</span>' : '';
        var cause = a.cause && (a.state === 'pending' || a.state === 'triggered')
            ? '<div class="small text-danger mt-1">' + esc(a.cause.name) + ' ' + esc(a.cause.event) + '</div>' : '';
        var offline = a.zones.filter(function (z) { return !z.online; });
        var open = a.zones.filter(function (z) { return z.open; });
        var notes = (offline.length ? '<div class="small text-warning-emphasis">Offline: ' + offline.map(function (z) { return esc(z.name); }).join(', ') + '</div>' : '') +
                    (open.length ? '<div class="small text-muted">Open: ' + open.map(function (z) { return esc(z.name) + (z.bypassed ? ' (bypassed)' : ''); }).join(', ') + '</div>' : '');
        var write = can('security:write');
        var armed = a.state !== 'disarmed';
        var pinField = '<input type="password" class="form-control form-control-sm" id="hb-pin" inputmode="numeric" ' +
                       'autocomplete="off" maxlength="8" placeholder="PIN" style="max-width:8rem">';
        var arm = ['home', 'away', 'night'].map(function (m) {
            return '<button class="btn btn-sm btn-outline-success hb-arm" data-arm="' + m + '">' +
                   '<i class="fas ' + MODE_ICON[m] + ' me-1"></i>Arm ' + m + '</button>';
        }).join('');
        var openList = s.openList ? '<div class="alert alert-warning small py-2 mt-2 mb-0">Open: ' +
            s.openList.map(function (o) { return esc(o.name); }).join(', ') +
            '. <button class="btn btn-sm btn-warning ms-1" id="hb-force" data-arm="' + esc(s.openList.mode) + '">Arm anyway</button>' +
            '<div class="text-muted mt-1">Those stay ignored until they close.</div></div>' : '';
        var controls = !write ? '' : armed
            ? '<div class="d-flex flex-wrap gap-2 align-items-center mt-2">' + pinField +
              '<button class="btn btn-sm btn-danger" id="hb-disarm"><i class="fas fa-unlock me-1"></i>Disarm</button></div>'
            : '<div class="d-flex flex-wrap gap-2 align-items-center mt-2">' +
              (a.arm_requires_pin ? pinField : '') + arm + '</div>' + openList;
        var pin = !write ? '' :
            '<details class="mt-3"><summary class="small">' + (a.have_pin ? 'Change my PIN' : 'Set my PIN') + '</summary>' +
            '<div class="d-flex flex-wrap gap-2 mt-2">' +
            (a.have_pin ? '<input type="password" class="form-control form-control-sm" id="hb-pin-cur" inputmode="numeric" maxlength="8" placeholder="Current" style="max-width:8rem">' : '') +
            '<input type="password" class="form-control form-control-sm" id="hb-pin-new" inputmode="numeric" maxlength="8" placeholder="New (4-8 digits)" style="max-width:10rem">' +
            '<button class="btn btn-sm btn-outline-secondary" id="hb-pin-save">Save PIN</button></div></details>' +
            (a.have_pin ? '' : '<div class="small text-muted mt-1">You need a PIN to disarm.</div>');
        return '<h6 class="mb-2">Alarm</h6>' +
               '<div><span class="badge ' + st.cls + '"><i class="fas ' + st.icon + ' me-1"></i>' + esc(st.label) + '</span>' + left + '</div>' +
               cause + notes + controls + pin + '<div class="small mt-2" id="hb-msg" role="status"></div>';
    }

    function renderModal() {
        var body = document.getElementById('hb-body');
        if (!body) return;
        // Don't wipe a PIN someone is typing.
        var typing = document.activeElement && body.contains(document.activeElement) &&
                     document.activeElement.tagName === 'INPUT';
        if (typing) return;
        body.innerHTML = modeSection() + alarmSection();
        wire(body);
    }

    function say(text, cls) {
        var el = document.getElementById('hb-msg');
        if (el) { el.className = 'small mt-2 ' + (cls || ''); el.textContent = text; }
    }

    function wire(body) {
        body.querySelectorAll('.hb-mode').forEach(function (b) {
            b.addEventListener('click', async function () {
                try { s.mode = await api('POST', '/api/house/mode', { mode: b.dataset.mode }); renderBadge(); renderModal(); }
                catch (e) { say(e.message, 'text-danger'); }
            });
        });
        var create = document.getElementById('hb-create');
        if (create) create.addEventListener('click', async function () {
            try { s.mode = await api('POST', '/api/house/mode/config/create-worker'); renderBadge(); renderModal(); }
            catch (e) { say(e.message, 'text-danger'); }
        });
        function pin() { var p = document.getElementById('hb-pin'); return p ? p.value.trim() : undefined; }
        async function arm(mode, force) {
            try {
                await api('POST', '/api/alarm/arm', { mode: mode, pin: pin(), force: !!force });
                s.openList = null;
                await refresh();
            } catch (e) {
                if (e.status === 409 && e.detail && e.detail.open) {
                    s.openList = e.detail.open;
                    s.openList.mode = mode;
                    renderModal();
                } else say(e.message, 'text-danger');
            }
        }
        body.querySelectorAll('.hb-arm').forEach(function (b) {
            b.addEventListener('click', function () { arm(b.dataset.arm, false); });
        });
        var force = document.getElementById('hb-force');
        if (force) force.addEventListener('click', function () { arm(force.dataset.arm, true); });
        var dis = document.getElementById('hb-disarm');
        if (dis) dis.addEventListener('click', async function () {
            try { await api('POST', '/api/alarm/disarm', { pin: pin() }); document.getElementById('hb-pin').value = ''; await refresh(); }
            catch (e) { say(e.message, 'text-danger'); }
        });
        var save = document.getElementById('hb-pin-save');
        if (save) save.addEventListener('click', async function () {
            var cur = document.getElementById('hb-pin-cur');
            try {
                await api('POST', '/api/alarm/pin', { pin: document.getElementById('hb-pin-new').value.trim(),
                                                      current: cur ? cur.value.trim() : undefined });
                document.activeElement && document.activeElement.blur();
                await refresh();
                say('PIN saved.', 'text-success');
            } catch (e) { say(e.message, 'text-danger'); }
        });
    }

    function openModal() {
        var prev = document.getElementById('houseModal');
        if (prev) prev.remove();
        s.openList = null;
        document.body.insertAdjacentHTML('beforeend',
            '<div class="modal fade" id="houseModal" tabindex="-1" aria-labelledby="houseModalTitle">' +
              '<div class="modal-dialog modal-dialog-centered">' +
                '<div class="modal-content">' +
                  '<div class="modal-header py-2"><h6 class="modal-title" id="houseModalTitle">' +
                    '<i class="fas fa-house-lock me-1"></i> House</h6>' +
                    '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button></div>' +
                  '<div class="modal-body" id="hb-body"></div>' +
                '</div></div></div>');
        var el = document.getElementById('houseModal');
        el.addEventListener('hidden.bs.modal', function () { clearInterval(s.tick); el.remove(); });
        renderModal();
        new bootstrap.Modal(el).show();
        // Count delays down between server updates.
        clearInterval(s.tick);
        s.tick = setInterval(function () {
            var l = document.getElementById('hb-left');
            if (l && s.alarm && s.alarm.seconds_left > 0) {
                s.alarm.seconds_left -= 1;
                l.textContent = s.alarm.seconds_left + 's';
            }
        }, 1000);
    }

    window.initHouseBadge = function () {
        if (s.timer) return;
        refresh();
        s.timer = setInterval(function () { if (!document.hidden) refresh(); }, POLL_MS);
        document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
        window.addEventListener('zmm-alarm-state', function (ev) {
            if (s.alarm) { s.alarm = Object.assign({}, s.alarm, ev.detail); renderBadge(); renderModal(); }
            else refresh();
        });
        window.addEventListener('zmm-worker-updated', function (ev) {
            if (s.mode && s.mode.configured && ev.detail && ev.detail.id === s.mode.worker) refresh();
        });
        if (window.zmmAuth && window.zmmAuth.onChange) window.zmmAuth.onChange(function () { refresh(); });
    };
})();
