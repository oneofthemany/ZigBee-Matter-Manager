// static/js/tab-loader.js as shipped, and main.js's use of it: a tab's module
// loads once on first open, behind a spinner; its own listeners still see the
// opening event; and nothing the page calls is missing before it loads.
// Run: node tests/frontend/js/test_tab_loader.mjs
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const JS = path.join(REPO, 'static/js');

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}
const tick = () => new Promise(r => setTimeout(r, 0));

// A stand-in DOM: just what tab-loader touches.
class El {
  constructor(attrs = {}) {
    this.attrs = { ...attrs }; this.children = []; this.parent = null; this.listeners = {};
    this.classList = { set: new Set(), contains(c) { return this.set.has(c); },
      add(c) { this.set.add(c); }, remove(c) { this.set.delete(c); } };
    this.innerHTML = ''; this.className = '';
  }
  getAttribute(k) { return this.attrs[k] ?? null; }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  prepend(c) { c.parent = this; this.children.unshift(c); }
  append(c) { c.parent = this; this.children.push(c); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this); this.parent = null; }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  dispatchEvent(e) {
    if (!e.target) Object.defineProperty(e, 'target', { value: this, configurable: true });
    for (let n = this; n; n = n.parent || (n !== doc ? doc : null)) {
      for (const f of n.listeners[e.type] || []) f.call(n, e);
      if (!e.bubbles) break;
    }
    return true;
  }
  querySelector(sel) {
    if (sel === 'button') { this._btn ||= new El(); return this._btn; }
    return null;
  }
  querySelectorAll() { return this.children.filter(c => c.attrs['data-bs-toggle'] === 'tab' && c.classList.contains('active')); }
}
const doc = new El();
doc.body = new El();
const panes = {}, buttons = {};
doc.getElementById = id => panes[id] ?? null;
doc.createElement = () => new El();
doc.querySelectorAll = sel => {
  const m = sel.match(/^\[data-bs-target="#([^"]+)"\]\.active$/);
  return m ? (buttons[m[1]] || []).filter(b => b.classList.contains('active')) : [];
};
function tab(id) {
  const pane = new El({ id }); panes[id] = pane; doc.body.append(pane);
  const btn = new El({ 'data-bs-target': '#' + id, 'data-bs-toggle': 'tab' }); doc.body.append(btn);
  (buttons[id] ||= []).push(btn);
  return { pane, btn };
}
const show = btn => { btn.classList.add('active'); btn.dispatchEvent(Object.assign(new Event('shown.bs.tab', { bubbles: true }), {})); };

globalThis.window = globalThis;
globalThis.document = doc;
globalThis.CSS = { escape: s => s };
const realError = console.error; console.error = () => {};
const { lazyTab, startTab, isLazyTab } = await import(pathToFileURL(path.join(JS, 'tab-loader.js')).href);

console.log('\n  first open');
{
  const { pane, btn } = tab('energy');
  let loads = 0, release;
  const seen = [];
  lazyTab('energy', () => { loads++; return new Promise(r => { release = r; }).then(() => {
    btn.addEventListener('shown.bs.tab', () => seen.push('module listener'));
  }); });
  check('a registered pane is lazy', isLazyTab('energy') && !isLazyTab('devices'));
  show(btn);
  const spin = pane.children[0];
  check('opening the tab shows a spinner in its pane at once', spin && /zmm-tab-loading/.test(spin.className) && /spinner-border/.test(spin.innerHTML));
  await tick();
  check('and starts its load', loads === 1);
  show(btn); await tick();
  check('opening it again meanwhile does not load twice', loads === 1);
  release(); await tick(); await tick();
  check('the spinner goes once the module is in', !pane.children.includes(spin));
  check("the module's own listener sees the opening event once", seen.length === 1, seen);
  check('the replay does not start another load', loads === 1);
  show(btn);
  check('later opens go straight to the module', loads === 1 && seen.length === 2, [loads, seen]);
}

console.log('\n  a sub-tab opened by a deep link while loading');
{
  const { pane, btn } = tab('settings');
  const sub = new El({ 'data-bs-target': '#settingsAI', 'data-bs-toggle': 'tab' });
  pane.append(sub);
  let release; const seen = [];
  lazyTab('settings', () => new Promise(r => { release = r; }).then(() => {
    sub.addEventListener('shown.bs.tab', () => seen.push('sub'));
  }));
  show(btn); await tick();
  sub.classList.add('active');
  sub.dispatchEvent(new Event('shown.bs.tab', { bubbles: true }));
  release(); await tick(); await tick();
  check("the active sub-tab's listener runs once the module is in", seen.join() === 'sub', seen);
}

console.log('\n  a handler clicked before the tab is opened');
{
  tab('groups');
  let loads = 0;
  lazyTab('groups', async () => { loads++; window.createGroup = function (a, b) { return [this, a, b]; }; },
    { globals: ['createGroup'] });
  check('the markup finds a function before load', typeof window.createGroup === 'function' && window.createGroup.zmmTabStub === 'groups');
  const self = { el: 1 };
  const got = await window.createGroup.call(self, 'x', 2);
  check('calling it loads the tab', loads === 1);
  check('and calls the real one with the same this and arguments', got[0] === self && got[1] === 'x' && got[2] === 2, got);
  check('which then replaces the stand-in', window.createGroup.zmmTabStub === undefined);

  tab('zones');
  lazyTab('zones', async () => {}, { globals: ['forgotten'] });
  let err = null;
  try { await window.forgotten(); } catch (e) { err = e; }
  check('a stand-in the module never defines fails loudly, not silently', err && /forgotten/.test(err.message), String(err));
}

console.log('\n  a failed load');
{
  const { pane, btn } = tab('media');
  let loads = 0, fail = true;
  lazyTab('media', async () => { loads++; if (fail) throw new Error('offline'); });
  show(btn); await tick(); await tick();
  const note = pane.children[0];
  check('says the tab failed to load, with a retry', note && /failed to load/.test(note.innerHTML) && /Retry/.test(note.innerHTML), note && note.innerHTML);
  fail = false;
  show(btn); await tick(); await tick();
  check('the next open tries again', loads === 2);
  await startTab('media');
  check('and startTab resolves once it is in', loads === 2);
}
console.error = realError;

// main.js and index.html as shipped.
console.log('\n  what main.js registers');
const main = fs.readFileSync(path.join(JS, 'main.js'), 'utf8');
const html = fs.readFileSync(path.join(REPO, 'static/index.html'), 'utf8');
const regs = main.split(/\nlazyTab\(/).slice(1).map(chunk => ({
  pane: chunk.match(/^'([^']+)'/)[1],
  modules: [...chunk.matchAll(/import\('\.\/([^']+)'\)/g)].map(m => m[1]),
  globals: [...(chunk.match(/globals:\s*\[([^\]]*)\]/)?.[1] || '').matchAll(/'([^']+)'/g)].map(m => m[1]),
}));
check('main.js registers the lazy tabs', regs.length >= 10, regs.map(r => r.pane));
for (const r of regs) {
  check(`pane #${r.pane} exists in index.html`, new RegExp(`id="${r.pane}"`).test(html));
}

// The eager graph: index.html's module tags and their static imports.
const STATIC_IMPORT = /^\s*import\s+(?:[^'"]*?\bfrom\s+)?['"]([^'"]+)['"]/gm;
const eager = new Set();
(function walkAll() {
  const roots = [...html.matchAll(/<script[^>]*type="module"[^>]*src="\/static\/js\/([^"]+)"/g)].map(m => path.join(JS, m[1]));
  const walk = f => { if (eager.has(f)) return; let s; try { s = fs.readFileSync(f, 'utf8'); } catch { return; } eager.add(f);
    for (const m of s.matchAll(STATIC_IMPORT)) walk(path.join(path.dirname(f), m[1])); };
  roots.forEach(walk);
})();
const lazyFiles = regs.flatMap(r => r.modules.map(m => ({ pane: r.pane, file: path.join(JS, m) })));
for (const { pane, file } of lazyFiles) {
  check(`${path.relative(JS, file)} (#${pane}) is not pulled in by the page's eager imports`, !eager.has(file));
}

// Window functions only a lazy module defines, called by markup or eager code.
console.log('\n  nothing the page calls is missing before its tab loads');
const eagerSrc = [...eager].map(f => fs.readFileSync(f, 'utf8'))
  .concat([...html.matchAll(/<script src="\/static\/(js\/[^"]+)"/g)].map(m => {
    try { return fs.readFileSync(path.join(REPO, 'static', m[1]), 'utf8'); } catch { return ''; } }))
  .join('\n');
const eagerLines = eagerSrc.split('\n');
const definedEagerly = name => new RegExp(`window\\.${name}\\s*=(?!=)|window\\[['"]${name}['"]\\]\\s*=|^function ${name}\\b|^\\s*${name}\\s*[:,]`, 'm').test(eagerSrc);
const inline = new Set([...html.matchAll(/ on[a-z]+="([^"]*)"/g)]
  .flatMap(m => [...m[1].matchAll(/(?:window\.)?([A-Za-z_$][\w$]*)\(/g)].map(x => x[1])));
// Each lazy module with everything it imports that the page doesn't already have.
const closure = [];
for (const { pane, file } of lazyFiles) {
  const walk = f => { if (eager.has(f) || closure.some(c => c.file === f)) return; let s;
    try { s = fs.readFileSync(f, 'utf8'); } catch { return; } closure.push({ pane, file: f });
    for (const m of s.matchAll(STATIC_IMPORT)) walk(path.join(path.dirname(f), m[1])); };
  walk(file);
}
check('the lazy tabs carry their own imports with them', closure.length > lazyFiles.length, closure.length);
let missing = [];
for (const { pane, file } of closure) {
  const src = fs.readFileSync(file, 'utf8');
  const defines = new Set([...src.matchAll(/window\.([A-Za-z_$][\w$]*)\s*=(?!=)/g)].map(m => m[1]));
  const reg = regs.find(r => r.pane === pane);
  for (const name of defines) {
    if (definedEagerly(name) || reg.globals.includes(name)) continue;
    if (inline.has(name)) missing.push(`${name} (#${pane}) is called by index.html markup`);
    // Eager code calling it without a guard (on its line or the one before)
    // would throw before the tab loads.
    const guard = new RegExp(`(?:typeof window\\.${name}|if \\(window\\.${name}\\b|window\\.${name} &&|window\\.${name}\\?\\.)`);
    const calls = eagerLines.flatMap((l, i) => l.includes(`window.${name}(`) ? [[eagerLines[i - 1] || '', l]] : []);
    if (calls.some(([prev, l]) => !guard.test(prev + l))) missing.push(`${name} (#${pane}) is called unguarded by eager code`);
  }
}
check('every such call has a stand-in or a guard', missing.length === 0, missing);

console.log(fails.length ? `\n  ${fails.length} failed` : '\n  all passed');
process.exit(fails.length ? 1 : 0);
