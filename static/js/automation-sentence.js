/**
 * Plain-English rendering of an automation rule.
 *
 * One voice for the whole app. The rules list and the rule editor both need to
 * say what a rule does, and saying it two ways would eventually mean saying it
 * two different ways — so the phrasing lives here and both import it.
 *
 * Device, player and place names are injected rather than imported, because the
 * two callers hold them in different places: the rules page caches them per
 * page load, the editor holds them per open device. `createHumanizer` takes
 * accessors, so neither has to reshape its state to borrow the other's voice.
 *
 * Everything returns HTML with values escaped. Callers insert it directly.
 */

import { DEVICE_ICON, deviceType } from './automation-humanize.js';

const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

const OPW = {
    eq: 'is', neq: 'is not', gt: 'is above', lt: 'is below',
    gte: 'is at least', lte: 'is at most', in: 'is one of', nin: 'is not one of',
};

const CMD_VERB = {
    on: 'Turn on', off: 'Turn off', toggle: 'Toggle', open: 'Open', close: 'Close',
    stop: 'Stop', lock: 'Lock', unlock: 'Unlock', brightness: 'Set brightness of',
    color_temp: 'Set colour of', position: 'Set position of',
};

export function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/**
 * Build a humanizer bound to one set of name lookups.
 *
 * @param {object} ctx
 * @param {function(string):object} ctx.device  ieee -> device summary, or undefined
 * @param {function(string):string} [ctx.player] player_id -> friendly name
 * @param {function(string):string} [ctx.place]  place id -> friendly name
 */
export function createHumanizer(ctx = {}) {
    const getDevice = ctx.device || (() => undefined);
    const getPlayer = ctx.player || (id => id);
    const getPlace = ctx.place || (id => id);

    // Resolve a device/group name + semantic type for an ieee (or group:N).
    function resolve(ieee) {
        if (ieee === '__time__') return { name: 'Time / Alarm', type: 'time' };
        if (typeof ieee === 'string' && ieee.startsWith('group:')) {
            const g = getDevice(ieee);
            const model = ((g && g.model) || '').toLowerCase();
            const type = model.includes('cover') ? 'cover'
                       : model.includes('lock') ? 'unknown'
                       : 'light';   // most groups are lights/switches
            return { name: g ? String(g.friendly_name || ieee).replace(/^🔗\s*/, '') : ieee, type };
        }
        const dev = getDevice(ieee);
        return { name: dev ? dev.friendly_name : ieee, type: deviceType(ieee, dev) };
    }

    const devSpan = ieee => `<span class="dev">${esc(resolve(ieee).name)}</span>`;
    const icon = t => `<i class="fas ${DEVICE_ICON[t] || DEVICE_ICON.unknown}"></i>`;

    function daySpan(days) {
        if (!days || days.length === 7) return 'every day';
        const s = [...days].sort((a, b) => a - b).join(',');
        if (s === '0,1,2,3,4') return 'on weekdays';
        if (s === '5,6') return 'at weekends';
        return 'on ' + days.map(d => DAY_NAMES[d]).join(', ');
    }

    const timePhrase = (a, b) => `between <b>${esc(a)}</b> and <b>${esc(b)}</b>`;

    // Core semantic map: attribute (+ device type hint) -> verb phrase.
    // Outlet suffixes are only surfaced for endpoint 2+ — "outlet 1" is the
    // default (or only) endpoint on nearly every device, so annotating it is
    // just noise.
    function attrVerb(type, attr, op, val) {
        const base = (attr || '').replace(/_\d+$/, '');
        const epM = (attr || '').match(/_(\d+)$/);
        const epTxt = (epM && +epM[1] >= 2) ? ` (outlet ${epM[1]})` : '';
        const truthy = v => v === true || v === 'true' || String(v).toUpperCase() === 'ON';
        if (type === 'contact') {
            if (base === 'is_open') return truthy(val) ? 'opens' : 'is shut';
            if (base === 'is_closed') return truthy(val) ? 'closes' : 'is open';
            if (base === 'contact') return truthy(val) ? 'closes' : 'opens';
        }
        if (base === 'action') {
            const m = { single: 'is single-pressed', double: 'is double-pressed',
                        triple: 'is triple-pressed', hold: 'is held',
                        release: 'is released', long: 'is long-pressed' };
            return m[val] || `sends “${esc(val)}”`;
        }
        if (/^(on|state)$/.test(base)) return (truthy(val) ? 'is on' : 'is off') + epTxt;
        const dispVal = Array.isArray(val) ? val.join(', ') : val;
        return `${OPW[op] || op} ${esc(dispVal)}${epTxt}`;
    }

    // "arrives at the Shops" / "leaves Home". "home" is per-user rather than a
    // configured place, so it is not in the lookup.
    function zoneVerb(c) {
        const one = p => p === 'any' ? 'any place'
            : p === 'home' ? 'Home'
            : (getPlace(p) || p);
        // A zone of several places reads as one destination: "arrives at Slough
        // or Osterley" — moving between them is movement inside the zone.
        const name = Array.isArray(c.place) ? c.place.map(one).join(' or ') : one(c.place);
        const verb = c.event === 'leave' ? 'leaves' : 'arrives at';
        return `${verb} <b>${esc(name)}</b>`;
    }

    // A trigger is the source device's first condition (or a schedule). The
    // text is a bare clause — the "When" label supplies the verb, so it is not
    // repeated here ("When" + "Door opens", not "When When Door opens").
    function triggerPhrase(rule) {
        const src = resolve(rule.source_ieee);
        const c = (rule.conditions || [])[0];
        if (!c) return { icon: src.type, text: `${devSpan(rule.source_ieee)} changes`, raw: '' };
        if (c.type === 'time_window')
            return { icon: 'time', text: `it's ${timePhrase(c.time_from, c.time_to)}, ${daySpan(c.days)}`,
                     raw: `time_window ${c.time_from}–${c.time_to}` };
        if (c.type === 'time')
            return { icon: 'time', text: `the time is <b>${esc(c.at)}</b>, ${daySpan(c.days)}`,
                     raw: `time ${c.at}` };
        if (c.type === 'sun')
            return { icon: 'time', text: `🌅 it's between ${esc(c.from)} and ${esc(c.to)}`,
                     raw: `sun ${c.from}→${c.to}` };
        if (c.type === 'zone')
            return { icon: src.type, text: `${devSpan(rule.source_ieee)} ${zoneVerb(c)}`,
                     raw: `zone ${c.event} ${c.place}` };
        return { icon: src.type,
                 text: `${devSpan(rule.source_ieee)} ${attrVerb(src.type, c.attribute, c.operator, c.value)}`,
                 raw: `${esc(c.attribute)} ${c.operator} ${esc(c.value)}` };
    }

    // A prerequisite / extra condition -> "ONLY IF …" phrase. Prerequisites
    // name their own device; a rule's 2nd+ trigger condition does not, because
    // it is implicitly about the rule's source — hence sourceIeee, which
    // callers pass for trigger conditions and omit for prerequisites.
    function condPhrase(p, sourceIeee) {
        const neg = p.negate ? '<span class="neg">NOT</span>' : '';
        if (p.type === 'zone')
            return { text: `${sourceIeee ? devSpan(sourceIeee) + ' ' : ''}${zoneVerb(p)}`,
                     raw: `zone ${p.event} ${p.place}` };
        if (p.type === 'time_window')
            return { text: `${neg}${timePhrase(p.time_from, p.time_to)}, ${daySpan(p.days)}`,
                     raw: `time_window ${p.time_from}–${p.time_to}` };
        if (p.type === 'sun')
            return { text: `${neg}🌅 between ${esc(p.from)} and ${esc(p.to)}`,
                     raw: `sun ${p.from}→${p.to}` };
        const ieee = p.ieee || sourceIeee;
        const dev = resolve(ieee);
        return { text: `${neg}${devSpan(ieee)} ${attrVerb(dev.type, p.attribute, p.operator, p.value)}`,
                 raw: `${ieee && String(ieee).startsWith('group:') ? ieee + ' ' : ''}${esc(p.attribute)} ${p.operator} ${esc(p.value)}` };
    }

    function cmdPhrase(s) {
        const verb = CMD_VERB[s.command] || esc(s.command);
        let tail = resolve(s.target_ieee).name;
        // Only annotate outlet 2+ on real (non-group) devices — outlet 1 is noise.
        if (s.endpoint_id >= 2 && !String(s.target_ieee).startsWith('group:'))
            tail += ` (outlet ${s.endpoint_id})`;
        if (s.command === 'brightness' && s.value != null) tail += ` to ${Math.round(s.value / 255 * 100)}%`;
        if (s.command === 'position' && s.value != null) tail += ` to ${esc(s.value)}%`;
        return `${verb} <span class="dev">${esc(tail)}</span>`;
    }

    // Player names come from /api/media/players; the raw player_id is the
    // fallback when the media service is off or the player has vanished.
    function mediaStepText(s) {
        const who = getPlayer(s.player_id) || s.player_id || 'player';
        const a = s.media_action || 'control';
        if (a === 'volume') return `set ${who} volume to ${Math.round((s.volume ?? 0) * 100)}%`;
        if (a === 'volume_adjust') {
            const d = s.delta || 0;
            return `turn ${who} volume ${d >= 0 ? 'up' : 'down'} ${Math.abs(Math.round(d * 100))}%`;
        }
        if (a === 'volume_fade') return `fade ${who} volume to ${Math.round((s.volume ?? 0) * 100)}% over ${s.fade_seconds || 300}s${s.stop_at_end ? ', then stop' : ''}`;
        if (a === 'announce') return `announce on ${who}: “${String(s.text || '').slice(0, 40)}”`;
        if (a === 'control') return `${s.control_action || 'control'} ${who}`;
        if (a === 'play_zone') return `play ${who}`;
        if (a === 'play_radio') return `play ${s.label || 'radio'} on ${who}`;
        if (a === 'play_tidal') return `play ${s.label || s.tidal_kind || 'Tidal'} on ${who}`;
        return `media: ${a}`;
    }

    // Render a then/else sequence into action lines, expanding parallel,
    // if_then_else and an offer's accept branch.
    function renderSeq(seq) {
        if (!seq || !seq.length) return '<span class="ap-raw">— nothing —</span>';
        let h = '';
        seq.forEach(s => {
            if (s.type === 'command')
                h += `<div class="ap-act"><i class="fas fa-bolt"></i><span>${cmdPhrase(s)}</span></div>`;
            else if (s.type === 'delay')
                h += `<div class="ap-act"><i class="fas fa-clock"></i><span>wait <span class="ap-delay">${esc(s.seconds)}s</span></span></div>`;
            else if (s.type === 'parallel') {
                const cmds = (s.branches || []).flat().filter(Boolean);
                h += `<div class="ap-par"><span class="ap-par-tag"><i class="fas fa-bolt"></i> at the same time</span>`
                   + cmds.map(c => c.type === 'command'
                        ? `<div class="ap-act"><i class="fas fa-bolt"></i><span>${cmdPhrase(c)}</span></div>`
                        : renderSeq([c])).join('')
                   + `</div>`;
            }
            else if (s.type === 'if_then_else') {
                const conds = (s.inline_conditions || []).map(c => {
                    const d = resolve(c.ieee);
                    return `${devSpan(c.ieee)} ${attrVerb(d.type, c.attribute, c.operator, c.value)}`;
                });
                const logic = ` ${s.condition_logic || 'and'} `;
                h += `<div class="ap-sub"><div class="ap-sub-head">Otherwise, if ${conds.join(logic) || '…'}:</div>${renderSeq(s.then_steps)}`;
                if ((s.else_steps || []).length)
                    h += `<div class="ap-sub-head" style="margin-top:6px">…else:</div>${renderSeq(s.else_steps)}`;
                h += `</div>`;
            }
            else if (s.type === 'wait_for') {
                const d = resolve(s.ieee);
                h += `<div class="ap-act"><i class="fas fa-hourglass-half"></i><span>wait for ${devSpan(s.ieee)} ${attrVerb(d.type, s.attribute, s.operator, s.value)}</span></div>`;
            }
            else if (s.type === 'condition') {
                const d = resolve(s.ieee);
                h += `<div class="ap-act"><i class="fas fa-filter"></i><span>only continue if ${devSpan(s.ieee)} ${attrVerb(d.type, s.attribute, s.operator, s.value)}</span></div>`;
            }
            else if (s.type === 'media')
                h += `<div class="ap-act"><i class="fas fa-music"></i><span>${esc(mediaStepText(s))}</span></div>`;
            else if (s.type === 'request')
                h += `<div class="ap-act"><i class="fas fa-comment"></i><span>message ${esc(s.to_user || '?')}: &ldquo;${esc(s.message || '')}&rdquo;`
                   + (s.from_user ? ` (from ${esc(s.from_user)})` : '') + `</span></div>`;
            else if (s.type === 'offer') {
                // The nested sequence is what makes an offer different from a
                // message, so it is shown rather than summarised as a count.
                h += `<div class="ap-act"><i class="fas fa-circle-question"></i><span>ask ${esc(s.to_user || '?')}: &ldquo;${esc(s.message || '')}&rdquo;</span></div>`;
                if ((s.accept_steps || []).length)
                    h += `<div class="ap-act-nested">${renderSeq(s.accept_steps)}</div>`;
            }
        });
        return h;
    }

    /**
     * The whole rule as one readable block: when, only if, then, otherwise.
     *
     * Used by the editor's live preview, where the point is to confirm at a
     * glance that an edit still says what was intended.
     */
    function rulePhrase(rule) {
        const trig = triggerPhrase(rule);
        const extra = (rule.conditions || []).slice(1)
            .map(c => condPhrase(c, rule.source_ieee).text);
        const joiner = (rule.condition_logic === 'or') ? ' or ' : ' and ';
        const prereqs = (rule.prerequisites || []).map(p => condPhrase(p).text);
        const when = [trig.text, ...extra].join(joiner);

        let h = `<div class="ap-act"><i class="fas fa-bolt"></i><span><strong>When</strong> ${when}</span></div>`;
        if (prereqs.length)
            h += `<div class="ap-act"><i class="fas fa-filter"></i><span><strong>only if</strong> ${prereqs.join(' and ')}</span></div>`;
        h += `<div class="ap-seq-head"><strong>then</strong></div>${renderSeq(rule.then_sequence)}`;
        if ((rule.else_sequence || []).length)
            h += `<div class="ap-seq-head"><strong>otherwise</strong></div>${renderSeq(rule.else_sequence)}`;
        return h;
    }

    return { resolve, devSpan, icon, daySpan, timePhrase, attrVerb, zoneVerb,
             triggerPhrase, condPhrase, cmdPhrase, mediaStepText, renderSeq,
             rulePhrase };
}
