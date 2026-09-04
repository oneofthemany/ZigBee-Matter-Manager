// The chooser logic in static/js/swarm-suggest.js, extracted from the shipped
// file so this tests the real code text rather than a copy of it.
//
// Covers the two things a browser would otherwise be needed to catch: that a
// pairing is turned into a rule the engine's schema accepts, and that the
// rendered options escape device names rather than injecting them.
const fs = require('fs');
const path = require('path');
const REPO = path.resolve(__dirname, '../../..');
const src = fs.readFileSync(path.join(REPO, 'static/js/swarm-suggest.js'), 'utf8');

function slice(startMark, endMark) {
  const start = src.indexOf(startMark);
  if (start < 0) { console.error('could not find: ' + startMark); process.exit(2); }
  const end = src.indexOf(endMark, start);
  if (end < 0) { console.error('could not find end: ' + endMark); process.exit(2); }
  return src.slice(start, end + endMark.length);
}

const consts = slice('const CONFIDENCE_BADGE = {', '};')
             + '\n' + slice('const CATEGORY_ICON = {', '};')
             + '\n' + slice('const esc = s =>', "}[c]));");

const fns = slice('export function sourcedBy(suggestions, ieee) {', '\n}')
                .replace('export ', '')
          + '\n' + slice('function _ruleFromPairing(p) {', '\n}')
          + '\n' + slice('function _suggestionCard(s, i) {', '\n}')
          + '\n' + slice('function _pairingRow(p, i) {', '\n}');

const m = { exports: {} };
new Function('module', consts + '\n' + fns + `
  module.exports = { _ruleFromPairing, _suggestionCard, _pairingRow, esc, sourcedBy };`)(m);
const S = m.exports;

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name +
              (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}
function section(t) { console.log('\n  ' + t); }

// A pairing exactly as /api/swarm/pairings returns one.
const PAIRING = {
  source_ieee: '0xradar',
  source_name: 'Radar Sensor - Hallway',
  trigger: {
    key: 'presence:detected', capability: 'presence', role: 'trigger',
    label: 'someone is detected in Hallway', attribute: 'presence',
    condition: { type: 'attribute', attribute: 'presence', operator: 'eq', value: true },
  },
  target_ieee: '0xhalllight',
  target_name: 'Light - Hallway',
  action: {
    key: 'on_off:turn_on', capability: 'on_off', role: 'action',
    label: 'turn on Light - Hallway',
    step: { type: 'command', target_ieee: '0xhalllight', command: 'on',
            value: null, endpoint_id: 1 },
  },
  score: 11, confidence: 'high', same_room: true, same_device: false,
  sentence: 'When someone is detected in Hallway, turn on Light - Hallway',
};

section('a pairing becomes a rule the engine accepts');
const rule = S._ruleFromPairing(PAIRING);
check('source is the trigger device', rule.source_ieee === '0xradar', rule.source_ieee);
check('the trigger becomes the condition',
      JSON.stringify(rule.conditions) === JSON.stringify([PAIRING.trigger.condition]),
      rule.conditions);
check('the action becomes the THEN step',
      JSON.stringify(rule.then_sequence) === JSON.stringify([PAIRING.action.step]),
      rule.then_sequence);
check('every field add_rule requires is present',
      ['name', 'source_ieee', 'conditions', 'condition_logic', 'prerequisites',
       'then_sequence', 'else_sequence', 'cooldown'].every(k => k in rule),
      Object.keys(rule));
check('condition_logic defaults to and', rule.condition_logic === 'and');
check('no prerequisites are invented', rule.prerequisites.length === 0);
check('the endpoint survives', rule.then_sequence[0].endpoint_id === 1);

section('only suggestions this device triggers are offered');
// The builder saves with source_ieee = the current device, so offering one
// sourced elsewhere would save a rule against the wrong device.
const MIXED = [
  { title: 'radar drives the light', rule: { source_ieee: '0xradar' } },
  { title: 'the light is driven',    rule: { source_ieee: '0xother' } },
  { title: 'no rule at all' },
];
const kept = S.sourcedBy(MIXED, '0xradar');
check('keeps the one it triggers', kept.length === 1, kept.map(k => k.title));
check('keeps the right one', kept[0].title === 'radar drives the light');
check('drops one sourced elsewhere',
      !kept.some(k => k.title === 'the light is driven'));
check('a malformed entry is dropped, not thrown on',
      S.sourcedBy([{ title: 'x' }, null], '0xradar').length === 0);
check('an empty list is fine', S.sourcedBy([], '0xradar').length === 0);
check('a missing list is fine', S.sourcedBy(undefined, '0xradar').length === 0);

section('device names are escaped, not injected');
const nasty = { ...PAIRING, target_name: '<img src=x onerror=alert(1)>',
                sentence: 'When <script>alert(1)</script> fires' };
const row = S._pairingRow(nasty, 0);
check('no raw script tag survives', !row.includes('<script>'), row.slice(0, 120));
check('no raw img tag survives', !row.includes('<img src=x'), row.slice(0, 120));
check('it is escaped rather than dropped', row.includes('&lt;script&gt;'));

section('a suggestion card renders its parts');
const SUGGESTION = {
  id: 'sg_abc', pattern_id: 'presence_light_when_dark',
  title: 'Light on when the room is occupied after dark',
  category: 'lighting', confidence: 'high', room_label: 'Hallway',
  sentence: 'When someone is detected in Hallway and Hallway is dark, turn on Light - Hallway',
  params: [{ id: 'dark_lux', label: 'Dark below', value: 11, unit: 'lx' }],
  devices: [], status: 'available',
};
const card = S._suggestionCard(SUGGESTION, 0);
check('the title shows', card.includes('Light on when the room is occupied'));
check('the sentence shows', card.includes('turn on Light - Hallway'));
check('the room shows', card.includes('Hallway'));
check('the tunable parameter shows with its unit', card.includes('Dark below 11lx'));
check('the category icon is chosen', card.includes('fa-lightbulb'), card.slice(0, 200));
check('confidence is badged', card.includes('bg-success'));
check('it calls back with its index', card.includes("_swarmPick('suggestion', 0)"));

section('an unknown category still renders');
const odd = S._suggestionCard({ ...SUGGESTION, category: 'nonsense', params: [] }, 3);
check('falls back to a default icon', odd.includes('fa-diagram-project'));
check('and keeps its index', odd.includes("_swarmPick('suggestion', 3)"));

section('missing optional fields do not break rendering');
const bare = S._suggestionCard({ title: 'T', sentence: 'S', confidence: 'low' }, 1);
check('no room badge when there is no room', !bare.includes('bg-light text-dark border'));
check('renders anyway', bare.includes('>T<') || bare.includes('T</div>'));

section('the builder round-trips an offer without losing its action');
// An offer nested inside a rule must survive edit-and-save. Losing accept_steps
// would turn a rule that acts into a rule that only asks, silently.
const bsrc = fs.readFileSync(path.join(REPO, 'static/js/modal/automation.js'), 'utf8');
const cleanStart = bsrc.indexOf('function _cleanTree(steps) {');
const cleanEnd = bsrc.indexOf('\n}', bsrc.indexOf('return false;', cleanStart)) + 2;
if (cleanStart < 0 || cleanEnd < 2) { console.error('could not slice _cleanTree'); process.exit(2); }
const bm = { exports: {} };
new Function('module', 'isZoneId', 'zoneOf',
  bsrc.slice(cleanStart, cleanEnd) + '\nmodule.exports = { _cleanTree };'
)(bm, () => false, () => null);
const clean = bm.exports._cleanTree;

const OFFER = {
  type: 'offer', to_user: 'sean', message: 'Cool the house down?',
  expires_in: 1800,
  accept_steps: [{ type: 'command', target_ieee: '0xplug', command: 'on' }],
};
const [out] = clean([OFFER]);
check('the offer survives', out && out.type === 'offer', out);
check('the recipient survives', out.to_user === 'sean');
check('the question survives', out.message === 'Cool the house down?');
check('the expiry survives', out.expires_in === 1800, out.expires_in);
check('the action survives', out.accept_steps.length === 1, out.accept_steps);
check('and is cleaned as a real step',
      out.accept_steps[0].target_ieee === '0xplug' &&
      out.accept_steps[0].command === 'on', out.accept_steps[0]);

check('an offer with nothing to run is dropped',
      clean([{ ...OFFER, accept_steps: [] }]).length === 0);
check('an offer with no recipient is dropped',
      clean([{ ...OFFER, to_user: '' }]).length === 0);
check('an offer with no question is dropped',
      clean([{ ...OFFER, message: '   ' }]).length === 0);
check('a default expiry is applied',
      clean([{ ...OFFER, expires_in: undefined }])[0].expires_in === 3600);
check('an invalid nested step is dropped, not the whole offer',
      clean([{ ...OFFER, accept_steps: [{ type: 'command' }] }]).length === 0);

console.log('\n' + (fails.length ? fails.length + ' failed' : 'all passed'));
process.exit(fails.length ? 1 : 0);
