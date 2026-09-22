// Daylight across a room, sliced out of the shipped floor-plan.js.
//
// The claims (docs/daylight.md §8): light falls away from the window, the sun
// makes a patch where the geometry says it lands, walls cast shadows, the sky
// model integrates to the diffuse light it was given, and Rayleigh scattering
// reddens the low sun and blues the sky.
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

const names = ['polygonCentroid', 'pointInPolygon', 'openingsOnRoomBoundary', 'rayleighDepth', 'airMass',
               'daylightTints', 'skyLuminanceClear', 'skyLuminanceOvercast', 'daylightSky',
               'daylightBlocked', 'roomDaylightGeometry', 'roomDaylightField'];
const consts = slice('const DL_WORK_PLANE_M', 'let _daylightCache = new Map();');
const fns = names.map(n => slice(`function ${n}(`, '\n}')).join('\n');
const m = { exports: {} };
new Function('module', consts + '\n' + fns + `\nmodule.exports = { ${names.join(', ')} };`)(m);
const { daylightTints, daylightSky, skyLuminanceClear, roomDaylightGeometry, roomDaylightField } = m.exports;

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}
function section(t) { console.log('\n  ' + t); }

// A 6 × 4 room, +y north; a 1.5 m window centred on the south wall.
const box = () => ({
  walls: [{ id: 's', x1: 0, y1: 0, x2: 6, y2: 0, type: 'external' },
          { id: 'e', x1: 6, y1: 0, x2: 6, y2: 4, type: 'external' },
          { id: 'n', x1: 6, y1: 4, x2: 0, y2: 4, type: 'external' },
          { id: 'w', x1: 0, y1: 4, x2: 0, y2: 0, type: 'external' }],
  openings: [{ id: 'win', kind: 'window', wall_id: 's', offset_m: 2.25, width_m: 1.5,
               height_m: 1.2, glazing: 'double' }],
  rooms: [{ id: 'r', polygon: [[0, 0], [6, 0], [6, 4], [0, 4]] }],
});
function at(f, x, y) {
  const i = Math.floor((x - f.x0) / f.h), j = Math.floor((y - f.y0) / f.h);
  const k = j * f.nx + i;
  return f.inside[k] ? Math.pow(10, f.data[k]) : NaN;
}
const overcast = { azimuth: 180, elevation: 30, diffuse: 10000, beam_n: 0, cloud: 1 };
const sunny = { azimuth: 180, elevation: 30, diffuse: 8000, beam_n: 60000, cloud: 0 };

section('the sky model');
for (const sky of [overcast, sunny, { ...sunny, elevation: 8 }]) {
  const S = daylightSky(sky, 0);
  let e = 0; const NA = 60, NP = 120;
  for (let a = 0; a < NA; a++) {
    const alt = (a + 0.5) * Math.PI / 2 / NA;
    for (let p = 0; p < NP; p++)
      e += S.luminance(alt, (p + 0.5) * 2 * Math.PI / NP) * Math.sin(alt) * Math.cos(alt) * (Math.PI / 2 / NA) * (2 * Math.PI / NP);
  }
  check(`whole sky gives the diffuse it was given (el ${sky.elevation}°, cloud ${sky.cloud})`,
        Math.abs(e - sky.diffuse) / sky.diffuse < 0.02, e);
}
const near = skyLuminanceClear(0.5, Math.cos(0.1)), side = skyLuminanceClear(0.5, 0), back = skyLuminanceClear(0.5, -1);
check('the clear sky glows round the sun', near > 3 * side, [near, side]);
check('and Rayleigh brightens it again opposite the sun (cos²γ)', back > side, [back, side]);

section('Rayleigh colour');
const high = daylightTints(60, 0), low = daylightTints(5, 0);
check('the beam is warm', high.sun[0] >= high.sun[2], high.sun);
check('and redder as the sun drops', low.sun[2] < high.sun[2] - 0.2, [low.sun, high.sun]);
check('scattered skylight is blue', high.sky[2] > high.sky[0], high.sky);
check('cloud greys both', daylightTints(5, 1).sun.every(c => c > 0.99), daylightTints(5, 1).sun);

section('light falls away from the window');
const lvl = box();
const geo = roomDaylightGeometry(lvl.rooms[0], lvl);
check('the window counts', geo.windows.length === 1, geo.windows);
let f = roomDaylightField(geo, overcast, 200, 0);
const nearW = at(f, 3, 0.6), mid = at(f, 3, 2), far = at(f, 3, 3.8);
check('near the glass is brightest', nearW > mid && mid > far, [nearW, mid, far]);
check('by a lot', nearW > 4 * far, [nearW, far]);
check('never below the reflected light', far >= 200 * 0.75 - 1, far);
check('off to the side is darker than straight in', at(f, 0.3, 1.5) < at(f, 3, 1.5));

section('the sun lands where the geometry says');
f = roomDaylightField(geo, sunny, 1500, 0);
// Working plane 0.85, sill 0.9, head 2.1, sun at 30°: the patch runs from
// (0.9−0.85)/tan30 ≈ 0.09 m to (2.1−0.85)/tan30 ≈ 2.17 m into the room.
const inPatch = at(f, 3, 1.2), beyond = at(f, 3, 3), beside = at(f, 1, 1.2);
check('in the patch is sunlit', f.sun[Math.floor((1.2 - f.y0) / f.h) * f.nx + Math.floor((3 - f.x0) / f.h)] > 0);
check('bright in the patch', inPatch > 10000, inPatch);
check('beyond the patch, only sky', beyond < inPatch / 5, [beyond, inPatch]);
check('beside the patch, only sky', beside < inPatch / 5, [beside, inPatch]);
const west = roomDaylightField(geo, { ...sunny, azimuth: 225 }, 1500, 0);
check('an afternoon sun shifts the patch east', at(west, 4.2, 1.2) > at(west, 1.8, 1.2) * 5,
      [at(west, 4.2, 1.2), at(west, 1.8, 1.2)]);
const north = roomDaylightField(geo, { ...sunny, azimuth: 0 }, 1500, 0);
check('a sun behind the house makes no patch', north.sun.every(v => v === 0));

section('walls cast shadows');
// A stub wall into the room beside the window: behind it is in its shadow.
const shaded = box();
shaded.walls.push({ id: 'stub', x1: 1.8, y1: 0, x2: 1.8, y2: 2.5, type: 'internal' });
const g2 = roomDaylightGeometry(shaded.rooms[0], shaded);
const open = roomDaylightField(geo, overcast, 200, 0), shadow = roomDaylightField(g2, overcast, 200, 0);
const cell = (f, x, y) => Math.floor((y - f.y0) / f.h) * f.nx + Math.floor((x - f.x0) / f.h);
check('behind the stub no sky reaches', shadow.sky[cell(shadow, 1, 1)] === 0 && open.sky[cell(open, 1, 1)] > 20,
      [shadow.sky[cell(shadow, 1, 1)], open.sky[cell(open, 1, 1)]]);
check('so only reflected light is left there', Math.abs(at(shadow, 1, 1) - shadow.irc) < 1, at(shadow, 1, 1));
check('in front of it is unchanged', Math.abs(at(shadow, 3, 1) - at(open, 3, 1)) < 1);

section('only glass onto the outdoors');
const inner = box();
inner.rooms.push({ id: 'porch', polygon: [[0, -3], [6, -3], [6, 0], [0, 0]] });
check('a window into another room lets no daylight in',
      roomDaylightGeometry(inner.rooms[0], inner).windows.length === 0);

console.log(`\n${fails.length ? fails.length + ' failed' : 'all passed'}`);
process.exit(fails.length ? 1 : 0);
