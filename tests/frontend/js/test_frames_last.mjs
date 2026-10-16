// static/js/frames-page.js (the phone app) imported as shipped: launch draws the
// last devices and frame before the hub answers, takes no taps until it does,
// and fetches everything in parallel.
// Run: node tests/frontend/js/test_frames_last.mjs
import fs from 'node:fs';
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
globalThis.toast = { error() {}, info() {}, success() {} };
globalThis.location = { protocol: 'https:', host: 'hub.test' };
globalThis.WebSocket = class { constructor() { this.opened = true; } };

const store = new Map();
globalThis.localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: k => store.delete(k),
};

// Each request waits until the test answers it, so the page can be inspected
// in the gap where the hub hasn't replied yet.
let pending = [];
globalThis.fetch = url => new Promise((resolve, reject) => pending.push({ url, resolve, reject }));
function answer(url, body) {
  const i = pending.findIndex(p => p.url.startsWith(url));
  if (i < 0) throw new Error(`no request for ${url}: ${pending.map(p => p.url)}`);
  const [p] = pending.splice(i, 1);
  if (body instanceof Error) p.reject(body);
  else p.resolve({ ok: true, json: async () => structuredClone(body) });
}
const settle = () => new Promise(r => setTimeout(r, 0));

function page() {
  const grid = { html: '', set innerHTML(v) { this.html = v; }, get innerHTML() { return this.html; } };
  const classes = new Set();
  let ready = null;
  globalThis.document = {
    body: { classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) } },
    addEventListener: (type, fn) => { if (type === 'DOMContentLoaded') ready = fn; },
    getElementById: id => (id === 'framesGrid' ? grid : null),
    querySelector: () => null, querySelectorAll: () => [],
  };
  return { grid, stale: () => classes.has('frames-stale'), launch: () => ready() };
}

const AUTO = '/api/frames/auto?split=chamber';
const frame = label => ({ success: true, split: 'chamber', total: 1, groups: [
  { key: 'lounge', label, cells: [{ ieee: 'aa', name: 'Lamp', kind: 'switch', available: true }] }] });
const devices = on => [{ ieee: 'aa', friendly_name: 'Lamp', state: { state: on ? 'ON' : 'OFF' }, available: true }];

let n = 0;
async function launch() {
  pending = [];
  const p = page();
  await import(pathToFileURL(path.join(REPO, 'static/js/frames-page.js')) + `?${n++}`);
  p.launch();
  await settle();
  return p;
}
const { state } = await import(pathToFileURL(path.join(REPO, 'static/js/state.js')));

console.log('\n  first launch');
let p = await launch();
check('with nothing stored, nothing is drawn before the hub answers', p.grid.html === '' && !p.stale(), p.grid.html);
check('devices, saved frames and the frame are all asked for at once',
  ['/api/devices', '/api/frames', AUTO].every(u => pending.some(r => r.url === u)), pending.map(r => r.url));
answer(AUTO, frame('Lounge'));
answer('/api/frames', { success: true, frames: [] });
await settle();
check('a frame that lands first is drawn', p.grid.html.includes('Lounge'));
answer('/api/devices', devices(true));
await settle();
check('and kept, with the devices, for next launch',
  JSON.parse(store.get('zmm.frames.lastFrame') ?? 'null')?.url === AUTO
  && JSON.parse(store.get('zmm.frames.lastDevices') ?? 'null')?.[0]?.ieee === 'aa', [...store.keys()]);

console.log('\n  next launch');
p = await launch();
check('the last frame is drawn before the hub answers', p.grid.html.includes('Lounge') && pending.length === 3,
  [p.grid.html.slice(0, 60), pending.length]);
check('with the last device states behind it', state.deviceCache.aa?.state.state === 'ON', state.deviceCache.aa);
check('and takes no taps while it is stale', p.stale());
answer(AUTO, frame('Lounge (renamed)'));
answer('/api/frames', { success: true, frames: [] });
await settle();
check('still none while the devices are outstanding', p.stale());
answer('/api/devices', devices(false));
await settle();
check('live once everything has answered', !p.stale());
check('drawn from the fresh frame and devices',
  p.grid.html.includes('Lounge (renamed)') && state.deviceCache.aa.state.state === 'OFF');

console.log('\n  when the hub cannot be reached');
p = await launch();
check('the last frame shows at first', p.grid.html.includes('Lounge') && p.stale());
answer('/api/devices', new Error('offline'));
answer('/api/frames', new Error('offline'));
answer(AUTO, new Error('offline'));
await settle();
check('then says so, rather than leave old states on screen',
  p.grid.html.includes('Could not reach the hive') && !p.stale(), p.grid.html.slice(0, 80));

console.log('\n  another selection');
store.set('zmm.frames.lastFrame', JSON.stringify({ url: '/api/frames/auto?split=type', data: frame('By type') }));
p = await launch();
check("a stored frame for a different selection isn't drawn", !p.grid.html.includes('By type') && !p.stale(),
  p.grid.html.slice(0, 60));

console.log('\n  logout');
const auth = fs.readFileSync(path.join(REPO, 'static/js/auth.js'), 'utf8');
const logout = auth.slice(auth.indexOf('async function logout'), auth.indexOf('function hasScope'));
check('logging out clears both stored copies',
  ['zmm.frames.lastDevices', 'zmm.frames.lastFrame'].every(k => logout.includes(`localStorage.removeItem('${k}')`)));

console.log(fails.length ? `\n  ${fails.length} FAILED` : '\n  all passed');
process.exit(fails.length ? 1 : 0);
