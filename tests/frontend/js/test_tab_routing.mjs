// static/js/tab-routing.js imported as shipped, over a stand-in DOM whose tab
// triggers behave like Bootstrap's: click shows the pane, then fires shown.bs.tab.
// Run: node tests/frontend/js/test_tab_routing.mjs
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const { initTabRouting, IDLE_RETURN_MS } =
  await import(pathToFileURL(path.join(REPO, 'static/js/tab-routing.js')));

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}

class Target {
  constructor() { this.listeners = {}; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, ev = {}) { for (const fn of this.listeners[type] || []) fn({ type, ...ev }); }
}

class El {
  constructor(id, classes = [], parent = null) {
    this.id = id;
    this.classList = new Set(classes);
    this.classList.contains = this.classList.has;
    this.parentElement = parent;
    this.dataset = {};
  }
  closest(sel) {
    const cls = sel.slice(1);
    for (let e = this; e; e = e.parentElement) if (e.classList.has(cls)) return e;
    return null;
  }
}
class Iframe extends El {}

// Page: devices, heating, settings › (notifications, network); one modal.
function page({ hash = '', navType = 'navigate', now = 0 } = {}) {
  const root = new El('root');
  const panes = {};
  const triggers = {};
  const addPane = (id, parent, active) => {
    panes[id] = new El(id, ['tab-pane', ...(active ? ['active'] : [])], parent);
    const t = new El(`tab-${id}`, [], parent);
    t.dataset.bsTarget = `#${id}`;
    t.click = () => {
      for (const p of Object.values(panes)) if (p.parentElement === panes[id].parentElement) p.classList.delete('active');
      panes[id].classList.add('active');
      clicks.push(id);
      doc.dispatch('shown.bs.tab', { target: t });
    };
    triggers[id] = t;
  };
  const clicks = [];
  addPane('devices', root, true);
  addPane('heating', root);
  addPane('settings', root);
  addPane('settingsNotifications', panes.settings, true);
  addPane('settingsNetwork', panes.settings);
  const modal = new El('deviceModal', ['modal', 'show']);
  modal.open = false;

  const doc = new Target();
  doc.hidden = false;
  doc.activeElement = root;
  doc.getElementById = id => panes[id] || (id === 'messages' ? new El('messages', ['offcanvas']) : null);
  doc.querySelector = sel => {
    const m = sel.match(/data-bs-target="#([^"]+)"/);
    return m ? triggers[m[1].replace(/\\/g, '')] || null : null;
  };
  doc.querySelectorAll = sel => (sel === '.modal.show' && modal.open ? [modal] : []);

  const win = new Target();
  const loc = { pathname: '/', search: '', hash };
  const entries = [hash];
  let at = 0;
  const go = url => { loc.hash = url.startsWith('#') ? url : (url.split('#')[1] ? `#${url.split('#')[1]}` : ''); };
  const hist = {
    pushState(_, __, url) { entries.splice(at + 1); entries.push(url); at++; go(url); },
    replaceState(_, __, url) { entries[at] = url; go(url); },
    back() { at--; go(entries[at] || ''); win.dispatch('popstate'); },
    get length() { return entries.length; },
  };

  const clock = { t: now };
  Object.assign(globalThis, {
    document: doc, window: win, location: loc, history: hist,
    CSS: { escape: s => s }, HTMLIFrameElement: Iframe,
    bootstrap: { Modal: { getInstance: m => ({ hide() { m.open = false; } }) } },
  });
  Object.defineProperty(globalThis, 'performance', { configurable: true, value: { getEntriesByType: () => [{ type: navType }] } });
  Date.now = () => clock.t;

  const returns = [];
  initTabRouting({ onReturn: () => returns.push(clock.t) });
  const active = () => Object.values(panes).filter(p => p.parentElement === root && p.classList.has('active')).map(p => p.id)[0];
  return { doc, win, loc, hist, clock, modal, clicks, returns, active, panes, root, Iframe };
}

const tick = () => new Promise(r => setTimeout(r, 0));

async function away(p, how, ms) {
  if (how === 'hidden') { p.doc.hidden = true; p.doc.dispatch('visibilitychange'); p.win.dispatch('blur'); }
  else p.win.dispatch('blur');
  await tick();
  p.clock.t += ms;
  if (how === 'hidden') { p.doc.hidden = false; p.doc.dispatch('visibilitychange'); }
  p.win.dispatch('focus');
}

console.log('\n  loading the page');
let p = page({ hash: '#heating', navType: 'reload' });
check('a reload lands on the device page', p.active() === 'devices', p.active());
check('and drops the tab from the URL without adding a Back step',
  p.loc.hash === '' && p.hist.length === 1, [p.loc.hash, p.hist.length]);

p = page({ hash: '#settingsNetwork', navType: 'reload' });
check('a reload from a sub-tab lands on the device page too', p.active() === 'devices', p.active());

p = page({ hash: '#heating', navType: 'navigate' });
check('a bookmark or link still opens the tab it names', p.active() === 'heating' && p.loc.hash === '#heating');

p = page({ hash: '#settingsNetwork', navType: 'back_forward' });
check('a sub-tab link opens its parent, then the sub-tab',
  p.active() === 'settings' && p.panes.settingsNetwork.classList.has('active') && p.clicks.join() === 'settings,settingsNetwork',
  p.clicks);

p = page({ hash: '#messages', navType: 'reload' });
check("a reload keeps a hash that isn't a page tab for its owner", p.loc.hash === '#messages');

console.log('\n  coming back');
p = page();
document.querySelector('[data-bs-target="#heating"]').click();
check('switching tab records it in the URL', p.loc.hash === '#heating' && p.hist.length === 2);
p.modal.open = true;
await away(p, 'hidden', IDLE_RETURN_MS);
check('back after five minutes hidden is on the device page', p.active() === 'devices' && p.loc.hash === '#devices',
  [p.active(), p.loc.hash]);
check('with any open modal closed', !p.modal.open);
check('and the device list refreshed, once', p.returns.length === 1, p.returns);
p.hist.back();
check('Back returns to where they left off', p.active() === 'heating', p.active());

p = page();
document.querySelector('[data-bs-target="#heating"]').click();
await away(p, 'hidden', IDLE_RETURN_MS - 1000);
check('a shorter absence changes nothing', p.active() === 'heating' && p.returns.length === 0);

p = page();
document.querySelector('[data-bs-target="#heating"]').click();
await away(p, 'blur', IDLE_RETURN_MS);
check('five minutes in another window counts as away', p.active() === 'devices');

p = page();
document.querySelector('[data-bs-target="#heating"]').click();
p.doc.activeElement = new p.Iframe('cameraPlayer', [], p.root);
p.win.dispatch('blur');
await tick();
p.clock.t += IDLE_RETURN_MS * 2;
p.doc.activeElement = p.root;
p.win.dispatch('focus');
check('time spent inside one of our iframes is not away', p.active() === 'heating' && p.returns.length === 0);

p = page();
const before = p.hist.length;
await away(p, 'hidden', IDLE_RETURN_MS);
check('already on the device page: refreshed, but no extra Back step',
  p.returns.length === 1 && p.hist.length === before, [p.returns.length, p.hist.length, before]);

console.log(fails.length ? `\n  ${fails.length} FAILED` : '\n  all passed');
process.exit(fails.length ? 1 : 0);
