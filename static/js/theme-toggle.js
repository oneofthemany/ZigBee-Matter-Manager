/* ============================================================
   ZigBee Matter Manager — Theme Toggle (Dark Mode)
   ============================================================ */

(function () {
    'use strict';

    var STORAGE_KEY = 'zbm-theme';

    // ----------------------------------------------------------
    // 1. DETERMINE INITIAL THEME
    // ----------------------------------------------------------

    function getPreferredTheme() {
        var stored = localStorage.getItem(STORAGE_KEY);
        if (stored) return stored;

        // Respect OS preference on first visit
        if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
            return 'light';
        }
        // Dark hive is the house default
        return 'dark';
    }

    var TRANSITION_MS = 250;
    var _transitionTimer = null;

    /**
     * Turn the colour transition on for the length of one toggle. The rule
     * lives behind .theme-transition in dark-mode.css — if it were always on,
     * every hover would lag 200ms behind the pointer.
     */
    function withTransition(fn) {
        var root = document.documentElement;
        root.classList.add('theme-transition');
        fn();
        clearTimeout(_transitionTimer);
        _transitionTimer = setTimeout(function () {
            root.classList.remove('theme-transition');
        }, TRANSITION_MS);
    }

    /**
     * @param {string} theme            'dark' | 'light'
     * @param {boolean} [persist=true]  false when following the OS — writing
     *   the key would count as an explicit user preference and the app would
     *   stop following the OS from then on.
     */
    function setTheme(theme, persist) {
        withTransition(function () {
            document.documentElement.setAttribute('data-theme', theme);
            // Keep Bootstrap 5.3's native theming in lockstep so its components
            // (tables, modals, dropdowns) theme themselves without overrides.
            document.documentElement.setAttribute('data-bs-theme', theme);
        });
        if (persist !== false) localStorage.setItem(STORAGE_KEY, theme);
        updateToggleButton(theme);
        // Notify listeners (charts, canvas widgets etc.) so they can redraw
        // with theme-appropriate colours without waiting for the next tick.
        document.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
    }

    // ----------------------------------------------------------
    // 2. INITIAL THEME
    // ----------------------------------------------------------
    // Already applied by the inline snippet in the <head> of index.html /
    // frames.html — it has to run before the first stylesheet or the page
    // paints light and flips to dark. This script loads at the end of <body>
    // and only reads back what that snippet decided.

    var initialTheme = document.documentElement.getAttribute('data-theme');
    if (!initialTheme) {
        // No inline snippet on this page — apply now and accept the flash.
        // The fix is to add the snippet to that page's <head>, not here.
        initialTheme = getPreferredTheme();
        document.documentElement.setAttribute('data-theme', initialTheme);
        document.documentElement.setAttribute('data-bs-theme', initialTheme);
    }

    // ----------------------------------------------------------
    // 3. INJECT TOGGLE BUTTON INTO NAVBAR
    // ----------------------------------------------------------

    function createToggleButton() {
        var navbar = document.querySelector('.navbar .d-flex.align-items-center.gap-3');
        if (!navbar) {
            navbar = document.querySelector('.navbar .container-fluid');
        }
        if (!navbar) return;

        var btn = document.createElement('button');
        btn.id = 'themeToggleBtn';
        btn.className = 'btn btn-sm btn-outline-light border-0';
        btn.title = 'Toggle dark/light mode';
        btn.setAttribute('aria-label', 'Toggle dark/light mode');
        btn.style.cssText = 'font-size: 1rem; padding: 0.25rem 0.5rem; opacity: 0.8; transition: opacity 0.2s;';
        btn.onmouseenter = function() { this.style.opacity = '1'; };
        btn.onmouseleave = function() { this.style.opacity = '0.8'; };

        btn.addEventListener('click', function () {
            var current = document.documentElement.getAttribute('data-theme');
            var next = current === 'dark' ? 'light' : 'dark';
            setTheme(next);
        });

        // Insert before the pairing btn-group
        var pairingGroup = navbar.querySelector('.btn-group');
        if (pairingGroup) {
            navbar.insertBefore(btn, pairingGroup);
        } else {
            navbar.appendChild(btn);
        }

        updateToggleButton(initialTheme);
    }

    function updateToggleButton(theme) {
        var btn = document.getElementById('themeToggleBtn');
        if (!btn) return;

        if (theme === 'dark') {
            btn.innerHTML = '<i class="fas fa-sun"></i>';
            btn.title = 'Switch to light mode';
        } else {
            btn.innerHTML = '<i class="fas fa-moon"></i>';
            btn.title = 'Switch to dark mode';
        }
    }

    // ----------------------------------------------------------
    // 4. LISTEN FOR OS THEME CHANGES
    // ----------------------------------------------------------

    if (window.matchMedia) {
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function (e) {
            // Only auto-switch if user hasn't explicitly set a preference —
            // and don't persist, or this would become one.
            if (!localStorage.getItem(STORAGE_KEY)) {
                setTheme(e.matches ? 'dark' : 'light', false);
            }
        });
    }

    // ----------------------------------------------------------
    // 5. INIT ON DOM READY
    // ----------------------------------------------------------

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', createToggleButton);
    } else {
        createToggleButton();
    }

})();