// A tab's module is imported and started on the tab's first open, not with the
// page (docs/structure.md §Lazy tabs). Each module still attaches its own
// shown.bs.tab listeners in its init; that init runs during the event that
// opened the tab, so the event is replayed once for the listeners it added.

const tabs = new Map();   // pane id -> { load, started }
const REPLAY = Symbol('zmmTabReplay');

/**
 * Load `load()` the first time pane `paneId` is shown. `globals` are window
 * functions the page's own markup calls: each gets a stand-in that loads the
 * tab and then calls the real one, which `load` must assign.
 */
export function lazyTab(paneId, load, { globals = [] } = {}) {
    tabs.set(paneId, { load, started: null });
    for (const name of globals) stubGlobal(name, paneId);
}

export function isLazyTab(paneId) { return tabs.has(paneId); }

/** Load a tab's module now (idempotent); resolves once its init has run. */
export function startTab(paneId) {
    const t = tabs.get(paneId);
    if (!t) return Promise.resolve();
    if (!t.started) {
        const pane = document.getElementById(paneId);
        const spinner = pane && showSpinner(pane);
        t.started = Promise.resolve().then(t.load);
        t.started.then(() => spinner?.remove(), err => {
            // Forgotten so the next open, or Retry, tries again.
            t.started = null;
            console.error(`[tab-loader] ${paneId}:`, err);
            if (spinner) showFailure(spinner, paneId);
        });
    }
    return t.started;
}

function stubGlobal(name, paneId) {
    const stub = function (...args) {
        return startTab(paneId).then(() => {
            const real = window[name];
            if (real === stub || typeof real !== 'function') {
                throw new Error(`${name} was not defined by the ${paneId} tab`);
            }
            return real.apply(this, args);
        });
    };
    stub.zmmTabStub = paneId;
    window[name] = stub;
}

function showSpinner(pane) {
    const el = document.createElement('div');
    el.className = 'zmm-tab-loading text-center text-muted py-4';
    el.setAttribute('role', 'status');
    el.innerHTML = '<span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>Loading…';
    pane.prepend(el);
    return el;
}

function showFailure(el, paneId) {
    el.removeAttribute('role');
    el.innerHTML = '<span class="text-danger me-2">This tab failed to load.</span>'
        + '<button type="button" class="btn btn-sm btn-outline-secondary">Retry</button>';
    el.querySelector('button').addEventListener('click', () => {
        el.remove();
        startTab(paneId).then(() => replayActive(paneId));
    });
}

// Every active tab button of the loaded pane: the one that opened it, and any
// sub-tab inside it that a deep link activated while the module was loading.
function replayActive(paneId) {
    const pane = document.getElementById(paneId);
    const buttons = [...document.querySelectorAll(`[data-bs-target="#${CSS.escape(paneId)}"].active`)];
    if (pane) buttons.push(...pane.querySelectorAll('[data-bs-toggle="tab"].active, [data-bs-toggle="pill"].active'));
    for (const btn of buttons) {
        const ev = new Event('shown.bs.tab', { bubbles: true });
        ev[REPLAY] = true;
        btn.dispatchEvent(ev);
    }
}

document.addEventListener('shown.bs.tab', e => {
    if (e[REPLAY]) return;
    const target = e.target.getAttribute?.('data-bs-target');
    if (!target || !target.startsWith('#')) return;
    const paneId = target.slice(1);
    const t = tabs.get(paneId);
    if (!t || t.started) return;
    startTab(paneId).then(() => replayActive(paneId), () => {});
});
