// The inputs a WiiM has come from the box itself, so a rule can only name one
// it can be switched to. cache: player_id -> PENDING | [{id,label}] | {error}.

export const PENDING = Object.freeze({ pending: true });

/**
 * Start one read of `pid`'s inputs into `cache`, or null when one is already
 * cached or in flight. A failure is stored as {error} so the step shows a
 * reason and a retry rather than an empty select that is never re-read.
 */
export function readInputs(cache, pid, fetchFn = fetch) {
    if (!pid || cache[pid]) return null;
    cache[pid] = PENDING;
    return (async () => {
        try {
            const r = await fetchFn('/api/media/device?player_id=' + encodeURIComponent(pid));
            const j = await r.json().catch(() => ({}));
            const opts = j.panel && j.panel.input && j.panel.input.options;
            cache[pid] = Array.isArray(opts) && opts.length ? opts
                : { error: j.error || (!r.ok ? `HTTP ${r.status}`
                    : j.supported === false ? 'the speaker did not answer' : 'it reported no inputs') };
        } catch (e) { cache[pid] = { error: e.message || 'request failed' }; }
    })();
}

/** The input picker for a step whose saved input is `v`. */
export function inputSelectHtml(ins, v, sid, esc) {
    const sel = inner => `<select class="form-select form-select-sm s-mdevval" data-sid="${sid}" style="width:auto">${inner}</select>`;
    // The saved input stays selectable whatever the box reports now, so an
    // edit to another field can't silently swap it for the first option.
    const keep = v ? `<option value="${esc(v)}" selected>${esc(v)}</option>` : '';
    if (Array.isArray(ins) && ins.length) {
        const listed = ins.some(i => String(i.id) === String(v));
        return sel((listed ? '' : keep) + ins.map(i =>
            `<option value="${i.id}" ${String(v) === String(i.id) ? 'selected' : ''}>${i.label}</option>`).join(''));
    }
    if (ins && ins.error) {
        return sel(keep || '<option value="">—</option>')
            + `<span class="small text-warning">Inputs unavailable: ${esc(ins.error)}</span>`
            + `<button type="button" class="btn btn-sm btn-outline-secondary" onclick="window._aDevRetry(${sid})">Retry</button>`;
    }
    return sel(keep || '<option value="">Loading…</option>');
}
