/* Namespaced browser-console logger — every JS module logs through a named
   logger rather than raw console.* calls:

     const log = zmmLog('groups');
     log.log(...)    // gated      log.error(...)  // always printed

   Silent by default; enable from the Debug tab or zmmLog.enable('*'). The
   selection persists in localStorage under 'zmm.debug'. Classic script — must
   load before every other app script. See docs/debugging.md. */

(function () {
    'use strict';

    const STORAGE_KEY = 'zmm.debug';

    // Pre-registered so the toggle UI can list every namespace even
    // before its module has loaded / logged anything.
    const KNOWN_NAMESPACES = [
        'actions', 'api-docs', 'auth', 'auth-settings', 'automations-page',
        'devices', 'editor', 'floor-plan', 'groups', 'heating',
        'heating-controller', 'interview-status-badge', 'logging', 'main',
        'media', 'mesh', 'modal-control', 'modal-device-settings',
        'modal-ota', 'modal-schedule', 'mqtt-explorer', 'my-account',
        'notifications', 'packet-analysis', 'presence', 'presence-settings',
        'pwa', 'remote-access', 'settings', 'setup-wizard', 'toasts',
        'upgrade', 'utils', 'websocket', 'zones',
    ];

    let allOn = false;
    let enabled = new Set();

    function load() {
        let raw = '';
        try { raw = localStorage.getItem(STORAGE_KEY) || ''; } catch (_) { /* private mode */ }
        allOn = raw.trim() === '*';
        enabled = new Set(allOn ? [] : raw.split(',').map(s => s.trim()).filter(Boolean));
    }

    function save() {
        try {
            localStorage.setItem(STORAGE_KEY, allOn ? '*' : Array.from(enabled).sort().join(','));
        } catch (_) { /* private mode */ }
    }

    load();

    const registry = new Set(KNOWN_NAMESPACES);
    const cache = Object.create(null);

    function isOn(ns) { return allOn || enabled.has(ns); }

    function zmmLog(ns) {
        if (cache[ns]) return cache[ns];
        registry.add(ns);
        const prefix = '[' + ns + ']';
        const gated = (fn) => (...args) => { if (isOn(ns)) console[fn](prefix, ...args); };
        cache[ns] = {
            debug: gated('debug'),
            log:   gated('log'),
            info:  gated('info'),
            warn:  gated('warn'),
            // Errors are rare and load-bearing — never suppressed.
            error: (...args) => console.error(prefix, ...args),
        };
        return cache[ns];
    }

    zmmLog.namespaces = () => Array.from(registry).sort();
    zmmLog.isEnabled = isOn;

    zmmLog.enable = (ns) => {
        if (ns === '*') { allOn = true; enabled.clear(); }
        else { registry.add(ns); enabled.add(ns); }
        save();
    };

    zmmLog.disable = (ns) => {
        if (ns === '*') { allOn = false; enabled.clear(); }
        else {
            // Dropping one namespace out of '*' materialises the rest.
            if (allOn) { allOn = false; enabled = new Set(registry); }
            enabled.delete(ns);
        }
        save();
    };

    window.zmmLog = zmmLog;
})();
