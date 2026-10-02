/* Tabs grouped under a dropdown inside a tab rail (main Tools, device Advanced). */
(function () {
    'use strict';

    // Same rails mobile-helpers.js gives a scroll-fade mask.
    var RAIL_SELECTOR = '#mainTabs, #devTabs, .zmm-icon-rail';

    // Bootstrap 5.3.0's Tab adds .show to the menu holding the tab it activates,
    // after this event, so close that menu on the next tick.
    document.addEventListener('show.bs.tab', function (e) {
        var menu = e.target.closest('.dropdown-menu');
        var toggle = menu && menu.parentElement.querySelector('[data-bs-toggle="dropdown"]');
        if (!toggle) return;
        setTimeout(function () { bootstrap.Dropdown.getOrCreateInstance(toggle).hide(); });
    });

    // The rail's mask clips its descendants, fixed-position menus included.
    function setOpen(e, open) {
        var rail = e.target.closest(RAIL_SELECTOR);
        if (rail) rail.classList.toggle('rail-menu-open', open);
    }
    document.addEventListener('show.bs.dropdown', function (e) { setOpen(e, true); });
    document.addEventListener('hidden.bs.dropdown', function (e) { setOpen(e, false); });
})();
