/**
 * Suggested automations (Automations → Suggested sub-tab)
 * Location: static/js/swarm-suggestions.js
 *
 * The swarm matches every stigmergy pattern against the live network and
 * compiles each fill to a rule. Until now the only way to see one was to open
 * the rule builder for a specific device, which showed the handful that device
 * triggers — so the rest of the list existed but nobody could reach it.
 *
 * This page is the whole list: every suggestion, grouped by the room it was
 * matched in, with its tunable parameters inline and one button that builds it.
 *
 * Create posts to /api/swarm/suggestions/{id}/apply rather than sending a rule
 * back up. The server re-matches the pattern against the network as it is now
 * and compiles from that, so a suggestion that went stale while the page was
 * open is refused rather than built against devices that have since moved.
 */

import { showToast, withBusy } from './utils.js';

const log = zmmLog('swarm-suggestions');

// Dismissals live in the browser. The server has no per-user store for them and
// a suggestion id is a stable hash of pattern plus devices, so the same list
// still hides the same cards after a restart. "Show dismissed" makes it
// recoverable, which is what stops a per-browser list being a trap.
const DISMISSED_KEY = 'zmm.swarm.dismissed';

const CONFIDENCE_BADGE = {
    high:   'bg-success',
    medium: 'bg-secondary',
    low:    'bg-dark',
};

const CATEGORY_ICON = {
    lighting: 'fa-lightbulb', climate: 'fa-temperature-half',
    security: 'fa-shield-halved', safety: 'fa-triangle-exclamation',
    energy: 'fa-bolt', presence: 'fa-person-walking',
    maintenance: 'fa-screwdriver-wrench', convenience: 'fa-wand-magic-sparkles',
};

// House-scoped patterns match with no room. They are not "unassigned" — they
// are deliberately about the whole house — so they get their own heading and
// sort first rather than falling to the bottom with the leftovers.
const HOUSE_KEY = '__house__';
const HOUSE_LABEL = 'Whole house';

let suggestions = [];
let summary = null;
let dismissed = new Set();
let loaded = false;
const filters = { room: '', category: '', confidence: '', search: '',
                  showBuilt: false, showDismissed: false };

export function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function canEdit() {
    const a = window.zmmAuth;
    return !!(a && a.hasScope && (a.hasScope('admin') || a.hasScope('automation:write')));
}

function loadDismissed() {
    try {
        const raw = JSON.parse(localStorage.getItem(DISMISSED_KEY) || '[]');
        return new Set(Array.isArray(raw) ? raw : []);
    } catch { return new Set(); }
}

function saveDismissed() {
    try {
        localStorage.setItem(DISMISSED_KEY, JSON.stringify([...dismissed]));
    } catch (e) { log.warn('could not persist dismissals', e); }
}

// FILTERING AND GROUPING

/**
 * Which cards to render.
 *
 * Built suggestions are hidden by default: this is a to-do list, and a row you
 * cannot act on is noise. The toggle keeps them reachable, because seeing that
 * the swarm already knows about a rule you built by hand is the answer to
 * "why isn't it suggesting X".
 */
export function applyFilters(items, f, dismissedIds) {
    const search = (f.search || '').trim().toLowerCase();
    return (items || []).filter(s => {
        if (!f.showBuilt && s.status !== 'available') return false;
        if (!f.showDismissed && dismissedIds && dismissedIds.has(s.id)) return false;
        if (f.room && (s.room || HOUSE_KEY) !== f.room) return false;
        if (f.category && (s.category || '') !== f.category) return false;
        if (f.confidence && s.confidence !== f.confidence) return false;
        if (search) {
            const hay = [s.title, s.sentence, s.room_label,
                         ...(s.devices || []).map(d => d.name)]
                .join(' ').toLowerCase();
            if (!hay.includes(search)) return false;
        }
        return true;
    });
}

/** Group into room sections, house-scoped first, then rooms alphabetically. */
export function groupByRoom(items) {
    const groups = new Map();
    (items || []).forEach(s => {
        const key = s.room || HOUSE_KEY;
        if (!groups.has(key)) {
            groups.set(key, { key, label: s.room_label || HOUSE_LABEL, items: [] });
        }
        groups.get(key).items.push(s);
    });
    return [...groups.values()].sort((a, b) => {
        if (a.key === HOUSE_KEY) return -1;
        if (b.key === HOUSE_KEY) return 1;
        return a.label.localeCompare(b.label);
    });
}

// PARAMETERS

/**
 * Turn what the user typed into what the compiler takes.
 *
 * Clamping here rather than only validating means a number outside the
 * pattern's range is corrected in place; the server checks it again, so this is
 * a courtesy, not the guard.
 */
export function coerceParam(spec, raw) {
    if (!spec) return null;
    if (spec.type === 'colour') {
        const choice = (spec.choices || {})[raw];
        return choice || spec.value;
    }
    if (spec.type === 'time' || spec.type === 'monthday') {
        // Shaped strings — 22:30, 12-01. Anything else keeps the pattern's value;
        // the server checks the day is real.
        const shape = spec.type === 'time'
            ? /^([01]\d|2[0-3]):[0-5]\d$/
            : /^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$/;
        const text = String(raw == null ? '' : raw).trim();
        return shape.test(text) ? text : spec.value;
    }
    const n = spec.type === 'float' ? parseFloat(raw) : parseInt(raw, 10);
    if (!Number.isFinite(n)) return spec.value;
    const lo = (spec.min === null || spec.min === undefined) ? n : spec.min;
    const hi = (spec.max === null || spec.max === undefined) ? n : spec.max;
    return Math.min(Math.max(n, lo), hi);
}

/** The name a colour choice goes by, so a swatch reads as "amber", not "[40,100]". */
export function colourName(spec, value) {
    const entries = Object.entries(spec.choices || {});
    const hit = entries.find(([, v]) => JSON.stringify(v) === JSON.stringify(value));
    return hit ? hit[0] : (entries[0] ? entries[0][0] : '');
}

export function paramField(p) {
    const id = esc(p.id);
    if (p.type === 'colour') {
        const current = colourName(p, p.value);
        const opts = Object.keys(p.choices || {}).map(name =>
            `<option value="${esc(name)}"${name === current ? ' selected' : ''}>${esc(name)}</option>`
        ).join('');
        return `
        <div class="col-auto">
            <label class="form-label text-muted mb-0" style="font-size:.7rem">${esc(p.label)}</label>
            <select class="form-select form-select-sm" data-param="${id}"
                    aria-label="${esc(p.label)}">${opts}</select>
        </div>`;
    }
    if (p.type === 'time' || p.type === 'monthday') {
        const input = p.type === 'time'
            ? 'type="time"'
            : 'type="text" inputmode="numeric" pattern="\\d{2}-\\d{2}" placeholder="MM-DD" maxlength="5"';
        return `
        <div class="col-auto">
            <label class="form-label text-muted mb-0" style="font-size:.7rem">${esc(p.label)}</label>
            <input ${input} class="form-control form-control-sm" data-param="${id}"
                   value="${esc(p.value)}" aria-label="${esc(p.label)}" style="max-width:7rem">
        </div>`;
    }
    const step = p.type === 'float' ? '0.5' : '1';
    return `
    <div class="col-auto">
        <label class="form-label text-muted mb-0" style="font-size:.7rem">${esc(p.label)}</label>
        <div class="input-group input-group-sm">
            <input type="number" class="form-control" data-param="${id}"
                   value="${esc(p.value)}" step="${step}"
                   ${p.min !== undefined && p.min !== null ? `min="${esc(p.min)}"` : ''}
                   ${p.max !== undefined && p.max !== null ? `max="${esc(p.max)}"` : ''}
                   aria-label="${esc(p.label)}" style="max-width:6rem">
            ${p.unit ? `<span class="input-group-text">${esc(p.unit)}</span>` : ''}
        </div>
    </div>`;
}

/** Read every parameter field on a card back into the shape apply expects. */
function readParams(card, specs) {
    const out = {};
    (specs || []).forEach(p => {
        const el = card.querySelector(`[data-param="${CSS.escape(p.id)}"]`);
        if (el) out[p.id] = coerceParam(p, el.value);
    });
    return out;
}

/** Devices unticked on a card's checklist — what apply leaves out. */
export function readExcluded(card) {
    return [...card.querySelectorAll('[data-sg-member]')]
        .filter(el => !el.checked).map(el => el.dataset.sgMember);
}

// RENDER

export function cardHtml(s, opts) {
    const editable = opts && opts.editable;
    const isDismissed = opts && opts.dismissed;
    const icon = CATEGORY_ICON[s.category] || 'fa-diagram-project';
    const badge = CONFIDENCE_BADGE[s.confidence] || 'bg-secondary';
    const built = s.status !== 'available';

    // A collected action ("every light") is a checklist the user may untick;
    // everything else is fixed by the pattern and shown as a badge.
    const choosable = new Set(!built && editable ? (s.choosable || []) : []);
    const devices = (s.devices || []).map(d => choosable.has(d.slot)
        ? `<label class="badge bg-light text-dark border fw-normal" title="${esc(d.label || d.offer)}">
               <input type="checkbox" class="form-check-input me-1 align-middle" checked
                      data-sg-member="${esc(d.ieee)}">${esc(d.name)}</label>`
        : `<span class="badge bg-light text-dark border fw-normal${d.proposed ? ' border-info' : ''}"
               title="${esc(d.label || d.offer)}">${d.proposed ? '<i class="fas fa-plus me-1"></i>' : ''}
            ${esc(d.name)}</span>`).join(' ');

    // A worker the rule needs and the house does not have is created with it,
    // and saying so first is the difference between a suggestion and a surprise.
    const creates = (s.creates_workers || []).length && !built
        ? `<div class="small mt-2 text-info-emphasis"><i class="fas fa-user-gear me-1"></i>
               Also creates ${s.creates_workers.map(w =>
                   `<b>${esc(w.name)}</b> (${esc(w.type)}${(w.options || []).length
                       ? `: ${esc(w.options.join(', '))}` : ''})`).join(', ')} on the Workers tab</div>`
        : '';

    const params = (s.params || []).length && !built
        ? `<div class="row g-2 align-items-end mt-1">${s.params.map(paramField).join('')}</div>`
        : '';

    const actions = built
        ? `<span class="badge bg-success-subtle text-success-emphasis border border-success-subtle">
               <i class="fas fa-check me-1"></i>${s.status === 'disabled' ? 'Built, disabled' : 'Already built'}</span>
           ${s.rule_name ? `<span class="text-muted small ms-2">${esc(s.rule_name)}</span>` : ''}`
        : editable
            ? `<button class="btn btn-sm btn-primary" data-sg-action="create" data-id="${esc(s.id)}">
                   <i class="fas fa-plus"></i> Create</button>
               <button class="btn btn-sm btn-outline-secondary" data-sg-action="${isDismissed ? 'restore' : 'dismiss'}"
                       data-id="${esc(s.id)}">
                   <i class="fas fa-${isDismissed ? 'rotate-left' : 'eye-slash'}"></i>
                   ${isDismissed ? 'Restore' : 'Dismiss'}</button>`
            : `<span class="text-muted small">Read-only — you cannot create rules</span>`;

    return `
    <div class="col-12 col-xl-6">
      <div class="card h-100 shadow-sm ${isDismissed ? 'opacity-50' : ''}" data-sg-card="${esc(s.id)}">
        <div class="card-body d-flex flex-column">
          <div class="d-flex justify-content-between align-items-start gap-2">
            <div class="fw-semibold">
                <i class="fas ${icon} me-2 text-primary"></i>${esc(s.title)}</div>
            <span class="badge ${badge} text-nowrap" title="How much of the pattern filled">
                ${esc(s.confidence)}</span>
          </div>
          <div class="small text-muted mt-1">${esc(s.sentence)}</div>
          <div class="mt-2 d-flex flex-wrap gap-1">${devices}</div>
          ${creates}
          ${params}
          <div class="mt-auto pt-3 d-flex flex-wrap align-items-center gap-2">${actions}</div>
        </div>
      </div>
    </div>`;
}

function optionList(values, selected, blank) {
    return [`<option value="">${esc(blank)}</option>`].concat(
        values.map(([value, label]) =>
            `<option value="${esc(value)}"${value === selected ? ' selected' : ''}>${esc(label)}</option>`)
    ).join('');
}

function filterBar() {
    const rooms = new Map();
    const categories = new Set();
    suggestions.forEach(s => {
        rooms.set(s.room || HOUSE_KEY, s.room_label || HOUSE_LABEL);
        if (s.category) categories.add(s.category);
    });
    return `
    <div class="row g-2 align-items-center mb-3">
        <div class="col-12 col-md">
            <input type="search" class="form-control form-control-sm" id="sg-search"
                   placeholder="Search suggestions, rooms or devices"
                   value="${esc(filters.search)}" aria-label="Search suggestions">
        </div>
        <div class="col-auto">
            <select class="form-select form-select-sm" id="sg-room" aria-label="Filter by room">
                ${optionList([...rooms.entries()].sort((a, b) => a[1].localeCompare(b[1])),
                             filters.room, 'All rooms')}
            </select>
        </div>
        <div class="col-auto">
            <select class="form-select form-select-sm" id="sg-category" aria-label="Filter by category">
                ${optionList([...categories].sort().map(c => [c, c]), filters.category, 'All kinds')}
            </select>
        </div>
        <div class="col-auto">
            <select class="form-select form-select-sm" id="sg-confidence" aria-label="Filter by confidence">
                ${optionList([['high', 'High'], ['medium', 'Medium'], ['low', 'Low']],
                             filters.confidence, 'Any confidence')}
            </select>
        </div>
        <div class="col-auto d-flex gap-3">
            <div class="form-check form-switch mb-0">
                <input class="form-check-input" type="checkbox" id="sg-show-built"
                       ${filters.showBuilt ? 'checked' : ''}>
                <label class="form-check-label small text-muted" for="sg-show-built">Built</label>
            </div>
            <div class="form-check form-switch mb-0">
                <input class="form-check-input" type="checkbox" id="sg-show-dismissed"
                       ${filters.showDismissed ? 'checked' : ''}>
                <label class="form-check-label small text-muted" for="sg-show-dismissed">Dismissed</label>
            </div>
        </div>
    </div>`;
}

/** What the swarm found, and what it could not — the second half is the hint. */
export function summaryLine(sum, shown, hiddenDismissed) {
    if (!sum) return '';
    const bits = [`${sum.available} to build`];
    if (sum.active) bits.push(`${sum.active} already built`);
    if (hiddenDismissed) bits.push(`${hiddenDismissed} dismissed`);
    if (sum.patterns_unmatched) {
        bits.push(`${sum.patterns_unmatched} of ${sum.patterns} patterns matched nothing`);
    }
    return `Showing ${shown}. ${bits.join(' · ')}.`;
}

function emptyState() {
    // The empty state has to distinguish "nothing matched" from "you filtered
    // everything out", because the fix for each is the opposite of the other.
    const anything = suggestions.length > 0;
    return `
    <div class="card border-0 shadow-sm">
        <div class="card-body">
            <h6 class="mb-1">${anything ? 'Nothing matches those filters' : 'No suggestions yet'}</h6>
            <p class="text-muted small mb-0">
                ${anything
                    ? 'Clear a filter, or switch on Built to see the ones you have already made.'
                    : `Suggestions are built by matching patterns against the devices in each
                       room, so most of them need devices assigned to a room first. Assign a
                       few in Chambers and they will appear here.`}
            </p>
        </div>
    </div>`;
}

function render() {
    const host = document.getElementById('suggestions-content');
    if (!host) return;

    const editable = canEdit();
    const visible = applyFilters(suggestions, filters, dismissed);
    const hiddenDismissed = filters.showDismissed
        ? 0
        : suggestions.filter(s => dismissed.has(s.id) && s.status === 'available').length;

    const groups = groupByRoom(visible).map(g => `
        <div class="mb-4">
            <div class="d-flex align-items-baseline gap-2 mb-2">
                <h6 class="mb-0">${esc(g.label)}</h6>
                <span class="text-muted small">${g.items.length}</span>
            </div>
            <div class="row g-3">
                ${g.items.map(s => cardHtml(s, { editable, dismissed: dismissed.has(s.id) })).join('')}
            </div>
        </div>`).join('');

    syncSuggestionsBadge(
        suggestions.filter(s => s.status === 'available' && !dismissed.has(s.id)).length);

    host.innerHTML = `
        <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
            <div>
                <h5 class="mb-0"><i class="fas fa-diagram-project text-primary"></i> Suggested</h5>
                <div class="text-muted small" id="sg-summary">
                    ${esc(summaryLine(summary, visible.length, hiddenDismissed))}</div>
            </div>
            <div class="ms-auto">
                <button class="btn btn-outline-secondary btn-sm" id="sg-refresh">
                    <i class="fas fa-rotate"></i> Refresh</button>
            </div>
        </div>
        ${filterBar()}
        ${visible.length ? groups : emptyState()}`;

    bind(host);
}

function bind(host) {
    const on = (id, event, fn) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener(event, fn);
    };
    on('sg-refresh', 'click', () => loadSuggestionsPage(true));
    on('sg-room', 'change', e => { filters.room = e.target.value; render(); });
    on('sg-category', 'change', e => { filters.category = e.target.value; render(); });
    on('sg-confidence', 'change', e => { filters.confidence = e.target.value; render(); });
    on('sg-show-built', 'change', e => { filters.showBuilt = e.target.checked; render(); });
    on('sg-show-dismissed', 'change', e => { filters.showDismissed = e.target.checked; render(); });

    // Search re-renders on input, which rebuilds the box — so focus and caret
    // are restored rather than lost after every keystroke.
    const search = document.getElementById('sg-search');
    if (search) {
        search.addEventListener('input', e => {
            filters.search = e.target.value;
            render();
            const again = document.getElementById('sg-search');
            if (again) { again.focus(); again.setSelectionRange(again.value.length, again.value.length); }
        });
    }

    host.querySelectorAll('[data-sg-action]').forEach(btn => {
        btn.addEventListener('click', () => {
            const id = btn.dataset.id;
            const action = btn.dataset.sgAction;
            if (action === 'create') return create(id, btn);
            if (action === 'dismiss') { dismissed.add(id); saveDismissed(); render(); return; }
            if (action === 'restore') { dismissed.delete(id); saveDismissed(); render(); }
        });
    });
}

// ACTIONS

async function create(id, btn) {
    const suggestion = suggestions.find(s => s.id === id);
    if (!suggestion) return;
    const card = document.querySelector(`[data-sg-card="${CSS.escape(id)}"]`);
    const params = card ? readParams(card, suggestion.params) : {};
    const exclude = card ? readExcluded(card) : [];

    await withBusy(btn, async () => {
        try {
            const res = await fetch(`/api/swarm/suggestions/${encodeURIComponent(id)}/apply`, {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ params, exclude }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Could not build that suggestion');
            const made = (data.workers_created || []).length
                ? ` and ${data.workers_created.length} worker${data.workers_created.length > 1 ? 's' : ''}`
                : '';
            showToast(`Created "${data.rule?.name || suggestion.title}"${made}`, 'success');
            // The new rule changes what else is still available — a pattern that
            // overlaps it is now built too — so the whole list is re-read rather
            // than the one card patched.
            await loadSuggestionsPage(true);
        } catch (e) {
            showToast(e.message, 'danger');
        }
    });
}

// LOAD

export async function loadSuggestionsPage(force) {
    const host = document.getElementById('suggestions-content');
    if (!host) return;
    if (!loaded || force) {
        host.innerHTML = `<div class="text-center text-muted py-4">
            <i class="fas fa-spinner fa-spin"></i> Working out what your devices could do...</div>`;
    }
    dismissed = loadDismissed();
    try {
        const res = await fetch('/api/swarm/suggestions', { credentials: 'same-origin' });
        if (!res.ok) throw new Error(`Swarm unavailable (${res.status})`);
        const data = await res.json();
        suggestions = data.suggestions || [];
        summary = data.summary || null;
        loaded = true;
        render();
    } catch (e) {
        log.error('Failed to load suggestions', e);
        host.innerHTML = `<div class="alert alert-warning">
            Could not load suggestions: ${esc(e.message)}</div>`;
    }
}

export function initSuggestionsPage() {
    const tab = document.querySelector('button[data-bs-target="#automationSuggestions"]');
    if (tab) tab.addEventListener('shown.bs.tab', () => loadSuggestionsPage());

    // Coming back to Automations reloads whichever sub-tab is open, the same way
    // Workers does — a suggestion list that predates the rules you just built
    // would offer to build them again.
    const outer = document.querySelector('button[data-bs-target="#automations"]');
    if (outer) {
        outer.addEventListener('shown.bs.tab', () => {
            if (document.querySelector('#automationSuggestions.active')) loadSuggestionsPage(true);
        });
    }
}

/**
 * The count on the sub-tab itself.
 *
 * Exported because the rules page already fetches /api/swarm/coverage for its
 * strip, so it can set the badge without this page having been opened once.
 */
export function syncSuggestionsBadge(count) {
    const el = document.getElementById('suggestions-count');
    if (!el) return;
    const n = Number(count) || 0;
    el.textContent = n > 99 ? '99+' : String(n);
    el.classList.toggle('d-none', n === 0);
}


/** Open the sub-tab from elsewhere — the coverage strip's "N suggested" badge. */
export function showSuggestionsTab() {
    const btn = document.querySelector('button[data-bs-target="#automationSuggestions"]');
    if (!btn) return;
    if (window.bootstrap && window.bootstrap.Tab) {
        window.bootstrap.Tab.getOrCreateInstance(btn).show();
    } else {
        btn.click();
    }
}
