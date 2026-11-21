// static/js/modal/device-inputs.js as shipped: a WiiM's inputs are read once
// per player at a time, and a rule's saved input survives whatever the box
// reports now.
// Run: node tests/frontend/js/test_device_inputs.mjs
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const { readInputs, inputSelectHtml, PENDING } =
  await import(pathToFileURL(path.join(REPO, 'static/js/modal/device-inputs.js')).href);

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
const selected = html => (html.match(/<option value="([^"]*)" selected/) || [])[1];
const values = html => [...html.matchAll(/<option value="([^"]*)"/g)].map(m => m[1]);

function speaker(body, { ok = true, status = 200 } = {}) {
  const calls = [];
  let answer;
  const gate = new Promise(r => { answer = r; });
  const fetchFn = async url => { calls.push(url); await gate; return { ok, status, json: async () => body }; };
  return { calls, fetchFn, answer };
}

console.log('\n  one read at a time');
{
  const cache = {};
  const s = speaker({ panel: { input: { options: [{ id: 'wifi', label: 'Wi-Fi' }, { id: 'optical', label: 'Optical' }] } } });
  const first = readInputs(cache, 'wiim:10.0.0.5', s.fetchFn);
  check('a read is marked in flight at once', cache['wiim:10.0.0.5'] === PENDING);
  const again = readInputs(cache, 'wiim:10.0.0.5', s.fetchFn);
  const third = readInputs(cache, 'wiim:10.0.0.5', s.fetchFn);
  check('re-renders while it is in flight start no other read', again === null && third === null);
  check('the picker says it is loading meanwhile', /Loading…/.test(inputSelectHtml(cache['wiim:10.0.0.5'], '', 1, esc)));
  s.answer();
  await first;
  check('only one request went to the speaker', s.calls.length === 1, s.calls);
  check('and it names the player', s.calls[0] === '/api/media/device?player_id=wiim%3A10.0.0.5', s.calls[0]);
  check('the inputs are cached', Array.isArray(cache['wiim:10.0.0.5']) && cache['wiim:10.0.0.5'].length === 2);
  check('a cached player is not read again', readInputs(cache, 'wiim:10.0.0.5', s.fetchFn) === null && s.calls.length === 1);

  const other = speaker({ panel: { input: { options: [{ id: 'hdmi', label: 'HDMI' }] } } });
  const p = readInputs(cache, 'wiim:10.0.0.6', other.fetchFn);
  check('another player gets its own read', p !== null);
  other.answer(); await p;
  check('no player id, no read', readInputs(cache, '', other.fetchFn) === null && other.calls.length === 1);
}

console.log('\n  failures');
{
  const cache = {};
  const s = speaker({ error: 'speaker offline' }, { ok: false, status: 502 });
  const p = readInputs(cache, 'wiim:1', s.fetchFn); s.answer(); await p;
  check('the server reason is kept', cache['wiim:1'].error === 'speaker offline', cache['wiim:1']);
  const html = inputSelectHtml(cache['wiim:1'], 'optical', 7, esc);
  check('the saved input stays selected after a failed read', selected(html) === 'optical', html);
  check('with a retry for that step', /window\._aDevRetry\(7\)/.test(html));

  const silent = speaker({ supported: false });
  const q = readInputs(cache, 'wiim:2', silent.fetchFn); silent.answer(); await q;
  check('a speaker that did not answer says so', cache['wiim:2'].error === 'the speaker did not answer');

  const r = readInputs(cache, 'wiim:3', async () => { throw new Error('network down'); }); await r;
  check('a network error is kept', cache['wiim:3'].error === 'network down');
  check('the error text is escaped', !/<b>/.test(inputSelectHtml({ error: '<b>x</b>' }, '', 1, esc)));
}

console.log('\n  the saved input');
{
  const live = [{ id: 'wifi', label: 'Wi-Fi' }, { id: 'optical', label: 'Optical' }];
  let html = inputSelectHtml(live, 'optical', 3, esc);
  check('a listed saved input is selected', selected(html) === 'optical', html);
  check('and not listed twice', values(html).filter(v => v === 'optical').length === 1, values(html));

  html = inputSelectHtml(live, 'line-in', 3, esc);
  check('a saved input the box no longer lists stays selected', selected(html) === 'line-in', html);
  check('alongside the live inputs', values(html).join() === 'line-in,wifi,optical', values(html));

  html = inputSelectHtml(live, '', 3, esc);
  check('a new step offers only the live inputs', values(html).join() === 'wifi,optical' && selected(html) === undefined, html);
  check('the saved value is escaped', !/"><script/.test(inputSelectHtml(live, '"><script>', 3, esc)));
}

console.log(fails.length ? `\n  ${fails.length} failed` : '\n  all passed');
process.exit(fails.length ? 1 : 0);
