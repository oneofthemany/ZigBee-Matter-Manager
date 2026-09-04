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
import { initAIAutomations, renderAIChatPanel } from './ai-automations.js';
import { DEVICE_ICON, DEVICE_LABEL, deviceType } from './automation-humanize.js';
import { createHumanizer, esc } from './automation-sentence.js';
import { showToast, withBusy } from './utils.js';


const log = zmmLog('automations-page');

// Swarm coverage for the header strip — null whenever the swarm is unavailable.
let swarmCoverage = null, swarmSummary = null;
// Offers awaiting an answer. An offer nobody can see is an offer nobody can
// accept, so these sit at the top of the page rather than in a side panel.
let pendingOffers = [];

const OP = { eq:'=', neq:'≠', gt:'>', lt:'<', gte:'≥', lte:'≤', in:'∈', nin:'∉', changed:'Δ' };

// HUMANIZATION — turn raw rule JSON into plain-English, device-aware phrasing.
// Device type comes from capability_list on the main device list (state.devices);
// falls back to name/model/state-key heuristics when capabilities are absent.

// DEVICE_ICON / DEVICE_LABEL / deviceType are imported from automation-humanize.js

// Plain-English rendering lives in automation-sentence.js so the rules list
// and the rule editor say the same thing the same way. The humanizer is bound
// to this page's caches, which are populated per page load.
let H = createHumanizer({ device: ieee => devMapCache[ieee] });

function _rebindHumanizer() {
    H = createHumanizer({
        device: ieee => devMapCache[ieee],
        player: id => playerNameCache[id],
        place: id => placeNameCache[id],
    });
}

const _esc = esc;
const _resolve = ieee => H.resolve(ieee);
const _devSpan = ieee => H.devSpan(ieee);
const _attrVerb = (t, a, o, v) => H.attrVerb(t, a, o, v);
const _triggerPhrase = rule => H.triggerPhrase(rule);
const _condPhrase = (p, src) => H.condPhrase(p, src);
const _renderSeq = seq => H.renderSeq(seq);
const _icon = t => H.icon(t);

let allRulesCache = [];
let devMapCache = {};
let playerNameCache = {};  // player_id -> friendly name, for media steps
let placeNameCache = {};   // place id -> friendly name, for zone conditions
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

        // ...and OpenZone zones, which a media step can target as zone:<id>.
        try {
            const zj = await (await fetch('/api/media/sync/groups')).json();
            (zj.groups || []).forEach(z => { playerNameCache['zone:' + z.id] = `the ${z.name} zone`; });
        } catch { /* OpenZone off — fall back to raw ids */ }

        // Place names for humanizing zone (enter/leave) conditions.
        try {
            const plj = await (await fetch('/api/places')).json();
            placeNameCache = {};
            (plj.places || []).forEach(p => { placeNameCache[p.id] = p.name; });
        } catch { /* fall back to raw place ids */ }

        // Offers waiting on somebody. Optional like the rest of the swarm.
        try {
            const oj = await (await fetch('/api/automations/offers')).json();
            pendingOffers = oj.offers || [];
        } catch { pendingOffers = []; }

        // Swarm coverage — how much of the house takes part in a rule at all.
        // Optional: an unavailable swarm just hides the strip.
        try {
            const cj = await (await fetch('/api/swarm/coverage')).json();
            swarmCoverage = cj.coverage || null;
            swarmSummary = cj.summary || null;
        } catch { swarmCoverage = null; swarmSummary = null; }

        // Is a location configured? Sun (sunrise/sunset) rules can't fire without
        // one. /api/sun/sunrise-sunset returns success:false when lat/lon are unset.
        try {
            const sun = await (await fetch('/api/sun/sunrise-sunset')).json();
            locationConfigured = sun.success === true;
        } catch { locationConfigured = true; /* don't false-alarm on a fetch error */ }

        _rebindHumanizer();
        _renderPage(container, devices);
    } catch (e) {
        container.innerHTML = `<div class="alert alert-danger"><i class="fas fa-exclamation-triangle"></i> ${e.message}</div>`;
    }
}

// PAGE RENDER

/**
 * How much of the house is automated at all, and how much more the swarm
 * reckons it could be.
 *
 * A device counts as covered whether it triggers a rule or is driven by one —
 * a bulb nobody has automated is a gap even though it triggers nothing itself.
 */
function _coverageStrip() {
    if (!swarmCoverage) return '';
    const c = swarmCoverage;
    const tone = c.percent >= 75 ? 'success' : c.percent >= 40 ? 'warning' : 'secondary';
    const spare = swarmSummary && swarmSummary.available
        ? `<span class="badge bg-light text-dark border" title="Suggestions not yet built">
             <i class="fas fa-diagram-project text-primary me-1"></i>${swarmSummary.available} suggested</span>`
        : '';
    const gaps = c.uncovered
        ? `<button class="btn btn-link btn-sm p-0 small text-decoration-none"
                   onclick="window._apShowGaps()">${c.uncovered} not automated</button>`
        : '';
    return `<span class="badge bg-${tone}" title="Devices taking part in at least one rule">
                ${c.covered}/${c.devices} devices automated</span>${spare}${gaps}`;
}

/**
 * Offers waiting on an answer.
 *
 * An offer is a rule that stopped to ask, so it belongs with the rules rather
 * than in a notification tray — the question and the automation that raised it
 * are the same thing.
 */
function _offersBanner() {
    if (!pendingOffers.length) return '';
    const rows = pendingOffers.map(o => `
        <div class="d-flex justify-content-between align-items-center gap-2 py-1">
            <div class="flex-grow-1">
                <div class="small">${_esc(o.message)}</div>
                <div class="text-muted" style="font-size:.75rem">
                    ${_esc(o.rule_name)} · asked ${_esc(o.to_user)}
                </div>
            </div>
            <div class="d-flex gap-1 flex-shrink-0">
                <button class="btn btn-sm btn-success" onclick="window._apAnswerOffer('${_esc(o.token)}','accept',this)">
                    <i class="fas fa-check"></i> Yes</button>
                <button class="btn btn-sm btn-outline-secondary" onclick="window._apAnswerOffer('${_esc(o.token)}','decline',this)">
                    <i class="fas fa-times"></i> No</button>
            </div>
        </div>`).join('<hr class="my-1">');
    return `
        <div class="card mb-3 border-warning" id="ap-offers">
            <div class="card-header bg-warning bg-opacity-10 py-1">
                <strong class="small"><i class="fas fa-circle-question me-1"></i>
                    ${pendingOffers.length} automation${pendingOffers.length !== 1 ? 's are' : ' is'} waiting on you</strong>
            </div>
            <div class="card-body py-2">${rows}</div>
        </div>`;
}

/** Answer an offer. The action lives in the engine; this only says yes or no. */
window._apAnswerOffer = async (token, answer, btn) => {
    await withBusy(btn, async () => {
        try {
            const res = await fetch(`/api/automations/offers/${encodeURIComponent(token)}/${answer}`,
                                    { method: 'POST' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'That offer is no longer available');
            showToast(answer === 'accept' ? 'Done' : 'Dismissed', 'success');
        } catch (e) {
            showToast(e.message, 'danger');
        }
        await loadAutomationsPage();
    });
};

/** List the devices no rule touches, so the gap is actionable rather than a number. */
window._apShowGaps = () => {
    if (!swarmCoverage) return;
    const host = document.getElementById('automations-content');
    if (!host) return;
    const existing = document.getElementById('ap-gaps');
    if (existing) { existing.remove(); return; }   // second click closes it
    const rows = (swarmCoverage.gaps || []).map(g =>
        `<tr><td>${_esc(g.name)}</td>
             <td class="text-muted small">${_esc(g.room_label || 'No room')}</td>
             <td class="text-muted small">${_esc(g.device_class || '')}</td></tr>`).join('');
    host.insertAdjacentHTML('afterbegin', `
        <div class="card mb-3" id="ap-gaps">
            <div class="card-header bg-light d-flex justify-content-between py-1">
                <strong class="small"><i class="fas fa-circle-exclamation text-warning me-1"></i>
                    Devices with no automation</strong>
                <button class="btn btn-sm btn-outline-secondary py-0"
                        onclick="document.getElementById('ap-gaps').remove()">
                    <i class="fas fa-times"></i></button>
            </div>
            <div class="card-body p-0" style="max-height:300px;overflow-y:auto">
                <table class="table table-sm mb-0"><tbody>${rows}</tbody></table>
            </div>
        </div>`);
};

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
        ${_offersBanner()}
        <!-- Header -->
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div class="d-flex align-items-center gap-3 flex-wrap">
                <span class="text-muted small">All automation rules across devices.</span>
                <span class="badge bg-primary">${allRulesCache.length} rule${allRulesCache.length !== 1 ? 's' : ''}</span>
                ${_coverageStrip()}
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
        ${renderAIChatPanel()}

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

// RULES LIST

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

    // Additional source conditions beyond the first join the trigger with the
    // rule's condition_logic — "and" (all must hold) or "or" (any one fires it).
    const joiner = rule.condition_logic === 'or' ? 'or' : 'and';
    (rule.conditions || []).slice(1).forEach(c => {
        const cp = _condPhrase(c, rule.source_ieee);
        flow += step(joiner, joiner, `${cp.text}<span class="ap-raw">${_esc(cp.raw)}</span>`);
    });

    (rule.prerequisites || []).forEach((p, i) => {
        const cp = _condPhrase(p);
        flow += step(i === 0 ? 'Only if' : 'and', i === 0 ? 'if' : 'and',
            `${cp.text}<span class="ap-raw">${_esc(cp.raw)}</span>`);
    });

    flow += step('Do', 'do', `<div class="ap-acts">${_renderSeq(rule.then_sequence)}</div>`);
    if ((rule.else_sequence || []).length)
        flow += step('Else', 'else', `<div class="ap-acts">${_renderSeq(rule.else_sequence)}</div>`);

    // Collapsed one-line summary: "When <trigger> → n action(s)". The summary
    // only names the first condition, so an OR rule says how many alternatives
    // it hides — otherwise it reads as a much narrower trigger than it is.
    const nActs = (rule.then_sequence || []).length;
    const nMore = Math.max(0, (rule.conditions || []).length - 1);
    const csumTrig = (joiner === 'or' && nMore)
        ? `${trig.text} <span class="ap-or-more">or ${nMore} more</span>`
        : trig.text;
    const isOpen = expandedRules.has(rule.id);

    return `<div class="ap-flowcard ${en ? '' : 'disabled'} ${isOpen ? '' : 'collapsed'}" id="ap-rule-${rule.id}">
        <div class="ap-crow" onclick="window._apToggleExpand('${rule.id}')" title="Expand">
            <span class="ap-idico sm">${_icon(src.type)}</span>
            ${nameHtml}
            <span class="ap-csum">When ${csumTrig} → ${nActs} action${nActs === 1 ? '' : 's'}</span>
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

// ACTIONS

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

// Expand / collapse

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

// Trace

// Open the page-level trace panel filtered to the pressed rule (the backend
// keeps a per-rule history, so the rule's full timeline survives churn in
// the shared log). The dropdown can switch to All / another rule / System.
// Reuses the trace loader from modal/automation.js (window._aRefTrace),
// which reads the shared #tf / #a-trace-c IDs — the create/edit hosts render
// their own copies of those IDs, so clear them first or they'd shadow the
// panel's.
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

// Create

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

// Edit

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

// Toggle / Delete

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

// WINDOW HANDLERS

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