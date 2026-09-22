// Room drawing rules, sliced out of the shipped floor-plan.js.
//
// The claim: a room's corners can only be the walls' corners, so its outline
// lies on the walls the backend matches windows and outside walls against,
// and no two rooms overlap — while neighbours may still share an edge.
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

const names = ['projectPointOntoSegment', 'lineIntersection', 'joinWallEnds', 'wallGraph', 'wallPath',
               'roomLegs', 'snapLevelToWalls', 'escapeHtml', 'polygonCentroid', 'pointInPolygon', 'samePoint', 'sideOf', 'segmentCrossing', 'withCornersOnTheWay', 'wallEnds',
               'planCorners', 'nearestPoint', 'pointStrictlyInPolygon', 'interiorPoint',
               'polygonArea', 'roomEdgeProblem', 'roomPolygonProblem', 'snapRoomToWalls'];
const fns = names.map(n => slice(`function ${n}(`, '\n}')).join('\n');
const m = { exports: {} };
new Function('module', 'const CORNER_EPS_M = 1e-3, GEOM_TOL_M = 0.01, CORNER_PICKUP_M = 0.05, WALL_JOIN_REACH_M = 0.3, ROOM_SNAP_REACH_M = 1.5;\n' + fns
             + `\nmodule.exports = { ${names.join(', ')} };`)(m);
const { planCorners, roomEdgeProblem, roomPolygonProblem, snapRoomToWalls, joinWallEnds,
        roomLegs, snapLevelToWalls } = m.exports;

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}
function section(t) { console.log('\n  ' + t); }
const P = (x, y) => ({ x, y });

// A 9 × 4 outline split by one internal wall at x = 5.
const level = () => ({
  walls: [{ id: 's', x1: 0, y1: 0, x2: 9, y2: 0 }, { id: 'e', x1: 9, y1: 0, x2: 9, y2: 4 },
          { id: 'n', x1: 9, y1: 4, x2: 0, y2: 4 }, { id: 'w', x1: 0, y1: 4, x2: 0, y2: 0 },
          { id: 'i', x1: 5, y1: 0, x2: 5, y2: 4 }],
  rooms: [],
});

section('where a corner can go');
const lvl = level();
const corners = planCorners(lvl);
const has = p => corners.some(c => Math.abs(c.x - p.x) < 1e-9 && Math.abs(c.y - p.y) < 1e-9);
check('wall ends are corners, once each', corners.length === 6 && has(P(5, 0)) && has(P(0, 4)), corners);
const crossed = { walls: [{ id: 'a', x1: 0, y1: 2, x2: 4, y2: 2 }, { id: 'b', x1: 2, y1: 0, x2: 2, y2: 4 }], rooms: [] };
check('so is where two walls cross', planCorners(crossed).some(c => c.x === 2 && c.y === 2),
      planCorners(crossed));

section('edges');
check('along the walls is fine', roomEdgeProblem(lvl, P(0, 0), P(5, 0)) === null);
check('through a wall is not', /wall/.test(roomEdgeProblem(lvl, P(0, 0), P(9, 4)) || ''),
      roomEdgeProblem(lvl, P(0, 0), P(9, 4)));

section('rooms');
const lounge = [P(0, 0), P(5, 0), P(5, 4), P(0, 4)];
check('a room on the wall corners closes', roomPolygonProblem(lvl, lounge) === null);
lvl.rooms.push({ id: 'lounge', name: 'Lounge', polygon: lounge.map(p => [p.x, p.y]) });
check('its neighbour may share the dividing wall',
      roomPolygonProblem(lvl, [P(5, 0), P(9, 0), P(9, 4), P(5, 4)]) === null);
check('the same room twice is refused',
      /Lounge/.test(roomPolygonProblem(lvl, lounge) || ''), roomPolygonProblem(lvl, lounge));
check('an edge through the lounge is refused',
      /Lounge/.test(roomEdgeProblem({ walls: [], rooms: lvl.rooms }, P(0, 0), P(5, 4)) || ''));
check('as is one around it',
      /Lounge/.test(roomPolygonProblem({ walls: [], rooms: lvl.rooms },
                                       [P(-1, -1), P(6, -1), P(6, 5), P(-1, 5)]) || ''));
check('and a bow-tie outline', /crosses itself/.test(
      roomPolygonProblem({ walls: [], rooms: [] }, [P(0, 0), P(4, 4), P(4, 0), P(0, 6)]) || ''));
check('and three corners in a line',
      /no floor/.test(roomPolygonProblem({ walls: [], rooms: [] }, [P(0, 0), P(2, 0), P(4, 0)]) || ''));

section('mending a hand-traced room');
const traced = level();
const kitchen = { id: 'k', name: 'Kitchen', polygon: [[5.2, 0.3], [8.7, 0.2], [8.8, 3.7], [5.3, 3.8]] };
traced.rooms.push(kitchen);
check('its corners move onto the wall corners', snapRoomToWalls(kitchen, traced) === null
      && JSON.stringify(kitchen.polygon) === JSON.stringify([[5, 0], [9, 0], [9, 4], [5, 4]]),
      kitchen.polygon);
const stray = { id: 'x', polygon: [[0.2, 0.2], [3, 0.2], [3, 2], [0.2, 2]] };
traced.rooms.push(stray);
const before = JSON.stringify(stray.polygon);
check('a corner nowhere near a wall corner is left alone, and says so',
      /more than 1.5 m from any wall corner/.test(snapRoomToWalls(stray, traced) || '') && JSON.stringify(stray.polygon) === before);

section('walls that nearly meet');
const gappy = level();
gappy.walls.find(w => w.id === 'i').y2 = 3.82;          // stops 18 cm short of the north wall
gappy.walls.find(w => w.id === 'e').y1 = 0.1;           // and a corner that doesn't close
gappy.openings = [{ id: 'o', wall_id: 'i', offset_m: 1, width_m: 1 }];
check('has no corner where they should meet', !planCorners(gappy).some(c => c.x === 5 && c.y === 4));
check('joining moves both near misses', joinWallEnds(gappy) === 2, gappy.walls);
const inner = gappy.walls.find(w => w.id === 'i');
check('the short wall now reaches the one it meets', Math.abs(inner.y2 - 4) < 1e-9 && inner.x2 === 5, inner);
check('and the open corner is closed', gappy.walls.find(w => w.id === 'e').y1 === 0);
check('an opening measured from a start that did not move stays put', gappy.openings[0].offset_m === 1);
check('a second pass has nothing to do', joinWallEnds(gappy) === 0);
const shifted = { walls: [{ id: 'a', x1: 0, y1: 0, x2: 4, y2: 0 },
                          { id: 'b', x1: 2, y1: 0.2, x2: 2, y2: 3 }],
                  openings: [{ id: 'o', wall_id: 'b', offset_m: 1, width_m: 0.8 }] };
joinWallEnds(shifted);
check('when a wall start moves, its openings keep their place on the ground',
      shifted.walls[1].y1 === 0 && Math.abs(shifted.openings[0].offset_m - 1.2) < 1e-9, shifted);
const stub = { walls: [{ id: 'a', x1: 0, y1: 0, x2: 4, y2: 0 }, { id: 'b', x1: 2, y1: 1, x2: 2, y2: 3 }] };
check('a wall end well clear of any wall is left alone', joinWallEnds(stub) === 0 && stub.walls[1].y1 === 1);
const tiny = { walls: [{ id: 'a', x1: 0, y1: 0, x2: 4, y2: 0 }, { id: 'b', x1: 2, y1: 0.08, x2: 2, y2: 0.15 }] };
check('and a wall is never shrunk to nothing by joining', joinWallEnds(tiny) === 0 || Math.hypot(
      tiny.walls[1].x2 - tiny.walls[1].x1, tiny.walls[1].y2 - tiny.walls[1].y1) >= 0.1, tiny.walls[1]);

section('edges follow the walls');
// An L-shaped room: the corner at (5, 2) is where a jog in the walls is.
const ell = { walls: [{ id: 'a', x1: 0, y1: 0, x2: 5, y2: 0 }, { id: 'b', x1: 5, y1: 0, x2: 5, y2: 2 },
                      { id: 'c', x1: 5, y1: 2, x2: 8, y2: 2 }, { id: 'd', x1: 8, y1: 2, x2: 8, y2: 5 },
                      { id: 'e', x1: 8, y1: 5, x2: 0, y2: 5 }, { id: 'f', x1: 0, y1: 5, x2: 0, y2: 0 }],
              rooms: [] };
const leg = roomLegs(ell, P(5, 0), P(8, 2));
check('clicking past a jog picks up its corner', leg.problem === null
      && leg.via.length === 1 && leg.via[0].x === 5 && leg.via[0].y === 2, leg);
const bare = { walls: [], rooms: [] };                   // nothing drawn but rooms
const across = roomLegs(bare, P(0, 0), P(4, 0));
check('with no walls to follow, the edge goes straight', across.via.length === 0 && across.problem === null);

section('corners the server rounded to the millimetre');
// A slanted party wall with two walls meeting it, their junctions stored to
// the mm — so a fraction of a millimetre off the slanted line.
const slant = { walls: [{ id: 'p', x1: 6.7, y1: 8.8, x2: 6.5, y2: 26.2 },
                        { id: 'a', x1: 6.602, y1: 17.3, x2: 9.4, y2: 17.3 },
                        { id: 'b', x1: 6.566, y1: 20.5, x2: 8.8, y2: 20.5 }], rooms: [] };
const j1 = P(6.602, 17.3), j2 = P(6.566, 20.5);
check('an edge along the wall between them does not cut it', roomEdgeProblem(slant, j1, j2) === null,
      roomEdgeProblem(slant, j1, j2));
check('they still count as joined, so picking the Room tool moves nothing',
      joinWallEnds(JSON.parse(JSON.stringify(slant))) === 0);
check('and the walls still link them, so the edge can follow the wall',
      roomLegs(slant, j1, j2).problem === null);
check('a real crossing is still a crossing',
      /wall/.test(roomEdgeProblem(slant, P(5, 18), P(8, 18)) || ''));

section('corners on the way are picked up');
// One long wall with a T-junction half way; a room edge clicked end to end.
const tee = { walls: [{ id: 'l', x1: 0, y1: 0, x2: 10, y2: 0 }, { id: 't', x1: 5, y1: 0, x2: 5, y2: 3 }],
              rooms: [] };
const run = roomLegs(tee, P(0, 0), P(10, 0));
check('an end-to-end edge bends through the junction it passes',
      run.problem === null && run.via.length === 1 && run.via[0].x === 5 && run.via[0].y === 0, run);
// A neighbour's corner 3 cm off the straight line between two corners.
const beside = { walls: [], rooms: [{ id: 'n', name: 'Next door', polygon: [[4, 0.03], [6, 0.03], [6, -3], [4, -3]] }] };
const skim = roomLegs(beside, P(0, 0), P(10, 0));
check("and through a neighbour's corners within 5 cm, instead of skimming into it",
      skim.problem === null && skim.via.map(p => p.x).join() === '4,6', skim);
check('a corner further off is left alone',
      roomLegs({ walls: [], rooms: [{ id: 'f', polygon: [[4, 0.2], [6, 0.2], [6, 3], [4, 3]] }] },
               P(0, 0), P(10, 0)).via.length === 0);

section('mending a whole traced level');
const whole = level();
whole.walls.find(w => w.id === 'i').y2 = 3.85;
whole.rooms = [{ id: 'l', name: 'Lounge', polygon: [[0.3, 0.2], [4.7, 0.3], [4.8, 3.7], [0.2, 3.8]] },
               { id: 'k', name: 'Kitchen', polygon: [[4.6, 0.2], [8.7, 0.2], [8.8, 3.8], [4.6, 3.8]] }];
const res = snapLevelToWalls(whole);
check('joins the walls, then fits every room — even one overlapping its neighbour as drawn',
      res.joined === 1 && res.snapped === 2 && res.problems.size === 0, res);
check('so the neighbours share the dividing wall exactly',
      JSON.stringify(whole.rooms.map(r => r.polygon.map(p => p.map(v => +v.toFixed(9))))) === JSON.stringify(
        [[[0, 0], [5, 0], [5, 4], [0, 4]], [[5, 0], [9, 0], [9, 4], [5, 4]]]), whole.rooms);

console.log(`\n${fails.length ? 'FAILED' : 'passed'}: ${fails.length} failure(s)`);
process.exit(fails.length ? 1 : 0);
