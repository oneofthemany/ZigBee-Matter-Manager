// Runs the Drive tab's fuel render path for real. test_formatters.js covers
// the formatters in isolation; this covers the functions that call them,
// which is where an undefined identifier would actually hide.
const fs = require('fs');
const path = require('path');
const REPO = path.resolve(__dirname, '../../..');
let src = fs.readFileSync(path.join(REPO, 'static/js/drive.js'), 'utf8');

src = src.replace(/^import .*$/m, '');
const open = src.indexOf('(function () {');
const close = src.lastIndexOf('})();');
const body = src.slice(open + '(function () {'.length, close);

const fails = [];
function check(n, c, e) {
  console.log((c ? '    ok   ' : '    FAIL ') + n +
              (c ? '' : '  <- ' + JSON.stringify(String(e)).slice(0, 220)));
  if (!c) fails.push(n);
}

const store = {};
const stubs = {
  window: { zmmLog: null, addEventListener() {} },
  document: {
    documentElement: { getAttribute: () => 'light' },
    getElementById: () => null,
    addEventListener() {},
  },
  localStorage: {
    getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = v; },
  },
  createChart: () => ({ dispose() {} }),
  fetch: async () => ({ ok: false, status: 500, json: async () => ({}) }),
  console,
};

const api = new Function(...Object.keys(stubs), body + `
  ; return { fuelCard, renderFuelResults, getFuelPrefs,
             setUnits: u => { fuelUnits = u; },
             setTypes: t => { fuelTypes = t; },
             setAttribution: a => { fuelAttribution = a; },
             setDefaultGrade: g => { fuelDefaultGrade = g; } };`
)(...Object.values(stubs));

const AU = { currency:'AUD', symbol:'A$', volume:'L', distance:'km',
             display_scale:'minor', decimals:3 };
const DATA = {
  count: 2, fuel: 'U91', fuel_label: 'Unleaded 91', centre: { source: 'place' },
  units: AU, attribution: 'FuelCheck — NSW Government, CC BY 4.0',
  stations: [
    { site_id:'a', brand:'BP', address:'1 Rd', postcode:'2000', distance_km:1.3,
      price:2.039, prices:{U91:2.039}, maps_url:'https://x' },
    { site_id:'b', brand:'Shell', address:'2 Rd', postcode:'2010', distance_km:2.1,
      price:2.089, prices:{U91:2.089}, maps_url:'https://y' },
  ],
};

console.log('\n  fuelCard');
let html;
try { html = api.fuelCard(); check('does not throw', true); }
catch (e) { check('does not throw', false, e); }
if (html) {
  check('has the place field', html.includes('fuel-postcode'));
  check('labelled Place, not Postcode', html.includes('>Place<'));
  check('the radius label carries a unit',
        /Within <span id="fuel-radius-label">8 km<\/span>/.test(html));
  check('a default attribution before one is loaded',
        html.includes('published open data'));
}

console.log('\n  attribution comes from the region');
api.setAttribution('FuelCheck — NSW Government, CC BY 4.0');
html = api.fuelCard();
check('shows the provider attribution', html.includes('FuelCheck'));
check('no longer claims the UK scheme', !html.includes('UK retailer'));

console.log('\n  the grade select follows the region');
api.setTypes({ U91:'Unleaded 91', E10:'Unleaded E10', DL:'Diesel' });
api.setDefaultGrade('U91');
html = api.fuelCard();
check('lists the region grades', html.includes('Unleaded 91') && html.includes('Diesel'));
check('does not list UK grades', !html.includes('Super diesel (SDV)'));

console.log('\n  renderFuelResults in Australian cents');
const out = { innerHTML: '' };
try { api.renderFuelResults(out, DATA); check('does not throw', true); }
catch (e) { check('does not throw', false, e); }
check('prices shown in cents', out.innerHTML.includes('203.9c'), out.innerHTML.slice(0, 400));
check('not shown as dollars', !out.innerHTML.includes('A$2.039'));
check('not shown as pence', !out.innerHTML.includes('203.9p'));
check('header names cents per litre', out.innerHTML.includes('Price/L (c)'));
check('distance in km', out.innerHTML.includes('1.3 km'));
check('a delta for the dearer station', out.innerHTML.includes('+5.0c'), out.innerHTML.slice(-700));
check('the cheapest is flagged', out.innerHTML.includes('cheapest'));
check('wording for a place centre', out.innerHTML.includes('around that location'));

console.log('\n  degenerate results');
const empty = { innerHTML: '' };
api.renderFuelResults(empty, { count:0, fuel:'U91', fuel_label:'Unleaded 91',
                               stations: [], units: AU, centre: {} });
check('no stations renders a message, not a crash', empty.innerHTML.length > 0);
const noUnits = { innerHTML: '' };
api.renderFuelResults(noUnits, { stations: DATA.stations, count:2, fuel:'U91',
                                 fuel_label:'x', centre: {} });
check('a missing units block falls back to the cached one', noUnits.innerHTML.length > 0);

console.log('\n  grade preferences survive a region change');
store['zbm-drive-fuel-prefs'] = JSON.stringify({ fuel:'B7', radius:25 });
const prefs = api.getFuelPrefs();
check('a grade from another region falls back to the default',
      prefs.fuel === 'U91', prefs);
check('other preferences survive', prefs.radius === 25, prefs);

process.exit(fails.length ? 1 : 0);
