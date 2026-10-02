/* Main tab rail, md and up: tabs that don't fit on one row move into the
   More dropdown. The original buttons stay in place (hidden) because other
   modules find them by [data-bs-target]; menu items just click them.
   Below md the rail stays the icon-only scroller from mobile.css. */
(function () {
    'use strict';

    var rail = document.getElementById('mainTabs');
    var more = document.getElementById('mainTabsMore');
    var tools = document.getElementById('mainTabsTools');
    if (!rail || !more) return;

    var toggle = more.querySelector('.dropdown-toggle');
    var label = more.querySelector('.tab-more-label');
    var menu = more.querySelector('.dropdown-menu');
    var mq = window.matchMedia('(min-width: 768px)');
    var items = Array.prototype.filter.call(rail.children, function (li) {
        return li !== more && li !== tools && li.querySelector('[data-bs-toggle="tab"]');
    });

    function linkOf(li) { return li.querySelector('[data-bs-toggle="tab"]'); }

    function textOf(link) {
        var el = link.querySelector('.tab-label');
        return (el || link).textContent.trim();
    }

    function fits() { return rail.scrollWidth <= rail.clientWidth + 1; }

    function menuItem(link) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'dropdown-item' + (link.classList.contains('active') ? ' active' : '');
        var icon = link.querySelector('i');
        if (icon) {
            icon = icon.cloneNode();
            icon.classList.remove('me-1');
            icon.classList.add('fa-fw', 'me-2');
            btn.appendChild(icon);
        }
        btn.appendChild(document.createTextNode(textOf(link)));
        btn.addEventListener('click', function () { link.click(); });
        var li = document.createElement('li');
        li.appendChild(btn);
        return li;
    }

    function layout() {
        items.forEach(function (li) { li.classList.remove('tab-overflowed'); });
        more.classList.add('d-none');
        toggle.classList.remove('active');
        label.textContent = 'More';
        menu.replaceChildren();
        rail.classList.toggle('tabs-priority', mq.matches);
        if (!mq.matches || fits()) return;

        more.classList.remove('d-none');
        var hidden = [];
        var visible = items.filter(function (li) {
            return getComputedStyle(linkOf(li)).display !== 'none';
        });
        // Trim from the end; an overflowed active tab names itself on the toggle
        for (var i = visible.length - 1; i >= 0 && !fits(); i--) {
            var link = linkOf(visible[i]);
            visible[i].classList.add('tab-overflowed');
            hidden.unshift(link);
            if (link.classList.contains('active')) {
                toggle.classList.add('active');
                label.textContent = textOf(link);
            }
        }
        hidden.forEach(function (link) { menu.appendChild(menuItem(link)); });
    }

    var queued = false;
    function schedule() {
        if (queued) return;
        queued = true;
        requestAnimationFrame(function () { queued = false; layout(); });
    }

    rail.addEventListener('shown.bs.tab', schedule);
    // Bootstrap 5.3.0's Tab adds .show to the menu of a dropdown holding the
    // tab it activates, after this event, so close Tools on the next tick.
    if (tools) {
        // The phone rail's scroll-fade mask clips descendants, fixed menus included.
        tools.addEventListener('show.bs.dropdown', function () { rail.classList.add('tools-open'); });
        tools.addEventListener('hidden.bs.dropdown', function () { rail.classList.remove('tools-open'); });
        rail.addEventListener('show.bs.tab', function (e) {
            if (!tools.contains(e.target)) return;
            setTimeout(function () {
                bootstrap.Dropdown.getOrCreateInstance(tools.querySelector('.dropdown-toggle')).hide();
            });
        });
    }
    if (window.ResizeObserver) new ResizeObserver(schedule).observe(rail);
    else window.addEventListener('resize', schedule);
    if (mq.addEventListener) mq.addEventListener('change', schedule);
    // Tab widths depend on the mono webfont
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(schedule);
    layout();
})();
