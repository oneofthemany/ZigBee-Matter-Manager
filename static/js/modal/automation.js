/**
 * Automation Tab — State Machine with Recursive Action Sequences
 *
 * Step types: command, delay, wait_for, condition (gate),
 *             if_then_else (branching), parallel (concurrent)
 *
 * Prerequisites support NOT (negate) flag.
 * Trigger conditions support AND/OR logic (rule.condition_logic).
 * Inline conditions in if_then_else support AND/OR logic.
 */

import { state } from '../state.js';
import { deviceType, attrLabel, attrEnum, typeTriggerAttrs } from '../automation-humanize.js';
import { renderChooser, invalidateChooser } from '../swarm-suggest.js';
import { createHumanizer } from '../automation-sentence.js';

let cachedActuators = [], cachedAttributes = [], cachedAllDevices = [], cachedPresenceUsers = [];
let cachedPlayers = [];   // media players (Cast/WiiM) for media steps
let cachedZones = [];     // OpenZone zones, targetable as zone:<id>
let cachedPlaces = [];    // named places (geofences) for zone conditions
let currentSourceIeee = null, editingRuleId = null;
let condRows = [], condIdC = 0, prereqRows = [], prereqIdC = 0;
// How the trigger conditions combine: 'and' (all must hold) or 'or' (any one).
let condLogic = 'and';
// Step trees stored in memory — rendered to DOM
let thenTree = [], elseTree = [], stepIdC = 0;

const OP = {'eq':'=','neq':'≠','gt':'>','lt':'<','gte':'>=','lte':'<=','in':'∈','nin':'∉'};

const OPT = {'eq':'equals','neq':'not equal','gt':'greater than','lt':'less than','gte':'greater or equal','lte':'less or equal','in':'in list','nin':'not in list'};

function _opOpts(sel) {
    return Object.entries(OP).map(([k,v]) =>
        `<option value="${k}" ${k===sel?'selected':''}>${v} ${OPT[k]}</option>`
    ).join('');
}
const SICON = {command:'fa-bolt',delay:'fa-clock',wait_for:'fa-hourglass-half',condition:'fa-filter',if_then_else:'fa-code-branch',parallel:'fa-columns',media:'fa-music',request:'fa-comment',offer:'fa-circle-question'};

const SLBL = {command:'Command',delay:'Delay',wait_for:'Wait For',condition:'Gate',if_then_else:'If / Then / Else',parallel:'Parallel',media:'Media',request:'Message',offer:'Ask First'};

// Media action picker options (label, value).
const MEDIA_ACTIONS = [['play_zone','Play Zone (saved source)'],['play_tidal','Play Tidal'],['play_radio','Play Radio'],['announce','Announce (TTS)'],['control','Control'],['volume','Volume'],['volume_adjust','Volume Up/Down'],['volume_fade','Volume Fade']];
const MEDIA_CONTROLS = [['pause','Pause'],['resume','Resume'],['stop','Stop'],['next','Next'],['prev','Previous']];

// A zone plays one server-built timeline rather than driving a device's own
// transport, so the queue controls have nothing to act on and only Stop is
// offered. play_zone is the mirror image — it means "whatever this zone is
// set to play", which only a zone has.
// A zone is a player, so it takes the full transport. Next/prev move its
// server-side queue and are heard once the delay line drains (open-zone.md §4.1b).
const ZONE_CONTROLS = MEDIA_CONTROLS;
const isZoneId = pid => String(pid||'').startsWith('zone:');
const zoneOf = pid => cachedZones.find(z => 'zone:'+z.id === pid) || null;
const TIDAL_KINDS = [['playlist','Playlist'],['album','Album'],['artist','Artist'],['mix','Mix'],['track','Track']];

// Readable label for a dynamic sun condition (sunrise/sunset window).
function _sunDesc(c) {
    const off = x => x ? ` ${x > 0 ? '+' : ''}${x}m` : '';
    const neg = c.negate ? '<span class="badge bg-danger ms-1">NOT</span> ' : '';
    return `${neg}🌅 <code>${c.from}${off(c.offset_from)} → ${c.to}${off(c.offset_to)}</code>`;
}
// Boundary choices for the Sun condition From/To pickers.
const _SUN_OPTS = [['sunrise','Sunrise'],['sunset','Sunset'],['00:00','Start of day'],['23:59','End of day']];

// Zone (enter/leave a place)
// Only a presence user has a `place`, so the Zone condition type is offered
// for people and nothing else.
const _isPerson = ieee => String(ieee||'').startsWith('user::');
// Friendly name for a place id. "home" is per-user rather than a configured
// place, so it never appears in /api/places and is labelled here.
function _placeName(id) {
    if (id === 'any') return 'any place';
    if (id === 'home') return 'Home';
    return cachedPlaces.find(p => p.id === id)?.name || id;
}
// A zone may group several places, so render it the same way the Time/Day row
// renders weekdays: "work" is one zone made of two offices, and moving between
// them is movement within it rather than a departure and an arrival.
const _placeLabel = p => Array.isArray(p) ? p.map(_placeName).join(' or ') : _placeName(p);

// Places a crossing can be about: any real location, home, or a named place.
// "away"/"unknown" are the absence of a place and are deliberately excluded —
// you leave the shops *for* away, you don't arrive at it.
function _placeIds() {
    const attr = cachedAttributes.find(a => a.attribute === 'place');
    const ids = (attr?.value_options || []).filter(v => v !== 'away' && v !== 'unknown');
    if (!ids.includes('home')) ids.unshift('home');
    return ids;
}
function _placeBoxes(id) {
    return [['any','Any place'], ..._placeIds().map(i => [i, _placeName(i)])]
        .map(([v,l]) => `<label class="me-2 small text-nowrap"><input type="checkbox" class="czp" data-id="${id}" data-place="${v}" ${v==='any'?'checked':''} onchange="window._aCZP(${id},this)"> ${l}</label>`)
        .join('');
}

// Group optgroup builder
function _devOpts(list, selectedIeee, extraAttrs='') {
    const devs = list.filter(d => !d._is_group);
    const grps = list.filter(d => d._is_group);
    let h = devs.map(d =>
        `<option value="${d.ieee}" ${extraAttrs ? extraAttrs.replace('$IEEE', d.ieee).replace('$SEL', d.ieee===selectedIeee?'selected':'') : (d.ieee===selectedIeee?'selected':'')}>${d.friendly_name}</option>`
    ).join('');
    if (grps.length) {
        h += `<optgroup label="── Groups ──">` + grps.map(d =>
            `<option value="${d.ieee}" ${extraAttrs ? extraAttrs.replace('$IEEE', d.ieee).replace('$SEL', d.ieee===selectedIeee?'selected':'') : (d.ieee===selectedIeee?'selected':'')}>${d.friendly_name}</option>`
        ).join('') + '</optgroup>';
    }
    return h;
}

function _uid() { return stepIdC++; }

// Device-aware helpers (Track B)

// Device summary (name/model/state_keys) for heuristics; feeds deviceType().
function _summary(ieee) { return (cachedAllDevices || []).find(d => d.ieee === ieee); }
const _dtype = ieee => deviceType(ieee, _summary(ieee));

// Merge canonical type triggers into a fetched attribute list, skipping dupes.
function _mergeTypeAttrs(attrs, type) {
    const have = new Set((attrs || []).map(a => a.attribute));
    const extra = typeTriggerAttrs(type)
        .filter(a => !have.has(a.attribute))
        .map(a => ({ current_value: '—', ...a }));
    return [...(attrs || []), ...extra];
}

// Friendly <option> text for an attribute descriptor.
//
// The raw attribute name is shown only when it says something the friendly
// label does not: "Place  ·  place" is the same word twice, and it was the
// widest thing in an already crowded row. "Open / Closed  ·  is_open" earns
// both halves.
//
// The live value stays — it is the one piece of context the row cannot show
// elsewhere, since the value field beside it holds the target, not the current
// reading — but it is truncated, because a long string pushes the operator and
// value controls off the row.
const _ATTR_CUR_MAX = 18;

function _attrOptLabel(a, type) {
    const friendly = attrLabel(type, a.attribute);
    const norm = x => String(x || '').toLowerCase().replace(/[\s_-]+/g, '');
    const raw = norm(friendly) === norm(a.attribute) ? '' : `  \u00b7  ${a.attribute}`;

    let cur = '';
    if (a.current_value !== undefined && a.current_value !== '\u2014'
            && a.current_value !== null && a.current_value !== '') {
        let v = String(a.current_value);
        if (v.length > _ATTR_CUR_MAX) v = v.slice(0, _ATTR_CUR_MAX - 1) + '\u2026';
        cur = `  \u2014 now ${v}`;
    }
    return `${friendly}${raw}${cur}`;
}

// Build the <option> list for an attribute <select>, humanized for a device type.
function _attrOptions(attrs, type) {
    return (attrs || []).map(a =>
        `<option value="${a.attribute}" data-type="${a.type}" data-operators='${JSON.stringify(a.operators || ['eq','neq'])}' data-current="${a.current_value}" data-vo='${JSON.stringify(a.value_options || [])}'>${_attrOptLabel(a, type)}</option>`
    ).join('');
}

// Value input, preferring a labeled enum for the (type, attribute) pair.
// Falls back to the raw value_options list (vo), then a free-text box.
function _valueInput(cls, id, type, attribute, valType, cur, vo, idAttr = 'data-id') {
    const en = attrEnum(type, attribute, valType);
    if (en) {
        const norm = cur !== undefined && cur !== '' ? String(cur).toLowerCase() : '';
        return `<select class="form-select form-select-sm ${cls}" ${idAttr}="${id}">`
            + en.map(o => `<option value="${o.value}" ${norm === String(o.value).toLowerCase() ? 'selected' : ''}>${o.label}</option>`).join('')
            + '</select>';
    }
    return _vI(cls, id, vo || [], cur, idAttr);
}

// RENDER

export function renderAutomationTab(device) {
    currentSourceIeee = device.ieee;
    return `<div id="automation-tab-content">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <span class="text-muted small">State-machine triggers with action sequences.</span>
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
}

export async function initAutomationTab(ieee) {
    currentSourceIeee = ieee;
    // The virtual "__time__" source has no physical device, so there are no
    // device attributes to fetch — its rules trigger purely on the clock.
    const isTime = ieee === '__time__';
    try {
        const [rR,aR,actR,dR] = await Promise.all([
            fetch(`/api/automations?source_ieee=${encodeURIComponent(ieee)}`),
            isTime ? Promise.resolve(null) : fetch(`/api/automations/device/${encodeURIComponent(ieee)}/attributes`),
            fetch('/api/automations/actuators'), fetch('/api/automations/devices'),
        ]);
        cachedAttributes = isTime ? [] : await aR.json(); cachedActuators = await actR.json();
        cachedAllDevices = await dR.json();
        // Device-aware: ensure canonical triggers for this device type are always
        // offered (e.g. a button's transient `action`), even if the current state
        // snapshot doesn't include them right now.
        if (!isTime) cachedAttributes = _mergeTypeAttrs(cachedAttributes, _dtype(ieee));
        _renderRules(await rR.json());
    } catch(e) { const el=document.getElementById('a-rules'); if(el)el.innerHTML=`<div class="alert alert-danger">${e.message}</div>`; }
    // Media players are optional — a failure here must not break the tab.
    try { const pj = await (await fetch('/api/media/players')).json(); cachedPlayers = pj.success ? (pj.players||[]) : []; }
    catch(e) { cachedPlayers = []; }
    // OpenZone zones are targets too, addressed as zone:<id>. Their own
    // endpoint, not /players: a zone is a saved arrangement of speakers, not a
    // device the media controller polls.
    try { const zj = await (await fetch('/api/media/sync/groups')).json(); cachedZones = zj.success ? (zj.groups||[]) : []; }
    catch(e) { cachedZones = []; }
    // Presence users feed the Request step's To/From dropdowns. Optional for
    // the same reason: no presence users just means a free-text field.
    try {
        const uj = await (await fetch('/api/presence/users')).json();
        cachedPresenceUsers = (uj.users||[]).filter(u=>u.enabled!==false);
    } catch(e) { cachedPresenceUsers = []; }
    // Named places label the zone (enter/leave) pickers. Optional: without them
    // the pickers fall back to raw place ids.
    try {
        const plj = await (await fetch('/api/places')).json();
        cachedPlaces = plj.places || [];
    } catch(e) { cachedPlaces = []; }
}

// RULES LIST

function _renderRules(rules) {
    const el = document.getElementById('a-rules'); if (!el) return;
    if (!rules?.length) { el.innerHTML = `<div class="text-center text-muted py-4"><i class="fas fa-robot fa-2x mb-2 d-block opacity-50"></i>No rules.</div>`; return; }
    let h = '';
    rules.forEach(rule => {
        const en = rule.enabled !== false;
        const nm = rule.name ? `<strong>${rule.name}</strong> ` : '';
        const st = rule._state||'unknown';
        const stB = st==='matched'?'<span class="badge bg-success ms-1">matched</span>':st==='unmatched'?'<span class="badge bg-secondary ms-1">unmatched</span>':'<span class="badge bg-dark ms-1">init</span>';
        const run = rule._running?'<span class="badge bg-warning text-dark ms-1">⏳</span>':'';

        let cH = '';
        const isOr = rule.condition_logic === 'or';
        (rule.conditions||[]).forEach((c,i) => {
            const p = i===0 ? '<strong class="text-primary">IF</strong>'
                : (isOr ? '<strong style="color:#6f42c1">OR</strong>'
                        : '<strong class="text-warning">AND</strong>');
            let cDesc;
            if (c.type === 'time_window') {
                const DAY_NAMES = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
                const dayStr = (!c.days || c.days.length === 7) ? 'Every day' : c.days.map(d => DAY_NAMES[d]).join(', ');
                const neg = c.negate ? '<span class="badge bg-danger ms-1">NOT</span>' : '';
                cDesc = `${neg} Time <code>${c.time_from} → ${c.time_to}</code> <span class="text-muted">${dayStr}</span>`;
            } else if (c.type === 'time') {
                const DAY_NAMES = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
                const dayStr = (!c.days || c.days.length === 7) ? 'Every day' : c.days.map(d => DAY_NAMES[d]).join(', ');
                cDesc = `⏰ Alarm <code>${c.at}</code> <span class="text-muted">${dayStr}</span>`;
            } else if (c.type === 'zone') {
                cDesc = `${c.event==='leave'?'🚶 Leaves':'📍 Enters'} <code>${_placeLabel(c.place)}</code>`;
            } else if (c.type === 'sun') {
                cDesc = _sunDesc(c);
            } else {
                const sus = c.sustain?`<span class="badge bg-info text-dark ms-1">⏱${c.sustain}s</span>`:'';
                const dispVal = Array.isArray(c.value) ? c.value.join(', ') : c.value;
                cDesc = `<code>${c.attribute}</code> ${OP[c.operator]||c.operator} <code>${dispVal}</code>${sus}`;
            }
            cH += `<div class="small">${p} ${cDesc}</div>`;
        });
        (rule.prerequisites||[]).forEach(p => {
            const neg = p.negate?'<span class="badge bg-danger ms-1">NOT</span>':'';
            let pDesc;
            if (p.type === 'time_window') {
                const DAY_NAMES = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
                const dayStr = (!p.days || p.days.length === 7) ? 'Every day' : p.days.map(d => DAY_NAMES[d]).join(', ');
                pDesc = `<code>${p.time_from} → ${p.time_to}</code> <span class="text-muted">${dayStr}</span>`;
            } else if (p.type === 'sun') {
                pDesc = _sunDesc(p);
            } else {
                pDesc = `${p.device_name||p.ieee} <code>${p.attribute}</code> ${OP[p.operator]||p.operator} <code>${p.value}</code>`;
            }
            cH += `<div class="small"><strong class="text-info">CHECK</strong>${neg} ${pDesc}</div>`;
        });
        const tH = _seqSummary(rule.then_sequence||[], 'THEN', 'success');
        const eH = _seqSummary(rule.else_sequence||[], 'ELSE', 'danger');

        h += `<div class="card mb-2 ${en?'':'opacity-50'}"><div class="card-body py-2 px-3">
            <div class="d-flex justify-content-between align-items-start">
                <div class="flex-grow-1"><div class="mb-1">${nm}<code class="text-muted small">${rule.id}</code>${stB}${run}</div>${cH}${tH}${eH}</div>
                <div class="d-flex gap-1 ms-2">
                    <span class="badge bg-secondary">${rule.cooldown||5}s</span>
                    <button class="btn btn-sm btn-outline-secondary" onclick="window._aTraceR('${rule.id}')"><i class="fas fa-search"></i></button>
                    <button class="btn btn-sm btn-outline-primary" onclick="window._aEdit('${rule.id}')"><i class="fas fa-edit"></i></button>
                    <button class="btn btn-sm ${en?'btn-outline-success':'btn-outline-secondary'}" onclick="window._aToggle('${rule.id}')"><i class="fas fa-${en?'toggle-on':'toggle-off'}"></i></button>
                    <button class="btn btn-sm btn-outline-danger" onclick="window._aDel('${rule.id}')"><i class="fas fa-trash"></i></button>
                    <button class="btn btn-sm btn-outline-info" onclick="window._aDownloadJson('${rule.id}')" title="Download JSON"><i class="fas fa-download"></i></button>
                </div>
            </div>
        </div></div>`;
    });
    el.innerHTML = h;
}

function _seqSummary(steps, label, color) {
    if (!steps.length) return '';
    const parts = steps.map(s => {
        if (s.type==='command') return `<span class="badge bg-info text-dark">${s.command}${s.value!=null?' ='+s.value:''}</span> <small class="text-muted">${s.target_name||s.target_ieee||'?'}</small>`;
        if (s.type==='delay') return `<span class="badge bg-warning text-dark">⏱${s.seconds}s</span>`;
        if (s.type==='wait_for') return `<span class="badge bg-secondary">⏳ ${s.device_name||s.ieee||'?'} ${s.attribute}</span>`;
        if (s.type==='condition') return `<span class="badge bg-dark">🔒 ${s.device_name||s.ieee||'?'} ${s.attribute}</span>`;
        if (s.type==='if_then_else') return `<span class="badge bg-purple" style="background:#6f42c1">IF/THEN/ELSE</span>`;
        if (s.type==='parallel') return `<span class="badge bg-dark">⚡ PARALLEL(${(s.branches||[]).length})</span>`;
        if (s.type==='media') return `<span class="badge" style="background:#0a9396">♪ ${_mediaDesc(s)}</span>`;
        if (s.type==='request') return `<span class="badge" style="background:#9d4edd">✉ ${s.to_user||'?'}</span>`;
        if (s.type==='offer') return `<span class="badge" style="background:#d4a017">? ask ${s.to_user||'?'} (${(s.accept_steps||[]).length} on yes)</span>`;
        return '';
    }).join(' <i class="fas fa-arrow-right text-muted small"></i> ');
    return `<div class="small mt-1"><strong class="text-${color}">${label}</strong> ${parts}</div>`;
}

// FORM

function _showForm(rule, forceNew = false) {
    const isE = !!rule; editingRuleId = (isE && !forceNew) ? rule.id : null;
    const el = document.getElementById('a-form'); if (!el) return;
    el.innerHTML = `
    <div class="card-header bg-light d-flex justify-content-between"><strong><i class="fas fa-${isE?'edit':'gears'}"></i> ${isE?'Edit':'New'} Automation</strong>
        <button class="btn btn-sm btn-outline-secondary" onclick="window._aHideForm()"><i class="fas fa-times"></i></button></div>
    <div class="card-body">
        <!-- What the rule currently says, in the same words the rules list uses.
             Kept at the top and refreshed on every edit: the form shows the
             parts, and this shows whether they still add up to what was meant. -->
        <div class="a-preview mb-3" id="a-preview"></div>
        <div class="mb-3"><label class="form-label small text-muted mb-0">Rule Name</label>
            <input type="text" class="form-control form-control-sm" id="a-name" value="${isE?(rule.name||''):''}"></div>
        <div class="mb-3"><div class="d-flex justify-content-between align-items-center mb-1"><label class="form-label fw-bold small mb-0">Trigger Conditions</label>
            <div class="d-flex align-items-center gap-2">
                <select class="form-select form-select-sm" id="a-clogic" style="width:150px" title="How the conditions below combine" onchange="window._aCLogic(this.value)">
                    <option value="and">Match ALL (AND)</option>
                    <option value="or">Match ANY (OR)</option>
                </select>
                <button class="btn btn-sm btn-outline-primary" onclick="window._aAddCond()"><i class="fas fa-plus"></i></button>
            </div></div><div id="cb"></div></div>
        <div class="mb-3 a-optional" id="a-prereq-sec"><div class="d-flex justify-content-between mb-1"><label class="form-label fw-bold small mb-0">Prerequisites <span class="text-muted fw-normal">(optional, supports NOT)</span></label>
            <button class="btn btn-sm btn-outline-info" onclick="window._aAddPrereq()"><i class="fas fa-plus"></i></button></div><div id="pb"></div></div>
        <div class="mb-3"><label class="form-label fw-bold small text-success">THEN sequence <span class="fw-normal text-muted">(conditions become true)</span></label>
            <div id="then-b"></div>${_addBtns('then')}</div>
        <div class="mb-3 a-optional" id="a-else-sec"><label class="form-label fw-bold small text-danger">ELSE sequence <span class="fw-normal text-muted">(conditions become false)</span></label>
            <div id="a-else-note" class="small text-warning mb-1" style="display:none"><i class="fas fa-info-circle"></i> A zone trigger fires on the crossing itself, so this rule only runs its THEN sequence. For the opposite crossing, add a second rule with <strong>Leaves</strong>.</div>
            <div id="else-b"></div>${_addBtns('else')}</div>
        <div class="row g-2 mb-3 align-items-end">
            <div class="col-6 col-md-3"><label class="form-label small text-muted mb-0">Cooldown (s)</label><input type="number" class="form-control form-control-sm" id="a-cd" value="${isE?(rule.cooldown||5):5}" min="0"></div>
            <div class="col-6 col-md-4">
                <button class="btn btn-sm btn-outline-secondary w-100" onclick="window._aToggleOptional()" id="a-opt-btn">
                    <i class="fas fa-sliders"></i> More options</button></div>
        </div>
        <div class="a-savebar">
            <button class="btn btn-outline-secondary btn-sm" onclick="window._aHideForm()">Cancel</button>
            <button class="btn btn-primary btn-sm" onclick="window._aSave()"><i class="fas fa-save"></i> ${isE?'Update':'Save'}</button>
        </div>
    </div>`;
    el.style.display = 'block';

    // One delegated listener rather than a handler per control: the builder
    // creates its widgets dynamically, so anything bound per-widget would miss
    // the ones added later.
    el.addEventListener('input', window._aPreview);
    el.addEventListener('change', window._aPreview);

    // Conditions
    condRows=[]; condIdC=0;
    condLogic = (isE && rule.condition_logic === 'or') ? 'or' : 'and';
    const clSel = document.getElementById('a-clogic'); if(clSel) clSel.value = condLogic;
    if(isE && rule.conditions?.length) rule.conditions.forEach(()=>condRows.push(condIdC++));
    else condRows.push(condIdC++);
    _refConds();
    if(isE && rule.conditions) setTimeout(()=>{
        rule.conditions.forEach((c,i)=>{if(condRows[i]!==undefined)_setC(condRows[i],c);});
        _refCondChrome();
    },50);

    // Prerequisites
    prereqRows=[]; prereqIdC=0;
    if(isE && rule.prerequisites?.length) rule.prerequisites.forEach(()=>prereqRows.push(prereqIdC++));
    _refPrereqs();
    if(isE && rule.prerequisites) setTimeout(()=>rule.prerequisites.forEach((p,i)=>{if(prereqRows[i]!==undefined)_setP(prereqRows[i],p);}),100);

    // Step trees
    stepIdC = 0;
    thenTree = isE ? _cloneSteps(rule.then_sequence||[]) : [];
    elseTree = isE ? _cloneSteps(rule.else_sequence||[]) : [];
    _renderStepTree('then');
    _renderStepTree('else');

    _syncOptionalSections();
    // The condition rows are populated on a 50ms timer above, so the first
    // preview waits for them rather than rendering an empty rule and flicking.
    setTimeout(_refreshPreview, 80);
}

function _cloneSteps(steps) {
    return steps.map(s => {
        const c = {...s, _id: _uid()};
        if(c.then_steps) c.then_steps = _cloneSteps(c.then_steps);
        if(c.else_steps) c.else_steps = _cloneSteps(c.else_steps);
        if(c.branches) c.branches = c.branches.map(b=>_cloneSteps(b));
        if(c.accept_steps) c.accept_steps = _cloneSteps(c.accept_steps);
        if(c.inline_conditions) c.inline_conditions = c.inline_conditions.map(ic=>({...ic, _id:_uid()}));
        return c;
    });
}

// The nine step types, as one palette. Rendered once per sequence and kept
// shut: eighteen buttons framing a rule with two steps read as chrome, not as
// choices, and the step being added is nearly always a command.
const STEP_PALETTE = [
    ['command',      'fa-bolt',             'Command',      'btn-outline-success'],
    ['delay',        'fa-clock',            'Delay',        'btn-outline-warning'],
    ['wait_for',     'fa-hourglass-half',   'Wait for',     'btn-outline-secondary'],
    ['condition',    'fa-filter',           'Gate',         'btn-outline-dark'],
    ['media',        'fa-music',            'Media',        'btn-outline-info'],
    ['request',      'fa-comment',          'Message',      'btn-outline-secondary'],
    ['offer',        'fa-circle-question',  'Ask first',    'btn-outline-warning'],
    ['if_then_else', 'fa-code-branch',      'If / Else',    'btn-outline-primary'],
    ['parallel',     'fa-columns',          'Together',     'btn-outline-info'],
];

function _addBtns(path) {
    const id = `pal-${String(path).replace(/[^a-z0-9-]/gi, '')}`;
    const buttons = STEP_PALETTE.map(([type, icon, label, cls]) =>
        `<button class="btn btn-sm ${cls}" onclick="window._aAddStep('${path}','${type}')"`
        + (type === 'offer' ? ' title="Ask somebody, and only act if they say yes"' : '')
        + `><i class="fas ${icon} me-1"></i>${label}</button>`).join('');
    return `<button class="btn btn-sm btn-outline-primary a-addstep mt-1"
                    onclick="window._aTogglePalette('${id}', this)">
                <i class="fas fa-plus me-1"></i>Add step
            </button>
            <div class="a-palette" id="${id}">${buttons}</div>`;
}

/** Open or shut one sequence's step palette. */
window._aTogglePalette = (id, btn) => {
    const pal = document.getElementById(id);
    if (!pal) return;
    const open = pal.classList.toggle('open');
    btn.innerHTML = open
        ? '<i class="fas fa-xmark me-1"></i>Close'
        : '<i class="fas fa-plus me-1"></i>Add step';
};

// VALUE INPUT

// Add 'idAttr' parameter to handle both data-sid and data-icid
function _vI(cls, id, opts, cur, idAttr = 'data-id') {
    if(opts?.length) {
        // pass the specific class (s-vl or ic-vl) and the ID attribute
        let h=`<select class="form-select form-select-sm ${cls}" ${idAttr}="${id}">`;
        opts.forEach(v=>{
            h+=`<option value="${v}" ${cur!==undefined&&String(cur).toLowerCase()===String(v).toLowerCase()?'selected':''}>${v}</option>`;
        });
        return h+'</select>';
    }
    return `<input type="text" class="form-control form-control-sm ${cls}" ${idAttr}="${id}" placeholder="Value" value="${cur!==undefined?cur:''}">`;
}

// CONDITIONS + PREREQUISITES (same pattern as before)

// Joiner badge shown on the 2nd+ condition row — OR gets its own colour so a
// glance at the rows tells you which way the rule combines.
const _joinBadge = () => condLogic === 'or'
    ? `<span class="badge small" style="background:#6f42c1">OR</span>`
    : `<span class="badge bg-warning text-dark small">AND</span>`;

function _renderCond(id, ctype) {
    // Default new conditions on the virtual time source to an alarm (no attrs exist).
    ctype = ctype || (currentSourceIeee === '__time__' ? 'time' : 'attribute');
    const opts = _attrOptions(cachedAttributes, _dtype(currentSourceIeee));
    const idx=condRows.indexOf(id);
    const badge = idx===0 ? `<span class="badge bg-primary small">IF</span>` : _joinBadge();
    const rmBtn = idx>0 ? `<button class="btn btn-sm btn-outline-danger" onclick="window._aRmC(${id})"><i class="fas fa-times"></i></button>` : '<div style="width:31px"></div>';
    const DAYS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    const dayBoxes = DAYS.map((d,i) => `<label class="me-1 small"><input type="checkbox" class="ctd" data-id="${id}" data-day="${i}" checked> ${d}</label>`).join('');
    const attrRow = `
        <div class="col"><select class="form-select form-select-sm ca" data-id="${id}" onchange="window._aCa(${id},this)"><option value="">Attr...</option>${opts}</select></div>
        <div class="col-auto"><select class="form-select form-select-sm co" data-id="${id}" style="width:120px"><option value="">Op...</option></select></div>
        <div class="col" id="cv-${id}"><input type="text" class="form-control form-control-sm cv" data-id="${id}" placeholder="Value"></div>
        <div class="col-auto" style="width:65px"><input type="number" class="form-control form-control-sm cs" data-id="${id}" placeholder="⏱s" min="0"></div>`;
    const timeRow = `
        <div class="col-auto"><div class="form-check form-check-inline mb-0"><input class="form-check-input cn" type="checkbox" data-id="${id}" title="NOT (negate)"><label class="form-check-label small text-danger">NOT</label></div></div>
        <div class="col-auto"><label class="small text-muted mb-0 me-1">From</label><input type="time" class="form-control form-control-sm ct-from" data-id="${id}" style="width:110px" value="00:00"></div>
        <div class="col-auto"><label class="small text-muted mb-0 me-1">To</label><input type="time" class="form-control form-control-sm ct-to" data-id="${id}" style="width:110px" value="23:59"></div>
        <div class="col"><div class="d-flex flex-wrap gap-1 align-items-center pt-1">${dayBoxes}</div></div>`;
    const sunSel = (cls, sel) => `<select class="form-select form-select-sm ${cls}" data-id="${id}" style="width:115px">${_SUN_OPTS.map(([v,l])=>`<option value="${v}" ${sel===v?'selected':''}>${l}</option>`).join('')}</select>`;
    const sunRow = `
        <div class="col-auto"><div class="form-check form-check-inline mb-0"><input class="form-check-input cn" type="checkbox" data-id="${id}" title="NOT (negate)"><label class="form-check-label small text-danger">NOT</label></div></div>
        <div class="col-auto"><label class="small text-muted mb-0 me-1">From</label>${sunSel('cs-from','sunset')}</div>
        <div class="col-auto" style="width:78px"><input type="number" class="form-control form-control-sm cs-off-from" data-id="${id}" placeholder="±min" title="offset minutes"></div>
        <div class="col-auto"><label class="small text-muted mb-0 me-1">To</label>${sunSel('cs-to','sunrise')}</div>
        <div class="col-auto" style="width:78px"><input type="number" class="form-control form-control-sm cs-off-to" data-id="${id}" placeholder="±min" title="offset minutes"></div>`;
    const alarmRow = `
        <div class="col-auto"><label class="small text-muted mb-0 me-1">At</label><input type="time" class="form-control form-control-sm ct-at" data-id="${id}" style="width:120px" value="07:00"></div>
        <div class="col"><div class="d-flex flex-wrap gap-1 align-items-center pt-1">${dayBoxes}</div></div>`;
    const zoneRow = `
        <div class="col-auto"><select class="form-select form-select-sm cz-ev" data-id="${id}" style="width:110px">
            <option value="enter">Enters</option><option value="leave">Leaves</option></select></div>
        <div class="col"><div class="d-flex flex-wrap gap-1 align-items-center pt-1">${_placeBoxes(id)}</div></div>`;
    const body = ctype==='time_window' ? timeRow : ctype==='time' ? alarmRow
        : ctype==='sun' ? sunRow : ctype==='zone' ? zoneRow : attrRow;
    const zoneOpt = _isPerson(currentSourceIeee)
        ? `<option value="zone" ${ctype==='zone'?'selected':''}>Zone</option>` : '';
    return `<div class="row g-1 mb-1 align-items-center flex-wrap" id="c-${id}">
        <div class="col-auto">${badge}</div>
        <div class="col-auto"><select class="form-select form-select-sm ctype" data-id="${id}" style="width:90px" onchange="window._aCType(${id},this)"><option value="attribute" ${ctype==='attribute'?'selected':''}>Attr</option><option value="time" ${ctype==='time'?'selected':''}>Alarm</option><option value="time_window" ${ctype==='time_window'?'selected':''}>Time/Day</option><option value="sun" ${ctype==='sun'?'selected':''}>Sun</option>${zoneOpt}</select></div>
        <div style="display:contents">${body}</div>
        <div class="col-auto">${rmBtn}</div>
    </div>`;
}
function _refConds(){const el=document.getElementById('cb');if(el)el.innerHTML=condRows.map(id=>_renderCond(id)).join('');_refCondChrome();}

// Bits of the form that depend on which condition rows exist right now.
function _refCondChrome(){
    // The AND/OR picker only means something with 2+ conditions.
    const sel=document.getElementById('a-clogic');
    if(sel)sel.style.display=condRows.length>1?'':'none';
    // Zone rules never run their ELSE — say so where the ELSE steps are added.
    const note=document.getElementById('a-else-note');
    if(note){
        const hasZone=condRows.some(id=>document.querySelector(`.ctype[data-id="${id}"]`)?.value==='zone');
        note.style.display=hasZone?'':'none';
    }
}

// Swap the joiner badges in place — re-rendering the rows would wipe values.
function _refJoinBadges(){
    condRows.forEach((id,idx)=>{
        if(idx===0)return;
        const row=document.getElementById(`c-${id}`);
        const b=row?.querySelector('.badge');
        if(b)b.outerHTML=_joinBadge();
    });
}
function _setC(id,c){
    const ctype = c.type || 'attribute';
    const row = document.getElementById(`c-${id}`);
    if (!row) return;
    row.outerHTML = _renderCond(id, ctype);
    const r2 = document.getElementById(`c-${id}`);
    if (!r2) return;
    if (ctype === 'time_window') {
        const neg = r2.querySelector('.cn'); if (neg) neg.checked = !!c.negate;
        const tf = r2.querySelector('.ct-from'); if (tf) tf.value = c.time_from || '00:00';
        const tt = r2.querySelector('.ct-to');   if (tt) tt.value = c.time_to   || '23:59';
        const days = c.days ?? [0,1,2,3,4,5,6];
        r2.querySelectorAll('.ctd').forEach(cb => { cb.checked = days.includes(parseInt(cb.dataset.day)); });
    } else if (ctype === 'time') {
        const at = r2.querySelector('.ct-at'); if (at) at.value = c.at || '07:00';
        const days = c.days ?? [0,1,2,3,4,5,6];
        r2.querySelectorAll('.ctd').forEach(cb => { cb.checked = days.includes(parseInt(cb.dataset.day)); });
    } else if (ctype === 'zone') {
        const ev = r2.querySelector('.cz-ev'); if (ev) ev.value = c.event || 'enter';
        const want = new Set((Array.isArray(c.place) ? c.place : [c.place ?? 'any']).map(String));
        const box = r2.querySelector('.czp')?.closest('div');
        r2.querySelectorAll('.czp').forEach(cb => { cb.checked = want.has(cb.dataset.place); });
        // A place deleted since the rule was saved still appears, ticked. It is
        // part of what the rule says even though it can no longer match, and
        // dropping it silently would make the rule read narrower than it is.
        [...want].filter(p => !r2.querySelector(`.czp[data-place="${p}"]`)).forEach(p => {
            box?.insertAdjacentHTML('beforeend',
                `<label class="me-2 small text-nowrap text-danger"><input type="checkbox" class="czp" data-id="${id}" data-place="${p}" checked onchange="window._aCZP(${id},this)"> ${p} (deleted)</label>`);
        });
    } else if (ctype === 'sun') {
        const neg = r2.querySelector('.cn'); if (neg) neg.checked = !!c.negate;
        const sf = r2.querySelector('.cs-from'); if (sf) sf.value = c.from || 'sunset';
        const st = r2.querySelector('.cs-to');   if (st) st.value = c.to   || 'sunrise';
        const of = r2.querySelector('.cs-off-from'); if (of && c.offset_from) of.value = c.offset_from;
        const ot = r2.querySelector('.cs-off-to');   if (ot && c.offset_to)   ot.value = c.offset_to;
    } else {
        const s=r2.querySelector('.ca');if(!s)return;s.value=c.attribute;window._aCa(id,s);
        setTimeout(()=>{const o=r2.querySelector('.co');if(o){o.value=c.operator;if(o.onchange)o.onchange();}
            const v=r2.querySelector(`#cv-${id} .cv`);
            if(v){const dv=Array.isArray(c.value)?c.value.join(', '):String(c.value);v.value=dv;}
            const ss=r2.querySelector('.cs');if(ss&&c.sustain)ss.value=c.sustain;},20);
    }
}

function _renderPrereq(id, ptype) {
    ptype = ptype || 'device';
    const _filtP = cachedAllDevices.filter(d => d.ieee !== currentSourceIeee);
    const _devP  = _filtP.filter(d => !d._is_group);
    const _grpP  = _filtP.filter(d => d._is_group);
    let devs = _devP.map(d => `<option value="${d.ieee}">${d.friendly_name}</option>`).join('');
    if (_grpP.length) devs += `<optgroup label="── Groups ──">` + _grpP.map(d => `<option value="${d.ieee}">${d.friendly_name}</option>`).join('') + '</optgroup>';

    const DAYS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    const dayBoxes = DAYS.map((d,i) => `<label class="me-1 small"><input type="checkbox" class="ptd" data-id="${id}" data-day="${i}" checked> ${d}</label>`).join('');

    const deviceRow = `
        <div class="col"><select class="form-select form-select-sm pd" data-id="${id}" onchange="window._aPd(${id},this)"><option value="">Device...</option>${devs}</select></div>
        <div class="col"><select class="form-select form-select-sm pa" data-id="${id}" onchange="window._aPa(${id},this)"><option value="">Attr...</option></select></div>
        <div class="col-auto"><select class="form-select form-select-sm po" data-id="${id}" style="width:70px">${Object.entries(OP).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></div>
        <div class="col" id="pv-${id}"><input type="text" class="form-control form-control-sm pv" data-id="${id}" placeholder="Value"></div>`;

    const timeRow = `
        <div class="col-auto"><label class="small text-muted mb-0 me-1">From</label><input type="time" class="form-control form-control-sm pt-from" data-id="${id}" style="width:110px" value="00:00"></div>
        <div class="col-auto"><label class="small text-muted mb-0 me-1">To</label><input type="time" class="form-control form-control-sm pt-to" data-id="${id}" style="width:110px" value="23:59"></div>
        <div class="col"><div class="d-flex flex-wrap gap-1 align-items-center pt-1">${dayBoxes}</div></div>`;
    const sunSel = (cls, sel) => `<select class="form-select form-select-sm ${cls}" data-id="${id}" style="width:115px">${_SUN_OPTS.map(([v,l])=>`<option value="${v}" ${sel===v?'selected':''}>${l}</option>`).join('')}</select>`;
    const sunRow = `
        <div class="col-auto"><label class="small text-muted mb-0 me-1">From</label>${sunSel('ps-from','sunset')}</div>
        <div class="col-auto" style="width:78px"><input type="number" class="form-control form-control-sm ps-off-from" data-id="${id}" placeholder="±min"></div>
        <div class="col-auto"><label class="small text-muted mb-0 me-1">To</label>${sunSel('ps-to','sunrise')}</div>
        <div class="col-auto" style="width:78px"><input type="number" class="form-control form-control-sm ps-off-to" data-id="${id}" placeholder="±min"></div>`;
    const body = ptype === 'time_window' ? timeRow : ptype === 'sun' ? sunRow : deviceRow;

    return `<div class="row g-1 mb-1 align-items-center flex-wrap" id="p-${id}">
        <div class="col-auto"><span class="badge bg-info text-dark small">CHECK</span></div>
        <div class="col-auto"><div class="form-check form-check-inline mb-0"><input class="form-check-input pn" type="checkbox" data-id="${id}" title="NOT (negate)"><label class="form-check-label small text-danger">NOT</label></div></div>
        <div class="col-auto">
            <select class="form-select form-select-sm ptype" data-id="${id}" style="width:90px" onchange="window._aPType(${id},this)">
                <option value="device" ${ptype==='device'?'selected':''}>Device</option>
                <option value="time_window" ${ptype==='time_window'?'selected':''}>Time/Day</option>
                <option value="sun" ${ptype==='sun'?'selected':''}>Sun</option>
            </select>
        </div>
        <div class="prereq-body-${id}" style="display:contents">${body}</div>
        <div class="col-auto"><button class="btn btn-sm btn-outline-danger" onclick="window._aRmP(${id})"><i class="fas fa-times"></i></button></div>
    </div>`;
}

function _refPrereqs() {
    const el = document.getElementById('pb');
    if (el) el.innerHTML = prereqRows.map(id => _renderPrereq(id)).join('');
}

function _setP(id, p) {
    const ptype = p.type || 'device';
    // Rebuild row with correct type first
    const row = document.getElementById(`p-${id}`);
    if (!row) return;
    row.outerHTML = _renderPrereq(id, ptype);
    const r2 = document.getElementById(`p-${id}`);
    if (!r2) return;

    const neg = r2.querySelector(`.pn`); if (neg) neg.checked = !!p.negate;

    if (ptype === 'time_window') {
        const tf = r2.querySelector('.pt-from'); if (tf) tf.value = p.time_from || '00:00';
        const tt = r2.querySelector('.pt-to');   if (tt) tt.value = p.time_to   || '23:59';
        const days = p.days ?? [0,1,2,3,4,5,6];
        r2.querySelectorAll('.ptd').forEach(cb => {
            cb.checked = days.includes(parseInt(cb.dataset.day));
        });
    } else if (ptype === 'sun') {
        const sf = r2.querySelector('.ps-from'); if (sf) sf.value = p.from || 'sunset';
        const st = r2.querySelector('.ps-to');   if (st) st.value = p.to   || 'sunrise';
        const of = r2.querySelector('.ps-off-from'); if (of && p.offset_from) of.value = p.offset_from;
        const ot = r2.querySelector('.ps-off-to');   if (ot && p.offset_to)   ot.value = p.offset_to;
    } else {
        const d = r2.querySelector('.pd'); if (d) { d.value = p.ieee; window._aPd(id, d); }
        setTimeout(() => {
            const a = r2.querySelector('.pa'); if (a) { a.value = p.attribute; }
            const o = r2.querySelector('.po'); if (o) { o.value = p.operator; if (o.onchange) o.onchange(); }
            const v = r2.querySelector(`#pv-${id} .pv`); if (v) v.value = Array.isArray(p.value) ? p.value.join(', ') : String(p.value);
        }, 50);
    }
}

// STEP TREE RENDERER (recursive)

function _renderStepTree(path) {
    const el = document.getElementById(`${path}-b`); if(!el) return;
    const tree = path==='then'?thenTree:elseTree;
    if(!tree.length){el.innerHTML='<div class="text-muted small fst-italic">No steps.</div>';return;}
    el.innerHTML = tree.map((s,i)=>_renderStep(s, path, i, tree.length)).join('');
    // Init dynamic selects after DOM render
    requestAnimationFrame(()=>_initStepSelects(tree, path));
}

function _renderStep(step, path, idx, total) {
    const sid = step._id;
    const ic = SICON[step.type]||'fa-cog';
    const lb = SLBL[step.type]||step.type;
    let body = '';

    if(step.type==='command') {
            const _devActs=cachedActuators.filter(d=>!d._is_group);
            const _grpActs=cachedActuators.filter(d=>d._is_group);
            let acts=_devActs.map(d=>`<option value="${d.ieee}" data-cmds='${JSON.stringify(d.commands)}' ${step.target_ieee===d.ieee?'selected':''}>${d.friendly_name}</option>`).join('');
            if(_grpActs.length){acts+=`<optgroup label="── Groups ──">`+_grpActs.map(d=>`<option value="${d.ieee}" data-cmds='${JSON.stringify(d.commands)}' ${step.target_ieee===d.ieee?'selected':''}>${d.friendly_name}</option>`).join('')+'</optgroup>';}
        body=`<div class="row g-1"><div class="col-md-5"><select class="form-select form-select-sm s-tgt" data-sid="${sid}" onchange="window._aSTC(${sid},this)"><option value="">Target...</option>${acts}</select></div>
            <div class="col-md-4"><select class="form-select form-select-sm s-cmd" data-sid="${sid}"><option value="">Cmd...</option></select></div>
            <div class="col-md-3"><input type="text" class="form-control form-control-sm s-val" data-sid="${sid}" placeholder="Value" value="${step.value!=null?step.value:''}"></div></div>
            <input type="hidden" class="s-ep" data-sid="${sid}" value="${step.endpoint_id||''}">`;
    } else if(step.type==='delay') {
        body=`<div class="d-flex gap-1 align-items-center"><input type="number" class="form-control form-control-sm s-sec" data-sid="${sid}" value="${step.seconds||5}" min="1" style="width:80px"><span class="small">seconds</span></div>`;
    } else if(step.type==='wait_for'||step.type==='condition') {
            const _devW=cachedAllDevices.filter(d=>!d._is_group);
            const _grpW=cachedAllDevices.filter(d=>d._is_group);
            let devs=_devW.map(d=>`<option value="${d.ieee}" ${step.ieee===d.ieee?'selected':''}>${d.friendly_name}</option>`).join('');
            if(_grpW.length){devs+=`<optgroup label="── Groups ──">`+_grpW.map(d=>`<option value="${d.ieee}" ${step.ieee===d.ieee?'selected':''}>${d.friendly_name}</option>`).join('')+'</optgroup>';}
        const neg = step.type==='condition'||step.type==='wait_for'?`<div class="form-check form-check-inline mb-0"><input class="form-check-input s-neg" type="checkbox" data-sid="${sid}" ${step.negate?'checked':''}><label class="small text-danger">NOT</label></div>`:'';
        const tout = step.type==='wait_for'?`<input type="number" class="form-control form-control-sm s-tout" data-sid="${sid}" value="${step.timeout||300}" min="1" style="width:65px" title="Timeout(s)">`:'';
        body=`<div class="row g-1 align-items-center"><div class="col-auto">${neg}</div><div class="col"><select class="form-select form-select-sm s-ieee" data-sid="${sid}" onchange="window._aSDC(${sid},this)"><option value="">Device...</option>${devs}</select></div>
            <div class="col"><select class="form-select form-select-sm s-attr" data-sid="${sid}"><option value="">Attr...</option></select></div>
            <div class="col-auto"><select class="form-select form-select-sm s-op" data-sid="${sid}" style="width:120px">${_opOpts(step.operator||'')}</select></div>
            <div class="col" id="sv-${sid}"><input type="text" class="form-control form-control-sm s-vl" data-sid="${sid}" placeholder="Value" value="${step.value!=null?step.value:''}"></div>
            ${tout?`<div class="col-auto">${tout}</div>`:''}</div>`;
    } else if(step.type==='if_then_else') {
        const logic = step.condition_logic||'and';
        const ics = step.inline_conditions||[];
        let icH = ics.map((ic,j)=>_renderInlineCond(ic,j,sid,ics.length)).join('');
        const showLogic = ics.length > 1;
        body=`<div class="mb-2"><div class="d-flex gap-2 align-items-center mb-1">
            <span class="small fw-bold">IF</span>
            <select class="form-select form-select-sm s-logic" data-sid="${sid}" style="width:70px${showLogic?'':';display:none'}"><option value="and" ${logic==='and'?'selected':''}>AND</option><option value="or" ${logic==='or'?'selected':''}>OR</option></select>
            <button class="btn btn-sm btn-outline-primary py-0" onclick="window._aAddIC(${sid})"><i class="fas fa-plus"></i></button></div>
            <div id="ic-${sid}">${icH}</div></div>
        <div class="border-start border-success border-3 ps-2 mb-2"><div class="small fw-bold text-success mb-1">THEN</div><div id="ite-then-${sid}">${(step.then_steps||[]).map((s,i)=>_renderStep(s,`ite-then-${sid}`,i,(step.then_steps||[]).length)).join('')}</div>
            ${_addBtns(`ite-then-${sid}`)}</div>
        <div class="border-start border-danger border-3 ps-2"><div class="small fw-bold text-danger mb-1">ELSE</div><div id="ite-else-${sid}">${(step.else_steps||[]).map((s,i)=>_renderStep(s,`ite-else-${sid}`,i,(step.else_steps||[]).length)).join('')}</div>
            ${_addBtns(`ite-else-${sid}`)}</div>`;
    } else if(step.type==='parallel') {
        const branches = step.branches||[[], []];
        body = branches.map((br,bi)=>`<div class="border-start border-info border-3 ps-2 mb-2"><div class="small fw-bold text-info mb-1">Branch ${bi+1}</div>
            <div id="par-${sid}-${bi}">${br.map((s,i)=>_renderStep(s,`par-${sid}-${bi}`,i,br.length)).join('')}</div>
            ${_addBtns(`par-${sid}-${bi}`)}</div>`).join('');
        body += `<button class="btn btn-sm btn-outline-info" onclick="window._aAddBranch(${sid})"><i class="fas fa-plus"></i> Branch</button>`;
    } else if(step.type==='media') {
        // Wrapped so switching target can rebuild the whole body — a zone and
        // a speaker do not offer the same actions.
        body = `<div id="step-body-${sid}">${_mediaStepBody(step, sid)}</div>`;
    } else if(step.type==='request') {
        // A message into the user-to-user messaging system (step type keeps
        // its historical name for saved rules). Lands in the recipient's
        // thread and goes out as a phone-waking web push. to/from are LOGIN
        // accounts — push subscriptions key on the username.
        const userOpt = (u, sel) => {
            const account = u.account || u.user_id;
            return `<option value="${account}" ${sel===account?'selected':''}>${u.display_name||u.user_id}</option>`;
        };
        const toOpts = cachedPresenceUsers.map(u=>userOpt(u, step.to_user)).join('');
        const fromOpts = cachedPresenceUsers.map(u=>userOpt(u, step.from_user)).join('');
        const toSel = cachedPresenceUsers.length
            ? `<select class="form-select form-select-sm s-rq-to" data-sid="${sid}"><option value="">To…</option>${toOpts}</select>`
            : `<input type="text" class="form-control form-control-sm s-rq-to" data-sid="${sid}" placeholder="To (username)" value="${step.to_user||''}">`;
        const fromSel = cachedPresenceUsers.length
            ? `<select class="form-select form-select-sm s-rq-from" data-sid="${sid}"><option value="">From: ZMM</option>${fromOpts}</select>`
            : `<input type="text" class="form-control form-control-sm s-rq-from" data-sid="${sid}" placeholder="From (optional)" value="${step.from_user||''}">`;
        body=`<div class="row g-1 align-items-center mb-1">
            <div class="col-md-6">${toSel}</div>
            <div class="col-md-6">${fromSel}</div></div>
            <input type="text" class="form-control form-control-sm s-rq-msg" data-sid="${sid}" placeholder="Message, e.g. At the shops — need anything?" value="${step.message?String(step.message).replace(/"/g,'&quot;'):''}">`;
    } else if(step.type==='offer') {
        // A message that can act. The recipient pickers are the message step's;
        // what differs is the nested sequence, which runs only on Accept — so
        // the rule decides what happens, not whatever comes back over the wire.
        const userOpt = (u, sel) => {
            const account = u.account || u.user_id;
            return `<option value="${account}" ${sel===account?'selected':''}>${u.display_name||u.user_id}</option>`;
        };
        const toOpts = cachedPresenceUsers.map(u=>userOpt(u, step.to_user)).join('');
        const toSel = cachedPresenceUsers.length
            ? `<select class="form-select form-select-sm s-of-to" data-sid="${sid}"><option value="">Ask…</option>${toOpts}</select>`
            : `<input type="text" class="form-control form-control-sm s-of-to" data-sid="${sid}" placeholder="Ask (username)" value="${step.to_user||''}">`;
        const mins = Math.round((step.expires_in||3600)/60);
        body=`<div class="row g-1 align-items-center mb-1">
            <div class="col-md-7">${toSel}</div>
            <div class="col-md-5"><div class="input-group input-group-sm">
                <span class="input-group-text">Expires</span>
                <input type="number" class="form-control s-of-exp" data-sid="${sid}" min="1" max="1440" value="${mins}" title="Minutes before the offer lapses unanswered">
                <span class="input-group-text">min</span></div></div></div>
            <input type="text" class="form-control form-control-sm s-of-msg mb-2" data-sid="${sid}" placeholder="Question, e.g. It's cooler outside — open up or run the AC?" value="${step.message?String(step.message).replace(/"/g,'&quot;'):''}">
            <div class="border-start border-warning border-3 ps-2"><div class="small fw-bold text-warning mb-1">IF THEY ACCEPT</div>
                <div id="ofa-${sid}">${(step.accept_steps||[]).map((s2,i)=>_renderStep(s2,`ofa-${sid}`,i,(step.accept_steps||[]).length)).join('')}</div>
                ${_addBtns(`ofa-${sid}`)}</div>`;
    }

    return `<div class="card card-body bg-light p-2 mb-1" id="step-${sid}">
        <div class="d-flex justify-content-between align-items-center mb-1">
            <span class="badge bg-dark"><i class="fas ${ic}"></i> ${lb} <small>${idx+1}/${total}</small></span>
            <button class="btn btn-sm btn-outline-danger py-0 px-1" onclick="window._aRmStep(${sid},'${path}')"><i class="fas fa-times"></i></button>
        </div>${body}</div>`;
}

function _renderInlineCond(ic, idx, parentSid, total) {
    const _devIC=cachedAllDevices.filter(d=>!d._is_group);
    const _grpIC=cachedAllDevices.filter(d=>d._is_group);
    let devs=_devIC.map(d=>`<option value="${d.ieee}" ${ic.ieee===d.ieee?'selected':''}>${d.friendly_name}</option>`).join('');
    if(_grpIC.length){devs+=`<optgroup label="── Groups ──">`+_grpIC.map(d=>`<option value="${d.ieee}" ${ic.ieee===d.ieee?'selected':''}>${d.friendly_name}</option>`).join('')+'</optgroup>';}
    const icId = ic._id;
    const rmBtn = total > 1 ? `<button class="btn btn-sm btn-outline-danger py-0" onclick="window._aRmIC(${parentSid},${icId})"><i class="fas fa-times"></i></button>` : '';
    return `<div class="row g-1 mb-1 align-items-center" id="ic-row-${icId}">
        <div class="col-auto"><div class="form-check form-check-inline mb-0"><input class="form-check-input ic-neg" type="checkbox" data-icid="${icId}" ${ic.negate?'checked':''}><label class="small text-danger">NOT</label></div></div>
        <div class="col"><select class="form-select form-select-sm ic-ieee" data-icid="${icId}" onchange="window._aICDev(${icId},this)"><option value="">Device...</option>${devs}</select></div>
        <div class="col"><select class="form-select form-select-sm ic-attr" data-icid="${icId}"><option value="">Attr...</option></select></div>
        <div class="col-auto"><select class="form-select form-select-sm ic-op" data-icid="${icId}" style="width:120px">${_opOpts(ic.operator||'')}</select></div>
        <div class="col" id="icv-${icId}"><input type="text" class="form-control form-control-sm ic-vl" data-icid="${icId}" placeholder="Value" value="${ic.value!=null?ic.value:''}"></div>
        <div class="col-auto">${rmBtn}</div>
    </div>`;
}

// Media step rendering
function _mediaStepBody(step, sid) {
    const zone = isZoneId(step.player_id);
    const players = cachedPlayers.map(p=>`<option value="${p.player_id}" ${step.player_id===p.player_id?'selected':''}>${p.name}${p.is_group?' (group)':''}</option>`).join('');
    const zones = cachedZones.length
        ? `<optgroup label="── OpenZone ──">${cachedZones.map(z=>`<option value="zone:${z.id}" ${step.player_id==='zone:'+z.id?'selected':''}>${String(z.name).replace(/</g,'&lt;')} (${(z.members||[]).length} speakers)</option>`).join('')}</optgroup>`
        : '';
    // Changing the target changes which actions make sense, so re-render.
    const playerSel = `<select class="form-select form-select-sm s-mplayer" data-sid="${sid}" onchange="window._aMPlayer(${sid},this)"><option value="">Player…</option>${players}${zones}</select>`;
    const actSel = `<select class="form-select form-select-sm s-maction" data-sid="${sid}" onchange="window._aMAction(${sid},this)">${_mediaActionsFor(step.player_id).map(([v,l])=>`<option value="${v}" ${(step.media_action||'play_tidal')===v?'selected':''}>${l}</option>`).join('')}</select>`;
    const hint = cachedPlayers.length || cachedZones.length ? '' : `<div class="small text-warning mt-1">No media players found — is the media service enabled?</div>`;
    return `<div class="row g-1 mb-1"><div class="col-md-6">${playerSel}</div><div class="col-md-6">${actSel}</div></div>
        <div id="media-sub-${sid}">${_mediaSubHtml(step, sid)}</div>${zone?_zoneNote(step.player_id, sid):''}${hint}`;
}

/** Play Zone is offered only for a zone; a zone has no infinite-radio queue,
 *  so Tidal's Radio∞ mode is quietly absent from its sub-form instead. */
function _mediaActionsFor(pid) {
    return isZoneId(pid) ? MEDIA_ACTIONS : MEDIA_ACTIONS.filter(([v])=>v!=='play_zone');
}

/** What the zone would do if played now — the saved source and window are the
 *  reason a rule can just say "play it", so they belong in front of whoever is
 *  writing the rule. */
/** Every step in both sequences, in render order, descending into branches. */
function _allSteps(steps, out = []) {
    for (const st of steps || []) {
        out.push(st);
        _allSteps(st.then_steps, out);
        _allSteps(st.else_steps, out);
        _allSteps(st.accept_steps, out);
        for (const b of st.branches || []) _allSteps(b, out);
    }
    return out;
}

/**
 * Whether this is the first step in the rule to target this player.
 *
 * Derived from the step trees rather than tracked across renders, so it gives
 * the same answer however many times the form redraws.
 */
function _firstUseOfPlayer(pid, sid) {
    // THEN runs before ELSE, so it is read first.
    const first = _allSteps(elseTree, _allSteps(thenTree))
        .find(st => st.player_id === pid);
    return !first || first._id === sid;
}

function _zoneNote(pid, sid) {
    const z = zoneOf(pid);
    if (!z) return '';
    // Identical on every step that plays the same zone: informative once,
    // noise twice.
    if (sid !== undefined && !_firstUseOfPlayer(pid, sid)) return '';
    const p = z.play || {};
    const src = p.media
        ? String(p.media.title || p.media.url || p.media.station_uuid || 'a saved source').replace(/</g,'&lt;')
        : '';
    const dur = p.duration_s ? ` for ${Math.round(p.duration_s/60)} min` : ' until stopped';
    return src
        ? `<div class="small text-muted mt-1"><i class="fas fa-circle-info me-1"></i>Saved source: <em>${src}</em>${dur}. Change it under Media → OpenZone.</div>`
        : `<div class="small text-warning mt-1"><i class="fas fa-triangle-exclamation me-1"></i>This zone has no saved source yet — pick one under Media → OpenZone, or use Play Tidal / Play Radio here.</div>`;
}

function _mediaSubHtml(step, sid) {
    const a = step.media_action || 'play_tidal';
    const zone = isZoneId(step.player_id);
    if (a === 'play_zone')
        return '';                       // the zone's own config is the input
    if (a === 'control')
        return `<select class="form-select form-select-sm s-mctrl" data-sid="${sid}" style="max-width:170px">${(zone?ZONE_CONTROLS:MEDIA_CONTROLS).map(([v,l])=>`<option value="${v}" ${step.control_action===v?'selected':''}>${l}</option>`).join('')}</select>`;
    if (a === 'volume') {
        const pct = step.volume!=null ? Math.round(step.volume*100) : 30;
        return `<div class="d-flex gap-1 align-items-center"><input type="number" class="form-control form-control-sm s-mvol" data-sid="${sid}" value="${pct}" min="0" max="100" style="width:80px"><span class="small">% volume</span></div>`;
    }
    if (a === 'volume_adjust') {
        const up = step.delta==null || step.delta>=0;
        const pct = step.delta!=null ? Math.abs(Math.round(step.delta*100)) : 10;
        return `<div class="d-flex gap-1 align-items-center">
            <select class="form-select form-select-sm s-mdir" data-sid="${sid}" style="width:auto"><option value="up" ${up?'selected':''}>Up</option><option value="down" ${!up?'selected':''}>Down</option></select>
            <input type="number" class="form-control form-control-sm s-mvol" data-sid="${sid}" value="${pct}" min="1" max="100" style="width:80px"><span class="small">% step</span></div>`;
    }
    if (a === 'announce') {
        const vol = step.volume!=null ? Math.round(step.volume*100) : '';
        return `<textarea class="form-control form-control-sm s-mtext mb-1" data-sid="${sid}" rows="2" placeholder="Spoken text, e.g. Front door has been open for 5 minutes">${step.text?String(step.text).replace(/</g,'&lt;'):''}</textarea>
            <div class="d-flex gap-1 align-items-center"><input type="number" class="form-control form-control-sm s-mvol" data-sid="${sid}" value="${vol}" min="0" max="100" placeholder="vol" style="width:75px"><span class="small text-muted">% volume (optional)</span></div>`;
    }
    if (a === 'volume_fade') {
        const pct = step.volume!=null ? Math.round(step.volume*100) : 0;
        const secs = step.fade_seconds!=null ? step.fade_seconds : 300;
        return `<div class="d-flex gap-1 align-items-center flex-wrap">
            <span class="small">to</span><input type="number" class="form-control form-control-sm s-mvol" data-sid="${sid}" value="${pct}" min="0" max="100" style="width:72px"><span class="small">%</span>
            <span class="small">over</span><input type="number" class="form-control form-control-sm s-mfade" data-sid="${sid}" value="${secs}" min="1" style="width:80px"><span class="small">s</span>
            <div class="form-check form-check-inline mb-0 ms-2"><input class="form-check-input s-mstop" type="checkbox" data-sid="${sid}" ${step.stop_at_end?'checked':''}><label class="small">stop at end</label></div></div>`;
    }
    if (a === 'play_radio')
        return `<div class="input-group input-group-sm">
            <input type="text" class="form-control s-msearch" data-sid="${sid}" placeholder="Search stations…" onkeydown="if(event.key==='Enter'){event.preventDefault();window._aMediaSearch(${sid},'radio');}">
            <button class="btn btn-outline-secondary" type="button" onclick="window._aMediaSearch(${sid},'radio')" title="Search the radio directory"><i class="fas fa-search"></i></button>
            <button class="btn btn-outline-warning" type="button" onclick="window._aMediaFavs(${sid})" title="Pick from favourites"><i class="fas fa-star"></i></button>
            <select class="form-select s-mtarget" data-sid="${sid}">${_mediaSavedOpt(step)}</select></div>`;
    // play_tidal
    const kind = step.tidal_kind || 'playlist';
    const kindSel = `<select class="form-select form-select-sm s-mkind" data-sid="${sid}" style="max-width:105px" onchange="window._aMediaKind(${sid},this)">${TIDAL_KINDS.map(([v,l])=>`<option value="${v}" ${kind===v?'selected':''}>${l}</option>`).join('')}</select>`;
    const search = kind === 'track'
        ? `<input type="text" class="form-control s-msearch" data-sid="${sid}" placeholder="Search tracks…" onkeydown="if(event.key==='Enter'){event.preventDefault();window._aMediaSearch(${sid},'tidal');}"><button class="btn btn-outline-secondary" type="button" onclick="window._aMediaSearch(${sid},'tidal')"><i class="fas fa-search"></i></button>`
        : '';
    // Radio∞ tops the queue up as it drains, which is a property of the
    // controller's queue — a zone walks a fixed list, so it isn't offered one.
    const modeSel = zone ? ''
        : `<select class="form-select form-select-sm s-mmode" data-sid="${sid}" style="max-width:88px"><option value="play" ${step.tidal_mode!=='radio'?'selected':''}>Play</option><option value="radio" ${step.tidal_mode==='radio'?'selected':''}>Radio∞</option></select>`;
    return `<div class="input-group input-group-sm">${kindSel}${search}
        <select class="form-select s-mtarget" data-sid="${sid}">${_mediaSavedOpt(step)}</select>${modeSel}</div>`;
}

function _mediaSavedOpt(step) {
    const id = step.tidal_id || step.station_uuid;
    if (!id) return '<option value="">— pick —</option>';
    const lbl = String(step.label || id).replace(/</g,'&lt;');
    return `<option value="${id}" data-label="${lbl.replace(/"/g,'&quot;')}" selected>${lbl}</option>`;
}

function _mediaDesc(s) {
    if (s.media_action==='play_zone') return 'Play zone';
    if (s.media_action==='control') return (s.control_action||'control').toUpperCase();
    if (s.media_action==='volume') return `VOL ${s.volume!=null?Math.round(s.volume*100):''}%`;
    if (s.media_action==='volume_adjust') return `VOL ${(s.delta||0)>=0?'+':'-'}${Math.abs(Math.round((s.delta||0)*100))}%`;
    if (s.media_action==='announce') return `Say: ${String(s.text||'').slice(0,28)}${(s.text||'').length>28?'…':''}`;
    if (s.media_action==='volume_fade') return `Fade→${s.volume!=null?Math.round(s.volume*100):0}% /${s.fade_seconds||300}s${s.stop_at_end?' ⏹':''}`;
    if (s.media_action==='play_radio') return `Radio: ${s.label||s.station_uuid||'?'}`;
    if (s.media_action==='play_tidal') return `${s.tidal_kind||''}${s.tidal_mode==='radio'?'∞':''}: ${s.label||s.tidal_id||'?'}`;
    return s.media_action||'media';
}

// Populate a media step's target dropdown from the user's Tidal library.
async function _aMediaLoadLib(sid, kind, selId) {
    const sel = document.querySelector(`.s-mtarget[data-sid="${sid}"]`); if(!sel) return;
    const libKind = kind==='mix' ? 'mixes' : kind+'s';
    sel.innerHTML = '<option value="">Loading…</option>';
    try {
        // A <select> has no "load more": page it out (TIDAL caps a favourites
        // request at 50) and bound the loop so a big library stays one dropdown.
        const items = []; let err = '';
        for (let page = 0; page < 10; page++) {
            const j = await (await fetch(`/api/media/tidal/library?kind=${libKind}`
                                         + `&limit=200&offset=${items.length}`)).json();
            if (!j.success) { err = j.error || ''; break; }
            items.push(...(j.items || []));
            if (!j.has_more || !(j.items || []).length) break;
        }
        if (!items.length) { sel.innerHTML = `<option value="">${(err||'none in library')}</option>`; return; }
        sel.innerHTML = '<option value="">— pick —</option>' + items.map(it=>
            `<option value="${it.id}" data-label="${String(it.name||it.id).replace(/"/g,'&quot;')}" ${String(it.id)===String(selId)?'selected':''}>${it.name||it.id}</option>`).join('');
        if (selId && !items.some(it=>String(it.id)===String(selId)))
            sel.insertAdjacentHTML('afterbegin', `<option value="${selId}" selected>${selId}</option>`);
    } catch(e) { sel.innerHTML = '<option value="">load failed</option>'; }
}

// Switching between a speaker and a zone changes which actions exist, so the
// whole step body is rebuilt — and an action the new target cannot do falls
// back rather than being saved as something that would fail at run time.
window._aMPlayer = (sid, sel) => {
    _syncTreeFromDOM(thenTree); _syncTreeFromDOM(elseTree);
    const s = _findStepById(sid); if(!s) return;
    s.player_id = sel.value;
    const zone = isZoneId(s.player_id);
    if (!zone && s.media_action === 'play_zone') s.media_action = 'play_tidal';
    if (zone) s.tidal_mode = 'play';       // no Radio∞ on a shared timeline
    const body = document.getElementById(`step-body-${sid}`);
    if (body) body.innerHTML = _mediaStepBody(s, sid);
    if (s.media_action==='play_tidal' && (s.tidal_kind||'playlist')!=='track')
        _aMediaLoadLib(sid, s.tidal_kind||'playlist', s.tidal_id);
};

window._aMAction = (sid, sel) => {
    _syncTreeFromDOM(thenTree); _syncTreeFromDOM(elseTree);
    const s = _findStepById(sid); if(!s) return;
    s.media_action = sel.value;
    const sub = document.getElementById(`media-sub-${sid}`);
    if (sub) sub.innerHTML = _mediaSubHtml(s, sid);
    if (s.media_action==='play_tidal' && (s.tidal_kind||'playlist')!=='track')
        _aMediaLoadLib(sid, s.tidal_kind||'playlist', s.tidal_id);
    else if (s.media_action==='play_radio')
        window._aMediaFavs(sid);
};

window._aMediaKind = (sid, sel) => {
    _syncTreeFromDOM(thenTree); _syncTreeFromDOM(elseTree);
    const s = _findStepById(sid); if(!s) return;
    s.tidal_kind = sel.value; s.tidal_id=''; s.station_uuid=''; s.label='';
    const sub = document.getElementById(`media-sub-${sid}`);
    if (sub) sub.innerHTML = _mediaSubHtml(s, sid);
    if (s.tidal_kind !== 'track') _aMediaLoadLib(sid, s.tidal_kind);
};

// Populate a radio step's target dropdown from pinned favourites. Favourited
// stations play from their stored snapshot at rule time, so the automation
// keeps working even when the radio-browser directory is unreachable.
window._aMediaFavs = async (sid) => {
    const sel = document.querySelector(`.s-mtarget[data-sid="${sid}"]`); if(!sel) return;
    const cur = sel.value;
    const curLbl = sel.options[sel.selectedIndex]?.dataset?.label || cur;
    sel.innerHTML = '<option value="">Loading…</option>';
    try {
        const j = await (await fetch('/api/media/radio/favourites')).json();
        const st = j.success ? (j.stations||[]) : [];
        if (!st.length) { sel.innerHTML = '<option value="">no favourites — ⭐ a station in the Media tab</option>'; return; }
        sel.innerHTML = '<option value="">— pick a favourite —</option>' + st.map(s=>
            `<option value="${s.uuid}" data-label="${String(s.name||'').replace(/"/g,'&quot;')}" ${s.uuid===cur?'selected':''}>⭐ ${s.name}${s.country?' · '+s.country:''}</option>`).join('');
        if (cur && !st.some(s=>s.uuid===cur))
            sel.insertAdjacentHTML('afterbegin', `<option value="${cur}" data-label="${String(curLbl).replace(/"/g,'&quot;')}" selected>${curLbl}</option>`);
    } catch(e) { sel.innerHTML = '<option value="">load failed</option>'; }
};

window._aMediaSearch = async (sid, kind) => {
    const q = document.querySelector(`.s-msearch[data-sid="${sid}"]`)?.value?.trim();
    const sel = document.querySelector(`.s-mtarget[data-sid="${sid}"]`);
    if (!q || !sel) return;
    sel.innerHTML = '<option value="">Searching…</option>';
    try {
        if (kind === 'radio') {
            const j = await (await fetch(`/api/media/radio/search?q=${encodeURIComponent(q)}&limit=20`)).json();
            const st = j.success ? (j.stations||[]) : [];
            sel.innerHTML = '<option value="">— pick —</option>' + st.map(s=>
                `<option value="${s.uuid}" data-label="${String(s.name||'').replace(/"/g,'&quot;')}">${s.name}${s.country?' · '+s.country:''}</option>`).join('');
        } else {
            const j = await (await fetch(`/api/media/tidal/search?q=${encodeURIComponent(q)}&limit=20`)).json();
            const tr = (j.success && j.results) ? (j.results.tracks||[]) : [];
            sel.innerHTML = '<option value="">— pick —</option>' + tr.map(t=>
                `<option value="${t.source_id}" data-label="${String((t.title||'')+' — '+(t.artist||'')).replace(/"/g,'&quot;')}">${t.title} — ${t.artist}</option>`).join('');
        }
        if (sel.options.length <= 1) sel.innerHTML = '<option value="">no results</option>';
    } catch(e) { sel.innerHTML = '<option value="">search failed</option>'; }
};

function _initStepSelects(steps, path) {
    steps.forEach(s => {
        if(s.type==='command' && s.target_ieee) {
            const sel=document.querySelector(`.s-tgt[data-sid="${s._id}"]`);
            if(sel){const o=sel.options[sel.selectedIndex];if(o?.dataset?.cmds)_popCmds(s._id,JSON.parse(o.dataset.cmds),s.command,s.endpoint_id);}
        } else if((s.type==='wait_for'||s.type==='condition')&&s.ieee) {
            _loadAttrs(s._id,s.ieee,s.attribute,s.value);
        } else if(s.type==='if_then_else') {
            (s.inline_conditions||[]).forEach(ic=>{if(ic.ieee)_loadICAttrs(ic._id,ic.ieee,ic.attribute,ic.value);});
            _initStepSelects(s.then_steps||[],'ite-then-'+s._id);
            _initStepSelects(s.else_steps||[],'ite-else-'+s._id);
        } else if(s.type==='parallel') {
            (s.branches||[]).forEach((br,bi)=>_initStepSelects(br,`par-${s._id}-${bi}`));
        } else if(s.type==='media') {
            // Reload the library so an edited Tidal step shows its peers (saved id stays selected).
            if(s.media_action==='play_tidal' && s.tidal_kind && s.tidal_kind!=='track')
                _aMediaLoadLib(s._id, s.tidal_kind, s.tidal_id);
            // Same for radio: offer favourites alongside the saved station.
            else if(s.media_action==='play_radio')
                window._aMediaFavs(s._id);
        }
    });
}

// SELECT HELPERS

function _popCmds(sid,cmds,selCmd,selEp) {
    const sel=document.querySelector(`.s-cmd[data-sid="${sid}"]`);if(!sel)return;
    sel.innerHTML='<option value="">Cmd...</option>';
    (cmds||[]).forEach(c=>{const o=document.createElement('option');o.value=c.command;o.dataset.ep=c.endpoint_id||'';o.textContent=`${c.label||c.command}${c.endpoint_id?' (EP'+c.endpoint_id+')':''}`;if(selCmd===c.command&&(!selEp||selEp==c.endpoint_id))o.selected=true;sel.appendChild(o);});
    sel.onchange=()=>{const o=sel.options[sel.selectedIndex];const ep=document.querySelector(`.s-ep[data-sid="${sid}"]`);if(ep&&o)ep.value=o.dataset.ep||'';};
    if(selEp){const ep=document.querySelector(`.s-ep[data-sid="${sid}"]`);if(ep)ep.value=selEp;}
}

async function _loadAttrs(sid,ieee,selAttr,selVal) {
    const aS=document.querySelector(`.s-attr[data-sid="${sid}"]`);if(!aS)return;
    const dtype=_dtype(ieee);
    try{const d=await(await fetch(`/api/automations/device/${encodeURIComponent(ieee)}/state`)).json();
        aS.innerHTML='<option value="">Attr...</option>';
        _mergeTypeAttrs(d.attributes||[],dtype).forEach(a=>{const o=document.createElement('option');o.value=a.attribute;o.dataset.vo=JSON.stringify(a.value_options||[]);o.dataset.current=a.current_value;o.dataset.type=a.type;o.textContent=_attrOptLabel(a,dtype);if(selAttr===a.attribute)o.selected=true;aS.appendChild(o);});

        aS.onchange=()=>{
            const o=aS.options[aS.selectedIndex];
            if(!o)return;
            const vo=JSON.parse(o.dataset.vo||'[]');
            const w=document.getElementById(`sv-${sid}`);
            if(w)w.innerHTML=_valueInput('s-vl', sid, dtype, o.value, o.dataset.type, '', vo, 'data-sid');
        };
        if(selAttr){
            const o=aS.options[aS.selectedIndex];
            if(o){
                const vo=JSON.parse(o.dataset.vo||'[]');
                const w=document.getElementById(`sv-${sid}`);
                if(w)w.innerHTML=_valueInput('s-vl', sid, dtype, o.value, o.dataset.type, selVal!=null?selVal:'', vo, 'data-sid');
            }
        }
    }catch(e){}
}

async function _loadICAttrs(icId,ieee,selAttr,selVal) {
    const aS=document.querySelector(`.ic-attr[data-icid="${icId}"]`);if(!aS)return;
    const dtype=_dtype(ieee);
    try{const d=await(await fetch(`/api/automations/device/${encodeURIComponent(ieee)}/state`)).json();
        aS.innerHTML='<option value="">Attr...</option>';
        _mergeTypeAttrs(d.attributes||[],dtype).forEach(a=>{const o=document.createElement('option');o.value=a.attribute;o.dataset.vo=JSON.stringify(a.value_options||[]);o.dataset.current=a.current_value;o.dataset.type=a.type;o.textContent=_attrOptLabel(a,dtype);if(selAttr===a.attribute)o.selected=true;aS.appendChild(o);});

        aS.onchange=()=>{
            const o=aS.options[aS.selectedIndex];
            if(!o)return;
            const vo=JSON.parse(o.dataset.vo||'[]');
            const w=document.getElementById(`icv-${icId}`);
            if(w)w.innerHTML=_valueInput('ic-vl', icId, dtype, o.value, o.dataset.type, '', vo, 'data-icid');
        };
        if(selAttr){
            const o=aS.options[aS.selectedIndex];
            if(o){
                const vo=JSON.parse(o.dataset.vo||'[]');
                const w=document.getElementById(`icv-${icId}`);
                if(w)w.innerHTML=_valueInput('ic-vl', icId, dtype, o.value, o.dataset.type, selVal!=null?selVal:'', vo, 'data-icid');
            }
        }
    }catch(e){}
}

// STEP TREE MANIPULATION

function _findStepList(path) {
    if(path==='then') return thenTree;
    if(path==='else') return elseTree;
    // Nested paths: "ite-then-{sid}", "ite-else-{sid}", "par-{sid}-{bi}",
    // or "ofa-{sid}" for an offer's accept sequence.
    const iteM = path.match(/^ite-(then|else)-(\d+)$/);
    if(iteM) {
        const branch=iteM[1], parentId=parseInt(iteM[2]);
        const step = _findStepById(parentId);
        if(!step) return null;
        return branch==='then' ? (step.then_steps||(step.then_steps=[])) : (step.else_steps||(step.else_steps=[]));
    }
    // "ofa-{sid}" — the sequence an offer runs only if it is accepted.
    const ofaM = path.match(/^ofa-(\d+)$/);
    if(ofaM) {
        const step = _findStepById(parseInt(ofaM[1]));
        if(!step) return null;
        return step.accept_steps||(step.accept_steps=[]);
    }
    const parM = path.match(/^par-(\d+)-(\d+)$/);
    if(parM) {
        const parentId=parseInt(parM[1]), bi=parseInt(parM[2]);
        const step = _findStepById(parentId);
        if(!step || !step.branches) return null;
        return step.branches[bi];
    }
    return null;
}

function _findStepById(id, list) {
    for(const tree of [thenTree, elseTree]) {
        const r = _findInTree(tree, id);
        if(r) return r;
    }
    return null;
}

function _findInTree(steps, id) {
    for(const s of steps) {
        if(s._id === id) return s;
        if(s.then_steps) { const r=_findInTree(s.then_steps,id); if(r) return r; }
        if(s.else_steps) { const r=_findInTree(s.else_steps,id); if(r) return r; }
        if(s.accept_steps) { const r=_findInTree(s.accept_steps,id); if(r) return r; }
        if(s.branches) { for(const b of s.branches) { const r=_findInTree(b,id); if(r) return r; } }
    }
    return null;
}

function _removeFromTree(steps, id) {
    const idx = steps.findIndex(s=>s._id===id);
    if(idx>=0) { steps.splice(idx,1); return true; }
    for(const s of steps) {
        if(s.then_steps && _removeFromTree(s.then_steps,id)) return true;
        if(s.else_steps && _removeFromTree(s.else_steps,id)) return true;
        if(s.accept_steps && _removeFromTree(s.accept_steps,id)) return true;
        if(s.branches) { for(const b of s.branches) { if(_removeFromTree(b,id)) return true; } }
    }
    return false;
}

// WINDOW HANDLERS

// Conditions
window._aCa=(id,sel)=>{const o=sel.options[sel.selectedIndex];if(!o?.value)return;const ops=JSON.parse(o.dataset.operators||'["eq","neq"]'),vo=JSON.parse(o.dataset.vo||'[]'),cur=o.dataset.current,typ=o.dataset.type,attr=o.value;
    const srcType=_dtype(currentSourceIeee);
    const dflt=typ==='boolean'?String(cur).toLowerCase():'';
    const os=document.querySelector(`#c-${id} .co`);if(os){os.innerHTML=ops.map(op=>`<option value="${op}">${OP[op]} ${OPT[op]}</option>`).join('');
        os.onchange=()=>{const opV=os.value;const w=document.getElementById(`cv-${id}`);if(w){
            if(opV==='in'||opV==='nin'){w.innerHTML=`<input type="text" class="form-control form-control-sm cv" data-id="${id}" placeholder="val1, val2, ...">`;}
            else{w.innerHTML=_valueInput('cv',id,srcType,attr,typ,dflt,vo);}}};}
    const w=document.getElementById(`cv-${id}`);if(w)w.innerHTML=_valueInput('cv',id,srcType,attr,typ,dflt,vo);};
window._aAddCond=()=>{if(condRows.length>=5)return;const nid=condIdC++;condRows.push(nid);const el=document.getElementById('cb');if(el)el.insertAdjacentHTML('beforeend',_renderCond(nid));_refCondChrome();};
window._aRmC=id=>{condRows=condRows.filter(r=>r!==id);const row=document.getElementById(`c-${id}`);if(row)row.remove();
    if(condRows.length>0){const first=document.getElementById(`c-${condRows[0]}`);if(first){const b=first.querySelector('.badge');if(b){b.outerHTML='<span class="badge bg-primary small">IF</span>';}
        const rm=first.querySelector('.btn-outline-danger');if(rm)rm.closest('.col-auto').innerHTML='<div style="width:31px"></div>';}}
    _refCondChrome();};
// AND = every condition must hold. OR = any one of them firing is enough.
window._aCLogic=v=>{condLogic=(v==='or')?'or':'and';_refJoinBadges();};
// "Any place" already covers everything, so it and a specific pick are mutually
// exclusive rather than additive.
window._aCZP=(id,cb)=>{
    const boxes=document.querySelectorAll(`.czp[data-id="${id}"]`);
    if(!cb.checked)return;
    if(cb.dataset.place==='any') boxes.forEach(b=>{if(b!==cb)b.checked=false;});
    else boxes.forEach(b=>{if(b.dataset.place==='any')b.checked=false;});
};
window._aCType = (id, sel) => {
    const ctype = sel.value;
    const row = document.getElementById(`c-${id}`);
    if (!row) return;
    const neg = row.querySelector('.cn')?.checked || false;
    row.outerHTML = _renderCond(id, ctype);
    const newRow = document.getElementById(`c-${id}`);
    // Carry the NOT flag across to the temporal rows that have it.
    if (newRow && (ctype === 'time_window' || ctype === 'sun')) {
        const n = newRow.querySelector('.cn'); if (n) n.checked = neg;
    }
    _refCondChrome();
};
// Prerequisites
window._aPd=async(id,sel)=>{const ieee=sel.value;const aS=document.querySelector(`#p-${id} .pa`);if(!aS||!ieee)return;
    const ptype=_dtype(ieee);
    aS.innerHTML='<option>Loading...</option>';
    try{const d=await(await fetch(`/api/automations/device/${encodeURIComponent(ieee)}/state`)).json();
        const attrs=_mergeTypeAttrs(d.attributes||[],ptype);
        aS.innerHTML='<option value="">Attr...</option>';attrs.forEach(a=>{const o=document.createElement('option');o.value=a.attribute;o.dataset.vo=JSON.stringify(a.value_options||[]);o.dataset.current=a.current_value;o.dataset.type=a.type;o.textContent=_attrOptLabel(a,ptype);aS.appendChild(o);});
        aS.onchange=()=>window._aPa(id,aS);
    }catch(e){}};
window._aPa=(id,sel)=>{const o=sel.options[sel.selectedIndex];if(!o)return;const vo=JSON.parse(o.dataset?.vo||'[]');const typ=o.dataset?.type;const cur=typ==='boolean'?String(o.dataset.current).toLowerCase():'';
    const ieee=document.querySelector(`#p-${id} .pd`)?.value;const ptype=_dtype(ieee);
    const w=document.getElementById(`pv-${id}`);if(w)w.innerHTML=_valueInput('pv',id,ptype,o.value,typ,cur,vo);};
window._aAddPrereq=()=>{if(prereqRows.length>=8)return;const nid=prereqIdC++;prereqRows.push(nid);const el=document.getElementById('pb');if(el)el.insertAdjacentHTML('beforeend',_renderPrereq(nid));_syncOptionalSections();window._aPreview();};
window._aRmP=id=>{prereqRows=prereqRows.filter(r=>r!==id);const row=document.getElementById(`p-${id}`);if(row)row.remove();_syncOptionalSections();window._aPreview();};

window._aPType = (id, sel) => {
    const ptype = sel.value;
    const row = document.getElementById(`p-${id}`);
    if (!row) return;
    const body = row.querySelector(`.prereq-body-${id}`);  // won't work with div.d-contents trick
    // Rebuild the full row keeping NOT state and new type
    const neg = row.querySelector('.pn')?.checked || false;
    const newHtml = _renderPrereq(id, ptype);
    row.outerHTML = newHtml;
    const newRow = document.getElementById(`p-${id}`);
    if (newRow) { const pn = newRow.querySelector('.pn'); if (pn) pn.checked = neg; }
};


// Steps
window._aAddStep = (path, type) => {
    const list = _findStepList(path);
    if (!list || list.length >= 15) return;

    // FIX: Sync current UI values to the tree before adding a new step
    _syncTreeFromDOM(thenTree);
    _syncTreeFromDOM(elseTree);

    const s = { _id: _uid(), type };
    if (type === 'delay') s.seconds = 5;
    if (type === 'wait_for') s.timeout = 300;
    if (type === 'media') { s.media_action = 'play_tidal'; s.tidal_kind = 'playlist'; s.tidal_mode = 'play'; }
    if (type === 'if_then_else') {
        s.inline_conditions = [{ _id: _uid(), ieee: '', attribute: '', operator: 'eq', value: '' }];
        s.condition_logic = 'and';
        s.then_steps = [];
        s.else_steps = [];
    }
    if (type === 'parallel') s.branches = [[], []];
    if (type === 'offer') { s.accept_steps = []; s.expires_in = 3600; }

    list.push(s);

    // Re-render sequences (which rebuilds the palette shut)
    _renderStepTree('then');
    _renderStepTree('else');
    _syncOptionalSections();
    window._aPreview();
};

window._aRmStep=(sid,path)=>{_syncTreeFromDOM(thenTree);_syncTreeFromDOM(elseTree);_removeFromTree(thenTree,sid);_removeFromTree(elseTree,sid);_renderStepTree('then');_renderStepTree('else');_syncOptionalSections();window._aPreview();};
window._aSTC=(sid,sel)=>{const o=sel.options[sel.selectedIndex];if(!o?.value)return;_popCmds(sid,JSON.parse(o.dataset.cmds||'[]'));};
window._aSDC=(sid,sel)=>{if(sel.value)_loadAttrs(sid,sel.value);};
window._aICDev=(icId,sel)=>{if(sel.value)_loadICAttrs(icId,sel.value);};

window._aAddIC = sid => {
    const s = _findStepById(sid);
    if (!s) return;

    // FIX: Sync current UI values
    _syncTreeFromDOM(thenTree);
    _syncTreeFromDOM(elseTree);

    if (!s.inline_conditions) s.inline_conditions = [];
    s.inline_conditions.push({ _id: _uid(), ieee: '', attribute: '', operator: 'eq', value: '' });

    _renderStepTree('then');
    _renderStepTree('else');
    _syncOptionalSections();
    window._aPreview();
};

window._aRmIC=(sid,icId)=>{_syncTreeFromDOM(thenTree);_syncTreeFromDOM(elseTree);const s=_findStepById(sid);if(!s||!s.inline_conditions)return;s.inline_conditions=s.inline_conditions.filter(c=>c._id!==icId);_renderStepTree('then');_renderStepTree('else');};

window._aAddBranch = sid => {
    const s = _findStepById(sid);
    if (!s || !s.branches) return;

    // FIX: Sync current UI values
    _syncTreeFromDOM(thenTree);
    _syncTreeFromDOM(elseTree);

    s.branches.push([]);
    _renderStepTree('then');
    _renderStepTree('else');
    _syncOptionalSections();
    window._aPreview();
};

// Form
//
// "Add Rule" opens the swarm chooser rather than a blank form: starting from
// what this device can actually do beats starting from an empty trigger row.
// Every path through the chooser ends here anyway — it picks a shape, the
// builder fills in, and the whole step palette (delay, gate, branch, parallel,
// media) is unchanged underneath.
window._aShowForm=()=>{
    const el=document.getElementById('a-form');
    if(!el){return;}
    if(currentSourceIeee==='__time__'){_showForm(null);return;}  // clock rules have no device to suggest for
    renderChooser(currentSourceIeee, el, ()=>_showForm(null))
        .catch(()=>_showForm(null));  // the swarm is an enhancement, never a gate
};
// Skip the chooser — used by "Start from scratch" and by callers that already
// know what they want.
window._aShowBlankForm=()=>_showForm(null);
// Populate the builder from a generated (unsaved) rule object, treated as new.
window._aShowFormWith=(rule)=>_showForm(rule, true);
window._aHideForm=()=>{document.getElementById('a-form').style.display='none';editingRuleId=null;};
window._aEdit=async id=>{try{const r=await(await fetch(`/api/automations/rule/${id}`)).json();_showForm(r);document.getElementById('a-form')?.scrollIntoView({behavior:'smooth'});}catch(e){window.toast.error(e.message);}};

// Trace
window._aTrace=async()=>{document.getElementById('a-trace').style.display='block';
    const f=document.getElementById('tf');if(f){const c=f.value;f.innerHTML='<option value="">All</option>';
        try{(await(await fetch(`/api/automations?source_ieee=${encodeURIComponent(currentSourceIeee)}`)).json()).forEach(r=>{f.innerHTML+=`<option value="${r.id}">${r.name||r.id}</option>`;});}catch(e){}f.innerHTML+='<option value="-">System</option>';f.value=c||'';}
    _loadTr();};
window._aRefTrace=_loadTr;
window._aTraceR=async id=>{await window._aTrace();const f=document.getElementById('tf');if(f)f.value=id;_loadTr();};

// Toggle/Delete
window._aToggle=async id=>{try{await fetch(`/api/automations/${id}/toggle`,{method:'PATCH'});await _ref();}catch(e){}};
window._aDel=async id=>{if(!await window.zbmConfirm({title:'Delete automation',message:'Delete this automation?',confirmText:'Delete',variant:'danger'}))return;try{await fetch(`/api/automations/${id}`,{method:'DELETE'});await _ref();}catch(e){}};

// SAVE (recursive gather)

/**
 * Read the form into a rule.
 *
 * Extracted from _aSave so the live preview and the save path build the same
 * object from the same DOM. A preview assembled a second way would eventually
 * disagree with what saving produces, which is worse than no preview.
 *
 * Returns {body, valid, error} — invalid means a field is incomplete, and the
 * caller decides whether that is worth a toast (saving) or silence (previewing
 * mid-edit).
 */
function _collectRule() {
    const conditions=[]; let valid=true;
    condRows.forEach(id=>{
        const row=document.getElementById(`c-${id}`);
        if(!row)return;
        const ctype=row.querySelector('.ctype')?.value||'attribute';
        if(ctype==='time_window'){
            const tf=row.querySelector('.ct-from')?.value;
            const tt=row.querySelector('.ct-to')?.value;
            if(!tf||!tt){valid=false;return;}
            const neg=row.querySelector('.cn')?.checked||false;
            const days=[];row.querySelectorAll('.ctd').forEach(cb=>{if(cb.checked)days.push(parseInt(cb.dataset.day));});
            conditions.push({type:'time_window',time_from:tf,time_to:tt,days,negate:neg});
        } else if(ctype==='time'){
            const at=row.querySelector('.ct-at')?.value;
            if(!at){valid=false;return;}
            const days=[];row.querySelectorAll('.ctd').forEach(cb=>{if(cb.checked)days.push(parseInt(cb.dataset.day));});
            conditions.push({type:'time',at,days});
        } else if(ctype==='zone'){
            const ev=row.querySelector('.cz-ev')?.value||'enter';
            const picked=[];row.querySelectorAll('.czp').forEach(cb=>{if(cb.checked)picked.push(cb.dataset.place);});
            if(!picked.length){valid=false;return;}
            // One place stays a plain string; several become a single zone.
            const place=picked.includes('any')?'any':(picked.length===1?picked[0]:picked);
            conditions.push({type:'zone',event:ev,place});
        } else if(ctype==='sun'){
            const frm=row.querySelector('.cs-from')?.value||'sunset';
            const to=row.querySelector('.cs-to')?.value||'sunrise';
            const neg=row.querySelector('.cn')?.checked||false;
            const offF=parseInt(row.querySelector('.cs-off-from')?.value);
            const offT=parseInt(row.querySelector('.cs-off-to')?.value);
            const c={type:'sun',from:frm,to:to,negate:neg};
            if(!isNaN(offF))c.offset_from=offF;
            if(!isNaN(offT))c.offset_to=offT;
            conditions.push(c);
        } else {
            const a=row.querySelector('.ca')?.value,o=row.querySelector('.co')?.value;
            const vE=row.querySelector(`#cv-${id} .cv`),r=vE?.value,s=row.querySelector('.cs')?.value;
            if(!a||!o||r===undefined||r===''){valid=false;return;}
            const ai=cachedAttributes.find(x=>x.attribute===a);
            let value;
            if(o==='in'||o==='nin'){value=String(r).split(',').map(v=>_ct(v.trim(),ai?.type));}
            else{value=_ct(r,ai?.type);}
            const c={type:'attribute',attribute:a,operator:o,value};if(s&&parseInt(s)>0)c.sustain=parseInt(s);conditions.push(c);
        }
    });
    if(!valid||!conditions.length) return {valid:false, error:'Fill all conditions.'};

    const prerequisites = [];
    prereqRows.forEach(id => {
        const row = document.getElementById(`p-${id}`);
        if (!row) return;
        const ptype = row.querySelector('.ptype')?.value || 'device';
        const neg   = row.querySelector('.pn')?.checked || false;

        if (ptype === 'time_window') {
            const tf = row.querySelector('.pt-from')?.value;
            const tt = row.querySelector('.pt-to')?.value;
            if (!tf || !tt) return;
            const days = [];
            row.querySelectorAll('.ptd').forEach(cb => { if (cb.checked) days.push(parseInt(cb.dataset.day)); });
            prerequisites.push({ type: 'time_window', time_from: tf, time_to: tt, days, negate: neg });
        } else if (ptype === 'sun') {
            const c = { type: 'sun', from: row.querySelector('.ps-from')?.value || 'sunset',
                        to: row.querySelector('.ps-to')?.value || 'sunrise', negate: neg };
            const offF = parseInt(row.querySelector('.ps-off-from')?.value);
            const offT = parseInt(row.querySelector('.ps-off-to')?.value);
            if (!isNaN(offF)) c.offset_from = offF;
            if (!isNaN(offT)) c.offset_to = offT;
            prerequisites.push(c);
        } else {
            const ieee = row.querySelector('.pd')?.value;
            const a    = row.querySelector('.pa')?.value;
            const o    = row.querySelector('.po')?.value;
            const vE   = row.querySelector(`#pv-${id} .pv`);
            const r    = vE?.value;
            if (!ieee || !a || !o || r === undefined || r === '') return;
            const pval = (o === 'in' || o === 'nin') ? String(r).split(',').map(v => _co(v.trim())) : _co(r);
            prerequisites.push({ type: 'device', ieee, attribute: a, operator: o, value: pval, negate: neg });
        }
    });

    // Gather step trees (reads current DOM values into the tree, then clean for API)
    _syncTreeFromDOM(thenTree);
    _syncTreeFromDOM(elseTree);
    const then_sequence = _cleanTree(thenTree);
    const else_sequence = _cleanTree(elseTree);
    if(!then_sequence.length&&!else_sequence.length)
        return {valid:false, error:'Add at least one step.'};

    return {valid:true, body:{
        name:document.getElementById('a-name')?.value||'', source_ieee:currentSourceIeee,
        conditions, condition_logic:condLogic, prerequisites, then_sequence, else_sequence,
        cooldown:parseInt(document.getElementById('a-cd')?.value)||5, enabled:true}};
}

/**
 * Refresh the plain-English preview from whatever the form currently says.
 *
 * Cheap enough to run on every keystroke — it reads the DOM and formats a
 * string. Mid-edit the rule is usually incomplete; that is shown as a hint
 * rather than an error, because an unfinished rule is the normal state of a
 * form somebody is filling in.
 */
function _refreshPreview() {
    const el = document.getElementById('a-preview');
    if (!el) return;

    // Names come from the same lists the pickers are built from, so the preview
    // reads with real device names rather than addresses.
    const devMap = {};
    (cachedAllDevices || []).forEach(d => { devMap[d.ieee] = d; });
    (cachedActuators || []).forEach(d => { if (!devMap[d.ieee]) devMap[d.ieee] = d; });
    const playerMap = {};
    (cachedPlayers || []).forEach(p => { playerMap[p.player_id] = p.name; });
    (cachedZones || []).forEach(z => { playerMap['zone:' + z.id] = `the ${z.name} zone`; });
    const placeMap = {};
    (cachedPlaces || []).forEach(p => { placeMap[p.id] = p.name; });

    const H = createHumanizer({
        device: ieee => devMap[ieee],
        player: id => playerMap[id],
        place: id => placeMap[id],
    });

    let collected;
    try {
        collected = _collectRule();
    } catch (e) {
        el.innerHTML = '';
        return;
    }
    if (!collected.valid) {
        el.innerHTML = `<div class="a-preview-hint"><i class="fas fa-pen-to-square"></i> ${collected.error}</div>`;
        return;
    }
    el.innerHTML = H.rulePhrase(collected.body);
}

// Debounced, so typing a device name does not re-render the preview per letter.
let _previewTimer = null;
window._aPreview = () => {
    clearTimeout(_previewTimer);
    _previewTimer = setTimeout(_refreshPreview, 120);
};

/** Show the optional sections whether or not they currently hold anything. */
window._aToggleOptional = () => {
    const form = document.getElementById('a-form');
    if (!form) return;
    const on = form.classList.toggle('a-show-optional');
    const btn = document.getElementById('a-opt-btn');
    if (btn) btn.innerHTML = on
        ? '<i class="fas fa-sliders"></i> Fewer options'
        : '<i class="fas fa-sliders"></i> More options';
};

/**
 * Reveal an optional section that has content.
 *
 * Prerequisites and the ELSE branch are empty on most rules, so they are hidden
 * until they hold something — but a rule that uses them must show them without
 * the user having to go looking.
 */
function _syncOptionalSections() {
    document.getElementById('a-prereq-sec')?.classList
        .toggle('a-used', prereqRows.length > 0);
    document.getElementById('a-else-sec')?.classList
        .toggle('a-used', elseTree.length > 0);
}

window._aSave=async()=>{
    const {valid, body, error} = _collectRule();
    if(!valid) return window.toast.warning(error);
    try{
        const res = editingRuleId
            ? await fetch(`/api/automations/${editingRuleId}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
            : await fetch('/api/automations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        const d=await res.json();
        if(res.ok&&d.success){invalidateChooser();window._aHideForm();await _ref();}
        else window.toast.error('Failed: '+(d.detail||d.error||'Unknown'));
    }catch(e){window.toast.error(e.message);}
};

function _syncTreeFromDOM(steps) {
    steps.forEach(s => {
        const sid = s._id;
        if(s.type==='command') {
            s.target_ieee=document.querySelector(`.s-tgt[data-sid="${sid}"]`)?.value||'';
            s.command=document.querySelector(`.s-cmd[data-sid="${sid}"]`)?.value||'';
            const v=document.querySelector(`.s-val[data-sid="${sid}"]`)?.value;
            s.value=(v!==undefined&&v!=='')?_co(v):null;
            const ep=document.querySelector(`.s-ep[data-sid="${sid}"]`)?.value;
            s.endpoint_id=ep?parseInt(ep):null;
        } else if(s.type==='delay') {
            s.seconds=parseInt(document.querySelector(`.s-sec[data-sid="${sid}"]`)?.value)||5;
        } else if(s.type==='wait_for'||s.type==='condition') {
            s.ieee=document.querySelector(`.s-ieee[data-sid="${sid}"]`)?.value||'';
            s.attribute=document.querySelector(`.s-attr[data-sid="${sid}"]`)?.value||'';
            s.operator=document.querySelector(`.s-op[data-sid="${sid}"]`)?.value||'eq';

            // Check if it's a select dropdown or a text input
            const valEl = document.querySelector(`.s-vl[data-sid="${sid}"]`);
            const rawVal = valEl?.value;
            s.value = _co(rawVal !== undefined ? rawVal : '');

            s.negate=document.querySelector(`.s-neg[data-sid="${sid}"]`)?.checked||false;
            if(s.type==='wait_for')s.timeout=parseInt(document.querySelector(`.s-tout[data-sid="${sid}"]`)?.value)||300;
        } else if(s.type==='if_then_else') {
            s.condition_logic=document.querySelector(`.s-logic[data-sid="${sid}"]`)?.value||'and';
            (s.inline_conditions||[]).forEach(ic=>{
                const icid = ic._id;
                ic.ieee=document.querySelector(`.ic-ieee[data-icid="${icid}"]`)?.value||'';
                ic.attribute=document.querySelector(`.ic-attr[data-icid="${icid}"]`)?.value||'';
                ic.operator=document.querySelector(`.ic-op[data-icid="${icid}"]`)?.value||'eq';

                // Support dropdown values for inline conditions
                const icValEl = document.querySelector(`.ic-vl[data-icid="${icid}"]`);
                const icRawVal = icValEl?.value;

                ic.value=(ic.operator==='in'||ic.operator==='nin')
                    ? String(icRawVal||'').split(',').map(x=>_co(x.trim()))
                    : _co(icRawVal||'');

                ic.negate=document.querySelector(`.ic-neg[data-icid="${icid}"]`)?.checked||false;
            });
            _syncTreeFromDOM(s.then_steps||[]);
            _syncTreeFromDOM(s.else_steps||[]);
        } else if(s.type==='parallel') {
            (s.branches||[]).forEach(br=>_syncTreeFromDOM(br));
        } else if(s.type==='request') {
            s.to_user=document.querySelector(`.s-rq-to[data-sid="${sid}"]`)?.value||'';
            s.from_user=document.querySelector(`.s-rq-from[data-sid="${sid}"]`)?.value||'';
            s.message=document.querySelector(`.s-rq-msg[data-sid="${sid}"]`)?.value||'';
        } else if(s.type==='offer') {
            s.to_user=document.querySelector(`.s-of-to[data-sid="${sid}"]`)?.value||'';
            s.message=document.querySelector(`.s-of-msg[data-sid="${sid}"]`)?.value||'';
            const mins=parseInt(document.querySelector(`.s-of-exp[data-sid="${sid}"]`)?.value,10);
            // Minutes in the form, seconds on the wire — the engine's unit.
            s.expires_in=(mins>0?mins:60)*60;
            _syncTreeFromDOM(s.accept_steps||[]);
        } else if(s.type==='media') {
            s.player_id=document.querySelector(`.s-mplayer[data-sid="${sid}"]`)?.value||'';
            s.media_action=document.querySelector(`.s-maction[data-sid="${sid}"]`)?.value||'play_tidal';
            const tgt=document.querySelector(`.s-mtarget[data-sid="${sid}"]`);
            const tgtVal=tgt?.value||'';
            const tgtLabel=tgt?.options[tgt.selectedIndex]?.dataset?.label||tgtVal;
            if(s.media_action==='play_radio'){ s.station_uuid=tgtVal; s.label=tgtLabel; }
            else if(s.media_action==='play_tidal'){
                s.tidal_kind=document.querySelector(`.s-mkind[data-sid="${sid}"]`)?.value||'playlist';
                s.tidal_id=tgtVal; s.label=tgtLabel;
                s.tidal_mode=document.querySelector(`.s-mmode[data-sid="${sid}"]`)?.value||'play';
            }
            else if(s.media_action==='control'){ s.control_action=document.querySelector(`.s-mctrl[data-sid="${sid}"]`)?.value||'stop'; }
            else if(s.media_action==='volume'){ const v=parseInt(document.querySelector(`.s-mvol[data-sid="${sid}"]`)?.value); s.volume=isNaN(v)?0.3:Math.max(0,Math.min(1,v/100)); }
            else if(s.media_action==='volume_adjust'){
                const v=parseInt(document.querySelector(`.s-mvol[data-sid="${sid}"]`)?.value);
                const mag=isNaN(v)?0.1:Math.max(0.01,Math.min(1,v/100));
                s.delta=(document.querySelector(`.s-mdir[data-sid="${sid}"]`)?.value==='down')?-mag:mag;
            }
            else if(s.media_action==='announce'){
                s.text=document.querySelector(`.s-mtext[data-sid="${sid}"]`)?.value||'';
                const v=parseInt(document.querySelector(`.s-mvol[data-sid="${sid}"]`)?.value);
                s.volume=isNaN(v)?null:Math.max(0,Math.min(1,v/100));
            }
            else if(s.media_action==='volume_fade'){
                const v=parseInt(document.querySelector(`.s-mvol[data-sid="${sid}"]`)?.value);
                s.volume=isNaN(v)?0:Math.max(0,Math.min(1,v/100));
                s.fade_seconds=parseInt(document.querySelector(`.s-mfade[data-sid="${sid}"]`)?.value)||300;
                s.stop_at_end=document.querySelector(`.s-mstop[data-sid="${sid}"]`)?.checked||false;
            }
        }
    });
}

function _cleanTree(steps) {
    return steps.map(s=>{
        const d={type:s.type};
        if(s.type==='command'){d.target_ieee=s.target_ieee;d.command=s.command;if(s.value!=null)d.value=s.value;if(s.endpoint_id)d.endpoint_id=s.endpoint_id;}
        else if(s.type==='delay'){d.seconds=s.seconds;}
        else if(s.type==='wait_for'||s.type==='condition'){d.ieee=s.ieee;d.attribute=s.attribute;d.operator=s.operator;d.value=s.value;if(s.negate)d.negate=true;if(s.type==='wait_for')d.timeout=s.timeout;}
        else if(s.type==='if_then_else'){d.inline_conditions=(s.inline_conditions||[]).map(ic=>({ieee:ic.ieee,attribute:ic.attribute,operator:ic.operator,value:ic.value,...(ic.negate?{negate:true}:{})}));d.condition_logic=s.condition_logic||'and';d.then_steps=_cleanTree(s.then_steps||[]);d.else_steps=_cleanTree(s.else_steps||[]);}
        else if(s.type==='parallel'){d.branches=(s.branches||[]).map(br=>_cleanTree(br));}
        else if(s.type==='request'){d.to_user=s.to_user;d.message=(s.message||'').trim();if(s.from_user)d.from_user=s.from_user;}
        else if(s.type==='offer'){d.to_user=s.to_user;d.message=(s.message||'').trim();d.accept_steps=_cleanTree(s.accept_steps||[]);d.expires_in=s.expires_in||3600;if(s.from_user)d.from_user=s.from_user;}
        else if(s.type==='media'){
            d.player_id=s.player_id; d.media_action=s.media_action;
            // The zone's name, so the run trace names the room rather than an id.
            if(s.media_action==='play_zone'){const z=zoneOf(s.player_id);if(z)d.label=`Play ${z.name}`;}
            else if(s.media_action==='play_radio'){d.station_uuid=s.station_uuid;if(s.label)d.label=s.label;}
            else if(s.media_action==='play_tidal'){d.tidal_kind=s.tidal_kind;d.tidal_id=s.tidal_id;d.tidal_mode=s.tidal_mode||'play';if(s.label)d.label=s.label;}
            else if(s.media_action==='control'){d.control_action=s.control_action;}
            else if(s.media_action==='volume'){d.volume=s.volume;}
            else if(s.media_action==='volume_adjust'){d.delta=s.delta;}
            else if(s.media_action==='announce'){d.text=s.text;if(s.volume!=null)d.volume=s.volume;}
            else if(s.media_action==='volume_fade'){d.volume=s.volume;d.fade_seconds=s.fade_seconds||300;if(s.stop_at_end)d.stop_at_end=true;}
        }
        return d;
    }).filter(d=>{
        if(d.type==='command')return d.target_ieee&&d.command;
        if(d.type==='delay')return d.seconds>0;
        if(d.type==='wait_for'||d.type==='condition')return d.ieee&&d.attribute;
        if(d.type==='request')return !!(d.to_user&&d.message);
        // An offer with nothing to run is a message, and the engine rejects it.
        if(d.type==='offer')return !!(d.to_user&&d.message&&(d.accept_steps||[]).length);
        if(d.type==='if_then_else')return(d.inline_conditions||[]).length>0;
        if(d.type==='parallel')return(d.branches||[]).length>=2;
        if(d.type==='media'){
            if(!d.player_id)return false;
            if(d.media_action==='play_zone')return isZoneId(d.player_id);
            if(d.media_action==='play_radio')return !!d.station_uuid;
            if(d.media_action==='play_tidal')return !!(d.tidal_kind&&d.tidal_id);
            if(d.media_action==='control')return !!d.control_action;
            if(d.media_action==='volume')return d.volume!=null;
            if(d.media_action==='volume_adjust')return typeof d.delta==='number'&&d.delta!==0;
            if(d.media_action==='announce')return !!(d.text&&d.text.trim());
            if(d.media_action==='volume_fade')return d.volume!=null;
            return false;
        }
        return false;
    });
}


function _co(v){if(typeof v!=='string')return v;const t=v.trim(),l=t.toLowerCase();if(l==='true')return true;if(l==='false')return false;if(!isNaN(t)&&t!=='')return parseFloat(t);return t;}
function _ct(v,typ){if(!typ)return _co(v);if(typ==='boolean')return _co(v);if(typ==='float'){const n=parseFloat(v);return isNaN(n)?v:n;}if(typ==='integer'){const n=parseInt(v,10);return isNaN(n)?_co(v):n;}return String(v).trim();}
async function _ref(){if(!currentSourceIeee)return;try{_renderRules(await(await fetch(`/api/automations?source_ieee=${encodeURIComponent(currentSourceIeee)}`)).json());}catch(e){}}

async function _loadTr() {
    const el=document.getElementById('a-trace-c');if(!el)return;
    const fv=document.getElementById('tf')?.value||'';
    try{const entries=await(await fetch(fv?`/api/automations/trace?rule_id=${encodeURIComponent(fv)}`:'/api/automations/trace')).json();
        if(!entries?.length){el.innerHTML='<div class="text-muted p-2">No trace.</div>';return;}
        let h='';[...entries].reverse().forEach(e=>{
            const ts=new Date(e.timestamp*1000).toLocaleTimeString(),r=e.result||'';
            let cl='text-muted';
            if(r==='SUCCESS'||r.includes('FIRING')||r==='COMPLETE'||r==='WAIT_MET'||r==='GATE_PASS'||r==='IF_TRUE'||r==='PARALLEL_DONE')cl='text-success';
            else if(r.includes('FAIL')||r.includes('ERROR')||r==='EXCEPTION'||r.includes('MISSING')||r==='CMD_FAIL')cl='text-danger';
            else if(r==='BLOCKED'||r==='SUSTAIN_WAIT'||r==='DELAY'||r==='WAITING')cl='text-warning';
            else if(r==='CANCELLED'||r==='WAIT_TIMEOUT'||r==='IF_FALSE')cl='text-info';
            h+=`<div class="border-bottom py-1 ${cl}"><span class="text-muted">${ts}</span> <span class="badge bg-dark">${e.phase||''}</span> <span class="badge bg-secondary">${r}</span> `;
            if(e.rule_id&&e.rule_id!=='-')h+=`<code>${e.rule_id}</code> `;
            h+=e.message||'';
            if(e.conditions?.length){h+='<div class="ms-3">';
                // OR rules log every condition they checked, so say which way they
                // combine — otherwise a FAIL line looks like the rule should be dead.
                if(e.condition_logic==='or')h+='<div class="small" style="color:#6f42c1">any one of (OR):</div>';
                e.conditions.forEach(c=>{const cc=c.result==='PASS'?'text-success':c.result==='SUSTAIN_WAIT'?'text-warning':'text-danger';
                let cLine;
                const DAY_NAMES=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
                if(c.type==='time_window'){
                    const dayStr=(!c.days||c.days.length===7)?'Every day':c.days.map(d=>DAY_NAMES[d]).join(',');
                    cLine=`#${c.index} Time${c.negate?' NOT':''} ${c.time_from}→${c.time_to} [${dayStr}] now=${c.now_time} weekday=${c.now_weekday}`;
                }else if(c.type==='time'){
                    const dayStr=(!c.days||c.days.length===7)?'Every day':c.days.map(d=>DAY_NAMES[d]).join(',');
                    cLine=`#${c.index} Alarm${c.negate?' NOT':''} ${c.at} [${dayStr}] now=${c.now_time} weekday=${c.now_weekday}`;
                }else if(c.type==='zone'){
                    const move=c.from_place!==undefined?` ${c.from_place??'?'} → ${c.to_place??'?'}`:'';
                    cLine=`#${c.index} ${c.event==='leave'?'Leaves':'Enters'} ${_placeLabel(c.place)}${move}`;
                }else if(c.type==='sun'){
                    cLine=`#${c.index} Sun ${c.from}→${c.to}${c.resolved?` (${c.resolved})`:''} now=${c.now_time||''}`;
                }else{
                    cLine=`#${c.index} ${c.attribute} ${c.operator||''} ${c.threshold_raw||c.threshold||'?'} → ${c.actual_raw||'?'} (${c.actual_type||''})`;
                    if(c.sustain_elapsed!=null)cLine+=` ⏱${c.sustain_elapsed}s`;if(c.value_source)cLine+=` ${c.value_source}`;
                }
                h+=`<div class="${cc}">${cLine} [${c.result}]`;if(c.reason)h+=` — ${c.reason}`;h+='</div>';});h+='</div>';}
            if(e.prerequisites?.length){h+='<div class="ms-3">';e.prerequisites.forEach(p=>{const pc=p.result==='PASS'?'text-success':'text-danger';
                const DAY_NAMES=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
                let pLine;
                if(p.type==='time_window'){
                    const dayStr=(!p.days||p.days.length===7)?'Every day':p.days.map(d=>DAY_NAMES[d]).join(',');
                    pLine=`CHECK${p.negate?' NOT':''} Time ${p.time_from}→${p.time_to} [${dayStr}] now=${p.now_time} weekday=${p.now_weekday}`;
                }else if(p.type==='sun'){
                    pLine=`CHECK Sun ${p.from}→${p.to}${p.resolved?` (${p.resolved})`:''} now=${p.now_time||''}`;
                }else{
                    pLine=`CHECK${p.negate?' NOT':''} ${p.device_name||p.ieee} ${p.attribute} ${p.operator||''} ${p.threshold_raw||'?'} → ${p.actual_raw||'?'}`;
                }
                h+=`<div class="${pc}">${pLine} [${p.result}]`;
                if(p.reason)h+=` — ${p.reason}`;h+='</div>';});h+='</div>';}
            if(e.inline_conditions?.length){h+='<div class="ms-3">';e.inline_conditions.forEach(ic=>{const cc=ic.result==='PASS'?'text-success':'text-danger';
                h+=`<div class="${cc}">  ${ic.negate?'NOT ':''}${ic.device_name||''} ${ic.attribute} ${ic.operator||''} ${ic.threshold||''} → ${ic.actual||'?'} [${ic.result}]</div>`;});h+='</div>';}
            if(e.error)h+=`<div class="ms-3 text-danger">${e.error}</div>`;h+='</div>';});
        el.innerHTML=h;
    }catch(err){el.innerHTML=`<div class="text-danger">${err.message}</div>`;}
}


// DOWNLOAD AUTOMATION FLOW
window._aDownloadJson = async (id) => {
    try {
        const res = await fetch(`/api/automations/rule/${id}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const blob = new Blob([JSON.stringify(data, null, 4)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `automation_${data.name ? data.name.replace(/[^a-z0-9_-]/gi,'_') : id}.json`;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 100);
    } catch (e) {
        window.toast.error('Failed to download: ' + e.message);
    }
};