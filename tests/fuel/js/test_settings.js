// Runs the Region section of static/js/settings.js against a stub DOM and
// fetch. `node --check` only parses: it cannot see an undefined identifier
// inside a function body, which is how "configured is not defined" once
// reached the browser. This is the check that catches that class of bug.
const fs = require('fs');
const path = require('path');
const REPO = path.resolve(__dirname, '../../..');
const src = fs.readFileSync(path.join(REPO, 'static/js/settings.js'), 'utf8');

function slice(from, to) {
  const a = src.indexOf(from);
  const b = to ? src.indexOf(to, a) : src.length;
  if (a < 0 || b < 0) throw new Error('could not slice ' + from);
  return src.slice(a, b);
}
const code = slice('function w_escape(s) {', '\n// Tidal login') +
             slice('function renderFuelRegionSection() {', 'function renderFuelFinderSection() {');

const fails = [];
function check(n, c, e) {
  console.log((c ? '    ok   ' : '    FAIL ') + n +
              (c ? '' : '  <- ' + JSON.stringify(String(e)).slice(0, 220)));
  if (!c) fails.push(n);
}

function makeDom() {
  const els = {};
  ['fuelRegionStatus','cfg_fuel_region','cfg_fuel_subdivision',
   'cfg_fuel_region_note','fuelRegionResult'].forEach(
     id => (els[id] = { id, value:'', innerHTML:'', textContent:'', disabled:false }));
  return { els, document: { getElementById: id => els[id] || null } };
}

const REGIONS = { regions: [
  { region:'GB',     country:'GB', label:'United Kingdom',        note:'UK note'  },
  { region:'DE',     country:'DE', label:'Germany',               note:'DE note'  },
  { region:'FR',     country:'FR', label:'France',                note:'FR note'  },
  { region:'AU-NSW', country:'AU', label:'Australia — NSW & ACT', note:'NSW note' },
  { region:'AU-QLD', country:'AU', label:'Australia — Queensland',note:'QLD note' },
  { region:'AU-WA',  country:'AU', label:'Australia — WA',        note:'WA note'  },
]};

async function run(payload, ok = true) {
  const dom = makeDom();
  const posted = [];
  const win = {};
  const fetchStub = async (url, opts) => {
    if (opts && opts.method === 'POST') {
      posted.push(JSON.parse(opts.body));
      return { ok: true, json: async () => ({ active: 'DE' }) };
    }
    return { ok, status: ok ? 200 : 500, json: async () => payload };
  };
  const api = new Function('document','fetch','window','console',
    code + '\n; return { loadFuelRegion };')(dom.document, fetchStub, win, console);
  await api.loadFuelRegion();
  return { dom, posted, win };
}

(async () => {
  console.log('\n  loadFuelRegion');
  let r = await run({ ...REGIONS, active:'GB',
                      configured:{country:'',subdivision:''}, detected:'DE' });
  let status = r.dom.els.fuelRegionStatus.innerHTML;
  check('renders without throwing', !status.includes('Could not load'), status);
  check('names the active region', status.includes('United Kingdom'), status);
  check('shows the detected country by label', status.includes('Germany'), status);
  check('invites the user to pick it', status.includes('pick it above'), status);

  console.log('\n  the select');
  let opts = r.dom.els.cfg_fuel_region.innerHTML;
  check('offers a not-set option', opts.includes('— not set —'), opts);
  check('lists every region',
        ['United Kingdom','Germany','France','Australia'].every(l => opts.includes(l)), opts);
  check('option values are full region keys',
        opts.includes('value="AU-NSW"') && opts.includes('value="AU-QLD"') &&
        opts.includes('value="AU-WA"'), opts);
  check('nothing preselected when unset', !opts.includes('selected'), opts);

  console.log('\n  already configured');
  r = await run({ ...REGIONS, active:'DE',
                  configured:{country:'DE',subdivision:''}, detected:'DE' });
  opts = r.dom.els.cfg_fuel_region.innerHTML;
  check('the configured country is preselected', /value="DE" selected/.test(opts), opts);
  check('no suggestion when it already matches',
        !r.dom.els.fuelRegionStatus.innerHTML.includes('look like'),
        r.dom.els.fuelRegionStatus.innerHTML);
  check('the note is the active region note',
        r.dom.els.cfg_fuel_region_note.textContent === 'DE note',
        r.dom.els.cfg_fuel_region_note.textContent);

  console.log('\n  a subdivided country');
  r = await run({ ...REGIONS, active:'AU-QLD',
                  configured:{country:'AU',subdivision:'QLD'}, detected:'AU' });
  opts = r.dom.els.cfg_fuel_region.innerHTML;
  check('AU-QLD preselected, not its neighbours',
        /value="AU-QLD" selected/.test(opts) &&
        !/value="AU-NSW" selected/.test(opts) &&
        !/value="AU-WA" selected/.test(opts), opts);
  check('the subdivision box is filled',
        r.dom.els.cfg_fuel_subdivision.value === 'QLD', r.dom.els.cfg_fuel_subdivision.value);
  check('no suggestion when the country already matches',
        !r.dom.els.fuelRegionStatus.innerHTML.includes('look like'),
        r.dom.els.fuelRegionStatus.innerHTML);

  console.log('\n  saveFuelRegion splits the key');
  async function save(key, typedSub) {
    const s = await run({ ...REGIONS, active:'GB', configured:{country:'',subdivision:''} });
    s.dom.els.cfg_fuel_region.value = key;
    s.dom.els.cfg_fuel_subdivision.value = typedSub || '';
    await s.win.saveFuelRegion();
    return s.posted[0];
  }
  check('AU-NSW posts country AU, subdivision NSW',
        JSON.stringify(await save('AU-NSW')) === '{"country":"AU","subdivision":"NSW"}',
        await save('AU-NSW'));
  check('AU-WA posts country AU, subdivision WA',
        JSON.stringify(await save('AU-WA')) === '{"country":"AU","subdivision":"WA"}',
        await save('AU-WA'));
  check('a plain key keeps the typed subdivision',
        JSON.stringify(await save('DE', 'XX')) === '{"country":"DE","subdivision":"XX"}',
        await save('DE', 'XX'));
  check('clearing posts an empty country',
        JSON.stringify(await save('')) === '{"country":"","subdivision":""}',
        await save(''));

  console.log('\n  failure paths');
  r = await run({}, false);
  check('an HTTP error shows a message and does not throw',
        r.dom.els.fuelRegionStatus.innerHTML.includes('Could not load'),
        r.dom.els.fuelRegionStatus.innerHTML);
  r = await run({ regions: [], active: 'GB', configured: {} });
  check('an empty registry still renders',
        !r.dom.els.fuelRegionStatus.innerHTML.includes('Could not load'),
        r.dom.els.fuelRegionStatus.innerHTML);

  process.exit(fails.length ? 1 : 0);
})();
