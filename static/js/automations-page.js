/**
 * Automations Page (Global Tab)
 * Location: static/js/automations-page.js
 *
 * Shows ALL automation rules across all devices with inline edit.
 * Reuses the existing modal/automation.js form builder via shared DOM IDs.
 * When editing, the source device is locked to the rule's source_ieee.
 */

import { state } from './state.js';
import { initAutomationTab } from './modal/automation.js';
import { initAIAutomations, renderAIPanel } from './ai-automations.js';
import { DEVICE_ICON, DEVICE_LABEL, deviceType } from './automation-humanize.js';


const log = zmmLog('automations-page');

const OP = { eq:'=', neq:'≠', gt:'>', lt:'<', gte:'≥', lte:'≤', in:'∈', nin:'∉', changed:'Δ' };

// ============================================================================
// HUMANIZATION — turn raw rule JSON into plain-English, device-aware phrasing.
// Device type comes from capability_list on the main device list (state.devices);
// falls back to name/model/state-key heuristics when capabilities are absent.
// ============================================================================

// DEVICE_ICON / DEVICE_LABEL / deviceType are imported from automation-humanize.js
const DAY_NAMES = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];

function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, c => (
        { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]));
}

// Resolve a device/group name + semantic type for an ieee (or group:N).
function _resolve(ieee) {
    if (ieee === '__time__') return { name:'Time / Alarm', type:'time' };
    if (typeof ieee === 'string' && ieee.startsWith('group:')) {
        const g = devMapCache[ieee];
        const model = (g && g.model || '').toLowerCase();
        const type = model.includes('cover') ? 'cover'
                   : model.includes('lock') ? 'unknown'
                   : 'light';   // most groups are lights/switches
        return { name: g ? String(g.friendly_name || ieee).replace(/^🔗\s*/, '') : ieee, type };
    }
    const dev = devMapCache[ieee];
    return { name: dev ? dev.friendly_name : ieee, type: deviceType(ieee, dev) };
}

const _icon = t => `<i class="fas ${DEVICE_ICON[t] || DEVICE_ICON.unknown}"></i>`;
const _devSpan = ieee => `<span class="dev">${_esc(_resolve(ieee).name)}</span>`;

function _daySpan(days) {
    if (!days || days.length === 7) return 'every day';
    const s = [...days].sort((a,b) => a-b).join(',');
    if (s === '0,1,2,3,4') return 'on weekdays';
    if (s === '5,6') return 'at weekends';
    return 'on ' + days.map(d => DAY_NAMES[d]).join(', ');
}
const _timePhrase = (a,b) => `between <b>${_esc(a)}</b> and <b>${_esc(b)}</b>`;

// Core semantic map: attribute (+ device type hint) → verb phrase.
// Outlet suffixes are only surfaced for endpoint 2+ — "outlet 1" is the default
// (or only) endpoint on nearly every device, so annotating it is just noise.
function _attrVerb(type, attr, op, val) {
    const base = (attr || '').replace(/_\d+$/, '');   // strip endpoint suffix (_1, _2)
    const epM = (attr || '').match(/_(\d+)$/);
    const epTxt = (epM && +epM[1] >= 2) ? ` (outlet ${epM[1]})` : '';
    const truthy = v => v === true || v === 'true' || String(v).toUpperCase() === 'ON';
    if (type === 'contact') {
        if (base === 'is_open')   return truthy(val) ? 'opens'   : 'is shut';
        if (base === 'is_closed') return truthy(val) ? 'closes'  : 'is open';
        if (base === 'contact')   return truthy(val) ? 'closes'  : 'opens';
    }
    if (base === 'action') {
        const m = { single:'is single-pressed', double:'is double-pressed', triple:'is triple-pressed',
                    hold:'is held', release:'is released', long:'is long-pressed' };
        return m[val] || `sends “${_esc(val)}”`;
    }
    if (/^(on|state)$/.test(base)) return (truthy(val) ? 'is on' : 'is off') + epTxt;
    const OPW = { eq:'is', neq:'is not', gt:'is above', lt:'is below', gte:'is at least',
                  lte:'is at most', in:'is one of', nin:'is not one of' };
    const dispVal = Array.isArray(val) ? val.join(', ') : val;
    return `${OPW[op] || op} ${_esc(dispVal)}${epTxt}`;
}

// A trigger is the source device's first condition (or a schedule).
// The text is a bare clause — the "When" label in the flow supplies the verb,
// so we don't repeat it here ("When" + "Door opens", not "When When Door opens").
function _triggerPhrase(rule) {
    const src = _resolve(rule.source_ieee);
    const c = (rule.conditions || [])[0];
    if (!c) return { icon: src.type, text: `${_devSpan(rule.source_ieee)} changes`, raw:'' };
    if (c.type === 'time_window')
        return { icon:'time', text:`it's ${_timePhrase(c.time_from, c.time_to)}, ${_daySpan(c.days)}`,
                 raw:`time_window ${c.time_from}–${c.time_to}` };
    if (c.type === 'time')
        return { icon:'time', text:`the time is <b>${_esc(c.at)}</b>, ${_daySpan(c.days)}`, raw:`time ${c.at}` };
    if (c.type === 'sun')
        return { icon:'time', text:`🌅 it's between ${_esc(c.from)} and ${_esc(c.to)}`, raw:`sun ${c.from}→${c.to}` };
    return { icon: src.type,
             text: `${_devSpan(rule.source_ieee)} ${_attrVerb(src.type, c.attribute, c.operator, c.value)}`,
             raw: `${_esc(c.attribute)} ${c.operator} ${_esc(c.value)}` };
}

// A prerequisite / extra condition → "ONLY IF …" phrase.
function _condPhrase(p) {
    const neg = p.negate ? '<span class="neg">NOT</span>' : '';
    if (p.type === 'time_window')
        return { text:`${neg}${_timePhrase(p.time_from, p.time_to)}, ${_daySpan(p.days)}`,
                 raw:`time_window ${p.time_from}–${p.time_to}` };
    if (p.type === 'sun')
        return { text:`${neg}🌅 between ${_esc(p.from)} and ${_esc(p.to)}`, raw:`sun ${p.from}→${p.to}` };
    const dev = _resolve(p.ieee);
    return { text:`${neg}${_devSpan(p.ieee)} ${_attrVerb(dev.type, p.attribute, p.operator, p.value)}`,
             raw:`${p.ieee && p.ieee.startsWith('group:') ? p.ieee + ' ' : ''}${_esc(p.attribute)} ${p.operator} ${_esc(p.value)}` };
}

const _CMD_VERB = {
    on:'Turn on', off:'Turn off', toggle:'Toggle', open:'Open', close:'Close', stop:'Stop',
    lock:'Lock', unlock:'Unlock', brightness:'Set brightness of', color_temp:'Set colour of',
    position:'Set position of',
};

function _cmdPhrase(s) {
    const verb = _CMD_VERB[s.command] || _esc(s.command);
    let tail = _resolve(s.target_ieee).name;
    // Only annotate outlet 2+ on real (non-group) devices — outlet 1 is noise.
    if (s.endpoint_id >= 2 && !String(s.target_ieee).startsWith('group:'))
        tail += ` (outlet ${s.endpoint_id})`;
    if (s.command === 'brightness' && s.value != null) tail += ` to ${Math.round(s.value / 255 * 100)}%`;
    if (s.command === 'position' && s.value != null) tail += ` to ${_esc(s.value)}%`;
    return `${verb} <span class="dev">${_esc(tail)}</span>`;
}

// Render a then/else sequence into action lines, expanding parallel + if_then_else.
function _renderSeq(seq) {
    if (!seq || !seq.length) return '<span class="ap-raw">— nothing —</span>';
    let h = '';
    seq.forEach(s => {
        if (s.type === 'command')
            h += `<div class="ap-act"><i class="fas fa-bolt"></i><span>${_cmdPhrase(s)}</span></div>`;
        else if (s.type === 'delay')
            h += `<div class="ap-act"><i class="fas fa-clock"></i><span>wait <span class="ap-delay">${_esc(s.seconds)}s</span></span></div>`;
        else if (s.type === 'parallel') {
            const cmds = (s.branches || []).flat().filter(Boolean);
            h += `<div class="ap-par"><span class="ap-par-tag"><i class="fas fa-bolt"></i> at the same time</span>`
               + cmds.map(c => c.type === 'command'
                    ? `<div class="ap-act"><i class="fas fa-bolt"></i><span>${_cmdPhrase(c)}</span></div>`
                    : _renderSeq([c])).join('')
               + `</div>`;
        }
        else if (s.type === 'if_then_else') {
            const conds = (s.inline_conditions || []).map(c => {
                const d = _resolve(c.ieee);
                return `${_devSpan(c.ieee)} ${_attrVerb(d.type, c.attribute, c.operator, c.value)}`;
            });
            const logic = ` ${s.condition_logic || 'and'} `;
            h += `<div class="ap-sub"><div class="ap-sub-head">Otherwise, if ${conds.join(logic) || '…'}:</div>${_renderSeq(s.then_steps)}`;
            if ((s.else_steps || []).length)
                h += `<div class="ap-sub-head" style="margin-top:6px">…else:</div>${_renderSeq(s.else_steps)}`;
            h += `</div>`;
        }
        else if (s.type === 'wait_for') {
            const d = _resolve(s.ieee);
            h += `<div class="ap-act"><i class="fas fa-hourglass-half"></i><span>wait for ${_devSpan(s.ieee)} ${_attrVerb(d.type, s.attribute, s.operator, s.value)}</span></div>`;
        }
        else if (s.type === 'condition') {
            const d = _resolve(s.ieee);
            h += `<div class="ap-act"><i class="fas fa-filter"></i><span>only continue if ${_devSpan(s.ieee)} ${_attrVerb(d.type, s.attribute, s.operator, s.value)}</span></div>`;
        }
        else if (s.type === 'media')
            h += `<div class="ap-act"><i class="fas fa-music"></i><span>${_esc(_mediaStepText(s))}</span></div>`;
    });
    return h;
}

// Human phrasing for a media step in the rules list. Player names come from
// /api/media/players; the raw player_id is the fallback when the media
// service is off or the player has vanished.
function _mediaStepText(s) {
    const who = playerNameCache[s.player_id] || s.player_id || 'player';
    const a = s.media_action || 'control';
    if (a === 'volume') return `set ${who} volume to ${Math.round((s.volume ?? 0) * 100)}%`;
    if (a === 'volume_adjust') {
        const d = s.delta || 0;
        return `turn ${who} volume ${d >= 0 ? 'up' : 'down'} ${Math.abs(Math.round(d * 100))}%`;
    }
    if (a === 'volume_fade') return `fade ${who} volume to ${Math.round((s.volume ?? 0) * 100)}% over ${s.fade_seconds || 300}s${s.stop_at_end ? ', then stop' : ''}`;
    if (a === 'announce') return `announce on ${who}: “${String(s.text || '').slice(0, 40)}”`;
    if (a === 'control') return `${s.control_action || 'control'} ${who}`;
    if (a === 'play_radio') return `play ${s.label || 'radio'} on ${who}`;
    if (a === 'play_tidal') return `play ${s.label || s.tidal_kind || 'Tidal'} on ${who}`;
    return `media: ${a}`;
}

let allRulesCache = [];
let devMapCache = {};
let playerNameCache = {};  // player_id -> friendly name, for media steps
let filterDevice = '';
let filterState = '';
// Rules the user has expanded — cards render collapsed (one line) by default
// to keep the page compact. Survives list re-renders within the session.
const expandedRules = new Set();
let locationConfigured = true;   // false → sun/sunrise-sunset can't resolve

// A rule "uses sun" if any condition OR prerequisite is a dynamic sun window.
function _ruleUsesSun(r) {
    const has = arr => Array.isArray(arr) && arr.some(c => c && c.type === 'sun');
    return has(r.conditions) || has(r.prerequisites);
}

// ============================================================================
// INIT
// ============================================================================

export function initAutomationsPage() {
    const tab = document.querySelector('button[data-bs-target="#automations"]');
    if (tab) {
        tab.addEventListener('shown.bs.tab', () => loadAutomationsPage());
    }
}

export async function loadAutomationsPage() {
    const container = document.getElementById('automations-content');
    if (!container) return;
    container.innerHTML = `<div class="text-center text-muted py-4"><i class="fas fa-spinner fa-spin"></i> Loading automations...</div>`;

    await initAIAutomations();

    try {
        const [rulesRes, devsRes] = await Promise.all([
            fetch('/api/automations'),
            fetch('/api/automations/devices')
        ]);
        allRulesCache = await rulesRes.json();
        const devices = await devsRes.json();

        // Player names for humanizing media steps (player_ids are uuids/ips).
        try {
            const pj = await (await fetch('/api/media/players')).json();
            playerNameCache = {};
            (pj.players || []).forEach(p => { playerNameCache[p.player_id] = p.name + (p.is_group ? ' (group)' : ''); });
        } catch { /* media service off — fall back to raw ids */ }

        // Is a location configured? Sun (sunrise/sunset) rules can't fire without
        // one. /api/sun/sunrise-sunset returns success:false when lat/lon are unset.
        try {
            const sun = await (await fetch('/api/sun/sunrise-sunset')).json();
            locationConfigured = sun.success === true;
        } catch { locationConfigured = true; /* don't false-alarm on a fetch error */ }

        _renderPage(container, devices);
    } catch (e) {
        container.innerHTML = `<div class="alert alert-danger"><i class="fas fa-exclamation-triangle"></i> ${e.message}</div>`;
    }
}

// ============================================================================
// PAGE RENDER
// ============================================================================

function _renderPage(container, devices) {
    // Device lookup
    const devMap = {};
    devices.forEach(d => { devMap[d.ieee] = d; });
    devMapCache = devMap;

    // Get unique source devices that have rules
    const sourcesWithRules = [...new Set(allRulesCache.map(r => r.source_ieee))];

    // Warn when sun-based rules exist but no location is set — they can never
    // fire because sunrise/sunset can't be computed without latitude/longitude.
    const sunRules = allRulesCache.filter(r => r.enabled !== false && _ruleUsesSun(r));
    const locationBanner = (!locationConfigured && sunRules.length) ? `
        <div class="alert alert-warning d-flex align-items-center justify-content-between py-2 mb-3">
            <div><i class="fas fa-map-marker-alt me-2"></i>
                <strong>${sunRules.length} sun-based rule${sunRules.length !== 1 ? 's' : ''} can't fire:</strong>
                your location isn't set, so sunrise/sunset times can't be calculated.</div>
            <button class="btn btn-sm btn-warning text-nowrap ms-3" onclick="window._apGoToLocationSettings(event)">
                <i class="fas fa-location-dot me-1"></i>Set location
            </button>
        </div>` : '';

    container.innerHTML = `
        <!-- Header -->
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div class="d-flex align-items-center gap-3">
                <span class="text-muted small">All automation rules across devices.</span>
                <span class="badge bg-primary">${allRulesCache.length} rule${allRulesCache.length !== 1 ? 's' : ''}</span>
            </div>
            <div class="d-flex gap-2">
                <select class="form-select form-select-sm" id="ap-filter-dev" style="width:auto;max-width:220px" onchange="window._apFilterDev(this.value)">
                    <option value="" ${filterDevice === '' ? 'selected' : ''}>All Devices</option>
                    ${sourcesWithRules.map(ieee => {
                        const d = devMap[ieee];
                        const label = ieee === '__time__' ? '⏰ Time / Alarm' : (d ? d.friendly_name : ieee);
                        return `<option value="${ieee}" ${ieee === filterDevice ? 'selected' : ''}>${label}</option>`;
                    }).join('')}
                </select>
                <select class="form-select form-select-sm" id="ap-filter-state" style="width:auto" onchange="window._apFilterState(this.value)">
                    <option value="" ${filterState === '' ? 'selected' : ''}>All States</option>
                    <option value="matched" ${filterState === 'matched' ? 'selected' : ''}>Matched</option>
                    <option value="unmatched" ${filterState === 'unmatched' ? 'selected' : ''}>Unmatched</option>
                    <option value="disabled" ${filterState === 'disabled' ? 'selected' : ''}>Disabled</option>
                </select>
                <button class="btn btn-sm btn-outline-secondary" id="ap-expand-btn" onclick="window._apExpandAll()" title="Expand all"><i class="fas fa-angles-down"></i></button>
                <button class="btn btn-sm btn-outline-secondary" onclick="window._apRefresh()"><i class="fas fa-sync-alt"></i></button>
                <button class="btn btn-sm btn-success" onclick="window._apCreate()"><i class="fas fa-plus"></i> New Rule</button>
            </div>
        </div>

        ${locationBanner}

        <!-- AI Automation Builder -->
        ${renderAIPanel()}

        <!-- Create Rule Panel (hidden by default) -->
        <div id="ap-create-panel" class="card mb-3" style="display:none">
            <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
                <strong><i class="fas fa-bolt"></i> New Automation</strong>
                <button class="btn btn-sm btn-outline-secondary" onclick="window._apCloseCreate()"><i class="fas fa-times"></i></button>
            </div>
            <div class="card-body">
                <div class="mb-3">
                    <label class="form-label small fw-bold">Source Device (trigger)</label>
                    <select class="form-select form-select-sm" id="ap-source-select" onchange="window._apSourceSelected(this.value)">
                        <option value="">Select a trigger…</option>
                        <option value="__time__">⏰ Time / Alarm (no device)</option>
                        ${devices.map(d => `<option value="${d.ieee}">${d.friendly_name}</option>`).join('')}
                    </select>
                </div>
                <div id="ap-form-host"></div>
            </div>
        </div>

        <!-- Edit Rule Panel (hidden by default) -->
        <div id="ap-edit-panel" class="card mb-3" style="display:none">
            <div class="card-header bg-light d-flex justify-content-between align-items-center py-2">
                <strong><i class="fas fa-edit"></i> Edit Automation — <span id="ap-edit-device-name"></span></strong>
                <button class="btn btn-sm btn-outline-secondary" onclick="window._apCloseEdit()"><i class="fas fa-times"></i></button>
            </div>
            <div class="card-body" id="ap-edit-host"></div>
        </div>

        <!-- Trace Panel (hidden by default) -->
        <div id="ap-trace-panel" class="card mb-3" style="display:none">
            <div class="card-header bg-dark text-white d-flex justify-content-between py-1">
                <strong><i class="fas fa-search"></i> Trace</strong>
                <div class="d-flex gap-2 align-items-center">
                    <select class="form-select form-select-sm bg-dark text-white border-secondary" id="tf" style="width:auto;max-width:220px;font-size:.75rem" onchange="window._aRefTrace()"><option value="">All</option></select>
                    <button class="btn btn-sm btn-outline-light" onclick="window._aRefTrace()"><i class="fas fa-sync-alt"></i></button>
                    <button class="btn btn-sm btn-outline-light" onclick="document.getElementById('ap-trace-panel').style.display='none'"><i class="fas fa-times"></i></button>
                </div>
            </div>
            <div class="card-body p-0" style="max-height:400px;overflow-y:auto"><div id="a-trace-c" class="font-monospace small p-2"></div></div>
        </div>

        <!-- Rules List -->
        <div id="ap-rules-list"></div>
    `;

    _renderRulesList(devMap);
}

// ============================================================================
// RULES LIST
// ============================================================================

function _visibleRules() {
    let rules = allRulesCache;
    if (filterDevice) rules = rules.filter(r => r.source_ieee === filterDevice);
    if (filterState === 'disabled') rules = rules.filter(r => r.enabled === false);
    else if (filterState === 'matched') rules = rules.filter(r => r._state === 'matched' && r.enabled !== false);
    else if (filterState === 'unmatched') rules = rules.filter(r => r._state !== 'matched' && r.enabled !== false);
    return rules;
}

function _renderRulesList(devMap = devMapCache) {
    const el = document.getElementById('ap-rules-list');
    if (!el) return;

    const rules = _visibleRules();
    _syncExpandBtn(rules);

    if (!rules.length) {
        el.innerHTML = `<div class="text-center text-muted py-4"><i class="fas fa-robot fa-2x mb-2 d-block opacity-50"></i>No automations found.</div>`;
        return;
    }

    // Group by source device (trigger).
    const grouped = {};
    rules.forEach(r => { (grouped[r.source_ieee] = grouped[r.source_ieee] || []).push(r); });

    let html = '';
    for (const [ieee, deviceRules] of Object.entries(grouped)) {
        const src = _resolve(ieee);
        html += `<div class="ap-devgroup">
            <div class="ap-devgroup-head">
                <span class="ap-idico sm">${_icon(src.type)}</span>
                <span class="ap-gname">${_esc(src.name)}</span>
                <span class="ap-gmeta">${_esc(ieee)}</span>
            </div>
            ${deviceRules.map(rule => _ruleCard(rule, src)).join('')}
        </div>`;
    }

    el.innerHTML = html;
}

// Render one rule as a WHEN → ONLY IF → DO → ELSE flow card.
function _ruleCard(rule, src) {
    const en = rule.enabled !== false;
    const st = rule._state || 'unknown';
    const trig = _triggerPhrase(rule);

    // status chip
    let stateChip = '';
    if (!en) stateChip = `<span class="ap-chip off"><i class="fas fa-pause"></i>disabled</span>`;
    else if (rule._running) stateChip = `<span class="ap-chip run"><i class="fas fa-spinner fa-spin"></i>running</span>`;
    else if (st === 'matched') stateChip = `<span class="ap-chip ok"><i class="fas fa-circle-check"></i>matched</span>`;
    else stateChip = `<span class="ap-chip mut"><i class="fas fa-circle"></i>${st === 'unmatched' ? 'idle' : 'init'}</span>`;

    const nameHtml = rule.name
        ? `<span class="ap-rname">${_esc(rule.name)}</span>`
        : `<span class="ap-rname untitled">Untitled rule</span>`;

    // flow steps: WHEN (trigger) → extra source conditions (AND) → ONLY IF (prereqs) → DO → ELSE
    const step = (lab, cls, inner) =>
        `<div class="ap-step"><div class="ap-lab ${cls}">${lab}</div><div class="ap-txt">${inner}</div></div>`;

    let flow = step('When', 'when', `${trig.text}${trig.raw ? `<span class="ap-raw">${_esc(trig.raw)}</span>` : ''}`);

    // Additional source conditions beyond the first are ANDed onto the trigger.
    (rule.conditions || []).slice(1).forEach(c => {
        const cp = _condPhrase(c);
        flow += step('and', 'and', `${cp.text}<span class="ap-raw">${_esc(cp.raw)}</span>`);
    });

    (rule.prerequisites || []).forEach((p, i) => {
        const cp = _condPhrase(p);
        flow += step(i === 0 ? 'Only if' : 'and', i === 0 ? 'if' : 'and',
            `${cp.text}<span class="ap-raw">${_esc(cp.raw)}</span>`);
    });

    flow += step('Do', 'do', `<div class="ap-acts">${_renderSeq(rule.then_sequence)}</div>`);
    if ((rule.else_sequence || []).length)
        flow += step('Else', 'else', `<div class="ap-acts">${_renderSeq(rule.else_sequence)}</div>`);

    // Collapsed one-line summary: "When <trigger> → n action(s)".
    const nActs = (rule.then_sequence || []).length;
    const isOpen = expandedRules.has(rule.id);

    return `<div class="ap-flowcard ${en ? '' : 'disabled'} ${isOpen ? '' : 'collapsed'}" id="ap-rule-${rule.id}">
        <div class="ap-crow" onclick="window._apToggleExpand('${rule.id}')" title="Expand">
            <span class="ap-idico sm">${_icon(src.type)}</span>
            ${nameHtml}
            <span class="ap-csum">When ${trig.text} → ${nActs} action${nActs === 1 ? '' : 's'}</span>
            ${stateChip}
            <span class="ap-crow-btns" onclick="event.stopPropagation()">
                <button class="btn btn-sm btn-outline-secondary py-0" onclick="window._apTrace('${rule.id}')" title="Trace"><i class="fas fa-search"></i></button>
                <button class="btn btn-sm btn-outline-primary py-0" onclick="window._apEdit('${rule.id}')" title="Edit"><i class="fas fa-edit"></i></button>
                <button class="btn btn-sm btn-outline-${en ? 'warning' : 'success'} py-0" onclick="window._apToggle('${rule.id}')" title="${en ? 'Disable' : 'Enable'}"><i class="fas fa-${en ? 'pause' : 'play'}"></i></button>
                <button class="btn btn-sm btn-outline-danger py-0" onclick="window._apDelete('${rule.id}')" title="Delete"><i class="fas fa-trash"></i></button>
            </span>
            <i class="fas fa-chevron-down ap-chev"></i>
        </div>
        <div class="ap-rail">
            <span class="ap-idico lg">${_icon(src.type)}</span>
            <div>${nameHtml}</div>
            <div class="ap-rmeta">
                <span class="ap-chip"><i class="fas ${DEVICE_ICON[src.type]}"></i>${DEVICE_LABEL[src.type]}</span>
                ${stateChip}
                ${rule.cooldown ? `<span class="ap-chip mut"><span class="num">⏱ ${rule.cooldown}s</span></span>` : ''}
            </div>
        </div>
        <div class="ap-flow">${flow}</div>
        <div class="ap-foot">
            <button class="btn btn-sm btn-outline-secondary" onclick="window._apToggleExpand('${rule.id}')" title="Collapse"><i class="fas fa-chevron-up"></i></button>
            <span class="spacer"></span>
            <button class="btn btn-sm btn-outline-secondary" onclick="window._apTrace('${rule.id}')" title="Trace"><i class="fas fa-search"></i> Trace</button>
            <button class="btn btn-sm btn-outline-primary" onclick="window._apEdit('${rule.id}')" title="Edit"><i class="fas fa-edit"></i> Edit</button>
            <button class="btn btn-sm btn-outline-${en ? 'warning' : 'success'}" onclick="window._apToggle('${rule.id}')" title="${en ? 'Disable' : 'Enable'}"><i class="fas fa-${en ? 'pause' : 'play'}"></i></button>
            <button class="btn btn-sm btn-outline-danger" onclick="window._apDelete('${rule.id}')" title="Delete"><i class="fas fa-trash"></i></button>
        </div>
    </div>`;
}

// ============================================================================
// ACTIONS
// ============================================================================

async function _apRefresh() {
    await loadAutomationsPage();
}

function _apFilterDev(val) {
    filterDevice = val;
    _renderRulesList();
}

function _apFilterState(val) {
    filterState = val;
    _renderRulesList();
}

// --- Expand / collapse ---

function _apToggleExpand(ruleId) {
    if (expandedRules.has(ruleId)) expandedRules.delete(ruleId);
    else expandedRules.add(ruleId);
    _renderRulesList();
}

// Expand every visible rule; if they're all already open, collapse them all.
function _apExpandAll() {
    const visible = _visibleRules().map(r => r.id);
    const allOpen = visible.length > 0 && visible.every(id => expandedRules.has(id));
    if (allOpen) visible.forEach(id => expandedRules.delete(id));
    else visible.forEach(id => expandedRules.add(id));
    _renderRulesList();
}

function _syncExpandBtn(rules) {
    const btn = document.getElementById('ap-expand-btn');
    if (!btn) return;
    const allOpen = rules.length > 0 && rules.every(r => expandedRules.has(r.id));
    btn.innerHTML = `<i class="fas fa-angles-${allOpen ? 'up' : 'down'}"></i>`;
    btn.title = allOpen ? 'Collapse all' : 'Expand all';
}

// --- Trace ---

// Open the page-level trace panel filtered to one rule. Reuses the trace
// loader from modal/automation.js (window._aRefTrace), which reads the shared
// #tf / #a-trace-c IDs — the create/edit hosts render their own copies of
// those IDs, so clear them first or they'd shadow the panel's.
function _apTrace(ruleId) {
    document.getElementById('ap-create-panel').style.display = 'none';
    document.getElementById('ap-form-host').innerHTML = '';
    document.getElementById('ap-edit-panel').style.display = 'none';
    document.getElementById('ap-edit-host').innerHTML = '';

    const panel = document.getElementById('ap-trace-panel');
    const f = document.getElementById('tf');
    f.innerHTML = '<option value="">All</option>'
        + allRulesCache.map(r => `<option value="${_esc(r.id)}">${_esc(r.name || r.id)}</option>`).join('')
        + '<option value="-">System</option>';
    f.value = ruleId || '';
    panel.style.display = 'block';
    panel.scrollIntoView({ behavior: 'smooth' });
    window._aRefTrace();
}

// --- Create ---

function _apCreate() {
    document.getElementById('ap-create-panel').style.display = 'block';
    document.getElementById('ap-edit-panel').style.display = 'none';
    document.getElementById('ap-trace-panel').style.display = 'none';
    document.getElementById('ap-source-select').value = '';
    document.getElementById('ap-form-host').innerHTML = '<div class="text-muted small">Select a source device to begin.</div>';
    document.getElementById('ap-create-panel').scrollIntoView({ behavior: 'smooth' });
}

function _apCloseCreate() {
    document.getElementById('ap-create-panel').style.display = 'none';
    document.getElementById('ap-form-host').innerHTML = '';
}

async function _apSourceSelected(ieee) {
    const host = document.getElementById('ap-form-host');
    if (!ieee) { host.innerHTML = '<div class="text-muted small">Select a source device to begin.</div>'; return; }

    // Render the automation form UI into the host
    host.innerHTML = `
        <div id="automation-tab-content">
            <div id="a-form" class="card mb-3" style="display:none"></div>
            <div id="a-trace" class="card mb-3" style="display:none">
                <div class="card-header bg-dark text-white d-flex justify-content-between py-1">
                    <strong><i class="fas fa-search"></i> Trace</strong>
                    <div class="d-flex gap-2 align-items-center">
                        <select class="form-select form-select-sm bg-dark text-white border-secondary" id="tf" style="width:auto;max-width:220px;font-size:.75rem" onchange="window._aRefTrace()"><option value="">All</option></select>
                        <button class="btn btn-sm btn-outline-light" onclick="window._aRefTrace()"><i class="fas fa-sync-alt"></i></button>
                        <button class="btn btn-sm btn-outline-light" onclick="document.getElementById('a-trace').style.display='none'"><i class="fas fa-times"></i></button>
                    </div>
                </div>
                <div class="card-body p-0" style="max-height:400px;overflow-y:auto"><div id="a-trace-c" class="font-monospace small p-2"></div></div>
            </div>
            <div id="a-rules"><div class="text-center text-muted py-3"><i class="fas fa-spinner fa-spin"></i></div></div>
        </div>`;

    // Init using existing modal/automation.js machinery
    await initAutomationTab(ieee);

    // Auto-open the new rule form
    if (typeof window._aShowForm === 'function') window._aShowForm();
}

// --- Edit ---

async function _apEdit(ruleId) {
    const rule = allRulesCache.find(r => r.id === ruleId);
    if (!rule) return;

    // Close create panel / trace panel if open
    document.getElementById('ap-create-panel').style.display = 'none';
    document.getElementById('ap-trace-panel').style.display = 'none';

    const editPanel = document.getElementById('ap-edit-panel');
    const editHost = document.getElementById('ap-edit-host');
    const editDevName = document.getElementById('ap-edit-device-name');

    // Resolve device name
    try {
        const devsRes = await fetch('/api/automations/devices');
        const devs = await devsRes.json();
        const dev = devs.find(d => d.ieee === rule.source_ieee);
        editDevName.textContent = dev ? dev.friendly_name : rule.source_ieee;
    } catch (e) {
        editDevName.textContent = rule.source_ieee;
    }

    // Render automation tab structure into edit host
    editHost.innerHTML = `
        <div id="automation-tab-content">
            <div class="d-flex justify-content-between align-items-center mb-3">
                <span class="text-muted small">Editing rule for this device.</span>
                <div>
                    <button class="btn btn-sm btn-outline-secondary me-1" onclick="window._aTrace()"><i class="fas fa-search"></i> Trace</button>
                    <button class="btn btn-sm btn-success" onclick="window._aShowForm()"><i class="fas fa-plus"></i> Add Rule</button>
                </div>
            </div>
            <div id="a-form" class="card mb-3" style="display:none"></div>
            <div id="a-trace" class="card mb-3" style="display:none">
                <div class="card-header bg-dark text-white d-flex justify-content-between py-1">
                    <strong><i class="fas fa-search"></i> Trace</strong>
                    <div class="d-flex gap-2 align-items-center">
                        <select class="form-select form-select-sm bg-dark text-white border-secondary" id="tf" style="width:auto;max-width:220px;font-size:.75rem" onchange="window._aRefTrace()"><option value="">All</option></select>
                        <button class="btn btn-sm btn-outline-light" onclick="window._aRefTrace()"><i class="fas fa-sync-alt"></i></button>
                        <button class="btn btn-sm btn-outline-light" onclick="document.getElementById('a-trace').style.display='none'"><i class="fas fa-times"></i></button>
                    </div>
                </div>
                <div class="card-body p-0" style="max-height:400px;overflow-y:auto"><div id="a-trace-c" class="font-monospace small p-2"></div></div>
            </div>
            <div id="a-rules"><div class="text-center text-muted py-3"><i class="fas fa-spinner fa-spin"></i></div></div>
        </div>`;

    editPanel.style.display = 'block';
    editPanel.scrollIntoView({ behavior: 'smooth' });

    // Init automation tab for this device
    await initAutomationTab(rule.source_ieee);

    // Open edit form for this specific rule
    if (typeof window._aEdit === 'function') window._aEdit(ruleId);
}

function _apCloseEdit() {
    document.getElementById('ap-edit-panel').style.display = 'none';
    document.getElementById('ap-edit-host').innerHTML = '';
    // Refresh the rules list to reflect any changes
    loadAutomationsPage();
}

// --- Toggle / Delete ---

async function _apToggle(ruleId) {
    try {
        await fetch(`/api/automations/${ruleId}/toggle`, { method: 'PATCH' });
        await loadAutomationsPage();
    } catch (e) {
        log.error('Toggle failed:', e);
    }
}

async function _apDelete(ruleId) {
    if (!await window.zbmConfirm({
        title: 'Delete rule',
        message: 'Delete this automation rule?',
        confirmText: 'Delete',
        variant: 'danger'
    })) return;
    try {
        await fetch(`/api/automations/${ruleId}`, { method: 'DELETE' });
        await loadAutomationsPage();
    } catch (e) {
        window.toast.error('Delete failed: ' + e.message);
    }
}

// Jump to Settings → Weather and focus the latitude field so the user can set
// their location (sun rules need it). Activates the inner tab the field lives in.
function _apGoToLocationSettings(ev) {
    if (ev) ev.preventDefault();
    const tabBtn = document.querySelector('button[data-bs-target="#settings"]');
    if (tabBtn) tabBtn.click();
    setTimeout(() => {
        const lat = document.getElementById('cfg_weather_lat');
        if (!lat) return;
        // The field may sit inside an inner Bootstrap tab-pane — activate it.
        const pane = lat.closest('.tab-pane');
        if (pane && pane.id) {
            const innerBtn = document.querySelector(`button[data-bs-target="#${pane.id}"]`);
            if (innerBtn) innerBtn.click();
        }
        lat.scrollIntoView({ behavior: 'smooth', block: 'center' });
        lat.focus();
    }, 450);
}

// ============================================================================
// WINDOW HANDLERS
// ============================================================================

window._apGoToLocationSettings = _apGoToLocationSettings;
window._apRefresh = _apRefresh;
window._apFilterDev = _apFilterDev;
window._apFilterState = _apFilterState;
window._apToggleExpand = _apToggleExpand;
window._apTrace = _apTrace;
window._apExpandAll = _apExpandAll;
window._apCreate = _apCreate;
window._apCloseCreate = _apCloseCreate;
window._apSourceSelected = _apSourceSelected;
window._apEdit = _apEdit;
window._apCloseEdit = _apCloseEdit;
window._apToggle = _apToggle;
window._apDelete = _apDelete;