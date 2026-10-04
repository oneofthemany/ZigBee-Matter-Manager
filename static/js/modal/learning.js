/**
 * "Learn this device": the guided experiments of docs/plans/device-learning.md,
 * then review, save (with history and rollback), export and import.
 * Mounted inside the Identity tab. Event delegation only (strict CSP target).
 */
import { editorHtml, applyField, applyButton } from './quirk_editor.js';

const log = zmmLog('modal-learning');

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const CONF = { high: 'bg-success', medium: 'bg-info text-dark', low: 'bg-warning text-dark', none: 'bg-secondary' };
const STATUS = { pending: 'bg-light text-dark', running: 'bg-primary', done: 'bg-info text-dark',
                 accepted: 'bg-success', skipped: 'bg-secondary' };

const timers = new Map();      // ieee -> countdown interval

async function api(ieee, path, body) {
    const opts = body === undefined ? {} : {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    };
    const res = await fetch(`/api/device/${encodeURIComponent(ieee)}${path}`, opts);
    return res.json();
}

function inputHtml(step, spec) {
    const id = `learn-${step.key}-${spec.name}`.replace(/[^a-zA-Z0-9_-]/g, '_');
    const field = spec.type === 'select'
        ? `<select class="form-select form-select-sm" data-learn-input="${esc(spec.name)}" id="${id}">
               ${spec.options.map(o => `<option value="${esc(o)}">${esc(o)}</option>`).join('')}</select>`
        : `<input class="form-control form-control-sm" data-learn-input="${esc(spec.name)}" id="${id}"
                  type="${spec.type === 'number' ? 'number' : 'text'}" ${spec.min != null ? `min="${esc(spec.min)}"` : ''}>`;
    return `<div class="col-auto"><label class="form-label small mb-0" for="${id}">${esc(spec.label)}</label>${field}</div>`;
}

function proposalsHtml(step) {
    const rows = step.proposals.map((p, i) => p.path
        ? `<div class="form-check small">
             <input class="form-check-input" type="checkbox" data-learn-accept="${i}" id="lp-${esc(step.key)}-${i}"
                    ${p.confidence === 'high' ? 'checked' : ''}>
             <label class="form-check-label" for="lp-${esc(step.key)}-${i}">
               <code>${esc(p.path)}</code> = <code>${esc(JSON.stringify(p.value))}</code>
               <span class="badge ${CONF[p.confidence] || 'bg-secondary'}">${esc(p.confidence)}</span>
               <div class="text-muted">${esc(p.evidence)}</div></label></div>`
        : `<div class="small text-muted">${esc(p.evidence)}</div>`).join('');
    return rows || '<div class="small text-muted">Nothing was inferred.</div>';
}

function tryHtml(step, trial) {
    const tried = step.tried.length
        ? `<div class="small text-muted">Ruled out: ${step.tried.map(esc).join(', ')}</div>` : '';
    if (trial && trial.step === step.key) {
        return `<div class="alert alert-warning small py-2 mb-0">
            ZMM wrote <code>EP${esc(trial.ep)} ${esc(trial.cluster)}/${esc(trial.attr)}</code>:
            ${esc(trial.old)} → ${esc(trial.new)}. It is put back when you answer, or in
            <span data-learn-trial-left>${esc(trial.expires_in)}</span>s.
            <div class="mt-1">Did the device change as you named it?
            <button class="btn btn-success btn-sm ms-2" data-learn-answer="yes">Yes, that's it</button>
            <button class="btn btn-outline-secondary btn-sm" data-learn-answer="no">No change</button></div></div>`;
    }
    return `<div class="row g-2 align-items-end">${step.inputs.map(s => inputHtml(step, s)).join('')}</div>
        ${tried}<button class="btn btn-outline-primary btn-sm mt-2" data-learn-candidates="${esc(step.key)}"
            ${trial ? 'disabled' : ''}>Show what ZMM may flip</button>
        <div data-learn-cands></div>`;
}

function stepHtml(step, running, trial) {
    const status = `<span class="badge ${STATUS[step.status] || 'bg-light text-dark'}">${esc(step.status)}</span>`;
    let body = `<div class="small mb-2">${esc(step.instruction)}</div>`;
    const disabled = running && running !== step.key ? 'disabled' : '';
    if (step.mode === 'summary') {
        body += step.proposals.length
            ? proposalsHtml(step) + `<div class="mt-2 d-flex gap-2">
                <button class="btn btn-success btn-sm" data-learn-decide="${esc(step.key)}">Accept selected</button>
                <button class="btn btn-outline-secondary btn-sm" data-learn-skip="${esc(step.key)}">Skip</button></div>`
            : '<div class="small text-muted">Run the switch tests first.</div>';
    } else if (step.mode === 'try_write' && step.status !== 'done') {
        body += tryHtml(step, trial);
        if (step.proposals.length) body += proposalsHtml(step);
    } else if (step.status === 'running') {
        body += `<div class="d-flex align-items-center gap-2">
            <span class="small" data-learn-countdown="${esc(step.key)}">${esc(step.window_s)}s</span>
            <button class="btn btn-primary btn-sm" data-learn-finish="${esc(step.key)}">Done</button></div>
            <div data-learn-live class="mt-2"></div>`;
    } else if (step.status === 'done') {
        body += proposalsHtml(step) + `<div class="mt-2 d-flex gap-2">
            <button class="btn btn-success btn-sm" data-learn-decide="${esc(step.key)}">Accept selected</button>
            <button class="btn btn-outline-secondary btn-sm" data-learn-skip="${esc(step.key)}">Skip</button>
            <button class="btn btn-outline-primary btn-sm" data-learn-redo="${esc(step.key)}">Redo</button></div>`;
    } else {
        body += `<div class="row g-2 align-items-end">${step.inputs.map(s => inputHtml(step, s)).join('')}
            <div class="col-auto"><button class="btn btn-outline-primary btn-sm" ${disabled}
                data-learn-begin="${esc(step.key)}">${step.status === 'pending' ? 'Start' : 'Run again'}</button></div></div>`;
    }
    return `<div class="card mb-2" data-learn-step="${esc(step.key)}">
        <div class="card-header py-1 d-flex gap-2 align-items-center">
            <span class="fw-bold small">${esc(step.title)}</span><span class="small text-muted">${esc(step.label)}</span>
            <span class="ms-auto">${status}</span></div>
        <div class="card-body py-2">${body}</div></div>`;
}

function liveHtml(live, step) {
    if (!live?.length) return '';
    const rows = live.map(r => {
        const own = step && step.label === r.label;
        const warn = r.moved && !own;
        return `<tr class="${warn ? 'table-warning' : (r.moved ? 'table-success' : '')}">
            <td>${esc(r.label)}${own ? ' <span class="badge bg-primary">testing</span>' : ''}</td>
            <td>${r.on == null ? '—' : (r.on ? 'On' : 'Off')}</td>
            <td class="text-end">${r.power_w == null ? '—' : `${esc(r.power_w)} W`}</td>
            <td class="small">${warn ? 'also showing this load' : (r.moved ? 'moved' : '')}</td></tr>`;
    }).join('');
    return `<table class="table table-sm small mb-0"><thead><tr><th>Endpoint</th><th>Switch</th>
        <th class="text-end">Power now</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
}

function updateLive(root, st) {
    const box = root.querySelector('[data-learn-live]');
    if (box) box.innerHTML = liveHtml(st.live, st.steps.find(x => x.key === st.running));
}

function renderState(root, ieee, st) {
    const box = root.querySelector('[data-learn-body]');
    if (!st.success) {
        box.innerHTML = `<div class="alert alert-danger small py-1">${esc(st.error)}</div>`;
        return;
    }
    if (!st.active) {
        box.innerHTML = `<button class="btn btn-outline-primary btn-sm" data-learn-start>Learn this device</button>
            <button class="btn btn-outline-secondary btn-sm ms-1" data-learn-review>Edit its entry</button>
            <div class="small text-muted mt-1">Learn: guided experiments that settle what the evidence alone cannot.
            Edit: correct types, names, metering and scaling yourself.</div>
            <div data-learn-review-box></div>`;
        return;
    }
    box.innerHTML = (st.steps.length
        ? st.steps.map(s => stepHtml(s, st.running, st.trial)).join('')
        : '<div class="small text-muted">No recipe applies to this device yet.</div>')
        + `<div class="d-flex gap-2 mt-2">
            <button class="btn btn-primary btn-sm" data-learn-review>Review &amp; save</button>
            <button class="btn btn-outline-secondary btn-sm" data-learn-end>End</button></div>
           <div data-learn-review-box></div>`;
    updateLive(root, st);
    startCountdown(root, ieee, st);
}

function startCountdown(root, ieee, st) {
    clearInterval(timers.get(ieee));
    // Both count from a deadline: hidden tabs throttle timers, and the step's
    // finish is sent from here, so a drifting count would stretch the step.
    const secondsLeft = deadline => Math.ceil((deadline - Date.now()) / 1000);
    if (st.trial) {
        const deadline = Date.now() + st.trial.expires_in * 1000;
        let left = st.trial.expires_in;
        timers.set(ieee, setInterval(() => {
            left = secondsLeft(deadline);
            const el = root.querySelector('[data-learn-trial-left]');
            if (el) el.textContent = `${Math.max(left, 0)}`;
            if (left <= 0) {                       // the server has put it back: show that
                clearInterval(timers.get(ieee));
                run(root, ieee, '/learn', undefined);
            }
        }, 1000));
        return;
    }
    const step = st.steps.find(s => s.key === st.running);
    if (!step) return;
    const deadline = Date.now() + step.window_s * 1000;
    let left = step.window_s, ticks = 0;
    timers.set(ieee, setInterval(() => {
        left = secondsLeft(deadline);
        if (++ticks % 2 === 0 && !document.hidden) {   // live readings, without re-rendering the step
            api(ieee, '/learn').then(fresh => { if (fresh.running === step.key) updateLive(root, fresh); })
                .catch(() => {});
        }
        const el = root.querySelector(`[data-learn-countdown="${CSS.escape(step.key)}"]`);
        if (el) el.textContent = `${Math.max(left, 0)}s`;
        if (left <= 0) {
            clearInterval(timers.get(ieee));
            run(root, ieee, `/learn/step/${encodeURIComponent(step.key)}/finish`, {});
        }
    }, 1000));
}

async function run(root, ieee, path, body) {
    try {
        renderState(root, ieee, await api(ieee, path, body));
    } catch (e) {
        log.error('learning request failed', e);
        root.querySelector('[data-learn-body]').insertAdjacentHTML('afterbegin',
            `<div class="alert alert-danger small py-1">Request failed: ${esc(e.message)}</div>`);
    }
}

function changesHtml(preview) {
    return preview.length
        ? `<table class="table table-sm small mb-2"><thead><tr><th>What</th><th>Now</th><th>After saving</th></tr></thead>
           <tbody>${preview.map(c => `<tr><td>${esc(c.what)}</td><td>${esc(JSON.stringify(c.from))}</td>
               <td>${esc(JSON.stringify(c.to))}</td></tr>`).join('')}</tbody></table>`
        : '<div class="small text-muted mb-2">Saving changes nothing the device does now.</div>';
}

function reviewHtml(rv) {
    if (!rv.success) return `<div class="alert alert-danger small py-1 mt-2">${esc(rv.error)}</div>`;
    const learned = rv.learned.map(l => `<li><code>${esc(l.path)}</code>: ${esc(l.evidence)}</li>`).join('');
    const based = rv.based_on ? `Starts from the ${esc(rv.based_on.source)} entry <code>${esc(rv.based_on.id)}</code>.`
        : 'Starts from what the evidence says; no entry covers this model yet.';
    return `<div class="card mt-2"><div class="card-body py-2">
        <div class="small text-muted mb-1">${based} Saving stores it as your profile for
            <code>${esc(rv.device.model)}</code> (every device of that model here); you can roll it back.</div>
        <div data-qe-form>${editorHtml(rv.entry, rv.device)}</div>
        <div data-qe-error></div>
        <div class="fw-bold small mb-1 mt-2">If you save</div><div data-qe-changes>${changesHtml(rv.preview)}</div>
        ${learned ? `<div class="small mb-2">Learned here:<ul class="mb-0">${learned}</ul></div>` : ''}
        <details class="small"><summary>The entry as JSON (settings and anything the form does not cover)</summary>
        <textarea class="form-control font-monospace small mt-1" rows="12" data-learn-entry>${esc(JSON.stringify(rv.entry, null, 2))}</textarea>
        </details>
        <div class="d-flex gap-2 mt-2 flex-wrap">
            <button class="btn btn-primary btn-sm" data-learn-save>Save as my profile</button>
            <button class="btn btn-outline-secondary btn-sm" data-learn-rollback>Roll back my profile</button>
            <button class="btn btn-outline-secondary btn-sm" data-learn-export>Export</button>
            <label class="btn btn-outline-secondary btn-sm mb-0">Import<input type="file" accept=".json"
                   class="d-none" data-learn-import></label>
        </div><div data-learn-note class="mt-2"></div></div></div>`;
}

function note(root, html) {
    const el = root.querySelector('[data-learn-note]');
    if (el) el.innerHTML = html;
}

let previewSeq = 0;

/** Push an edit made in the form (or the JSON box) to the other, and re-preview. */
async function syncEntry(root, ieee, err, fromJson) {
    const errBox = root.querySelector('[data-qe-error]');
    errBox.innerHTML = err ? `<div class="alert alert-warning small py-1 mb-1">${esc(err)}</div>` : '';
    if (!fromJson) root.querySelector('[data-learn-entry]').value = JSON.stringify(root._entry, null, 2);
    root.querySelector('[data-qe-form]').innerHTML = editorHtml(root._entry, root._device);
    const seq = ++previewSeq;
    const out = await api(ieee, '/profile/preview', { entry: root._entry });
    if (seq !== previewSeq) return;          // a later edit's preview is on its way
    if (out.success) root.querySelector('[data-qe-changes]').innerHTML = changesHtml(out.preview);
}

async function onEditorChange(root, ieee, el) {
    if (el.matches('[data-learn-entry]')) {
        try { root._entry = JSON.parse(el.value); }
        catch (e) { return syncEntry(root, ieee, `Not valid JSON: ${e.message}`, true); }
        return syncEntry(root, ieee, null, true);
    }
    if (el.dataset.qe && root._entry) syncEntry(root, ieee, applyField(root._entry, el, root._device));
}

async function onClick(root, ieee, t) {
    const d = t.dataset;
    if ((d.qeAdd || d.qeDel) && root._entry) {
        return syncEntry(root, ieee, applyButton(root._entry, root, t));
    }
    const card = t.closest('[data-learn-step]');
    if (d.learnStart !== undefined) return run(root, ieee, '/learn/start', {});
    if (d.learnEnd !== undefined) { clearInterval(timers.get(ieee)); return run(root, ieee, '/learn/end', {}); }
    if (d.learnBegin || d.learnRedo) {
        const key = d.learnBegin || d.learnRedo;
        const inputs = {};
        card?.querySelectorAll('[data-learn-input]').forEach(el => { inputs[el.dataset.learnInput] = el.value; });
        return run(root, ieee, `/learn/step/${encodeURIComponent(key)}/begin`, { inputs });
    }
    if (d.learnCandidates) {
        const out = await api(ieee, `/learn/step/${encodeURIComponent(d.learnCandidates)}/candidates`);
        const box = card.querySelector('[data-learn-cands]');
        if (!out.success) { box.innerHTML = `<div class="alert alert-warning small py-1 mt-2">${esc(out.error)}</div>`; return; }
        root._cands = out.candidates;
        box.innerHTML = out.candidates.length ? `<table class="table table-sm small mt-2 mb-0"><tbody>
            ${out.candidates.map((x, i) => `<tr><td><code>EP${esc(x.ep)} ${esc(x.cluster)}/${esc(x.attr)}</code></td>
                <td>${esc(x.type)}</td><td>now ${esc(x.value ?? '?')}</td><td class="text-end">
                <button class="btn btn-outline-warning btn-sm" data-learn-try="${i}" ${x.ruled_out ? 'disabled' : ''}>
                ${x.ruled_out ? 'Ruled out' : 'Try'}</button></td></tr>`).join('')}</tbody></table>`
            : '<div class="small text-muted mt-2">Nothing this device offers can be flipped safely.</div>';
        return;
    }
    if (d.learnTry !== undefined) {
        const cand = root._cands?.[Number(d.learnTry)];
        if (!cand) return;
        const inputs = {};
        card?.querySelectorAll('[data-learn-input]').forEach(el => { inputs[el.dataset.learnInput] = el.value; });
        const key = card.dataset.learnStep;
        return run(root, ieee, `/learn/step/${encodeURIComponent(key)}/try`, { candidate: cand, inputs });
    }
    if (d.learnAnswer) {
        clearInterval(timers.get(ieee));
        const key = card.dataset.learnStep;
        return run(root, ieee, `/learn/step/${encodeURIComponent(key)}/answer`,
                   { changed: d.learnAnswer === 'yes' });
    }
    if (d.learnFinish) { clearInterval(timers.get(ieee)); return run(root, ieee, `/learn/step/${encodeURIComponent(d.learnFinish)}/finish`, {}); }
    if (d.learnDecide || d.learnSkip) {
        const key = d.learnDecide || d.learnSkip;
        const accept = d.learnDecide
            ? [...card.querySelectorAll('[data-learn-accept]:checked')].map(el => Number(el.dataset.learnAccept)) : [];
        return run(root, ieee, `/learn/step/${encodeURIComponent(key)}/decide`, { accept });
    }
    if (d.learnReview !== undefined) {
        const rv = await api(ieee, '/learn/review');
        root._entry = rv.entry;
        root._device = rv.device;
        root.querySelector('[data-learn-review-box]').innerHTML = reviewHtml(rv);
        return;
    }
    if (d.learnSave !== undefined) {
        let entry;
        try { entry = JSON.parse(root.querySelector('[data-learn-entry]').value); }
        catch (e) { return note(root, `<div class="alert alert-danger small py-1">Not valid JSON: ${esc(e.message)}</div>`); }
        const out = await api(ieee, '/learn/save', { entry });
        return note(root, out.success
            ? `<div class="alert alert-success small py-1">Saved as your profile <code>${esc(out.saved_profile)}</code>.</div>`
            : `<div class="alert alert-danger small py-1">${esc(out.error)}</div>`);
    }
    if (d.learnRollback !== undefined) {
        const out = await api(ieee, '/profile/rollback', {});
        return note(root, out.success
            ? `<div class="alert alert-success small py-1">${out.restored ? 'Previous version restored.'
                : 'Your profile was removed; the shipped entry applies again.'}</div>`
            : `<div class="alert alert-warning small py-1">${esc(out.error)}</div>`);
    }
    if (d.learnExport !== undefined) {
        const out = await api(ieee, '/profile/export');
        const blob = new Blob([JSON.stringify(out, null, 2)], { type: 'application/json' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `${(out.entry?.id || 'device').replace(/[^a-zA-Z0-9._-]/g, '_')}.zmm.json`;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 1000);
        return;
    }
    if (d.learnApplyImport !== undefined) {
        const out = await api(ieee, '/profile/import', { payload: root._pendingImport, apply: true });
        return note(root, out.success
            ? `<div class="alert alert-success small py-1">Imported as your profile.</div>`
            : `<div class="alert alert-danger small py-1">${esc(out.error)}</div>`);
    }
}

async function onImport(root, ieee, input) {
    const file = input.files?.[0];
    if (!file) return;
    let payload;
    try { payload = JSON.parse(await file.text()); }
    catch (e) { return note(root, `<div class="alert alert-danger small py-1">Not a JSON file.</div>`); }
    const out = await api(ieee, '/profile/import', { payload, apply: false });
    if (!out.success) return note(root, `<div class="alert alert-danger small py-1">${esc(out.error)}</div>`);
    root._pendingImport = payload;
    note(root, `<div class="small">Importing would change:</div>
        <ul class="small mb-1">${out.preview.map(c => `<li>${esc(c.what)}: ${esc(JSON.stringify(c.from))}
            → ${esc(JSON.stringify(c.to))}</li>`).join('') || '<li>nothing the device does now</li>'}</ul>
        <button class="btn btn-primary btn-sm" data-learn-apply-import>Apply import</button>`);
}

export function renderLearningPanel() {
    return `<div class="mt-3" data-learn-panel><div class="fw-bold small mb-1">Device learning</div>
        <div data-learn-body></div></div>`;
}

export async function initLearningPanel(root, ieee) {
    const panel = root.querySelector('[data-learn-panel]');
    if (!panel) return;
    if (!panel.dataset.bound) {
        panel.dataset.bound = '1';
        panel.addEventListener('click', ev => {
            const t = ev.target.closest('button');
            if (t && !t.disabled) onClick(panel, ieee, t);
        });
        panel.addEventListener('change', ev => {
            if (ev.target.matches('[data-learn-import]')) onImport(panel, ieee, ev.target);
            else onEditorChange(panel, ieee, ev.target);
        });
    }
    try {
        renderState(panel, ieee, await api(ieee, '/learn'));
    } catch (e) {
        log.error('learning state failed', e);
    }
}
