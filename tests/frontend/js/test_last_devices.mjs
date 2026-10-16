// static/js/devices.js imported as shipped: a reload draws the device table from
// the tab's last payload before /api/devices answers, and the fetch that follows
// re-renders only if something changed.
// Run: node tests/frontend/js/test_last_devices.mjs
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}

// Page globals the import graph touches at load (charts, Bootstrap, the logger).
globalThis.zmmLog = () => ({ log() {}, debug() {}, warn() {}, error() {}, info() {} });
globalThis.echarts = { graphic: { LinearGradient: function () {} }, registerTheme() {}, init() {} };
globalThis.bootstrap = { Modal: function () {}, Tab: function () {} };
globalThis.window = globalThis;

// Only the coordinator, so a render writes the tbody once and builds no rows.
const tbody = { renders: 0, removeAttribute() {}, set innerHTML(v) { if (v) this.renders++; }, get innerHTML() { return ''; } };
globalThis.document = {
  addEventListener() {}, querySelector: () => null, querySelectorAll: () => [],
  getElementById: id => (id === 'deviceTableBody' ? tbody : null),
};

let store = new Map();
let storageBlocked = false;
globalThis.sessionStorage = {
  getItem(k) { if (storageBlocked) throw new Error('blocked'); return store.has(k) ? store.get(k) : null; },
  setItem(k, v) { if (storageBlocked) throw new Error('blocked'); store.set(k, String(v)); },
  removeItem(k) { store.delete(k); },
};
let payload = null;
let fetches = 0;
globalThis.fetch = async () => { fetches++; return { ok: true, json: async () => structuredClone(payload) }; };

const { renderLastDevices, fetchAllDevices, LAST_DEVICES_KEY } =
  await import(pathToFileURL(path.join(REPO, 'static/js/devices.js')));
const { state } = await import(pathToFileURL(path.join(REPO, 'static/js/state.js')));

const coord = (name = 'Coordinator') => [{ ieee: '00:01', type: 'Coordinator', friendly_name: name, state: { a: 1 } }];
const reset = () => { state.devices = []; state.deviceCache = {}; tbody.renders = 0; fetches = 0; };

console.log('\n  drawing the last list');
reset();
check('with nothing stored, nothing is drawn', renderLastDevices() === false && tbody.renders === 0);

store.set(LAST_DEVICES_KEY, JSON.stringify(coord()));
check('a stored list is drawn without a request', renderLastDevices() === true && tbody.renders === 1 && fetches === 0,
  [tbody.renders, fetches]);
check('and fills the state and the cache Frames reads', state.devices.length === 1 && state.deviceCache['00:01']?.state.a === 1);

payload = coord();
await fetchAllDevices();
check('a fetch that brings the same list does not draw it again', fetches === 1 && tbody.renders === 1, tbody.renders);

payload = coord('Renamed');
await fetchAllDevices();
check('a fetch that brings a change draws it', tbody.renders === 2, tbody.renders);
check('and is what the next reload draws', JSON.parse(store.get(LAST_DEVICES_KEY))[0].friendly_name === 'Renamed');

check('a list already on screen is not replaced by the stored one', renderLastDevices() === false && tbody.renders === 2);

console.log('\n  when storage cannot help');
reset();
store.set(LAST_DEVICES_KEY, '{not json');
check('a corrupt entry is ignored', renderLastDevices() === false && tbody.renders === 0);
store.set(LAST_DEVICES_KEY, '{"a":1}');
check('so is one that is not a list', renderLastDevices() === false);

reset();
storageBlocked = true;
check('blocked storage draws nothing and does not throw', renderLastDevices() === false);
payload = coord('Fresh');
await fetchAllDevices(true);
check('and the fetch still renders', tbody.renders === 1 && state.devices[0].friendly_name === 'Fresh', tbody.renders);
storageBlocked = false;

console.log('\n  logout');
const auth = fs.readFileSync(path.join(REPO, 'static/js/auth.js'), 'utf8');
const logout = auth.slice(auth.indexOf('async function logout'), auth.indexOf('function hasScope'));
check('logging out clears the stored list, under the key devices.js uses',
  logout.includes(`sessionStorage.removeItem('${LAST_DEVICES_KEY}')`), LAST_DEVICES_KEY);

console.log(fails.length ? `\n  ${fails.length} FAILED` : '\n  all passed');
process.exit(fails.length ? 1 : 0);
