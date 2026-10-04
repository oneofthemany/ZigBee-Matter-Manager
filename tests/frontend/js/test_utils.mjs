// static/js/utils.js imported as shipped, with a stand-in document whose
// visibility the test controls. Run: node tests/frontend/js/test_utils.mjs
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

class FakeDocument extends EventTarget {
  hidden = false;
  setHidden(h) { this.hidden = h; this.dispatchEvent(new Event('visibilitychange')); }
}
globalThis.document = new FakeDocument();
globalThis.window = {};
globalThis.zmmLog = () => ({ log() {}, warn() {}, error() {} });

const { whileVisible, escapeHtml, jsArg } = await import(pathToFileURL(path.join(REPO, 'static/js/utils.js')));

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}

console.log('\n  whileVisible');
let calls = 0;
const tick = whileVisible(() => { calls++; });
tick(); tick();
check('runs normally while the page is visible', calls === 2, calls);
document.setHidden(true);
tick(); tick(); tick();
check('skips every tick while hidden', calls === 2, calls);
document.setHidden(false);
check('catches up exactly once when shown again', calls === 3, calls);
document.setHidden(true); document.setHidden(false);
check('no catch-up if nothing was skipped while hidden', calls === 3, calls);

let a = 0, b = 0;
const ta = whileVisible(() => { a++; }), tb = whileVisible(() => { throw new Error('boom'); });
const tc = whileVisible(() => { b++; });
document.setHidden(true); ta(); tb(); tc();
document.setHidden(false);
check('one poller failing on catch-up doesn\'t stop the others', a === 1 && b === 1, { a, b });

let got = null;
const withArgs = whileVisible((x, y) => { got = [x, y]; return 'r'; });
check('passes arguments and returns the result when visible', withArgs(1, 2) === 'r' && got[1] === 2, got);

console.log('\n  escapeHtml / jsArg');
const NASTY = 'Kid\'s "Room" & <b>';
check('escapeHtml leaves no markup or quotes', escapeHtml(NASTY) === 'Kid&#39;s &quot;Room&quot; &amp; &lt;b&gt;', escapeHtml(NASTY));
check('escapeHtml treats null/undefined as empty', escapeHtml(null) === '' && escapeHtml(undefined) === '');
// What the browser does with onclick="f(${jsArg(x)})": decode the attribute, then run it as JS.
const decoded = jsArg(NASTY).replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<')
                            .replace(/&gt;/g, '>').replace(/&amp;/g, '&');
check('jsArg survives attribute decoding as one exact JS string', new Function('return ' + decoded)() === NASTY, decoded);
check('jsArg output has no raw quotes to end the attribute', !/["']/.test(jsArg(NASTY)), jsArg(NASTY));
const backslash = 'C:\\path\\x';
check('jsArg keeps backslashes', new Function('return ' + jsArg(backslash).replace(/&quot;/g, '"'))() === backslash);

console.log(`\n  ${fails.length ? fails.length + ' failed' : 'all passed'}`);
process.exit(fails.length ? 1 : 0);
