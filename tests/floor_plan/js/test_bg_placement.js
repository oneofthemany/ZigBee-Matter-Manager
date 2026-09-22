// Placing a background image, sliced out of the shipped floor-plan.js.
//
// The claim: every way of moving, resizing or rotating the image agrees about
// where its corners are, a corner drag keeps the opposite corner still, and
// "Fit image to walls" lands the image over what has already been drawn.
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

const fns = ['function round3(v) {', 'function bgGeom(bg) {', 'function bgPoint(bg, u, v) {',
             'function bgFrac(bg, p) {', 'function bgVecToImage(bg, v) {',
             'function bgResizeAnchored(bg, wNewM, uA, vA, anchor) {',
             'function drawingBounds(lvl) {']
  .map(mark => slice(mark, '\n}')).join('\n');
const m = { exports: {} };
new Function('module', fns + '\nmodule.exports = { round3, bgGeom, bgPoint, bgFrac,'
             + ' bgVecToImage, bgResizeAnchored, drawingBounds };')(m);
const { bgGeom, bgPoint, bgFrac, bgVecToImage, bgResizeAnchored, drawingBounds } = m.exports;

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}
function section(t) { console.log('\n  ' + t); }
const near = (a, b, tol = 1e-6) => Math.abs(a - b) < tol;
const nearPt = (p, q, tol = 1e-6) => near(p.x, q.x, tol) && near(p.y, q.y, tol);

// 800x600 px at 100 px/m => 8 x 6 m, bottom-left at (2, 1).
const bg = (over = {}) => ({
  present: true, pixels_per_metre: 100, image_width_px: 800, image_height_px: 600,
  origin_x_m: 2, origin_y_m: 1, rotation_deg: 0, opacity: 0.5, ...over,
});

section('the corners of an unrotated image are where the numbers say');
{
  const b = bg();
  const g = bgGeom(b);
  check('it is 8 x 6 m', near(g.wM, 8) && near(g.hM, 6), g);
  check('bottom-left is the origin', nearPt(bgPoint(b, 0, 1), { x: 2, y: 1 }));
  check('top-left is one height up', nearPt(bgPoint(b, 0, 0), { x: 2, y: 7 }));
  check('bottom-right is one width across', nearPt(bgPoint(b, 1, 1), { x: 10, y: 1 }));
  check('top-right is both', nearPt(bgPoint(b, 1, 0), { x: 10, y: 7 }));
  check('the centre is the centre', nearPt(bgPoint(b, 0.5, 0.5), { x: 6, y: 4 }));
}

section('bgFrac is the inverse of bgPoint, rotated or not');
{
  for (const rot of [0, 30, 90, 217.5, 359]) {
    const b = bg({ rotation_deg: rot });
    let worst = 0;
    for (const [u, v] of [[0, 0], [1, 0], [0.3, 0.8], [0.5, 0.5], [1, 1]]) {
      const f = bgFrac(b, bgPoint(b, u, v));
      worst = Math.max(worst, Math.abs(f.u - u), Math.abs(f.v - v));
    }
    check(`round-trips at ${rot} deg`, worst < 1e-9, worst);
  }
}

section('a point outside the image reads as outside it');
{
  const b = bg();
  const inside = p => { const f = bgFrac(b, p); return f.u >= 0 && f.u <= 1 && f.v >= 0 && f.v <= 1; };
  check('the centre is inside', inside({ x: 6, y: 4 }));
  check('just past the right edge is not', !inside({ x: 10.01, y: 4 }));
  check('below the bottom edge is not', !inside({ x: 6, y: 0.99 }));
}

section('rotation turns the image about its own centre');
{
  const b = bg({ rotation_deg: 90 });
  // 90 deg anti-clockwise: the bottom-left corner swings to the bottom-right.
  check('the centre does not move', nearPt(bgPoint(b, 0.5, 0.5), { x: 6, y: 4 }));
  check('bottom-left swings round', nearPt(bgPoint(b, 0, 1), { x: 9, y: 0 }), bgPoint(b, 0, 1));
}

section('dragging a corner pins the opposite one');
{
  for (const rot of [0, 37]) {
    const b = bg({ rotation_deg: rot });
    // Drag the top-right corner; the bottom-left (0, 1) must not move.
    const anchor = bgPoint(b, 0, 1);
    bgResizeAnchored(b, 12, 0, 1, anchor);
    check(`the pinned corner holds at ${rot} deg`, nearPt(bgPoint(b, 0, 1), anchor, 1e-3),
          [anchor, bgPoint(b, 0, 1)]);
    const g = bgGeom(b);
    check(`and the aspect ratio survives at ${rot} deg`,
          near(g.wM / g.hM, 800 / 600, 1e-3), g);
    check(`and px/m follows the new width at ${rot} deg`,
          near(b.pixels_per_metre, 800 / 12, 1e-3), b.pixels_per_metre);
  }
}

section('the drag projects the cursor onto the diagonal it started on');
{
  const b = bg();
  // Grip: top-right (u=1, v=0); anchor: bottom-left (u=0, v=1).
  const anchor = bgPoint(b, 0, 1);
  const g0 = bgGeom(b);
  const d0 = { x: (1 - 0) * g0.wM, y: (1 - 0) * g0.hM };   // (8, 6) in image frame
  const len2 = d0.x * d0.x + d0.y * d0.y;
  // Cursor exactly at twice the diagonal => the image should double.
  const cursor = { x: anchor.x + 16, y: anchor.y + 12 };
  const d = bgVecToImage(b, { x: cursor.x - anchor.x, y: cursor.y - anchor.y });
  const s = (d.x * d0.x + d.y * d0.y) / len2;
  check('the scale factor is 2', near(s, 2), s);
  bgResizeAnchored(b, g0.wM * s, 0, 1, anchor);
  check('the grip lands under the cursor', nearPt(bgPoint(b, 1, 0), cursor, 1e-3), bgPoint(b, 1, 0));
}

section('px/m stays inside the range the backend accepts');
{
  const b = bg();
  bgResizeAnchored(b, 1e9, 0.5, 0.5, { x: 0, y: 0 });
  check('a huge drag clamps at 1 px/m', near(b.pixels_per_metre, 1), b.pixels_per_metre);
  bgResizeAnchored(b, 1e-9, 0.5, 0.5, { x: 0, y: 0 });
  check('a tiny drag clamps at 10000 px/m', near(b.pixels_per_metre, 10000), b.pixels_per_metre);
}

section('fit to walls covers the whole drawing');
{
  // The fit itself needs currentLevel/toast, so exercise the maths it runs.
  const lvl = {
    walls: [{ x1: 0, y1: 0, x2: 10, y2: 0 }, { x1: 10, y1: 0, x2: 10, y2: 4 }],
    rooms: [{ polygon: [[0, 0], [10, 0], [10, 4], [0, 4]] }],
  };
  const bounds = drawingBounds(lvl);
  check('the bounds are the walls', bounds.minX === 0 && bounds.minY === 0
        && bounds.maxX === 10 && bounds.maxY === 4, bounds);

  const b = bg({ pixels_per_metre: 4000 });   // absurdly small: 0.2 x 0.15 m
  const aspect = b.image_height_px / b.image_width_px;
  const wM = Math.max(bounds.maxX - bounds.minX, (bounds.maxY - bounds.minY) / aspect);
  bgResizeAnchored(b, wM, 0.5, 0.5,
                   { x: (bounds.minX + bounds.maxX) / 2, y: (bounds.minY + bounds.maxY) / 2 });
  const g = bgGeom(b);
  check('the image is at least as wide as the drawing', g.wM >= 10 - 1e-9, g);
  check('and at least as tall', g.hM >= 4 - 1e-9, g);
  const f0 = bgFrac(b, { x: 0, y: 0 }), f1 = bgFrac(b, { x: 10, y: 4 });
  check('so every drawn corner falls on the image',
        f0.u >= -1e-9 && f0.v <= 1 + 1e-9 && f1.u <= 1 + 1e-9 && f1.v >= -1e-9, [f0, f1]);
}

section('an untouched level has no bounds to fit to');
{
  check('so the fit is skipped', drawingBounds({ walls: [], rooms: [] }) === null);
}

console.log('\n' + (fails.length ? `  ${fails.length} FAILED` : '  all passed'));
process.exit(fails.length ? 1 : 0);
