// Recalibrating a background image, sliced out of the shipped floor-plan.js.
//
// The claim: a plan traced over a background image still sits on that image
// after the image's scale is corrected, and only what was *drawn* moves —
// typed measurements (a radiator's length, a window's height) are the user's
// own figures and must survive untouched.
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

const fns = ['function round3(v) {', 'function scaleLevelGeometry(lvl, s, about, map) {',
             'function levelHasDrawing(lvl) {']
  .map(mark => slice(mark, '\n}')).join('\n');
const m = { exports: {} };
new Function('module', fns + '\nmodule.exports = { round3, scaleLevelGeometry, levelHasDrawing };')(m);
const { scaleLevelGeometry, levelHasDrawing } = m.exports;

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}
function section(t) { console.log('\n  ' + t); }

const level = () => ({
  walls: [{ id: 'w', x1: 0, y1: 0, x2: 4, y2: 0 }],
  rooms: [{ id: 'r', polygon: [[0, 0], [4, 0], [4, 3], [0, 3]] }],
  openings: [{ id: 'o', wall_id: 'w', offset_m: 1, width_m: 1, height_m: 1.2 }],
  radiators: [{ id: 'rad', x: 2, y: 1, offset_m: 0.5, length_m: 0.6, watts_at_dt50: 1000 }],
  sensors: [{ id: 's', x: 3, y: 2, height_m: 1.5 }],
  devices: [{ ieee: '0xa', x: 1, y: 2 }],
});

section('the drawing scales about the point the image is anchored on');
{
  const lvl = level();
  scaleLevelGeometry(lvl, 2, { x: 0, y: 0 }, null);
  check('walls', lvl.walls[0].x2 === 8 && lvl.walls[0].y2 === 0, lvl.walls[0]);
  check('room polygons', JSON.stringify(lvl.rooms[0].polygon) === JSON.stringify([[0, 0], [8, 0], [8, 6], [0, 6]]), lvl.rooms[0]);
  check('a window slides and widens with its wall',
        lvl.openings[0].offset_m === 2 && lvl.openings[0].width_m === 2, lvl.openings[0]);
  check('radiators, sensors and placed devices move',
        lvl.radiators[0].x === 4 && lvl.sensors[0].x === 6 && lvl.devices[0].y === 4,
        [lvl.radiators[0], lvl.sensors[0], lvl.devices[0]]);
  check('a radiator slides along its wall', lvl.radiators[0].offset_m === 1);
}

section('typed measurements are not the tracing');
{
  const lvl = level();
  scaleLevelGeometry(lvl, 3, { x: 0, y: 0 }, null);
  check("a radiator's length and output stay",
        lvl.radiators[0].length_m === 0.6 && lvl.radiators[0].watts_at_dt50 === 1000);
  check("a window's height stays", lvl.openings[0].height_m === 1.2);
  check("a sensor's mounting height stays", lvl.sensors[0].height_m === 1.5);
}

section('the anchor stays put, and shrinking works');
{
  const lvl = level();
  scaleLevelGeometry(lvl, 0.5, { x: 4, y: 0 }, null);
  check('the point calibration anchored on does not move',
        lvl.walls[0].x2 === 4 && lvl.walls[0].y2 === 0, lvl.walls[0]);
  check('everything else halves its distance from it', lvl.walls[0].x1 === 2, lvl.walls[0]);
}

section('the map pin');
{
  const lvl = level();
  const map = { anchor_x_m: 2, anchor_y_m: 2, opacity: 0.6 };
  scaleLevelGeometry(lvl, 2, { x: 0, y: 0 }, map);
  check('follows when it is passed', map.anchor_x_m === 4 && map.anchor_y_m === 4, map);
  const other = { anchor_x_m: 2, anchor_y_m: 2 };
  scaleLevelGeometry(level(), 2, { x: 0, y: 0 }, null);
  check('is left alone on a multi-level plan', other.anchor_x_m === 2);
}

section('a traced wall still sits on the image it was traced over');
{
  // Redo what promptCalibrationDistance does to the image, then to the drawing.
  const bg = { image_width_px: 800, image_height_px: 600, pixels_per_metre: 100,
               origin_x_m: 0, origin_y_m: 0 };
  const lvl = level();
  // A wall traced a quarter of the way across the image, 1 m above its foot.
  lvl.walls = [{ id: 'w', x1: 2, y1: 1, x2: 2, y2: 4 }];
  const p1 = { x: 1, y: 1 };                 // the first calibration click
  const drawn = 2, real = 5, factor = real / drawn;
  const before = (lvl.walls[0].x1 - bg.origin_x_m) / (bg.image_width_px / bg.pixels_per_metre);

  const oldPpm = bg.pixels_per_metre, newPpm = oldPpm * (drawn / real);
  const wOldM = bg.image_width_px / oldPpm, hOldM = bg.image_height_px / oldPpm;
  const u = (p1.x - bg.origin_x_m) / wOldM, v = (bg.origin_y_m + hOldM - p1.y) / hOldM;
  bg.pixels_per_metre = newPpm;
  const wNewM = bg.image_width_px / newPpm, hNewM = bg.image_height_px / newPpm;
  bg.origin_x_m = p1.x - u * wNewM;
  bg.origin_y_m = p1.y - (hNewM - v * hNewM);
  scaleLevelGeometry(lvl, factor, p1, null);

  const after = (lvl.walls[0].x1 - bg.origin_x_m) / (bg.image_width_px / bg.pixels_per_metre);
  check('it is at the same place on the image as before', Math.abs(after - before) < 1e-9,
        [before, after]);
  check('and its length is now the real one', lvl.walls[0].y2 - lvl.walls[0].y1 === 3 * factor,
        lvl.walls[0]);
}

section('an empty level has nothing to scale');
{
  check('so it is not asked about',
        !levelHasDrawing({ walls: [], rooms: [], radiators: [], sensors: [], devices: [] })
        && levelHasDrawing(level()));
}

console.log('\n' + (fails.length ? `  ${fails.length} FAILED` : '  all passed'));
process.exit(fails.length ? 1 : 0);
