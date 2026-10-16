/**
 * tab-routing.js
 * The URL hash names the innermost open page tab (#heating, #settingsSpectrum),
 * so Back and bookmarks land where the user was. A reload, or coming back
 * after IDLE_RETURN_MS away, lands on the device page instead. Hashes that
 * aren't a page tab (e.g. #messages) are left to their owners.
 */

export const IDLE_RETURN_MS = 5 * 60 * 1000;

/** @param {{onReturn?: () => void}} opts onReturn runs on a return from away. */
export function initTabRouting({ onReturn } = {}) {
    const triggerFor = id => document.querySelector(
        `[data-bs-toggle="tab"][data-bs-target="#${CSS.escape(id)}"]:not(.modal *)`);
    const hashTab = () => {
        const id = decodeURIComponent(location.hash.slice(1)) || 'devices';
        return document.getElementById(id)?.classList.contains('tab-pane') && triggerFor(id) ? id : null;
    };
    let applying = false;

    function applyHash() {
        const id = hashTab();
        if (!id) return;
        const chain = [];
        for (let p = document.getElementById(id); p; p = p.parentElement.closest('.tab-pane')) chain.unshift(p.id);
        applying = true;
        // click(), not Tab.show(): some tabs (Settings) load on click.
        try { chain.forEach(pid => triggerFor(pid)?.click()); } finally { applying = false; }
    }

    document.addEventListener('shown.bs.tab', e => {
        const id = e.target.dataset.bsTarget?.slice(1);
        if (applying || !id || e.target.closest('.modal') || location.hash === `#${id}`) return;
        history.pushState(null, '', `#${id}`);
    });
    window.addEventListener('popstate', applyHash);

    if (performance.getEntriesByType('navigation')[0]?.type === 'reload' && location.hash && hashTab()) {
        history.replaceState(null, '', location.pathname + location.search);
    }
    applyHash();

    // Pushed, not replaced, so Back returns to where they left off.
    function returnToDevices() {
        document.querySelectorAll('.modal.show').forEach(m => bootstrap.Modal.getInstance(m)?.hide());
        if (hashTab() !== 'devices') history.pushState(null, '', '#devices');
        applyHash();
        onReturn?.();
    }

    // Away = tab hidden, or window unfocused; focus inside one of our iframes
    // (camera player, editor) blurs the window too but is not away.
    let awaySince = null;
    const leave = () => { awaySince ??= Date.now(); };
    const back = () => {
        if (awaySince !== null && Date.now() - awaySince >= IDLE_RETURN_MS) returnToDevices();
        awaySince = null;
    };
    document.addEventListener('visibilitychange', () => (document.hidden ? leave() : back()));
    window.addEventListener('blur', () => setTimeout(() => {
        if (!(document.activeElement instanceof HTMLIFrameElement)) leave();
    }));
    window.addEventListener('focus', back);
}
