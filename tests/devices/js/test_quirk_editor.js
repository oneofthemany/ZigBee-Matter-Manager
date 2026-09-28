// The entry editor's edits, run against the shipped static/js/modal/quirk_editor.js.
//
// The claim: each form control writes the entry shape the Python normaliser
// accepts (device_profiles.valid_metering and _normalise_zmm), refuses what it
// would drop, and never leaves a half-made value behind.
const fs = require('fs');
const path = require('path');
const REPO = path.resolve(__dirname, '../../..');
const src = fs.readFileSync(path.join(REPO, 'static/js/modal/quirk_editor.js'), 'utf8')
  .replace(/^export /gm, '');
const m = { exports: {} };
new Function('module', src + '\nmodule.exports = { editorHtml, applyField, applyButton };')(m);
const { editorHtml, applyField, applyButton } = m.exports;

const fails = [];
let passed = 0;
function check(name, cond, extra) {
  if (cond) { passed++; console.log('    ok   ' + name); }
  else { fails.push(name); console.log('    FAIL ' + name + (extra === undefined ? '' : '  <- ' + JSON.stringify(extra))); }
}

const device = {
  model: 'lumi.plug.aeu002',
  endpoints: [
    { ep: 1, clusters: ['0x0006', '0x0012', '0x0702', '0x0B04'], kind: 'switch', label: 'Socket 1' },
    { ep: 2, clusters: ['0x0006', '0x0012', '0x0B04'], kind: 'switch', label: 'Socket 2' },
    { ep: 3, clusters: ['0x0006', '0x0B04'], kind: 'switch', label: 'USB' },
  ],
  measurements: {
    active_power: { cluster: '0x0B04', attr: '0x050B', ep: 1 },
    energy: { cluster: '0x0702', attr: '0x0000', ep: 1 },
  },
};
const el = (qe, data, value, checked) => ({ dataset: { qe, ...data }, value, checked });
const entry = { id: 'lumi.plug.aeu002', match: { model: 'lumi.plug.aeu002' }, endpoints: {} };

console.log('endpoints');
applyField(entry, el('ep-kind', { ep: '3' }, 'switch'), device);
applyField(entry, el('ep-label', { ep: '3' }, '  USB  '), device);
check('type and name land on the EP, trimmed', entry.endpoints['3'].kind === 'switch'
      && entry.endpoints['3'].label === 'USB', entry.endpoints);
applyField(entry, el('ep-kind', { ep: '3' }, ''), device);
check('"decided by evidence" removes the override', !('kind' in entry.endpoints['3']));

applyField(entry, el('ep-metering', { ep: '2' }, 'measures'), device);
check('"other EPs" starts from the EP itself', entry.endpoints['2'].metering === 'measures:2');
applyField(entry, el('ep-measures', { ep: '2', target: '3' }, '', true), device);
applyField(entry, el('ep-measures', { ep: '2', target: '1' }, '', true), device);
applyField(entry, el('ep-measures', { ep: '2', target: '2' }, '', false), device);
check('ticks build a sorted measures:<eps>', entry.endpoints['2'].metering === 'measures:1,3',
      entry.endpoints['2']);
applyField(entry, el('ep-measures', { ep: '2', target: '1' }, '', false), device);
const err = applyField(entry, el('ep-measures', { ep: '2', target: '3' }, '', false), device);
check('the last tick cannot be cleared', err && entry.endpoints['2'].metering === 'measures:3',
      entry.endpoints['2']);
applyField(entry, el('ep-metering', { ep: '1' }, 'device_total'), device);
check('a fixed scope is stored as is', entry.endpoints['1'].metering === 'device_total');
applyField(entry, el('ep-actions', { ep: '1' }, '', true), device);
check('button actions are multistate', entry.endpoints['1'].actions === 'multistate');

console.log('measurements');
applyField(entry, el('m-mode', { m: 'active_power' }, 'scaled'), device);
applyField(entry, el('m-num', { m: 'active_power', k: 'divisor' }, '10'), device);
check('scaling carries the cluster and attribute the normaliser needs',
      JSON.stringify(entry.zmm.measurements.active_power)
      === JSON.stringify({ cluster: '0x0B04', attr: '0x050B', multiplier: 1, divisor: 10 }),
      entry.zmm.measurements.active_power);
check('a divisor of 0 is refused, the old one kept',
      applyField(entry, el('m-num', { m: 'active_power', k: 'divisor' }, '0'), device)
      && entry.zmm.measurements.active_power.divisor === 10);
applyField(entry, el('m-mode', { m: 'energy' }, 'scaled'), device);
check('energy names the EP its meter is on', entry.zmm.measurements.energy.ep === 1);
applyField(entry, el('m-mode', { m: 'energy' }, 'absent'), device);
check('"not supported" is a null, not a missing key', entry.zmm.measurements.energy === null);

console.log('blob tags and presses');
const root = { querySelector: sel => ({ '[data-qe-new="tag"]': { value: '0x97' },
                                        '[data-qe-new="press"]': { value: '2' } })[sel] };
applyButton(entry, root, { dataset: { qeAdd: 'tag' } });
check('a new tag starts unpublished', entry.zmm.struct_tags['0x97'] === null, entry.zmm.struct_tags);
applyField(entry, el('tag-name', { key: '0x97' }, 'current'), device);
applyField(entry, el('tag-scale', { key: '0x97' }, '0.001'), device);
check('naming a tag publishes it with its scale',
      JSON.stringify(entry.zmm.struct_tags['0x97']) === JSON.stringify({ name: 'current', scale: 0.001 }));
check('a name the normaliser would drop is refused',
      applyField(entry, el('tag-name', { key: '0x97' }, 'Current A'), device)
      && entry.zmm.struct_tags['0x97'].name === 'current');
applyButton(entry, root, { dataset: { qeDel: 'tag', key: '0x97' } });
check('a tag can be removed', !('0x97' in entry.zmm.struct_tags));
applyButton(entry, root, { dataset: { qeAdd: 'press' } });
applyField(entry, el('press-name', { key: '2' }, 'double'), device);
check('a press value is named', entry.zmm.press_names['2'] === 'double');

console.log('the form');
const html = editorHtml(entry, device);
check('shows the EP picker for a measures scope', html.includes('data-qe="ep-measures" data-ep="2"'));
check('offers button actions only where the EP has 0x0012',
      /data-qe="ep-actions"\s+data-ep="3"\s+disabled/.test(html.replace(/\s+/g, ' ').replace(/ checked/g, '')));
check('escapes what it shows', !editorHtml({ endpoints: { 1: { label: '<b>' } } }, device).includes('<b>'));

console.log(`${passed} passed, ${fails.length} failed`);
process.exit(fails.length ? 1 : 0);
