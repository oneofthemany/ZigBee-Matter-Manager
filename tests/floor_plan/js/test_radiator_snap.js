// Radiator wall-snapping, sliced out of the shipped floor-plan.js.
//
// The claim: a radiator mounts on a wall of its own room, centred where it was
// dropped — not on the nearest wall on the level, and not half its length off.
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

const names = ['projectPointOntoSegment', 'nearestWallWithProjection', 'wallBordersRoom', 'radiatorWallSnap'];
const m = { exports: {} };
new Function('module', names.map(n => slice(`function ${n}(`, '\n}')).join('\n')
             + `\nmodule.exports = { ${names.join(', ')} };`)(m);
const { wallBordersRoom, radiatorWallSnap } = m.exports;

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}
function section(t) { console.log('\n  ' + t); }

// Lounge (0..5) and hall (5..8). The hall has a cupboard wall at x = 5.3 that
// is nearer the lounge's east side than some of the lounge's own walls.
const lvl = {
  walls: [{ id: 's', x1: 0, y1: 0, x2: 8, y2: 0 }, { id: 'n', x1: 8, y1: 4, x2: 0, y2: 4 },
          { id: 'w', x1: 0, y1: 4, x2: 0, y2: 0 }, { id: 'e', x1: 8, y1: 0, x2: 8, y2: 4 },
          { id: 'i', x1: 5, y1: 0, x2: 5, y2: 4 }, { id: 'cup', x1: 5.3, y1: 1, x2: 5.3, y2: 3 }],
  rooms: [{ id: 'lounge', polygon: [[0, 0], [5, 0], [5, 4], [0, 4]] },
          { id: 'hall', polygon: [[5, 0], [8, 0], [8, 4], [5, 4]] }],
};
const wall = id => lvl.walls.find(w => w.id === id);
const lounge = lvl.rooms[0];

section('which walls are a room\'s');
check('the shared internal wall borders the lounge', wallBordersRoom(wall('i'), lounge));
check('a long outside wall borders it where they overlap', wallBordersRoom(wall('s'), lounge));
check('the hall\'s far wall does not', !wallBordersRoom(wall('e'), lounge));
check('nor a wall standing inside the hall', !wallBordersRoom(wall('cup'), lounge));

section('snapping');
const rad = { room_id: 'lounge', length_m: 1.0 };
let snap = radiatorWallSnap(lvl, rad, { x: 5.2, y: 2 }, 0.5);
check('a drop nearer another room\'s wall still mounts on its own room\'s',
      snap && snap.wall.id === 'i', snap && snap.wall.id);
check('centred on the drop point', snap && Math.abs(snap.offset_m - 1.5) < 1e-6, snap && snap.offset_m);
snap = radiatorWallSnap(lvl, rad, { x: 4.9, y: 3.9 }, 0.5);
check('and kept on the wall near a corner',
      snap && snap.offset_m >= 0 && snap.offset_m + rad.length_m <= 8 + 1e-6, snap);
check('nothing within reach leaves it freestanding',
      radiatorWallSnap(lvl, rad, { x: 2.5, y: 2 }, 0.5) === null);
snap = radiatorWallSnap(lvl, { length_m: 0.6 }, { x: 5.25, y: 2 }, 0.5);
check('a radiator with no room takes the nearest wall', snap && snap.wall.id === 'cup', snap && snap.wall.id);

console.log(fails.length ? `\n  ${fails.length} failed` : '\n  all passed');
process.exit(fails.length ? 1 : 0);
