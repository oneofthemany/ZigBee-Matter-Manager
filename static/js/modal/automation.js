/**
 * Device Automation Tab — State Machine with Action Sequences
 * Location: static/js/modal/automation.js
 *
 * Visual builder for THEN/ELSE action sequences with step types:
 *   command, delay, wait_for, condition
 */

import { state } from '../state.js';

let cachedActuators = [];
let cachedAttributes = [];
let cachedAllDevices = [];
let currentSourceIeee = null;
let editingRuleId = null;

// Form state
let conditionRows = [], condIdCtr = 0;
let prereqRows = [], prereqIdCtr = 0;
let thenSteps = [], elseSteps = [], stepIdCtr = 0;

const OP = { 'eq':'=','neq':'≠','gt':'>','lt':'<','gte':'>=','lte':'<=' };
const OPT = { 'eq':'equals','neq':'not equal','gt':'>','lt':'<','gte':'≥','lte':'≤' };
const STEP_ICONS = { command:'fas fa-bolt', delay:'fas fa-clock', wait_for:'fas fa-hourglass-half', condition:'fas fa-filter' };
const STEP_LABELS = { command:'Command', delay:'Delay', wait_for:'Wait For', condition:'Gate' };

// ============================================================================
// MAIN RENDER
// ============================================================================

export function renderAutomationTab(device) {
    currentSourceIeee = device.ieee;
    return `
    <div id="automation-tab-content">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <span class="text-muted small">State-machine triggers — fires on transitions only.</span>
            <div>
                <button class="btn btn-sm btn-outline-secondary me-1" onclick="window._autoTrace()"><i class="fas fa-search"></i> Trace</button>
                <button class="btn btn-sm btn-success" onclick="window._autoShowForm()"><i class="fas fa-plus"></i> Add Rule</button>
            </div>
        </div>
        <div id="automation-add-form" class="card mb-3" style="display:none"></div>
        <div id="automation-trace-panel" class="card mb-3" style="display:none">
            <div class="card-header bg-dark text-white d-flex justify-content-between align-items-center py-1">
                <strong><i class="fas fa-search"></i> Trace</strong>
                <div class="d-flex gap-2 align-items-center">
                    <select class="form-select form-select-sm bg-dark text-white border-secondary" id="trace-filter" style="width:auto;max-width:220px;font-size:.75rem" onchange="window._autoRefreshTrace()"><option value="">All</option></select>
                    <button class="btn btn-sm btn-outline-light" onclick="window._autoRefreshTrace()"><i class="fas fa-sync-alt"></i></button>
                    <button class="btn btn-sm btn-outline-light" onclick="document.getElementById('automation-trace-panel').style.display='none'"><i class="fas fa-times"></i></button>
                </div>
            </div>
            <div class="card-body p-0" style="max-height:400px;overflow-y:auto"><div id="automation-trace-content" class="font-monospace small p-2">Loading...</div></div>
        </div>
        <div id="automation-rules-list"><div class="text-center text-muted py-3"><i class="fas fa-spinner fa-spin"></i></div></div>
    </div>`;
}

export async function initAutomationTab(ieee) {
    currentSourceIeee = ieee;
    try {
        const [rR, aR, actR, dR] = await Promise.all([
            fetch(`/api/automations?source_ieee=${encodeURIComponent(ieee)}`),
            fetch(`/api/automations/device/${encodeURIComponent(ieee)}/attributes`),
            fetch('/api/automations/actuators'),
            fetch('/api/automations/devices'),
        ]);
        cachedAttributes = await aR.json();
        cachedActuators = await actR.json();
        cachedAllDevices = await dR.json();
        renderRulesList(await rR.json());
    } catch (e) {
        const el = document.getElementById('automation-rules-list');
        if (el) el.innerHTML = `<div class="alert alert-danger">${e.message}</div>`;
    }
}

// ============================================================================
// RULES LIST
// ============================================================================

function renderRulesList(rules) {
    const el = document.getElementById('automation-rules-list');
    if (!el) return;
    if (!rules?.length) {
        el.innerHTML = `<div class="text-center text-muted py-4"><i class="fas fa-robot fa-2x mb-2 d-block opacity-50"></i>No rules. Click <strong>Add Rule</strong>.</div>`;
        return;
    }
    let h = '';
    rules.forEach(rule => {
        const en = rule.enabled !== false;
        const name = rule.name ? `<strong>${rule.name}</strong> ` : '';
        const st = rule._state || 'unknown';
        const running = rule._running ? '<span class="badge bg-warning text-dark ms-1">⏳ running</span>' : '';
        const stBadge = st === 'matched' ? '<span class="badge bg-success ms-1">matched</span>'
                      : st === 'unmatched' ? '<span class="badge bg-secondary ms-1">unmatched</span>'
                      : '<span class="badge bg-dark ms-1">init</span>';

        // Conditions
        let cH = '';
        (rule.conditions||[]).forEach((c,i) => {
            const pfx = i===0?'<strong class="text-primary">IF</strong>':'<strong class="text-warning">AND</strong>';
            const sus = c.sustain?`<span class="badge bg-info text-dark ms-1">⏱${c.sustain}s</span>`:'';
            cH += `<div class="small">${pfx} <code>${c.attribute}</code> <span class="badge bg-light text-dark border">${OP[c.operator]||c.operator}</span> <code>${c.value}</code>${sus}</div>`;
        });
        // Prerequisites
        (rule.prerequisites||[]).forEach(p => {
            cH += `<div class="small"><strong class="text-info">CHECK</strong> <span title="${p.ieee}">${p.device_name||p.ieee}</span> <code>${p.attribute}</code> <span class="badge bg-light text-dark border">${OP[p.operator]||p.operator}</span> <code>${p.value}</code></div>`;
        });
        // THEN sequence
        let tH = _renderSequenceSummary(rule.then_sequence||[], 'THEN', 'success');
        let eH = _renderSequenceSummary(rule.else_sequence||[], 'ELSE', 'danger');

        h += `
        <div class="card mb-2 ${en?'':'opacity-50'}">
            <div class="card-body py-2 px-3">
                <div class="d-flex justify-content-between align-items-start">
                    <div class="flex-grow-1">
                        <div class="mb-1">${name}<code class="text-muted small">${rule.id}</code>${stBadge}${running}</div>
                        ${cH}${tH}${eH}
                    </div>
                    <div class="d-flex gap-1 ms-2">
                        <span class="badge bg-secondary" title="Cooldown">${rule.cooldown||5}s</span>
                        <button class="btn btn-sm btn-outline-secondary" onclick="window._autoTraceRule('${rule.id}')" title="Trace"><i class="fas fa-search"></i></button>
                        <button class="btn btn-sm btn-outline-primary" onclick="window._autoEdit('${rule.id}')" title="Edit"><i class="fas fa-edit"></i></button>
                        <button class="btn btn-sm ${en?'btn-outline-success':'btn-outline-secondary'}" onclick="window._autoToggle('${rule.id}')" title="${en?'Disable':'Enable'}"><i class="fas fa-${en?'toggle-on':'toggle-off'}"></i></button>
                        <button class="btn btn-sm btn-outline-danger" onclick="window._autoDelete('${rule.id}')" title="Delete"><i class="fas fa-trash"></i></button>
                    </div>
                </div>
            </div>
        </div>`;
    });
    el.innerHTML = h;
}

function _renderSequenceSummary(steps, label, color) {
    if (!steps.length) return '';
    let h = `<div class="small mt-1"><strong class="text-${color}">${label}</strong> `;
    steps.forEach((s,i) => {
        if (i > 0) h += '<i class="fas fa-arrow-right text-muted mx-1"></i>';
        if (s.type === 'command') {
            h += `<span class="badge bg-info text-dark">${s.command}${s.value!=null?' ='+s.value:''}</span>`;
            h += `<span class="text-muted" title="${s.target_ieee}"> ${s.target_name||s.target_ieee||'?'}${s.endpoint_id?' EP'+s.endpoint_id:''}</span>`;
        } else if (s.type === 'delay') {
            h += `<span class="badge bg-warning text-dark">⏱ ${s.seconds}s</span>`;
        } else if (s.type === 'wait_for') {
            h += `<span class="badge bg-secondary">⏳ ${s.device_name||s.ieee} ${s.attribute} ${OP[s.operator]||''} ${s.value}</span>`;
        } else if (s.type === 'condition') {
            h += `<span class="badge bg-dark">🔒 ${s.device_name||s.ieee} ${s.attribute} ${OP[s.operator]||''} ${s.value}</span>`;
        }
    });
    h += '</div>';
    return h;
}

// ============================================================================
// FORM
// ============================================================================

function renderForm(rule) {
    const isEdit = !!rule;
    editingRuleId = isEdit ? rule.id : null;
    const el = document.getElementById('automation-add-form');
    if (!el) return;

    el.innerHTML = `
    <div class="card-header bg-light d-flex justify-content-between align-items-center">
        <strong><i class="fas fa-${isEdit?'edit':'bolt'}"></i> ${isEdit?'Edit':'New'} Automation</strong>
        <button class="btn btn-sm btn-outline-secondary" onclick="window._autoHideForm()"><i class="fas fa-times"></i></button>
    </div>
    <div class="card-body">
        <div class="mb-3"><label class="form-label small text-muted mb-0">Rule Name</label>
            <input type="text" class="form-control form-control-sm" id="auto-name" placeholder="e.g. Kitchen motion light" value="${isEdit?(rule.name||''):''}">
        </div>
        <div class="mb-3">
            <div class="d-flex justify-content-between align-items-center mb-1">
                <label class="form-label fw-bold small mb-0">Conditions (trigger evaluation)</label>
                <button class="btn btn-sm btn-outline-primary" onclick="window._autoAddCond()"><i class="fas fa-plus"></i></button>
            </div>
            <div id="cond-builder"></div>
        </div>
        <div class="mb-3">
            <div class="d-flex justify-content-between align-items-center mb-1">
                <label class="form-label fw-bold small mb-0">Prerequisites <span class="text-muted fw-normal">(optional device state checks)</span></label>
                <button class="btn btn-sm btn-outline-info" onclick="window._autoAddPrereq()"><i class="fas fa-plus"></i></button>
            </div>
            <div id="prereq-builder"></div>
        </div>
        <div class="mb-3">
            <label class="form-label fw-bold small text-success">THEN sequence <span class="fw-normal text-muted">(on conditions becoming true)</span></label>
            <div id="then-builder"></div>
            <div class="mt-1">${_stepAddButtons('then')}</div>
        </div>
        <div class="mb-3">
            <label class="form-label fw-bold small text-danger">ELSE sequence <span class="fw-normal text-muted">(on conditions becoming false)</span></label>
            <div id="else-builder"></div>
            <div class="mt-1">${_stepAddButtons('else')}</div>
        </div>
        <div class="row g-2 mb-3">
            <div class="col-md-4"><label class="form-label small text-muted mb-0">Cooldown (seconds)</label>
                <input type="number" class="form-control form-control-sm" id="auto-cooldown" value="${isEdit?(rule.cooldown||5):5}" min="0">
            </div>
            <div class="col-md-4 d-flex align-items-end">
                <button class="btn btn-primary btn-sm w-100" onclick="window._autoSave()"><i class="fas fa-save"></i> ${isEdit?'Update':'Save'}</button>
            </div>
        </div>
    </div>`;
    el.style.display = 'block';

    // Init conditions
    conditionRows = []; condIdCtr = 0;
    if (isEdit && rule.conditions?.length) {
        rule.conditions.forEach(() => conditionRows.push(condIdCtr++));
    } else {
        conditionRows.push(condIdCtr++);
    }
    _refreshConds();
    if (isEdit && rule.conditions) {
        setTimeout(() => rule.conditions.forEach((c,i) => { if (conditionRows[i]!==undefined) _setCondVals(conditionRows[i],c); }), 50);
    }

    // Init prereqs
    prereqRows = []; prereqIdCtr = 0;
    if (isEdit && rule.prerequisites?.length) {
        rule.prerequisites.forEach(() => prereqRows.push(prereqIdCtr++));
    }
    _refreshPrereqs();
    if (isEdit && rule.prerequisites) {
        setTimeout(() => rule.prerequisites.forEach((p,i) => { if(prereqRows[i]!==undefined) _setPrereqVals(prereqRows[i],p); }), 100);
    }

    // Init sequences
    thenSteps = []; elseSteps = []; stepIdCtr = 0;
    if (isEdit) {
        (rule.then_sequence||[]).forEach(s => { const id = stepIdCtr++; thenSteps.push({id, ...s}); });
        (rule.else_sequence||[]).forEach(s => { const id = stepIdCtr++; elseSteps.push({id, ...s}); });
    }
    _refreshSteps('then');
    _refreshSteps('else');
}

function _stepAddButtons(path) {
    return `<div class="btn-group btn-group-sm">
        <button class="btn btn-outline-success" onclick="window._autoAddStep('${path}','command')"><i class="fas fa-bolt"></i> Command</button>
        <button class="btn btn-outline-warning" onclick="window._autoAddStep('${path}','delay')"><i class="fas fa-clock"></i> Delay</button>
        <button class="btn btn-outline-secondary" onclick="window._autoAddStep('${path}','wait_for')"><i class="fas fa-hourglass-half"></i> Wait For</button>
        <button class="btn btn-outline-dark" onclick="window._autoAddStep('${path}','condition')"><i class="fas fa-filter"></i> Gate</button>
    </div>`;
}

// ============================================================================
// VALUE INPUT - dropdown for bool/enum, text otherwise
// ============================================================================

function _valInput(cls, rowId, opts, cur) {
    if (opts?.length) {
        let h = `<select class="form-select form-select-sm ${cls}" data-row="${rowId}">`;
        opts.forEach(v => { h += `<option value="${v}" ${cur!==undefined&&String(cur).toLowerCase()===String(v).toLowerCase()?'selected':''}>${v}</option>`; });
        return h + '</select>';
    }
    return `<input type="text" class="form-control form-control-sm ${cls}" data-row="${rowId}" placeholder="Value" value="${cur!==undefined?cur:''}">`;
}

// ============================================================================
// CONDITION BUILDER
// ============================================================================

function _renderCondRow(id) {
    const opts = cachedAttributes.map(a => {
        const ic = a.type==='boolean'?'⚡':a.type==='float'?'📊':'📈';
        return `<option value="${a.attribute}" data-type="${a.type}" data-operators='${JSON.stringify(a.operators)}' data-current="${a.current_value}" data-valueopts='${JSON.stringify(a.value_options||[])}'>${ic} ${a.attribute} (${a.current_value})</option>`;
    }).join('');
    const idx = conditionRows.indexOf(id);
    return `<div class="row g-1 mb-1 align-items-center" id="cond-${id}">
        <div class="col-auto"><span class="badge ${idx===0?'bg-primary':'bg-warning text-dark'} small">${idx===0?'IF':'AND'}</span></div>
        <div class="col"><select class="form-select form-select-sm ca" data-row="${id}" onchange="window._autoCondAttr(${id},this)"><option value="">Attribute...</option>${opts}</select></div>
        <div class="col-auto"><select class="form-select form-select-sm co" data-row="${id}" style="width:90px"><option value="">Op</option></select></div>
        <div class="col" id="cv-${id}"><input type="text" class="form-control form-control-sm cv" data-row="${id}" placeholder="Value"></div>
        <div class="col-auto" style="width:70px"><input type="number" class="form-control form-control-sm cs" data-row="${id}" placeholder="⏱s" min="0" title="Sustain (optional)"></div>
        <div class="col-auto">${idx>0?`<button class="btn btn-sm btn-outline-danger" onclick="window._autoRmCond(${id})"><i class="fas fa-times"></i></button>`:'<div style="width:31px"></div>'}</div>
    </div>`;
}
function _refreshConds() { const el=document.getElementById('cond-builder'); if(el) el.innerHTML=conditionRows.map(id=>_renderCondRow(id)).join(''); }
function _setCondVals(id,c) {
    const s=document.querySelector(`#cond-${id} .ca`); if(!s) return; s.value=c.attribute;
    window._autoCondAttr(id,s);
    setTimeout(()=>{ const o=document.querySelector(`#cond-${id} .co`); if(o) o.value=c.operator;
        const v=document.querySelector(`#cv-${id} .cv`); if(v) v.value=String(c.value);
        const ss=document.querySelector(`#cond-${id} .cs`); if(ss&&c.sustain) ss.value=c.sustain; },20);
}

// ============================================================================
// PREREQUISITE BUILDER
// ============================================================================

function _renderPrereqRow(id) {
    const devs = cachedAllDevices.filter(d=>d.ieee!==currentSourceIeee).map(d=>
        `<option value="${d.ieee}" data-keys='${JSON.stringify(d.state_keys)}'>${d.friendly_name}</option>`).join('');
    return `<div class="row g-1 mb-1 align-items-center" id="pq-${id}">
        <div class="col-auto"><span class="badge bg-info text-dark small">CHECK</span></div>
        <div class="col"><select class="form-select form-select-sm pd" data-row="${id}" onchange="window._autoPrereqDev(${id},this)"><option value="">Device...</option>${devs}</select></div>
        <div class="col"><select class="form-select form-select-sm pa" data-row="${id}" onchange="window._autoPrereqAttr(${id},this)"><option value="">Attr...</option></select></div>
        <div class="col-auto"><select class="form-select form-select-sm po" data-row="${id}" style="width:80px">${Object.entries(OP).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></div>
        <div class="col" id="pv-${id}"><input type="text" class="form-control form-control-sm pv" data-row="${id}" placeholder="Value"></div>
        <div class="col-auto"><button class="btn btn-sm btn-outline-danger" onclick="window._autoRmPrereq(${id})"><i class="fas fa-times"></i></button></div>
    </div>`;
}
function _refreshPrereqs() { const el=document.getElementById('prereq-builder'); if(el) el.innerHTML=prereqRows.map(id=>_renderPrereqRow(id)).join(''); }
function _setPrereqVals(id,p) {
    const d=document.querySelector(`#pq-${id} .pd`); if(!d) return; d.value=p.ieee;
    window._autoPrereqDev(id,d);
    setTimeout(()=>{ const a=document.querySelector(`#pq-${id} .pa`); if(a){a.value=p.attribute;window._autoPrereqAttr(id,a);}
        setTimeout(()=>{ const o=document.querySelector(`#pq-${id} .po`); if(o) o.value=p.operator;
            const v=document.querySelector(`#pv-${id} .pv`); if(v) v.value=String(p.value); },20); },100);
}

// ============================================================================
// STEP BUILDER
// ============================================================================

function _renderStep(step, path) {
    const sId = `step-${path}-${step.id}`;
    const steps = path === 'then' ? thenSteps : elseSteps;
    const idx = steps.indexOf(step) + 1;
    const total = steps.length;
    const icon = STEP_ICONS[step.type] || 'fas fa-cog';
    const label = STEP_LABELS[step.type] || step.type;
    let body = '';

    if (step.type === 'command') {
        // Target device + command
        const actOpts = cachedActuators.map(d =>
            `<option value="${d.ieee}" data-commands='${JSON.stringify(d.commands)}' ${step.target_ieee===d.ieee?'selected':''}>${d.friendly_name}</option>`
        ).join('');
        body = `
        <div class="row g-1">
            <div class="col-md-5"><select class="form-select form-select-sm st-target" data-sid="${step.id}" data-path="${path}" onchange="window._autoStepTargetChange(${step.id},'${path}',this)"><option value="">Target...</option>${actOpts}</select></div>
            <div class="col-md-4"><select class="form-select form-select-sm st-cmd" data-sid="${step.id}"><option value="">Command...</option></select></div>
            <div class="col-md-3"><input type="text" class="form-control form-control-sm st-val" data-sid="${step.id}" placeholder="Value" value="${step.value!=null?step.value:''}"></div>
        </div>
        <input type="hidden" class="st-ep" data-sid="${step.id}" value="${step.endpoint_id||''}">`;
    } else if (step.type === 'delay') {
        body = `<div class="row g-1"><div class="col-auto"><input type="number" class="form-control form-control-sm st-secs" data-sid="${step.id}" value="${step.seconds||5}" min="1" style="width:80px"></div><div class="col-auto pt-1">seconds</div></div>`;
    } else if (step.type === 'wait_for' || step.type === 'condition') {
        const devOpts = cachedAllDevices.map(d =>
            `<option value="${d.ieee}" data-keys='${JSON.stringify(d.state_keys)}' ${step.ieee===d.ieee?'selected':''}>${d.friendly_name}</option>`
        ).join('');
        const timeoutH = step.type === 'wait_for' ? `<div class="col-auto"><input type="number" class="form-control form-control-sm st-timeout" data-sid="${step.id}" value="${step.timeout||300}" min="1" style="width:70px" title="Timeout (s)"></div>` : '';
        body = `
        <div class="row g-1 align-items-center">
            <div class="col"><select class="form-select form-select-sm st-ieee" data-sid="${step.id}" onchange="window._autoStepDevChange(${step.id},'${path}',this)"><option value="">Device...</option>${devOpts}</select></div>
            <div class="col"><select class="form-select form-select-sm st-attr" data-sid="${step.id}"><option value="">Attr...</option></select></div>
            <div class="col-auto"><select class="form-select form-select-sm st-op" data-sid="${step.id}" style="width:70px">${Object.entries(OP).map(([k,v])=>`<option value="${k}" ${step.operator===k?'selected':''}>${v}</option>`).join('')}</select></div>
            <div class="col" id="stv-${step.id}"><input type="text" class="form-control form-control-sm st-value" data-sid="${step.id}" placeholder="Value" value="${step.value!=null?step.value:''}"></div>
            ${timeoutH}
        </div>`;
    }

    return `<div class="card card-body p-2 mb-1 bg-light" id="${sId}">
        <div class="d-flex justify-content-between align-items-center mb-1">
            <span class="badge bg-dark"><i class="${icon}"></i> ${label} <small>${idx}/${total}</small></span>
            <button class="btn btn-sm btn-outline-danger py-0 px-1" onclick="window._autoRmStep(${step.id},'${path}')"><i class="fas fa-times"></i></button>
        </div>
        ${body}
    </div>`;
}

function _refreshSteps(path) {
    const el = document.getElementById(`${path}-builder`);
    if (!el) return;
    const steps = path === 'then' ? thenSteps : elseSteps;
    if (!steps.length) {
        el.innerHTML = `<div class="text-muted small fst-italic">No steps — add one below.</div>`;
        return;
    }
    el.innerHTML = steps.map(s => _renderStep(s, path)).join('');
    // Restore command dropdowns for existing command steps
    steps.forEach(s => {
        if (s.type === 'command' && s.target_ieee) {
            setTimeout(() => {
                const sel = document.querySelector(`.st-target[data-sid="${s.id}"]`);
                if (sel) {
                    const opt = sel.options[sel.selectedIndex];
                    if (opt?.dataset?.commands) {
                        _populateStepCommands(s.id, JSON.parse(opt.dataset.commands), s.command, s.endpoint_id);
                    }
                }
            }, 30);
        } else if ((s.type === 'wait_for' || s.type === 'condition') && s.ieee) {
            setTimeout(() => {
                const sel = document.querySelector(`.st-ieee[data-sid="${s.id}"]`);
                if (sel) _loadStepDevAttrs(s.id, s.ieee, s.attribute, s.value);
            }, 30);
        }
    });
}

function _populateStepCommands(stepId, commands, selectedCmd, selectedEp) {
    const sel = document.querySelector(`.st-cmd[data-sid="${stepId}"]`);
    if (!sel) return;
    sel.innerHTML = '<option value="">Command...</option>';
    (commands||[]).forEach(cmd => {
        const opt = document.createElement('option');
        opt.value = cmd.command;
        opt.dataset.ep = cmd.endpoint_id || '';
        opt.textContent = `${cmd.label||cmd.command}${cmd.endpoint_id?' (EP'+cmd.endpoint_id+')':''}`;
        if (selectedCmd === cmd.command && (!selectedEp || selectedEp == cmd.endpoint_id)) opt.selected = true;
        sel.appendChild(opt);
    });
    sel.onchange = () => {
        const o = sel.options[sel.selectedIndex];
        const epH = document.querySelector(`.st-ep[data-sid="${stepId}"]`);
        if (epH && o) epH.value = o.dataset.ep || '';
    };
    if (selectedEp) {
        const epH = document.querySelector(`.st-ep[data-sid="${stepId}"]`);
        if (epH) epH.value = selectedEp;
    }
}

async function _loadStepDevAttrs(stepId, ieee, selectedAttr, selectedVal) {
    const attrSel = document.querySelector(`.st-attr[data-sid="${stepId}"]`);
    if (!attrSel) return;
    try {
        const res = await fetch(`/api/automations/device/${encodeURIComponent(ieee)}/state`);
        const data = await res.json();
        attrSel.innerHTML = '<option value="">Attr...</option>';
        (data.attributes||[]).forEach(a => {
            const opt = document.createElement('option');
            opt.value = a.attribute;
            opt.dataset.valueopts = JSON.stringify(a.value_options||[]);
            opt.dataset.current = a.current_value;
            opt.dataset.type = a.type;
            opt.textContent = `${a.attribute} (${a.current_value})`;
            if (selectedAttr === a.attribute) opt.selected = true;
            attrSel.appendChild(opt);
        });
        attrSel.onchange = () => {
            const o = attrSel.options[attrSel.selectedIndex];
            if (!o) return;
            const vo = JSON.parse(o.dataset.valueopts||'[]');
            const w = document.getElementById(`stv-${stepId}`);
            if (w) w.innerHTML = _valInput('st-value', stepId, vo, selectedVal||'');
        };
        // Set initial value widget
        if (selectedAttr) {
            const o = attrSel.options[attrSel.selectedIndex];
            if (o) {
                const vo = JSON.parse(o.dataset.valueopts||'[]');
                const w = document.getElementById(`stv-${stepId}`);
                if (w) w.innerHTML = _valInput('st-value', stepId, vo, selectedVal!=null?selectedVal:'');
            }
        }
    } catch(e) { /* fallback: leave generic */ }
}

// ============================================================================
// WINDOW HANDLERS
// ============================================================================

// Conditions
window._autoCondAttr = function(id,sel) {
    const o = sel.options[sel.selectedIndex]; if(!o?.value) return;
    const ops = JSON.parse(o.dataset.operators||'["eq","neq"]');
    const vo = JSON.parse(o.dataset.valueopts||'[]');
    const cur = o.dataset.current;
    const type = o.dataset.type;
    const opS = document.querySelector(`#cond-${id} .co`);
    if(opS) opS.innerHTML = ops.map(op=>`<option value="${op}">${OP[op]} ${OPT[op]}</option>`).join('');
    const w = document.getElementById(`cv-${id}`);
    if(w) w.innerHTML = _valInput('cv', id, vo, type==='boolean'?String(cur).toLowerCase():'');
};
window._autoAddCond = ()=>{ if(conditionRows.length>=5)return alert('Max 5'); conditionRows.push(condIdCtr++); _refreshConds(); };
window._autoRmCond = id=>{ conditionRows=conditionRows.filter(r=>r!==id); _refreshConds(); };

// Prerequisites
window._autoPrereqDev = async (id,sel)=>{
    const ieee=sel.value; const aS=document.querySelector(`#pq-${id} .pa`); if(!aS||!ieee)return;
    aS.innerHTML='<option value="">Loading...</option>';
    try{
        const r=await fetch(`/api/automations/device/${encodeURIComponent(ieee)}/state`);
        const d=await r.json(); aS.innerHTML='<option value="">Attr...</option>';
        (d.attributes||[]).forEach(a=>{ const o=document.createElement('option'); o.value=a.attribute;
            o.dataset.valueopts=JSON.stringify(a.value_options||[]); o.dataset.current=a.current_value; o.dataset.type=a.type;
            o.textContent=`${a.attribute} (${a.current_value})`; aS.appendChild(o); });
        aS.onchange=()=>window._autoPrereqAttr(id,aS);
    } catch(e){ const dev=cachedAllDevices.find(d=>d.ieee===ieee); aS.innerHTML='<option value="">Attr...</option>';
        if(dev) dev.state_keys.forEach(k=>{aS.innerHTML+=`<option value="${k}">${k}</option>`;}); }
};
window._autoPrereqAttr = (id,sel)=>{
    const o=sel.options[sel.selectedIndex]; if(!o)return;
    const vo=JSON.parse(o.dataset?.valueopts||'[]');
    const w=document.getElementById(`pv-${id}`);
    if(w) w.innerHTML=_valInput('pv',id,vo,o.dataset?.type==='boolean'?String(o.dataset.current).toLowerCase():'');
};
window._autoAddPrereq = ()=>{ if(prereqRows.length>=5)return alert('Max 5'); prereqRows.push(prereqIdCtr++); _refreshPrereqs(); };
window._autoRmPrereq = id=>{ prereqRows=prereqRows.filter(r=>r!==id); _refreshPrereqs(); };

// Steps
window._autoAddStep = (path,type)=>{
    const steps = path==='then'?thenSteps:elseSteps;
    if(steps.length>=10) return alert('Max 10 steps');
    const s = {id:stepIdCtr++, type};
    if(type==='delay') s.seconds=5;
    if(type==='wait_for') s.timeout=300;
    steps.push(s);
    _refreshSteps(path);
};
window._autoRmStep = (id,path)=>{
    if(path==='then') thenSteps=thenSteps.filter(s=>s.id!==id);
    else elseSteps=elseSteps.filter(s=>s.id!==id);
    _refreshSteps(path);
};
window._autoStepTargetChange = (sid,path,sel)=>{
    const o=sel.options[sel.selectedIndex]; if(!o?.value) return;
    _populateStepCommands(sid, JSON.parse(o.dataset.commands||'[]'));
};
window._autoStepDevChange = (sid,path,sel)=>{
    const ieee=sel.value; if(!ieee) return;
    _loadStepDevAttrs(sid, ieee);
};

// Form
window._autoShowForm = ()=>renderForm(null);
window._autoHideForm = ()=>{ document.getElementById('automation-add-form').style.display='none'; editingRuleId=null; };
window._autoEdit = async ruleId=>{
    try{ const r=await fetch(`/api/automations/rule/${ruleId}`); if(!r.ok) return alert('Load failed');
        renderForm(await r.json()); document.getElementById('automation-add-form')?.scrollIntoView({behavior:'smooth'});
    }catch(e){alert(e.message);}
};

// Trace
window._autoTrace = async ()=>{
    document.getElementById('automation-trace-panel').style.display='block';
    const f=document.getElementById('trace-filter'); if(f){ const cur=f.value; f.innerHTML='<option value="">All</option>';
        try{ const r=await fetch(`/api/automations?source_ieee=${encodeURIComponent(currentSourceIeee)}`);
            (await r.json()).forEach(r=>{f.innerHTML+=`<option value="${r.id}">${r.name||r.id}</option>`;});
        }catch(e){} f.innerHTML+='<option value="-">System</option>'; f.value=cur||''; }
    _loadTrace();
};
window._autoRefreshTrace = _loadTrace;
window._autoTraceRule = async ruleId=>{ await window._autoTrace(); const f=document.getElementById('trace-filter'); if(f)f.value=ruleId; _loadTrace(); };

// ============================================================================
// SAVE
// ============================================================================

window._autoSave = async ()=>{
    // Conditions
    const conditions=[]; let valid=true;
    conditionRows.forEach(id=>{
        const attr=document.querySelector(`#cond-${id} .ca`)?.value;
        const op=document.querySelector(`#cond-${id} .co`)?.value;
        const vEl=document.querySelector(`#cv-${id} .cv`);
        const raw=vEl?.value; const sus=document.querySelector(`#cond-${id} .cs`)?.value;
        if(!attr||!op||raw===undefined||raw===''){valid=false;return;}
        const aInfo=cachedAttributes.find(a=>a.attribute===attr);
        let value=_coerceTyped(raw,aInfo?.type);
        const c={attribute:attr,operator:op,value};
        if(sus&&parseInt(sus)>0) c.sustain=parseInt(sus);
        conditions.push(c);
    });
    if(!valid||!conditions.length) return alert('Fill all condition fields.');

    // Prerequisites
    const prerequisites=[];
    prereqRows.forEach(id=>{
        const ieee=document.querySelector(`#pq-${id} .pd`)?.value;
        const attr=document.querySelector(`#pq-${id} .pa`)?.value;
        const op=document.querySelector(`#pq-${id} .po`)?.value;
        const vEl=document.querySelector(`#pv-${id} .pv`);
        const raw=vEl?.value;
        if(!ieee||!attr||!op||raw===undefined||raw==='') return;
        prerequisites.push({ieee,attribute:attr,operator:op,value:_coerce(raw)});
    });

    // Sequences
    const then_sequence = _gatherSteps(thenSteps);
    const else_sequence = _gatherSteps(elseSteps);
    if(!then_sequence.length&&!else_sequence.length) return alert('Add at least one step to THEN or ELSE.');

    const body = {
        name: document.getElementById('auto-name')?.value||'',
        source_ieee: currentSourceIeee,
        conditions, prerequisites, then_sequence, else_sequence,
        cooldown: parseInt(document.getElementById('auto-cooldown')?.value)||5,
        enabled: true,
    };
    try{
        let res;
        if(editingRuleId) res=await fetch(`/api/automations/${editingRuleId}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        else res=await fetch('/api/automations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        const data=await res.json();
        if(res.ok&&data.success){window._autoHideForm();await _refresh();}
        else alert('Failed: '+(data.detail||data.error||'Unknown'));
    }catch(e){alert(e.message);}
};

function _gatherSteps(steps) {
    return steps.map(s => {
        const d = {type:s.type};
        if(s.type==='command'){
            d.target_ieee = document.querySelector(`.st-target[data-sid="${s.id}"]`)?.value||'';
            d.command = document.querySelector(`.st-cmd[data-sid="${s.id}"]`)?.value||'';
            const v = document.querySelector(`.st-val[data-sid="${s.id}"]`)?.value;
            if(v!==undefined&&v!=='') d.value=_coerce(v);
            const ep = document.querySelector(`.st-ep[data-sid="${s.id}"]`)?.value;
            if(ep) d.endpoint_id=parseInt(ep);
        } else if(s.type==='delay'){
            d.seconds = parseInt(document.querySelector(`.st-secs[data-sid="${s.id}"]`)?.value)||5;
        } else if(s.type==='wait_for'||s.type==='condition'){
            d.ieee = document.querySelector(`.st-ieee[data-sid="${s.id}"]`)?.value||'';
            d.attribute = document.querySelector(`.st-attr[data-sid="${s.id}"]`)?.value||'';
            d.operator = document.querySelector(`.st-op[data-sid="${s.id}"]`)?.value||'eq';
            const v = document.querySelector(`.st-value[data-sid="${s.id}"]`)?.value;
            d.value = _coerce(v||'');
            if(s.type==='wait_for') d.timeout = parseInt(document.querySelector(`.st-timeout[data-sid="${s.id}"]`)?.value)||300;
        }
        return d;
    }).filter(d => {
        if(d.type==='command') return d.target_ieee && d.command;
        if(d.type==='delay') return d.seconds > 0;
        if(d.type==='wait_for'||d.type==='condition') return d.ieee && d.attribute;
        return false;
    });
}

// ============================================================================
// TOGGLE / DELETE / REFRESH
// ============================================================================

window._autoToggle = async id=>{ try{const r=await fetch(`/api/automations/${id}/toggle`,{method:'PATCH'});if(r.ok)await _refresh();}catch(e){alert(e.message);} };
window._autoDelete = async id=>{ if(!confirm('Delete?'))return;try{const r=await fetch(`/api/automations/${id}`,{method:'DELETE'});if(r.ok)await _refresh();}catch(e){alert(e.message);} };
async function _refresh() { if(!currentSourceIeee)return;try{const r=await fetch(`/api/automations?source_ieee=${encodeURIComponent(currentSourceIeee)}`);renderRulesList(await r.json());}catch(e){} }

// ============================================================================
// COERCE + TRACE
// ============================================================================

function _coerce(v) {
    if(typeof v!=='string')return v;
    const t=v.trim(),l=t.toLowerCase();
    if(l==='true')return true;if(l==='false')return false;
    if(!isNaN(t)&&t!=='')return parseFloat(t);
    return t;
}
function _coerceTyped(v,type) {
    if(!type) return _coerce(v);
    if(type==='boolean') return _coerce(v);
    if(type==='float') { const n=parseFloat(v); return isNaN(n)?v:n; }
    if(type==='integer') { const n=parseInt(v,10); return isNaN(n)?_coerce(v):n; }
    return String(v).trim();
}

async function _loadTrace() {
    const el=document.getElementById('automation-trace-content');if(!el)return;
    const fv=document.getElementById('trace-filter')?.value||'';
    const url=fv?`/api/automations/trace?rule_id=${encodeURIComponent(fv)}`:'/api/automations/trace';
    try{
        const entries=await(await fetch(url)).json();
        if(!entries?.length){el.innerHTML='<div class="text-muted p-2">No trace entries.</div>';return;}
        let h='';
        [...entries].reverse().forEach(e=>{
            const ts=new Date(e.timestamp*1000).toLocaleTimeString(),r=e.result||'';
            let cl='text-muted';
            if(r==='SUCCESS'||r.includes('FIRING')||r==='COMPLETE'||r==='WAIT_MET')cl='text-success';
            else if(r.includes('FAIL')||r.includes('ERROR')||r==='EXCEPTION'||r.includes('MISSING'))cl='text-danger';
            else if(r==='BLOCKED'||r==='SUSTAIN_WAIT'||r==='DELAY'||r==='WAITING')cl='text-warning';
            else if(r==='CANCELLED'||r==='WAIT_TIMEOUT')cl='text-info';

            h+=`<div class="border-bottom py-1 ${cl}"><span class="text-muted">${ts}</span> <span class="badge bg-dark">${e.phase||''}</span> <span class="badge bg-secondary">${r}</span> `;
            if(e.rule_id&&e.rule_id!=='-')h+=`<code>${e.rule_id}</code> `;
            h+=e.message||'';
            if(e.conditions?.length){
                h+='<div class="ms-3 mt-1">';
                e.conditions.forEach(c=>{const cc=c.result==='PASS'?'text-success':c.result==='SUSTAIN_WAIT'?'text-warning':'text-danger';
                    h+=`<div class="${cc}">#${c.index} ${c.attribute} ${c.operator||''} ${c.threshold_raw||c.threshold||'?'} → actual: ${c.actual_raw||'?'} (${c.actual_type||'?'}) [${c.result}]`;
                    if(c.sustain_elapsed!=null)h+=` ⏱${c.sustain_elapsed}s`;if(c.value_source)h+=` src:${c.value_source}`;if(c.reason)h+=` — ${c.reason}`;h+='</div>';});
                h+='</div>';}
            if(e.prerequisites?.length){
                h+='<div class="ms-3 mt-1">';
                e.prerequisites.forEach(p=>{const pc=p.result==='PASS'?'text-success':'text-danger';
                    h+=`<div class="${pc}">CHECK ${p.device_name||p.ieee} ${p.attribute} ${p.operator||''} ${p.threshold_raw||'?'}`;
                    if(p.threshold_normalised)h+=` [norm:${p.threshold_normalised}]`;
                    h+=` → actual: ${p.actual_raw||'?'}`;if(p.actual_normalised)h+=` [norm:${p.actual_normalised}]`;
                    h+=` [${p.result}]`;if(p.reason)h+=` — ${p.reason}`;h+='</div>';});
                h+='</div>';}
            if(e.error)h+=`<div class="ms-3 text-danger">${e.error}</div>`;
            h+='</div>';
        });
        el.innerHTML=h;
    }catch(err){el.innerHTML=`<div class="text-danger">${err.message}</div>`;}
}