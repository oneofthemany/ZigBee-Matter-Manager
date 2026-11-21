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

function worker({ online = true, hashes = null, stores = new Map() } = {}) {
  const handlers = {};
  const network = [];
  const shellFetches = [];
  const href = req => typeof req === 'string' ? new URL(req, ORIGIN).href : req.url;
  const response = (body, ok = true) => ({
    body, ok, clone() { return response(body, ok); }, json: async () => JSON.parse(body),
  });
  const open = name => {
    if (!stores.has(name)) stores.set(name, new Map());
    const m = stores.get(name);
    return {
      put: async (req, res) => { m.set(href(req), res.body); },
      match: async req => m.has(href(req)) ? response(m.get(href(req))) : undefined,
      addAll: async urls => { for (const u of urls) { shellFetches.push(u); m.set(href(u), `shell:${u}`); } },
    };
  };
  // The /sw.js route's rewrite, as main.py does it.
  const src = hashes ? SRC.replace('var ASSET_HASHES = {};', `var ASSET_HASHES = ${JSON.stringify(hashes)};`) : SRC;
  const scope = {
    Response: function (body) { return response(body); },
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
  vm.runInNewContext(src, scope);

  async function request(p, method = 'GET') {
    let responded = null;
    handlers.fetch({ request: { url: ORIGIN + p, method }, respondWith: r => { responded = r; } });
    return responded ? await responded : 'passthrough';
  }
  const settle = () => new Promise(r => setTimeout(r, 0));
  return { handlers, stores, network, shellFetches, request, settle, name: vm.runInNewContext('CACHE_NAME', scope) };
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

console.log('\n  a deploy that changes one file');
{
  const u = p => `${ORIGIN}${p}`;
  const MANIFEST = u('/__zbm_asset_hashes__');
  const stores = new Map([['zbm-old', new Map([
    [MANIFEST, JSON.stringify({ '/static/js/a.js': 'h1', '/static/js/b.js': 'h1', '/static/js/gone.js': 'h1', '/static/css/table.css': 'h1' })],
    [u('/static/js/a.js'), 'old:a'], [u('/static/js/b.js'), 'old:b'], [u('/static/js/gone.js'), 'old:gone'],
    [u('/static/css/table.css'), 'old:table'],
  ])]]);
  const hashes = { '/static/js/a.js': 'h1', '/static/js/b.js': 'h2', '/static/css/table.css': 'h1' };
  const w = worker({ hashes, stores });
  let done;
  w.handlers.install({ waitUntil: p => { done = p; } });
  await done;
  const fresh = w.stores.get(w.name);
  check('an unchanged file is carried into the new cache', fresh.get(u('/static/js/a.js')) === 'old:a', [...fresh.keys()]);
  check('a changed file is not', !fresh.has(u('/static/js/b.js')));
  check('nor a file the deploy removed', !fresh.has(u('/static/js/gone.js')));
  check('an unchanged shell file is carried, not fetched again',
        fresh.get(u('/static/css/table.css')) === 'old:table' && !w.shellFetches.includes('/static/css/table.css'), w.shellFetches);
  check('the pages themselves are still fetched', w.shellFetches.includes('/') && w.shellFetches.includes('/frames'));
  check('the new cache records the map it was built against', fresh.get(MANIFEST) === JSON.stringify(hashes), fresh.get(MANIFEST));

  let done2;
  w.handlers.activate({ waitUntil: p => { done2 = p; } });
  await done2;
  check('activate still drops the old cache', !w.stores.has('zbm-old'), [...w.stores.keys()]);
  w.network.length = 0;
  let r = await w.request('/static/js/a.js');
  check('the unchanged file is served with no network request', r.body === 'old:a' && w.network.length === 0, [r.body, w.network]);
  r = await w.request('/static/js/b.js');
  check('the changed file is fetched', r.body === 'net:/static/js/b.js' && w.network.length === 1, [r.body, w.network]);
}
{
  // A cache built by a worker from before the map: nothing in it is known current.
  const u = p => `${ORIGIN}${p}`;
  const stores = new Map([['zbm-v12-legacy', new Map([[u('/static/js/a.js'), 'old:a']])]]);
  const w = worker({ hashes: { '/static/js/a.js': 'h1' }, stores });
  let done;
  w.handlers.install({ waitUntil: p => { done = p; } });
  await done;
  check('a cache without a map carries nothing over', !w.stores.get(w.name).has(u('/static/js/a.js')));
}
{
  const u = p => `${ORIGIN}${p}`;
  const stores = new Map([['someone-else', new Map([
    [u('/__zbm_asset_hashes__'), JSON.stringify({ '/static/js/a.js': 'h1' })], [u('/static/js/a.js'), 'foreign']])]]);
  const w = worker({ hashes: { '/static/js/a.js': 'h1' }, stores });
  let done;
  w.handlers.install({ waitUntil: p => { done = p; } });
  await done;
  check("another app's cache on the origin is not read", !w.stores.get(w.name).has(u('/static/js/a.js')));
}

console.log(fails.length ? `\n  ${fails.length} FAILED` : '\n  all passed');
process.exit(fails.length ? 1 : 0);
