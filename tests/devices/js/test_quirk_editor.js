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

console.log('blob tags');
{
  const dev = { ...device, blob_tags: [{ tag: '0x17', blob: '0x00F7', value: 3211, map: null },
                                       { tag: '0x05', blob: '0x00F7', value: 1, map: 'voltage' }] };
  const e = { id: 'x', match: { model: 'x' }, endpoints: {}, zmm: { struct_tags: { '0x18': { name: 'battery', scale: 1 } } } };
  const html = editorHtml(e, dev);
  check('every tag the device sent is listed with its last value', html.includes('3211') && html.includes('0x05'));
  check('and what the global map would call one left unnamed', html.includes('unnamed (map: voltage)'));
  check('a tag the entry names but the device has not sent says so', html.includes('not seen'));
  applyField(e, el('tag-name', { key: '0x17' }, ''), dev);
  check('leaving a seen tag unnamed adds nothing to the entry', !('0x17' in e.zmm.struct_tags));
  applyField(e, el('tag-name', { key: '0x17' }, 'battery_voltage'), dev);
  applyField(e, el('tag-scale', { key: '0x17' }, '0.001'), dev);
  check('naming it, with a scale, makes it an entry tag',
        JSON.stringify(e.zmm.struct_tags['0x17']) === JSON.stringify({ name: 'battery_voltage', scale: 0.001 }));
  check('and the form shows the scaled value beside the raw one', editorHtml(e, dev).includes('3211 <span class="text-muted">→ 3.211'));
}

console.log('attributes');
{
  const dev = { ...device, attributes: [
    { ep: 1, cluster: '0xFCC0', attr: '0x0162', mfr: '0x115F', type: 'uint32', value: 600000, reported: true, writable: true },
    { ep: 1, cluster: '0xFCC0', attr: '0x0142', mfr: '0x115F', type: 'uint8', value: 1, reported: true, writable: false },
    { ep: 1, cluster: '0xFCC0', attr: '0x0009', mfr: '0x115F', type: 'uint8', value: 0, reported: false, writable: true }] };
  const e = { id: 'x', match: { model: 'x' }, endpoints: {} };
  let html = editorHtml(e, dev);
  check('the attributes the device sends are listed with value and access',
        html.includes('0x0162') && html.includes('600000') && html.includes('settable') && html.includes('read-only'));
  check('the ones it only lists are behind a tick box', !html.includes('0x0009') && html.includes('Also show the 1'));
  applyField(e, el('attr-all', {}, '', true), dev);
  check('which shows them', editorHtml(e, dev).includes('0x0009'));
  applyField(e, el('attr-name', { key: '1|0xFCC0|0x0162' }, 'sampling_period'), dev);
  applyField(e, el('attr-scale', { key: '1|0xFCC0|0x0162' }, '1000'), dev);
  const st = (e.zmm.settings || [])[0] || {};
  check('naming a settable one makes it a setting, with its type, maker code and scale',
        [st.id, st.type, st.attr, st.mfr, st.scale].join() === 'sampling_period,uint32,0x0162,0x115F,1000', st);
  check('and the form shows its value divided', editorHtml(e, dev).includes('600000 <span class="text-muted">→ 600'));
  applyField(e, el('attr-name', { key: '1|0xFCC0|0x0142' }, 'occupancy'), dev);
  check('naming a read-only one makes it a reading',
        e.endpoints['1'].clusters['0xFCC0'].attributes['0x0142'].name === 'occupancy');
  applyField(e, el('attr-name', { key: '1|0xFCC0|0x0162' }, ''), dev);
  check('clearing a name removes it, leaving nothing behind', !e.zmm.settings);
  check('a bad name is refused', applyField(e, el('attr-name', { key: '1|0xFCC0|0x0142' }, 'Bad Name'), dev) !== null);
}

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
