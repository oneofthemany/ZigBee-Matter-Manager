/**
 * Swarm Intelligence — the suggestion step of the rule builder.
 *
 * Sits between choosing a device and filling in the form. Rather than starting
 * from an empty trigger row, the user is shown what this device could actually
 * do — in plain English, drawn from the swarm's capability vocabulary — and
 * picking one lands them in the existing builder with the form already filled.
 *
 * Nothing here creates a rule. Every path ends at window._aShowFormWith(), so
 * the builder stays the single place a rule is authored, and every advanced
 * step (delay, gate, if/then/else, parallel, media) is still one click away
 * once the simple choice has been made.
 */

import { showToast } from './utils.js';

const log = (typeof zmmLog === 'function') ? zmmLog('swarm-suggest') : console;

// Pairings are a full cross-product, so the list is capped and floored. The
// point of this step is the handful worth doing, not completeness — the raw
// builder remains available for anything below the line.
const PAIRING_LIMIT = 12;
const PAIRING_FLOOR = 'medium';

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

let cache = { ieee: null, suggestions: [], pairings: [] };


/**
 * Keep only the suggestions this device actually triggers.
 *
 * The builder saves with `source_ieee: currentSourceIeee` and fetches its
 * attribute list for that device, so a suggestion sourced on a *different*
 * device would be saved against the wrong source and rendered against the wrong
 * attributes. The API's `device=` filter is broader on purpose — it answers
 * "what does this device take part in" — so the narrowing happens here, where
 * the constraint lives.
 *
 * Exported for the tests: this is the guard that keeps a wrong rule from being
 * one click away.
 */
export function sourcedBy(suggestions, ieee) {
    return (suggestions || []).filter(s => s && s.rule && s.rule.source_ieee === ieee);
}

const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));


/**
 * Fetch what this device could do. Both calls are best-effort: the swarm is an
 * enhancement to the builder, so if it is unavailable the user still gets the
 * blank form rather than a dead end.
 */
async function _load(ieee) {
    if (cache.ieee === ieee) return cache;

    const [sugg, pair] = await Promise.allSettled([
        fetch(`/api/swarm/suggestions?device=${encodeURIComponent(ieee)}`),
        fetch(`/api/swarm/pairings/${encodeURIComponent(ieee)}`
              + `?min_confidence=${PAIRING_FLOOR}&limit=${PAIRING_LIMIT}`),
    ]);

    let suggestions = [], pairings = [];
    if (sugg.status === 'fulfilled' && sugg.value.ok) {
        const body = await sugg.value.json();
        // Anything already built is dropped rather than shown greyed out: this
        // is a creation step, and an option that cannot be picked is clutter.
        suggestions = sourcedBy(
            (body.suggestions || []).filter(s => s.status === 'available'), ieee);
    }
    if (pair.status === 'fulfilled' && pair.value.ok) {
        const body = await pair.value.json();
        // Outbound only — an inbound pairing is triggered by another device, and
        // this builder authors rules sourced on the current one.
        pairings = body.outbound || [];
    }

    cache = { ieee, suggestions, pairings };
    return cache;
}


/** A stigmergy suggestion: a whole shape, already parameterised. */
function _suggestionCard(s, i) {
    const icon = CATEGORY_ICON[s.category] || 'fa-diagram-project';
    const badge = CONFIDENCE_BADGE[s.confidence] || 'bg-secondary';
    const where = s.room_label ? `<span class="badge bg-light text-dark border">${esc(s.room_label)}</span>` : '';
    const params = (s.params || []).map(p =>
        `<span class="badge bg-light text-muted border fw-normal">${esc(p.label)} ${esc(p.value)}${esc(p.unit || '')}</span>`
    ).join(' ');

    return `
    <button type="button" class="list-group-item list-group-item-action swarm-option"
            onclick="window._swarmPick('suggestion', ${i})">
        <div class="d-flex justify-content-between align-items-start gap-2">
            <div class="flex-grow-1">
                <div class="fw-semibold"><i class="fas ${icon} me-2 text-primary"></i>${esc(s.title)}</div>
                <div class="small text-muted mt-1">${esc(s.sentence)}</div>
                <div class="mt-2 d-flex flex-wrap gap-1">${where}${params}</div>
            </div>
            <span class="badge ${badge} text-nowrap">${esc(s.confidence)}</span>
        </div>
    </button>`;
}


/** A raw pairing: one trigger wired to one action, for the swarm to fill in. */
function _pairingRow(p, i) {
    return `
    <button type="button" class="list-group-item list-group-item-action swarm-option py-2"
            onclick="window._swarmPick('pairing', ${i})">
        <div class="d-flex justify-content-between align-items-center gap-2">
            <span class="small">${esc(p.sentence)}</span>
            <span class="badge ${CONFIDENCE_BADGE[p.confidence] || 'bg-secondary'} text-nowrap">
                ${esc(p.target_name)}</span>
        </div>
    </button>`;
}


/**
 * Render the chooser into the form card.
 *
 * `onBlank` is called when the user opts out, so the caller decides what an
 * empty form means — the device modal and the global page open it differently.
 */
export async function renderChooser(ieee, container, onBlank) {
    container.innerHTML = `
        <div class="card-header bg-light d-flex justify-content-between align-items-center">
            <strong><i class="fas fa-diagram-project"></i> New Automation</strong>
            <button class="btn btn-sm btn-outline-secondary" onclick="window._aHideForm()">
                <i class="fas fa-times"></i></button>
        </div>
        <div class="card-body">
            <div class="text-center text-muted py-4">
                <i class="fas fa-spinner fa-spin"></i> Working out what this device can do...
            </div>
        </div>`;
    container.style.display = 'block';

    let data;
    try {
        data = await _load(ieee);
    } catch (e) {
        log.warn?.('swarm unavailable', e);
        onBlank();
        return;
    }

    const { suggestions, pairings } = data;

    // Nothing to offer is a legitimate outcome — an unrecognised device, or one
    // whose every suggestion is already built. Skip straight to the form rather
    // than showing an empty step.
    if (!suggestions.length && !pairings.length) {
        onBlank();
        return;
    }

    const suggestionsHtml = suggestions.length ? `
        <div class="mb-3">
            <div class="small text-muted mb-2">
                <i class="fas fa-star text-warning me-1"></i>
                Ready-made — pick one and refine it
            </div>
            <div class="list-group list-group-flush border rounded">
                ${suggestions.map(_suggestionCard).join('')}
            </div>
        </div>` : '';

    const pairingsHtml = pairings.length ? `
        <div class="mb-3">
            <div class="small text-muted mb-2 d-flex justify-content-between align-items-center">
                <span><i class="fas fa-link me-1"></i> Or wire it to something</span>
                <span class="badge bg-light text-muted border fw-normal">${pairings.length} shown</span>
            </div>
            <div class="list-group list-group-flush border rounded"
                 style="max-height:260px;overflow-y:auto">
                ${pairings.map(_pairingRow).join('')}
            </div>
        </div>` : '';

    container.innerHTML = `
        <div class="card-header bg-light d-flex justify-content-between align-items-center">
            <strong><i class="fas fa-diagram-project"></i> New Automation</strong>
            <button class="btn btn-sm btn-outline-secondary" onclick="window._aHideForm()">
                <i class="fas fa-times"></i></button>
        </div>
        <div class="card-body">
            ${suggestionsHtml}
            ${pairingsHtml}
            <div class="d-flex justify-content-between align-items-center border-top pt-3">
                <span class="small text-muted">
                    Every option opens the full builder, already filled in.
                </span>
                <button class="btn btn-sm btn-outline-secondary" onclick="window._swarmBlank()">
                    <i class="fas fa-pen"></i> Start from scratch
                </button>
            </div>
        </div>`;

    window._swarmBlank = onBlank;
}


/**
 * Turn one pairing into a rule skeleton.
 *
 * A pairing is a trigger and an action with nothing between them, which is
 * exactly the shape the builder expects — so it is assembled here rather than
 * round-tripping through the server for a rule the user has not committed to.
 */
function _ruleFromPairing(p) {
    return {
        name: '',
        source_ieee: p.source_ieee,
        conditions: [p.trigger.condition],
        condition_logic: 'and',
        prerequisites: [],
        then_sequence: [p.action.step],
        else_sequence: [],
        cooldown: 5,
    };
}


/** Hand the chosen shape to the builder. */
window._swarmPick = (kind, index) => {
    const item = kind === 'suggestion' ? cache.suggestions[index] : cache.pairings[index];
    if (!item) return;

    const rule = kind === 'suggestion'
        ? { ...item.rule, name: item.rule.name || item.title }
        : _ruleFromPairing(item);

    if (typeof window._aShowFormWith !== 'function') {
        showToast('The rule builder is not loaded', 'danger');
        return;
    }
    window._aShowFormWith(rule);
    document.getElementById('a-form')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
};


/** Drop the cache so the next open re-reads a network that may have changed. */
export function invalidateChooser() {
    cache = { ieee: null, suggestions: [], pairings: [] };
}
