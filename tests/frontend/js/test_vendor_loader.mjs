// static/js/vendor-loader.js and chart-utils.js as shipped: ECharts and Leaflet
// arrive on first use, once, and a chart made before ECharts is there still
// draws everything it was given.
// Run: node tests/frontend/js/test_vendor_loader.mjs
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const LOADER = fs.readFileSync(path.join(REPO, 'static/js/vendor-loader.js'), 'utf8');

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}
const tick = () => new Promise(r => setTimeout(r, 0));

// A page with only <head>: appended scripts are recorded, not fetched.
function page() {
  const scripts = [];
  const win = {};
  win.document = {
    createElement: () => ({ remove() { this.removed = true; } }),
    head: { appendChild: s => { scripts.push(s); } },
  };
  win.window = win;
  vm.runInNewContext(LOADER, win);
  return { win, scripts };
}

console.log('\n  loader');
{
  const { win, scripts } = page();
  const a = win.zmmLoadECharts(), b = win.zmmLoadECharts();
  check('two early callers share one script tag', scripts.length === 1, scripts.length);
  check('the tag points at the vendored ECharts', scripts[0].src === '/static/js/vendor/echarts.min.js', scripts[0].src);
  win.echarts = { id: 'ec' };
  scripts[0].onload();
  const [ra, rb] = await Promise.all([a, b]);
  check('both resolve to the loaded library', ra === win.echarts && rb === win.echarts);
  await win.zmmLoadECharts();
  check('a later caller adds no tag', scripts.length === 1, scripts.length);

  win.zmmLoadLeaflet();
  check('Leaflet is a separate tag from its vendored path', scripts.length === 2 && scripts[1].src === '/static/vendor/leaflet.js', scripts.map(s => s.src));
}
{
  const { win, scripts } = page();
  const first = win.zmmLoadLeaflet();
  scripts[0].onerror();
  let failed = false;
  try { await first; } catch (e) { failed = true; }
  check('a failed load rejects', failed);
  check('and its tag is removed', scripts[0].removed === true);
  win.zmmLoadLeaflet();
  check('the next use tries again', scripts.length === 2, scripts.length);
}
{
  const { win, scripts } = page();
  win.L = { id: 'leaflet' };
  const got = await win.zmmLoadLeaflet();
  check('a library already on the page is used as is', got === win.L && scripts.length === 0);
}

console.log('\n  chart-utils');
{
  // Stand-in browser globals the module reads.
  let release, reject;
  globalThis.window = globalThis;
  globalThis.zmmLoadECharts = () => new Promise((res, rej) => { release = res; reject = rej; });
  globalThis.ResizeObserver = class { observe() {} disconnect() {} };
  globalThis.requestAnimationFrame = f => f();
  const listeners = {};
  globalThis.document = {
    documentElement: { getAttribute: () => 'light' },
    addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); },
    removeEventListener: () => {},
  };
  const inits = [], themes = [];
  const ec = {
    registerTheme: n => themes.push(n),
    init: (el, theme) => { const i = { el, theme, opts: [], setOption(o, nm) { this.opts.push([o, nm]); }, resize() {}, dispose() { this.gone = true; } }; inits.push(i); return i; },
  };

  const { createChart } = await import(pathToFileURL(path.join(REPO, 'static/js/chart-utils.js')).href);
  check('importing the module needs no ECharts', themes.length === 0);

  const el = { clientWidth: 100, clientHeight: 50, isConnected: true };
  const chart = createChart(el);
  check('a chart is returned straight away', chart && typeof chart.setOption === 'function');
  check('with no instance before ECharts arrives', chart.instance() === null);
  chart.setOption({ a: 1 });
  chart.setOption({ b: 2 }, false);
  chart.resize();
  const gone = createChart({ isConnected: true });
  gone.dispose();

  release(ec);
  await tick();
  check('the themes are registered once it arrives', themes.join() === 'zbm-light,zbm-dark', themes);
  check('only the live chart is initialised', inits.length === 1 && inits[0].el === el, inits.length);
  check('in the page theme', inits[0].theme === 'zbm-light');
  check('queued options replay in order, merge flag kept',
        JSON.stringify(inits[0].opts) === JSON.stringify([[{ a: 1 }, true], [{ b: 2 }, false]]), inits[0].opts);
  check('instance() is the live instance', chart.instance() === inits[0]);

  chart.setOption({ c: 3 });
  check('later options go straight through', inits[0].opts.length === 3);

  const second = createChart({ isConnected: true });
  second.setOption({ x: 1 });
  second.setOption({ y: 2 });
  await tick();
  check('a second chart reuses the loaded library', themes.length === 2 && inits.length === 2, [themes.length, inits.length]);
  check('a full replace drops what was queued before it',
        JSON.stringify(inits[1].opts) === JSON.stringify([[{ y: 2 }, true]]), inits[1].opts);

  const before = inits.length;
  listeners.themechange[0]();
  check('a theme change re-inits and replays the last option',
        inits[0].gone && inits.length === before + 1 && inits.at(-1).el === el
        && JSON.stringify(inits.at(-1).opts) === JSON.stringify([[{ c: 3 }, true]]), inits.at(-1).opts);
}

const src = fs.readFileSync(path.join(REPO, 'static/index.html'), 'utf8');
const frames = fs.readFileSync(path.join(REPO, 'static/frames.html'), 'utf8');
console.log('\n  pages');
check('index.html loads neither library up front', !/<script[^>]+(echarts\.min|leaflet)\.js/.test(src));
check('frames.html loads neither library up front', !/<script[^>]+(echarts\.min|leaflet)\.js/.test(frames));
const at = s => src.indexOf(s);
check('index.html loads the loader before any module', at('vendor-loader.js') > 0 && at('vendor-loader.js') < at('type="module"'));
check('frames.html loads the loader before presence-badge', frames.indexOf('vendor-loader.js') > 0 && frames.indexOf('vendor-loader.js') < frames.indexOf('src="/static/js/presence-badge.js"'));

console.log(fails.length ? `\n  ${fails.length} failed` : '\n  all passed');
process.exit(fails.length ? 1 : 0);
