// The map's coordinate maths, sliced out of the shipped floor-plan.js.
//
// The plan is drawn in metres and the map in Web Mercator tiles, turned by
// the compass. Getting a sign wrong here puts the Home pin — and anything
// else placed by coordinates — in the wrong place on the plan, which is not
// obvious by eye on an unfamiliar street.
const fs = require('fs');
const path = require('path');
const REPO = path.resolve(__dirname, '../../..');
const src = fs.readFileSync(path.join(REPO, 'static/js/floor-plan.js'), 'utf8');

function slice(startMark, endMark) {
  const start = src.indexOf(startMark);
  if (start < 0) { console.error('could not find: ' + startMark); process.exit(2); }
  const end = src.indexOf(endMark, start);
  if (end < 0) { console.error('could not find end: ' + endMark); process.exit(2); }
  return src.slice(start, end + endMark.length);
}

const consts = slice('const MAP_MAX_TILE_ZOOM =', ';') + '\n'
             + slice('const EARTH_M =', ';');
// modelToSvg is a one-liner; the rest are blocks.
const fns = [slice('function modelToSvg(p)', '; }')].concat(
  ['function mapGeo() {', 'function unturnAboutAnchor(p, anchor) {', 'function mapAnchorSvg() {',
   'function mapFrame(z) {', 'function modelToLatLon(p) {', 'function latLonToModel(ll) {',
   'function geoOffsetM(a, b) {'].map(mark => slice(mark, '\n}'))).join('\n');

const m = { exports: {} };
new Function('module', 'let _state = null, _home = null;\n' + consts + '\n' + fns + `
  module.exports = {
    set: (state, home) => { _state = state; _home = home; },
    modelToLatLon, latLonToModel, geoOffsetM, mapFrame,
  };`)(m);
const G = m.exports;

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}
function section(t) { console.log('\n  ' + t); }

const HOME = { lat: 51.5074, lon: -0.1278 };
const state = (north, anchor = { x: 0, y: 0 }) => ({
  zoom: 80, plan: { north_offset_deg: north, map: { anchor_x_m: anchor.x, anchor_y_m: anchor.y } },
});

section('the point the map is lined up by');
{
  G.set(state(0), HOME);
  const at = G.latLonToModel(HOME);
  check('sits exactly on the map anchor', Math.abs(at.x) < 1e-9 && Math.abs(at.y) < 1e-9, at);
  const back = G.modelToLatLon({ x: 0, y: 0 });
  check('and reads back as itself',
        Math.abs(back.lat - HOME.lat) < 1e-9 && Math.abs(back.lon - HOME.lon) < 1e-9, back);
}

section('metres on the plan are metres on the ground');
{
  G.set(state(0), HOME);
  // 100 m east and 100 m north of the anchor, with the plan facing north.
  const east = G.modelToLatLon({ x: 100, y: 0 });
  const north = G.modelToLatLon({ x: 0, y: 100 });
  const [dxE, dyE] = G.geoOffsetM(HOME, east);
  const [dxN, dyN] = G.geoOffsetM(HOME, north);
  check('+x is 100 m east', Math.abs(dxE - 100) < 0.5 && Math.abs(dyE) < 0.5, [dxE, dyE]);
  check('+y is 100 m north', Math.abs(dyN - 100) < 0.5 && Math.abs(dxN) < 0.5, [dxN, dyN]);
}

section('the compass turns the map, not the plan');
{
  // North 90° clockwise from plan-up means plan +x points north.
  G.set(state(90), HOME);
  const [dx, dy] = G.geoOffsetM(HOME, G.modelToLatLon({ x: 100, y: 0 }));
  check('with north at 90°, +x is north', Math.abs(dy - 100) < 0.5 && Math.abs(dx) < 0.5, [dx, dy]);
  G.set(state(180), HOME);
  const [dx2, dy2] = G.geoOffsetM(HOME, G.modelToLatLon({ x: 0, y: 100 }));
  check('with north at 180°, +y is south', Math.abs(dy2 + 100) < 0.5 && Math.abs(dx2) < 0.5, [dx2, dy2]);
}

section('round trips, wherever the anchor is and however it is turned');
{
  for (const north of [0, 37, 90, 200, 359]) {
    for (const anchor of [{ x: 0, y: 0 }, { x: -12.5, y: 7.25 }]) {
      G.set(state(north, anchor), HOME);
      for (const p of [{ x: 3, y: 4 }, { x: -40, y: 25 }, { x: 120, y: -80 }]) {
        const back = G.latLonToModel(G.modelToLatLon(p));
        const off = Math.hypot(back.x - p.x, back.y - p.y);
        if (off > 0.01) { check(`round trip north=${north} anchor=${anchor.x} p=${p.x},${p.y}`, false, [p, back]); }
      }
    }
  }
  check('every round trip lands back within a centimetre', true);
}

section("the map's own point wins over the home once it is pinned");
{
  const pinned = state(0);
  // Lined up a few hundred metres off — what a stale alignment looks like.
  pinned.plan.map.lat = 51.5100; pinned.plan.map.lon = -0.1300;
  G.set(pinned, HOME);
  const at = G.latLonToModel(pinned.plan.map);
  check('the pinned point is what sits on the anchor',
        Math.abs(at.x) < 1e-9 && Math.abs(at.y) < 1e-9, at);
  const home = G.latLonToModel(HOME);
  const away = Math.hypot(home.x, home.y);
  check('so the Home pin is drawn off it, by the real distance',
        Math.abs(away - Math.hypot(...G.geoOffsetM(pinned.plan.map, HOME))) < 1, away);
  check('which is the few hundred metres it was out by', away > 250 && away < 400, away);
}

console.log('\n' + (fails.length ? `  ${fails.length} FAILED` : '  all passed'));
process.exit(fails.length ? 1 : 0);
