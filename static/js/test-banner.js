/**
 * Test-recovery banner — standalone and dependency-free.
 *
 * A CLASSIC script with no imports, so it still runs when a test deploy ships
 * broken JavaScript and the ES-module graph dies with a SyntaxError. That exact
 * failure once left a pending batch with no working Confirm/Rollback UI, so
 * this must never depend on main.js, toasts.js, editor.js or Bootstrap.
 *
 * editor.js takes over the element for in-editor deploys, calling
 * window.zmmTestBanner.stop() first so the two countdowns do not fight.
 */
(function () {
    'use strict';

    var RETRIES = 3;         // auth/session may need a beat right after boot
    var RETRY_DELAY = 5000;
    var interval = null;

    function stop() {
        if (interval) { clearInterval(interval); interval = null; }
    }

    function statusLine(banner, text, background) {
        stop();
        banner.innerHTML =
            '<div style="background:' + background + ';color:#fff;font-size:13px;' +
            'text-align:center;padding:8px 16px;">' + text + '</div>';
    }

    function post(banner, url, okText, okBg) {
        fetch(url, { method: 'POST' })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d.success && d.restart_deferred) {
                    // Files are restored but the old code is still running,
                    // so the banner must not read as "all done".
                    statusLine(banner, 'Files restored — service NOT restarted. ' +
                               (d.reason ? d.reason.message : '') +
                               ' Restart from Settings once it clears.', '#b45309');
                } else if (d.success) {
                    statusLine(banner, okText, okBg);
                    setTimeout(function () { banner.remove(); }, 4000);
                } else {
                    statusLine(banner, 'Failed: ' + (d.error || 'unknown error'), '#dc3545');
                }
            })
            .catch(function (e) {
                // A rollback of a restart-type batch kills the server mid-response
                statusLine(banner, 'Service restarting… (' + e.message + ')', '#b45309');
            });
    }

    function render(remaining) {
        if (document.getElementById('testRecoveryBanner')) return;

        var banner = document.createElement('div');
        banner.id = 'testRecoveryBanner';
        banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;';
        banner.innerHTML =
            '<div style="background:#1e1e1e;border-bottom:2px solid #dc3545;padding:8px 16px;' +
            'display:flex;align-items:center;justify-content:space-between;gap:8px;' +
            'font-family:system-ui,sans-serif;">' +
              '<div style="color:#fff;font-size:13px;">' +
                '<strong>Test Deploy Active</strong> — ' +
                '<span style="color:#ffc107;"><span id="testCountdown">' + remaining +
                '</span>s to confirm or changes roll back</span>' +
              '</div>' +
              '<div style="display:flex;gap:8px;">' +
                '<button id="zmmTestConfirm" style="background:#16825d;color:#fff;border:0;' +
                'border-radius:4px;padding:4px 14px;font-size:13px;cursor:pointer;">' +
                'Confirm — Keep Changes</button>' +
                '<button id="zmmTestRollback" style="background:#dc3545;color:#fff;border:0;' +
                'border-radius:4px;padding:4px 14px;font-size:13px;cursor:pointer;">' +
                'Rollback</button>' +
              '</div>' +
            '</div>';
        document.body.prepend(banner);

        banner.querySelector('#zmmTestConfirm').addEventListener('click', function () {
            post(banner, '/api/editor/test-confirm', 'Changes confirmed and kept.', '#16825d');
        });
        banner.querySelector('#zmmTestRollback').addEventListener('click', function () {
            post(banner, '/api/editor/test-rollback',
                 'Rolled back. If the batch restarted the service, it is restarting again now.',
                 '#b45309');
        });

        interval = setInterval(function () {
            remaining--;
            var el = document.getElementById('testCountdown');
            if (el) el.textContent = remaining;
            if (remaining <= 0) {
                stop();
                // Window expired — let the server's verdict decide the message
                fetch('/api/editor/test-status')
                    .then(function (r) { return r.json(); })
                    .then(function (d) {
                        if (d.pending) return;   // clock skew; server timer will act
                        statusLine(banner, 'Confirm window expired — changes were rolled back.',
                                   '#dc3545');
                        setTimeout(function () { window.location.reload(); }, 4000);
                    })
                    .catch(function () { window.location.reload(); });
            }
        }, 1000);
    }

    function check(attempt) {
        fetch('/api/editor/test-status')
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d.pending) {
                    render(typeof d.remaining === 'number' ? d.remaining : 120);
                }
                // Not pending → nothing to show; stop. We only start polling
                // once authenticated (see startWhenAuthed), so a stray
                // auth_required here is a transient race — don't loop on it.
            })
            .catch(function () {
                if (attempt < RETRIES) {
                    setTimeout(function () { check(attempt + 1); }, RETRY_DELAY);
                }
            });
    }

    window.zmmTestBanner = { stop: stop };

    // The banner only reports on an in-progress *authenticated* editor test
    // deploy. Polling /api/editor/test-status while anonymous (the login page or
    // the first-run setup wizard) just spams 401s, so gate the first poll on a
    // real principal. zmmAuth.onChange fires on boot (null when anonymous) and
    // again after a successful login, so the banner still appears post-login.
    var started = false;
    function startWhenAuthed() {
        if (started) return;
        var auth = window.zmmAuth;
        if (auth && typeof auth.onChange === 'function') {
            auth.onChange(function (principal) {
                if (principal && !started) { started = true; check(0); }
            });
            return;
        }
        // Escape hatch: auth.js is absent (e.g. a broken test deploy killed the
        // module graph) — poll directly so Confirm/Rollback still works.
        started = true;
        check(0);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', startWhenAuthed);
    } else {
        startWhenAuthed();
    }
})();
