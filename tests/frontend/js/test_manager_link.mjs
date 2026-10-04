// static/js/manager-link.js as shipped: which address a ZMM Manager link uses.
// The manager is private-only, so a public hostname must never be used for it.
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
globalThis.window = {};
globalThis.document = Object.assign(new EventTarget(), { querySelectorAll: () => [] });

const { isPrivateHost, pickManagerUrl } = await import(pathToFileURL(path.join(REPO, 'static/js/manager-link.js')));

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}

console.log('\n  private vs public hostnames');
for (const h of ['192.168.1.1', '10.0.0.5', '172.20.1.2', '127.0.0.1', 'localhost', 'rocky', 'zmm.local', 'hub.lan', '[::1]', '[fd12:3456::1]'])
  check(`${h} is private`, isPrivateHost(h));
for (const h of ['zmm.example.com', 'home.mydomain.co.uk', '172.32.0.1', '8.8.8.8', '[2001:db8::1]'])
  check(`${h} is public`, !isPrivateHost(h));

console.log('\n  which address the link uses');
const info = { port: 8001, up: true, scheme: 'https', lan_url: 'https://192.168.1.1:8001/' };
check('on the LAN address: that same host',
      pickManagerUrl({ hostname: '192.168.1.1', protocol: 'https:' }, info) === 'https://192.168.1.1:8001/');
check('on a LAN name: that same name', pickManagerUrl({ hostname: 'rocky', protocol: 'https:' }, info) === 'https://rocky:8001/');
const pub = pickManagerUrl({ hostname: 'zmm.example.com', protocol: 'https:' }, info);
check('on the public domain: the hub\'s private LAN address', pub === 'https://192.168.1.1:8001/', pub);
check('…never the public domain', !String(pub).includes('example.com'), pub);
check('on the public domain with no LAN address known: nothing, rather than the public domain',
      pickManagerUrl({ hostname: 'zmm.example.com', protocol: 'https:' }, { ...info, lan_url: null }) === null);
check('before the hub has answered, a public page still gets no public link',
      pickManagerUrl({ hostname: 'zmm.example.com', protocol: 'https:' }, null) === null);
check('a non-default manager port is respected on the LAN',
      pickManagerUrl({ hostname: '192.168.1.1', protocol: 'https:' }, { ...info, port: 8011 }) === 'https://192.168.1.1:8011/');

console.log(`\n  ${fails.length ? fails.length + ' failed' : 'all passed'}`);
process.exit(fails.length ? 1 : 0);
