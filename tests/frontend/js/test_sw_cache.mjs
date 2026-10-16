// static/sw.js run as shipped in a stand-in worker scope: a page load served
// from the cache costs no network requests, which on HTTP/1.1 is what kept
// API calls queued behind ~100 asset revalidations.
// Run: node tests/frontend/js/test_sw_cache.mjs
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const SRC = fs.readFileSync(path.join(REPO, 'static/sw.js'), 'utf8');

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}

const ORIGIN = 'https://hub.test';

function worker({ online = true } = {}) {
  const handlers = {};
  const stores = new Map();           // cache name -> Map(url -> body)
  const network = [];
  const open = name => {
    if (!stores.has(name)) stores.set(name, new Map());
    const m = stores.get(name);
    return {
      put: async (req, res) => { m.set(req.url, res.body); },
      addAll: async urls => { for (const u of urls) m.set(new URL(u, ORIGIN).href, `shell:${u}`); },
    };
  };
  const response = (body, ok = true) => ({ body, ok, clone() { return response(body, ok); } });
  const scope = {
    self: null,
    console: { log() {}, warn() {} },
    location: { origin: ORIGIN },
    URL,
    Promise,
    caches: {
      open: async name => open(name),
      keys: async () => [...stores.keys()],
      delete: async name => stores.delete(name),
      match: async req => {
        const url = typeof req === 'string' ? new URL(req, ORIGIN).href : req.url;
        for (const m of stores.values()) if (m.has(url)) return response(m.get(url));
        return undefined;
      },
    },
    fetch: async req => {
      network.push(req.url);
      if (!online) throw new TypeError('offline');
      return response(`net:${new URL(req.url).pathname}`, !req.url.endsWith('/missing.js'));
    },
  };
  scope.self = {
    addEventListener: (type, fn) => { handlers[type] = fn; },
    skipWaiting() {}, clients: { claim() {}, matchAll: async () => [] },
    location: { origin: ORIGIN },
  };
  vm.runInNewContext(SRC, scope);

  async function request(p, method = 'GET') {
    let responded = null;
    handlers.fetch({ request: { url: ORIGIN + p, method }, respondWith: r => { responded = r; } });
    return responded ? await responded : 'passthrough';
  }
  const settle = () => new Promise(r => setTimeout(r, 0));
  return { handlers, stores, network, request, settle, name: vm.runInNewContext('CACHE_NAME', scope) };
}

console.log('\n  static assets');
let w = worker();
w.handlers.install({ waitUntil: () => {} });
await w.settle();
let r = await w.request('/static/js/devices.js');
await w.settle();
check('a first visit fetches the file', r.body === 'net:/static/js/devices.js' && w.network.length === 1, w.network);
check('and keeps it', w.stores.get(w.name).has(`${ORIGIN}/static/js/devices.js`));
w.network.length = 0;
r = await w.request('/static/js/devices.js');
await w.settle();
check('a cached file is served with no network request', r.body === 'net:/static/js/devices.js' && w.network.length === 0,
  w.network);
r = await w.request('/static/css/table.css');
check('an app-shell file is served from the install-time cache', r.body === 'shell:/static/css/table.css'
  && w.network.length === 0, [r.body, w.network]);
await w.request('/static/missing.js');
await w.settle();
check('an error response is not cached', !w.stores.get(w.name).has(`${ORIGIN}/static/missing.js`));

console.log('\n  everything else');
w = worker();
w.network.length = 0;
await w.request('/api/devices');
await w.request('/api/devices');
check('API calls always go to the network', w.network.length === 2, w.network);
check('writes are left to the browser', (await w.request('/api/devices', 'POST')) === 'passthrough');

console.log('\n  a new release or edit');
w = worker();
const old = 'zbm-old';
w.stores.set(old, new Map([[`${ORIGIN}/static/js/devices.js`, 'stale']]));
w.handlers.activate({ waitUntil: () => {} });
await w.settle();
check("the new worker drops the old worker's cache", !w.stores.has(old), [...w.stores.keys()]);
r = await w.request('/static/js/devices.js');
check('so the changed file comes from the network', r.body === 'net:/static/js/devices.js', r.body);

console.log(fails.length ? `\n  ${fails.length} FAILED` : '\n  all passed');
process.exit(fails.length ? 1 : 0);
