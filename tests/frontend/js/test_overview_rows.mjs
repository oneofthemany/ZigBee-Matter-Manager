// static/js/modal/overview.js imported as shipped: unnamed raw attributes stay in
// state for entry mapping and learning but never become Overview rows.
// Run: node tests/frontend/js/test_overview_rows.mjs
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}

globalThis.zmmLog = () => ({ log() {}, debug() {}, warn() {}, error() {}, info() {} });
globalThis.window = globalThis;
globalThis.document = {
  addEventListener() {}, querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };

const { renderOverviewTab } = await import(pathToFileURL(path.join(REPO, 'static/js/modal/overview.js')));

const rows = html => [...html.matchAll(/<td class="fw-bold small text-capitalize"[^>]*>([^<]+)<\/td>/g)].map(m => m[1]);
const html = renderOverviewTab({
  ieee: '54:ef:44:10:01:65:4d:91', model: 'lumi.sensor_occupy.agl8', manufacturer: 'Aqara', type: 'EndDevice',
  capabilities: [{ id: 1, inputs: [0x0000, 0x0001, 0x0400, 0xFCC0].map(id => ({ id })), outputs: [] }],
  named_raw_keys: [], config_schema: [],
  state: {
    humidity: 67.3, illuminance: 6.0, temperature: 20.01, battery: 100, power_outage_count: 17,
    battery_voltage: 2.977, checkin_interval: 16682,
    opple_0x0162: 360000, opple_0x0016: '4526169b', attr_0400_0004: 1, cluster_fcc0_attr_0199: 0,
  },
});
const shown = rows(html);

console.log('\n  the Overview rows');
check('readings are shown', ['humidity', 'illuminance', 'temperature', 'battery', 'power outage count']
  .every(r => shown.includes(r)), shown);
check('no unnamed raw attribute is shown', !shown.some(r => /^(opple|attr|cluster) /.test(r)), shown);
check('checkin interval and battery voltage are not shown',
  !shown.includes('checkin interval') && !shown.includes('battery voltage'), shown);

if (fails.length) { console.log(`\n${fails.length} failed`); process.exit(1); }
console.log('\nall passed');
