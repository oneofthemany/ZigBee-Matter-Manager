/* ============================================================
   ZigBee Matter Manager — Service Worker (PWA)
   ============================================================ */

// Bump this on every frontend change — the `activate` handler purges any
// cache whose name != CACHE_NAME, so a new version wipes stale cached assets.
var CACHE_NAME = 'zbm-v9';

// App shell files to cache on install.
//
// Everything under /static/ is also cached opportunistically by the fetch
// handler below, so this list is specifically about the FIRST load working
// offline. It must therefore carry every stylesheet the two pages link, not a
// subset: Bootstrap supplies .tab-pane/.modal/.d-none display rules, so a page
// that loads without it paints all 35 tab panes and every modal at once.
var APP_SHELL = [
    '/',
    '/frames',
    '/static/index.html',
    '/static/frames.html',

    // Vendored third-party CSS — load-bearing for layout and icons
    '/static/vendor/bootstrap.min.css',
    '/static/vendor/bootstrap.bundle.min.js',
    '/static/vendor/fontawesome/css/all.min.css',
    '/static/vendor/fontawesome/webfonts/fa-solid-900.woff2',
    '/static/vendor/fontawesome/webfonts/fa-regular-400.woff2',
    '/static/vendor/fontawesome/webfonts/fa-brands-400.woff2',
    '/static/vendor/leaflet.css',

    // Every stylesheet referenced by index.html / frames.html
    '/static/css/adblock.css',
    '/static/css/automations.css',
    '/static/css/dark-mode.css',
    '/static/css/debug.css',
    '/static/css/device-status.css',
    '/static/css/floor-plan.css',
    '/static/css/frames.css',
    '/static/css/frames-page.css',
    '/static/css/groups.css',
    '/static/css/hive-components.css',
    '/static/css/hive-tokens.css',
    '/static/css/mesh.css',
    '/static/css/mobile.css',
    '/static/css/mqtt-explorer.css',
    '/static/css/notifications.css',
    '/static/css/setup-wizard.css',
    '/static/css/table.css',
    '/static/css/toasts.css',
    '/static/css/upgrade.css',
    '/static/css/wiki.css',

    '/static/images/zigbee-manager-logo.png',
    '/static/js/presence.js',
    '/static/js/presence-settings.js'
];

// Install: cache app shell
self.addEventListener('install', function (event) {
    event.waitUntil(
        caches.open(CACHE_NAME).then(function (cache) {
            console.log('[SW] Caching app shell');
            return cache.addAll(APP_SHELL).catch(function (err) {
                // Don't fail install if some assets can't be cached
                console.warn('[SW] Some assets failed to cache:', err);
            });
        })
    );
    // Activate immediately
    self.skipWaiting();
});

// Activate: clean old caches
self.addEventListener('activate', function (event) {
    event.waitUntil(
        caches.keys().then(function (names) {
            return Promise.all(
                names.filter(function (name) {
                    return name !== CACHE_NAME;
                }).map(function (name) {
                    console.log('[SW] Removing old cache:', name);
                    return caches.delete(name);
                })
            );
        })
    );
    // Take control of all pages immediately
    self.clients.claim();
});

// Fetch: network-first for API, cache-first for static assets
self.addEventListener('fetch', function (event) {
    var url = new URL(event.request.url);

    // Skip non-GET requests
    if (event.request.method !== 'GET') return;

    // Skip WebSocket upgrade requests
    if (url.protocol === 'ws:' || url.protocol === 'wss:') return;

    // API calls: network-first (always try server, fallback to cache)
    if (url.pathname.startsWith('/api/')) {
        event.respondWith(
            fetch(event.request).then(function (response) {
                return response;
            }).catch(function () {
                return caches.match(event.request);
            })
        );
        return;
    }

    // Static assets: cache-first with network update
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.match(event.request).then(function (cached) {
                var fetchPromise = fetch(event.request).then(function (response) {
                    // Update cache with fresh version
                    if (response.ok) {
                        var clone = response.clone();
                        caches.open(CACHE_NAME).then(function (cache) {
                            cache.put(event.request, clone);
                        });
                    }
                    return response;
                }).catch(function () {
                    // Network failed, cached version already returned
                });

                return cached || fetchPromise;
            })
        );
        return;
    }

    // Main page: network-first.
    // Offline, fall back to the page that was actually asked for — /frames must
    // not resolve to the dashboard, or a phone offline lands in the wrong app.
    var isFrames = url.pathname === '/frames';
    event.respondWith(
        fetch(event.request).catch(function () {
            return isFrames
                ? caches.match('/frames').then(function (r) { return r || caches.match('/static/frames.html'); })
                : caches.match('/').then(function (r) { return r || caches.match('/static/index.html'); });
        })
    );
});

// Push notification handling
self.addEventListener('push', function (event) {
    var data = { title: 'ZigBee Manager', body: 'Device update', icon: '/static/images/zigbee-manager-logo.png' };

    try {
        if (event.data) {
            data = event.data.json();
        }
    } catch (e) {
        if (event.data) {
            data.body = event.data.text();
        }
    }

    event.waitUntil(
        self.registration.showNotification(data.title || 'ZigBee Manager', {
            body: data.body || '',
            icon: data.icon || '/static/images/zigbee-manager-logo.png',
            badge: '/static/images/zigbee-manager-logo.png',
            tag: data.tag || 'zbm-notification',
            // Tags collapse a burst into one notification — but replacing a
            // tagged notification is SILENT unless renotify is set, which
            // made every message after a thread's first arrive without a
            // sound. Re-alerting on each replacement is the whole point of
            // a ping.
            renotify: true,
            silent: false,
            data: Object.assign({}, data.data || {}, {
                kind: data.kind || null
            }),
            vibrate: [100, 50, 100],
            requireInteraction: data.requireInteraction || false
        })
    );
});

// Notification click: focus or open the app
self.addEventListener('notificationclick', function (event) {
    var d = event.notification.data || {};
    event.notification.close();

    // A tapped message opens the app onto the conversation: #messages is
    // read by messages-ui.js at startup and opens the panel.
    var target = d.kind === 'message_created' ? '/#messages' : '/';

    event.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (clients) {
            // Focus existing window if open
            for (var i = 0; i < clients.length; i++) {
                if (clients[i].url.includes(self.location.origin)) {
                    return clients[i].focus();
                }
            }
            // Otherwise open new window
            return self.clients.openWindow(target);
        })
    );
});