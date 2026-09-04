// Two crowding fixes in the rule editor, sliced out of the shipped file so this
// tests the real code text rather than a copy of it.
//
// Both were visible only in a screenshot of a real rule: an attribute option
// that said the same word twice, and a zone note repeated once per media step.
const fs = require('fs');
const path = require('path');
const REPO = path.resolve(__dirname, '../../..');
const src = fs.readFileSync(path.join(REPO, 'static/js/modal/automation.js'), 'utf8');
const humanize = fs.readFileSync(path.join(REPO, 'static/js/automation-humanize.js'), 'utf8');

function slice(startMark, endMark) {
  const start = src.indexOf(startMark);
  if (start < 0) { console.error('could not find: ' + startMark); process.exit(2); }
  const end = src.indexOf(endMark, start);
  if (end < 0) { console.error('could not find end: ' + endMark); process.exit(2); }
  return src.slice(start, end + endMark.length);
}
const strip = t => t.replace(/^import .*$/gm, '').replace(/^export /gm, '');

const m = { exports: {} };
new Function('module', 'state',
  strip(humanize) + '\n'
  + slice('const _ATTR_CUR_MAX = 18;', '\n}') + '\n'
  + slice('function _allSteps(steps, out = []) {', '\n}') + '\n'
  + slice('function _firstUseOfPlayer(pid, sid) {', '\n}') + '\n'
  + `
  let thenTree = [], elseTree = [];
  module.exports = {
    _attrOptLabel, _allSteps, _firstUseOfPlayer,
    setTrees: (t, e) => { thenTree = t; elseTree = e; },
  };`
)(m, { devices: [] });
const E = m.exports;

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name +
              (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
  return !!cond;          // so `if (!check(...)) break;` works as it reads
}
function section(t) { console.log('\n  ' + t); }

section('an attribute option does not say the same word twice');
// The reported case: "Place  ·  place — now away" on a presence user.
const place = E._attrOptLabel({ attribute: 'place', current_value: 'away' }, 'presence_user');
console.log('    →  ' + place);
check('the friendly name is kept', place.includes('Place'), place);
check('the raw name is dropped when it is redundant',
      !/·\s*place/.test(place), place);
check('the live value is kept — the row cannot show it elsewhere',
      place.includes('now away'), place);

// An attribute whose raw name genuinely adds something keeps both halves.
const contact = E._attrOptLabel({ attribute: 'is_open', current_value: 'true' }, 'contact');
console.log('    →  ' + contact);
check('a differing raw name is kept', contact.includes('is_open'), contact);
check('with the separator', contact.includes('·'), contact);

section('a long live value is truncated rather than pushing the row apart');
const long = E._attrOptLabel(
  { attribute: 'note', current_value: 'a very long free text value indeed' }, 'generic');
check('shortened', long.length < 60, long);
check('and marked as elided', long.includes('…'), long);

section('a missing or empty live value is simply omitted');
for (const v of [undefined, null, '', '—']) {
  const out = E._attrOptLabel({ attribute: 'place', current_value: v }, 'presence_user');
  if (!check(`no "now" for ${JSON.stringify(v)}`, !out.includes('now'), out)) break;
}

section('every step is walked, including nested ones');
const nested = [
  { _id: 1, type: 'command' },
  { _id: 2, type: 'if_then_else',
    then_steps: [{ _id: 3, type: 'media', player_id: 'zone:1' }],
    else_steps: [{ _id: 4, type: 'command' }] },
  { _id: 5, type: 'offer', accept_steps: [{ _id: 6, type: 'command' }] },
  { _id: 7, type: 'parallel', branches: [[{ _id: 8, type: 'command' }]] },
];
const ids = E._allSteps(nested).map(s => s._id);
check('branches, accept steps and parallel arms are all reached',
      ids.sort((a, b) => a - b).join(',') === '1,2,3,4,5,6,7,8', ids);

section('the zone note lands on the first step that plays the zone');
// Two media steps on one zone: the note describes the zone, not the step.
E.setTrees([{ _id: 10, type: 'media', player_id: 'zone:1' },
            { _id: 11, type: 'media', player_id: 'zone:1' }], []);
check('shown on the first', E._firstUseOfPlayer('zone:1', 10) === true);
check('suppressed on the second', E._firstUseOfPlayer('zone:1', 11) === false);

E.setTrees([{ _id: 20, type: 'media', player_id: 'zone:1' }],
           [{ _id: 21, type: 'media', player_id: 'zone:1' }]);
check('THEN is read before ELSE', E._firstUseOfPlayer('zone:1', 20) === true,
      'else branch claimed it first');
check('so the ELSE copy is suppressed', E._firstUseOfPlayer('zone:1', 21) === false);

E.setTrees([{ _id: 30, type: 'media', player_id: 'zone:1' },
            { _id: 31, type: 'media', player_id: 'zone:2' }], []);
check('a different zone gets its own note', E._firstUseOfPlayer('zone:2', 31) === true);

E.setTrees([], []);
check('an unknown player does not suppress its own note',
      E._firstUseOfPlayer('zone:9', 99) === true);

section('a colour command survives the builder');
// hs_color carries [hue, saturation]; a serialiser that coerced it to a number
// or dropped it would silently turn a colour rule into nothing.
const bsrc2 = fs.readFileSync(path.join(REPO, 'static/js/modal/automation.js'), 'utf8');
const cs = bsrc2.indexOf('function _cleanTree(steps) {');
const ce = bsrc2.indexOf('\n}', bsrc2.indexOf('return false;', cs)) + 2;
const cm = { exports: {} };
new Function('module', 'isZoneId', 'zoneOf',
  bsrc2.slice(cs, ce) + '\nmodule.exports = { _cleanTree };')(cm, () => false, () => null);
const clean2 = cm.exports._cleanTree;
const [colourStep] = clean2([{ type: 'command', target_ieee: '0xbulb',
                               command: 'hs_color', value: [0, 100], endpoint_id: 1 }]);
check('the command survives', colourStep && colourStep.command === 'hs_color', colourStep);
check('the hue/saturation pair survives intact',
      JSON.stringify(colourStep.value) === '[0,100]', colourStep.value);
check('the endpoint survives', colourStep.endpoint_id === 1);

console.log('\n' + (fails.length ? fails.length + ' failed' : 'all passed'));
process.exit(fails.length ? 1 : 0);
