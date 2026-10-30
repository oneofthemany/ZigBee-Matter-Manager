// static/js/devices.js + table-utils.js imported as shipped: the device table's
// sort is saved to the account on a header click and restored on load, by
// column key, and survives the table being rebuilt.
// Run: node tests/frontend/js/test_device_sort.mjs
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

// Just enough table for table-utils: header cells with data-col, rows with sort values.
const cls = () => { const s = new Set(); return { toggle: (c, on) => (on ? s.add(c) : s.delete(c)), has: c => s.has(c) }; };
const th = (col, type) => ({ dataset: { col, sortType: type }, classList: cls() });
const heads = [th('name', 'string'), th('lqi', 'number'), th('last_seen', 'number')];
const row = (name, lqi, seen) => ({
  dataset: {}, querySelector: () => null,
  cells: [name, lqi, seen].map(v => ({ dataset: { sortValue: String(v) }, textContent: String(v) })),
});
class FakeTable extends EventTarget {
  constructor() {
    super();
    this.dataset = {};
    this.tHead = { rows: [{ cells: heads }] };
    this.body = { rows: [], appendChild: r => { this.body.rows = this.body.rows.filter(x => x !== r).concat(r); } };
    this.tBodies = [this.body];
  }
}
const table = new FakeTable();
const tbody = { closest: () => table };
globalThis.document = {
  addEventListener() {}, querySelector: () => null, querySelectorAll: () => [],
  getElementById: id => (id === 'deviceTableBody' ? tbody : null),
};
const order = () => table.body.rows.map(r => r.cells[0].textContent);
const fill = () => { table.body.rows = [row('bravo', 40, 3), row('alpha', 200, 1), row('charlie', 90, 2)]; };

const { initDeviceSortPref, DEVICE_SORT_PREF } =
  await import(pathToFileURL(path.join(REPO, 'static/js/devices.js')));
const { reapplySort } = await import(pathToFileURL(path.join(REPO, 'static/js/table-utils.js')));

let saved = null;
const calls = [];
const fakeFetch = async (url, opts = {}) => {
  calls.push({ url, method: opts.method || 'GET' });
  if (opts.method === 'PUT') { saved = JSON.parse(opts.body).value; return { ok: true, json: async () => ({}) }; }
  return { ok: true, json: async () => ({ prefs: saved ? { [DEVICE_SORT_PREF]: saved } : {} }) };
};

console.log('\n  restoring a saved sort');
fill();
saved = { col: 'lqi', dir: 'desc' };
await initDeviceSortPref(fakeFetch);
check('the saved column and direction are applied on load', order().join() === 'alpha,charlie,bravo', order());
check('the header shows it', heads[1].classList.has('tbl-sort-desc'));

fill();
reapplySort(table);
check('a rebuilt table keeps the sort', order().join() === 'alpha,charlie,bravo', order());

console.log('\n  saving a new sort');
table.dispatchEvent(new CustomEvent('tbl-sort', { detail: { index: 0, dir: 'asc', col: 'name' } }));
await new Promise(r => setTimeout(r, 0));
check('a header click is saved to the account by column key',
  calls.some(c => c.method === 'PUT' && c.url === `/api/auth/prefs/${DEVICE_SORT_PREF}`) &&
  saved?.col === 'name' && saved?.dir === 'asc', saved);

console.log('\n  what it tolerates');
const t2 = new FakeTable();
tbody.closest = () => t2;
t2.body.rows = [row('bravo', 40, 3), row('alpha', 200, 1)];
saved = { col: 'gone', dir: 'asc' };
await initDeviceSortPref(fakeFetch);
check('a saved column that no longer exists leaves the table as it was', t2.body.rows[0].cells[0].textContent === 'bravo' && t2.dataset.sortIndex === undefined);
await initDeviceSortPref(async () => ({ ok: false }));
check('signed out (prefs refused), the table still works unsorted', t2.dataset.sortIndex === undefined);

console.log(fails.length ? `\n${fails.length} failed` : '\nall passed');
process.exit(fails.length ? 1 : 0);
