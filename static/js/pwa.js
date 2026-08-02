/* ZigBee Matter Manager — PWA + Browser Notifications */

(function () {
    'use strict';

    var PREFS_KEY = 'zbm-notification-prefs';

    // Default notification preferences
    var defaultPrefs = {
        enabled: false,
        deviceOffline: true,
        deviceOnline: false,
        lowBattery: true,
        thermostatReached: true,
        suppressMinutes: 5  // Don't repeat same notification within N minutes
    };

    var notifHistory = {}; // Track sent notifications to avoid spam
    var previousStates = {}; // Track previous device states for diff

    // 1. SERVICE WORKER REGISTRATION (PWA)

    function registerServiceWorker() {
        if (!('serviceWorker' in navigator)) {
            zmmLog('pwa').log('[PWA] Service workers not supported');
            return;
        }

        var host = window.location.hostname;
        var isLocalhost = (host === 'localhost' || host === '127.0.0.1' || host === '::1');
if (window.location.protocol !== 'https:' && !isLocalhost) {
    zmmLog('pwa').log('[PWA] Skipping SW registration — requires HTTPS or localhost');
    return;
}

        navigator.serviceWorker.register('/sw.js', { scope: '/' })
            .then(function (reg) {
                zmmLog('pwa').log('[PWA] Service worker registered, scope:', reg.scope);
                setInterval(function () {
                    reg.update();
                }, 60 * 60 * 1000);
            })
            .catch(function (err) {
                zmmLog('pwa').warn('[PWA] Service worker registration failed:', err);
            });
    }

    // 2. PLATFORM DETECTION & PREFERENCES

    function getPrefs() {
        try {
            var stored = localStorage.getItem(PREFS_KEY);
            if (stored) return JSON.parse(stored);
        } catch (e) {}
        return Object.assign({}, defaultPrefs);
    }

    function savePrefs(prefs) {
        localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    }

    function isIOS() {
        return /iPad|iPhone|iPod/.test(navigator.userAgent) ||
               (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    }

    function isStandalone() {
        return window.matchMedia('(display-mode: standalone)').matches ||
               window.navigator.standalone === true;
    }

    function isAndroid() {
        return /Android/.test(navigator.userAgent);
    }

    function getNotificationSupport() {
        // Secure context FIRST: the Notification and serviceWorker APIs exist on
        // an insecure origin and are refused only at the point of use, so feature
        // detection reports full support and delivery then fails silently. A
        // self-signed LAN origin is a cert error, so not a secure context.
        // See docs/notifications.md.
        if (!window.isSecureContext) {
            return 'insecure';
        }

        // Full native support
        if ('Notification' in window && 'serviceWorker' in navigator) {
            if (isIOS() && !isStandalone()) {
                return 'ios-browser'; // iOS Safari — needs PWA install first
            }
            return 'full';
        }
        // No support at all
        if (!('Notification' in window)) {
            return 'none';
        }
        // Basic support (no SW)
        return 'basic';
    }

    // 2b. NOTIFICATION PERMISSION

    async function requestPermission() {
        var support = getNotificationSupport();

        if (support === 'insecure') {
            // Name the remedy. "Not supported" would be wrong and would send
            // people hunting through browser settings that cannot fix it.
            if (window.toast) {
                window.toast.warning(
                    'Notifications need a secure connection. This page is served over ' +
                    'a certificate the browser does not trust, so it blocks them. ' +
                    'Open ZMM on your public/tunnel address, or install the hub\'s ' +
                    'certificate on this device, then try again.',
                    { duration: 12000 }
                );
            }
            return false;
        }

        if (support === 'none') {
            if (window.toast) window.toast.warning('Notifications are not supported in this browser');
            return false;
        }

        if (support === 'ios-browser') {
            if (window.toast) {
                window.toast.info(
                    'On iOS, notifications only work when the app is installed to your home screen. ' +
                    'Tap the Share button → "Add to Home Screen", then enable notifications from within the app.',
                    { duration: 10000 }
                );
            }
            return false;
        }

        if (Notification.permission === 'granted') return true;

        if (Notification.permission === 'denied') {
            if (window.toast) {
                window.toast.error(
                    'Notifications are blocked. Open your browser settings for this site and allow notifications.',
                    { duration: 8000 }
                );
            }
            return false;
        }

        var result = await Notification.requestPermission();
        if (result === 'granted') {
            // Fire and forget: a failed subscription must not make the
            // permission grant look like it failed.
            subscribeToPush();
        }
        return result === 'granted';
    }


    // WEB PUSH SUBSCRIPTION

    /**
     * Register this browser for server-initiated push. Distinct from
     * Notification.permission: that lets the page notify while it runs, this
     * lets the hub notify when nothing is open. Needs a trusted secure context,
     * so it is a no-op on the self-signed LAN address.
     */
    async function subscribeToPush() {
        if (!('serviceWorker' in navigator) || !('PushManager' in window)) return false;
        if (!window.isSecureContext) return false;
        if (Notification.permission !== 'granted') return false;

        try {
            var reg = await navigator.serviceWorker.ready;

            var existing = await reg.pushManager.getSubscription();
            if (existing) {
                // Re-post it anyway: the hub may have been rebuilt, and a
                // subscription it does not know about is one it cannot use.
                await postSubscription(existing);
                return true;
            }

            var r = await fetch('/api/push/key', { credentials: 'same-origin' });
            if (!r.ok) return false;
            var key = (await r.json()).key;

            var sub = await reg.pushManager.subscribe({
                // Required by Chrome: the hub must be able to read every
                // payload it sends, so silent pushes are not permitted.
                userVisibleOnly: true,
                applicationServerKey: urlBase64ToUint8Array(key),
            });
            await postSubscription(sub);
            return true;
        } catch (e) {
            if (window.zmmLog) zmmLog('pwa').warn('[push] subscribe failed', e);
            return false;
        }
    }

    async function postSubscription(sub) {
        var j = sub.toJSON();
        if (!j.keys || !j.keys.p256dh || !j.keys.auth) return;
        await fetch('/api/push/subscribe', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                endpoint: j.endpoint,
                p256dh: j.keys.p256dh,
                auth: j.keys.auth,
                label: navigator.userAgent.slice(0, 60),
            }),
        });
    }

    /** VAPID keys arrive base64url; PushManager wants raw bytes. */
    function urlBase64ToUint8Array(base64String) {
        var padding = '='.repeat((4 - base64String.length % 4) % 4);
        var base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
        var raw = window.atob(base64);
        var out = new Uint8Array(raw.length);
        for (var i = 0; i < raw.length; ++i) out[i] = raw.charCodeAt(i);
        return out;
    }

    window.zbmSubscribeToPush = subscribeToPush;

    /**
     * Render the push-delivery panel (status line + browser/server test
     * buttons) into any host element. Shared by the navbar bell modal and
     * Settings → Notifications, so both show the same truth about whether
     * this device can be pinged.
     */
    window.zbmRenderPushPanel = function (host) {
        if (!host) return;
        host.innerHTML =
            '<div class="d-flex gap-1">' +
              '<button class="btn btn-outline-primary btn-sm w-100" data-push-test="local">' +
                '<i class="fas fa-bell me-1"></i> Test (this browser)</button>' +
              '<button class="btn btn-outline-primary btn-sm w-100" data-push-test="server">' +
                '<i class="fas fa-paper-plane me-1"></i> Test (server push)</button>' +
            '</div>' +
            '<div class="small text-muted mt-2" data-push-status></div>';

        var statusEl = host.querySelector('[data-push-status]');

        host.querySelector('[data-push-test="local"]').onclick = async function () {
            var granted = await requestPermission();
            if (granted) {
                sendNotification('Test Notification',
                    'ZigBee Manager notifications are working!', 'test-' + Date.now());
                if (window.toast) window.toast.success('Test notification sent!');
            }
            renderPushStatusInto(statusEl);
        };

        host.querySelector('[data-push-test="server"]').onclick = async function () {
            var btn = this;
            btn.disabled = true;
            try {
                var granted = await requestPermission();
                if (!granted) return;
                await subscribeToPush();
                var r = await fetch('/api/push/test',
                                    { method: 'POST', credentials: 'same-origin' });
                var j = await r.json().catch(function () { return {}; });
                if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
                if (window.toast) window.toast.success(
                    'Server push sent — it should arrive as a notification ' +
                    'even with this tab closed.');
            } catch (e) {
                if (window.toast) window.toast.error(String(e.message || e), { duration: 10000 });
            } finally {
                btn.disabled = false;
                renderPushStatusInto(statusEl);
            }
        };

        renderPushStatusInto(statusEl);
    };

    /** One honest line naming the broken link, into a given element. */
    async function renderPushStatusInto(el) {
        if (!el) return;
        if (!window.isSecureContext) {
            el.innerHTML = '<i class="fas fa-triangle-exclamation text-warning me-1"></i>' +
                'Server push unavailable: untrusted origin. Open ZMM on the tunnel/public URL.';
            return;
        }
        if (!('Notification' in window) || !('serviceWorker' in navigator) ||
            !('PushManager' in window)) {
            el.innerHTML = 'Server push not supported by this browser.';
            return;
        }
        if (Notification.permission !== 'granted') {
            el.innerHTML = 'Notification permission not granted yet.';
            return;
        }
        try {
            var reg = await navigator.serviceWorker.ready;
            var sub = await reg.pushManager.getSubscription();
            el.innerHTML = sub
                ? '<i class="fas fa-circle-check text-success me-1"></i>' +
                  'This device is subscribed — server pushes can wake it.'
                : '<i class="fas fa-triangle-exclamation text-warning me-1"></i>' +
                  'Permission granted but no push subscription. Use "Test (server push)" to create one.';
        } catch (e) {
            el.innerHTML = 'Could not read push state: ' + String(e.message || e);
        }
    }

    // 3. SEND NOTIFICATION

    /**
     * Show a notification. Returns false ONLY when this channel is switched off,
     * so callers can fall back to their own in-app alert. A suppressed duplicate
     * returns true — it was handled. See docs/notifications.md.
     */
    function sendNotification(title, body, tag, options) {
        var prefs = getPrefs();
        if (!prefs.enabled) return false;

        // Suppress duplicate notifications within the cooldown window
        var key = tag || (title + ':' + body);
        var now = Date.now();
        var suppressMs = (prefs.suppressMinutes || 5) * 60 * 1000;

        if (notifHistory[key] && (now - notifHistory[key]) < suppressMs) {
            return true; // Too recent, deliberately silent
        }
        notifHistory[key] = now;

        var nativeSupported = ('Notification' in window) && Notification.permission === 'granted';

        if (nativeSupported) {
            // Use service worker notification if available (works in background)
            if (navigator.serviceWorker && navigator.serviceWorker.controller) {
                navigator.serviceWorker.ready.then(function (reg) {
                    reg.showNotification(title, {
                        body: body,
                        icon: '/static/images/zigbee-manager-logo.png',
                        badge: '/static/images/zigbee-manager-logo.png',
                        tag: tag || 'zbm-' + Date.now(),
                        // Replacing a tagged notification is silent unless
                        // renotify is set — the same defect that made every
                        // message after a thread's first arrive without a
                        // sound. A repeat alert is the point of a repeat.
                        renotify: true,
                        silent: false,
                        vibrate: [100, 50, 100],
                        requireInteraction: options && options.persistent || false,
                        data: options && options.data || {}
                    });
                });
            } else {
                // Fallback to basic Notification API
                try {
                    new Notification(title, {
                        body: body,
                        icon: '/static/images/zigbee-manager-logo.png',
                        tag: tag || 'zbm-' + Date.now()
                    });
                } catch (e) {
                    // Some mobile browsers throw on new Notification()
                    sendInAppNotification(title, body, tag);
                }
            }
        } else {
            // In-app fallback for browsers without notification support
            sendInAppNotification(title, body, tag);
        }
        return true;
    }

    /**
     * In-app notification fallback — uses the toast system + audio ping
     * Works on ALL browsers including iOS Safari without PWA install
     */
    function sendInAppNotification(title, body, tag) {
        if (!window.toast) return;

        // Map notification types to toast types
        var type = 'info';
        var lower = (title + ' ' + body).toLowerCase();
        if (lower.match(/offline|error|fail/)) type = 'error';
        else if (lower.match(/online|reached|success/)) type = 'success';
        else if (lower.match(/battery|warning|low/)) type = 'warning';

        window.toast[type](title + ': ' + body, { duration: 8000 });

        // Play a subtle notification sound if available
        try {
            var audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            var osc = audioCtx.createOscillator();
            var gain = audioCtx.createGain();
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            osc.type = 'sine';
            osc.frequency.value = type === 'error' ? 440 : type === 'warning' ? 523 : 659;
            gain.gain.value = 0.08;
            gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.3);
            osc.start(audioCtx.currentTime);
            osc.stop(audioCtx.currentTime + 0.3);
        } catch (e) {
            // Audio not available, silent fallback
        }
    }

    // 4. DEVICE STATE MONITORING

    function checkDeviceState(ieee, newState, deviceName) {
        var prefs = getPrefs();
        if (!prefs.enabled) return;

        var prev = previousStates[ieee] || {};
        var name = deviceName || ieee.slice(-8);

        // Device offline detection
        if (prefs.deviceOffline && prev.available === true && newState.available === false) {
            sendNotification(
                'Device Offline',
                name + ' has gone offline',
                'offline-' + ieee,
                { persistent: false }
            );
        }

        // Device online detection
        if (prefs.deviceOnline && prev.available === false && newState.available === true) {
            sendNotification(
                'Device Online',
                name + ' is back online',
                'online-' + ieee
            );
        }

        // Low battery warning
        if (prefs.lowBattery) {
            var battery = newState.battery || newState.battery_percentage;
            var prevBattery = prev.battery || prev.battery_percentage;

            if (battery !== undefined && battery <= 15) {
                // Only notify once when crossing the threshold
                if (prevBattery === undefined || prevBattery > 15) {
                    sendNotification(
                        'Low Battery',
                        name + ' battery is at ' + battery + '%',
                        'battery-' + ieee,
                        { persistent: true }
                    );
                }
            }
        }

        // Thermostat target reached
        if (prefs.thermostatReached) {
            var target = newState.occupied_heating_setpoint || newState.heating_setpoint;
            var current = newState.internal_temperature || newState.temperature || newState.local_temperature;
            var prevCurrent = prev.internal_temperature || prev.temperature || prev.local_temperature;

            if (target && current && prevCurrent) {
                var targetNum = Number(target);
                var currentNum = Number(current);
                var prevNum = Number(prevCurrent);

                // Notify when temperature crosses the target threshold (within 0.3°C)
                if (prevNum < targetNum - 0.3 && currentNum >= targetNum - 0.3) {
                    sendNotification(
                        'Target Temperature Reached',
                        name + ' has reached ' + currentNum.toFixed(1) + '°C (target: ' + targetNum.toFixed(1) + '°C)',
                        'temp-reached-' + ieee
                    );
                }
            }
        }

        // Store current state for next comparison
        previousStates[ieee] = Object.assign({}, prev, newState);
    }

    // Expose for the WebSocket handler to call
    window.zbmCheckDeviceState = checkDeviceState;
    window.zbmSendNotification = sendNotification;

    // 5. HOOK INTO WEBSOCKET UPDATES

    var _hookTries = 0;

    function hookWebSocket() {
        // Patch the global handleDeviceUpdate if it exists
        // We watch for state.deviceCache changes via MutationObserver on the table
        // as a simpler hook that doesn't require modifying existing modules

        var tbody = document.getElementById('deviceTableBody');
        if (!tbody) {
            // Bounded: pages without a device table (Frames) would otherwise
            // retry every second for the life of the tab, forever.
            if (++_hookTries > 30) return;
            setTimeout(hookWebSocket, 1000);
            return;
        }

        // Use a polling approach to check for state changes
        // This works because devices.js updates state.deviceCache on every WS message
        setInterval(function () {
            if (!window.state || !window.state.deviceCache) return;

            var cache = window.state.deviceCache;
            Object.keys(cache).forEach(function (ieee) {
                var device = cache[ieee];
                if (!device || !device.state) return;

                var stateWithMeta = Object.assign({}, device.state, {
                    available: device.available
                });

                checkDeviceState(ieee, stateWithMeta, device.friendly_name);
            });
        }, 5000); // Check every 5 seconds
    }

    // 6. NOTIFICATION BELL + SETTINGS PANEL

    function createNotificationBell() {
        var navbar = document.querySelector('.navbar .d-flex.align-items-center.gap-3');
        if (!navbar) return;

        var prefs = getPrefs();

        var btn = document.createElement('button');
        btn.id = 'zbm-notif-bell';
        btn.className = 'btn btn-sm btn-outline-light border-0';
        btn.title = 'Notification settings';
        btn.style.cssText = 'font-size: 1rem; padding: 0.25rem 0.5rem; opacity: 0.8; transition: opacity 0.2s; position: relative;';
        btn.innerHTML = prefs.enabled
            ? '<i class="fas fa-bell"></i>'
            : '<i class="fas fa-bell-slash"></i>';
        btn.onmouseenter = function () { this.style.opacity = '1'; };
        btn.onmouseleave = function () { this.style.opacity = '0.8'; };

        btn.addEventListener('click', function () {
            openNotificationSettings();
        });

        // Insert before the theme toggle if present, otherwise before pairing group
        var themeBtn = document.getElementById('themeToggleBtn');
        if (themeBtn) {
            navbar.insertBefore(btn, themeBtn);
        } else {
            var pairingGroup = navbar.querySelector('.btn-group');
            if (pairingGroup) {
                navbar.insertBefore(btn, pairingGroup);
            } else {
                navbar.appendChild(btn);
            }
        }
    }

    function updateBellIcon() {
        var btn = document.getElementById('zbm-notif-bell');
        if (!btn) return;
        var prefs = getPrefs();
        btn.innerHTML = prefs.enabled
            ? '<i class="fas fa-bell"></i>'
            : '<i class="fas fa-bell-slash"></i>';
    }

    function openNotificationSettings() {
        // Remove existing modal if present
        var existing = document.getElementById('zbm-notif-modal');
        if (existing) existing.remove();

        var prefs = getPrefs();
        var support = getNotificationSupport();
        var permissionStatus = ('Notification' in window) ? Notification.permission : 'unsupported';

        // Build platform-specific status alert
        var statusAlert = '';
        if (support === 'insecure') {
            // The most common reason delivery silently fails here, and the one
            // no amount of toggling in this dialog can fix.
            statusAlert =
                '<div class="alert alert-warning small mb-3">' +
                    '<i class="fas fa-lock-open me-1"></i>' +
                    '<strong>Insecure connection</strong> — the browser blocks notifications ' +
                    'on this address, so nothing will be delivered no matter what is enabled below.' +
                    '<br><br>' +
                    'You are on <code>' + String(location.origin).replace(/</g, '&lt;') + '</code>, ' +
                    'whose certificate this device does not trust. Either:' +
                    '<ul class="mb-0 mt-1" style="padding-left: 1.2rem;">' +
                        '<li>open ZMM on your public/tunnel address (recommended), or</li>' +
                        '<li>install the hub\'s certificate on this device.</li>' +
                    '</ul>' +
                '</div>';
        } else if (support === 'ios-browser') {
            statusAlert =
                '<div class="alert alert-warning small mb-3">' +
                    '<i class="fas fa-mobile-alt me-1"></i>' +
                    '<strong>iOS detected</strong> — notifications require the app to be installed on your home screen.' +
                    '<br><br>' +
                    '<strong>How to install:</strong>' +
                    '<ol class="mb-0 mt-1" style="padding-left: 1.2rem;">' +
                        '<li>Tap the <i class="fas fa-share-square"></i> <strong>Share</strong> button in Safari</li>' +
                        '<li>Scroll down and tap <strong>"Add to Home Screen"</strong></li>' +
                        '<li>Open the app from your home screen</li>' +
                        '<li>Come back here and enable notifications</li>' +
                    '</ol>' +
                '</div>';
        } else if (support === 'none') {
            statusAlert =
                '<div class="alert alert-danger small mb-3">' +
                    '<i class="fas fa-times-circle me-1"></i>' +
                    'Notifications are not supported in this browser. ' +
                    'In-app alerts (toasts with sound) will be used as a fallback.' +
                '</div>';
        } else if (permissionStatus === 'denied') {
            statusAlert =
                '<div class="alert alert-danger small mb-3">' +
                    '<i class="fas fa-times-circle me-1"></i>' +
                    'Notifications are <strong>blocked</strong> by your browser.' +
                    '<br><small>' + (isAndroid() ?
                        'Open Chrome menu → Settings → Site settings → Notifications → Allow for this site' :
                        'Open browser settings for this site and allow notifications') +
                    '</small>' +
                '</div>';
        } else if (permissionStatus === 'granted') {
            statusAlert =
                '<div class="alert alert-success small mb-3">' +
                    '<i class="fas fa-check-circle me-1"></i>' +
                    'Notifications are <strong>enabled</strong>' +
                    (isStandalone() ? ' — running as installed app' : '') +
                '</div>';
        } else {
            statusAlert =
                '<div class="alert alert-info small mb-3">' +
                    '<i class="fas fa-info-circle me-1"></i>' +
                    'Browser permission: <strong>' + permissionStatus + '</strong> — you\'ll be prompted when you enable notifications' +
                '</div>';
        }

        // In-app fallback notice
        var fallbackNotice = '';
        if (support !== 'full' && support !== 'basic') {
            fallbackNotice =
                '<div class="alert alert-info small mb-3">' +
                    '<i class="fas fa-bell me-1"></i>' +
                    '<strong>In-app mode:</strong> Notifications will appear as toast alerts with a sound ping when the app is open.' +
                '</div>';
        }

        var modal = document.createElement('div');
        modal.id = 'zbm-notif-modal';
        modal.className = 'modal fade';
        modal.tabIndex = -1;
        modal.innerHTML =
            '<div class="modal-dialog">' +
                '<div class="modal-content">' +
                    '<div class="modal-header">' +
                        '<h5 class="modal-title"><i class="fas fa-bell me-2"></i>Notification Settings</h5>' +
                        '<button type="button" class="btn-close" data-bs-dismiss="modal"></button>' +
                    '</div>' +
                    '<div class="modal-body">' +

                        // Platform-specific status
                        statusAlert +
                        fallbackNotice +

                        // Master toggle
                        '<div class="form-check form-switch mb-3 pb-3 border-bottom">' +
                            '<input class="form-check-input" type="checkbox" id="zbm-notif-enabled" ' + (prefs.enabled ? 'checked' : '') + '>' +
                            '<label class="form-check-label fw-bold" for="zbm-notif-enabled">Enable notifications</label>' +
                        '</div>' +

                        // Individual toggles
                        '<div id="zbm-notif-options" style="' + (prefs.enabled ? '' : 'opacity:0.5;pointer-events:none;') + '">' +
                            '<div class="form-check form-switch mb-2">' +
                                '<input class="form-check-input" type="checkbox" id="zbm-notif-offline" ' + (prefs.deviceOffline ? 'checked' : '') + '>' +
                                '<label class="form-check-label" for="zbm-notif-offline">' +
                                    '<i class="fas fa-plug text-danger me-1"></i> Device goes offline' +
                                '</label>' +
                            '</div>' +
                            '<div class="form-check form-switch mb-2">' +
                                '<input class="form-check-input" type="checkbox" id="zbm-notif-online" ' + (prefs.deviceOnline ? 'checked' : '') + '>' +
                                '<label class="form-check-label" for="zbm-notif-online">' +
                                    '<i class="fas fa-plug text-success me-1"></i> Device comes online' +
                                '</label>' +
                            '</div>' +
                            '<div class="form-check form-switch mb-2">' +
                                '<input class="form-check-input" type="checkbox" id="zbm-notif-battery" ' + (prefs.lowBattery ? 'checked' : '') + '>' +
                                '<label class="form-check-label" for="zbm-notif-battery">' +
                                    '<i class="fas fa-battery-quarter text-warning me-1"></i> Low battery warning (&lt;15%)' +
                                '</label>' +
                            '</div>' +
                            '<div class="form-check form-switch mb-3">' +
                                '<input class="form-check-input" type="checkbox" id="zbm-notif-thermostat" ' + (prefs.thermostatReached ? 'checked' : '') + '>' +
                                '<label class="form-check-label" for="zbm-notif-thermostat">' +
                                    '<i class="fas fa-thermometer-half text-info me-1"></i> Thermostat target reached' +
                                '</label>' +
                            '</div>' +

                            // Cooldown
                            '<div class="mb-3">' +
                                '<label class="form-label small fw-bold">Suppress duplicates for</label>' +
                                '<select class="form-select form-select-sm" id="zbm-notif-suppress">' +
                                    '<option value="1" ' + (prefs.suppressMinutes === 1 ? 'selected' : '') + '>1 minute</option>' +
                                    '<option value="5" ' + (prefs.suppressMinutes === 5 ? 'selected' : '') + '>5 minutes</option>' +
                                    '<option value="15" ' + (prefs.suppressMinutes === 15 ? 'selected' : '') + '>15 minutes</option>' +
                                    '<option value="30" ' + (prefs.suppressMinutes === 30 ? 'selected' : '') + '>30 minutes</option>' +
                                    '<option value="60" ' + (prefs.suppressMinutes === 60 ? 'selected' : '') + '>1 hour</option>' +
                                '</select>' +
                            '</div>' +
                        '</div>' +

                        // Shared push panel: status + browser/server tests.
                        '<div id="zbm-push-panel"></div>' +

                    '</div>' +
                    '<div class="modal-footer">' +
                        '<button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>' +
                    '</div>' +
                '</div>' +
            '</div>';

        document.body.appendChild(modal);

        // Bind events
        var masterToggle = document.getElementById('zbm-notif-enabled');
        var optionsDiv = document.getElementById('zbm-notif-options');

        masterToggle.addEventListener('change', async function () {
            if (this.checked) {
                var support = getNotificationSupport();

                if (support === 'full' || support === 'basic') {
                    // Try to get native permission
                    var granted = await requestPermission();
                    if (!granted) {
                        // Fall back to in-app mode silently
                        if (window.toast) window.toast.info('Using in-app notifications (toast alerts with sound)');
                    }
                } else if (support === 'ios-browser') {
                    // iOS without PWA — allow in-app mode
                    if (window.toast) {
                        window.toast.info(
                            'Notifications will appear as in-app alerts. Install to home screen for native notifications.',
                            { duration: 6000 }
                        );
                    }
                }
                // Always allow enabling (in-app fallback works everywhere)
            }
            optionsDiv.style.opacity = this.checked ? '1' : '0.5';
            optionsDiv.style.pointerEvents = this.checked ? 'auto' : 'none';
            saveCurrentPrefs();
        });

        // Save on any toggle change
        ['zbm-notif-offline', 'zbm-notif-online', 'zbm-notif-battery', 'zbm-notif-thermostat', 'zbm-notif-suppress'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.addEventListener('change', saveCurrentPrefs);
        });

        // Shared push panel (status + tests) — same one Settings →
        // Notifications shows.
        window.zbmRenderPushPanel(document.getElementById('zbm-push-panel'));

        var bsModal = new bootstrap.Modal(modal);
        bsModal.show();

        // Cleanup on close
        modal.addEventListener('hidden.bs.modal', function () {
            modal.remove();
        });
    }

    function saveCurrentPrefs() {
        var prefs = {
            enabled: document.getElementById('zbm-notif-enabled').checked,
            deviceOffline: document.getElementById('zbm-notif-offline').checked,
            deviceOnline: document.getElementById('zbm-notif-online').checked,
            lowBattery: document.getElementById('zbm-notif-battery').checked,
            thermostatReached: document.getElementById('zbm-notif-thermostat').checked,
            suppressMinutes: parseInt(document.getElementById('zbm-notif-suppress').value) || 5
        };
        savePrefs(prefs);
        updateBellIcon();
    }

    // 7. INIT

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            registerServiceWorker();
            setTimeout(createNotificationBell, 300);
            setTimeout(hookWebSocket, 2000);
            setTimeout(healPushSubscription, 4000);
        });
    } else {
        registerServiceWorker();
        setTimeout(createNotificationBell, 300);
        setTimeout(hookWebSocket, 2000);
        setTimeout(healPushSubscription, 4000);
    }

    /**
     * Re-assert the push subscription on every page load. Subscribing only at
     * the moment permission was granted left devices silently unreachable after
     * a browser-rotated subscription or a hub rebuild.
     */
    function healPushSubscription() {
        if (!window.isSecureContext) return;
        if (!('Notification' in window) || Notification.permission !== 'granted') return;
        subscribeToPush().then(function (ok) {
            if (!ok) zmmLog('pwa').warn('[push] subscription heal failed');
        });
    }

})();