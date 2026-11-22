// The device Overview form's rules, run against the shipped static/js/modal/form-guard.js
// (docs/device-profiles.md §4, "Accepted").
//
// The claims: a device update never re-renders the form over an edit in progress;
// after a save the form shows what the device took; a save sends QoS only when its
// control changed; a battery device is asked to be woken first.
const fs = require('fs');
const path = require('path');
const REPO = path.resolve(__dirname, '../../..');
const src = fs.readFileSync(path.join(REPO, 'static/js/modal/form-guard.js'), 'utf8')
  .replace(/^export /gm, '');
const m = { exports: {} };
new Function('module', src + `
module.exports = { isSleepy, formIsBusy, shownValue, seedApplied, settleFields, verdicts,
                   storedQos, collectChanges };`)(m);
const g = m.exports;

const fails = [];
let passed = 0;
function check(name, cond, extra) {
  if (cond) { passed++; console.log('    ok   ' + name); }
  else { fails.push(name); console.log('    FAIL ' + name + (extra === undefined ? '' : '  <- ' + JSON.stringify(extra))); }
}

// A form as the Overview renders it: each control carries the value it showed.
const field = (name, value, initial) => ({ name, value: String(value), dataset: initial === undefined ? {} : { initial: String(initial) } });
function form(fields, { focused = null, saving = false } = {}) {
  const elements = fields.slice();
  fields.forEach(f => { elements[f.name] = f; });
  return { elements, dataset: saving ? { saving: '1' } : {}, contains: el => el !== null && fields.includes(el), _focused: focused };
}

console.log('c6: a device update does not re-render over unsaved edits');
{
  const timeout = field('motion_timeout', 60, 60), qos = field('qos', 2, 2);
  const f = form([timeout, qos]);
  check('a form as it was shown may be re-rendered', !g.formIsBusy(f, null));
  timeout.value = '120';
  check('a typed but unsaved value holds the form', g.formIsBusy(f, null));
  timeout.value = '60';
  check('focus in a control holds it, before anything is typed', g.formIsBusy(f, timeout));
  check('focus elsewhere on the page does not', !g.formIsBusy(f, { name: 'search' }));
  check('a save in flight holds it', g.formIsBusy(form([timeout], { saving: true }), null));
  check('no form, nothing to protect', !g.formIsBusy(null, null));
}

console.log('c7: after a save the form shows what the device took');
{
  const def = { name: 'motion_timeout', current_value: 60 };
  check('live state wins over the device list\'s older value',
        g.shownValue(def, { motion_timeout: 120 }) === 120);
  check('a waiting setting shows what was asked for, not the old state',
        g.shownValue({ ...def, pending: true, current_value: 120 }, { motion_timeout: 60 }) === 120);
  check('zones keep the decoded mask, not raw state bytes',
        g.shownValue({ name: 'zones', type: 'zones', current_value: 5 }, { zones: '0003ff' }) === 5);
  check('nothing known shows blank', g.shownValue({ name: 'x' }, {}) === '');

  const device = { state: { motion_timeout: 60 }, config_schema: [{ name: 'motion_timeout', current_value: 60 }] };
  g.seedApplied(device, { motion_timeout: 120 }, null);
  check('a taken value is put into the cached device for the next render',
        device.state.motion_timeout === 120 && device.config_schema[0].current_value === 120, device);
  check('and the next render shows it',
        g.shownValue(device.config_schema[0], device.state) === 120);

  const t = field('motion_timeout', 120, 60), s = field('sensitivity', 2, 1);
  const f = form([t, s]);
  g.settleFields(f, { applied: { motion_timeout: 120 }, failed: ['sensitivity'] });
  check('a taken value becomes the form\'s baseline, so updates may render again',
        t.dataset.initial === '120' && !g.formIsBusy(form([t]), null), t);
  check('a refused value goes back to what the device holds', s.value === '1', s);
  check('verdicts name each setting',
        JSON.stringify(g.verdicts({ applied: { a: 1 }, unconfirmed: ['b'], queued: ['c'], failed: ['d'] }))
        === JSON.stringify({ a: 'set', b: 'unconfirmed', c: 'queued', d: 'failed' }));
}

console.log('c8: QoS changes only when its control changed');
{
  check('the form shows the stored QoS', g.storedQos({ settings: { qos: 2 } }) === 2);
  check('a stored QoS of 0 is shown as 0, never a fallback', g.storedQos({ settings: { qos: 0 } }) === 0);
  check('no stored QoS shows 0', g.storedQos({}) === 0);
  const els = { qos: field('qos', 2, 2), motion_timeout: field('motion_timeout', 120, 60),
                polling_interval: field('polling_interval', 0, 0) };
  const out = g.collectChanges([['qos', '2'], ['motion_timeout', '120'], ['polling_interval', '0']], els);
  check('saving a setting sends no QoS', out.qos === null && out.pollingInterval === null, out);
  check('and sends the setting', JSON.stringify(out.updates) === '{"motion_timeout":120}', out.updates);
  els.qos.value = '1';
  const changed = g.collectChanges([['qos', '1']], els);
  check('a changed QoS control is sent', changed.qos === 1, changed);
}

console.log('c11: battery devices are asked to be woken first');
{
  check('a battery device', g.isSleepy({ power_source: 'Battery' }));
  check('an end device', g.isSleepy({ type: 'EndDevice' }));
  check('not a mains router', !g.isSleepy({ power_source: 'Mains', type: 'Router' }));
  check('nor an unknown device', !g.isSleepy(undefined));
}

console.log(`\n${passed} passed, ${fails.length} failed`);
process.exit(fails.length ? 1 : 0);
