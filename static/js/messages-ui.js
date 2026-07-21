/**
 * messages-ui.js
 * --------------------------------------------------------------------------
 * Person-to-person messaging: navbar chat badge + conversation modal.
 *
 * Threads live on the hub
 * (modules/messages_store.py); delivery is websocket for open apps and web
 * push for pockets (see sw.js — a tapped notification opens '/#messages',
 * which this module reads at startup to open the panel).
 *
 * Views inside one modal:
 *   threads list  →  tap a person  →  conversation with composer
 * A "new message" picker starts a thread with any presence user's account.
 * --------------------------------------------------------------------------
 */
(function () {
    'use strict';

    var POLL_MS = 30000;   // fallback only; websocket events are the fast path

    var state = {
        threads: [], peer: null, msgs: [],
        modal: null, timer: null, people: null,
    };

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function me() {
        var a = window.zmmAuth, w = a && a.whoami && a.whoami();
        return w ? w.username : null;
    }

    // ---- data -------------------------------------------------------------

    async function loadThreads() {
        if (!me()) { state.threads = []; renderBadge(); return; }
        try {
            var r = await fetch('/api/messages/threads',
                                { credentials: 'same-origin', cache: 'no-store' });
            if (!r.ok) return;
            state.threads = (await r.json()).threads || [];
            renderBadge();
            if (state.modal && !state.peer) renderModalBody();
        } catch (e) { /* transient; poll or WS will catch up */ }
    }

    async function loadConversation(peer) {
        try {
            var r = await fetch('/api/messages/with/' + encodeURIComponent(peer) + '?limit=100',
                                { credentials: 'same-origin', cache: 'no-store' });
            if (!r.ok) return;
            state.msgs = (await r.json()).messages || [];
            if (state.modal && state.peer === peer) renderModalBody();
            // Opening the thread is reading it.
            fetch('/api/messages/with/' + encodeURIComponent(peer) + '/read',
                  { method: 'POST', credentials: 'same-origin' })
                .then(function () { loadThreads(); });
        } catch (e) { /* ignore */ }
    }

    // Send targets: presence users' login accounts, minus me.
    async function loadPeople() {
        if (state.people !== null) return;
        try {
            var r = await fetch('/api/presence/users', { credentials: 'same-origin' });
            if (!r.ok) { state.people = []; return; }
            var who = me();
            state.people = ((await r.json()).users || [])
                .filter(function (u) { return u.enabled !== false; })
                .map(function (u) {
                    return { account: u.account || u.user_id,
                             name: u.display_name || u.user_id };
                })
                .filter(function (u) { return u.account && u.account !== who; });
        } catch (e) { state.people = []; }
    }

    function displayName(account) {
        var p = (state.people || []).find(function (x) { return x.account === account; });
        if (p) return p.name;
        if (account === 'zmm') return 'ZMM';
        return account;
    }

    // ---- badge ------------------------------------------------------------

    function renderBadge() {
        var host = document.getElementById('messages-badge-host');
        if (!host) return;
        if (!me()) { host.innerHTML = ''; return; }
        var unread = state.threads.reduce(function (n, t) { return n + (t.unread || 0); }, 0);
        host.innerHTML =
            '<button class="btn btn-sm btn-link p-0 border-0 position-relative" id="messages-badge-btn" ' +
              'title="Messages" aria-label="Messages">' +
              '<i class="fas fa-comments' + (unread ? ' text-primary' : ' text-muted') + '"></i>' +
              (unread
                  ? '<span class="badge rounded-pill bg-danger position-absolute top-0 start-100 translate-middle" ' +
                      'style="font-size:0.6rem">' + unread + '</span>'
                  : '') +
            '</button>';
        var btn = document.getElementById('messages-badge-btn');
        if (btn) btn.onclick = function () { openModal(); };
    }

    // ---- modal ------------------------------------------------------------

    function openModal(peer) {
        var prev = document.getElementById('messagesModal');
        if (prev) prev.remove();

        state.peer = peer || null;

        document.body.insertAdjacentHTML('beforeend',
        '<div class="modal fade" id="messagesModal" tabindex="-1">' +
          '<div class="modal-dialog modal-dialog-centered">' +
            '<div class="modal-content">' +
              '<div class="modal-header py-2">' +
                '<h6 class="modal-title" id="messages-modal-title">' +
                  '<i class="fas fa-comments me-1"></i> Messages</h6>' +
                '<button type="button" class="btn-close" data-bs-dismiss="modal"></button>' +
              '</div>' +
              '<div class="modal-body p-2" id="messages-modal-body"></div>' +
            '</div>' +
          '</div>' +
        '</div>');

        var el = document.getElementById('messagesModal');
        state.modal = new bootstrap.Modal(el);
        el.addEventListener('hidden.bs.modal', function () {
            state.modal = null; state.peer = null; el.remove();
        });

        loadPeople().then(function () { if (state.modal) renderModalBody(); });
        renderModalBody();
        state.modal.show();
        if (peer) loadConversation(peer);
        else loadThreads();
    }

    function renderModalBody() {
        var host = document.getElementById('messages-modal-body');
        if (!host) return;
        if (state.peer) renderConversation(host);
        else renderThreads(host);
        var title = document.getElementById('messages-modal-title');
        if (title) {
            title.innerHTML = state.peer
                ? '<button class="btn btn-sm btn-link p-0 me-2" id="messages-back" aria-label="Back">' +
                    '<i class="fas fa-arrow-left"></i></button>' + esc(displayName(state.peer))
                : '<i class="fas fa-comments me-1"></i> Messages';
            var back = document.getElementById('messages-back');
            if (back) back.onclick = function () {
                state.peer = null; state.msgs = [];
                renderModalBody(); loadThreads();
            };
        }
    }

    function fmtWhen(ts) {
        if (!ts) return '';
        var d = new Date(ts * 1000), now = new Date();
        var time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
        if (d.toDateString() === now.toDateString()) return time;
        return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) + ' ' + time;
    }

    function renderThreads(host) {
        var html = '';
        if (!state.threads.length) {
            html += '<div class="text-muted small text-center py-3">No conversations yet.</div>';
        } else {
            html += state.threads.map(function (t) {
                var who = t.last_from === me() ? 'You: ' : '';
                return '<button class="btn w-100 text-start border-0 border-bottom rounded-0 py-2 px-1 d-flex justify-content-between align-items-center" ' +
                    'data-open-peer="' + esc(t.peer) + '">' +
                    '<span class="text-truncate">' +
                      '<span class="fw-bold">' + esc(displayName(t.peer)) + '</span>' +
                      '<br><span class="small text-muted">' + esc(who + (t.last_body || '')) + '</span>' +
                    '</span>' +
                    '<span class="text-end ms-2 flex-shrink-0">' +
                      '<span class="small text-muted d-block">' + fmtWhen(t.last_at) + '</span>' +
                      (t.unread ? '<span class="badge rounded-pill bg-danger">' + t.unread + '</span>' : '') +
                    '</span>' +
                '</button>';
            }).join('');
        }

        // Start a new conversation.
        var opts = (state.people || []).map(function (p) {
            return '<option value="' + esc(p.account) + '">' + esc(p.name) + '</option>';
        }).join('');
        html += '<div class="d-flex gap-1 mt-2">' +
            '<select class="form-select form-select-sm" id="messages-new-peer">' +
              '<option value="">New message to…</option>' + opts + '</select>' +
            '</div>';

        host.innerHTML = html;

        host.querySelectorAll('[data-open-peer]').forEach(function (b) {
            b.onclick = function () {
                state.peer = b.getAttribute('data-open-peer');
                state.msgs = [];
                renderModalBody();
                loadConversation(state.peer);
            };
        });
        var np = document.getElementById('messages-new-peer');
        if (np) np.onchange = function () {
            if (!np.value) return;
            state.peer = np.value; state.msgs = [];
            renderModalBody();
            loadConversation(state.peer);
        };
    }

    function renderConversation(host) {
        var mine = me();
        var rows = state.msgs.map(function (m) {
            var out = m.from_user === mine;
            var meta = fmtWhen(m.created_at) +
                (m.source === 'automation' ? ' · automation' : '') +
                (out && m.read_at ? ' · seen' : '');
            return '<div class="d-flex ' + (out ? 'justify-content-end' : 'justify-content-start') + ' mb-1">' +
              '<div class="rounded-3 px-2 py-1" style="max-width:80%;' +
                (out ? 'background:var(--bs-primary);color:#fff'
                     : 'background:var(--bs-secondary-bg)') + '">' +
                '<div style="white-space:pre-wrap;word-break:break-word">' + esc(m.body) + '</div>' +
                '<div class="small ' + (out ? 'text-white-50' : 'text-muted') + '" style="font-size:0.65rem">' +
                  esc(meta) + '</div>' +
              '</div>' +
            '</div>';
        }).join('');

        host.innerHTML =
            '<div id="messages-scroll" style="max-height:50vh;overflow-y:auto" class="px-1 py-2">' +
              (rows || '<div class="text-muted small text-center py-3">Say hello.</div>') +
            '</div>' +
            '<div class="d-flex gap-1 mt-2">' +
              '<input type="text" class="form-control form-control-sm" id="messages-input" ' +
                'placeholder="Message…" maxlength="1000" autocomplete="off">' +
              '<button class="btn btn-sm btn-primary" id="messages-send">' +
                '<i class="fas fa-paper-plane"></i></button>' +
            '</div>';

        var scroll = document.getElementById('messages-scroll');
        if (scroll) scroll.scrollTop = scroll.scrollHeight;

        var send = document.getElementById('messages-send');
        var input = document.getElementById('messages-input');
        if (send) send.onclick = sendCurrent;
        if (input) {
            input.onkeydown = function (ev) {
                if (ev.key === 'Enter') { ev.preventDefault(); sendCurrent(); }
            };
            input.focus();
        }
    }

    async function sendCurrent() {
        var input = document.getElementById('messages-input');
        var body = (input && input.value || '').trim();
        if (!body || !state.peer) return;
        var btn = document.getElementById('messages-send');
        if (btn) btn.disabled = true;
        try {
            var r = await fetch('/api/messages', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ to_user: state.peer, body: body }),
            });
            var j = await r.json().catch(function () { return {}; });
            if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
            if (input) input.value = '';
            await loadConversation(state.peer);
        } catch (e) {
            if (window.toast) window.toast.error('Could not send: ' + (e.message || e));
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    // ---- live updates -----------------------------------------------------

    function onEvent(type, payload) {
        var who = me();
        if (!who) return;
        if (type === 'message_created') {
            if (payload.to_user !== who && payload.from_user !== who) return;
            // In the open conversation: append live. Elsewhere: refresh badge.
            if (state.modal && state.peer &&
                (payload.from_user === state.peer || payload.to_user === state.peer)) {
                loadConversation(state.peer);
            } else {
                loadThreads();
                // In-app heads-up for a message from someone else while the
                // panel is closed; the push covers the app-closed case.
                if (payload.to_user === who && window.toast) {
                    window.toast.info(displayName(payload.from_user) + ': ' +
                                      (payload.body || '').slice(0, 80));
                }
            }
        } else if (type === 'messages_read') {
            if (payload.reader === who) { loadThreads(); }
            else if (state.modal && state.peer === payload.reader) {
                // Their read receipt for my messages — refresh "seen" marks.
                loadConversation(state.peer);
            }
        }
    }

    // ---- init -------------------------------------------------------------

    window.initMessagesUI = function () {
        loadThreads();
        if (state.timer) clearInterval(state.timer);
        state.timer = setInterval(loadThreads, POLL_MS);
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') loadThreads();
        });
        if (window.zmmAuth && window.zmmAuth.onChange) {
            window.zmmAuth.onChange(function () { state.people = null; loadThreads(); });
        }
        // Arrived via a tapped push notification (sw.js opens '/#messages').
        if (window.location.hash === '#messages') {
            history.replaceState(null, '', window.location.pathname);
            loadPeople().then(function () { openModal(); });
        }
    };

    window.zmmHandleMessageEvent = onEvent;
    window.zmmOpenMessages = openModal;
})();
