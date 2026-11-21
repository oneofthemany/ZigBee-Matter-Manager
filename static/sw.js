/* ZigBee Matter Manager — Service Worker (PWA) */

// The /sw.js route appends the app version and a fingerprint of static/, so any
// changed file gets a new name and `activate` purges the old cache.
var CACHE_NAME = 'zbm-v12';

// /static path -> content hash, written by the same route. Install carries each
// file whose hash is unchanged over from the previous cache, so a deploy costs
// a download of only what it changed. Each cache stores the map it was built
// against under MANIFEST_URL; a cache without one is never trusted.
var ASSET_HASHES = {};
var MANIFEST_URL = '/__zbm_asset_hashes__';

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

    '/static/images/zigbee-manager-logo-192.png',
    '/static/js/presence.js',
    '/static/js/presence-settings.js'
];

// Install: carry unchanged files over, then fetch whatever of the shell is missing.
self.addEventListener('install', function (event) {
    event.waitUntil(
        caches.open(CACHE_NAME).then(function (cache) {
            return carryOver(cache).then(function () {
                return Promise.all(APP_SHELL.map(function (u) { return cache.match(u); }));
            }).then(function (hits) {
                var missing = APP_SHELL.filter(function (u, i) { return !hits[i]; });
                console.log('[SW] Caching app shell:', missing.length, 'of', APP_SHELL.length);
                return cache.addAll(missing).catch(function (err) {
                    // Don't fail install if some assets can't be cached
                    console.warn('[SW] Some assets failed to cache:', err);
                });
            }).then(function () {
                return cache.put(MANIFEST_URL, new Response(JSON.stringify(ASSET_HASHES),
                    { headers: { 'Content-Type': 'application/json' } }));
            });
        })
    );
    // Activate immediately
    self.skipWaiting();
});

// A failed carry-over only costs downloads, so it never fails the install.
function carryOver(cache) {
    return caches.keys().then(function (names) {
        return names.filter(function (n) { return n !== CACHE_NAME && n.indexOf('zbm-') === 0; })
            .reduce(function (p, name) {
                return p.then(function () { return carryFrom(caches.open(name), cache); });
            }, Promise.resolve());
    }).catch(function (err) { console.warn('[SW] Carry-over skipped:', err); });
}

function carryFrom(opening, cache) {
    return opening.then(function (old) {
        return old.match(MANIFEST_URL).then(function (res) {
            if (!res) return;
            return res.json().then(function (prev) {
                return Promise.all(Object.keys(ASSET_HASHES).map(function (path) {
                    if (prev[path] !== ASSET_HASHES[path]) return;
                    return cache.match(path).then(function (have) {
                        if (have) return;
                        return old.match(path).then(function (r) { if (r) return cache.put(path, r); });
                    });
                }));
            });
        });
    });
}

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

    // Static assets: cache-first, network only on a miss. No background refresh:
    // CACHE_NAME carries a fingerprint of static/, so a changed file arrives as
    // a new worker whose cache lacks it, and a page load costs no asset requests.
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.match(event.request).then(function (cached) {
                if (cached) return cached;
                return fetch(event.request).then(function (response) {
                    if (response.ok) {
                        var clone = response.clone();
                        caches.open(CACHE_NAME).then(function (cache) {
                            cache.put(event.request, clone);
                        });
                    }
                    return response;
                });
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
    var data = { title: 'ZigBee Manager', body: 'Device update', icon: '/static/images/zigbee-manager-logo-192.png' };

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
            icon: data.icon || '/static/images/zigbee-manager-logo-192.png',
            badge: '/static/images/zigbee-manager-logo-192.png',
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