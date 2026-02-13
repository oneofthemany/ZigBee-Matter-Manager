/**
 * Device Automation Tab
 * Location: static/js/modal/automation.js
 *
 * Features:
 *   - Named rules with edit support
 *   - Compound AND conditions with optional sustain
 *   - Prerequisites (other device state checks)
 *   - Optional delay before command
 *   - Bool/enum dropdowns for value fields
 *   - Endpoint auto-populated from command selection
 *   - Trace log viewer
 */

import { state } from '../state.js';

let cachedActuators = [];
let cachedAttributes = [];
let cachedAllDevices = [];
let currentSourceIeee = null;

// Form state
let conditionRows = [];
let conditionIdCounter = 0;
let prereqRows = [];
let prereqIdCounter = 0;

// Edit mode: null = creating, string = rule_id being edited
let editingRuleId = null;

const OP_LABELS = { 'eq':'=','neq':'≠','gt':'>','lt':'<','gte':'>=','lte':'<=' };
const OP_TEXT = { 'eq':'equals','neq':'not equal','gt':'greater than','lt':'less than','gte':'≥','lte':'≤' };

// ============================================================================
// MAIN RENDERER
// ============================================================================

export function renderAutomationTab(device) {
    currentSourceIeee = device.ieee;
    return `
    <div id="automation-tab-content">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <span class="text-muted small">Direct ZigBee triggers — no MQTT delay.</span>
            <div>
                <button class="btn btn-sm btn-outline-secondary me-1" onclick="window._autoTrace()"><i class="fas fa-search"></i> Trace</button>
                <button class="btn btn-sm btn-success" onclick="window._autoShowForm()"><i class="fas fa-plus"></i> Add Rule</button>
            </div>
        </div>
        <div id="automation-add-form" class="card mb-3" style="display:none;"></div>
        <div id="automation-trace-panel" class="card mb-3" style="display:none;">
            <div class="card-header bg-dark text-white d-flex justify-content-between align-items-center py-1">
                <strong><i class="fas fa-search"></i> Trace Log</strong>
                <div class="d-flex align-items-center gap-2">
                    <select class="form-select form-select-sm bg-dark text-white border-secondary" id="trace-filter" style="width:auto;max-width:220px;font-size:0.75rem" onchange="window._autoRefreshTrace()">
                        <option value="">All rules</option>
                    </select>
                    <button class="btn btn-sm btn-outline-light" onclick="window._autoRefreshTrace()"><i class="fas fa-sync-alt"></i></button>
                    <button class="btn btn-sm btn-outline-light" onclick="document.getElementById('automation-trace-panel').style.display='none'"><i class="fas fa-times"></i></button>
                </div>
            </div>
            <div class="card-body p-0" style="max-height:400px;overflow-y:auto">
                <div id="automation-trace-content" class="font-monospace small p-2">Loading...</div>
            </div>
        </div>
        <div id="automation-rules-list">
            <div class="text-center text-muted py-3"><i class="fas fa-spinner fa-spin"></i> Loading...</div>
        </div>
    </div>`;
}

// ============================================================================
// INIT
// ============================================================================

export async function initAutomationTab(ieee) {
    currentSourceIeee = ieee;
    try {
        const [rulesRes, attrsRes, actRes, devRes] = await Promise.all([
            fetch(`/api/automations?source_ieee=${encodeURIComponent(ieee)}`),
            fetch(`/api/automations/device/${encodeURIComponent(ieee)}/attributes`),
            fetch('/api/automations/actuators'),
            fetch('/api/automations/devices'),
        ]);
        const rules = await rulesRes.json();
        cachedAttributes = await attrsRes.json();
        cachedActuators = await actRes.json();
        cachedAllDevices = await devRes.json();
        renderRulesList(rules);
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
    if (!rules || rules.length === 0) {
        el.innerHTML = `<div class="text-center text-muted py-4">
            <i class="fas fa-robot fa-2x mb-2 d-block opacity-50"></i>
            No rules yet. Click <strong>Add Rule</strong>.</div>`;
        return;
    }

    let html = '';
    rules.forEach(rule => {
        const conds = rule.conditions || [];
        const prereqs = rule.prerequisites || [];
        const a = rule.action || {};
        const enabled = rule.enabled !== false;
        const tgt = rule.target_name || rule.target_ieee;
        const valD = a.value != null ? ` = ${a.value}` : '';
        const epD = a.endpoint_id ? ` EP${a.endpoint_id}` : '';
        const delayD = (a.delay && a.delay > 0) ? `<span class="badge bg-warning text-dark me-1">⏱${a.delay}s</span>` : '';
        const name = rule.name ? `<strong class="me-2">${rule.name}</strong>` : '';

        let condH = '';
        conds.forEach((c, i) => {
            const pfx = i === 0 ? '<strong class="text-primary">IF</strong>' : '<strong class="text-warning">AND</strong>';
            const sus = c.sustain ? `<span class="badge bg-info text-dark ms-1">⏱${c.sustain}s</span>` : '';
            condH += `<div class="small">${pfx} <code>${c.attribute}</code> <span class="badge bg-light text-dark border">${OP_LABELS[c.operator]||c.operator}</span> <code>${c.value}</code>${sus}</div>`;
        });

        let preH = '';
        prereqs.forEach(p => {
            const pn = p.device_name || p.ieee;
            preH += `<div class="small"><strong class="text-info">CHECK</strong> <span title="${p.ieee}">${pn}</span> <code>${p.attribute}</code> <span class="badge bg-light text-dark border">${OP_LABELS[p.operator]||p.operator}</span> <code>${p.value}</code></div>`;
        });

        html += `
        <div class="card mb-2 ${enabled ? '' : 'opacity-50'}">
            <div class="card-body py-2 px-3">
                <div class="d-flex justify-content-between align-items-start">
                    <div class="flex-grow-1">
                        <div class="mb-1">${name}<code class="text-muted small">${rule.id}</code></div>
                        ${condH}${preH}
                        <div class="small mt-1">
                            <strong class="text-success">THEN</strong> ${delayD}
                            <span class="badge bg-info text-dark">${a.command}${valD}</span>
                            <i class="fas fa-arrow-right text-muted mx-1"></i>
                            <span title="${rule.target_ieee}">${tgt}${epD}</span>
                        </div>
                    </div>
                    <div class="d-flex gap-1 ms-2">
                        <span class="badge bg-secondary" title="Cooldown">${rule.cooldown||5}s</span>
                        <button class="btn btn-sm btn-outline-secondary" onclick="window._autoTraceRule('${rule.id}')" title="Trace this rule"><i class="fas fa-search"></i></button>
                        <button class="btn btn-sm btn-outline-primary" onclick="window._autoEdit('${rule.id}')" title="Edit"><i class="fas fa-edit"></i></button>
                        <button class="btn btn-sm ${enabled?'btn-outline-success':'btn-outline-secondary'}" onclick="window._autoToggle('${rule.id}')" title="${enabled?'Disable':'Enable'}"><i class="fas fa-${enabled?'toggle-on':'toggle-off'}"></i></button>
                        <button class="btn btn-sm btn-outline-danger" onclick="window._autoDelete('${rule.id}')" title="Delete"><i class="fas fa-trash"></i></button>
                    </div>
                </div>
            </div>
        </div>`;
    });
    el.innerHTML = html;
}

// ============================================================================
// FORM RENDERING
// ============================================================================

function renderForm(rule) {
    const isEdit = !!rule;
    editingRuleId = isEdit ? rule.id : null;
    const a = isEdit ? (rule.action || {}) : {};

    const formEl = document.getElementById('automation-add-form');
    if (!formEl) return;

    formEl.innerHTML = `
    <div class="card-header bg-light d-flex justify-content-between align-items-center">
        <strong><i class="fas fa-${isEdit ? 'edit' : 'bolt'}"></i> ${isEdit ? 'Edit' : 'New'} Automation Rule</strong>
        <button class="btn btn-sm btn-outline-secondary" onclick="window._autoHideForm()"><i class="fas fa-times"></i></button>
    </div>
    <div class="card-body">
        <div class="mb-3">
            <label class="form-label small text-muted mb-0">Rule Name (optional)</label>
            <input type="text" class="form-control form-control-sm" id="auto-name" placeholder="e.g. Kitchen motion → light on" value="${isEdit ? (rule.name||'') : ''}">
        </div>

        <div class="mb-3">
            <div class="d-flex justify-content-between align-items-center mb-2">
                <label class="form-label fw-bold small mb-0">1. When ALL of these are true...</label>
                <button class="btn btn-sm btn-outline-primary" onclick="window._autoAddCond()"><i class="fas fa-plus"></i> Condition</button>
            </div>
            <div id="conditions-builder"></div>
        </div>

        <div class="mb-3">
            <div class="d-flex justify-content-between align-items-center mb-2">
                <label class="form-label fw-bold small mb-0">2. When ALL conditional states are met... <span class="text-muted fw-normal">(optional)</span></label>
                <button class="btn btn-sm btn-outline-info" onclick="window._autoAddPrereq()"><i class="fas fa-plus"></i> Check</button>
            </div>
            <div id="prereqs-builder"></div>
            <div class="form-text small text-muted">Check other devices' current state before firing.</div>
        </div>

        <div class="mb-3">
            <label class="form-label fw-bold small">3. Then send command to...</label>
            <div class="row g-2">
                <div class="col-md-5">
                    <select class="form-select form-select-sm" id="auto-target" onchange="window._autoTargetChange(this)">
                        <option value="">Select target...</option>
                    </select>
                </div>
                <div class="col-md-4">
                    <select class="form-select form-select-sm" id="auto-command" onchange="window._autoCmdChange(this)">
                        <option value="">Select command...</option>
                    </select>
                </div>
                <div class="col-md-3">
                    <input type="text" class="form-control form-control-sm" id="auto-command-value" placeholder="Value (opt)" value="${isEdit && a.value != null ? a.value : ''}">
                </div>
            </div>
        </div>

        <div class="row g-2 mb-3">
            <div class="col-md-3">
                <label class="form-label small text-muted mb-0">Delay (opt, seconds)</label>
                <input type="number" class="form-control form-control-sm" id="auto-delay" placeholder="0" min="0" max="3600" value="${isEdit && a.delay ? a.delay : ''}">
            </div>
            <div class="col-md-3">
                <label class="form-label small text-muted mb-0">Cooldown (seconds)</label>
                <input type="number" class="form-control form-control-sm" id="auto-cooldown" value="${isEdit ? (rule.cooldown||5) : 5}" min="0" max="3600">
            </div>
            <div class="col-md-3">
                <label class="form-label small text-muted mb-0">Endpoint (auto)</label>
                <input type="number" class="form-control form-control-sm" id="auto-endpoint" placeholder="Auto" readonly value="${isEdit && a.endpoint_id ? a.endpoint_id : ''}">
            </div>
            <div class="col-md-3 d-flex align-items-end">
                <button class="btn btn-primary btn-sm w-100" onclick="window._autoSave()">
                    <i class="fas fa-save"></i> ${isEdit ? 'Update' : 'Save'} Rule
                </button>
            </div>
        </div>
    </div>`;

    formEl.style.display = 'block';

    // Populate target select
    const tSel = document.getElementById('auto-target');
    cachedActuators.forEach(d => {
        if (d.ieee === currentSourceIeee) return;
        const opt = document.createElement('option');
        opt.value = d.ieee;
        opt.dataset.commands = JSON.stringify(d.commands);
        opt.textContent = `${d.friendly_name} (${d.model})`;
        if (isEdit && d.ieee === rule.target_ieee) opt.selected = true;
        tSel.appendChild(opt);
    });

    // If editing, populate commands for the selected target
    if (isEdit && rule.target_ieee) {
        const tOpt = tSel.options[tSel.selectedIndex];
        if (tOpt && tOpt.dataset.commands) {
            _populateCommands(JSON.parse(tOpt.dataset.commands), a.command, a.endpoint_id);
        }
    }

    // Build condition rows
    conditionRows = [];
    conditionIdCounter = 0;
    if (isEdit && rule.conditions && rule.conditions.length > 0) {
        rule.conditions.forEach(() => conditionRows.push(conditionIdCounter++));
    } else {
        conditionRows.push(conditionIdCounter++);
    }
    _refreshConditions();

    // Restore condition values if editing
    if (isEdit && rule.conditions) {
        setTimeout(() => {
            rule.conditions.forEach((c, i) => {
                const rowId = conditionRows[i];
                if (rowId === undefined) return;
                _setConditionValues(rowId, c);
            });
        }, 50);
    }

    // Build prereq rows
    prereqRows = [];
    prereqIdCounter = 0;
    if (isEdit && rule.prerequisites && rule.prerequisites.length > 0) {
        rule.prerequisites.forEach(() => prereqRows.push(prereqIdCounter++));
    }
    _refreshPrereqs();

    // Restore prereq values if editing
    if (isEdit && rule.prerequisites) {
        setTimeout(() => {
            rule.prerequisites.forEach((p, i) => {
                const rowId = prereqRows[i];
                if (rowId === undefined) return;
                _setPrereqValues(rowId, p);
            });
        }, 100);
    }
}

// ============================================================================
// VALUE INPUT HELPER — dropdown for bool/enum, text input otherwise
// ============================================================================

function makeValueInput(className, rowId, valueOptions, currentValue) {
    if (valueOptions && valueOptions.length > 0) {
        let html = `<select class="form-select form-select-sm ${className}" data-row="${rowId}">`;
        valueOptions.forEach(v => {
            const sel = (currentValue !== undefined && String(currentValue).toLowerCase() === String(v).toLowerCase()) ? 'selected' : '';
            html += `<option value="${v}" ${sel}>${v}</option>`;
        });
        html += `</select>`;
        return html;
    }
    return `<input type="text" class="form-control form-control-sm ${className}" data-row="${rowId}" placeholder="Value" value="${currentValue !== undefined ? currentValue : ''}">`;
}

// ============================================================================
// CONDITION BUILDER
// ============================================================================

function _renderCondRow(rowId) {
    const opts = cachedAttributes.map(a => {
        const icon = a.type === 'boolean' ? '⚡' : a.type === 'float' ? '📊' : '📈';
        return `<option value="${a.attribute}"
            data-type="${a.type}"
            data-operators='${JSON.stringify(a.operators)}'
            data-current="${a.current_value}"
            data-valueopts='${JSON.stringify(a.value_options || [])}'>
            ${icon} ${a.attribute} (${a.current_value})</option>`;
    }).join('');

    return `
    <div class="row g-1 mb-1 align-items-center" id="cond-row-${rowId}">
        <div class="col-auto"><span class="badge ${conditionRows.indexOf(rowId)===0?'bg-primary':'bg-warning text-dark'} small">${conditionRows.indexOf(rowId)===0?'IF':'AND'}</span></div>
        <div class="col"><select class="form-select form-select-sm cond-attribute" data-row="${rowId}" onchange="window._autoCondAttr(${rowId},this)"><option value="">Attribute...</option>${opts}</select></div>
        <div class="col-auto"><select class="form-select form-select-sm cond-operator" data-row="${rowId}" style="width:90px"><option value="">Op...</option></select></div>
        <div class="col" id="cond-value-wrap-${rowId}"><input type="text" class="form-control form-control-sm cond-value" data-row="${rowId}" placeholder="Value"></div>
        <div class="col-auto" style="width:80px"><input type="number" class="form-control form-control-sm cond-sustain" data-row="${rowId}" placeholder="⏱ sec" min="0" title="Optional: hold for N seconds"></div>
        <div class="col-auto">${conditionRows.indexOf(rowId)>0 ? `<button class="btn btn-sm btn-outline-danger" onclick="window._autoRemoveCond(${rowId})"><i class="fas fa-times"></i></button>` : '<div style="width:31px"></div>'}</div>
    </div>`;
}

function _refreshConditions() {
    const el = document.getElementById('conditions-builder');
    if (!el) return;
    el.innerHTML = conditionRows.map(id => _renderCondRow(id)).join('');
}

function _setConditionValues(rowId, cond) {
    const attrSel = document.querySelector(`#cond-row-${rowId} .cond-attribute`);
    if (!attrSel) return;
    attrSel.value = cond.attribute;
    // Trigger attribute change to populate operators + value widget
    window._autoCondAttr(rowId, attrSel);
    // Now set operator and value
    setTimeout(() => {
        const opSel = document.querySelector(`#cond-row-${rowId} .cond-operator`);
        if (opSel) opSel.value = cond.operator;
        const valEl = document.querySelector(`#cond-value-wrap-${rowId} .cond-value`);
        if (valEl) valEl.value = String(cond.value);
        const susEl = document.querySelector(`#cond-row-${rowId} .cond-sustain`);
        if (susEl && cond.sustain) susEl.value = cond.sustain;
    }, 20);
}

// ============================================================================
// PREREQUISITE BUILDER
// ============================================================================

function _renderPrereqRow(rowId) {
    const devOpts = cachedAllDevices
        .filter(d => d.ieee !== currentSourceIeee)
        .map(d => `<option value="${d.ieee}" data-keys='${JSON.stringify(d.state_keys)}'>${d.friendly_name}</option>`)
        .join('');

    return `
    <div class="row g-1 mb-1 align-items-center" id="prereq-row-${rowId}">
        <div class="col-auto"><span class="badge bg-info text-dark small">CHECK</span></div>
        <div class="col"><select class="form-select form-select-sm prereq-device" data-row="${rowId}" onchange="window._autoPrereqDev(${rowId},this)"><option value="">Device...</option>${devOpts}</select></div>
        <div class="col"><select class="form-select form-select-sm prereq-attribute" data-row="${rowId}" onchange="window._autoPrereqAttr(${rowId},this)"><option value="">Attr...</option></select></div>
        <div class="col-auto"><select class="form-select form-select-sm prereq-operator" data-row="${rowId}" style="width:80px">${Object.entries(OP_LABELS).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></div>
        <div class="col" id="prereq-value-wrap-${rowId}"><input type="text" class="form-control form-control-sm prereq-value" data-row="${rowId}" placeholder="Value"></div>
        <div class="col-auto"><button class="btn btn-sm btn-outline-danger" onclick="window._autoRemovePrereq(${rowId})"><i class="fas fa-times"></i></button></div>
    </div>`;
}

function _refreshPrereqs() {
    const el = document.getElementById('prereqs-builder');
    if (!el) return;
    el.innerHTML = prereqRows.map(id => _renderPrereqRow(id)).join('');
}

function _setPrereqValues(rowId, prereq) {
    const devSel = document.querySelector(`#prereq-row-${rowId} .prereq-device`);
    if (!devSel) return;
    devSel.value = prereq.ieee;
    window._autoPrereqDev(rowId, devSel);
    setTimeout(() => {
        const attrSel = document.querySelector(`#prereq-row-${rowId} .prereq-attribute`);
        if (attrSel) {
            attrSel.value = prereq.attribute;
            window._autoPrereqAttr(rowId, attrSel);
        }
        setTimeout(() => {
            const opSel = document.querySelector(`#prereq-row-${rowId} .prereq-operator`);
            if (opSel) opSel.value = prereq.operator;
            const valEl = document.querySelector(`#prereq-value-wrap-${rowId} .prereq-value`);
            if (valEl) valEl.value = String(prereq.value);
        }, 20);
    }, 100);
}

// ============================================================================
// COMMAND HELPERS
// ============================================================================

function _populateCommands(commands, selectedCmd, selectedEp) {
    const sel = document.getElementById('auto-command');
    if (!sel) return;
    sel.innerHTML = '<option value="">Select command...</option>';
    (commands || []).forEach(cmd => {
        const epStr = cmd.endpoint_id ? ` (EP${cmd.endpoint_id})` : '';
        const opt = document.createElement('option');
        opt.value = cmd.command;
        opt.dataset.ep = cmd.endpoint_id || '';
        opt.dataset.type = cmd.type || 'button';
        opt.textContent = `${cmd.label || cmd.command}${epStr}`;
        if (selectedCmd === cmd.command && (!selectedEp || selectedEp == cmd.endpoint_id)) opt.selected = true;
        sel.appendChild(opt);
    });
    // Auto-populate endpoint from selected
    if (selectedEp) {
        const epInput = document.getElementById('auto-endpoint');
        if (epInput) epInput.value = selectedEp;
    }
}

// ============================================================================
// WINDOW HANDLERS
// ============================================================================

// --- Conditions ---
window._autoCondAttr = function(rowId, sel) {
    const opt = sel.options[sel.selectedIndex];
    if (!opt || !opt.value) return;
    const type = opt.dataset.type;
    const ops = JSON.parse(opt.dataset.operators || '["eq","neq"]');
    const cur = opt.dataset.current;
    const valueOpts = JSON.parse(opt.dataset.valueopts || '[]');

    // Operators
    const opSel = document.querySelector(`#cond-row-${rowId} .cond-operator`);
    if (opSel) opSel.innerHTML = ops.map(o => `<option value="${o}">${OP_LABELS[o]} ${OP_TEXT[o]}</option>`).join('');

    // Value widget: dropdown for bool/enum, text for numeric
    const wrap = document.getElementById(`cond-value-wrap-${rowId}`);
    if (wrap) {
        const defaultVal = (type === 'boolean') ? String(cur).toLowerCase() : '';
        wrap.innerHTML = makeValueInput('cond-value', rowId, valueOpts, defaultVal);
    }
};

window._autoAddCond = function() {
    if (conditionRows.length >= 5) return alert('Maximum 5 conditions.');
    conditionRows.push(conditionIdCounter++);
    _refreshConditions();
};
window._autoRemoveCond = function(id) {
    conditionRows = conditionRows.filter(r => r !== id);
    _refreshConditions();
};

// --- Prerequisites ---
window._autoPrereqDev = async function(rowId, sel) {
    const ieee = sel.value;
    const attrSel = document.querySelector(`#prereq-row-${rowId} .prereq-attribute`);
    if (!attrSel || !ieee) return;
    attrSel.innerHTML = '<option value="">Loading...</option>';

    try {
        const res = await fetch(`/api/automations/device/${encodeURIComponent(ieee)}/state`);
        const data = await res.json();
        const attrs = data.attributes || [];
        attrSel.innerHTML = '<option value="">Attribute...</option>';
        attrs.forEach(a => {
            const opt = document.createElement('option');
            opt.value = a.attribute;
            opt.dataset.current = a.current_value;
            opt.dataset.valueopts = JSON.stringify(a.value_options || []);
            opt.dataset.type = a.type;
            opt.textContent = `${a.attribute} (${a.current_value})`;
            attrSel.appendChild(opt);
        });
    } catch (e) {
        const dev = cachedAllDevices.find(d => d.ieee === ieee);
        attrSel.innerHTML = '<option value="">Attribute...</option>';
        if (dev) dev.state_keys.forEach(k => { attrSel.innerHTML += `<option value="${k}">${k}</option>`; });
    }
};

window._autoPrereqAttr = function(rowId, sel) {
    const opt = sel.options[sel.selectedIndex];
    if (!opt || !opt.value) return;
    const valueOpts = JSON.parse(opt.dataset.valueopts || '[]');
    const cur = opt.dataset.current;
    const type = opt.dataset.type;

    const wrap = document.getElementById(`prereq-value-wrap-${rowId}`);
    if (wrap) {
        const defaultVal = (type === 'boolean' || valueOpts.length > 0) ? String(cur) : '';
        wrap.innerHTML = makeValueInput('prereq-value', rowId, valueOpts, defaultVal);
    }
};

window._autoAddPrereq = function() {
    if (prereqRows.length >= 5) return alert('Maximum 5 prerequisites.');
    prereqRows.push(prereqIdCounter++);
    _refreshPrereqs();
};
window._autoRemovePrereq = function(id) {
    prereqRows = prereqRows.filter(r => r !== id);
    _refreshPrereqs();
};

// --- Target/Command ---
window._autoTargetChange = function(sel) {
    const opt = sel.options[sel.selectedIndex];
    if (!opt || !opt.value) return;
    _populateCommands(JSON.parse(opt.dataset.commands || '[]'));
};

window._autoCmdChange = function(sel) {
    const opt = sel.options[sel.selectedIndex];
    if (!opt) return;
    const ep = opt.dataset.ep;
    const epInput = document.getElementById('auto-endpoint');
    if (epInput) epInput.value = ep || '';
};

// --- Form show/hide ---
window._autoShowForm = function() { renderForm(null); };
window._autoHideForm = function() {
    document.getElementById('automation-add-form').style.display = 'none';
    editingRuleId = null;
};

// --- Edit ---
window._autoEdit = async function(ruleId) {
    try {
        const res = await fetch(`/api/automations/rule/${ruleId}`);
        if (!res.ok) return alert('Failed to load rule');
        const rule = await res.json();
        renderForm(rule);
        // Scroll form into view
        document.getElementById('automation-add-form')?.scrollIntoView({ behavior: 'smooth' });
    } catch (e) { alert(e.message); }
};

// --- Trace ---
window._autoTrace = async function() {
    document.getElementById('automation-trace-panel').style.display = 'block';
    // Populate filter dropdown with current rules
    const filterSel = document.getElementById('trace-filter');
    if (filterSel) {
        const current = filterSel.value;
        filterSel.innerHTML = '<option value="">All rules</option>';
        try {
            const res = await fetch(`/api/automations?source_ieee=${encodeURIComponent(currentSourceIeee)}`);
            const rules = await res.json();
            rules.forEach(r => {
                const label = r.name ? `${r.name} (${r.id})` : r.id;
                filterSel.innerHTML += `<option value="${r.id}">${label}</option>`;
            });
        } catch (e) { /* ignore */ }
        // Also add a "-" option for system-level entries
        filterSel.innerHTML += `<option value="-">System (entry/lookup)</option>`;
        filterSel.value = current || '';
    }
    _loadTrace();
};
window._autoRefreshTrace = _loadTrace;

window._autoTraceRule = async function(ruleId) {
    document.getElementById('automation-trace-panel').style.display = 'block';
    // Populate filter and pre-select the rule
    await window._autoTrace();
    const filterSel = document.getElementById('trace-filter');
    if (filterSel) filterSel.value = ruleId;
    _loadTrace();
};

// ============================================================================
// SAVE (CREATE or UPDATE)
// ============================================================================

window._autoSave = async function() {
    // Gather conditions
    const conditions = [];
    let valid = true;
    conditionRows.forEach(rowId => {
        const attr = document.querySelector(`#cond-row-${rowId} .cond-attribute`)?.value;
        const op = document.querySelector(`#cond-row-${rowId} .cond-operator`)?.value;
        const valEl = document.querySelector(`#cond-value-wrap-${rowId} .cond-value`);
        const rawVal = valEl?.value;
        const susVal = document.querySelector(`#cond-row-${rowId} .cond-sustain`)?.value;

        if (!attr || !op || rawVal === undefined || rawVal === '') { valid = false; return; }

        // Type-aware coercion: match threshold type to attribute's actual type
        const attrInfo = cachedAttributes.find(a => a.attribute === attr);
        let value;
        if (attrInfo) {
            if (attrInfo.type === 'boolean') {
                value = _coerce(rawVal); // true/false
            } else if (attrInfo.type === 'float') {
                value = parseFloat(rawVal);
                if (isNaN(value)) { valid = false; return; }
            } else if (attrInfo.type === 'integer') {
                value = parseInt(rawVal, 10);
                if (isNaN(value)) value = parseFloat(rawVal);
                if (isNaN(value)) { valid = false; return; }
            } else {
                // String type — keep as string, don't coerce ON/OFF to bool
                value = String(rawVal).trim();
            }
        } else {
            value = _coerce(rawVal);
        }
        const cond = { attribute: attr, operator: op, value };
        // Only include sustain if explicitly set > 0
        if (susVal && parseInt(susVal) > 0) cond.sustain = parseInt(susVal);
        conditions.push(cond);
    });
    if (!valid || conditions.length === 0) return alert('Fill all condition fields.');

    // Gather prerequisites
    const prerequisites = [];
    prereqRows.forEach(rowId => {
        const ieee = document.querySelector(`#prereq-row-${rowId} .prereq-device`)?.value;
        const attr = document.querySelector(`#prereq-row-${rowId} .prereq-attribute`)?.value;
        const op = document.querySelector(`#prereq-row-${rowId} .prereq-operator`)?.value;
        const valEl = document.querySelector(`#prereq-value-wrap-${rowId} .prereq-value`);
        const rawVal = valEl?.value;
        if (!ieee || !attr || !op || rawVal === undefined || rawVal === '') return;
        prerequisites.push({ ieee, attribute: attr, operator: op, value: _coerce(rawVal) });
    });

    const name = document.getElementById('auto-name')?.value || '';
    const targetIeee = document.getElementById('auto-target')?.value;
    const command = document.getElementById('auto-command')?.value;
    const cmdValRaw = document.getElementById('auto-command-value')?.value;
    const delayRaw = document.getElementById('auto-delay')?.value;
    const cooldown = parseInt(document.getElementById('auto-cooldown')?.value) || 5;
    const epRaw = document.getElementById('auto-endpoint')?.value;

    if (!targetIeee || !command) return alert('Select target device and command.');

    const commandValue = (cmdValRaw !== null && cmdValRaw !== '') ? _coerce(cmdValRaw) : null;
    const endpointId = epRaw ? parseInt(epRaw) : null;
    const delay = (delayRaw && parseInt(delayRaw) > 0) ? parseInt(delayRaw) : null;

    const body = {
        name,
        source_ieee: currentSourceIeee,
        conditions,
        prerequisites,
        target_ieee: targetIeee,
        command,
        command_value: commandValue,
        endpoint_id: endpointId,
        delay,
        cooldown,
        enabled: true,
    };

    try {
        let res;
        if (editingRuleId) {
            // UPDATE
            res = await fetch(`/api/automations/${editingRuleId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
        } else {
            // CREATE
            res = await fetch('/api/automations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
        }
        const data = await res.json();
        if (res.ok && data.success) {
            window._autoHideForm();
            await _refreshRules();
        } else {
            alert('Failed: ' + (data.detail || data.error || 'Unknown'));
        }
    } catch (e) { alert(e.message); }
};

// ============================================================================
// TOGGLE / DELETE
// ============================================================================

window._autoToggle = async function(id) {
    try {
        const r = await fetch(`/api/automations/${id}/toggle`, { method: 'PATCH' });
        if (r.ok) await _refreshRules();
    } catch (e) { alert(e.message); }
};

window._autoDelete = async function(id) {
    if (!confirm('Delete this rule?')) return;
    try {
        const r = await fetch(`/api/automations/${id}`, { method: 'DELETE' });
        if (r.ok) await _refreshRules();
    } catch (e) { alert(e.message); }
};

// ============================================================================
// HELPERS
// ============================================================================

function _coerce(val) {
    if (typeof val !== 'string') return val;
    const trimmed = val.trim();
    const lower = trimmed.toLowerCase();
    // Only convert literal true/false to boolean — NOT on/off
    // on/off are device state strings and must stay as strings
    if (lower === 'true') return true;
    if (lower === 'false') return false;
    if (!isNaN(trimmed) && trimmed !== '') return parseFloat(trimmed);
    return trimmed;
}

async function _refreshRules() {
    if (!currentSourceIeee) return;
    try {
        const res = await fetch(`/api/automations?source_ieee=${encodeURIComponent(currentSourceIeee)}`);
        renderRulesList(await res.json());
    } catch (e) { console.error(e); }
}

async function _loadTrace() {
    const el = document.getElementById('automation-trace-content');
    if (!el) return;
    const filterVal = document.getElementById('trace-filter')?.value || '';
    const url = filterVal ? `/api/automations/trace?rule_id=${encodeURIComponent(filterVal)}` : '/api/automations/trace';
    try {
        const res = await fetch(url);
        const entries = await res.json();
        if (!entries || entries.length === 0) {
            el.innerHTML = '<div class="text-muted p-2">No trace entries yet.</div>';
            return;
        }
        let html = '';
        [...entries].reverse().forEach(e => {
            const ts = new Date(e.timestamp * 1000).toLocaleTimeString();
            const r = e.result || '';
            let cl = 'text-muted';
            if (r === 'SUCCESS' || r === 'FIRING') cl = 'text-success';
            else if (r.includes('FAIL') || r.includes('ERROR') || r === 'EXCEPTION' || r.includes('MISSING')) cl = 'text-danger';
            else if (r === 'BLOCKED' || r === 'SUSTAIN_WAIT') cl = 'text-warning';
            else if (r === 'PREREQ_FAIL') cl = 'text-info';
            else if (r === 'EVALUATING' || r === 'CALLING' || r === 'WAITING') cl = 'text-info';

            html += `<div class="border-bottom py-1 ${cl}"><span class="text-muted">${ts}</span> <span class="badge bg-dark">${e.phase||''}</span> <span class="badge bg-secondary">${r}</span> `;
            if (e.rule_id && e.rule_id !== '-') html += `<code>${e.rule_id}</code> `;
            html += e.message || '';

            if (e.conditions?.length) {
                html += '<div class="ms-3 mt-1">';
                e.conditions.forEach(c => {
                    const cc = c.result==='PASS'?'text-success':c.result==='SUSTAIN_WAIT'?'text-warning':'text-danger';
                    html += `<div class="${cc}">#${c.index} ${c.attribute} ${c.operator||''} ${c.threshold_raw||c.threshold||'?'} → actual: ${c.actual_raw||'?'} (${c.actual_type||'?'}) [${c.result}]`;
                    if (c.sustain_elapsed != null) html += ` ⏱${c.sustain_elapsed}s`;
                    if (c.value_source) html += ` src:${c.value_source}`;
                    if (c.reason) html += ` — ${c.reason}`;
                    html += '</div>';
                });
                html += '</div>';
            }
            if (e.prerequisites?.length) {
                html += '<div class="ms-3 mt-1">';
                e.prerequisites.forEach(p => {
                    const pc = p.result==='PASS'?'text-success':'text-danger';
                    html += `<div class="${pc}">CHECK ${p.device_name||p.ieee} ${p.attribute} ${p.operator||''} ${p.threshold_raw||'?'}`;
                    if (p.threshold_normalised) html += ` [norm:${p.threshold_normalised}]`;
                    html += ` → actual: ${p.actual_raw||'?'}`;
                    if (p.actual_normalised) html += ` [norm:${p.actual_normalised}]`;
                    html += ` [${p.result}]`;
                    if (p.reason) html += ` — ${p.reason}`;
                    html += '</div>';
                });
                html += '</div>';
            }
            if (e.error) html += `<div class="ms-3 text-danger">${e.error}</div>`;
            html += '</div>';
        });
        el.innerHTML = html;
    } catch (err) { el.innerHTML = `<div class="text-danger">${err.message}</div>`; }
}