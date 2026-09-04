// The shared humanizer in static/js/automation-sentence.js, extracted from the
// shipped file so this tests the real code text rather than a copy of it.
//
// It is the one voice the rules list and the rule editor both speak in, so a
// regression here shows up in two places at once.
const fs = require('fs');
const path = require('path');
const REPO = path.resolve(__dirname, '../../..');
const src = fs.readFileSync(path.join(REPO, 'static/js/automation-sentence.js'), 'utf8');
const humanize = fs.readFileSync(path.join(REPO, 'static/js/automation-humanize.js'), 'utf8');

// automation-humanize.js supplies DEVICE_ICON and deviceType; pull them in the
// same way so the phrasing is exercised against the real device typing.
function strip(text) {
  return text.replace(/^import .*$/gm, '').replace(/^export /gm, '');
}

// deviceType() reads the shared device list off state.js for capability hints,
// so it is stubbed with the same devices the humanizer is given.
const m = { exports: {} };
const STATE = { devices: [] };
new Function('module', 'state', strip(humanize) + '\n' + strip(src) +
  '\nmodule.exports = { createHumanizer, esc };')(m, STATE);
const { createHumanizer, esc } = m.exports;

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name +
              (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
  return !!cond;          // so `if (!check(...)) break;` works as it reads
}
function section(t) { console.log('\n  ' + t); }

const DEVICES = {
  '0xradar':  { ieee: '0xradar', friendly_name: 'Radar - Hallway',
                model: 'TS0601', state_keys: ['presence', 'illuminance_lux'] },
  '0xlight':  { ieee: '0xlight', friendly_name: 'Light - Hallway',
                model: 'Bulb', state_keys: ['state', 'brightness'] },
  '0xdoor':   { ieee: '0xdoor', friendly_name: 'Front Door',
                model: 'Contact', state_keys: ['contact'] },
  'group:3':  { ieee: 'group:3', friendly_name: '🔗 Downstairs', model: 'Light Group' },
  'user::sean': { ieee: 'user::sean', friendly_name: 'Sean', model: 'Presence User',
                  state_keys: ['presence', 'place'] },
};

STATE.devices = Object.values(DEVICES);

const H = createHumanizer({
  device: ieee => DEVICES[ieee],
  player: id => ({ p1: 'Kitchen Speaker' }[id]),
  place: id => ({ shops: 'The Shops' }[id]),
});

section('escaping');
check('angle brackets are escaped', esc('<b>x</b>') === '&lt;b&gt;x&lt;/b&gt;');
check('quotes are escaped', esc('a"b') === 'a&quot;b');
check('null becomes empty', esc(null) === '');

section('device names');
check('a known device reads by name',
      H.resolve('0xlight').name === 'Light - Hallway', H.resolve('0xlight'));
check('an unknown device falls back to its address',
      H.resolve('0xghost').name === '0xghost');
check('the group emoji is stripped',
      H.resolve('group:3').name === 'Downstairs', H.resolve('group:3'));
check('the time source has a name',
      H.resolve('__time__').name === 'Time / Alarm');

section('contact polarity reads correctly');
// The Zigbee convention is the confusing one: contact true means shut.
check('contact true is a close', H.attrVerb('contact', 'contact', 'eq', true) === 'closes');
check('contact false is an open', H.attrVerb('contact', 'contact', 'eq', false) === 'opens');
check('is_open true is an open', H.attrVerb('contact', 'is_open', 'eq', true) === 'opens');
check('is_closed true is a close', H.attrVerb('contact', 'is_closed', 'eq', true) === 'closes');

section('on/off and outlets');
check('state ON reads as on', H.attrVerb('light', 'state', 'eq', 'ON') === 'is on');
check('state OFF reads as off', H.attrVerb('light', 'state', 'eq', 'OFF') === 'is off');
check('outlet 1 is not annotated',
      H.attrVerb('switch', 'state_1', 'eq', 'ON') === 'is on');
check('outlet 2 is annotated',
      H.attrVerb('switch', 'state_2', 'eq', 'ON') === 'is on (outlet 2)');

section('numeric comparisons');
check('above', H.attrVerb('sensor', 'temperature', 'gt', 21) === 'is above 21');
check('below', H.attrVerb('sensor', 'illuminance_lux', 'lt', 11) === 'is below 11');
check('a list reads as a list',
      H.attrVerb('sensor', 'place', 'in', ['a', 'b']) === 'is one of a, b');

section('button actions');
check('double press', H.attrVerb('button', 'action', 'eq', 'double') === 'is double-pressed');
check('an unknown action is quoted',
      H.attrVerb('button', 'action', 'eq', 'wiggle').includes('wiggle'));

section('zones');
check('arriving', H.zoneVerb({ event: 'enter', place: 'home' }).startsWith('arrives at'));
check('leaving', H.zoneVerb({ event: 'leave', place: 'home' }).startsWith('leaves'));
check('a named place is resolved',
      H.zoneVerb({ event: 'enter', place: 'shops' }).includes('The Shops'));
check('several places read as one destination',
      H.zoneVerb({ event: 'enter', place: ['home', 'shops'] }).includes('Home or The Shops'));
check('any place', H.zoneVerb({ event: 'enter', place: 'any' }).includes('any place'));

section('commands');
check('on', H.cmdPhrase({ command: 'on', target_ieee: '0xlight' }).startsWith('Turn on'));
check('the device is named',
      H.cmdPhrase({ command: 'on', target_ieee: '0xlight' }).includes('Light - Hallway'));
check('brightness is shown as a percentage',
      H.cmdPhrase({ command: 'brightness', target_ieee: '0xlight', value: 128 }).includes('50%'),
      H.cmdPhrase({ command: 'brightness', target_ieee: '0xlight', value: 128 }));
check('outlet 2 is annotated',
      H.cmdPhrase({ command: 'on', target_ieee: '0xlight', endpoint_id: 2 }).includes('outlet 2'));
check('a group is not annotated with an outlet',
      !H.cmdPhrase({ command: 'on', target_ieee: 'group:3', endpoint_id: 2 }).includes('outlet'));

section('sequences');
const seq = H.renderSeq([
  { type: 'command', target_ieee: '0xlight', command: 'on' },
  { type: 'delay', seconds: 30 },
  { type: 'command', target_ieee: '0xlight', command: 'off' },
]);
check('every step appears', (seq.match(/ap-act/g) || []).length === 3, seq);
check('the delay reads in seconds', seq.includes('30s'));
check('an empty sequence says so', H.renderSeq([]).includes('nothing'));

section('an offer shows what happens on acceptance');
const offer = H.renderSeq([{
  type: 'offer', to_user: 'sean', message: 'Cool the house?',
  accept_steps: [{ type: 'command', target_ieee: '0xlight', command: 'on' }],
}]);
check('the question is shown', offer.includes('Cool the house?'));
check('the recipient is shown', offer.includes('sean'));
check('the nested action is shown', offer.includes('Light - Hallway'), offer);
check('and is nested, not inlined', offer.includes('ap-act-nested'));

section('a whole rule reads as one block');
const rule = {
  source_ieee: '0xradar',
  conditions: [
    { type: 'attribute', attribute: 'presence', operator: 'eq', value: true },
    { type: 'attribute', attribute: 'illuminance_lux', operator: 'lt', value: 11 },
  ],
  condition_logic: 'and',
  prerequisites: [{ ieee: '0xdoor', attribute: 'contact', operator: 'eq', value: false }],
  then_sequence: [{ type: 'command', target_ieee: '0xlight', command: 'on' }],
  else_sequence: [{ type: 'command', target_ieee: '0xlight', command: 'off' }],
};
const text = H.rulePhrase(rule);
check('it opens with When', text.includes('<strong>When</strong>'));
check('the trigger device is named', text.includes('Radar - Hallway'));
check('the second condition is joined with and', text.includes('and'), text);
check('the prerequisite is an only-if', text.includes('<strong>only if</strong>'));
check('the prerequisite names its own device', text.includes('Front Door'));
check('then is present', text.includes('<strong>then</strong>'));
check('otherwise is present', text.includes('<strong>otherwise</strong>'));

const orRule = { ...rule, condition_logic: 'or', prerequisites: [], else_sequence: [] };
const orText = H.rulePhrase(orRule);
check('or logic is honoured', orText.includes(' or '), orText);
check('no only-if when there are no prerequisites',
      !orText.includes('only if'));
check('no otherwise when there is no else branch',
      !orText.includes('otherwise'));

section('a zone rule reads as arriving, not as an attribute');
const zoneRule = {
  source_ieee: 'user::sean',
  conditions: [{ type: 'zone', event: 'enter', place: 'home' }],
  then_sequence: [{ type: 'command', target_ieee: '0xlight', command: 'on' }],
};
const zoneText = H.rulePhrase(zoneRule);
check('the person is named', zoneText.includes('Sean'));
check('and arrives', zoneText.includes('arrives at'), zoneText);

section('a rule with no conditions does not throw');
const bare = H.rulePhrase({ source_ieee: '0xradar', then_sequence: [] });
check('it renders something', bare.length > 0);
check('and says the source changes', bare.includes('changes'), bare);

section('a real saved rule: alarm trigger, place prerequisites, media steps');
// The Radio X Morning rule, as it is actually stored. This is the text the
// editor preview shows, so it has to read as prose rather than as fields.
DEVICES['__time__'] = { ieee: '__time__', friendly_name: 'Time / Alarm' };
DEVICES['user::charlie'] = { ieee: 'user::charlie', friendly_name: 'Charlie',
                             model: 'Presence User', state_keys: ['presence', 'place'] };
STATE.devices = Object.values(DEVICES);
const H2 = createHumanizer({
  device: ieee => DEVICES[ieee],
  player: id => ({ 'zone:1': 'the Home - Drone zone' }[id]),
  place: id => ({}[id]),
});
const radio = {
  source_ieee: '__time__',
  conditions: [{ type: 'time', at: '09:00', days: [0, 1, 2, 3, 4] }],
  condition_logic: 'and',
  prerequisites: [
    { ieee: 'user::sean', attribute: 'place', operator: 'eq', value: 'home' },
    { ieee: 'user::charlie', attribute: 'place', operator: 'eq', value: 'home' },
  ],
  then_sequence: [
    { type: 'media', player_id: 'zone:1', media_action: 'play_radio', label: 'Radio X' },
    { type: 'media', player_id: 'zone:1', media_action: 'volume', volume: 0.25 },
  ],
  else_sequence: [],
};
const rt = H2.rulePhrase(radio);
const plain = rt.replace(/<[^>]*>/g, '');
console.log('    →  ' + plain.replace(/\s+/g, ' ').trim());
check('the alarm time is named', plain.includes('09:00'), plain);
check('weekdays are collapsed, not listed', plain.includes('on weekdays'), plain);
check('both people are named as only-ifs',
      plain.includes('only if') && plain.includes('Sean') && plain.includes('Charlie'), plain);
check('the station is named', plain.includes('Radio X'), plain);
check('the volume reads as a percentage', plain.includes('25%'), plain);
check('the zone is named, not its id', !plain.includes('zone:1'), plain);
check('no otherwise branch is invented', !plain.includes('otherwise'), plain);

section('media steps');
check('announce', H.mediaStepText({ media_action: 'announce', player_id: 'p1', text: 'hello' })
      .includes('Kitchen Speaker'));
check('an unknown player falls back to its id',
      H.mediaStepText({ media_action: 'control', player_id: 'zz', control_action: 'pause' })
      .includes('zz'));

console.log('\n' + (fails.length ? fails.length + ' failed' : 'all passed'));
process.exit(fails.length ? 1 : 0);
