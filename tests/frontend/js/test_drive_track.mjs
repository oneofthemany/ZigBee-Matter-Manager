// static/js/drive-track.js imported as shipped; it is DOM-free.
// Run: node tests/frontend/js/test_drive_track.mjs
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const { LAYERS, NO_DATA_COLOUR, rampColour, layerColour, findStops, findPeaks, nearestIndex,
        terrainOf, terrainSummary, terrainRuns } =
  await import(pathToFileURL(path.join(REPO, 'static/js/drive-track.js')));

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name + (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}

// 10 s cadence; speed in m/s.
const fix = (i, speed, extra = {}) => ({ ts: 1000 + i * 10, lat: 51 + i / 1000, lon: -1, speed_mps: speed, ...extra });

console.log('\n  colour scales');
const ramp = ['#000000', '#ff0000', '#ffffff'];
check('ramp ends are the first and last stops', rampColour(ramp, 0) === '#000000' && rampColour(ramp, 1) === '#ffffff');
check('ramp interpolates between stops', rampColour(ramp, 0.25) === '#800000', rampColour(ramp, 0.25));
check('ramp clamps outside 0..1', rampColour(ramp, -3) === '#000000' && rampColour(ramp, 7) === '#ffffff');

const track = [fix(0, 0), fix(1, 10), fix(2, 31), fix(3, 200), fix(4, null)];
const dom = LAYERS.speed.domain(track);
check('speed domain rounds the trip maximum up to 10 mph', dom[0] === 0 && dom[1] === 70, dom);
check('a GPS-glitch speed neither sets the scale nor gets a colour',
  LAYERS.speed.value(track[3]) === null && layerColour(LAYERS.speed, dom, LAYERS.speed.value(track[3])) === NO_DATA_COLOUR);
check('a missing speed is grey, not the slow end of the scale',
  layerColour(LAYERS.speed, dom, LAYERS.speed.value(track[4])) === NO_DATA_COLOUR);
check('a standstill is the slow end, not grey',
  layerColour(LAYERS.speed, dom, LAYERS.speed.value(track[0])) === LAYERS.speed.ramp[0]);
check('a short slow trip still gets a 30 mph scale', LAYERS.speed.domain([fix(0, 2)])[1] === 30);

const hills = [fix(0, 10, { gradient_pct: -12 }), fix(1, 10, { gradient_pct: 3 }), fix(2, 10, { gradient_pct: null })];
const gdom = LAYERS.gradient.domain(hills);
check('gradient domain is symmetric and covers the steepest descent', gdom[0] === -15 && gdom[1] === 15, gdom);
check('level road sits on the diverging midpoint', layerColour(LAYERS.gradient, gdom, 0) === LAYERS.gradient.ramp[1]);
check('roughness scale is fixed at 2 m/s² for a smooth trip',
  LAYERS.roughness.domain([fix(0, 10, { vert_rms_mps2: 0.34 })])[1] === 2);

console.log('\n  stops');
// stationary start, drive, 30 s at lights, drive, tunnel gap while stopped, drive, brief stop at the end
const drive = [
  fix(0, 0), fix(1, 0.2),
  fix(2, 8), fix(3, 12),
  fix(4, 0.1), fix(5, 0), fix(6, 0.3),
  fix(7, 9),
  fix(8, 0), { ...fix(20, 0) },
  fix(21, 7), fix(22, 0),
];
const stops = findStops(drive);
check('one pin per moving→stopped transition', stops.length === 3, stops);
check('standing before the first movement is not a stop', stops[0].ts === drive[4].ts, stops[0]);
check('idle time spans the whole standstill', stops[0].idle_s === 30, stops[0]);
check('a signal gap longer than 30 s adds no idling', stops[1].idle_s === 10, stops[1]);
check('a stop is pinned where the car came to rest', stops[0].lat === drive[4].lat);
const broken = [fix(0, 8), fix(1, null), fix(2, 0)];
check('an unknown speed before a standstill is not a transition', findStops(broken).length === 0);

console.log('\n  peaks');
const motion = [
  fix(0, 10, { long_peak_mps2: -1.2, lat_peak_mps2: 0.4 }),
  fix(1, 10, { long_peak_mps2: 1.1, lat_peak_mps2: 1.3 }),
  fix(2, 10, { long_peak_mps2: -2.9, lat_peak_mps2: 0.2 }),
  fix(3, 10),
];
const peaks = findPeaks(motion);
check('peak braking is the most negative longitudinal fix, as a magnitude',
  peaks.brake.point === motion[2] && peaks.brake.value === 2.9, peaks.brake);
check('peak acceleration is the most positive longitudinal fix', peaks.accel.point === motion[1] && peaks.accel.value === 1.1);
check('peak cornering is the largest lateral fix', peaks.corner.point === motion[1] && peaks.corner.value === 1.3);
const none = findPeaks([fix(0, 10), fix(1, 12)]);
check('a trip without motion data has no peaks', none.brake === null && none.accel === null && none.corner === null, none);
check('a trip that only ever braked has no acceleration peak',
  findPeaks([fix(0, 10, { long_peak_mps2: -1 })]).accel === null);

console.log('\n  nearestIndex');
check('finds the fix closest in time', nearestIndex(drive, 1034) === 3 && nearestIndex(drive, 1036) === 4);
check('clamps to the ends', nearestIndex(drive, 0) === 0 && nearestIndex(drive, 9e9) === drive.length - 1);
check('an empty track has no nearest fix', nearestIndex([], 5) === -1);

console.log('\n  terrain');
const g = (i, speed, grad) => fix(i, speed, { gradient_pct: grad });
check('a gradient inside the level band is level, not a hill',
  terrainOf(g(0, 10, 1.5)) === 'level' && terrainOf(g(0, 10, -1.5)) === 'level');
check('an unknown gradient is no terrain at all, not level', terrainOf(g(0, 10, null)) === null);
// climb at 10 m/s for 30 s, descend at 20 m/s for 20 s, crawl 20 s, then a 5 min gap
const hilly = [g(0, 10, 0), g(1, 10, 5), g(2, 10, 6), g(3, 10, 4), g(4, 20, -5), g(5, 20, -7),
               g(6, 2, null), g(7, 2, null), g(40, 15, 5)];
const ts = terrainSummary(hilly);
check('uphill distance is speed × time over the climbing fixes', ts.up.distance_m === 300 && ts.up.duration_s === 30, ts.up);
check('downhill average speed is its own, not the trip\'s', ts.down.avg_speed_mps === 20, ts.down);
check('a class never driven is absent rather than zero', ts.level === null, ts.level);
check('crawling with no gradient is counted as unclassified', ts.unknown_s === 20, ts.unknown_s);
check('a signal gap contributes to no class', ts.up.duration_s === 30);
const braked = [g(0, 10, 0), fix(1, 10, { gradient_pct: -6, long_peak_mps2: -2.5 }),
                fix(2, 10, { gradient_pct: -6, long_peak_mps2: -1.0 }),
                fix(3, 10, { gradient_pct: 6, long_peak_mps2: 1.8 }), g(4, 10, 0)];
const bs = terrainSummary(braked);
check('peak braking on a descent is the hardest stop made going down, as a magnitude', bs.down.max_brake_mps2 === 2.5, bs.down);
check('acceleration on the climb is not credited to the descent', bs.down.max_accel_mps2 === null && bs.up.max_accel_mps2 === 1.8, bs);
check('terrain without motion data has no peaks rather than zero ones', bs.level.max_brake_mps2 === null && bs.level.max_accel_mps2 === null, bs.level);
const runs = terrainRuns(hilly);
check('a sustained climb then descent are two runs', runs.length === 2 && runs[0].kind === 'up' && runs[1].kind === 'down', runs);
check('a run spans from the fix before it to its last fix', runs[0].from === hilly[0].ts && runs[0].to === hilly[3].ts, runs[0]);
check('a single steep fix is not a hill', terrainRuns([g(0, 10, 0), g(1, 10, 9), g(2, 10, 0)]).length === 0);
check('a trip with no barometer has no terrain',
  terrainRuns(track).length === 0 && terrainSummary(track).up === null);

console.log(`\n  ${fails.length ? fails.length + ' failed' : 'all passed'}`);
process.exit(fails.length ? 1 : 0);
