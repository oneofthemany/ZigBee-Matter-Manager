// static/js/modal/control.js and overview.js imported as shipped: a sensor whose entry
// names its occupancy readings (FP300: radar `occupancy`, PIR `pir_detection`) gets a
// tile for each, and Overview no longer hides them for want of a standard cluster.
// Run: node tests/frontend/js/test_occupancy_tiles.mjs
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}

globalThis.zmmLog = () => ({ log() {}, debug() {}, warn() {}, error() {}, info() {} });
globalThis.echarts = { graphic: { LinearGradient: function () {} }, registerTheme() {}, init() {} };
globalThis.bootstrap = { Modal: function () {}, Tab: function () {} };
globalThis.window = globalThis;
globalThis.document = {
  addEventListener() {}, querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };

const { renderControlTab } = await import(pathToFileURL(path.join(REPO, 'static/js/modal/control.js')));
const { renderOverviewTab } = await import(pathToFileURL(path.join(REPO, 'static/js/modal/overview.js')));

const fp300 = (state) => ({
  ieee: '54:ef:44:10:01:65:4d:91', model: 'lumi.sensor_occupy.agl8', manufacturer: 'Aqara', type: 'EndDevice',
  capability_list: ['battery', 'motion_sensor', 'temperature_sensor'],
  capabilities: [{ id: 1, inputs: [0x0000, 0x0001, 0x0400, 0xFCC0].map(id => ({ id })), outputs: [] }],
  occupancy_readings: [{ name: 'occupancy', device_class: 'occupancy' }, { name: 'pir_detection', device_class: 'motion' }],
  named_raw_keys: [], config_schema: [], state,
});
const tiles = html => [...html.matchAll(/data-occupancy-reading="([^"]+)"/g)].map(m => m[1]);
const tile = (html, name) => html.split(`data-occupancy-reading="${name}"`)[1]?.split('data-occupancy-reading')[0] || '';

console.log('\n  the Control tab');
let html = renderControlTab(fp300({ occupancy: true, pir_detection: false }));
check('radar and PIR get a tile each', JSON.stringify(tiles(html)) === '["occupancy","pir_detection"]', tiles(html));
check('radar presence reads PRESENT while PIR is CLEAR',
  tile(html, 'occupancy').includes('PRESENT') && tile(html, 'pir_detection').includes('CLEAR'));
check('they are titled Presence and Motion',
  tile(html, 'occupancy').includes('Presence') && tile(html, 'pir_detection').includes('Motion'));
check('the merged Motion Sensor tile is not drawn as well', !html.includes('Motion Sensor'));
check('two tiles stack on a phone and sit side by side from sm up', html.includes('col-12 col-sm-6'));
html = renderControlTab(fp300({ occupancy: false, pir_detection: true }));
check('PIR alone reads MOTION and leaves presence CLEAR',
  tile(html, 'pir_detection').includes('MOTION') && tile(html, 'occupancy').includes('CLEAR'));

const plain = { ...fp300({ occupancy: true }), occupancy_readings: [], model: 'SML001' };
html = renderControlTab(plain);
check('a sensor with no entry readings keeps the single Motion Sensor tile',
  html.includes('Motion Sensor') && tiles(html).length === 0);

console.log('\n  the Overview tab');
html = renderOverviewTab(fp300({ occupancy: true, pir_detection: false, temperature: 20 }));
check('radar occupancy is listed, not filtered out for lacking cluster 0x0406', /occupancy/i.test(html), html.length);
html = renderOverviewTab({ ...fp300({ occupancy: true, temperature: 20 }), occupancy_readings: [] });
check('without entry readings or an occupancy cluster it is still hidden, as before', !/occupancy/i.test(html));

if (fails.length) { console.log(`\n  ${fails.length} failed`); process.exit(1); }
console.log('\n  all passed');
