/**
 * Identity tab: what ZMM decided about each endpoint, from which source and
 * why, with confirm / correct / reset (docs/plans/zmm-quirks.md §8).
 * Event delegation only: the strict CSP target forbids inline handlers.
 */
const log = zmmLog('modal-identity');

const SOURCE_BADGE = {
    user: ['bg-primary', 'You'],
    zmm: ['bg-success', 'ZMM entry'],
    profile: ['bg-info text-dark', 'Profile'],
    rule: ['bg-secondary', 'Rules'],
    answered: ['bg-secondary', 'Device'],
    default: ['bg-warning text-dark', 'Default'],
};

const CHOICES = {
    kind: ['switch', 'light'],
    metering: ['self', 'device_total', 'none'],
};

function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function renderIdentityTab(device) {
    if (!device?.ieee) return '';
    return `<div data-identity-ieee="${escapeHtml(device.ieee)}">
        <div class="text-muted small py-2">Loading…</div>
    </div>`;
}

function decisionRow(ep, d) {
    const [cls, label] = SOURCE_BADGE[d.source] || ['bg-light text-dark', d.source || '?'];
    const change = d.previous_value && d.changed_at
        ? `<div class="small text-muted">was ${escapeHtml(d.previous_value)} · ${escapeHtml(new Date(d.changed_at).toLocaleString())}</div>`
        : '';
    const choices = (CHOICES[d.subject] || []).filter(v => v !== d.value).map(v =>
        `<button class="btn btn-outline-secondary btn-sm me-1" data-identity-set="${escapeHtml(v)}"
                 data-ep="${ep}" data-subject="${escapeHtml(d.subject)}">Make ${escapeHtml(v.replace('_', ' '))}</button>`).join('');
    const confirm = d.user_set || !d.value ? '' :
        `<button class="btn btn-outline-success btn-sm me-1" data-identity-set="${escapeHtml(d.value)}"
                 data-ep="${ep}" data-subject="${escapeHtml(d.subject)}">Confirm</button>`;
    const reset = d.user_set
        ? `<button class="btn btn-outline-danger btn-sm" data-identity-reset data-ep="${ep}"
                   data-subject="${escapeHtml(d.subject)}">Reset</button>` : '';
    return `<tr>
        <td class="fw-bold small text-capitalize">${escapeHtml(d.subject)}</td>
        <td><span class="badge bg-dark">${escapeHtml(d.value ?? '—')}</span></td>
        <td><span class="badge ${cls}">${escapeHtml(label)}</span></td>
        <td class="small">${escapeHtml(d.reason || '')}${change}</td>
        <td class="text-end text-nowrap">${confirm}${choices}${reset}</td>
    </tr>`;
}

function endpointCard(ep) {
    const rows = ep.decisions.map(d => decisionRow(ep.id, d)).join('')
        || '<tr><td colspan="5" class="small text-muted">Nothing decided for this endpoint.</td></tr>';
    const resetLabel = ep.label_user_set
        ? `<button class="btn btn-outline-danger btn-sm" data-identity-reset data-ep="${ep.id}" data-subject="label">Reset</button>` : '';
    return `<div class="card mb-3">
        <div class="card-header py-1 d-flex align-items-center gap-2 flex-wrap">
            <span class="fw-bold">EP${ep.id}</span>
            <input class="form-control form-control-sm w-auto" maxlength="40" placeholder="Name this endpoint"
                   data-identity-label-for="${ep.id}" value="${escapeHtml(ep.label || '')}">
            <button class="btn btn-outline-primary btn-sm" data-identity-label="${ep.id}">Save name</button>
            ${resetLabel}
            <span class="ms-auto small text-muted">type ${escapeHtml(ep.device_type)} · ${escapeHtml(ep.in_clusters.join(' '))}</span>
        </div>
        <div class="card-body p-0">
            <table class="table table-sm mb-0 align-middle"><tbody>${rows}</tbody></table>
        </div>
    </div>`;
}

function render(root, data) {
    if (!data.success) {
        root.innerHTML = `<div class="alert alert-danger small">${escapeHtml(data.error || 'Could not load identity')}</div>`;
        return;
    }
    const profile = data.profile
        ? `${data.profile.source === 'zmm' ? 'ZMM entry' : 'Profile'} <code>${escapeHtml(data.profile.id)}</code>`
        : 'No ZMM entry or profile';
    const facts = Object.entries(data.facts_by_source || {})
        .map(([s, n]) => `${escapeHtml(s.replace('_', ' '))}: ${n}`).join(' · ') || 'none recorded';
    root.innerHTML = `
        <div class="small mb-3">
            <div><span class="text-muted">Model</span> <code>${escapeHtml(data.model)}</code>
                 · <span class="text-muted">Quirk</span> ${escapeHtml(data.quirk || 'none')}
                 · ${profile}</div>
            <div class="text-muted">Evidence — ${facts}</div>
            <div data-identity-error></div>
        </div>
        ${data.endpoints.map(endpointCard).join('')}
        <div class="mt-2">
            <button class="btn btn-outline-secondary btn-sm" data-identity-draft>Draft ZMM entry</button>
            <div data-identity-draft-panel></div>
        </div>`;
}

function renderDraft(root, data) {
    const panel = root.querySelector('[data-identity-draft-panel]');
    if (!panel) return;
    if (!data.success) {
        panel.innerHTML = `<div class="alert alert-danger small py-1 mt-2">${escapeHtml(data.error)}</div>`;
        return;
    }
    const candidates = (data.candidates || []).map(c =>
        `<li><code>EP${c.ep} ${escapeHtml(c.cluster)}/${escapeHtml(c.attr)}</code>
             ${escapeHtml(c.type || '')}${c.value !== undefined && c.value !== null ? ` = ${escapeHtml(c.value)}` : ''}</li>`).join('');
    panel.innerHTML = `
        <div class="small text-muted mt-2">Only what the evidence supports is filled in. Review, then save as your profile for this model.</div>
        <textarea class="form-control font-monospace small mt-1" rows="16" data-identity-draft-json>${escapeHtml(JSON.stringify(data.entry, null, 2))}</textarea>
        <button class="btn btn-primary btn-sm mt-2" data-identity-draft-save>Save as my profile</button>
        ${candidates ? `<div class="small mt-2">Writable manufacturer attributes (name them by trying them before adding as settings):
            <ul class="mb-0">${candidates}</ul></div>` : ''}`;
}

function send(root, ieee, body) {
    return sendTo(root, `/api/device/${encodeURIComponent(ieee)}/identity`, body);
}

async function sendTo(root, url, body) {
    const err = root.querySelector('[data-identity-error]');
    try {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!data.success) {
            if (err) err.innerHTML = `<div class="alert alert-danger small py-1 mt-2">${escapeHtml(data.error)}</div>`;
            return;
        }
        render(root, data);
    } catch (e) {
        log.error('identity update failed', e);
        if (err) err.innerHTML = `<div class="alert alert-danger small py-1 mt-2">Request failed: ${escapeHtml(e.message)}</div>`;
    }
}

export async function initIdentityTab(ieee) {
    const root = document.querySelector(`[data-identity-ieee="${CSS.escape(ieee)}"]`);
    if (!root) return;
    if (!root.dataset.bound) {
        root.dataset.bound = '1';
        root.addEventListener('click', ev => {
            const t = ev.target.closest('button');
            if (!t) return;
            const ep = Number(t.dataset.ep ?? t.dataset.identityLabel);
            if (t.dataset.identityDraft !== undefined) {
                fetch(`/api/device/${encodeURIComponent(ieee)}/identity/draft`)
                    .then(r => r.json()).then(d => renderDraft(root, d))
                    .catch(e => renderDraft(root, { success: false, error: e.message }));
                return;
            }
            if (t.dataset.identityDraftSave !== undefined) {
                let entry;
                try {
                    entry = JSON.parse(root.querySelector('[data-identity-draft-json]').value);
                } catch (e) {
                    renderDraft(root, { success: false, error: `Not valid JSON: ${e.message}` });
                    return;
                }
                sendTo(root, `/api/device/${encodeURIComponent(ieee)}/identity/draft`, { entry });
                return;
            }
            if (t.dataset.identitySet !== undefined) {
                send(root, ieee, { endpoint_id: ep, subject: t.dataset.subject, value: t.dataset.identitySet });
            } else if (t.dataset.identityReset !== undefined) {
                send(root, ieee, { endpoint_id: ep, subject: t.dataset.subject, value: null });
            } else if (t.dataset.identityLabel !== undefined) {
                const input = root.querySelector(`[data-identity-label-for="${ep}"]`);
                send(root, ieee, { endpoint_id: ep, subject: 'label', value: input?.value ?? '' });
            }
        });
    }
    try {
        const res = await fetch(`/api/device/${encodeURIComponent(ieee)}/identity`);
        render(root, await res.json());
    } catch (e) {
        log.error('identity load failed', e);
        root.innerHTML = `<div class="alert alert-danger small">Request failed: ${escapeHtml(e.message)}</div>`;
    }
}
