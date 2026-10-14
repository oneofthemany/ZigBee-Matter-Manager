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
  global.CSS = { escape: x => x };
  const dev = { ...device, attributes: [
    { ep: 1, cluster: '0xFCC0', attr: '0x0162', mfr: '0x115F', type: 'uint32', value: 600000, reported: true, writable: true },
    { ep: 1, cluster: '0xFCC0', attr: '0x0170', mfr: '0x115F', type: 'uint8', value: 1, reported: true, writable: true },
    { ep: 1, cluster: '0xFCC0', attr: '0x0142', mfr: '0x115F', type: 'uint8', value: 1, reported: true, writable: false },
    { ep: 1, cluster: '0xFCC0', attr: '0x0009', mfr: '0x115F', type: 'uint8', value: 0, reported: false, writable: true }] };
  const e = { id: 'x', match: { model: 'x' }, endpoints: {} };
  const K = attr => `1|0xFCC0|${attr}`;
  let html = editorHtml(e, dev);
  check('the attributes the device sends are listed with value and access',
        html.includes('0x0162') && html.includes('600000') && html.includes('settable') && html.includes('read-only'));
  check('the ones it only lists are behind a tick box', !html.includes('0x0009') && html.includes('Also show the 1'));
  applyField(e, el('attr-all', {}, '', true), dev);
  check('which shows them', editorHtml(e, dev).includes('0x0009'));

  // A number the device stores in milliseconds, shown in seconds, with its range (cf. a sampling period).
  applyField(e, el('attr-name', { key: K('0x0162') }, 'sampling_period'), dev);
  check('naming a settable one makes it a setting with its type and maker code, and opens its details',
        [e.zmm.settings[0].id, e.zmm.settings[0].type, e.zmm.settings[0].mfr].join() === 'sampling_period,uint32,0x115F'
        && editorHtml(e, dev).includes('Kind of value'));
  for (const [f, v] of [['unit', 'sec'], ['scale', '1000'], ['min', '0.5'], ['max', '3600'], ['step', '0.5'],
                        ['label', 'Sampling period'], ['description', 'How often readings are taken']]) {
    check(`its ${f} is set`, applyField(e, el('attr-f', { key: K('0x0162'), f }, v), dev) === null);
  }
  const st = e.zmm.settings[0];
  check('as the settings form needs it: seconds, 0.5 to 3600 in steps of 0.5, stored ×1000',
        JSON.stringify([st.unit, st.scale, st.min, st.max, st.step, st.label])
        === JSON.stringify(['sec', 1000, 0.5, 3600, 0.5, 'Sampling period']), st);
  check('and the list shows its value as the user will see it', editorHtml(e, dev).includes('600000 <span class="text-muted">→ 600'));
  check('a min above the max is refused', applyField(e, el('attr-f', { key: K('0x0162'), f: 'min' }, '5000'), dev) !== null);
  applyField(e, el('attr-f', { key: K('0x0162'), f: 'min' }, '0.5'), dev);
  check('so is a scale of 0', applyField(e, el('attr-f', { key: K('0x0162'), f: 'scale' }, '0'), dev) !== null);

  // One of a list, each raw value named (cf. sampling off / low / medium / high / custom).
  applyField(e, el('attr-name', { key: K('0x0170') }, 'sampling'), dev);
  applyField(e, el('attr-f', { key: K('0x0170'), f: 'kind' }, 'choice'), dev);
  const root = { querySelector: q => ({ [`[data-qe-new="attr-raw-${K('0x0170')}"]`]: { value: '0' },
                                        [`[data-qe-new="attr-label-${K('0x0170')}"]`]: { value: 'off' } })[q] };
  check('a value is added with what it means', applyButton(e, root, { dataset: { qeAdd: 'attr-value', key: K('0x0170') } }) === null);
  applyField(e, el('attr-v', { key: K('0x0170'), raw: '1' }, 'low'), dev);
  const sampling = e.zmm.settings.find(x => x.id === 'sampling');
  check('the setting carries the value names the form offers as choices',
        JSON.stringify(sampling.values) === JSON.stringify({ 0: 'off', 1: 'low' }), sampling.values);
  check('and the list shows the device\'s value by its name', editorHtml(e, dev).includes('1 <span class="text-muted">→ low'));
  check('an empty meaning is refused', applyField(e, el('attr-v', { key: K('0x0170'), raw: '1' }, ' '), dev) !== null);
  applyButton(e, root, { dataset: { qeDel: 'attr-value', key: K('0x0170'), raw: '0' } });
  check('and a value can be removed', !('0' in sampling.values));
  applyField(e, el('attr-f', { key: K('0x0170'), f: 'kind' }, 'onoff'), dev);
  check('on / off is the two values Off and On', JSON.stringify(sampling.values) === JSON.stringify({ 0: 'Off', 1: 'On' }));

  // A reading.
  applyField(e, el('attr-name', { key: K('0x0142') }, 'presence'), dev);
  applyField(e, el('attr-f', { key: K('0x0142'), f: 'kind' }, 'onoff'), dev);
  applyField(e, el('attr-f', { key: K('0x0142'), f: 'device_class' }, 'occupancy'), dev);
  const r = e.endpoints['1'].clusters['0xFCC0'].attributes['0x0142'];
  check('a read-only one is a reading; on / off makes it true or false, with its HA class',
        r.name === 'presence' && r.value_map[1] === true && r.value_map[0] === false && r.device_class === 'occupancy', r);

  applyField(e, el('attr-name', { key: K('0x0162') }, 'temp_sampling_period'), dev);
  check('renaming keeps everything its details say', e.zmm.settings.find(x => x.id === 'temp_sampling_period')?.scale === 1000);
  applyField(e, el('attr-name', { key: K('0x0162') }, ''), dev);
  check('clearing a name removes it, leaving nothing behind', !e.zmm.settings.some(x => x.attr === '0x0162'));
  check('a bad name is refused', applyField(e, el('attr-name', { key: K('0x0142') }, 'Bad Name'), dev) !== null);
}

console.log('what ZMM asks of the device, and actions');
{
  global.CSS = { escape: x => x };
  const dev = { ...device, attributes: [
    { ep: 1, cluster: '0xFCC0', attr: '0x0142', mfr: '0x115F', type: 'uint8', value: 1, reported: true, writable: false }] };
  const e = { id: 'x', match: { model: 'x' }, endpoints: {} };
  const K = '1|0xFCC0|0x0142';
  applyField(e, el('attr-name', { key: K }, 'presence'), dev);
  applyField(e, el('attr-rep-on', { key: K }, '', true), dev);
  check('reporting a maker\'s attribute carries its type and code: at once, and at least hourly',
        JSON.stringify(e.reporting) === JSON.stringify([{ ep: 1, cluster: '0xFCC0', attr: '0x0142', min: 0, max: 3600,
                                                          delta: 1, mfr: '0x115F', type: 'uint8' }]), e.reporting);
  applyField(e, el('attr-rep', { key: K, f: 'max' }, '1800'), dev);
  check('its gaps can be changed', e.reporting[0].max === 1800);
  check('a negative or fractional gap is refused',
        applyField(e, el('attr-rep', { key: K, f: 'min' }, '-1'), dev) !== null
        && applyField(e, el('attr-rep', { key: K, f: 'min' }, '1.5'), dev) !== null);
  applyField(e, el('attr-read', { key: K }, '', true), dev);
  check('it can be read when the device joins', JSON.stringify(e.reads) ===
        JSON.stringify([{ ep: 1, cluster: '0xFCC0', attr: '0x0142', mfr: '0x115F' }]), e.reads);
  check('the details show both ticked', /attr-rep-on[^>]*checked/.test(editorHtml(e, { ...dev, open_attributes: new Set([K]) })));
  applyField(e, el('attr-name', { key: K }, ''), dev);
  check('unnaming it drops what was asked of it', !e.reporting && !e.reads);

  const vals = { 'act-label': 'Restart device', 'act-attr': '0x00E8', 'act-type': 'uint8', 'act-value': '1' };
  const root = { querySelector: q => { const m = /data-qe-new="([^"]+)"/.exec(q); return m && { value: vals[m[1]] }; } };
  check('an action is added from a name, attribute, type and value',
        applyButton(e, root, { dataset: { qeAdd: 'action', ep: '1', cluster: '0xFCC0', mfr: '0x115F' } }) === null);
  check('as a write the Control tab can run: typed and with the maker\'s code',
        JSON.stringify(e.actions) === JSON.stringify([{ id: 'restart_device', label: 'Restart device', ep: 1, cluster: '0xFCC0',
          writes: [{ ep: 1, cluster: '0xFCC0', attr: '0x00E8', value: 1, type: 'uint8', mfr: '0x115F' }] }]), e.actions);
  vals['act-attr'] = 'zz';
  check('a bad attribute is refused', applyButton(e, root, { dataset: { qeAdd: 'action', ep: '1', cluster: '0xFCC0' } }) !== null);
  applyField(e, el('act-f', { i: '0', f: 'value' }, '2'), dev);
  check('its value can be changed', e.actions[0].writes[0].value === 2);
  applyButton(e, root, { dataset: { qeDel: 'action', i: '0' } });
  check('and it can be removed', e.actions.length === 0);
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
