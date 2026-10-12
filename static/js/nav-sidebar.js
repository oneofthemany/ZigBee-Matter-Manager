/* The main tab list is a sidebar (hive-components.css). md and up this
   collapses it to icons and remembers the choice per browser; below md it
   opens and closes it as a drawer. */
(function () {
    'use strict';

    var rail = document.getElementById('mainTabs');
    var btn = document.getElementById('mainTabsCollapse');
    if (!rail || !btn) return;

    var KEY = 'zmm.navCollapsed';

    // Collapsed links show only their icon, so the name moves to a tooltip.
    rail.querySelectorAll('.nav-link').forEach(function (link) {
        var label = link.querySelector('.tab-label');
        if (label && !link.title) link.title = label.textContent.trim();
    });

    function apply(collapsed) {
        rail.classList.toggle('sidebar-collapsed', collapsed);
        btn.setAttribute('aria-expanded', String(!collapsed));
        btn.setAttribute('aria-label', collapsed ? 'Expand navigation' : 'Collapse navigation');
        btn.title = collapsed ? 'Expand' : 'Collapse';
    }

    var stored = false;
    try { stored = localStorage.getItem(KEY) === '1'; } catch (e) { /* storage blocked */ }
    apply(stored);

    btn.addEventListener('click', function () {
        var collapsed = !rail.classList.contains('sidebar-collapsed');
        apply(collapsed);
        try { localStorage.setItem(KEY, collapsed ? '1' : '0'); } catch (e) { /* storage blocked */ }
    });
})();


(function () {
    'use strict';

    var rail = document.getElementById('mainTabs');
    var btn = document.getElementById('mainTabsOpen');
    var backdrop = document.getElementById('mainTabsBackdrop');
    if (!rail || !btn || !backdrop) return;

    var phone = window.matchMedia('(max-width: 767.98px)');

    function setOpen(open) {
        rail.classList.toggle('drawer-open', open);
        backdrop.classList.toggle('show', open);
        btn.setAttribute('aria-expanded', String(open));
        if (open) {
            var active = rail.querySelector('.nav-link.active') || rail.querySelector('.nav-link');
            if (active) active.focus({ preventScroll: true });
        }
    }

    function close(returnFocus) {
        if (!rail.classList.contains('drawer-open')) return;
        setOpen(false);
        if (returnFocus) btn.focus();
    }

    btn.addEventListener('click', function () { setOpen(!rail.classList.contains('drawer-open')); });
    backdrop.addEventListener('click', function () { close(false); });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && !document.querySelector('.modal.show')) close(true);
    });

    // Picking a tab is the end of the trip, Tools' submenu items included.
    rail.addEventListener('shown.bs.tab', function () { close(false); });

    // Widening past md turns the drawer back into the docked sidebar.
    phone.addEventListener('change', function (e) { if (!e.matches) close(false); });
})();
