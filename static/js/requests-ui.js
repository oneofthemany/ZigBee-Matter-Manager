/**
 * requests-ui.js
 * --------------------------------------------------------------------------
 * Header badge + modal for requests — asks that need an answer.
 *
 * A request is not a notification. It is addressed to you, it expects accept
 * or decline, and if you do neither the sender is told. So this deliberately
 * does NOT behave like a toast: it persists in the header until answered,
 * shows how long is left, and is visible on every tab rather than only where
 * it was raised.
 * --------------------------------------------------------------------------
 */
(function () {
    'use strict';

    var POLL_MS = 20000;    // requests are time-limited; staleness has a cost
    var state = { mine: [], sent: [], timer: null, modal: null };

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function me() {
        var a = window.zmmAuth, w = a && a.whoami && a.whoami();
        return w ? w.username : null;
    }

    function fmtLeft(s) {
        if (s == null) return '';
        if (s <= 0) return 'expired';
        if (s < 90) return s + 's left';
        if (s < 5400) return Math.round(s / 60) + ' min left';
        return Math.round(s / 3600) + ' h left';
    }

    async function load() {
        var who = me();
        if (!who) { state.mine = []; state.sent = []; renderBadge(); return; }
        try {
            var r = await fetch('/api/requests?include_settled=true', {
                credentials: 'same-origin', cache: 'no-store',
            });
            if (!r.ok) return;
            var all = (await r.json()).requests || [];
            // Pending asks addressed to me are the actionable set. Everything
            // else is history, shown only inside the modal.
            state.mine = all.filter(function (x) {
                return x.to_user === who && x.state === 'pending';
            });
            state.sent = all.filter(function (x) { return x.from_user === who; });
            renderBadge();
            if (state.modal) renderModalBody();
        } catch (e) { /* leave the last known state on screen */ }
    }

    // ---- badge ------------------------------------------------------------

    function renderBadge() {
        var host = document.getElementById('requests-badge-host');
        if (!host) return;

        // Anything of mine that lapsed unanswered is worth surfacing too —
        // that is the sender's half of the loop.
        var lapsed = state.sent.filter(function (x) {
            return x.state === 'expired';
        }).length;
        var pending = state.mine.length;

        if (!pending && !lapsed) { host.innerHTML = ''; return; }

        var cls = pending ? 'bg-warning text-dark' : 'bg-secondary';
        var label = pending
            ? pending + (pending === 1 ? ' request' : ' requests')
            : lapsed + ' unanswered';

        host.innerHTML =
            '<button class="btn btn-sm btn-link p-0 border-0" id="requests-badge-btn" ' +
                'title="Requests needing an answer">' +
              '<span class="badge ' + cls + '">' +
                '<i class="fas fa-hand-point-right me-1"></i>' + esc(label) +
              '</span>' +
            '</button>';
        var b = document.getElementById('requests-badge-btn');
        if (b) b.onclick = openModal;
    }

    // ---- modal ------------------------------------------------------------

    function openModal() {
        var prev = document.getElementById('requestsModal');
        if (prev) prev.remove();

        document.body.insertAdjacentHTML('beforeend',
        '<div class="modal fade" id="requestsModal" tabindex="-1">' +
          '<div class="modal-dialog modal-dialog-centered">' +
            '<div class="modal-content">' +
              '<div class="modal-header py-2">' +
                '<h6 class="modal-title"><i class="fas fa-hand-point-right me-1"></i> Requests</h6>' +
                '<button type="button" class="btn-close" data-bs-dismiss="modal"></button>' +
              '</div>' +
              '<div class="modal-body" id="requests-modal-body"></div>' +
            '</div>' +
          '</div>' +
        '</div>');

        var el = document.getElementById('requestsModal');
        state.modal = new bootstrap.Modal(el);
        el.addEventListener('hidden.bs.modal', function () {
            state.modal = null; el.remove();
        });
        renderModalBody();
        state.modal.show();
    }

    function stateBadge(s) {
        if (s === 'accepted') return '<span class="badge bg-success">accepted</span>';
        if (s === 'declined') return '<span class="badge bg-secondary">declined</span>';
        if (s === 'expired') return '<span class="badge bg-danger">no answer</span>';
        return '<span class="badge bg-warning text-dark">waiting</span>';
    }

    function renderModalBody() {
        var host = document.getElementById('requests-modal-body');
        if (!host) return;

        var html = '';

        if (state.mine.length) {
            html += '<div class="fw-bold small mb-2">For you</div>';
            html += state.mine.map(function (r) {
                return '<div class="border rounded p-2 mb-2">' +
                    '<div>' + esc(r.message) + '</div>' +
                    '<div class="text-muted small mb-2">' +
                        'from <strong>' + esc(r.from_user) + '</strong> · ' +
                        esc(fmtLeft(r.seconds_remaining)) +
                    '</div>' +
                    '<button class="btn btn-sm btn-success me-1" data-accept="' + esc(r.id) + '">' +
                        'Accept</button>' +
                    '<button class="btn btn-sm btn-outline-secondary" data-decline="' + esc(r.id) + '">' +
                        'Decline</button>' +
                '</div>';
            }).join('');
        } else {
            html += '<div class="text-muted small mb-3">Nothing needs your answer.</div>';
        }

        var sent = state.sent.slice(0, 8);
        if (sent.length) {
            html += '<hr><div class="fw-bold small mb-2">You asked</div>';
            html += sent.map(function (r) {
                return '<div class="d-flex justify-content-between align-items-center py-1 border-bottom">' +
                    '<span class="small">' + esc(r.message) +
                        '<span class="text-muted"> → ' + esc(r.to_user) + '</span></span>' +
                    stateBadge(r.state) +
                '</div>';
            }).join('');
        }

        host.innerHTML = html;

        host.querySelectorAll('[data-accept]').forEach(function (b) {
            b.onclick = function () { answer(b.getAttribute('data-accept'), true, b); };
        });
        host.querySelectorAll('[data-decline]').forEach(function (b) {
            b.onclick = function () { answer(b.getAttribute('data-decline'), false, b); };
        });
    }

    async function answer(id, accept, btn) {
        if (btn) btn.disabled = true;
        var r = await fetch('/api/requests/' + encodeURIComponent(id) +
                            (accept ? '/accept' : '/decline'),
                            { method: 'POST', credentials: 'same-origin' });
        if (!r.ok) {
            var e = await r.json().catch(function () { return {}; });
            // 409 means it settled while the dialog was open — usually it
            // expired. Say so plainly instead of leaving a dead button.
            if (window.toast) {
                window.toast[r.status === 409 ? 'warning' : 'error'](
                    e.detail || 'Could not answer that request');
            }
        }
        await load();
    }

    // ---- live updates -----------------------------------------------------

    function onEvent(type, payload) {
        var who = me();
        if (!who) return;
        // Only surface what concerns this person.
        if (payload.to_user !== who && payload.from_user !== who) return;

        if (type === 'request_created' && payload.to_user === who) {
            if (window.zbmSendNotification) {
                window.zbmSendNotification(
                    'Request from ' + payload.from_user, payload.message,
                    'zmm-request-' + payload.id);
            }
        } else if (type === 'request_expired' && payload.from_user === who) {
            if (window.zbmSendNotification) {
                window.zbmSendNotification(
                    'No answer from ' + payload.to_user, payload.message,
                    'zmm-request-exp-' + payload.id);
            }
        }
        load();
    }

    window.initRequestsUI = function () {
        if (state.timer) return;
        load();
        state.timer = setInterval(function () {
            if (!document.hidden) load();
        }, POLL_MS);
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) load();
        });
        if (window.zmmAuth && window.zmmAuth.onChange) {
            window.zmmAuth.onChange(function () { load(); });
        }
    };

    // Websocket hook — the hub emits these the moment anything changes, so
    // polling is only a safety net for a dropped connection.
    window.zmmHandleRequestEvent = onEvent;
})();
