/* md and up the main tab list is a left sidebar (hive-components.css); this
   collapses it to icons and remembers the choice per browser. Below md the
   same list is mobile.css's icon rail and the button is hidden. */
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
