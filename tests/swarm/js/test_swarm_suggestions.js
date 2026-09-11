// The Suggested sub-tab's logic in static/js/swarm-suggestions.js, sliced out
// of the shipped file so this tests the real code text rather than a copy.
//
// Covers what a browser would otherwise be needed to catch: that the filters
// and grouping treat a house-scoped suggestion (room === null) as the whole
// house rather than as a missing room, that a parameter typed into a card is
// coerced and clamped to what the compiler takes, and that a card escapes the
// device names it renders.
const fs = require('fs');
const path = require('path');
const REPO = path.resolve(__dirname, '../../..');
const src = fs.readFileSync(path.join(REPO, 'static/js/swarm-suggestions.js'), 'utf8');

function slice(startMark, endMark) {
  const start = src.indexOf(startMark);
  if (start < 0) { console.error('could not find: ' + startMark); process.exit(2); }
  const end = src.indexOf(endMark, start);
  if (end < 0) { console.error('could not find end: ' + endMark); process.exit(2); }
  return src.slice(start, end + endMark.length);
}

const consts = slice('const CONFIDENCE_BADGE = {', '};')
             + '\n' + slice('const CATEGORY_ICON = {', '};')
             + '\n' + slice("const HOUSE_KEY =", ';')
             + '\n' + slice("const HOUSE_LABEL =", ';');

const fns = [
  'export function esc(s) {',
  'export function applyFilters(items, f, dismissedIds) {',
  'export function groupByRoom(items) {',
  'export function coerceParam(spec, raw) {',
  'export function colourName(spec, value) {',
  'export function paramField(p) {',
  'export function cardHtml(s, opts) {',
  'export function summaryLine(sum, shown, hiddenDismissed) {',
].map(mark => slice(mark, '\n}').replace('export ', '')).join('\n');

const m = { exports: {} };
new Function('module', consts + '\n' + fns + `
  module.exports = { esc, applyFilters, groupByRoom, coerceParam, colourName,
                     paramField, cardHtml, summaryLine, HOUSE_KEY, HOUSE_LABEL };`)(m);
const S = m.exports;

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name +
              (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
  return !!cond;
}
function section(t) { console.log('\n  ' + t); }

// Suggestions exactly as /api/swarm/suggestions returns them.
const HALL = {
  id: 'sg_aaaa', pattern_id: 'motion_light', title: 'Light on motion',
  sentence: 'when motion is detected in Hallway, turn on Hall Light',
  category: 'lighting', room: 'hallway', room_label: 'Hallway',
  confidence: 'high', status: 'available',
  devices: [{ slot: 'sensor', ieee: '0xmotion', name: 'Motion - Hallway',
              offer: 'motion:detected', label: 'motion is detected' },
            { slot: 'light', ieee: '0xlight', name: 'Hall Light',
              offer: 'on_off:turn_on', label: 'turn on Hall Light' }],
  params: [{ id: 'clear_hold_s', label: 'Wait before off', type: 'int',
             unit: 's', min: 0, max: 3600, default: 120, value: 120 }],
  rule: { name: 'Light on motion', source_ieee: '0xmotion' },
};
const KITCHEN = { ...HALL, id: 'sg_bbbb', room: 'kitchen', room_label: 'Kitchen',
                  confidence: 'medium', category: 'lighting',
                  title: 'Light on motion',
                  sentence: 'when motion is detected in Kitchen, turn on Kitchen Spots',
                  devices: [{ slot: 'light', ieee: '0xk', name: 'Kitchen Spots',
                              offer: 'on_off:turn_on', label: 'turn on Kitchen Spots' }] };
// House-scoped: the matcher leaves room null on purpose.
const HOUSE = { ...HALL, id: 'sg_cccc', room: null, room_label: null,
                category: 'security', confidence: 'low',
                title: 'Lock up at night', sentence: 'at 23:00, lock the front door',
                devices: [{ slot: 'lock', ieee: '0xnuki', name: 'Front Door',
                            offer: 'lock:lock', label: 'lock Front Door' }] };
const BUILT = { ...HALL, id: 'sg_dddd', status: 'active', rule_id: 'r1',
                rule_name: 'Hall light', room: 'landing', room_label: 'Landing' };

const ALL = [HALL, KITCHEN, HOUSE, BUILT];
const NO_FILTER = { room: '', category: '', confidence: '', search: '',
                    showBuilt: false, showDismissed: false };
const ids = list => list.map(s => s.id);

section('applyFilters');
{
  const none = new Set();
  check('built suggestions are hidden by default',
        !ids(S.applyFilters(ALL, NO_FILTER, none)).includes('sg_dddd'),
        ids(S.applyFilters(ALL, NO_FILTER, none)));

  check('showBuilt brings them back',
        ids(S.applyFilters(ALL, { ...NO_FILTER, showBuilt: true }, none)).includes('sg_dddd'));

  const dismissed = new Set(['sg_bbbb']);
  check('a dismissed suggestion is hidden',
        !ids(S.applyFilters(ALL, NO_FILTER, dismissed)).includes('sg_bbbb'));
  check('showDismissed brings it back',
        ids(S.applyFilters(ALL, { ...NO_FILTER, showDismissed: true }, dismissed))
            .includes('sg_bbbb'));

  // The room <select> carries HOUSE_KEY for the house-scoped group, because a
  // null room cannot be an <option> value that round-trips.
  check('room filter selects a real room',
        ids(S.applyFilters(ALL, { ...NO_FILTER, room: 'kitchen' }, none)).join() === 'sg_bbbb');
  check('room filter selects the house-scoped group',
        ids(S.applyFilters(ALL, { ...NO_FILTER, room: S.HOUSE_KEY }, none)).join() === 'sg_cccc');

  check('category filter', 
        ids(S.applyFilters(ALL, { ...NO_FILTER, category: 'security' }, none)).join() === 'sg_cccc');
  check('confidence filter',
        ids(S.applyFilters(ALL, { ...NO_FILTER, confidence: 'medium' }, none)).join() === 'sg_bbbb');

  check('search matches a device name, not just the title',
        ids(S.applyFilters(ALL, { ...NO_FILTER, search: 'kitchen spots' }, none)).join() === 'sg_bbbb');
  check('search matches the room label',
        ids(S.applyFilters(ALL, { ...NO_FILTER, search: 'hallway' }, none)).join() === 'sg_aaaa');
  check('search is case-insensitive and trims',
        ids(S.applyFilters(ALL, { ...NO_FILTER, search: '  LOCK UP  ' }, none)).join() === 'sg_cccc');
}

section('groupByRoom');
{
  const groups = S.groupByRoom([KITCHEN, HALL, HOUSE]);
  check('house-scoped group sorts first', groups[0].key === S.HOUSE_KEY, groups.map(g => g.key));
  check('house-scoped group is labelled', groups[0].label === S.HOUSE_LABEL, groups[0].label);
  check('rooms follow alphabetically',
        groups.slice(1).map(g => g.label).join() === 'Hallway,Kitchen',
        groups.map(g => g.label));
  check('every suggestion lands in exactly one group',
        groups.reduce((n, g) => n + g.items.length, 0) === 3);
}

section('coerceParam');
{
  const int = { id: 'clear_hold_s', type: 'int', min: 0, max: 3600, value: 120 };
  const flt = { id: 'cold_c', type: 'float', min: 0, max: 30, value: 18 };
  check('int input is parsed', S.coerceParam(int, '240') === 240);
  check('a decimal typed into an int field truncates', S.coerceParam(int, '90.7') === 90);
  check('float input keeps its fraction', S.coerceParam(flt, '18.5') === 18.5);
  check('above max clamps down', S.coerceParam(int, '99999') === 3600);
  check('below min clamps up', S.coerceParam(int, '-5') === 0);
  check('garbage falls back to the suggested value', S.coerceParam(int, 'soon') === 120);
  check('an empty field falls back too', S.coerceParam(int, '') === 120);

  // A colour is carried as [hue, saturation]; the card offers the names.
  const colour = { id: 'alert_colour', type: 'colour', value: [0, 100],
                   choices: { red: [0, 100], amber: [40, 100] } };
  check('a colour choice resolves to its pair',
        JSON.stringify(S.coerceParam(colour, 'amber')) === '[40,100]');
  check('an unknown colour falls back to the suggested one',
        JSON.stringify(S.coerceParam(colour, 'chartreuse')) === '[0,100]');
  check('colourName names the current value', S.colourName(colour, [40, 100]) === 'amber');
  check('colourName falls back to the first choice',
        S.colourName(colour, [7, 7]) === 'red');
  check('a missing spec coerces to null', S.coerceParam(undefined, '3') === null);

  // Quiet hours and seasons: shaped strings, never numbers.
  const quiet = { id: 'quiet_from', type: 'time', value: '22:30' };
  check('a clock time is kept', S.coerceParam(quiet, '23:15') === '23:15');
  check('an impossible hour falls back', S.coerceParam(quiet, '25:00') === '22:30');
  check('a time is not parsed as a number', S.coerceParam(quiet, '7') === '22:30');
  const season = { id: 'season_from', type: 'monthday', value: '12-01' };
  check('a month-day is kept', S.coerceParam(season, ' 11-15 ') === '11-15');
  check('month 13 falls back', S.coerceParam(season, '13-01') === '12-01');
  check('a full date falls back', S.coerceParam(season, '2026-11-15') === '12-01');
}

section('paramField');
{
  const html = S.paramField({ id: 'clear_hold_s', label: 'Wait before off', type: 'int',
                              unit: 's', min: 0, max: 3600, value: 120 });
  check('carries the param id for read-back', html.includes('data-param="clear_hold_s"'));
  check('starts at the suggested value', html.includes('value="120"'));
  check('carries the pattern bounds', html.includes('min="0"') && html.includes('max="3600"'));
  check('shows the unit', html.includes('>s</span>'));

  const colour = S.paramField({ id: 'alert_colour', label: 'Colour', type: 'colour',
                                value: [40, 100],
                                choices: { red: [0, 100], amber: [40, 100] } });
  check('a colour renders a named select', colour.includes('<select') &&
        colour.includes('value="amber" selected'), colour);

  const time = S.paramField({ id: 'quiet_from', label: 'Quiet from', type: 'time', value: '22:30' });
  check('a time renders a time input', time.includes('type="time"') &&
        time.includes('data-param="quiet_from"') && time.includes('value="22:30"'), time);
  const day = S.paramField({ id: 'season_from', label: 'Season from', type: 'monthday', value: '12-01' });
  check('a month-day renders a text input with its shape', day.includes('type="text"') &&
        day.includes('placeholder="MM-DD"') && day.includes('value="12-01"'), day);
}

section('cardHtml escaping');
{
  const nasty = { ...HALL, id: 'sg_x"><script>alert(1)</script>',
                  title: '<img src=x onerror=alert(1)>',
                  devices: [{ slot: 'light', ieee: '0x1',
                              name: '</span><script>alert(2)</script>',
                              offer: 'on_off:turn_on', label: 'x" onmouseover="alert(3)' }] };
  const html = S.cardHtml(nasty, { editable: true, dismissed: false });
  check('no script tag survives', !/<script/i.test(html));
  check('no unescaped img tag survives', !/<img /i.test(html));
  check('the id is escaped inside the attribute', !html.includes('data-id="sg_x">'), html.slice(0, 200));
  // The quote is what matters, not the word: escaped, the label stays inside
  // the title attribute and the handler never becomes one.
  check('an offer label cannot break out of its title attribute',
        !html.includes('title="x" onmouseover') &&
        html.includes('&quot; onmouseover=&quot;'), html.slice(0, 400));
}

section('cardHtml states');
{
  const editable = S.cardHtml(HALL, { editable: true, dismissed: false });
  check('offers Create when the user may write', editable.includes('data-sg-action="create"'));
  check('offers Dismiss', editable.includes('data-sg-action="dismiss"'));
  check('renders the tunable parameter', editable.includes('data-param="clear_hold_s"'));

  const readonly = S.cardHtml(HALL, { editable: false, dismissed: false });
  check('no Create button without the scope', !readonly.includes('data-sg-action="create"'));

  const hidden = S.cardHtml(HALL, { editable: true, dismissed: true });
  check('a dismissed card offers Restore instead', hidden.includes('data-sg-action="restore"') &&
        !hidden.includes('data-sg-action="dismiss"'));

  const built = S.cardHtml(BUILT, { editable: true, dismissed: false });
  check('a built suggestion says so', built.includes('Already built'));
  check('a built suggestion offers no Create', !built.includes('data-sg-action="create"'));
  // Editing the parameters of a rule that exists belongs in the rule editor,
  // not here — this card cannot save them anywhere.
  check('a built suggestion hides its parameter fields', !built.includes('data-param='));

  const disabled = S.cardHtml({ ...BUILT, status: 'disabled' }, { editable: true });
  check('a disabled rule is distinguished from an enabled one',
        disabled.includes('Built, disabled'));

  const needs = { ...HALL, creates_workers: [{ id: 'house_mode', name: 'House <mode>', type: 'mode',
                                               options: ['home', 'away'] }] };
  const withWorker = S.cardHtml(needs, { editable: true, dismissed: false });
  check('a card says which worker it creates', withWorker.includes('Also creates') &&
        withWorker.includes('home, away'), withWorker);
  check('and escapes its name', withWorker.includes('House &lt;mode&gt;') &&
        !withWorker.includes('House <mode>'));
  check('a built card does not', !S.cardHtml({ ...BUILT, creates_workers: needs.creates_workers },
        { editable: true }).includes('Also creates'));
}

section('summaryLine');
{
  const sum = { patterns: 40, patterns_matched: 26, patterns_unmatched: 14,
                total: 106, available: 92, active: 14, disabled: 0 };
  const line = S.summaryLine(sum, 92, 3);
  check('says how many are shown', line.startsWith('Showing 92.'), line);
  check('says how many can be built', line.includes('92 to build'), line);
  check('says how many are already built', line.includes('14 already built'), line);
  check('says how many are dismissed', line.includes('3 dismissed'), line);
  check('names the patterns that matched nothing',
        line.includes('14 of 40 patterns matched nothing'), line);
  check('no summary renders nothing', S.summaryLine(null, 0, 0) === '');
  check('zero dismissed is not mentioned', !S.summaryLine(sum, 92, 0).includes('dismissed'));
}

console.log('\n' + (fails.length ? `  ${fails.length} FAILED` : '  all passed'));
process.exit(fails.length ? 1 : 0);
