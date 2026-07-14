/**
 * Automation humanization — shared semantic layer.
 *
 * Turns raw device/attribute/value identifiers into device-type-aware,
 * plain-English labels. Used by BOTH the rules list (automations-page.js)
 * and the rule builder (modal/automation.js) so phrasing stays consistent.
 *
 * Device typing is derived from `capability_list` on the main device list
 * (state.devices), with a name/model/state-key heuristic fallback.
 */

import { state } from './state.js';

// ── Device type → icon (FontAwesome) + human label ──
export const DEVICE_ICON = {
    contact:'fa-door-open', button:'fa-circle-dot', socket:'fa-plug', fan:'fa-fan',
    light:'fa-lightbulb', cover:'fa-window-maximize', motion:'fa-person-running',
    presence:'fa-satellite-dish', climate:'fa-temperature-half', time:'fa-clock',
    unknown:'fa-microchip',
};
export const DEVICE_LABEL = {
    contact:'Contact sensor', button:'Wireless button', socket:'Smart plug', fan:'Fan',
    light:'Light', cover:'Blinds / cover', motion:'Motion sensor', presence:'Presence sensor',
    climate:'Climate', time:'Schedule', unknown:'Device',
};
export const deviceIcon  = t => DEVICE_ICON[t]  || DEVICE_ICON.unknown;
export const deviceLabel = t => DEVICE_LABEL[t] || DEVICE_LABEL.unknown;

// capability_list lives on the main device list (state.devices), keyed by ieee.
function _capList(ieee) {
    const d = (state.devices || []).find(x => x.ieee === ieee);
    return d && Array.isArray(d.capability_list) ? d.capability_list : null;
}

/**
 * Resolve a semantic device type for an ieee (or group:N).
 * @param {string} ieee
 * @param {object} [summary] optional {friendly_name, model, state_keys} for heuristics
 */
export function deviceType(ieee, summary) {
    if (ieee === '__time__') return 'time';
    if (typeof ieee === 'string' && ieee.startsWith('group:')) {
        const m = (summary && summary.model || '').toLowerCase();
        if (m.includes('cover')) return 'cover';
        return 'light';   // most groups are lights / switches
    }
    const name = (summary && summary.friendly_name || '').toLowerCase();
    const model = (summary && summary.model || '').toLowerCase();
    const keys = (summary && summary.state_keys) || [];
    // A device that sends `action` events (or is named a button/remote) is a
    // button even if it also advertises a switch/on_off cluster.
    const looksButton = keys.includes('action') || /\b(button|remote)\b/.test(name);

    // Locks first — a lock's state keys (locked/lock_state) are unambiguous
    if (keys.includes('locked') || keys.includes('lock_state')) return 'lock';

    const caps = _capList(ieee);
    if (caps) {
        if (caps.includes('lock') || caps.includes('door_lock')) return 'lock';
        if (caps.includes('contact_sensor')) return 'contact';
        if (caps.includes('presence_sensor') || caps.includes('radar_sensor')) return 'presence';
        if (caps.includes('motion_sensor') || caps.includes('occupancy_sensing')) return 'motion';
        if (caps.includes('fan_control')) return 'fan';
        if (caps.includes('cover') || caps.includes('window_covering')) return 'cover';
        if (caps.includes('thermostat')) return 'climate';
        if (caps.includes('light')) return 'light';
        if (caps.includes('switch') || caps.includes('on_off')) return looksButton ? 'button' : 'socket';
        if (caps.includes('temperature_sensor') || caps.includes('humidity_sensor')) return 'climate';
    }
    if (model.includes('magnet') || keys.includes('is_open') || keys.includes('contact')) return 'contact';
    if (looksButton) return 'button';
    if (name.includes('fan')) return 'fan';
    if (name.includes('blind') || name.includes('curtain') || name.includes('cover')) return 'cover';
    if (name.includes('socket') || name.includes('plug')) return 'socket';
    if (/(light|lamp|pendant|led|bulb|spot)/.test(name)) return 'light';
    if (name.includes('motion')) return 'motion';
    if (name.includes('thermostat') || name.includes('trv')) return 'climate';
    return 'unknown';
}

// ── Attribute → friendly label ──
// Keyed by the base attribute (endpoint suffix _1/_2 stripped). Endpoint 2+ is
// annotated as "(outlet N)"; outlet 1 is the default and left unlabelled.
const ATTR_LABEL = {
    is_open:'Open / Closed', is_closed:'Closed / Open', contact:'Contact',
    action:'Button action', click:'Button click',
    occupancy:'Occupancy', presence:'Presence', presence_state:'Presence',
    on:'Power', state:'Power', brightness:'Brightness', color_temp:'Colour temperature',
    position:'Position', tilt:'Tilt',
    temperature:'Temperature', local_temperature:'Temperature', humidity:'Humidity',
    pressure:'Pressure', illuminance:'Light level', illuminance_lux:'Light level',
    battery:'Battery', voltage:'Voltage', power:'Power draw', energy:'Energy',
    smoke:'Smoke', water_leak:'Water leak', gas:'Gas', vibration:'Vibration',
    tamper:'Tamper', running_state:'Running',
    locked:'Locked / Unlocked', lock_state:'Lock state',
    door_state:'Door sensor', battery_critical:'Battery critical',
};

function _splitEndpoint(attr) {
    const m = (attr || '').match(/^(.*?)_(\d+)$/);
    if (m) return { base: m[1], ep: +m[2] };
    return { base: attr || '', ep: null };
}

const _titleCase = s => String(s).replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase()).trim();

/** Friendly label for an attribute, given the owning device's type. */
export function attrLabel(type, attribute) {
    const { base, ep } = _splitEndpoint(attribute);
    const label = ATTR_LABEL[base] || ATTR_LABEL[attribute] || _titleCase(base || attribute);
    return (ep && ep >= 2) ? `${label} (outlet ${ep})` : label;
}

// ── Value enums by attribute — the set of choices a value picker should offer,
// each as {value, label}. Returns null when the value is free/continuous.
const _BOOL = neg => [{ value:'true', label: neg ? neg[0] : 'Yes' },
                      { value:'false', label: neg ? neg[1] : 'No' }];
const _ONOFF = [{ value:'ON', label:'On' }, { value:'OFF', label:'Off' }];
const ACTION_ENUM = [
    { value:'single', label:'Single press' }, { value:'double', label:'Double press' },
    { value:'triple', label:'Triple press' }, { value:'hold', label:'Hold' },
    { value:'release', label:'Release' }, { value:'long', label:'Long press' },
];
const LOCK_STATE_ENUM = [
    { value:'locked', label:'Locked' }, { value:'unlocked', label:'Unlocked' },
    { value:'unlatched', label:'Unlatched' }, { value:'locking', label:'Locking' },
    { value:'unlocking', label:'Unlocking' }, { value:'motor blocked', label:'Motor blocked' },
];

/**
 * Enumerated value choices for an attribute (or null if free-form/numeric).
 * @param {string} type device type
 * @param {string} attribute
 * @param {string} [valType] optional value type hint ('boolean'|'string'|…)
 */
export function attrEnum(type, attribute, valType) {
    const { base } = _splitEndpoint(attribute);
    if (base === 'action' || base === 'click') return ACTION_ENUM;
    if (base === 'is_open')   return _BOOL(['Open', 'Closed']);
    if (base === 'is_closed') return _BOOL(['Closed', 'Open']);
    if (base === 'contact')   return _BOOL(['Closed', 'Open']);
    if (base === 'occupancy' || base === 'presence' || base === 'presence_state')
        return _BOOL(['Detected', 'Clear']);
    if (base === 'water_leak' || base === 'smoke' || base === 'gas' || base === 'tamper' || base === 'vibration')
        return _BOOL(['Detected', 'Clear']);
    if (base === 'locked') return _BOOL(['Locked', 'Unlocked']);
    if (base === 'lock_state') return LOCK_STATE_ENUM;
    if (base === 'battery_critical') return _BOOL(['Critical', 'OK']);
    if (base === 'on') return _BOOL(['On', 'Off']);
    if (base === 'state') return _ONOFF;
    if (valType === 'boolean') return _BOOL(['Yes', 'No']);
    return null;
}

/** Human label for a single raw value of an attribute. */
export function valueLabel(type, attribute, value) {
    const en = attrEnum(type, attribute);
    if (en) {
        const norm = String(value).toLowerCase();
        const hit = en.find(o => String(o.value).toLowerCase() === norm);
        if (hit) return hit.label;
    }
    return String(value);
}

/**
 * Canonical trigger attributes to offer for a device type even if the current
 * device state doesn't currently expose them (e.g. a button's transient
 * `action`). Returned as attribute descriptors compatible with the builder's
 * attribute <select> (attribute, type, operators, value_options, _synthetic).
 */
export function typeTriggerAttrs(type) {
    const enumVals = en => en.map(o => o.value);
    switch (type) {
        case 'button':
            return [{ attribute:'action', type:'string', operators:['eq','neq'],
                      value_options: enumVals(ACTION_ENUM), _synthetic:true }];
        case 'contact':
            return [{ attribute:'is_open', type:'boolean', operators:['eq','neq'],
                      value_options:['true','false'], _synthetic:true }];
        case 'motion':
        case 'presence':
            return [{ attribute: type === 'motion' ? 'occupancy' : 'presence',
                      type:'boolean', operators:['eq','neq'],
                      value_options:['true','false'], _synthetic:true }];
        case 'lock':
            // Offer lock triggers even before the first state poll lands
            return [
                { attribute:'locked', type:'boolean', operators:['eq','neq'],
                  value_options:['true','false'], _synthetic:true },
                { attribute:'lock_state', type:'string', operators:['eq','neq'],
                  value_options: enumVals(LOCK_STATE_ENUM), _synthetic:true },
            ];
        default:
            return [];
    }
}
