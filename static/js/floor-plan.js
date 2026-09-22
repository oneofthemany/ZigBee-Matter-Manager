// The home floor plan editor: walls, openings, rooms, radiators, sensors,
// contacts and placed devices, on one plan shared by Heating and Topology.
//
//   openFloorPlanEditor({ circuits, devices, sensors, contacts, onSave });  // Heating: modal, heating view
//   showFloorPlanInline(hostEl);                                             // Topology: inline, home view
//
// One editor DOM (#fpRoot) moves between the two hosts; the view only decides
// what is shown. Saves to /api/floor-plan. Coordinates, overlays and the
// one-position rule: docs/floor-plan.md § Placing devices.

const log = zmmLog('floor-plan');

const FP_VERSION = 1;
const DEFAULT_LEVEL_HEIGHT = 2.4;
const DEFAULT_SNAP_M = 0.1;          // default snap-to-grid step, metres (user-adjustable)
const PIXELS_PER_METRE_DEFAULT = 80;

// module state (per-modal-open)

let _state = null;         // see resetState()
let _onSaveCallback = null;
let _availableDevices = { trvs: [], sensors: [], contacts: [] };
// Circuits passed in by the caller (heating-controller config). Used purely
// for display: showing which circuit a radiator's bound room belongs to,
// in the radiator props panel. Schema: [{ id, name, rooms: [{id, name}] }, ...]
let _availableCircuits = [];
// True while the Alt key is held — suppresses grid snap and endpoint merging
// so points can be placed exactly where the cursor is.
let _altDown = false;
// 'heating' shows heating's devices and tools; 'home' shows every device.
let _view = 'heating';
let _root = null;          // #fpRoot, mounted in the modal or inline
let _inlineHost = null;    // Topology's container, once it has asked
// ieee → { name, kind } for every device the hub knows; kind picks the icon.
let _catalogue = new Map();
// Devices heating works with (its own classification, from its routes).
let _heatingIeees = new Set();
let _home = null;          // { lat, lon } for the map backdrop
let _devDrag = null;       // { ieee } or { sensorId } while dragging a marker
// A plan loaded into a canvas with no size yet (a modal still animating in, a
// hidden tab) is fitted when the canvas first gets one.
let _needsFit = false;

function resetState(plan) {
    _fieldCache = new Map();
    _daylightCache = new Map();
    _state = {
        plan: plan || newEmptyPlan(),
        currentLevelId: null,
        tool: 'select',
        selection: null,           // {kind, id} or null
        drawBuffer: null,          // tool-specific scratchpad
        zoom: PIXELS_PER_METRE_DEFAULT,
        pan: { x: 0, y: 0 },       // pixel offset
        snapStep: DEFAULT_SNAP_M,  // grid-snap step in metres; 0 = off
        angleStep: 1,              // Ctrl angle-lock increment in degrees
        showGrid: true,
        showBackground: true,
        showSun: false,
        showThermal: false,
        showContours: false,
        showColdZones: false,
        thermalMode: 'rad',        // 'rad' | 'rad+sun' | 'sun' — heat sources in the field
        heatFluxWm2: 50,
        sunData: null,
        solarImpact: null,         // Map roomKey → measured entry, once fetched
        solarImpactLoaded: false,  // true after fetch attempt (even if empty)
        calibration: null,         // { p1: {x,y} } during 2-click calibrate
        showMap: false,
        placeCursor: null,         // pointer position while a device is armed
        showDaylight: false,
        daylightError: null,       // why there is no estimate, shown in the panel
        showMesh: false,
        mesh: null,                // /api/floor-plan/mesh, fetched when shown
        showCoverage: false,
        coverage: null,            // /api/floor-plan/coverage (or its last snapshot while that runs)
        coverageMeta: null,        // which snapshot is shown, and whether a fresh one is on its way
        coverageHistory: [],       // /api/floor-plan/coverage/history, for the Compare picker
        coverageSource: '',        // one device's coverage instead of the best ('' = best)
        coverageView: null,        // that device's fields
        coverageCompare: null,     // buildComparison() against a chosen snapshot
        daylight: null,            // /api/floor-plan/daylight, fetched when shown
        daylightIndex: 0,
        placing: null,             // ieee armed from the palette, placed on next tap
    };
    _state.currentLevelId = (_state.plan.levels[0] || {}).id || null;
}

function newEmptyPlan() {
    return {
        version: FP_VERSION,
        north_offset_deg: 0,
        scale_pixels_per_metre: PIXELS_PER_METRE_DEFAULT,
        circuits: [],
        levels: [{
            id: 'ground',
            name: 'Ground floor',
            index: 0,
            ceiling_height_m: DEFAULT_LEVEL_HEIGHT,
            floor_above_ground_m: 0,
            walls: [],
            openings: [],
            rooms: [],
            radiators: [],
            sensors: [],
            contacts: [],
            devices: [],
        }],
    };
}

function genId(prefix) {
    return `${prefix}_${Math.random().toString(36).slice(2, 8)}`;
}

function currentLevel() {
    return _state.plan.levels.find(l => l.id === _state.currentLevelId) || _state.plan.levels[0];
}

// public entry

/** Heating's entry: the modal, filtered to heating. */
export async function openFloorPlanEditor(opts = {}) {
    _availableDevices = {
        trvs: opts.devices?.thermostats || [],
        sensors: opts.sensors || [],
        contacts: opts.contacts || [],
        receivers: opts.devices?.receivers || opts.receivers || [],
    };
    _availableCircuits = opts.circuits || [];
    _onSaveCallback = opts.onSave || null;
    ensureModal();
    const modalEl = document.getElementById('floorPlanModal');
    mountRoot(modalEl.querySelector('.modal-content'));
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
    await loadPlan('heating');
}

/** Topology's entry: inline in ``host``, every device. */
export async function showFloorPlanInline(host) {
    _inlineHost = host;
    // Heating has it open; it comes back here when that modal closes.
    if (_root && _root.closest('#floorPlanModal.show')) return;
    _onSaveCallback = null;
    mountRoot(host);
    await loadPlan('home');
}

async function loadPlan(view) {
    let initialPlan;
    try {
        const r = await fetch('/api/floor-plan').then(r => r.json());
        initialPlan = (r && r.success && r.plan) ? r.plan : newEmptyPlan();
        _home = r?.home || null;
    } catch {
        initialPlan = newEmptyPlan();
    }

    // Heal orphan images: any level with no `background` block but an image
    // on disk gets a synthesised metadata block so the user sees their image
    // and can recalibrate it.
    let orphansAdopted = 0;
    for (const lvl of (initialPlan.levels || [])) {
        if (lvl.background?.present) continue;
        const adopted = await tryAdoptOrphanImage(lvl);
        if (adopted) orphansAdopted += 1;
    }

    resetState(initialPlan);
    // Ensure plan always has a circuits array (backward compat with older saves)
    if (!Array.isArray(_state.plan.circuits)) _state.plan.circuits = [];
    for (const l of _state.plan.levels) if (!Array.isArray(l.devices)) l.devices = [];
    _view = view;
    await loadCatalogue();
    applyViewChrome();
    const status = document.getElementById('fpSaveStatus');
    if (status) status.innerHTML = '';
    _needsFit = true;
    renderAll();

    if (orphansAdopted > 0) {
        const status = document.getElementById('fpSaveStatus');
        if (status) {
            status.innerHTML = `<span class="text-warning"><i class="fas fa-exclamation-triangle me-1"></i>` +
                `Recovered ${orphansAdopted} orphan image${orphansAdopted === 1 ? '' : 's'} — ` +
                `please run <strong>Calibrate</strong> for affected levels and Save.</span>`;
        }
    }
}

/**
 * Names and kinds for every device, and which of them heating works with.
 * Heating's list comes from heating's own routes (passed in, or fetched for
 * the home view) so the filter never disagrees with the heating page.
 */
async function loadCatalogue() {
    const get = url => fetch(url).then(r => r.ok ? r.json() : null).catch(() => null);
    const wantHeating = _view === 'home';
    const [devs, hDev, hSens, hCon] = await Promise.all([
        get('/api/devices'),
        wantHeating ? get('/api/heating/controller/devices') : null,
        wantHeating ? get('/api/heating/controller/sensors') : null,
        wantHeating ? get('/api/heating/controller/contact-sensors') : null,
    ]);
    if (wantHeating) {
        _availableDevices = {
            trvs: hDev?.thermostats || [], receivers: hDev?.receivers || [],
            sensors: hSens?.sensors || [], contacts: hCon?.sensors || [],
        };
    }
    _heatingIeees = new Set(
        [..._availableDevices.trvs, ..._availableDevices.sensors, ..._availableDevices.contacts]
            .map(d => String(d.ieee || '').toLowerCase()).filter(Boolean));
    _catalogue = new Map();
    for (const d of (Array.isArray(devs) ? devs : [])) {
        if (!d?.ieee) continue;
        const ieee = String(d.ieee).toLowerCase();
        _catalogue.set(ieee, { name: d.friendly_name || d.name || ieee, kind: deviceKind(ieee, d) });
    }
}

function deviceKind(ieee, d) {
    const caps = d.capability_list || [];
    if (_heatingIeees.has(ieee)) return 'heating';
    if (caps.includes('light')) return 'light';
    if (d.type === 'Coordinator') return 'coordinator';
    if (d.type === 'Router') return 'router';
    return 'other';
}

const KIND_LABEL = { light: 'Lights', heating: 'Heating', router: 'Routers',
                     coordinator: 'Coordinator', other: 'Other' };

/** Show only what this view is for: ``data-fp-view`` and ``data-fp-host``. */
function applyViewChrome() {
    const host = _root?.closest('#floorPlanModal') ? 'modal' : 'inline';
    _root?.querySelectorAll('[data-fp-view]').forEach(el => {
        el.classList.toggle('d-none', el.dataset.fpView !== _view);
    });
    _root?.querySelectorAll('[data-fp-host]').forEach(el => {
        el.classList.toggle('d-none', el.dataset.fpHost !== host);
    });
    const title = _root?.querySelector('#fpTitle');
    if (title) title.textContent = _view === 'heating' ? 'Floor plan — heating' : 'Floor plan';
    // The open panel may belong to the other view (Devices / Circuits).
    showPanel(savedPanel());
}

// sidebar panels — one open at a time beside the icon rail, or none

const PANEL_STORE_KEY = 'fp.sidebarPanel';

/** The panel last left open ('' = collapsed to the rail); Draw by default. */
function savedPanel() {
    try {
        const v = localStorage.getItem(PANEL_STORE_KEY);
        return v === null ? 'draw' : v;
    } catch { return 'draw'; }
}

/** Open panel ``key`` (falsy collapses to the rail) and remember it. */
function showPanel(key) {
    if (!_root) return;
    const btn = k => _root.querySelector(`.fp-rail-btn[data-fp-panel="${k}"]`);
    if (key && (!btn(key) || btn(key).classList.contains('d-none'))) key = 'draw';
    _root.querySelectorAll('.fp-rail-btn').forEach(b => {
        const on = b.dataset.fpPanel === key;
        b.classList.toggle('active', on);
        b.setAttribute('aria-expanded', String(on));
    });
    _root.querySelectorAll('[data-fp-panel-body]').forEach(p => { p.hidden = p.dataset.fpPanelBody !== key; });
    _root.classList.toggle('fp-panel-collapsed', !key);
    try { localStorage.setItem(PANEL_STORE_KEY, key || ''); } catch { /* per-viewer nicety only */ }
}

/**
 * For a level missing its `background` block, probe the image endpoint and
 * synthesise metadata if the image bytes exist on the server. Returns true
 * iff a block was synthesised.
 */
async function tryAdoptOrphanImage(lvl) {
    try {
        const url = `/api/floor-plan/image/${encodeURIComponent(lvl.id)}`;
        const resp = await fetch(url, { method: 'GET' });
        if (!resp.ok) return false;
        const blob = await resp.blob();
        if (!blob || blob.size === 0) return false;
        const dims = await readImageDimensions(blob);
        const contentType = blob.type || 'image/png';
        lvl.background = {
            present: true,
            pixels_per_metre: 50.0,            // placeholder — user must calibrate
            image_width_px: dims.width,
            image_height_px: dims.height,
            origin_x_m: 0,
            origin_y_m: 0,
            rotation_deg: 0,
            opacity: 0.5,
            content_type: contentType,
            _cb: Date.now(),
        };
        // The calibration is what was lost, so 50 px/m at the origin is a
        // guess. If the level was traced, sit the image on that tracing
        // instead — far closer than the guess, and visible either way.
        fitBackgroundToDrawing({ silent: true, level: lvl });
        return true;
    } catch {
        return false;
    }
}

// modal scaffold

/** The modal shell Heating opens; #fpRoot is moved into it. */
function ensureModal() {
    if (document.getElementById('floorPlanModal')) return;
    document.body.insertAdjacentHTML('beforeend', `
    <div class="modal fade" id="floorPlanModal" tabindex="-1" aria-hidden="true">
      <div class="modal-dialog modal-fullscreen"><div class="modal-content"></div></div>
    </div>`);
    document.getElementById('floorPlanModal').addEventListener('hidden.bs.modal', () => {
        _altDown = false;
        closeMobileDrawers();
        _state = null;
        // Topology was showing the plan before Heating borrowed it.
        if (_inlineHost?.isConnected && _inlineHost.offsetParent !== null) {
            showFloorPlanInline(_inlineHost);
        }
    });
}

/** Build #fpRoot once, then move it into ``host``. */
function mountRoot(host) {
    if (!_root) {
        _root = document.createElement('div');
        _root.id = 'fpRoot';
        _root.className = 'fp-root d-flex flex-column h-100';
        _root.innerHTML = rootHtml();
        host.appendChild(_root);
        bindModalEvents();
    } else if (_root.parentElement !== host) {
        host.appendChild(_root);
    }
}

function rootHtml() {
    return `
          <div class="modal-header py-2">
            <h5 class="modal-title"><i class="fas fa-drafting-compass me-2"></i><span id="fpTitle">Floor plan</span></h5>
            <div class="d-flex align-items-center gap-2 ms-auto">
              <div class="small text-muted d-none d-md-block" id="fpStatus"></div>
              <!-- Phone-only: the tools/levels and properties panes are off-canvas
                   drawers below 768px (see floor-plan.css) — there's no room for a
                   fixed 240px + 300px rail either side of the canvas. -->
              <button type="button" class="btn btn-sm btn-outline-secondary d-md-none" id="fpToggleSidebarBtn"
                      title="Tools & levels" aria-label="Toggle tools and levels">
                <i class="fas fa-sliders"></i>
              </button>
              <button type="button" class="btn btn-sm btn-outline-secondary d-md-none" id="fpTogglePropsBtn"
                      title="Properties" aria-label="Toggle properties">
                <i class="fas fa-list"></i>
              </button>
              <button type="button" class="btn-close" data-bs-dismiss="modal" data-fp-host="modal"></button>
            </div>
          </div>
          <div class="modal-body p-0 d-flex" style="overflow:hidden">
            <!-- Left: an icon rail; each button opens its panel beside it
                 (click it again to hide the panel and give the canvas room). -->
            <div id="fpSidebar" class="border-end fp-sidebar">
              <nav class="fp-rail" aria-label="Floor plan panels">
                <button type="button" class="fp-rail-btn" data-fp-panel="draw" title="Draw"
                        aria-controls="fpPanel-draw" aria-expanded="false">
                  <i class="fas fa-pen-ruler"></i><span>Draw</span></button>
                <button type="button" class="fp-rail-btn" data-fp-panel="levels" title="Levels"
                        aria-controls="fpPanel-levels" aria-expanded="false">
                  <i class="fas fa-layer-group"></i><span>Levels</span></button>
                <button type="button" class="fp-rail-btn" data-fp-panel="devices" data-fp-view="home" title="Devices"
                        aria-controls="fpPanel-devices" aria-expanded="false">
                  <i class="fas fa-microchip"></i><span>Devices</span></button>
                <button type="button" class="fp-rail-btn" data-fp-panel="circuits" data-fp-view="heating" title="Circuits"
                        aria-controls="fpPanel-circuits" aria-expanded="false">
                  <i class="fas fa-diagram-project"></i><span>Circuits</span></button>
                <button type="button" class="fp-rail-btn" data-fp-panel="layers" title="Layers"
                        aria-controls="fpPanel-layers" aria-expanded="false">
                  <i class="fas fa-eye"></i><span>Layers</span></button>
                <button type="button" class="fp-rail-btn" data-fp-panel="image" title="Image"
                        aria-controls="fpPanel-image" aria-expanded="false">
                  <i class="fas fa-image"></i><span>Image</span></button>
                <button type="button" class="fp-rail-btn" data-fp-panel="orient" title="Orient"
                        aria-controls="fpPanel-orient" aria-expanded="false">
                  <i class="fas fa-compass"></i><span>Orient</span></button>
              </nav>
              <div class="fp-panels">
                <section class="fp-panel" id="fpPanel-draw" data-fp-panel-body="draw" hidden>
                  <header class="fp-panel-head">
                    <h6>Draw</h6>
                    <button type="button" class="fp-panel-close" data-fp-panel-close title="Hide panel"
                            aria-label="Hide panel"><i class="fas fa-angles-left"></i></button>
                  </header>
                    <div class="btn-group-vertical w-100" role="group" id="fpToolbar">
                      <button class="btn btn-sm btn-outline-primary" data-tool="select"><i class="fas fa-mouse-pointer me-1"></i>Select</button>
                      <button class="btn btn-sm btn-outline-primary" data-tool="wall"><i class="fas fa-grip-lines-vertical me-1"></i>Wall</button>
                      <button class="btn btn-sm btn-outline-primary" data-tool="room"><i class="fas fa-vector-square me-1"></i>Room</button>
                      <button class="btn btn-sm btn-outline-primary" data-tool="window"><i class="fas fa-window-maximize me-1"></i>Window</button>
                      <button class="btn btn-sm btn-outline-primary" data-tool="door"><i class="fas fa-door-open me-1"></i>Door</button>
                      <button class="btn btn-sm btn-outline-primary" data-tool="radiator" data-fp-view="heating"><i class="fas fa-fire me-1"></i>Radiator</button>
                      <button class="btn btn-sm btn-outline-primary" data-tool="sensor" data-fp-view="heating"><i class="fas fa-thermometer-half me-1"></i>Sensor</button>
                      <button class="btn btn-sm btn-outline-primary" data-tool="contact" data-fp-view="heating"><i class="fas fa-link me-1"></i>Contact</button>
                      <button class="btn btn-sm btn-outline-warning" data-tool="calibrate"><i class="fas fa-ruler me-1"></i>Calibrate</button>
                      <button class="btn btn-sm btn-outline-warning" data-tool="bg"><i class="fas fa-image me-1"></i>Adjust image</button>
                    </div>
                    <div class="d-flex align-items-center gap-2 mt-2">
                      <label class="small text-muted mb-0" for="fpSnapStep">Snap</label>
                      <select class="form-select form-select-sm py-0" id="fpSnapStep" style="font-size:0.78rem">
                        <option value="0">Off</option>
                        <option value="0.01">1 cm</option>
                        <option value="0.05">5 cm</option>
                        <option value="0.1" selected>10 cm</option>
                        <option value="0.25">25 cm</option>
                        <option value="0.5">50 cm</option>
                      </select>
                    </div>
                    <div class="d-flex align-items-center gap-2 mt-1">
                      <label class="small text-muted mb-0" for="fpAngleStep">Angle</label>
                      <select class="form-select form-select-sm py-0" id="fpAngleStep" style="font-size:0.78rem">
                        <option value="1" selected>1°</option>
                        <option value="2">2°</option>
                        <option value="5">5°</option>
                        <option value="15">15°</option>
                        <option value="45">45°</option>
                      </select>
                    </div>
                    <div class="form-text small mt-1">Hold <kbd>Alt</kbd>: snap off · <kbd>Ctrl</kbd>: angle lock</div>
                </section>
                <section class="fp-panel" id="fpPanel-levels" data-fp-panel-body="levels" hidden>
                  <header class="fp-panel-head">
                    <h6>Levels</h6>
                    <button type="button" class="fp-panel-close" data-fp-panel-close title="Hide panel"
                            aria-label="Hide panel"><i class="fas fa-angles-left"></i></button>
                  </header>
                    <div id="fpLevelList" class="list-group list-group-flush small"></div>
                    <button class="btn btn-sm btn-outline-secondary w-100 mt-2" id="fpAddLevel"><i class="fas fa-plus me-1"></i>Add level</button>
                </section>
                <section class="fp-panel" id="fpPanel-devices" data-fp-panel-body="devices" data-fp-view="home" hidden>
                  <header class="fp-panel-head">
                    <h6>Devices to place</h6>
                    <button type="button" class="fp-panel-close" data-fp-panel-close title="Hide panel"
                            aria-label="Hide panel"><i class="fas fa-angles-left"></i></button>
                  </header>
                    <input type="search" id="fpPaletteSearch" class="form-control form-control-sm mb-1"
                           placeholder="Find a device" aria-label="Find a device">
                    <div class="form-text small mb-1">Drag one onto its room, or tap it and then tap the plan.</div>
                    <div id="fpPalette" class="small"></div>
                </section>
                <section class="fp-panel" id="fpPanel-circuits" data-fp-panel-body="circuits" data-fp-view="heating" hidden>
                  <header class="fp-panel-head">
                    <h6>Circuits</h6>
                    <button class="btn btn-sm btn-outline-success py-0 px-1" id="fpAddCircuit" title="Add circuit"><i class="fas fa-plus"></i></button>
                    <button type="button" class="fp-panel-close" data-fp-panel-close title="Hide panel"
                            aria-label="Hide panel"><i class="fas fa-angles-left"></i></button>
                  </header>
                    <div id="fpCircuitList" class="mb-1"></div>
                </section>
                <section class="fp-panel" id="fpPanel-layers" data-fp-panel-body="layers" hidden>
                  <header class="fp-panel-head">
                    <h6>Layers</h6>
                    <button type="button" class="fp-panel-close" data-fp-panel-close title="Hide panel"
                            aria-label="Hide panel"><i class="fas fa-angles-left"></i></button>
                  </header>
                    <div class="form-check form-switch small">
                      <input class="form-check-input" type="checkbox" id="fpToggleGrid" checked>
                      <label class="form-check-label" for="fpToggleGrid">Grid</label>
                    </div>
                    <div class="form-check form-switch small">
                      <input class="form-check-input" type="checkbox" id="fpToggleSun">
                      <label class="form-check-label" for="fpToggleSun">Sun path (today)</label>
                    </div>
                    <div class="form-check form-switch small">
                      <input class="form-check-input" type="checkbox" id="fpToggleDaylight">
                      <label class="form-check-label" for="fpToggleDaylight">Daylight in each room</label>
                    </div>
                    <div data-fp-view="home">
                      <div class="form-check form-switch small">
                        <input class="form-check-input" type="checkbox" id="fpToggleMesh">
                        <label class="form-check-label" for="fpToggleMesh">Mesh links</label>
                      </div>
                      <div class="form-check form-switch small">
                        <input class="form-check-input" type="checkbox" id="fpToggleCoverage">
                        <label class="form-check-label" for="fpToggleCoverage">Signal heatmap</label>
                      </div>
                      <div id="fpCoverageControls" class="ms-3 mb-1 small d-none">
                        <div id="fpCoverageStatus" class="mb-1" role="status" aria-live="polite"></div>
                        <div id="fpCoveragePickers" class="d-none">
                          <label class="form-label small mb-0" for="fpCoverageSource">Show</label>
                          <select id="fpCoverageSource" class="form-select form-select-sm mb-1"></select>
                          <label class="form-label small mb-0" for="fpCoverageCompare">Compare with</label>
                          <select id="fpCoverageCompare" class="form-select form-select-sm mb-1"></select>
                          <div id="fpCoverageCompareResult"></div>
                        </div>
                        <div id="fpCoverageModel" class="text-muted"></div>
                        <div id="fpCoverageAdvice" class="mt-1"></div>
                      </div>
                      <div id="fpMeshControls" class="ms-3 mb-1 small d-none">
                        <div><span class="fp-link-key fp-link-good"></span>LQI 200+
                          <span class="fp-link-key fp-link-ok ms-2"></span>150+</div>
                        <div><span class="fp-link-key fp-link-weak"></span>100+
                          <span class="fp-link-key fp-link-bad ms-2"></span>below 100</div>
                        <div class="form-text small" id="fpMeshNote"></div>
                      </div>
                    </div>
                    <div id="fpDaylightControls" class="ms-3 mb-1 d-none">
                      <input type="range" id="fpDaylightTime" class="form-range" min="0" max="48" step="1"
                             aria-label="Time of day">
                      <div class="small d-none" id="fpDaylightReadout"><span id="fpDaylightClock"></span>
                        · outside <span id="fpDaylightOutdoor"></span></div>
                      <div class="form-text small" id="fpDaylightNote">Estimated from the saved plan's windows
                        and today's weather.</div>
                    </div>
                    <div data-fp-view="heating">
                    <div class="form-check form-switch small">
                      <input class="form-check-input" type="checkbox" id="fpToggleThermal">
                      <label class="form-check-label" for="fpToggleThermal">Thermal overlay</label>
                    </div>
                    <div class="ms-3 mb-1">
                      <select class="form-select form-select-sm py-0" id="fpThermalMode" style="font-size:0.78rem" disabled>
                        <option value="rad" selected>Radiators only</option>
                        <option value="rad+sun">Radiators + solar</option>
                        <option value="sun">Solar gain only</option>
                      </select>
                    </div>
                    <div class="form-check form-switch small ms-3">
                      <input class="form-check-input" type="checkbox" id="fpToggleContours" disabled>
                      <label class="form-check-label text-muted" for="fpToggleContours">Contour lines</label>
                    </div>
                    <div class="form-check form-switch small ms-3">
                      <input class="form-check-input" type="checkbox" id="fpToggleColdZones" disabled>
                      <label class="form-check-label text-muted" for="fpToggleColdZones">Cold zones</label>
                    </div>
                    <div id="fpColdZoneControls" style="display:none" class="ms-4 mb-1">
                      <label class="small text-muted d-block mb-1">Building heat loss</label>
                      <select class="form-select form-select-sm py-0" id="fpHeatFlux" style="font-size:0.78rem">
                        <option value="30">Well insulated — 30 W/m²</option>
                        <option value="50" selected>Average — 50 W/m²</option>
                        <option value="80">Poorly insulated — 80 W/m²</option>
                      </select>
                    </div>
                    </div>
                </section>
                <section class="fp-panel" id="fpPanel-image" data-fp-panel-body="image" hidden>
                  <header class="fp-panel-head">
                    <h6>Background image</h6>
                    <button type="button" class="fp-panel-close" data-fp-panel-close title="Hide panel"
                            aria-label="Hide panel"><i class="fas fa-angles-left"></i></button>
                  </header>
                    <input type="file" id="fpImageFile" class="form-control form-control-sm mb-2"
                           accept=".png,.jpg,.jpeg,.pdf,image/png,image/jpeg,application/pdf">
                    <div class="d-flex gap-1 mb-2">
                      <button class="btn btn-sm btn-outline-danger flex-fill" id="fpRemoveImage" disabled><i class="fas fa-trash me-1"></i>Remove</button>
                    </div>
                    <label class="form-label small mb-0">Opacity</label>
                    <input type="range" id="fpImageOpacity" class="form-range" min="0.05" max="1" step="0.05" value="0.5">
                    <div class="form-check form-switch small mt-1">
                      <input class="form-check-input" type="checkbox" id="fpToggleBackground" checked>
                      <label class="form-check-label" for="fpToggleBackground">Show image</label>
                    </div>
                    <div id="fpBgAdjust" class="mt-2 d-none">
                      <button class="btn btn-sm btn-outline-warning w-100 mb-2" id="fpBgFit"
                              title="Scale and centre the image over the walls already drawn">
                        <i class="fas fa-expand me-1"></i>Fit image to walls</button>
                      <div class="row g-1">
                        <div class="col-6">
                          <label class="form-label small mb-0" for="fpBgWidth">Width (m)</label>
                          <input type="number" step="0.05" min="0.05" class="form-control form-control-sm py-0" id="fpBgWidth">
                        </div>
                        <div class="col-6">
                          <label class="form-label small mb-0" for="fpBgRot">Rotation (&deg;)</label>
                          <input type="number" step="0.5" class="form-control form-control-sm py-0" id="fpBgRot">
                        </div>
                        <div class="col-6">
                          <label class="form-label small mb-0" for="fpBgX">Left X (m)</label>
                          <input type="number" step="0.05" class="form-control form-control-sm py-0" id="fpBgX">
                        </div>
                        <div class="col-6">
                          <label class="form-label small mb-0" for="fpBgY">Bottom Y (m)</label>
                          <input type="number" step="0.05" class="form-control form-control-sm py-0" id="fpBgY">
                        </div>
                      </div>
                      <div class="form-text small mt-1">Scale <span id="fpBgPpm">&mdash;</span> px/m. Pick
                        <strong>Adjust image</strong> in Draw to drag the image, or its corners to resize;
                        arrow keys nudge.</div>
                    </div>
                </section>
                <section class="fp-panel" id="fpPanel-orient" data-fp-panel-body="orient" hidden>
                  <header class="fp-panel-head">
                    <h6>Orientation</h6>
                    <button type="button" class="fp-panel-close" data-fp-panel-close title="Hide panel"
                            aria-label="Hide panel"><i class="fas fa-angles-left"></i></button>
                  </header>
                    <div class="fp-panel-sub">Compass (North)</div>
                    <div id="fpCompass" class="position-relative" style="width:120px;height:120px;margin:0 auto"></div>
                    <div class="small text-center mt-1">
                      <input type="number" id="fpNorthDeg" class="form-control form-control-sm text-center" step="1" style="display:inline-block;width:80px"> °
                    </div>
                    <div class="fp-panel-sub mt-3">Map</div>
                    <div class="form-check form-switch small">
                      <input class="form-check-input" type="checkbox" id="fpToggleMap">
                      <label class="form-check-label" for="fpToggleMap">Show map under the plan</label>
                    </div>
                    <div id="fpMapControls" class="d-none">
                      <label class="form-label small mb-0" for="fpMapOpacity">Opacity</label>
                      <input type="range" id="fpMapOpacity" class="form-range" min="0.05" max="1" step="0.05" value="0.6">
                      <button class="btn btn-sm btn-outline-secondary w-100" id="fpMapAnchor">
                        <i class="fas fa-location-crosshairs me-1"></i>Mark where the home pin is</button>
                      <div class="form-text small">Turn the compass until the map's buildings line up with your walls.</div>
                    </div>
                    <div id="fpMapMissing" class="form-text small d-none">Set the home location in Settings to use the map.</div>
                </section>
              </div>
            </div>

            <!-- Centre: SVG canvas -->
            <div id="fpCanvasWrap" class="flex-grow-1 position-relative" style="overflow:hidden">
              <svg id="fpCanvas" style="width:100%;height:100%;display:block;cursor:crosshair">
                <defs>
                  <pattern id="fpGridFine" width="8" height="8" patternUnits="userSpaceOnUse">
                    <path class="fp-grid-line-fine" d="M 8 0 L 0 0 0 8" fill="none" stroke-width="0.5"/>
                  </pattern>
                  <pattern id="fpGridMinor" width="40" height="40" patternUnits="userSpaceOnUse">
                    <rect id="fpGridMinorFill" width="40" height="40" fill="url(#fpGridFine)"/>
                    <path class="fp-grid-line-minor" d="M 40 0 L 0 0 0 40" fill="none" stroke-width="0.5"/>
                  </pattern>
                  <pattern id="fpGridMajor" width="200" height="200" patternUnits="userSpaceOnUse">
                    <rect id="fpGridMajorFill" width="200" height="200" fill="url(#fpGridMinor)"/>
                    <path class="fp-grid-line-major" d="M 200 0 L 0 0 0 200" fill="none" stroke-width="1"/>
                  </pattern>
                </defs>
                <g id="fpGrid">
                  <rect id="fpGridRect" width="100%" height="100%" fill="url(#fpGridMajor)"/>
                  <line id="fpAxisX" class="fp-axis" x1="-100000" x2="100000" y1="0" y2="0"/>
                  <line id="fpAxisY" class="fp-axis" x1="0" x2="0" y1="-100000" y2="100000"/>
                </g>
                <g id="fpScene"></g>
                <g id="fpOverlay"></g>
              </svg>
              <div id="fpZoomCtl" role="group" aria-label="Zoom">
                <button type="button" id="fpZoomIn" title="Zoom in" aria-label="Zoom in"><i class="fas fa-plus"></i></button>
                <button type="button" id="fpZoomFit" title="Fit the plan" aria-label="Fit the plan"><i class="fas fa-expand"></i></button>
                <button type="button" id="fpZoomOut" title="Zoom out" aria-label="Zoom out"><i class="fas fa-minus"></i></button>
              </div>
              <div id="fpScaleBar"><span id="fpScaleBarRule"></span><span id="fpScaleBarLabel"></span></div>
              <div id="fpLegend" style="display:none"></div>
            </div>

            <!-- Right: properties pane -->
            <div id="fpProps" class="border-start" style="width:300px;min-width:300px;overflow:auto;padding:12px">
              <div class="text-muted small">Select something to edit its properties.</div>
            </div>
          </div>
          <div class="modal-footer py-2">
            <button type="button" class="btn btn-outline-warning btn-sm me-2" id="fpSwitchMode" data-fp-view="heating"
                    title="Switch the heating controller back to manual configuration">
              <i class="fas fa-list-ul me-1"></i>Switch to manual
            </button>
            <div id="fpSaveStatus" class="me-auto small text-muted"></div>
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal" data-fp-host="modal">Cancel</button>
            <button type="button" class="btn btn-outline-secondary" id="fpRevert" data-fp-host="inline"
                    title="Discard unsaved changes"><i class="fas fa-rotate-left me-1"></i>Revert</button>
            <button type="button" class="btn btn-primary" id="fpSave"><i class="fas fa-save me-1"></i>Save plan</button>
          </div>`;
}

/** Phone-only off-canvas drawers — see the @media block in floor-plan.css. */
function setMobileDrawer(which, open) {
    const modal = _root;
    if (!modal) return;
    const cls = which === 'sidebar' ? 'fp-sidebar-open' : 'fp-props-open';
    const other = which === 'sidebar' ? 'fp-props-open' : 'fp-sidebar-open';
    modal.classList.remove(other);   // only one drawer open at a time
    modal.classList.toggle(cls, open);
}

function closeMobileDrawers() {
    _root?.classList.remove('fp-sidebar-open', 'fp-props-open');
}

function bindModalEvents() {
    document.getElementById('fpSave').addEventListener('click', save);
    document.getElementById('fpSwitchMode')?.addEventListener('click', switchToManual);
    document.getElementById('fpAddLevel').addEventListener('click', addLevel);
    document.getElementById('fpAddCircuit').addEventListener('click', addCircuit);
    _root.querySelectorAll('.fp-rail-btn').forEach(b => {
        // A second click on the open panel's button hides it.
        b.addEventListener('click', () => showPanel(b.classList.contains('active') ? '' : b.dataset.fpPanel));
    });
    _root.querySelectorAll('[data-fp-panel-close]').forEach(b => b.addEventListener('click', () => showPanel('')));
    document.querySelectorAll('#fpToolbar [data-tool]').forEach(b => {
        b.addEventListener('click', () => {
            setTool(b.dataset.tool);
            // The image's position and size boxes live in the Image panel.
            if (b.dataset.tool === 'bg') showPanel('image');
            // On a phone the tools drawer covers the canvas — picking a tool
            // means "I'm about to draw", so get out of the way automatically.
            closeMobileDrawers();
        });
    });

    // Phone-only off-canvas drawers (see the @media block in floor-plan.css).
    // These buttons are hidden at desktop widths, so no harm binding always.
    document.getElementById('fpToggleSidebarBtn')?.addEventListener('click', () => {
        const open = !_root.classList.contains('fp-sidebar-open');
        setMobileDrawer('sidebar', open);
    });
    document.getElementById('fpTogglePropsBtn')?.addEventListener('click', () => {
        const open = !_root.classList.contains('fp-props-open');
        setMobileDrawer('props', open);
    });
    // Tapping the dimmed canvas while a drawer is open closes it. The CSS
    // scrim (::after on #fpCanvasWrap, see floor-plan.css) is a real painted
    // layer above the svg, so this is the only element the tap can actually
    // hit — the svg's own mousedown/touchstart never see it.
    document.getElementById('fpCanvasWrap')?.addEventListener('click', () => {
        closeMobileDrawers();
    });
    document.getElementById('fpToggleGrid').addEventListener('change', e => {
        _state.showGrid = e.target.checked;
        document.getElementById('fpGrid').style.display = _state.showGrid ? '' : 'none';
    });
    document.getElementById('fpSnapStep').addEventListener('change', e => {
        _state.snapStep = parseFloat(e.target.value) || 0;
        renderGrid();
    });
    document.getElementById('fpAngleStep').addEventListener('change', e => {
        _state.angleStep = parseFloat(e.target.value) || 1;
    });
    document.getElementById('fpToggleSun').addEventListener('change', async e => {
        _state.showSun = e.target.checked;
        if (_state.showSun) {
            await loadSunData(); await loadSolarImpact();
            if (!_state.daylight) await loadDaylight();   // the light field under the sun path
        }
        renderScene(); renderOverlay(); renderProps();
        // The arc is wider than the house, so make room for it (and give it
        // back when it goes away).
        zoomFit();
    });
    document.getElementById('fpToggleThermal').addEventListener('change', e => {
        _state.showThermal = e.target.checked;
        const contoursEl = document.getElementById('fpToggleContours');
        const coldEl = document.getElementById('fpToggleColdZones');
        const modeEl = document.getElementById('fpThermalMode');
        contoursEl.disabled = !_state.showThermal;
        coldEl.disabled = !_state.showThermal;
        modeEl.disabled = !_state.showThermal;
        if (!_state.showThermal) {
            _state.showContours = false; contoursEl.checked = false;
            _state.showColdZones = false; coldEl.checked = false;
            document.getElementById('fpColdZoneControls').style.display = 'none';
        }
        renderScene();
    });
    document.getElementById('fpThermalMode').addEventListener('change', async e => {
        _state.thermalMode = e.target.value;
        // Solar modes need the day's sun curve + any measured solar impact
        if (_state.thermalMode !== 'rad') {
            if (!_state.sunData) await loadSunData();
            await loadSolarImpact();
        }
        renderScene(); renderProps();
    });
    document.getElementById('fpToggleContours').addEventListener('change', e => {
        _state.showContours = e.target.checked;
        renderScene();
    });
    document.getElementById('fpToggleColdZones').addEventListener('change', e => {
        _state.showColdZones = e.target.checked;
        document.getElementById('fpColdZoneControls').style.display = _state.showColdZones ? '' : 'none';
        renderScene();
    });
    document.getElementById('fpHeatFlux').addEventListener('change', e => {
        _state.heatFluxWm2 = parseInt(e.target.value, 10) || 50;
        renderScene();
    });
    document.getElementById('fpZoomIn').addEventListener('click', () => zoomBy(1.25));
    document.getElementById('fpZoomOut').addEventListener('click', () => zoomBy(0.8));
    document.getElementById('fpZoomFit').addEventListener('click', zoomFit);
    document.getElementById('fpNorthDeg').addEventListener('change', e => {
        const v = parseFloat(e.target.value);
        if (Number.isFinite(v)) {
            _state.plan.north_offset_deg = ((v % 360) + 360) % 360;
            renderCompass();
            renderProps();
        }
    });

    bindDeviceLayerEvents();

    document.getElementById('fpImageFile').addEventListener('change', onImageFileChosen);
    document.getElementById('fpRemoveImage').addEventListener('click', removeBackgroundImage);
    document.getElementById('fpBgFit').addEventListener('click', () => {
        if (fitBackgroundToDrawing()) { renderScene(); renderOverlay(); syncBackgroundControls(); }
    });
    // Typed placement. Width grows the image about its centre, so the number
    // boxes and the corner grips agree about what "resize" means.
    const bgNumber = (id, apply) =>
        document.getElementById(id).addEventListener('change', e => {
            const bg = currentLevel()?.background;
            const v = parseFloat(e.target.value);
            if (!bg?.present || !Number.isFinite(v)) { syncBackgroundControls(); return; }
            apply(bg, v);
            renderScene(); renderOverlay(); syncBackgroundControls();
        });
    bgNumber('fpBgWidth', (bg, v) => {
        if (v > 0) bgResizeAnchored(bg, v, 0.5, 0.5, bgPoint(bg, 0.5, 0.5));
    });
    bgNumber('fpBgX', (bg, v) => { bg.origin_x_m = round3(v); });
    bgNumber('fpBgY', (bg, v) => { bg.origin_y_m = round3(v); });
    bgNumber('fpBgRot', (bg, v) => { bg.rotation_deg = round3(((v % 360) + 360) % 360); });
    document.getElementById('fpImageOpacity').addEventListener('input', e => {
        const lvl = currentLevel();
        if (!lvl.background?.present) return;
        lvl.background.opacity = parseFloat(e.target.value);
        renderScene();
    });
    document.getElementById('fpToggleBackground').addEventListener('change', e => {
        _state.showBackground = e.target.checked;
        renderScene();
    });

    const svg = document.getElementById('fpCanvas');
    svg.addEventListener('mousedown', onCanvasMouseDown);
    svg.addEventListener('mousemove', onCanvasMouseMove);
    svg.addEventListener('mouseup', onCanvasMouseUp);
    svg.addEventListener('wheel', onCanvasWheel, { passive: false });
    svg.addEventListener('touchstart',  onCanvasTouchStart,  { passive: false });
    svg.addEventListener('touchmove',   onCanvasTouchMove,   { passive: false });
    svg.addEventListener('touchend',    onCanvasTouchEnd,    { passive: false });
    svg.addEventListener('touchcancel', onCanvasTouchEnd,    { passive: false });
    // Right-click finishes a wall or room chain (or just suppresses the
    // browser context menu when nothing is in progress).
    svg.addEventListener('contextmenu', e => {
        e.preventDefault();
        if (_state?.tool === 'wall' && _state.drawBuffer?.points?.length >= 2) {
            finishWallChain();
        } else if (_state?.tool === 'room' && _state.drawBuffer?.points?.length >= 3) {
            finishRoom();
        }
    });
    // Double-click also finishes chains. Useful when the last vertex isn't
    // close enough to the first for a single click to close.
    svg.addEventListener('dblclick', e => {
        if (_state?.tool === 'wall' && _state.drawBuffer?.points?.length >= 2) {
            e.preventDefault();
            finishWallChain();
        } else if (_state?.tool === 'room' && _state.drawBuffer?.points?.length >= 3) {
            e.preventDefault();
            finishRoom();
        }
    });

    // Keyboard: Enter / Esc handle in-progress chains across the whole modal
    // so the user doesn't have to keep focus on the canvas.
    const onKey = (e) => {
        // Bound once for the page; only acts while the editor is on screen.
        if (!_state || !_root || _root.offsetParent === null) return;
        // Alt suppresses snapping while held (tracked before the form-field
        // guard so it works regardless of focus).
        if (e.key === 'Alt') _altDown = true;
        // Ignore when typing in a form field
        const tag = (e.target?.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
        if (e.key === 'Enter') {
            if (_state.tool === 'wall' && _state.drawBuffer?.points?.length >= 2) {
                e.preventDefault(); finishWallChain();
            } else if (_state.tool === 'room' && _state.drawBuffer?.points?.length >= 3) {
                e.preventDefault(); finishRoom();
            }
        } else if (e.key === 'Escape') {
            if (_state.drawBuffer) { e.preventDefault(); cancelDrawing(); }
            else if (_state.calibration) { e.preventDefault(); _state.calibration = null; renderOverlay(); }
        } else if (_state.tool === 'bg' && e.key.startsWith('Arrow')) {
            // Nudge the image by one snap step (ten with Shift) so it can be
            // lined up on a wall more finely than a drag allows.
            const bg = currentLevel()?.background;
            if (bg?.present) {
                e.preventDefault();
                const step = (_state.snapStep || 0.1) * (e.shiftKey ? 10 : 1);
                if (e.key === 'ArrowLeft')  bg.origin_x_m = round3(bg.origin_x_m - step);
                if (e.key === 'ArrowRight') bg.origin_x_m = round3(bg.origin_x_m + step);
                if (e.key === 'ArrowDown')  bg.origin_y_m = round3(bg.origin_y_m - step);
                if (e.key === 'ArrowUp')    bg.origin_y_m = round3(bg.origin_y_m + step);
                renderScene(); renderOverlay(); syncBackgroundControls();
            }
        } else if (e.key === 'Backspace' || e.key === 'Delete') {
            // Backspace removes the last placed vertex of the current chain
            if ((_state.tool === 'wall' || _state.tool === 'room')
                && _state.drawBuffer?.points?.length >= 1) {
                e.preventDefault();
                _state.drawBuffer.points.pop();
                if (_state.drawBuffer.points.length === 0) _state.drawBuffer = null;
                renderOverlay();
            }
        }
    };
    document.addEventListener('keydown', onKey);
    // Alt release / window blur ends snap suppression. preventDefault on the
    // keyup stops Firefox focusing its menu bar on a bare Alt press.
    const onKeyUp = (e) => { if (e.key === 'Alt') { _altDown = false; e.preventDefault(); } };
    const onBlur = () => { _altDown = false; };
    document.addEventListener('keyup', onKeyUp);
    window.addEventListener('blur', onBlur);

    document.getElementById('fpRevert').addEventListener('click', () => {
        if (_inlineHost) showFloorPlanInline(_inlineHost);
    });
}

// render top-level

function renderAll() {
    renderPalette();
    syncMapControls();
    document.getElementById('fpToggleDaylight').checked = !!_state.showDaylight;
    syncDaylightControls();
    document.getElementById('fpToggleMesh').checked = !!_state.showMesh;
    syncMeshControls();
    document.getElementById('fpToggleCoverage').checked = !!_state.showCoverage;
    syncCoverageControls();
    renderLevelList();
    renderCircuitList();
    renderToolbar();
    renderCompass();
    syncViewToggles();
    renderScene();
    renderOverlay();
    renderProps();
    document.getElementById('fpNorthDeg').value = Math.round(_state.plan.north_offset_deg);
    syncBackgroundControls();
    requestAnimationFrame(zoomFit);
}

// Sync sidebar checkbox/toggle state to _state so the UI always reflects the
// actual render state (fixes stale DOM after modal close/reopen).
function syncViewToggles() {
    const set = (id, checked, disabled = false) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.checked = checked;
        el.disabled = disabled;
    };
    set('fpToggleGrid',       _state.showGrid);
    set('fpToggleBackground', _state.showBackground);
    set('fpToggleSun',        _state.showSun);
    set('fpToggleThermal',    _state.showThermal);
    set('fpToggleContours',   _state.showContours,  !_state.showThermal);
    set('fpToggleColdZones',  _state.showColdZones, !_state.showThermal);

    const modeEl = document.getElementById('fpThermalMode');
    if (modeEl) { modeEl.value = _state.thermalMode; modeEl.disabled = !_state.showThermal; }

    // Cold zone sub-controls
    const czCtrl = document.getElementById('fpColdZoneControls');
    if (czCtrl) czCtrl.style.display = _state.showColdZones ? '' : 'none';
    const hfEl = document.getElementById('fpHeatFlux');
    if (hfEl) hfEl.value = String(_state.heatFluxWm2 || 50);

    // Keep grid visibility in sync with state
    const grid = document.getElementById('fpGrid');
    if (grid) grid.style.display = _state.showGrid ? '' : 'none';

    const snapEl = document.getElementById('fpSnapStep');
    if (snapEl) snapEl.value = String(_state.snapStep);
    const angleEl = document.getElementById('fpAngleStep');
    if (angleEl) angleEl.value = String(_state.angleStep);
}

function syncBackgroundControls() {
    const lvl = currentLevel();
    const removeBtn = document.getElementById('fpRemoveImage');
    const opSlider = document.getElementById('fpImageOpacity');
    if (!removeBtn || !opSlider) return;
    const bg = lvl?.background?.present ? lvl.background : null;
    removeBtn.disabled = !bg;
    const adjust = document.getElementById('fpBgAdjust');
    if (adjust) adjust.classList.toggle('d-none', !bg);
    if (!bg) return;
    opSlider.value = bg.opacity ?? 0.5;
    const g = bgGeom(bg);
    // Don't fight the user mid-edit: leave the box they are typing in alone.
    const set = (id, v) => {
        const el = document.getElementById(id);
        if (el && document.activeElement !== el) el.value = v;
    };
    set('fpBgWidth', g.wM.toFixed(2));
    set('fpBgX', bg.origin_x_m.toFixed(2));
    set('fpBgY', bg.origin_y_m.toFixed(2));
    set('fpBgRot', (bg.rotation_deg || 0).toFixed(1));
    const ppm = document.getElementById('fpBgPpm');
    if (ppm) ppm.textContent = bg.pixels_per_metre.toFixed(1);
}

function renderLevelList() {
    const wrap = document.getElementById('fpLevelList');
    wrap.innerHTML = _state.plan.levels
        .slice()
        .sort((a, b) => b.index - a.index)
        .map(l => {
            const active = l.id === _state.currentLevelId ? 'active' : '';
            return `
              <button type="button" class="list-group-item list-group-item-action py-1 ${active}" data-level-id="${l.id}">
                <div class="d-flex justify-content-between align-items-center">
                  <span>${escapeHtml(l.name)}</span>
                  <small class="text-muted">L${l.index}</small>
                </div>
              </button>`;
        }).join('');
    wrap.querySelectorAll('[data-level-id]').forEach(el => {
        el.addEventListener('click', () => {
            _state.currentLevelId = el.dataset.levelId;
            _state.selection = null;
            renderAll();
        });
    });
}

function renderToolbar() {
    document.querySelectorAll('#fpToolbar [data-tool]').forEach(b => {
        b.classList.toggle('active', b.dataset.tool === _state.tool);
    });
    const cursors = {
        select: 'default', wall: 'crosshair', room: 'crosshair',
        window: 'crosshair', door: 'crosshair', radiator: 'crosshair',
        sensor: 'crosshair', contact: 'crosshair', calibrate: 'crosshair',
        place: 'copy', 'map-anchor': 'crosshair', bg: 'move',
    };
    document.getElementById('fpCanvas').style.cursor = cursors[_state.tool] || 'default';
}

function renderCompass() {
    const el = document.getElementById('fpCompass');
    if (!el) return;
    const deg = _state.plan.north_offset_deg;
    el.innerHTML = `
      <svg width="120" height="120" viewBox="-60 -60 120 120" style="cursor:grab">
        <circle class="fp-compass-ring" cx="0" cy="0" r="55" fill="none"/>
        <text class="fp-compass-label" x="0" y="-44" text-anchor="middle" font-size="10">N</text>
        <text class="fp-compass-label" x="44" y="3"   text-anchor="middle" font-size="10">E</text>
        <text class="fp-compass-label" x="0" y="50"   text-anchor="middle" font-size="10">S</text>
        <text class="fp-compass-label" x="-44" y="3"  text-anchor="middle" font-size="10">W</text>
        <g id="fpCompassNeedle" transform="rotate(${deg})">
          <polygon class="fp-compass-needle" points="0,-50 -8,0 0,-12 8,0"/>
          <polygon class="fp-compass-tail"   points="0,50 -8,0 0,12 8,0"/>
        </g>
      </svg>`;
    const svg = el.querySelector('svg');
    let dragging = false;
    const update = (cx, cy) => {
        const rect = svg.getBoundingClientRect();
        const x = cx - rect.left - rect.width / 2;
        const y = cy - rect.top  - rect.height / 2;
        const a = (Math.atan2(x, -y) * 180 / Math.PI + 360) % 360;
        _state.plan.north_offset_deg = Math.round(a);
        document.getElementById('fpCompassNeedle').setAttribute('transform', `rotate(${a})`);
        document.getElementById('fpNorthDeg').value = Math.round(a);
    };
    svg.addEventListener('mousedown', e => { dragging = true; update(e.clientX, e.clientY); });
    window.addEventListener('mousemove', e => { if (dragging) update(e.clientX, e.clientY); });
    window.addEventListener('mouseup', () => { dragging = false; });
}

// coordinates

// SVG y-axis goes DOWN. Model y-axis goes UP. Convert at the boundary.
function modelToSvg(p)  { return { x: p.x,  y: -p.y }; }
function svgToModel(p)  { return { x: p.x,  y: -p.y }; }

// background image placement
//
// `origin_{x,y}_m` is the BOTTOM-LEFT corner of the unrotated image in model
// coordinates (+y up), `pixels_per_metre` its scale, and `rotation_deg` turns
// it anti-clockwise about its own centre. Everything that moves, scales or
// rotates the image goes through these four helpers so the render, the drag
// handles, the number boxes and Calibrate can never disagree.

/** Size in metres, centre and rotation of a background block. */
function bgGeom(bg) {
    const wM = bg.image_width_px / bg.pixels_per_metre;
    const hM = bg.image_height_px / bg.pixels_per_metre;
    return {
        wM, hM,
        cx: bg.origin_x_m + wM / 2,
        cy: bg.origin_y_m + hM / 2,
        phi: (bg.rotation_deg || 0) * Math.PI / 180,
    };
}

/** Model point of the image's (u across, v down from the top) 0..1 coord. */
function bgPoint(bg, u, v) {
    const g = bgGeom(bg);
    const ex = (u - 0.5) * g.wM, ey = (0.5 - v) * g.hM;
    const c = Math.cos(g.phi), sn = Math.sin(g.phi);
    return { x: g.cx + ex * c - ey * sn, y: g.cy + ex * sn + ey * c };
}

/** Inverse of bgPoint: where a model point falls in the image (0..1 inside). */
function bgFrac(bg, p) {
    const g = bgGeom(bg);
    const c = Math.cos(-g.phi), sn = Math.sin(-g.phi);
    const dx = p.x - g.cx, dy = p.y - g.cy;
    return { u: (dx * c - dy * sn) / g.wM + 0.5, v: 0.5 - (dx * sn + dy * c) / g.hM };
}

/** A model-space vector turned into the image's own (unrotated) frame. */
function bgVecToImage(bg, v) {
    const phi = -(bg.rotation_deg || 0) * Math.PI / 180;
    const c = Math.cos(phi), sn = Math.sin(phi);
    return { x: v.x * c - v.y * sn, y: v.x * sn + v.y * c };
}

/**
 * Resize the image to `wNewM` metres wide — aspect ratio kept — while its
 * (uA, vA) corner stays pinned to the model point `anchor`. Rotation is left
 * alone. Width is clamped to the px/m range the backend will accept.
 */
function bgResizeAnchored(bg, wNewM, uA, vA, anchor) {
    const wPx = bg.image_width_px;
    wNewM = Math.max(wPx / 10000, Math.min(wPx / 1, wNewM));
    const hNewM = wNewM * (bg.image_height_px / wPx);
    const phi = (bg.rotation_deg || 0) * Math.PI / 180;
    const ex = (uA - 0.5) * wNewM, ey = (0.5 - vA) * hNewM;
    const c = Math.cos(phi), sn = Math.sin(phi);
    bg.pixels_per_metre = round3(wPx / wNewM);
    bg.origin_x_m = round3(anchor.x - (ex * c - ey * sn) - wNewM / 2);
    bg.origin_y_m = round3(anchor.y - (ex * sn + ey * c) - hNewM / 2);
}

/** Bounding box of what has been drawn on a level, or null if nothing has. */
function drawingBounds(lvl) {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    const consider = (x, y) => {
        if (x < minX) minX = x; if (x > maxX) maxX = x;
        if (y < minY) minY = y; if (y > maxY) maxY = y;
    };
    for (const w of lvl.walls || []) { consider(w.x1, w.y1); consider(w.x2, w.y2); }
    for (const r of lvl.rooms || []) for (const q of r.polygon || []) consider(q[0], q[1]);
    return Number.isFinite(minX) ? { minX, minY, maxX, maxY } : null;
}

/**
 * Scale and centre the image over the walls and rooms already drawn, so a
 * plan that has drifted out of scale lands back on its tracing in one click.
 * The image is sized to *contain* the drawing, so nothing drawn falls off it.
 * Returns false (and says nothing, when `silent`) if there is nothing to fit.
 */
function fitBackgroundToDrawing({ silent = false, level = null } = {}) {
    const lvl = level || currentLevel();
    const bg = lvl?.background;
    if (!bg?.present) {
        if (!silent) toast('warn', 'No image', 'Import a background image first.');
        return false;
    }
    const b = drawingBounds(lvl);
    if (!b) {
        if (!silent) toast('warn', 'Nothing to fit to', 'Draw some walls or rooms first.');
        return false;
    }
    const wDraw = Math.max(b.maxX - b.minX, 0.5);
    const hDraw = Math.max(b.maxY - b.minY, 0.5);
    const aspect = bg.image_height_px / bg.image_width_px;
    // Contain: wide enough for the drawing's width, and tall enough for its
    // height once the image's own aspect ratio is applied.
    const wM = Math.max(wDraw, hDraw / aspect);
    bgResizeAnchored(bg, wM, 0.5, 0.5,
                     { x: (b.minX + b.maxX) / 2, y: (b.minY + b.maxY) / 2 });
    if (!silent) {
        toast('success', 'Image fitted',
              `${wM.toFixed(2)} m wide at ${bg.pixels_per_metre.toFixed(1)} px/m — `
              + 'drag it or its corners to line it up, then Save plan.');
    }
    return true;
}

// Returns the model-space centre point of a radiator (wall-mounted or freestanding).
function radiatorCenter(r, lvl) {
    const wall = r.wall_id ? lvl.walls.find(w => w.id === r.wall_id) : null;
    if (wall) {
        const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) || 1;
        const ux = (wall.x2 - wall.x1) / wlen, uy = (wall.y2 - wall.y1) / wlen;
        const len = r.length_m || 0.6;
        const t0 = Math.max(0, Math.min(wlen - len, (r.offset_m ?? wlen / 2)));
        return { x: wall.x1 + ux * (t0 + len / 2), y: wall.y1 + uy * (t0 + len / 2) };
    }
    return { x: r.x ?? 0, y: r.y ?? 0 };
}

// Centroid of the whole plan — average of room centroids, falling back to wall midpoints.
/**
 * How far the level's geometry reaches from `origin`, in metres — the radius
 * of a circle that contains the building.
 */
function planReach(lvl, origin) {
    let far = 0;
    const see = (x, y) => { far = Math.max(far, Math.hypot(x - origin.x, y - origin.y)); };
    for (const r of lvl.rooms || []) for (const [x, y] of r.polygon || []) see(x, y);
    for (const w of lvl.walls || []) { see(w.x1, w.y1); see(w.x2, w.y2); }
    return far;
}

/**
 * Where the sun arc sits for this level: outside the building, and growing
 * with it. A fixed radius ends up indoors as soon as the plan is bigger.
 * `min` is how far in the midday pull-in may come — the building line.
 */
function sunArc(lvl) {
    const origin = planCentroid(lvl);
    const reach = planReach(lvl, origin);
    const clear = Math.max(1.5, reach * 0.18);
    return { origin, clear, r: Math.max(7, reach * 1.6 + clear), min: reach + clear };
}

function planCentroid(lvl) {
    const rooms = lvl.rooms || [];
    if (rooms.length > 0) {
        let cx = 0, cy = 0;
        for (const r of rooms) { const c = polygonCentroid(r.polygon); cx += c.x; cy += c.y; }
        return { x: cx / rooms.length, y: cy / rooms.length };
    }
    const walls = lvl.walls || [];
    if (walls.length > 0) {
        const cx = walls.reduce((s, w) => s + (w.x1 + w.x2) / 2, 0) / walls.length;
        const cy = walls.reduce((s, w) => s + (w.y1 + w.y2) / 2, 0) / walls.length;
        return { x: cx, y: cy };
    }
    return { x: 0, y: 0 };
}

// Compute which rooms receive direct solar gain through their windows today.
// Returns a Map<room.id, sunMinutes>.
function computeSolarGain(lvl, sunData, northOffsetDeg) {
    const result = new Map();
    if (!sunData || !sunData.points) return result;
    const stepMin = sunData.step_minutes || 20;
    const daytime = sunData.points.filter(pt => pt.el > 0);
    if (daytime.length === 0) return result;

    for (const opening of (lvl.openings || [])) {
        if (opening.kind !== 'window') continue;
        const wall = (lvl.walls || []).find(w => w.id === opening.wall_id);
        if (!wall) continue;

        const dx = wall.x2 - wall.x1, dy = wall.y2 - wall.y1;
        const wlen = Math.hypot(dx, dy) || 1;
        const ux = dx / wlen, uy = dy / wlen;
        const nx = -uy, ny = ux; // one perpendicular

        // Window midpoint in model space
        const wmx = wall.x1 + ux * (opening.offset_m + opening.width_m / 2);
        const wmy = wall.y1 + uy * (opening.offset_m + opening.width_m / 2);

        // Count sun-minutes for each face of this wall
        let minN = 0, minNeg = 0;
        for (const pt of daytime) {
            const planAz = ((pt.az + northOffsetDeg) % 360 + 360) % 360;
            const sx = Math.sin(planAz * Math.PI / 180);
            const sy = Math.cos(planAz * Math.PI / 180);
            if (sx * nx + sy * ny > 0.15) minN += stepMin;
            else if (sx * (-nx) + sy * (-ny) > 0.15) minNeg += stepMin;
        }
        if (minN === 0 && minNeg === 0) continue;

        for (const room of (lvl.rooms || [])) {
            if (!room.polygon || room.polygon.length < 3) continue;

            // Check window midpoint is on a polygon edge (within 0.3 m tolerance)
            let onBoundary = false;
            for (let i = 0; i < room.polygon.length; i++) {
                const a = room.polygon[i], b = room.polygon[(i + 1) % room.polygon.length];
                const ex = b[0] - a[0], ey = b[1] - a[1];
                const elen2 = ex * ex + ey * ey || 1;
                const t = Math.max(0, Math.min(1, ((wmx - a[0]) * ex + (wmy - a[1]) * ey) / elen2));
                if (Math.hypot(wmx - (a[0] + t * ex), wmy - (a[1] + t * ey)) < 0.3) {
                    onBoundary = true; break;
                }
            }
            if (!onBoundary) continue;

            // Determine which side of the wall the room is on
            const centroid = polygonCentroid(room.polygon);
            const dot = (centroid.x - wmx) * nx + (centroid.y - wmy) * ny;
            // Room is on +n side → exterior face is -n → sun enters via minNeg
            const sunMin = dot > 0 ? minNeg : minN;
            if (sunMin > 0) result.set(room.id, (result.get(room.id) || 0) + sunMin);
        }
    }
    return result;
}

// Resolve the live open/closed state of a contact sensor bound to an opening.
// Returns true (open), false (closed), or null (no sensor / unknown).
function openingContactState(opening, lvl) {
    const contact = (lvl.contacts || []).find(c => c.opening_id === opening.id);
    if (!contact) return null;
    const dev = (_availableDevices.contacts || []).find(d => d.ieee === contact.ieee);
    if (!dev) return null;
    if (typeof dev.is_open   === 'boolean') return dev.is_open;
    if (typeof dev.contact   === 'boolean') return !dev.contact; // zigbee2mqtt: contact=true → closed
    return null;
}

function clientToSvg(evt) {
    const svg = document.getElementById('fpCanvas');
    const pt = svg.createSVGPoint();
    pt.x = evt.clientX; pt.y = evt.clientY;
    const ctm = document.getElementById('fpScene').getScreenCTM();
    if (!ctm) return { x: 0, y: 0 };
    const m = pt.matrixTransform(ctm.inverse());
    return { x: m.x, y: m.y };
}

function snap(v) {
    const s = _altDown ? 0 : (_state?.snapStep ?? DEFAULT_SNAP_M);
    return s > 0 ? Math.round(v / s) * s : v;
}
function snapPt(p) { return { x: snap(p.x), y: snap(p.y) }; }

// Endpoint-merge radius in model metres: a fixed ~14 px on screen, so zooming
// in shrinks it and lets the user place vertices close together without them
// being merged. This is what makes small gaps drawable.
function snapRadiusM() {
    return _altDown ? 0 : Math.min(0.3, 14 / (_state?.zoom || PIXELS_PER_METRE_DEFAULT));
}

// Chain-close click radius — slightly larger than the merge radius.
function closeRadiusM() {
    return Math.max(0.12, 18 / (_state?.zoom || PIXELS_PER_METRE_DEFAULT));
}

// scene render

function renderScene() {
    const lvl = currentLevel();
    if (!lvl) return;
    renderGrid();
    const scene = document.getElementById('fpScene');
    const m2px = _state.zoom;
    scene.setAttribute('transform',
        `translate(${_state.pan.x}, ${_state.pan.y}) scale(${m2px}, ${m2px})`);

    const parts = [];

    if (_state.showMap) parts.push(...renderMapParts());

    // Background image (drawn under everything but the map)
    if (_state.showBackground && lvl.background?.present) {
        const bg = lvl.background;
        const { wM, hM, cx, cy } = bgGeom(bg);
        // SVG <image> draws downward from its (x,y), and the model is +y up, so
        // the image's top edge in model space is origin_y + hM. The rotation is
        // anti-clockwise in model space, which is a negative SVG rotation.
        const tlSvg = modelToSvg({ x: bg.origin_x_m, y: bg.origin_y_m + hM });
        const cSvg = modelToSvg({ x: cx, y: cy });
        const bgTransform = `rotate(${-(bg.rotation_deg || 0)} ${cSvg.x} ${cSvg.y})`;
        parts.push(`
          <image href="/api/floor-plan/image/${escapeAttr(lvl.id)}?t=${bg._cb || 0}"
                 x="${tlSvg.x}" y="${tlSvg.y}" width="${wM}" height="${hM}"
                 opacity="${bg.opacity}" preserveAspectRatio="none"
                 transform="${bgTransform}"
                 pointer-events="none"/>
          <rect class="fp-bg-frame" x="${tlSvg.x}" y="${tlSvg.y}" width="${wM}" height="${hM}"
                transform="${bgTransform}" pointer-events="none"/>`);
    }

    // Thermal overlay — a per-room heat-coverage field (see the thermal field
    // engine section) rendered as a smooth bitmap heat-map, with true marching-
    // squares isotherm contours and live thermostat readings on sensors.
    if (_state.showThermal) {
        parts.push(...renderThermalParts(lvl));
    }

    if (_state.showCoverage && _state.coverage) parts.push(...renderCoverageParts(lvl));

    // Rooms (under shapes but over the image)
    // Build circuit colour palette for rooms
    const CIRCUIT_COLOURS = [
        'rgba(59,130,246,0.18)',   // blue
        'rgba(16,185,129,0.18)',   // green
        'rgba(245,158,11,0.18)',   // amber
        'rgba(239,68,68,0.18)',    // red
        'rgba(139,92,246,0.18)',   // violet
        'rgba(236,72,153,0.18)',   // pink
        'rgba(20,184,166,0.18)',   // teal
        'rgba(249,115,22,0.18)',   // orange
    ];
    const circuitColourMap = {};
    (_state.plan.circuits || []).forEach((c, i) => {
        circuitColourMap[c.id] = CIRCUIT_COLOURS[i % CIRCUIT_COLOURS.length];
    });

    for (const r of lvl.rooms) {
        const sel = isSelected('room', r.id);
        const path = polygonToPath(r.polygon);
        const fillColour = r.circuit_id && circuitColourMap[r.circuit_id]
            ? circuitColourMap[r.circuit_id]
            : 'rgba(100,116,139,0.08)';
        parts.push(`
          <path class="fp-room ${sel ? 'fp-selected' : ''}" d="${path}"
                fill="${fillColour}"
                stroke-width="${sel ? 0.05 : 0.025}" stroke-dasharray="0.1 0.1"
                data-kind="room" data-id="${r.id}" pointer-events="visiblePainted"/>`);
        const c = polygonCentroid(r.polygon);
        const sc = modelToSvg(c);
        const circuitName = r.circuit_id
            ? ((_state.plan.circuits || []).find(x => x.id === r.circuit_id)?.name || r.circuit_id)
            : null;
        parts.push(`<text class="fp-room-label" x="${sc.x}" y="${sc.y}" font-size="0.18" text-anchor="middle"
                    pointer-events="none">${escapeHtml(r.name || r.id)}</text>`);
        if (circuitName) {
            parts.push(`<text class="fp-room-label" x="${sc.x}" y="${sc.y + 0.22}" font-size="0.13" text-anchor="middle"
                        fill="#64748b" pointer-events="none">${escapeHtml(circuitName)}</text>`);
        }
    }

    if (_state.showDaylight && _state.daylight) parts.push(...renderDaylightParts(lvl));

    // Sun path: each room's light field as it is now (docs/daylight.md §8),
    // and how long the sun is on its windows today. The Daylight layer draws
    // the field itself when it is on, at its own slider time.
    if (_state.showSun && _state.sunData) {
        const solarGain = computeSolarGain(lvl, _state.sunData, _state.plan.north_offset_deg);
        const d = _state.daylight;
        const now = d ? daylightNowIndex(d) : -1;
        const byRoom = new Map((d?.rooms || []).filter(r => r.level_id === lvl.id).map(r => [r.room_id, r]));
        const maxMin = solarGain.size ? Math.max(...solarGain.values()) : 1;
        const defs = [];
        for (const room of lvl.rooms) {
            const minutes = solarGain.get(room.id);
            const est = byRoom.get(room.id);
            let drawn = !!_state.showDaylight;
            if (!drawn && est && d.sky?.[now]) {
                const lf = roomLightFieldParts(room, lvl, d.sky[now], est.lux[now] || 0);
                if (lf) { defs.push(...lf.defs); parts.push(...lf.parts); drawn = true; }
            }
            if (!minutes) continue;
            const sc = modelToSvg(polygonCentroid(room.polygon));
            if (!drawn) {
                // No estimate for this room (unsaved window, no location): the plain tint.
                const fillOpacity = (0.12 + 0.22 * Math.min(1, minutes / maxMin)).toFixed(2);
                parts.push(`<path d="${polygonToPath(room.polygon)}" fill="rgba(251,191,36,${fillOpacity})" pointer-events="none"/>`);
            }
            // Offset label below name/circuit lines already in the room
            const labelY = sc.y + (circuitColourMap[room.circuit_id] ? 0.55 : 0.38);
            parts.push(`<text class="fp-sun-hours" x="${sc.x}" y="${labelY}" font-size="0.16" text-anchor="middle"
                              pointer-events="none">${(minutes / 60).toFixed(1)}h sun</text>`);
        }
        if (defs.length) parts.push(`<defs>${defs.join('')}</defs>`);
    }

    // Cold zones — everywhere the shared heat-coverage field falls below the
    // comfort threshold (window/door drafts are baked into the field, so the
    // cold pools visibly gather around leaky openings). Requires thermal on.
    if (_state.showColdZones && _state.showThermal) {
        parts.push(...renderColdParts(lvl));
    }

    // Walls
    for (const w of lvl.walls) {
        const sel = isSelected('wall', w.id);
        const a = modelToSvg({ x: w.x1, y: w.y1 });
        const b = modelToSvg({ x: w.x2, y: w.y2 });
        const typ = w.type || 'unknown';
        parts.push(`
          <line class="fp-wall fp-wall-${typ} ${sel ? 'fp-selected' : ''}"
                x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"
                stroke-width="${sel ? 0.12 : 0.08}" stroke-linecap="square"
                data-kind="wall" data-id="${w.id}" style="cursor:pointer"/>`);
        // Endpoint drag handles only on the selected wall, and only when the
        // current tool is 'select' (otherwise drawing tools take precedence).
        if (sel && _state.tool === 'select') {
            parts.push(`
              <circle class="fp-wall-handle" cx="${a.x}" cy="${a.y}" r="0.16"
                      stroke-width="0.04" data-kind="wall-handle"
                      data-id="${w.id}" data-which="1"
                      style="cursor:grab"/>
              <circle class="fp-wall-handle" cx="${b.x}" cy="${b.y}" r="0.16"
                      stroke-width="0.04" data-kind="wall-handle"
                      data-id="${w.id}" data-which="2"
                      style="cursor:grab"/>`);
        }
    }

    // Openings — drawn ON the wall they belong to
    for (const o of lvl.openings) {
        const wall = lvl.walls.find(w => w.id === o.wall_id);
        if (!wall) continue;
        const sel = isSelected('opening', o.id);
        const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1);
        if (wlen < 1e-6) continue;
        const ux = (wall.x2 - wall.x1) / wlen;
        const uy = (wall.y2 - wall.y1) / wlen;
        const start = { x: wall.x1 + ux * o.offset_m, y: wall.y1 + uy * o.offset_m };
        const end   = { x: start.x + ux * o.width_m,  y: start.y + uy * o.width_m };
        const sa = modelToSvg(start), sb = modelToSvg(end);
        parts.push(`
          <line class="fp-opening fp-opening-${o.kind} ${sel ? 'fp-selected' : ''}"
                x1="${sa.x}" y1="${sa.y}" x2="${sb.x}" y2="${sb.y}"
                stroke-width="${sel ? 0.16 : 0.12}" stroke-linecap="butt"
                data-kind="opening" data-id="${o.id}" style="cursor:pointer"/>`);
        if (o.kind === 'door') {
            const mid = modelToSvg({ x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 });
            parts.push(`<circle class="fp-door-pivot" cx="${mid.x}" cy="${mid.y}" r="0.05" pointer-events="none"/>`);
        }
    }

    // Two modes: wall-mounted draws a thin strip along the host wall, offset
    // toward the room centroid; freestanding draws a length x 0.1 m strip at
    // (x, y). Plan depth is fixed — height_m is physical, not footprint.
    const RAD_PLAN_DEPTH_M = 0.10;   // fixed plan-view strip depth
    for (const r of lvl.radiators) {
        const sel = isSelected('radiator', r.id);
        const len = r.length_m || 0.6;
        const hgt = RAD_PLAN_DEPTH_M;   // plan-view depth — NOT r.height_m
        const wall = r.wall_id ? lvl.walls.find(w => w.id === r.wall_id) : null;

        if (wall) {
            // Wall-mounted geometry
            const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) || 1;
            const ux = (wall.x2 - wall.x1) / wlen, uy = (wall.y2 - wall.y1) / wlen;
            // Clamp offset so the radiator's CENTRE stays on the wall (length
            // can overhang slightly if wall is shorter than radiator)
            const t0 = Math.max(0, Math.min(wlen - len, (r.offset_m ?? wlen / 2)));
            // Perpendicular direction: rotate (ux, uy) 90° → (-uy, ux). We
            // pick the side facing the bound room's centroid. If no room
            // bound, default to the +90 side.
            let nx = -uy, ny = ux;
            const room = r.room_id ? (lvl.rooms || []).find(rm => rm.id === r.room_id) : null;
            if (room) {
                const c = polygonCentroid(room.polygon);
                const wallMidX = (wall.x1 + wall.x2) / 2;
                const wallMidY = (wall.y1 + wall.y2) / 2;
                // Dot product of (centroid - wallMid) with (nx, ny). Positive
                // means the chosen normal already points toward the room.
                const dot = (c.x - wallMidX) * nx + (c.y - wallMidY) * ny;
                if (dot < 0) { nx = -nx; ny = -ny; }
            }
            // Four corners of the radiator rectangle, in MODEL coords.
            const ax = wall.x1 + ux * t0,         ay = wall.y1 + uy * t0;
            const bx = ax + ux * len,             by = ay + uy * len;
            const cx = bx + nx * hgt,             cy = by + ny * hgt;
            const dx = ax + nx * hgt,             dy = ay + ny * hgt;
            // Convert to SVG coords (flip y)
            const A = modelToSvg({ x: ax, y: ay });
            const B = modelToSvg({ x: bx, y: by });
            const C = modelToSvg({ x: cx, y: cy });
            const D = modelToSvg({ x: dx, y: dy });
            // Label centre = average of midpoints of AB and CD
            const labelMx = (A.x + B.x + C.x + D.x) / 4;
            const labelMy = (A.y + B.y + C.y + D.y) / 4;
            parts.push(`
              <g data-kind="radiator" data-id="${r.id}" style="cursor:pointer">
                <polygon class="fp-radiator ${sel ? 'fp-selected' : ''}"
                         points="${A.x},${A.y} ${B.x},${B.y} ${C.x},${C.y} ${D.x},${D.y}"
                         stroke-width="${sel ? 0.04 : 0.02}"/>
                <text class="fp-radiator-label" x="${labelMx}" y="${labelMy + 0.05}"
                      font-size="0.14" text-anchor="middle">${Math.round(r.watts_at_dt50 || 0)}W</text>
              </g>`);
            // Slide handle: midpoint of the wall-side edge (A→B), only when
            // selected with the Select tool. Drag to slide along the wall.
            if (sel && _state.tool === 'select') {
                const handleMx = (A.x + B.x) / 2;
                const handleMy = (A.y + B.y) / 2;
                parts.push(`
                  <circle class="fp-rad-handle" cx="${handleMx}" cy="${handleMy}" r="0.18"
                          stroke-width="0.04" data-kind="rad-handle" data-id="${r.id}"
                          style="cursor:ew-resize"/>`);
            }
        } else {
            // Freestanding geometry — axis-aligned strip at (x, y)
            const p = modelToSvg({ x: r.x ?? 0, y: r.y ?? 0 });
            parts.push(`
              <g data-kind="radiator" data-id="${r.id}" style="cursor:pointer">
                <rect class="fp-radiator ${sel ? 'fp-selected' : ''}"
                      x="${p.x - len / 2}" y="${p.y - hgt / 2}" width="${len}" height="${hgt}"
                      stroke-width="${sel ? 0.04 : 0.02}"/>
                <text class="fp-radiator-label" x="${p.x}" y="${p.y + hgt / 2 + 0.22}"
                      font-size="0.14" text-anchor="middle">${Math.round(r.watts_at_dt50 || 0)}W</text>
              </g>`);
        }
    }

    // Sensors
    for (const s of lvl.sensors) {
        const sel = isSelected('sensor', s.id);
        const p = modelToSvg({ x: s.x ?? 0, y: s.y ?? 0 });
        parts.push(`
          <g data-kind="sensor" data-id="${s.id}" style="cursor:pointer">
            <circle class="fp-sensor ${sel ? 'fp-selected' : ''}"
                    cx="${p.x}" cy="${p.y}" r="0.13" stroke-width="${sel ? 0.04 : 0.02}"/>
            ${s.primary ? `<circle class="fp-sensor-primary-dot" cx="${p.x}" cy="${p.y}" r="0.06"/>` : ''}
          </g>`);
    }

    // Contact sensors — drawn near their opening's centre, slightly offset
    for (const c of lvl.contacts) {
        const op = lvl.openings.find(o => o.id === c.opening_id);
        if (!op) continue;
        const wall = lvl.walls.find(w => w.id === op.wall_id);
        if (!wall) continue;
        const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) || 1;
        const ux = (wall.x2 - wall.x1) / wlen, uy = (wall.y2 - wall.y1) / wlen;
        const cx = wall.x1 + ux * (op.offset_m + op.width_m / 2);
        const cy = wall.y1 + uy * (op.offset_m + op.width_m / 2);
        const p = modelToSvg({ x: cx + (-uy) * 0.12, y: cy + ux * 0.12 });
        const sel = isSelected('contact', c.id);
        parts.push(`
          <g data-kind="contact" data-id="${c.id}" style="cursor:pointer">
            <rect class="fp-contact ${sel ? 'fp-selected' : ''}"
                  x="${p.x - 0.07}" y="${p.y - 0.07}" width="0.14" height="0.14"
                  stroke-width="${sel ? 0.04 : 0.02}"/>
          </g>`);
    }

    if (_state.showMesh && _state.mesh) parts.push(...renderMeshParts(lvl));
    parts.push(...renderDeviceParts(lvl));

    scene.innerHTML = parts.join('');

    // Click bindings.
    // Wall-handle elements get special treatment: mousedown starts a drag,
    // not a selection (the wall is already selected when handles are visible).
    scene.querySelectorAll('[data-kind="wall-handle"]').forEach(el => {
        el.addEventListener('mousedown', e => {
            if (_state.tool !== 'select') return;
            e.stopPropagation();
            const wall = currentLevel().walls.find(w => w.id === el.dataset.id);
            if (!wall) return;
            _wallDrag = { wallId: wall.id, which: parseInt(el.dataset.which, 10) };
        });
    });

    // Radiator slide handle — drag along the host wall to change offset_m.
    scene.querySelectorAll('[data-kind="rad-handle"]').forEach(el => {
        el.addEventListener('mousedown', e => {
            if (_state.tool !== 'select') return;
            e.stopPropagation();
            const rad = currentLevel().radiators.find(r => r.id === el.dataset.id);
            if (!rad || !rad.wall_id) return;
            _radDrag = { radId: rad.id };
        });
    });

    // Regular selection on every other interactive element.
    scene.querySelectorAll('[data-kind][data-id]').forEach(el => {
        if (el.dataset.kind === 'wall-handle') return;
        if (el.dataset.kind === 'rad-handle') return;
        el.addEventListener('mousedown', e => {
            if (_state.tool !== 'select') return;
            e.stopPropagation();
            _state.selection = { kind: el.dataset.kind, id: el.dataset.id };
            // A device's marker (or a sensor, which is one) moves with the pointer.
            if (el.dataset.kind === 'device') _devDrag = { ieee: el.dataset.id };
            else if (el.dataset.kind === 'sensor') _devDrag = { sensorId: el.dataset.id };
            renderScene(); renderProps();
        });
    });

    renderLegend();
}

function renderOverlay() {
    const ov = document.getElementById('fpOverlay');
    const m2px = _state.zoom;
    // Always sync the overlay transform so all overlays (sun, calibration,
    // draw-preview) render in the same coordinate space as fpScene.
    ov.setAttribute('transform',
        `translate(${_state.pan.x}, ${_state.pan.y}) scale(${m2px}, ${m2px})`);
    let html = '';

    // Background-adjust frame: drag the image to move it, a corner to resize.
    if (_state.tool === 'bg' && _state.showBackground
        && currentLevel()?.background?.present) {
        html += bgAdjustParts(currentLevel().background);
    }

    // Room tool: the corners a room may use, and the one a click would take.
    if (_state.tool === 'room' && currentLevel()) {
        const px = 1 / (_state.zoom || PIXELS_PER_METRE_DEFAULT);   // markers keep their size on screen
        for (const c of roomCorners(currentLevel())) {
            const sp = modelToSvg(c);
            html += `<circle class="fp-room-corner" cx="${sp.x}" cy="${sp.y}" r="${4 * px}"/>`;
        }
        if (_state.roomHover) {
            const sp = modelToSvg(_state.roomHover);
            html += `<circle class="fp-room-corner-hover" cx="${sp.x}" cy="${sp.y}" r="${9 * px}"/>`;
        }
    }

    // Drawing preview
    if (_state.drawBuffer) {
        const db = _state.drawBuffer;

        // Window/door: single-segment drag (start → cur) along a host wall
        if ((_state.tool === 'window' || _state.tool === 'door') && db.start && db.cur) {
            const a = modelToSvg(db.start);
            const b = modelToSvg(db.cur);
            html += `<line class="fp-preview fp-preview-${_state.tool}"
                          x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"
                          stroke-width="0.08" stroke-dasharray="0.1 0.05"/>`;
            const dx = db.cur.x - db.start.x, dy = db.cur.y - db.start.y;
            const len = Math.hypot(dx, dy);
            const mid = modelToSvg({ x: (db.start.x + db.cur.x) / 2, y: (db.start.y + db.cur.y) / 2 });
            html += `<text class="fp-preview-label" x="${mid.x}" y="${mid.y - 0.15}" font-size="0.15" text-anchor="middle">${len.toFixed(2)} m</text>`;
        }

        // Wall (chain mode) and Room (polygon) share the same preview
        // shape: a polyline through committed vertices plus a rubber-band
        // segment to the cursor, with dots at every vertex.
        else if ((_state.tool === 'wall' || _state.tool === 'room') && db.points) {
            const ptsSvg = db.points.map(modelToSvg);
            const mouseSvg = db.cur ? modelToSvg(db.cur) : null;
            const viaSvg = (_state.tool === 'room' && db.curVia) ? db.curVia.map(modelToSvg) : [];
            const polyPts = ptsSvg.concat(viaSvg).map(p => `${p.x},${p.y}`).join(' ')
                + (mouseSvg ? ` ${mouseSvg.x},${mouseSvg.y}` : '');
            const cls = _state.tool === 'room'
                ? `fp-preview-room${db.curOk === false ? ' fp-preview-invalid' : ''}`
                : 'fp-preview fp-preview-wall';
            html += `<polyline class="${cls}"
                              points="${polyPts}"
                              stroke-width="0.06" stroke-dasharray="0.1 0.05"
                              fill="none"/>`;
            // Vertex dots — committed vertices are solid; first one slightly
            // bigger to invite "click here to close"
            for (let i = 0; i < ptsSvg.length; i++) {
                const sp = ptsSvg[i];
                const r = (i === 0 && _state.tool === 'wall' && db.points.length >= 2) ? 0.12 : 0.07;
                html += `<circle class="fp-preview-vertex" cx="${sp.x}" cy="${sp.y}" r="${r}"/>`;
            }
            // Live length + bearing label on the rubber-band segment for walls
            if (_state.tool === 'wall' && mouseSvg && db.points.length > 0) {
                const last = db.points[db.points.length - 1];
                const dx = db.cur.x - last.x, dy = db.cur.y - last.y;
                const len = Math.hypot(dx, dy);
                const rad = Math.atan2(dy, dx);
                const deg = ((rad * 180 / Math.PI) + 360) % 360;
                // Sticky-ortho feedback: when the segment sits exactly on a
                // 45° axis, extend a construction guide through the vertex.
                const spokeIdx = Math.round(rad / (Math.PI / 4));
                if (len > 0.01 && Math.abs(rad - spokeIdx * Math.PI / 4) < 1e-4) {
                    const gx = Math.cos(spokeIdx * Math.PI / 4);
                    const gy = Math.sin(spokeIdx * Math.PI / 4);
                    const g1 = modelToSvg({ x: last.x - gx * 200, y: last.y - gy * 200 });
                    const g2 = modelToSvg({ x: last.x + gx * 200, y: last.y + gy * 200 });
                    html += `<line class="fp-ortho-guide" x1="${g1.x}" y1="${g1.y}"
                                   x2="${g2.x}" y2="${g2.y}"/>`;
                }
                const mid = modelToSvg({ x: (last.x + db.cur.x) / 2, y: (last.y + db.cur.y) / 2 });
                html += `<text class="fp-preview-label" x="${mid.x}" y="${mid.y - 0.15}"
                               font-size="0.18" text-anchor="middle">${len.toFixed(2)} m · ${deg.toFixed(1)}°</text>`;
            }
        }
    }

    // Calibration: show first picked point and rubber-band to mouse
    if (_state.calibration && _state.calibration.p1) {
        const cal = _state.calibration;
        const p1svg = modelToSvg(cal.p1);
        // Crosshair-style marker: ring + centre dot for clear targeting.
        html += `<circle class="fp-calibration-marker" cx="${p1svg.x}" cy="${p1svg.y}"
                         r="0.25" stroke-width="0.06" fill="none"/>`;
        html += `<circle class="fp-calibration-marker" cx="${p1svg.x}" cy="${p1svg.y}"
                         r="0.06" stroke-width="0"/>`;
        html += `<text class="fp-preview-label" x="${p1svg.x}" y="${p1svg.y - 0.35}"
                       font-size="0.18" text-anchor="middle">A</text>`;

        // Live rubber-band to cursor (or to the locked p2 if click 2 just landed)
        const target = cal.p2 || cal.cur;
        if (target) {
            const p2svg = modelToSvg(target);
            html += `<line class="fp-preview fp-preview-wall"
                          x1="${p1svg.x}" y1="${p1svg.y}"
                          x2="${p2svg.x}" y2="${p2svg.y}"
                          stroke-width="0.06" stroke-dasharray="0.15 0.08"/>`;
            const dx = target.x - cal.p1.x, dy = target.y - cal.p1.y;
            const d  = Math.hypot(dx, dy);
            const mid = modelToSvg({ x: (cal.p1.x + target.x) / 2, y: (cal.p1.y + target.y) / 2 });
            html += `<text class="fp-preview-label" x="${mid.x}" y="${mid.y - 0.2}"
                           font-size="0.18" text-anchor="middle">${d.toFixed(2)} m drawn</text>`;
            // Second marker at current/locked endpoint
            html += `<circle class="fp-calibration-marker" cx="${p2svg.x}" cy="${p2svg.y}"
                             r="0.25" stroke-width="0.06" fill="none"/>`;
            html += `<circle class="fp-calibration-marker" cx="${p2svg.x}" cy="${p2svg.y}"
                             r="0.06" stroke-width="0"/>`;
            html += `<text class="fp-preview-label" x="${p2svg.x}" y="${p2svg.y - 0.35}"
                           font-size="0.18" text-anchor="middle">B</text>`;
        }
    }

    // The armed device follows the pointer as a pin, so it is clear where the
    // tip — the spot it will be placed at — actually is.
    if (_state.tool === 'place' && _state.placing && _state.placeCursor) {
        const info = deviceInfo(_state.placing);
        const p = modelToSvg(snapPt(_state.placeCursor));
        html += `<g class="fp-place-pin" pointer-events="none">
            <path class="fp-pin-body fp-device-${info.kind}"
                  d="M${p.x} ${p.y} l-0.22 -0.46 a0.26 0.26 0 1 1 0.44 0 Z"/>
            <circle class="fp-pin-hole" cx="${p.x}" cy="${p.y - 0.62}" r="0.09"/>
            <circle class="fp-pin-tip" cx="${p.x}" cy="${p.y}" r="0.05"/>
            <text class="fp-pin-text" x="${p.x}" y="${p.y + 0.26}" font-size="0.15"
                  text-anchor="middle">${escapeHtml(info.name)}</text>
          </g>`;
    }

    // Sun overlay — arc projected onto the floor plan, centred on the plan centroid.
    // Radial distance encodes solar elevation (high sun = arc pulled inward).
    if (_state.showSun) {
        if (!_state.sunData) {
            // Data not yet loaded or location not configured — show hint
            html += `<text class="fp-sun-label" x="0" y="0" font-size="0.28" text-anchor="middle"
                           opacity="0.6">Sun position unavailable — check location config</text>`;
        } else {
            const sd = _state.sunData;
            const lvl = currentLevel();
            const origin = planCentroid(lvl);
            // High sun pulls the arc inward (radius encodes elevation); it
            // stops at the building line so midday never crosses the rooms.
            const { clear: CLEAR_M, r: ARC_R, min: MIN_R } = sunArc(lvl);

            const sunPtToSvg = (pt) => {
                const planAz = ((pt.az + _state.plan.north_offset_deg) % 360 + 360) % 360;
                const ang = planAz * Math.PI / 180;
                const projR = Math.max(MIN_R, ARC_R * Math.cos(pt.el * Math.PI / 180));
                return modelToSvg({ x: origin.x + Math.sin(ang) * projR, y: origin.y + Math.cos(ang) * projR });
            };

            const daytime = (sd.points || []).filter(pt => pt.el > 0);
            if (daytime.length > 1) {
                const originSvg = modelToSvg(origin);
                const maxEl = Math.max(...daytime.map(p => p.el), 1);
                const arcPts = daytime.map(sunPtToSvg);
                const dStr = arcPts.map((p, i) =>
                    `${i === 0 ? 'M' : 'L'}${p.x.toFixed(3)},${p.y.toFixed(3)}`).join(' ');

                html += `<defs>
                  <filter id="fpSunGlow" x="-80%" y="-80%" width="260%" height="260%">
                    <feGaussianBlur stdDeviation="0.14"/>
                  </filter>
                  <radialGradient id="fpSunBall">
                    <stop offset="0%" stop-color="#fffbe8"/>
                    <stop offset="55%" stop-color="#fcd34d"/>
                    <stop offset="100%" stop-color="#f59e0b"/>
                  </radialGradient>
                </defs>`;

                // Soft halo under the arc, then elevation-coloured segments:
                // deep amber at the horizon → bright gold at solar noon.
                html += `<path class="fp-sun-path-glow" d="${dStr}" stroke-width="0.30"
                               stroke-linecap="round" filter="url(#fpSunGlow)"/>`;
                for (let i = 0; i < arcPts.length - 1; i++) {
                    const t = ((daytime[i].el + daytime[i + 1].el) / 2) / maxEl;
                    const col = `hsl(${(24 + 26 * t).toFixed(0)} 96% ${(52 + 12 * t).toFixed(0)}%)`;
                    html += `<line x1="${arcPts[i].x}" y1="${arcPts[i].y}"
                                   x2="${arcPts[i + 1].x}" y2="${arcPts[i + 1].y}"
                                   stroke="${col}" stroke-width="0.085" stroke-linecap="round"/>`;
                }

                // Hour ticks along the arc, labelled every 3 hours
                for (let i = 0; i < daytime.length; i++) {
                    const dte = new Date(daytime[i].ts);
                    if (dte.getMinutes() !== 0) continue;
                    const p = arcPts[i];
                    const dx = p.x - originSvg.x, dy = p.y - originSvg.y;
                    const dl = Math.hypot(dx, dy) || 1;
                    const rx = dx / dl, ry = dy / dl;
                    html += `<line class="fp-sun-tick" x1="${p.x - rx * 0.09}" y1="${p.y - ry * 0.09}"
                                   x2="${p.x + rx * 0.09}" y2="${p.y + ry * 0.09}" stroke-width="0.035"/>`;
                    if (dte.getHours() % 3 === 0) {
                        // Hour, then where the sun is at that hour
                        const lx = p.x + rx * 1.05, ly = p.y + ry * 1.05;
                        html += `<text class="fp-sun-hour" x="${lx}" y="${ly}" font-size="0.52"
                                       text-anchor="middle" font-weight="600">${String(dte.getHours()).padStart(2, '0')}:00</text>`;
                        html += `<text class="fp-sun-hour" x="${lx}" y="${ly + 0.42}" font-size="0.32"
                                       text-anchor="middle">az ${Math.round(daytime[i].az)}° · el ${Math.round(daytime[i].el)}°</text>`;
                    }
                }

                // Endpoint dots at sunrise / sunset
                const srPt = arcPts[0], ssPt = arcPts[arcPts.length - 1];
                html += `<circle class="fp-sun-endpoint" cx="${srPt.x}" cy="${srPt.y}" r="0.15"/>`;
                html += `<circle class="fp-sun-endpoint" cx="${ssPt.x}" cy="${ssPt.y}" r="0.15"/>`;

                // Rise / set time labels
                const fmtT = iso => iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
                const srL = fmtT(sd.sunrise), ssL = fmtT(sd.sunset);
                const riseSetLabel = (pt, time, dp) => time ? `
                    <text class="fp-sun-label" x="${pt.x}" y="${pt.y - 0.95}" font-size="0.64"
                          text-anchor="middle" font-weight="600">${time}</text>
                    <text class="fp-sun-label" x="${pt.x}" y="${pt.y - 0.50}" font-size="0.34"
                          text-anchor="middle">az ${Math.round(dp.az)}°</text>` : '';
                html += riseSetLabel(srPt, srL, daytime[0]);
                html += riseSetLabel(ssPt, ssL, daytime[daytime.length - 1]);

                // Current-time marker — closest data point to local clock
                const nowMs = Date.now();
                let closest = null, minDt = Infinity;
                for (const pt of daytime) {
                    const dt = Math.abs(new Date(pt.ts).getTime() - nowMs);
                    if (dt < minDt) { minDt = dt; closest = pt; }
                }
                if (closest) {
                    const sp = sunPtToSvg(closest);
                    // Light beam — sunlight is effectively parallel, so the
                    // shaft opens out to the full width of the building rather
                    // than converging on its centre, fading as it arrives.
                    const bdx = originSvg.x - sp.x, bdy = originSvg.y - sp.y;
                    const blen = Math.hypot(bdx, bdy) || 1;
                    const bx = bdx / blen, by = bdy / blen;   // unit vector sun → house
                    const pxv = -by, pyv = bx;                // perpendicular
                    const reach = planReach(lvl, origin);
                    const wSun = 0.45, wHouse = Math.max(1.6, reach * 1.05);
                    const beamPts = [
                        `${sp.x + pxv * wSun},${sp.y + pyv * wSun}`,
                        `${sp.x - pxv * wSun},${sp.y - pyv * wSun}`,
                        `${originSvg.x - pxv * wHouse},${originSvg.y - pyv * wHouse}`,
                        `${originSvg.x + pxv * wHouse},${originSvg.y + pyv * wHouse}`,
                    ].join(' ');
                    html += `<defs>
                      <linearGradient id="fpSunBeamGrad" gradientUnits="userSpaceOnUse"
                                      x1="${sp.x}" y1="${sp.y}" x2="${originSvg.x}" y2="${originSvg.y}">
                        <stop offset="0%"   stop-color="rgba(251,191,36,0.45)"/>
                        <stop offset="40%"  stop-color="rgba(251,191,36,0.18)"/>
                        <stop offset="75%"  stop-color="rgba(251,191,36,0.07)"/>
                        <stop offset="100%" stop-color="rgba(251,191,36,0)"/>
                      </linearGradient>
                    </defs>`;
                    html += `<polygon class="fp-sun-beam" points="${beamPts}"
                                      fill="url(#fpSunBeamGrad)" filter="url(#fpSunGlow)"/>`;
                    // Sun-ray particles — glowing motes drifting down the beam.
                    // Each particle animates translate(--fp-d, --fp-cy) in a
                    // group rotated to the beam direction, so the CSS keyframes
                    // stay direction-agnostic. Negative delays start the stream
                    // mid-flight instead of all bunched at the sun.
                    const angDeg = Math.atan2(bdy, bdx) * 180 / Math.PI;
                    let motes = '';
                    for (let i = 0; i < 30; i++) {
                        const dur = (2.6 + Math.random() * 2.6).toFixed(2);
                        const delay = (-Math.random() * 5).toFixed(2);
                        const r = (0.03 + Math.random() * 0.06).toFixed(3);
                        const dist = (blen * (0.72 + Math.random() * 0.33)).toFixed(2);
                        const drift = ((Math.random() - 0.5) * wHouse * 1.8).toFixed(3);
                        motes += `<circle class="fp-sun-particle" r="${r}"
                            style="--fp-d:${dist}px; --fp-cy:${drift}px;
                                   animation-duration:${dur}s; animation-delay:${delay}s;"/>`;
                    }
                    html += `<g transform="translate(${sp.x} ${sp.y}) rotate(${angDeg.toFixed(2)})">${motes}</g>`;
                    // Where that light lands: shafts through each sun-facing
                    // window, with motes drifting into the room.
                    html += sunShaftParts(lvl, closest);
                    // Glowing pulsing sun
                    html += `<circle cx="${sp.x}" cy="${sp.y}" r="0.42"
                                     fill="rgba(251,191,36,0.35)" filter="url(#fpSunGlow)"/>`;
                    html += `<circle class="fp-sun-pulse" cx="${sp.x}" cy="${sp.y}" r="0.34"/>`;
                    html += `<circle cx="${sp.x}" cy="${sp.y}" r="0.20" fill="url(#fpSunBall)"/>`;
                    html += `<text class="fp-sun-hour" x="${sp.x}" y="${sp.y + 0.98}" font-size="0.48"
                                   text-anchor="middle" font-weight="600">${fmtT(closest.ts)}</text>`;
                    html += `<text class="fp-sun-hour" x="${sp.x}" y="${sp.y + 1.40}" font-size="0.36"
                                   text-anchor="middle">az ${Math.round(closest.az)}° · el ${Math.round(closest.el)}°</text>`;
                }

                // North marker just outside the arc, honouring the compass offset
                const nRad = (_state.plan.north_offset_deg * Math.PI) / 180;
                const np = modelToSvg({
                    x: origin.x + Math.sin(nRad) * (ARC_R + CLEAR_M * 0.6),
                    y: origin.y + Math.cos(nRad) * (ARC_R + CLEAR_M * 0.6),
                });
                html += `<text class="fp-north-mark" x="${np.x}" y="${np.y + 0.09}" font-size="0.28"
                               text-anchor="middle" font-weight="bold">N</text>`;
            }
        }
    }

    ov.innerHTML = html;

    ov.querySelectorAll('[data-kind="bg-handle"]').forEach(el => {
        el.addEventListener('mousedown', e => { e.stopPropagation(); startBgResize(el); });
    });
}

/** The outline and corner grips shown while the Adjust image tool is active. */
function bgAdjustParts(bg) {
    const g = bgGeom(bg);
    const r = 7 / _state.zoom;                 // grips a constant size on screen
    const corners = [[0, 0], [1, 0], [1, 1], [0, 1]];
    const pts = corners.map(([u, v]) => modelToSvg(bgPoint(bg, u, v)));
    let out = `<polygon class="fp-bg-adjust" points="${pts.map(q => `${q.x},${q.y}`).join(' ')}"/>`;
    pts.forEach((q, i) => {
        // Opposite corners share a diagonal, so the cursor reads correctly.
        const cursor = (corners[i][0] === corners[i][1]) ? 'nwse-resize' : 'nesw-resize';
        out += `<rect class="fp-bg-handle" data-kind="bg-handle"
                      data-u="${corners[i][0]}" data-v="${corners[i][1]}"
                      x="${q.x - r}" y="${q.y - r}" width="${2 * r}" height="${2 * r}"
                      style="cursor:${cursor}"/>`;
    });
    const lbl = modelToSvg(bgPoint(bg, 0.5, 1));
    out += `<text class="fp-preview-label" x="${lbl.x}" y="${lbl.y + 0.35}"
                  font-size="0.22" text-anchor="middle">${g.wM.toFixed(2)} &#215; ${g.hM.toFixed(2)} m</text>`;
    return out;
}

// interactions

function setTool(tool) {
    _state.tool = tool;
    _state.drawBuffer = null;
    _bgDrag = null;
    // Adjusting an image you can't see is a trap; turn it back on.
    if (tool === 'bg' && !_state.showBackground) {
        _state.showBackground = true;
        const t = document.getElementById('fpToggleBackground');
        if (t) t.checked = true;
    }
    _state.calibration = null;
    _state.selection = null;
    _state.roomHover = null;
    if (tool === 'room') {
        // Room corners can only go where walls really meet, so close the
        // near misses first; otherwise those junctions have no corner.
        const joined = joinWallEnds(currentLevel());
        if (joined) toast('info', 'Walls joined up',
            `${joined} wall end${joined === 1 ? ' was' : 's were'} just off the wall `
            + `${joined === 1 ? 'it meets and now meets it' : 'they meet and now meet them'}, `
            + 'so rooms can use those corners. Save to keep it.');
    }
    renderToolbar(); renderProps(); renderOverlay();
}

let _isPanning = false;
let _panStart = null;

// Wall vertex drag state — set when the user grabs an endpoint handle on
// a selected wall. `which` is 1 or 2 (which endpoint of the wall). The
// drag updates `wall.x{which}/y{which}` live; mouse-up commits with snap.
let _wallDrag = null;

// Radiator drag state — set when the user grabs the slide handle on a
// selected wall-mounted radiator. Drag projects the cursor onto the host
// wall and updates `radiator.offset_m` live, clamped so the radiator stays
// on the wall.
let _radDrag = null;

// Background-image drag state, set by the Adjust image tool: `move` slides the
// image, `scale` resizes it about the corner opposite the one being dragged.
let _bgDrag = null;

/** Grab a corner grip: pin the opposite corner and remember the start size. */
function startBgResize(el) {
    const bg = currentLevel()?.background;
    if (!bg?.present) return;
    const u = parseFloat(el.dataset.u), v = parseFloat(el.dataset.v);
    const uA = 1 - u, vA = 1 - v;
    const g = bgGeom(bg);
    const d0 = { x: (u - uA) * g.wM, y: (vA - v) * g.hM };
    _bgDrag = {
        mode: 'scale', uA, vA, w0: g.wM,
        anchor: bgPoint(bg, uA, vA),
        d0, len2: d0.x * d0.x + d0.y * d0.y,
    };
}

function onCanvasMouseDown(e) {
    if (e.button === 1 || (e.button === 0 && e.shiftKey)) {
        _isPanning = true;
        _panStart = { x: e.clientX - _state.pan.x, y: e.clientY - _state.pan.y };
        document.getElementById('fpCanvas').style.cursor = 'grabbing';
        return;
    }
    if (e.button !== 0) return;
    const m = snapPt(clientToSvgModel(e));
    const lvl = currentLevel();

    if (_state.tool === 'wall') {
        // Chain mode: each click drops a vertex; consecutive vertices form a
        // wall. Click the first vertex again (or press Enter / Esc / right-
        // click) to finish the chain. Ctrl locks the segment angle to 15°
        // increments; Alt disables all snapping for the click.
        const snapped = wallDrawPoint(e, lvl);
        if (!_state.drawBuffer || !_state.drawBuffer.points) {
            _state.drawBuffer = { points: [snapped], cur: snapped };
        } else {
            const pts = _state.drawBuffer.points;
            const first = pts[0];
            const last = pts[pts.length - 1];
            // Click on first point closes the chain
            if (pts.length >= 2 && Math.hypot(snapped.x - first.x, snapped.y - first.y) < closeRadiusM()) {
                pts.push(first);
                finishWallChain();
                return;
            }
            // Reject zero-length segment
            if (Math.hypot(snapped.x - last.x, snapped.y - last.y) < 0.04) {
                toast('warn', 'Too short', 'Move further before clicking.');
                return;
            }
            pts.push(snapped);
        }
        renderOverlay();
    } else if (_state.tool === 'window' || _state.tool === 'door') {
        // Find nearest wall to start point; constrain to it
        const w = nearestWall(lvl, m);
        if (!w) return;
        const proj = projectPointOntoSegment(m, w);
        _state.drawBuffer = { wall: w, start: proj.point, startT: proj.t, cur: proj.point };
    } else if (_state.tool === 'room') {
        // Corner to corner: every vertex lands on a wall corner (or another
        // room's), and clicking the first corner again closes the room.
        addRoomCorner(roomDrawPoint(e, lvl), lvl);
    } else if (_state.tool === 'radiator' || _state.tool === 'sensor') {
        addPointFeature(_state.tool, m);
    } else if (_state.tool === 'contact') {
        // If the click is near a window/door, auto-bind to that opening.
        // Otherwise place an unbound contact and let the user pick the
        // opening from a dropdown in the property panel.
        const op = nearestOpening(lvl, m);
        addContact(op || null);
        if (!op) {
            toast('info', 'No opening', 'Contact placed; choose a window/door in the panel.');
        }
    } else if (_state.tool === 'calibrate') {
        if (!_state.calibration) {
            _state.calibration = { p1: m, cur: m };
            toast('info', 'Calibration', 'Click the second known point.');
            renderOverlay();
        } else {
            const p1 = _state.calibration.p1;
            const p2 = m;
            const drawnDist = Math.hypot(p2.x - p1.x, p2.y - p1.y);
            // Show BOTH points before the prompt, so the user sees their
            // second click registered. The prompt blocks the event loop, so
            // we render synchronously first, then yield to let the browser
            // paint, then open the prompt.
            _state.calibration = { p1, p2, cur: p2 };
            renderOverlay();
            // requestAnimationFrame to ensure the second marker is painted
            // before the modal prompt blocks rendering on some browsers.
            requestAnimationFrame(() => {
                _state.calibration = null;
                promptCalibrationDistance(p1, p2, drawnDist);
                renderOverlay();
            });
        }
    } else if (_state.tool === 'bg') {
        const bg = lvl.background;
        if (!bg?.present) {
            toast('warn', 'No image', 'Import a background image first.');
            return;
        }
        // Free (unsnapped) point: the image is being lined up with the walls
        // by eye, and the grid would fight that.
        const raw = clientToSvgModel(e);
        const f = bgFrac(bg, raw);
        if (f.u < 0 || f.u > 1 || f.v < 0 || f.v > 1) return;
        _bgDrag = { mode: 'move', grab: raw, ox: bg.origin_x_m, oy: bg.origin_y_m };
    } else if (_state.tool === 'place' && _state.placing) {
        placeDevice(_state.placing, clientToSvgModel(e));
    } else if (_state.tool === 'map-anchor') {
        _state.plan.map = { opacity: 0.6, ...(_state.plan.map || {}),
                            anchor_x_m: round3(m.x), anchor_y_m: round3(m.y) };
        setTool('select');
        renderScene();
    } else if (_state.tool === 'select') {
        // A tap arrives here, not on the element; find what is under it.
        const hit = document.elementFromPoint?.(e.clientX, e.clientY)
            ?.closest?.('#fpScene [data-kind][data-id]');
        _state.selection = hit && !hit.dataset.kind.endsWith('handle')
            ? { kind: hit.dataset.kind, id: hit.dataset.id } : null;
        renderScene(); renderProps();
    }
}

function onCanvasMouseMove(e) {
    if (_isPanning && _panStart) {
        _state.pan.x = e.clientX - _panStart.x;
        _state.pan.y = e.clientY - _panStart.y;
        renderScene(); renderOverlay();
        return;
    }
    // Wall-handle drag: live update the endpoint as the user moves. Snap to
    // grid by default; if a nearby OTHER wall's endpoint is in range, prefer
    // that (so dragging an endpoint can reattach to a neighbour cleanly).
    if (_wallDrag) {
        const lvl = currentLevel();
        const w = lvl.walls.find(x => x.id === _wallDrag.wallId);
        if (w) {
            const m = snapPt(clientToSvgModel(e));
            const snapped = snapToOtherWallEndpoint(lvl, w.id, m) || snapOntoWall(lvl, m, w.id) || m;
            if (_wallDrag.which === 1) { w.x1 = snapped.x; w.y1 = snapped.y; }
            else                       { w.x2 = snapped.x; w.y2 = snapped.y; }
            renderScene(); renderProps();
        }
        return;
    }
    // Radiator slide drag: project cursor onto the host wall, compute the
    // offset that keeps the radiator's visual centre under the cursor, clamp
    // so it stays on the wall, write back to offset_m.
    if (_radDrag) {
        const lvl = currentLevel();
        const r = lvl.radiators.find(x => x.id === _radDrag.radId);
        const w = r?.wall_id ? lvl.walls.find(wl => wl.id === r.wall_id) : null;
        if (r && w) {
            const m = clientToSvgModel(e);   // no grid-snap; smooth drag
            const proj = projectPointOntoSegment(m, w);
            const wlen = Math.hypot(w.x2 - w.x1, w.y2 - w.y1) || 1;
            const len = r.length_m || 0.6;
            // proj.t is the cursor's position along the wall (in metres from
            // wall.start). We want the radiator CENTRE under the cursor:
            //   offset_m = proj.t - len/2
            let offset = proj.t - len / 2;
            // Clamp so the radiator stays on the wall. If wall is shorter
            // than the radiator, pin offset to 0 — render code is forgiving.
            const maxOffset = Math.max(0, wlen - len);
            offset = Math.max(0, Math.min(maxOffset, offset));
            r.offset_m = Math.round(offset * 1000) / 1000;
            renderScene(); renderProps();
        }
        return;
    }
    if (_bgDrag) {
        const bg = currentLevel()?.background;
        if (bg?.present) {
            const p = clientToSvgModel(e);
            if (_bgDrag.mode === 'move') {
                bg.origin_x_m = round3(_bgDrag.ox + (p.x - _bgDrag.grab.x));
                bg.origin_y_m = round3(_bgDrag.oy + (p.y - _bgDrag.grab.y));
            } else {
                // Project the cursor onto the diagonal the grip started on, so
                // the resize keeps the image's aspect ratio whatever the drag.
                const d = bgVecToImage(bg, { x: p.x - _bgDrag.anchor.x,
                                             y: p.y - _bgDrag.anchor.y });
                const s = _bgDrag.len2 > 0
                    ? (d.x * _bgDrag.d0.x + d.y * _bgDrag.d0.y) / _bgDrag.len2 : 1;
                bgResizeAnchored(bg, Math.max(0.01, _bgDrag.w0 * s),
                                 _bgDrag.uA, _bgDrag.vA, _bgDrag.anchor);
            }
            renderScene(); renderOverlay(); syncBackgroundControls();
        }
        return;
    }
    if (_devDrag) {
        const m = snapPt(clientToSvgModel(e));
        const target = _devDrag.ieee
            ? (currentLevel().devices || []).find(d => d.ieee === _devDrag.ieee)
            : currentLevel().sensors.find(x => x.id === _devDrag.sensorId);
        if (target) { target.x = round3(m.x); target.y = round3(m.y); renderScene(); }
        return;
    }
    if (_state.tool === 'place' && _state.placing) {
        _state.placeCursor = clientToSvgModel(e);
        renderOverlay();
        return;
    }
    // Calibration tool tracks cursor between the two clicks for live feedback.
    if (_state.tool === 'calibrate' && _state.calibration && _state.calibration.p1) {
        _state.calibration.cur = snapPt(clientToSvgModel(e));
        renderOverlay();
        return;
    }
    if (_state.tool === 'room') {
        // Light up the corner a click would land on, and show whether the
        // edge to it is allowed, before the click.
        const lvl = currentLevel();
        const hover = roomDrawPoint(e, lvl);
        const db = _state.drawBuffer;
        if (!db) {
            const was = _state.roomHover;
            _state.roomHover = hover;
            if (!!was !== !!hover || (was && hover && !samePoint(was, hover))) renderOverlay();
            return;
        }
        _state.roomHover = hover;
        db.cur = hover || clientToSvgModel(e);
        const last = db.points[db.points.length - 1];
        const legs = hover && !samePoint(hover, last) ? roomLegs(lvl, last, hover) : null;
        db.curOk = !!hover && !legs?.problem;
        db.curVia = legs && !legs.problem ? legs.via : [];   // preview the route the edge will take
        renderOverlay();
        return;
    }
    if (!_state.drawBuffer) return;
    const m = snapPt(clientToSvgModel(e));
    if (_state.tool === 'wall') {
        // Endpoint snap / Ctrl angle lock, same as the click handler
        const lvl = currentLevel();
        _state.drawBuffer.cur = wallDrawPoint(e, lvl);
    } else if (_state.tool === 'window' || _state.tool === 'door') {
        const proj = projectPointOntoSegment(m, _state.drawBuffer.wall);
        _state.drawBuffer.cur = proj.point;
        _state.drawBuffer.curT = proj.t;
    }
    renderOverlay();
}

function onCanvasMouseUp(e) {
    if (_isPanning) { _isPanning = false; _panStart = null; renderToolbar(); return; }
    if (_wallDrag) { _wallDrag = null; renderScene(); renderProps(); return; }
    if (_radDrag)  { _radDrag = null;  renderScene(); renderProps(); return; }
    if (_devDrag)  { _devDrag = null;  renderScene(); renderProps(); return; }
    if (_bgDrag)   { _bgDrag = null;   renderScene(); renderOverlay();
                     syncBackgroundControls(); return; }
    if (!_state.drawBuffer) return;
    const lvl = currentLevel();

    if (_state.tool === 'wall') {
        // Chain mode commits walls in onCanvasMouseDown, not on mouse-up.
        // Nothing to do here. (Kept as a no-op so the mouseup still fires
        // for clean event handling.)
        return;
    } else if (_state.tool === 'window' || _state.tool === 'door') {
        const wall = _state.drawBuffer.wall;
        const t1 = _state.drawBuffer.startT;
        const t2 = _state.drawBuffer.curT;
        const offset_m = Math.min(t1, t2);
        const width_m  = Math.abs(t2 - t1);
        if (width_m >= 0.1) {
            const id = genId(_state.tool === 'window' ? 'win' : 'dr');
            const op = {
                id, wall_id: wall.id, kind: _state.tool,
                offset_m, width_m,
                height_m: _state.tool === 'window' ? 1.2 : 2.0,
            };
            if (_state.tool === 'window') op.glazing = 'double';
            else op.door_type = 'internal';
            lvl.openings.push(op);
            _state.selection = { kind: 'opening', id };
        }
        _state.drawBuffer = null;
        renderScene(); renderOverlay(); renderProps();
    }
}

function onCanvasWheel(e) {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
    zoomBy(factor, e.clientX, e.clientY);
}

// Touch: one finger moved <8 px and released <400 ms is a tap (simulated
// mousedown/mouseup so drawing still works), one finger moved further is a pan,
// two fingers pinch-zoom. All three work in any drawing mode.

let _touch = null;   // active touch gesture state

function onCanvasTouchStart(e) {
    e.preventDefault();
    if (e.touches.length === 1) {
        const t = e.touches[0];
        // A finger on the selected marker drags it; anywhere else pans.
        const hit = document.elementFromPoint?.(t.clientX, t.clientY)
            ?.closest?.('#fpScene [data-kind="device"], #fpScene [data-kind="sensor"]');
        if (hit && _state.tool === 'select' && isSelected(hit.dataset.kind, hit.dataset.id)) {
            _devDrag = hit.dataset.kind === 'device' ? { ieee: hit.dataset.id } : { sensorId: hit.dataset.id };
            _touch = { mode: 'drag' };
            return;
        }
        if (_state.tool === 'bg') {
            const grip = document.elementFromPoint?.(t.clientX, t.clientY)
                ?.closest?.('#fpOverlay [data-kind="bg-handle"]');
            if (grip) startBgResize(grip);
            else onCanvasMouseDown({ clientX: t.clientX, clientY: t.clientY,
                                     button: 0, shiftKey: false });
            if (_bgDrag) { _touch = { mode: 'drag' }; return; }
        }
        _touch = {
            mode: 'single',
            id: t.identifier,
            startX: t.clientX, startY: t.clientY,
            curX: t.clientX, curY: t.clientY,
            startTime: Date.now(),
            moved: false,
        };
    } else if (e.touches.length === 2) {
        // Second finger arrived — cancel any single-touch pan/tap in progress
        if (_isPanning) { _isPanning = false; _panStart = null; }
        const dx = e.touches[1].clientX - e.touches[0].clientX;
        const dy = e.touches[1].clientY - e.touches[0].clientY;
        _touch = {
            mode: 'pinch',
            dist: Math.hypot(dx, dy) || 1,
        };
    }
}

function onCanvasTouchMove(e) {
    e.preventDefault();
    if (!_touch) return;

    if (_touch.mode === 'drag' && e.touches.length === 1) {
        onCanvasMouseMove({ clientX: e.touches[0].clientX, clientY: e.touches[0].clientY });
        return;
    }
    if (_touch.mode === 'single' && e.touches.length === 1) {
        const t = e.touches[0];
        const dx = t.clientX - _touch.startX;
        const dy = t.clientY - _touch.startY;

        if (!_touch.moved && Math.hypot(dx, dy) > 8) {
            // Crossed the movement threshold — switch to pan mode
            _touch.moved = true;
            _isPanning = true;
            _panStart = { x: _touch.startX - _state.pan.x, y: _touch.startY - _state.pan.y };
        }

        if (_touch.moved) {
            _state.pan.x = t.clientX - _panStart.x;
            _state.pan.y = t.clientY - _panStart.y;
            renderScene(); renderOverlay();
        } else {
            // Still within tap threshold — feed live position to drawing tools
            // so they can show a preview snap indicator while the finger rests.
            onCanvasMouseMove({ clientX: t.clientX, clientY: t.clientY });
        }
        _touch.curX = t.clientX;
        _touch.curY = t.clientY;

    } else if (_touch.mode === 'pinch' && e.touches.length === 2) {
        const dx = e.touches[1].clientX - e.touches[0].clientX;
        const dy = e.touches[1].clientY - e.touches[0].clientY;
        const dist = Math.hypot(dx, dy) || 1;
        const factor = dist / _touch.dist;
        const cx = (e.touches[0].clientX + e.touches[1].clientX) / 2;
        const cy = (e.touches[0].clientY + e.touches[1].clientY) / 2;
        zoomBy(factor, cx, cy);
        _touch.dist = dist;
    }
}

function onCanvasTouchEnd(e) {
    e.preventDefault();
    if (!_touch) return;

    if (_touch.mode === 'drag') {
        _devDrag = null;
        _bgDrag = null;
        _touch = null;
        renderScene(); renderOverlay(); renderProps(); syncBackgroundControls();
        return;
    }
    if (_touch.mode === 'single') {
        const dt = Date.now() - _touch.startTime;
        if (!_touch.moved && dt < 400) {
            // Tap — simulate a click so drawing tools and selection work
            const synth = { clientX: _touch.startX, clientY: _touch.startY, button: 0, shiftKey: false };
            onCanvasMouseDown(synth);
            onCanvasMouseUp(synth);
        }
        if (_isPanning) { _isPanning = false; _panStart = null; renderToolbar(); }
    }

    _touch = null;
}

function clientToSvgModel(evt) {
    const svgPt = clientToSvg(evt);
    return svgToModel(svgPt);
}

function isSelected(kind, id) {
    return _state.selection && _state.selection.kind === kind && _state.selection.id === id;
}

// geometry helpers (frontend)

function projectPointOntoSegment(p, w) {
    const dx = w.x2 - w.x1, dy = w.y2 - w.y1;
    const L = Math.hypot(dx, dy);
    if (L < 1e-9) return { point: { x: w.x1, y: w.y1 }, t: 0 };
    const t = ((p.x - w.x1) * dx + (p.y - w.y1) * dy) / (L * L) * L;
    const tClamped = Math.max(0, Math.min(L, t));
    return {
        t: tClamped,
        point: { x: w.x1 + (dx / L) * tClamped, y: w.y1 + (dy / L) * tClamped },
    };
}

function nearestWall(lvl, p) {
    let best = null, bestD = Infinity;
    for (const w of lvl.walls) {
        const proj = projectPointOntoSegment(p, w);
        const d = Math.hypot(p.x - proj.point.x, p.y - proj.point.y);
        if (d < bestD) { bestD = d; best = w; }
    }
    return bestD < 0.5 ? best : null;
}

function nearestOpening(lvl, p) {
    let best = null, bestD = Infinity;
    for (const o of lvl.openings) {
        const w = lvl.walls.find(x => x.id === o.wall_id);
        if (!w) continue;
        const wlen = Math.hypot(w.x2 - w.x1, w.y2 - w.y1) || 1;
        const ux = (w.x2 - w.x1) / wlen, uy = (w.y2 - w.y1) / wlen;
        const cx = w.x1 + ux * (o.offset_m + o.width_m / 2);
        const cy = w.y1 + uy * (o.offset_m + o.width_m / 2);
        const d = Math.hypot(p.x - cx, p.y - cy);
        if (d < bestD) { bestD = d; best = o; }
    }
    return bestD < 0.6 ? best : null;
}

/**
 * If `p` is within the zoom-aware merge radius of an existing wall endpoint
 * (or an in-progress chain vertex), return that endpoint snapped exactly.
 * Otherwise null. This makes chains close cleanly and adjacent walls share
 * endpoints exactly, while still allowing tight gaps when zoomed in.
 */
function snapToExistingEndpoint(lvl, p) {
    const SNAP_R = snapRadiusM();
    if (SNAP_R <= 0) return null;
    let best = null, bestD = SNAP_R;
    for (const w of (lvl.walls || [])) {
        for (const ep of [{ x: w.x1, y: w.y1 }, { x: w.x2, y: w.y2 }]) {
            const d = Math.hypot(p.x - ep.x, p.y - ep.y);
            if (d < bestD) { bestD = d; best = ep; }
        }
    }
    // Also snap to vertices already placed in the current chain
    if (_state?.drawBuffer?.points) {
        for (const ep of _state.drawBuffer.points) {
            const d = Math.hypot(p.x - ep.x, p.y - ep.y);
            if (d < bestD) { bestD = d; best = ep; }
        }
    }
    return best ? { x: best.x, y: best.y } : null;
}

/**
 * Snap-to-endpoint variant for drag operations: like snapToExistingEndpoint
 * but excludes the wall being dragged (so an endpoint can't snap to itself
 * or to its other end on the same wall).
 */
function snapToOtherWallEndpoint(lvl, excludeWallId, p) {
    const SNAP_R = snapRadiusM();
    if (SNAP_R <= 0) return null;
    let best = null, bestD = SNAP_R;
    for (const w of (lvl.walls || [])) {
        if (w.id === excludeWallId) continue;
        for (const ep of [{ x: w.x1, y: w.y1 }, { x: w.x2, y: w.y2 }]) {
            const d = Math.hypot(p.x - ep.x, p.y - ep.y);
            if (d < bestD) { bestD = d; best = ep; }
        }
    }
    return best ? { x: best.x, y: best.y } : null;
}

function polygonCentroid(poly) {
    if (!poly || poly.length < 3) {
        return poly && poly[0] ? { x: poly[0][0], y: poly[0][1] } : { x: 0, y: 0 };
    }
    let cx = 0, cy = 0, a = 0;
    for (let i = 0; i < poly.length; i++) {
        const [x1, y1] = poly[i];
        const [x2, y2] = poly[(i + 1) % poly.length];
        const cross = x1 * y2 - x2 * y1;
        a += cross;
        cx += (x1 + x2) * cross;
        cy += (y1 + y2) * cross;
    }
    a *= 0.5;
    if (Math.abs(a) < 1e-9) {
        return { x: poly[0][0], y: poly[0][1] };
    }
    return { x: cx / (6 * a), y: cy / (6 * a) };
}

function polygonToPath(poly) {
    if (!poly || poly.length < 2) return '';
    return poly.map((p, i) => {
        const sp = modelToSvg({ x: p[0], y: p[1] });
        return `${i === 0 ? 'M' : 'L'} ${sp.x} ${sp.y}`;
    }).join(' ') + ' Z';
}

// adaptive grid & HUD

// Nice step ladders (metres) for the grid tiers and the scale bar.
const GRID_STEPS = [0.5, 1, 2, 5, 10, 20, 50];
const SCALE_BAR_STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20];

/**
 * Model-aligned adaptive grid. Unlike the old fixed 40 px screen pattern,
 * the three tiers pick real-world steps (e.g. 1 m / 20 cm / 4 cm) from the
 * current zoom, and the pattern phase follows the pan so lines sit exactly
 * on round metre values — with the model origin axes highlighted. Also
 * drives the scale bar and the header status readout.
 */
function renderGrid() {
    const z = _state.zoom;
    const M = GRID_STEPS.find(s => s * z >= 64) ?? GRID_STEPS[GRID_STEPS.length - 1];
    const minor = M / 5;
    const fine = M / 25;
    const ox = _state.pan.x, oy = _state.pan.y;

    const setPattern = (id, stepPx) => {
        const p = document.getElementById(id);
        if (!p) return;
        p.setAttribute('width', stepPx);
        p.setAttribute('height', stepPx);
        p.setAttribute('x', ox);
        p.setAttribute('y', oy);
        p.querySelector('path')?.setAttribute('d', `M ${stepPx} 0 L 0 0 0 ${stepPx}`);
        const fill = p.querySelector('rect');
        if (fill) { fill.setAttribute('width', stepPx); fill.setAttribute('height', stepPx); }
    };
    setPattern('fpGridMajor', M * z);
    setPattern('fpGridMinor', minor * z);
    setPattern('fpGridFine', fine * z);
    // Fine tier only when its lines are ≥ 8 px apart (else it's visual noise)
    document.getElementById('fpGridMinorFill')
        ?.setAttribute('fill', fine * z >= 8 ? 'url(#fpGridFine)' : 'none');

    // Origin axes (screen space — the grid group carries no transform)
    const ax = document.getElementById('fpAxisX');
    const ay = document.getElementById('fpAxisY');
    if (ax) { ax.setAttribute('y1', oy); ax.setAttribute('y2', oy); }
    if (ay) { ay.setAttribute('x1', ox); ay.setAttribute('x2', ox); }

    // Scale bar — the step whose screen length lands closest to ~100 px
    const rule = document.getElementById('fpScaleBarRule');
    const lab = document.getElementById('fpScaleBarLabel');
    if (rule && lab) {
        let bestL = SCALE_BAR_STEPS[0];
        for (const s of SCALE_BAR_STEPS) {
            if (Math.abs(s * z - 100) < Math.abs(bestL * z - 100)) bestL = s;
        }
        rule.style.width = `${Math.round(bestL * z)}px`;
        lab.textContent = bestL < 1 ? `${Math.round(bestL * 100)} cm` : `${bestL} m`;
    }

    // Header status: zoom, effective grid resolution, snap step
    const st = document.getElementById('fpStatus');
    if (st) {
        const fmtM = v => v < 1 ? `${Math.round(v * 100)} cm` : `${v} m`;
        const snapLbl = _state.snapStep > 0 ? fmtM(_state.snapStep) : 'off';
        st.textContent = `${Math.round(z)} px/m · grid ${fmtM(minor)} · snap ${snapLbl}`;
    }
}

/**
 * Legend for whichever overlays are active, rendered as a small HUD card in
 * the canvas corner. Hidden when no overlay needs explaining.
 */
function renderLegend() {
    const el = document.getElementById('fpLegend');
    if (!el) return;
    const rows = [];
    if (_state.showThermal) {
        const modeLabel = {
            'rad': 'Radiator heat coverage',
            'rad+sun': 'Radiator + solar coverage',
            'sun': 'Solar gain coverage',
        }[_state.thermalMode || 'rad'];
        rows.push(`<div class="fp-legend-row"><span class="fp-legend-ramp"></span>${modeLabel}</div>`);
        if (_state.showContours) {
            rows.push(`<div class="fp-legend-row"><span class="fp-legend-line fp-legend-line-contour"></span>Isotherm contours</div>`);
        }
        if (_state.showColdZones) {
            rows.push(`<div class="fp-legend-row"><span class="fp-legend-swatch fp-legend-swatch-cold"></span>Below comfort @ ${_state.heatFluxWm2} W/m²</div>`);
            rows.push(`<div class="fp-legend-row"><span class="fp-legend-line fp-legend-line-cold"></span>Comfort boundary</div>`);
        }
    }
    if (_state.showSun) {
        rows.push(`<div class="fp-legend-row"><span class="fp-legend-dot"></span>Sun path (today)</div>`);
    }
    el.style.display = rows.length ? '' : 'none';
    el.innerHTML = rows.join('');
}

/**
 * Resolve the next wall-chain vertex from a pointer event: Ctrl locks the
 * segment angle to the configured increment (default 1°) from the last
 * vertex, with the distance still grid-snapped; otherwise grid-snap then
 * endpoint-merge. Alt bypasses all of it.
 *
 * Sticky ortho: while angle-locked, bearings within ±3° of a cardinal
 * (0/90/180/270) or ±2° of a diagonal (45/135/…) snap hard to that axis,
 * so square rooms stay square even at a 1° angle step.
 */
function wallDrawPoint(e, lvl) {
    const raw = clientToSvgModel(e);
    const pts = _state.drawBuffer?.points;
    if (e.ctrlKey && pts && pts.length > 0) {
        const last = pts[pts.length - 1];
        const dist = snap(Math.hypot(raw.x - last.x, raw.y - last.y));
        const rawA = Math.atan2(raw.y - last.y, raw.x - last.x);
        const step = (_state.angleStep || 1) * Math.PI / 180;
        let a = Math.round(rawA / step) * step;
        const spokeIdx = Math.round(rawA / (Math.PI / 4));
        const spoke = spokeIdx * Math.PI / 4;
        const stickyTol = (spokeIdx % 2 === 0 ? 3 : 2) * Math.PI / 180;
        if (Math.abs(rawA - spoke) < stickyTol) a = spoke;
        return { x: last.x + Math.cos(a) * dist, y: last.y + Math.sin(a) * dist };
    }
    const m = snapPt(raw);
    return snapToExistingEndpoint(lvl, m) || snapOntoWall(lvl, m) || m;
}

/**
 * A wall end dropped beside another wall lands on it, so the junction is a
 * real corner for rooms to use (planCorners) rather than a near miss.
 */
function snapOntoWall(lvl, p, excludeWallId = null) {
    const R = snapRadiusM();
    if (R <= 0) return null;
    let best = null, bestD = R;
    for (const w of (lvl.walls || [])) {
        if (w.id === excludeWallId) continue;
        const proj = projectPointOntoSegment(p, w);
        const d = Math.hypot(p.x - proj.point.x, p.y - proj.point.y);
        if (d < bestD) { bestD = d; best = proj.point; }
    }
    return best;
}

// Reach for a room corner click: roomier than the wall-end merge radius,
// since a room corner can only go on a corner anyway.
function cornerSnapRadiusM() {
    return Math.min(1.0, Math.max(0.3, 28 / (_state?.zoom || PIXELS_PER_METRE_DEFAULT)));
}

/**
 * The corners a new room may use. Where the level has walls, only theirs:
 * a room drawn before the rules may sit off them, and must not lead a new
 * one off them too.
 */
function roomCorners(lvl) {
    return planCorners(lvl, { wallsOnly: (lvl.walls || []).length > 0 });
}

/**
 * Where a room-corner click lands: the nearest plan corner (or a corner of
 * the room being drawn), or null when none is in reach. A level with no
 * walls has no corners to hold to, so there the grid point stands.
 */
function roomDrawPoint(e, lvl) {
    const raw = clientToSvgModel(e);
    const corners = roomCorners(lvl).concat(_state.drawBuffer?.points || []);
    const hit = nearestPoint(corners, raw, cornerSnapRadiusM());
    if (hit || (lvl.walls || []).length) return hit;
    return snapPt(raw);
}

function addRoomCorner(p, lvl) {
    if (!p) {
        toast('warn', 'Not on a corner',
              'Room corners go on wall corners — click one of the marked points. '
              + 'For an open-plan split, draw an internal wall there first.');
        return;
    }
    const db = _state.drawBuffer;
    if (!db) {
        _state.drawBuffer = { points: [p], cur: p, curOk: true };
        renderOverlay();
        return;
    }
    const pts = db.points;
    if (samePoint(p, pts[pts.length - 1])) return;   // second click of a double-click
    if (pts.length >= 3 && samePoint(p, pts[0])) { finishRoom(); return; }
    if (pts.some(q => samePoint(p, q))) {
        toast('warn', 'Corner already used', 'Each corner goes into a room once.');
        return;
    }
    const legs = roomLegs(lvl, pts[pts.length - 1], p);
    const problem = legs.problem;
    if (problem) { toast('warn', "Can't go there", problem); return; }
    if (legs.via.some(q => pts.some(r => samePoint(q, r)))) {
        toast('warn', 'Corner already used', 'Following the walls there goes back over this room.');
        return;
    }
    pts.push(...legs.via, p);
    renderOverlay();
}

/**
 * The way from room corner a to b: along the walls when they join the two
 * (picking up any corner in between, so the outline stays on the walls),
 * else straight across — an open-plan split. `problem` says why neither
 * will do.
 */
function roomLegs(lvl, a, b, graph = wallGraph(lvl)) {
    const path = wallPath(graph, a, b);
    const straight = Math.hypot(b.x - a.x, b.y - a.y);
    const route = path && path.length <= straight * 1.5 + 0.3 ? path.points : [a, b];
    const pts = withCornersOnTheWay(lvl, route);
    for (let i = 1; i < pts.length; i++) {
        const problem = roomEdgeProblem(lvl, pts[i - 1], pts[i]);
        if (problem) return { via: [], problem };
    }
    return { via: pts.slice(1, -1), problem: null };
}

//: A corner this close to a room edge, between its ends, becomes a corner of
//: the room: the backend's own wall match allows 5 cm.
const CORNER_PICKUP_M = 0.05;

/**
 * The route with every corner lying on (within CORNER_PICKUP_M of) one of
 * its legs put in, in order. A long edge that passes a junction — a wall
 * meeting it, a neighbouring room's corner — then bends through that corner
 * rather than skimming past it a hair to one side, which reads as cutting
 * the wall or the room there, and neighbours end up sharing corners.
 */
function withCornersOnTheWay(lvl, route) {
    const corners = planCorners(lvl);
    const out = [route[0]];
    for (let i = 1; i < route.length; i++) {
        const a = route[i - 1], b = route[i];
        const L = Math.hypot(b.x - a.x, b.y - a.y);
        if (L > GEOM_TOL_M) {
            const ux = (b.x - a.x) / L, uy = (b.y - a.y) / L;
            corners
                .map(c => ({ c, t: (c.x - a.x) * ux + (c.y - a.y) * uy,
                             off: Math.abs((c.x - a.x) * uy - (c.y - a.y) * ux) }))
                .filter(o => o.t > GEOM_TOL_M && o.t < L - GEOM_TOL_M && o.off < CORNER_PICKUP_M
                             && !samePoint(o.c, a) && !samePoint(o.c, b))
                .sort((p, q) => p.t - q.t)
                .forEach(o => { if (!samePoint(o.c, out[out.length - 1])) out.push(o.c); });
        }
        if (!samePoint(b, out[out.length - 1])) out.push(b);
    }
    return out;
}

//: How far a wall end may be off the wall it was meant to meet and still be
//: joined to it: hand-drawn junctions miss by a few centimetres to ~20 cm.
const WALL_JOIN_REACH_M = 0.3;

function lineIntersection(a, b, c, d) {
    const rx = b.x - a.x, ry = b.y - a.y, sx = d.x - c.x, sy = d.y - c.y;
    const den = rx * sy - ry * sx;
    if (Math.abs(den) < 1e-9) return null;
    const t = ((c.x - a.x) * sy - (c.y - a.y) * sx) / den;
    return { x: a.x + rx * t, y: a.y + ry * t, u: ((c.x - a.x) * ry - (c.y - a.y) * rx) / den };
}

/**
 * Close the near misses where walls were meant to meet: a wall end short of
 * (or past) another wall is extended or trimmed along its own line onto it,
 * and two ends that nearly touch meet where their lines cross. Openings and
 * radiators on a wall whose start moved keep their place. Returns how many
 * wall ends moved.
 */
function joinWallEnds(lvl, reach = WALL_JOIN_REACH_M) {
    const walls = lvl.walls || [];
    const onWall = (p, w) => {
        const pr = projectPointOntoSegment(p, w);
        return Math.hypot(p.x - pr.point.x, p.y - pr.point.y) < GEOM_TOL_M;
    };
    const setEnd = (w, which, p) => {
        if (which === 1) {
            // Offsets run from the start, so a moved start shifts them back.
            const L = Math.hypot(w.x2 - w.x1, w.y2 - w.y1) || 1;
            const shift = ((p.x - w.x1) * (w.x2 - w.x1) + (p.y - w.y1) * (w.y2 - w.y1)) / L;
            for (const o of [...(lvl.openings || []), ...(lvl.radiators || [])]) {
                if (o.wall_id === w.id && typeof o.offset_m === 'number') {
                    o.offset_m = Math.max(0, o.offset_m - shift);
                }
            }
            w.x1 = p.x; w.y1 = p.y;
        } else { w.x2 = p.x; w.y2 = p.y; }
    };
    let moved = 0;
    for (const w of walls) {
        for (const which of [1, 2]) {
            const end = which === 1 ? { x: w.x1, y: w.y1 } : { x: w.x2, y: w.y2 };
            const others = walls.filter(o => o !== w);
            // Already joined: shares an end, or sits on another wall.
            if (others.some(o => wallEnds(o).some(e => samePoint(e, end)) || onWall(end, o))) continue;
            const [a, b] = wallEnds(w);
            let best = null, bestD = reach;
            for (const o of others) {
                const [c, d] = wallEnds(o);
                const x = lineIntersection(a, b, c, d);
                // An L: the other wall's nearby end meets this one where the
                // lines cross, so both ends move there.
                for (const [oe, ow] of [[c, 1], [d, 2]]) {
                    const dist = Math.hypot(oe.x - end.x, oe.y - end.y);
                    if (dist >= bestD) continue;
                    const at = x && Math.hypot(x.x - end.x, x.y - end.y) < reach
                        && Math.hypot(x.x - oe.x, x.y - oe.y) < reach ? { x: x.x, y: x.y } : oe;
                    best = { at, other: o, otherEnd: ow }; bestD = dist;
                }
                // A T: this end lands on the other wall's side.
                if (x && x.u > 0 && x.u < 1) {
                    const dist = Math.hypot(x.x - end.x, x.y - end.y);
                    if (dist < bestD) { best = { at: { x: x.x, y: x.y } }; bestD = dist; }
                }
            }
            if (!best) continue;
            // Never shrink a wall to nothing by joining it.
            const keeps = (wl, we) => {
                const [p, q] = wallEnds(wl), far = we === 1 ? q : p;
                return Math.hypot(far.x - best.at.x, far.y - best.at.y) >= 0.1;
            };
            if (!keeps(w, which) || (best.other && !keeps(best.other, best.otherEnd))) continue;
            setEnd(w, which, best.at);
            if (best.other) setEnd(best.other, best.otherEnd, best.at);
            moved++;
        }
    }
    return moved;
}

//: How far a hand-traced room corner may be from the wall corner it is moved to.
const ROOM_SNAP_REACH_M = 1.5;

/** The walls as a graph: each corner joined to the next corner along the same wall. */
function wallGraph(lvl) {
    const nodes = planCorners(lvl, { wallsOnly: true });
    const edges = nodes.map(() => []);
    for (const w of lvl.walls || []) {
        const [a, b] = wallEnds(w);
        const L = Math.hypot(b.x - a.x, b.y - a.y);
        if (L < CORNER_EPS_M) continue;
        const along = [];
        nodes.forEach((n, i) => {
            const t = ((n.x - a.x) * (b.x - a.x) + (n.y - a.y) * (b.y - a.y)) / L;
            const off = Math.abs((n.x - a.x) * (b.y - a.y) - (n.y - a.y) * (b.x - a.x)) / L;
            if (off < GEOM_TOL_M && t > -GEOM_TOL_M && t < L + GEOM_TOL_M) along.push([t, i]);
        });
        along.sort((p, q) => p[0] - q[0]);
        for (let k = 1; k < along.length; k++) {
            const [t0, i] = along[k - 1], [t1, j] = along[k];
            edges[i].push([j, t1 - t0]); edges[j].push([i, t1 - t0]);
        }
    }
    return { nodes, edges };
}

/** The corners along the walls from a to b (shortest way), or null. */
function wallPath(graph, a, b) {
    const ia = graph.nodes.findIndex(n => samePoint(n, a));
    const ib = graph.nodes.findIndex(n => samePoint(n, b));
    if (ia < 0 || ib < 0) return null;
    const dist = graph.nodes.map(() => Infinity), prev = graph.nodes.map(() => -1);
    const done = new Set();
    dist[ia] = 0;
    while (done.size < graph.nodes.length) {
        let u = -1;
        dist.forEach((d, i) => { if (!done.has(i) && d < Infinity && (u < 0 || d < dist[u])) u = i; });
        if (u < 0 || u === ib) break;
        done.add(u);
        for (const [v, len] of graph.edges[u]) {
            if (dist[u] + len < dist[v]) { dist[v] = dist[u] + len; prev[v] = u; }
        }
    }
    if (dist[ib] === Infinity) return null;
    const path = [];
    for (let i = ib; i >= 0; i = prev[i]) path.unshift(graph.nodes[i]);
    return { points: path, length: dist[ib] };
}

/**
 * Move each corner of a hand-traced room onto the nearest wall corner, then
 * run each edge along the walls between them, picking up any corner the
 * tracing cut across. An edge with no wall under it (an open-plan split)
 * stays straight. Returns why it couldn't, or null once the room is moved.
 */
function snapRoomToWalls(room, lvl) {
    const graph = wallGraph(lvl);
    const moved = [], missing = [];
    for (const [x, y] of room.polygon || []) {
        const c = nearestPoint(graph.nodes, { x, y }, ROOM_SNAP_REACH_M);
        if (!c) { missing.push([x, y]); continue; }
        if (!moved.length || !samePoint(c, moved[moved.length - 1])) moved.push(c);
    }
    if (moved.length > 1 && samePoint(moved[0], moved[moved.length - 1])) moved.pop();
    if (missing.length) {
        return `${missing.length} corner${missing.length === 1 ? ' is' : 's are'} more than `
            + `${ROOM_SNAP_REACH_M} m from any wall corner. Draw the missing walls, or redraw the room.`;
    }
    const outline = [];
    for (let i = 0; i < moved.length; i++) {
        const a = moved[i], b = moved[(i + 1) % moved.length];
        // Along the walls round a jog (not round another room), through any
        // corner on the way.
        const legs = roomLegs(lvl, a, b, graph);
        outline.push(a, ...(legs.problem ? [] : legs.via));
    }
    const pts = outline.filter((p, i) => !outline.slice(0, i).some(q => samePoint(p, q)));
    const problem = roomPolygonProblem(lvl, pts, room.id);
    if (problem) return problem;
    room.polygon = pts.map(p => [p.x, p.y]);
    return null;
}

/**
 * Snap every room on the level onto its walls, after joining the walls' near
 * misses. A room may only fit once its neighbour has moved, so this goes
 * round again while that still gets more rooms in.
 */
function snapLevelToWalls(lvl) {
    const joined = joinWallEnds(lvl);
    const pending = new Set((lvl.rooms || []).map(r => r.id));
    const problems = new Map();
    for (let progress = true; progress && pending.size;) {
        progress = false;
        for (const r of lvl.rooms || []) {
            if (!pending.has(r.id)) continue;
            const problem = snapRoomToWalls(r, lvl);
            if (problem) problems.set(r.id, problem);
            else { pending.delete(r.id); problems.delete(r.id); progress = true; }
        }
    }
    return { joined, snapped: (lvl.rooms || []).length - pending.size, problems };
}

// Where the backend has a trustworthy measurement, the plan prefers it over the
// clear-sky estimate — its calibration_ratio scales the solar field and badges.
// See modules/solar_impact.py and docs/heating.md.

/** Fetch measured solar impact once per modal session (heavy endpoint). */
async function loadSolarImpact() {
    if (_state.solarImpactLoaded) return;
    _state.solarImpactLoaded = true;      // one attempt per open
    try {
        const r = await fetch('/api/heating/solar-impact?days=14').then(r => r.json());
        if (!r || !r.success || !Array.isArray(r.rooms)) return;
        const map = new Map();
        for (const room of r.rooms) {
            if (room.room_id) map.set(String(room.room_id), room);
            // Fallback key: room name (plan ids and controller ids can differ
            // in manual mode)
            if (room.room_name && !map.has(room.room_name)) {
                map.set(String(room.room_name), room);
            }
        }
        _state.solarImpact = map;
        _fieldCache = new Map();          // ratios change the field
    } catch (e) {
        log.warn('solar-impact fetch failed', e);
    }
}

/** Measured entry for a plan room, or null. */
function roomSolarImpact(room) {
    const m = _state.solarImpact;
    if (!m) return null;
    return m.get(String(room.id)) || (room.name ? m.get(String(room.name)) : null) || null;
}

/**
 * Calibration ratio to apply to this room's modelled solar watts: the
 * measured/modelled ratio when confidence is medium+; 1.0 otherwise.
 */
function roomSolarRatio(room) {
    const e = roomSolarImpact(room);
    if (!e || !['medium', 'high'].includes(e.confidence)) return 1.0;
    const ratio = (e.solar || {}).calibration_ratio;
    if (!Number.isFinite(ratio) || ratio <= 0) return 1.0;
    return Math.min(3.0, ratio);
}

/** True when direct sun is on any of the room's windows right now. */
function roomSunlitNow(room, lvl) {
    const sd = _state.sunData;
    if (!sd?.points || !room.polygon || room.polygon.length < 3) return false;
    const nowMs = Date.now();
    let cur = null, best = Infinity;
    for (const pt of sd.points) {
        const d = Math.abs(new Date(pt.ts).getTime() - nowMs);
        if (d < best) { best = d; cur = pt; }
    }
    if (!cur || cur.el <= 0) return false;
    const planAz = ((cur.az + _state.plan.north_offset_deg) % 360 + 360) % 360;
    const sx = Math.sin(planAz * Math.PI / 180), sy = Math.cos(planAz * Math.PI / 180);
    const centroid = polygonCentroid(room.polygon);
    for (const { opening, mid } of openingsOnRoomBoundary(room, lvl)) {
        if (opening.kind !== 'window') continue;
        const wall = (lvl.walls || []).find(w => w.id === opening.wall_id);
        if (!wall) continue;
        const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) || 1;
        const ux = (wall.x2 - wall.x1) / wlen, uy = (wall.y2 - wall.y1) / wlen;
        let nx = -uy, ny = ux;
        if ((centroid.x - mid.x) * nx + (centroid.y - mid.y) * ny > 0) { nx = -nx; ny = -ny; }
        if (sx * nx + sy * ny > 0.15) return true;
    }
    return false;
}

/**
 * Light shafts for the sun at `pt`: every window whose exterior faces the sun
 * throws a parallelogram of light into its room, as deep as the window head
 * lets it reach at this elevation, clipped to the room and carrying motes.
 */
function sunShaftParts(lvl, pt) {
    if (!pt || pt.el <= 0) return '';
    const planAz = ((pt.az + _state.plan.north_offset_deg) % 360 + 360) % 360;
    const sx = Math.sin(planAz * Math.PI / 180), sy = Math.cos(planAz * Math.PI / 180);
    const lx = -sx, ly = -sy;                          // light travel, model space
    const tanEl = Math.tan(Math.max(4, pt.el) * Math.PI / 180);
    const lSvg = modelToSvg({ x: lx, y: ly });
    const angDeg = Math.atan2(lSvg.y, lSvg.x) * 180 / Math.PI;
    let defs = '', body = '';
    for (const room of lvl.rooms || []) {
        if (!room.polygon || room.polygon.length < 3) continue;
        const centroid = polygonCentroid(room.polygon);
        let shafts = '';
        for (const { opening, mid } of openingsOnRoomBoundary(room, lvl)) {
            if (opening.kind !== 'window') continue;
            const wall = (lvl.walls || []).find(w => w.id === opening.wall_id);
            if (!wall) continue;
            const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) || 1;
            const ux = (wall.x2 - wall.x1) / wlen, uy = (wall.y2 - wall.y1) / wlen;
            let nx = -uy, ny = ux;                     // exterior normal
            if ((centroid.x - mid.x) * nx + (centroid.y - mid.y) * ny > 0) { nx = -nx; ny = -ny; }
            const facing = sx * nx + sy * ny;
            if (facing <= 0.15) continue;
            // Head height ≈ sill (0.9 m) + window height; floor patch depth.
            const head = 0.9 + (opening.height_m || 1.2);
            const depth = Math.min(12, head / tanEl);
            const half = (opening.width_m || 1) / 2;
            const a = { x: mid.x - ux * half, y: mid.y - uy * half };
            const b = { x: mid.x + ux * half, y: mid.y + uy * half };
            const pts = [a, b, { x: b.x + lx * depth, y: b.y + ly * depth },
                               { x: a.x + lx * depth, y: a.y + ly * depth }].map(modelToSvg);
            const m0 = modelToSvg(mid), m1 = modelToSvg({ x: mid.x + lx * depth, y: mid.y + ly * depth });
            const gid = `fpShaftGrad-${opening.id}`;
            defs += `<linearGradient id="${gid}" gradientUnits="userSpaceOnUse"
                         x1="${m0.x}" y1="${m0.y}" x2="${m1.x}" y2="${m1.y}">
                       <stop offset="0%"   stop-color="rgba(251,191,36,${(0.30 + 0.2 * facing).toFixed(2)})"/>
                       <stop offset="100%" stop-color="rgba(251,191,36,0.03)"/>
                     </linearGradient>`;
            shafts += `<polygon class="fp-sun-shaft" points="${pts.map(p => `${p.x},${p.y}`).join(' ')}"
                                fill="url(#${gid})"/>`;
            // Motes start spread along the glazing and fall along the light.
            const n = Math.max(4, Math.min(14, Math.round(half * 2 * 6)));
            for (let i = 0; i < n; i++) {
                const t = Math.random();
                const s0 = modelToSvg({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
                const dur = (2.2 + Math.random() * 2.4).toFixed(2);
                const delay = (-Math.random() * 4.5).toFixed(2);
                const r = (0.025 + Math.random() * 0.04).toFixed(3);
                const dist = (depth * (0.55 + Math.random() * 0.45)).toFixed(2);
                const drift = ((Math.random() - 0.5) * 0.25).toFixed(3);
                shafts += `<g transform="translate(${s0.x} ${s0.y}) rotate(${angDeg.toFixed(2)})">
                    <circle class="fp-sun-particle" r="${r}"
                        style="--fp-d:${dist}px; --fp-cy:${drift}px;
                               animation-duration:${dur}s; animation-delay:${delay}s;"/></g>`;
            }
        }
        if (!shafts) continue;
        const cid = `fpShaftClip-${room.id}`;
        defs += `<clipPath id="${cid}"><path d="${polygonToPath(room.polygon)}"/></clipPath>`;
        body += `<g class="fp-sun-shafts" clip-path="url(#${cid})">${shafts}</g>`;
    }
    return body ? `<defs>${defs}</defs>${body}` : '';
}

// A per-room scalar heat-coverage field on a coarse grid. One field drives the
// heat map, the isotherm contours and the cold-zone tint, so all three agree by
// construction. Formula and the meaning of COLD_THRESH: docs/heating.md
// § Thermal overlays.

const COLD_THRESH = Math.exp(-1);   // coverage value at d = r₀
let _fieldCache = new Map();        // room.id → field entry (reset per open)

/** Openings whose midpoint lies on this room's polygon boundary (±0.3 m). */
function openingsOnRoomBoundary(room, lvl) {
    const res = [];
    for (const opening of (lvl.openings || [])) {
        const wall = (lvl.walls || []).find(w => w.id === opening.wall_id);
        if (!wall) continue;
        const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) || 1;
        const ux = (wall.x2 - wall.x1) / wlen, uy = (wall.y2 - wall.y1) / wlen;
        const mx = wall.x1 + ux * ((opening.offset_m || 0) + (opening.width_m || 0) / 2);
        const my = wall.y1 + uy * ((opening.offset_m || 0) + (opening.width_m || 0) / 2);
        for (let i = 0; i < room.polygon.length; i++) {
            const a = room.polygon[i], b = room.polygon[(i + 1) % room.polygon.length];
            const ex = b[0] - a[0], ey = b[1] - a[1];
            const el2 = ex * ex + ey * ey || 1;
            const t = Math.max(0, Math.min(1, ((mx - a[0]) * ex + (my - a[1]) * ey) / el2));
            if (Math.hypot(mx - (a[0] + t * ex), my - (a[1] + t * ey)) < 0.3) {
                res.push({ opening, mid: { x: mx, y: my } });
                break;
            }
        }
    }
    return res;
}

// Clear-sky heuristic, for insight rather than engineering: average watts
// admitted per window over daylight hours. See docs/heating.md.

const SOLAR_SHGC = { single: 0.85, double: 0.72, triple: 0.55 };
const SOLAR_IRRADIANCE_WM2 = 500;

/** Minutes today with direct sun on this window's EXTERIOR face. */
function windowSunMinutes(opening, wall, roomCentroid) {
    const sd = _state.sunData;
    if (!sd?.points) return 0;
    const stepMin = sd.step_minutes || 20;
    const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) || 1;
    const ux = (wall.x2 - wall.x1) / wlen, uy = (wall.y2 - wall.y1) / wlen;
    const nx = -uy, ny = ux;
    const wmx = wall.x1 + ux * ((opening.offset_m || 0) + (opening.width_m || 0) / 2);
    const wmy = wall.y1 + uy * ((opening.offset_m || 0) + (opening.width_m || 0) / 2);
    // Exterior normal points AWAY from the room's centroid
    const dot = (roomCentroid.x - wmx) * nx + (roomCentroid.y - wmy) * ny;
    const ex = dot > 0 ? -nx : nx, ey = dot > 0 ? -ny : ny;
    let mins = 0;
    for (const pt of sd.points) {
        if (pt.el <= 0) continue;
        const planAz = ((pt.az + _state.plan.north_offset_deg) % 360 + 360) % 360;
        const sx = Math.sin(planAz * Math.PI / 180), sy = Math.cos(planAz * Math.PI / 180);
        if (sx * ex + sy * ey > 0.15) mins += stepMin;
    }
    return mins;
}

/** Daylight minutes in today's sun curve. */
function daylightMinutes() {
    const sd = _state.sunData;
    if (!sd?.points) return 0;
    return sd.points.filter(p => p.el > 0).length * (sd.step_minutes || 20);
}

/** Daylight-averaged solar watts admitted through a window. */
function windowSolarWatts(opening, wall, roomCentroid) {
    if (opening.kind !== 'window') return 0;
    const daylight = daylightMinutes();
    if (!daylight) return 0;
    const mins = windowSunMinutes(opening, wall, roomCentroid);
    if (!mins) return 0;
    const area = (opening.width_m || 1) * (opening.height_m || 1.2);
    const g = SOLAR_SHGC[opening.glazing] ?? SOLAR_SHGC.double;
    return SOLAR_IRRADIANCE_WM2 * g * area * (mins / daylight);
}

/**
 * Compute (or fetch from cache) the heat-coverage field for a room. The
 * cache key covers every input that shapes the field, so edits invalidate
 * naturally and pan/zoom re-renders are free. `_state.thermalMode` decides
 * which sources drive the field: radiators, radiators + solar, or solar
 * gain alone (for judging what the sun does to the house by itself).
 */
function roomHeatField(room, lvl, heatFlux) {
    const mode = _state.thermalMode || 'rad';
    const rads = lvl.radiators.filter(r => r.room_id === room.id);
    const bops = openingsOnRoomBoundary(room, lvl);
    const key = JSON.stringify([
        room.polygon, heatFlux, mode,
        mode !== 'rad' ? [_state.plan.north_offset_deg, _state.sunData?.sunrise || null,
                          roomSolarRatio(room)] : null,
        rads.map(r => [r.wall_id, r.offset_m, r.x, r.y, r.length_m, r.watts_at_dt50]),
        bops.map(b => [b.opening.id, b.opening.kind, b.opening.width_m, b.opening.height_m,
                       b.opening.glazing, b.opening.door_type, openingContactState(b.opening, lvl)]),
    ]);
    const hit = _fieldCache.get(room.id);
    if (hit && hit.key === key) return hit;

    const xs = room.polygon.map(p => p[0]), ys = room.polygon.map(p => p[1]);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const h = Math.max(0.08, Math.min(0.30, Math.max(maxX - minX, maxY - minY) / 56));
    const nx = Math.max(2, Math.ceil((maxX - minX) / h) + 2);
    const ny = Math.max(2, Math.ceil((maxY - minY) / h) + 2);
    const x0 = minX - h, y0 = minY - h;   // one cell of padding

    const srcs = mode === 'sun' ? [] : rads.map(r => {
        const c = radiatorCenter(r, lvl);
        const r0 = Math.sqrt(Math.max(50, r.watts_at_dt50 || 0) / (heatFlux * Math.PI));
        return { x: c.x, y: c.y, r0 };
    });
    // Solar sources: each sun-facing window becomes a heat source at its
    // midpoint, sized by the daylight-averaged watts it admits — scaled by
    // the room's measured calibration ratio when telemetry has one.
    if (mode !== 'rad') {
        const centroid = polygonCentroid(room.polygon);
        const ratio = roomSolarRatio(room);
        for (const { opening, mid } of bops) {
            const wall = (lvl.walls || []).find(w => w.id === opening.wall_id);
            if (!wall) continue;
            const sw = windowSolarWatts(opening, wall, centroid) * ratio;
            if (sw < 15) continue;
            srcs.push({ x: mid.x, y: mid.y, r0: Math.sqrt(sw / (heatFlux * Math.PI)) });
        }
    }
    const drafts = bops.map(({ opening, mid }) => {
        const isOpen = openingContactState(opening, lvl);
        const glazing = { single: 1.45, double: 1.0, triple: 0.60 }[opening.glazing] ?? 1.0;
        const doorF = opening.kind === 'door'
            ? ({ external: 1.15, internal: 0.55 }[opening.door_type] ?? 0.8)
            : 1.0;
        const base = isOpen === true ? 0.85 : isOpen === false ? 0.12 : 0.35;
        const amp = Math.min(1.1, base * glazing * doorF);
        const rD = Math.max(0.9, (opening.width_m || 1) * 1.3) * (isOpen === true ? 1.5 : 1.0);
        return { x: mid.x, y: mid.y, amp, rD };
    });

    const data = new Float32Array(nx * ny);
    const inside = new Uint8Array(nx * ny);
    for (let j = 0; j < ny; j++) {
        for (let i = 0; i < nx; i++) {
            const px = x0 + (i + 0.5) * h, py = y0 + (j + 0.5) * h;
            if (!pointInPolygon({ x: px, y: py }, room.polygon)) continue;
            const k = j * nx + i;
            inside[k] = 1;
            let v = 0;
            for (const s of srcs) {
                const d2 = (px - s.x) ** 2 + (py - s.y) ** 2;
                v += Math.exp(-d2 / (s.r0 * s.r0));
            }
            for (const d of drafts) {
                const d2 = (px - d.x) ** 2 + (py - d.y) ** 2;
                v -= d.amp * Math.exp(-d2 / (d.rD * d.rD * 0.35));
            }
            data[k] = v;
        }
    }

    const entry = { key, nx, ny, x0, y0, h, data, inside, urls: {} };
    _fieldCache.set(room.id, entry);
    return entry;
}

/** Warm colour ramp: amber → orange → red → deep red. Returns [r,g,b]. */
/** Signal ramp: red (unusable) through amber to green (strong). */
function rssiRgb(t) {
    const stops = [[0, 214, 48, 49], [0.33, 225, 112, 85], [0.55, 253, 203, 110],
                   [0.78, 162, 196, 60], [1, 0, 184, 148]];
    for (let i = 1; i < stops.length; i++) {
        if (t <= stops[i][0]) {
            const [t0, r0, g0, b0] = stops[i - 1], [t1, r1, g1, b1] = stops[i];
            const k = (t - t0) / (t1 - t0);
            return [r0 + (r1 - r0) * k, g0 + (g1 - g0) * k, b0 + (b1 - b0) * k].map(Math.round);
        }
    }
    const last = stops[stops.length - 1];
    return [last[1], last[2], last[3]];
}

function heatRgb(t) {
    const stops = [
        [0.00, 251, 191, 36],
        [0.45, 249, 115, 22],
        [0.90, 239, 68, 68],
        [1.35, 153, 27, 27],
    ];
    if (t <= stops[0][0]) return [stops[0][1], stops[0][2], stops[0][3]];
    for (let i = 1; i < stops.length; i++) {
        if (t <= stops[i][0]) {
            const [t0, r0, g0, b0] = stops[i - 1];
            const [t1, r1, g1, b1] = stops[i];
            const f = (t - t0) / (t1 - t0);
            return [r0 + (r1 - r0) * f, g0 + (g1 - g0) * f, b0 + (b1 - b0) * f].map(Math.round);
        }
    }
    const last = stops[stops.length - 1];
    return [last[1], last[2], last[3]];
}

/**
 * Rasterise a field to a smooth PNG data-URI (bilinear-upscaled ×6). Cached
 * per (mode, intensity) on the field entry, so pan/zoom re-renders reuse the
 * exact same string. Placed as an SVG <image> in model coordinates and
 * clipped to the room polygon.
 */
function fieldToImage(f, mode, intensity) {
    const cacheKey = `${mode}:${intensity.toFixed(2)}`;
    if (f.urls[cacheKey]) return f.urls[cacheKey];
    const { nx, ny, data, inside } = f;
    const cnv = document.createElement('canvas');
    cnv.width = nx; cnv.height = ny;
    const ctx = cnv.getContext('2d');
    const img = ctx.createImageData(nx, ny);
    for (let j = 0; j < ny; j++) {
        for (let i = 0; i < nx; i++) {
            const k = j * nx + i;
            if (!inside[k]) continue;              // transparent outside the room
            const px = (((ny - 1 - j) * nx) + i) * 4;  // canvas row 0 = max model y
            const v = data[k];
            let r, g, b, a;
            if (mode === 'rssi') {
                // −100 dBm red, −85 amber, −70 lime, −55 green.
                const t = Math.max(0, Math.min(1, (v + 100) / 45));
                [r, g, b] = rssiRgb(t);
                a = 0.45;
            } else if (mode === 'delta') {
                // dB gained (green) or lost (red) since a snapshot; within
                // 1 dB is no change worth a colour, and ±15 dB is full.
                const t = Math.max(-1, Math.min(1, v / 15));
                [r, g, b] = t >= 0 ? [22, 163, 74] : [220, 38, 38];
                a = Math.abs(v) < 1 ? 0 : Math.min(0.6, 0.12 + Math.abs(t) * 0.5);
            } else if (mode === 'heat') {
                const t = Math.max(0, Math.min(1.35, v));
                a = Math.max(0, Math.min(0.60, (t - 0.05) * 0.55)) * (0.55 + 0.45 * intensity);
                [r, g, b] = heatRgb(t);
            } else { // 'cold'
                a = Math.max(0, Math.min(0.45, (COLD_THRESH - v) * 1.5));
                r = 59; g = 130; b = 246;
            }
            img.data[px] = r; img.data[px + 1] = g; img.data[px + 2] = b;
            img.data[px + 3] = Math.round(a * 255);
        }
    }
    ctx.putImageData(img, 0, 0);
    const up = document.createElement('canvas');
    up.width = nx * 6; up.height = ny * 6;
    const uctx = up.getContext('2d');
    uctx.imageSmoothingEnabled = true;
    uctx.imageSmoothingQuality = 'high';
    uctx.drawImage(cnv, 0, 0, up.width, up.height);
    f.urls[cacheKey] = up.toDataURL('image/png');
    return f.urls[cacheKey];
}

/** Marching squares: iso-line segments of the field at `level`, model coords. */
function marchingSquares(f, level) {
    const { nx, ny, data, x0, y0, h } = f;
    const segs = [];
    const V = (i, j) => data[j * nx + i] - level;
    for (let j = 0; j < ny - 1; j++) {
        for (let i = 0; i < nx - 1; i++) {
            const c = [V(i, j), V(i + 1, j), V(i + 1, j + 1), V(i, j + 1)];
            const cx = [x0 + (i + 0.5) * h, x0 + (i + 1.5) * h, x0 + (i + 1.5) * h, x0 + (i + 0.5) * h];
            const cy = [y0 + (j + 0.5) * h, y0 + (j + 0.5) * h, y0 + (j + 1.5) * h, y0 + (j + 1.5) * h];
            const pts = [];
            for (let e = 0; e < 4; e++) {
                const u = c[e], w = c[(e + 1) % 4];
                if ((u < 0) !== (w < 0)) {
                    const t = u / (u - w);
                    pts.push({
                        x: cx[e] + (cx[(e + 1) % 4] - cx[e]) * t,
                        y: cy[e] + (cy[(e + 1) % 4] - cy[e]) * t,
                    });
                }
            }
            if (pts.length === 2) segs.push([pts[0], pts[1]]);
            else if (pts.length === 4) { segs.push([pts[0], pts[1]]); segs.push([pts[2], pts[3]]); }
        }
    }
    return segs;
}

/** Join marching-squares segments into polylines so they can be smoothed. */
function chainSegments(segs) {
    const keyOf = p => `${Math.round(p.x * 2000)}:${Math.round(p.y * 2000)}`;
    const adj = new Map();
    segs.forEach((s, i) => {
        for (const end of [0, 1]) {
            const k = keyOf(s[end]);
            if (!adj.has(k)) adj.set(k, []);
            adj.get(k).push({ i, end });
        }
    });
    const used = new Array(segs.length).fill(false);
    const chains = [];
    for (let start = 0; start < segs.length; start++) {
        if (used[start]) continue;
        used[start] = true;
        const pts = [segs[start][0], segs[start][1]];
        for (const dir of [1, 0]) {          // extend the tail, then the head
            let guard = segs.length;
            while (guard-- > 0) {
                const tip = dir === 1 ? pts[pts.length - 1] : pts[0];
                const next = (adj.get(keyOf(tip)) || []).find(c => !used[c.i]);
                if (!next) break;
                used[next.i] = true;
                const np = segs[next.i][next.end === 0 ? 1 : 0];
                if (dir === 1) pts.push(np); else pts.unshift(np);
            }
        }
        const closed = pts.length > 2 && keyOf(pts[0]) === keyOf(pts[pts.length - 1]);
        if (closed) pts.pop();
        chains.push({ points: pts, closed });
    }
    return chains;
}

/** Model-space polyline → smoothed SVG path (quadratic through midpoints). */
function smoothPathSvg(pts, closed) {
    const sv = pts.map(p => modelToSvg(p));
    if (sv.length < 2) return '';
    const fx = n => n.toFixed(3);
    if (sv.length === 2) return `M ${fx(sv[0].x)} ${fx(sv[0].y)} L ${fx(sv[1].x)} ${fx(sv[1].y)}`;
    const mid = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
    let d;
    if (closed) {
        const n = sv.length;
        const m0 = mid(sv[n - 1], sv[0]);
        d = `M ${fx(m0.x)} ${fx(m0.y)}`;
        for (let k = 0; k < n; k++) {
            const v = sv[k], m = mid(sv[k], sv[(k + 1) % n]);
            d += ` Q ${fx(v.x)} ${fx(v.y)} ${fx(m.x)} ${fx(m.y)}`;
        }
        d += ' Z';
    } else {
        d = `M ${fx(sv[0].x)} ${fx(sv[0].y)}`;
        for (let k = 1; k < sv.length - 1; k++) {
            const m = mid(sv[k], sv[k + 1]);
            d += ` Q ${fx(sv[k].x)} ${fx(sv[k].y)} ${fx(m.x)} ${fx(m.y)}`;
        }
        d += ` L ${fx(sv[sv.length - 1].x)} ${fx(sv[sv.length - 1].y)}`;
    }
    return d;
}

/** Cached smoothed contour path of the field at `level` (SVG d string). */
function fieldContourPath(f, level) {
    const cacheKey = `ct:${level.toFixed(3)}`;
    if (f.urls[cacheKey] !== undefined) return f.urls[cacheKey];
    const chains = chainSegments(marchingSquares(f, level));
    f.urls[cacheKey] = chains.map(ch => smoothPathSvg(ch.points, ch.closed)).join(' ');
    return f.urls[cacheKey];
}

/** Coldest in-room cell centre, or null if the whole room is above threshold. */
function fieldColdSpot(f) {
    if (f.coldSpot !== undefined) return f.coldSpot;
    let minV = Infinity, at = null;
    for (let j = 0; j < f.ny; j++) {
        for (let i = 0; i < f.nx; i++) {
            const k = j * f.nx + i;
            if (!f.inside[k] || f.data[k] >= minV) continue;
            minV = f.data[k];
            at = { x: f.x0 + (i + 0.5) * f.h, y: f.y0 + (j + 0.5) * f.h };
        }
    }
    f.coldSpot = (at && minV < COLD_THRESH) ? at : null;
    return f.coldSpot;
}

/** Thermal overlay parts: heat-map images, isotherm contours, sensor rings. */
function renderThermalParts(lvl) {
    const defs = [];
    const out = [];
    const mode = _state.thermalMode || 'rad';
    const maxWatts = Math.max(...lvl.radiators.map(r => r.watts_at_dt50 || 0), 100);

    for (const room of lvl.rooms) {
        if (!room.polygon || room.polygon.length < 3) continue;
        const rads = lvl.radiators.filter(r => r.room_id === room.id);
        // In solar modes, rooms without radiators still heat via their windows
        if (rads.length === 0 && mode === 'rad') continue;
        const totalWatts = rads.reduce((s, r) => s + (r.watts_at_dt50 || 0), 0);
        const intensity = rads.length ? Math.min(1, totalWatts / maxWatts) : 0.8;

        const f = roomHeatField(room, lvl, _state.heatFluxWm2 || 50);
        const uid = room.id.replace(/[^a-z0-9]/gi, '_');
        defs.push(`<clipPath id="tc_${uid}"><path d="${polygonToPath(room.polygon)}"/></clipPath>`);

        out.push(`<image href="${fieldToImage(f, 'heat', intensity)}"
                        x="${f.x0}" y="${-(f.y0 + f.ny * f.h)}"
                        width="${f.nx * f.h}" height="${f.ny * f.h}"
                        clip-path="url(#tc_${uid})" preserveAspectRatio="none"
                        pointer-events="none"/>`);

        if (_state.showContours) {
            const CONTOUR_LEVELS = [
                { level: 1.05, opacity: 0.60 },
                { level: 0.75, opacity: 0.42 },
                { level: 0.50, opacity: 0.28 },
            ];
            for (const { level, opacity } of CONTOUR_LEVELS) {
                const d = fieldContourPath(f, level);
                if (!d) continue;
                out.push(`<path class="fp-contour" d="${d}" clip-path="url(#tc_${uid})"
                                style="opacity:${(opacity * (0.6 + 0.4 * intensity)).toFixed(2)}"
                                pointer-events="none"/>`);
            }
        }
    }

    // Per-window solar-gain badges — the numbers that justify reflective
    // film or shading: daylight-averaged watts admitted through each window.
    if (mode !== 'rad' && _state.sunData) {
        for (const opening of (lvl.openings || [])) {
            if (opening.kind !== 'window') continue;
            const wall = lvl.walls.find(w => w.id === opening.wall_id);
            if (!wall) continue;
            const room = (lvl.rooms || []).find(r =>
                r.polygon?.length >= 3 && openingsOnRoomBoundary(r, lvl).some(b => b.opening.id === opening.id));
            if (!room) continue;
            const centroid = polygonCentroid(room.polygon);
            const ratio = roomSolarRatio(room);
            const measured = ratio !== 1.0;
            const watts = windowSolarWatts(opening, wall, centroid) * ratio;
            if (watts < 15) continue;
            const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) || 1;
            const ux = (wall.x2 - wall.x1) / wlen, uy = (wall.y2 - wall.y1) / wlen;
            const wmx = wall.x1 + ux * (opening.offset_m + opening.width_m / 2);
            const wmy = wall.y1 + uy * (opening.offset_m + opening.width_m / 2);
            // Label offset toward the exterior side
            let nx = -uy, ny = ux;
            if ((centroid.x - wmx) * nx + (centroid.y - wmy) * ny > 0) { nx = -nx; ny = -ny; }
            const lp = modelToSvg({ x: wmx + nx * 0.42, y: wmy + ny * 0.42 });
            out.push(`<text class="fp-solar-badge ${measured ? 'fp-solar-badge-measured' : ''}"
                            x="${lp.x}" y="${lp.y + 0.06}" font-size="0.15"
                            text-anchor="middle" pointer-events="none">☀ ${Math.round(watts)}W${measured ? ' ✓' : ''}</text>`);
        }
    }

    // "Solar overheat now" — the live model-vs-measurement cross-check: the
    // room's sensor reads above target while the sun is on its windows.
    if ((mode !== 'rad' || _state.showSun) && _state.sunData) {
        for (const room of lvl.rooms) {
            if (!room.target_temp || !room.polygon || room.polygon.length < 3) continue;
            const sensor = lvl.sensors.find(s => s.room_id === room.id && s.ieee);
            const dev = sensor ? (_availableDevices.sensors || []).find(d => d.ieee === sensor.ieee) : null;
            const temp = dev && dev.temperature != null ? Number(dev.temperature) : null;
            if (temp === null || temp <= room.target_temp + 0.5) continue;
            if (!roomSunlitNow(room, lvl)) continue;
            const sc = modelToSvg(polygonCentroid(room.polygon));
            out.push(`<text class="fp-overheat-badge" x="${sc.x}" y="${sc.y - 0.42}" font-size="0.16"
                            text-anchor="middle" pointer-events="none">☀ +${(temp - room.target_temp).toFixed(1)}° solar overheat</text>`);
        }
    }

    // Thermostat rings on sensors, with the live reading when a device is bound
    for (const s of lvl.sensors) {
        const sp = modelToSvg({ x: s.x ?? 0, y: s.y ?? 0 });
        const dev = s.ieee ? (_availableDevices.sensors || []).find(d => d.ieee === s.ieee) : null;
        const temp = dev && dev.temperature != null ? Number(dev.temperature) : null;
        out.push(`
          <circle class="fp-thermo-ring" cx="${sp.x}" cy="${sp.y}" r="0.40" pointer-events="none"/>
          <circle class="fp-thermo-core" cx="${sp.x}" cy="${sp.y}" r="0.19" pointer-events="none"/>
          ${temp != null ? `<text class="fp-thermo-temp" x="${sp.x}" y="${sp.y - 0.52}"
                font-size="0.17" text-anchor="middle"
                pointer-events="none">${temp.toFixed(1)}°</text>` : ''}`);
    }

    return (defs.length || out.length) ? [`<defs>${defs.join('')}</defs>`, ...out] : [];
}

/** Cold-zone parts: below-threshold tint, comfort boundary, labels, badges. */
function renderColdParts(lvl) {
    const out = [];
    const heatFlux = _state.heatFluxWm2 || 50;

    for (const room of lvl.rooms) {
        if (!room.polygon || room.polygon.length < 3) continue;
        const roomPath = polygonToPath(room.polygon);
        const rads = lvl.radiators.filter(r => r.room_id === room.id);

        // In radiators-only mode an unheated room is trivially all-cold; in
        // solar modes its windows may still heat it, so fall through to the
        // field-based tint instead.
        if (rads.length === 0 && (_state.thermalMode || 'rad') === 'rad') {
            const c = modelToSvg(polygonCentroid(room.polygon));
            out.push(`<path d="${roomPath}" class="fp-cold-noheat" pointer-events="none"/>`);
            out.push(`<text class="fp-cold-label" x="${c.x}" y="${c.y + 0.60}" font-size="0.17"
                            text-anchor="middle" pointer-events="none">no heating</text>`);
            continue;
        }

        const f = roomHeatField(room, lvl, heatFlux);
        const uid = room.id.replace(/[^a-z0-9]/gi, '_');   // clip defined by thermal pass

        // Cold tint — everywhere coverage is below the comfort threshold
        out.push(`<image href="${fieldToImage(f, 'cold', 1)}"
                        x="${f.x0}" y="${-(f.y0 + f.ny * f.h)}"
                        width="${f.nx * f.h}" height="${f.ny * f.h}"
                        clip-path="url(#tc_${uid})" preserveAspectRatio="none"
                        pointer-events="none"/>`);

        // Comfort boundary — the C = e⁻¹ isoline (≈ heated radius, bent by drafts)
        const d = fieldContourPath(f, COLD_THRESH);
        if (d) {
            out.push(`<path class="fp-cold-boundary" d="${d}" clip-path="url(#tc_${uid})"
                            pointer-events="none"/>`);
        }

        // Target-temp label anchored at the coldest spot of the room
        const spot = fieldColdSpot(f);
        if (spot && room.target_temp) {
            const lp = modelToSvg(spot);
            out.push(`<text class="fp-cold-label" x="${lp.x}" y="${lp.y}" font-size="0.17"
                            text-anchor="middle" pointer-events="none">&lt;${room.target_temp}°C</text>`);
        }
    }

    // Open/closed badges on openings with a bound contact sensor
    for (const opening of (lvl.openings || [])) {
        const wall = lvl.walls.find(w => w.id === opening.wall_id);
        if (!wall) continue;
        const isOpen = openingContactState(opening, lvl);
        if (isOpen === null) continue;
        const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) || 1;
        const ux = (wall.x2 - wall.x1) / wlen, uy = (wall.y2 - wall.y1) / wlen;
        const wsvg = modelToSvg({
            x: wall.x1 + ux * (opening.offset_m + opening.width_m / 2),
            y: wall.y1 + uy * (opening.offset_m + opening.width_m / 2),
        });
        out.push(`<text x="${wsvg.x}" y="${wsvg.y - 0.28}" font-size="0.15" text-anchor="middle"
                        fill="${isOpen ? 'rgba(220,38,38,0.90)' : 'rgba(22,163,74,0.85)'}"
                        pointer-events="none">${isOpen ? 'open' : 'closed'}</text>`);
    }

    return out;
}

// circuit management

const CIRCUIT_BADGE_COLOURS = [
    '#3b82f6','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899','#14b8a6','#f97316'
];

function renderCircuitList() {
    const wrap = document.getElementById('fpCircuitList');
    if (!wrap) return;
    const circuits = _state.plan.circuits || [];
    if (!circuits.length) {
        wrap.innerHTML = '<div class="text-muted small fst-italic">No circuits yet.</div>';
        return;
    }
    // Count rooms per circuit across all levels
    const roomCounts = {};
    (_state.plan.levels || []).forEach(lvl => {
        (lvl.rooms || []).forEach(r => {
            if (r.circuit_id) roomCounts[r.circuit_id] = (roomCounts[r.circuit_id] || 0) + 1;
        });
    });
    wrap.innerHTML = circuits.map((c, i) => {
        const colour = CIRCUIT_BADGE_COLOURS[i % CIRCUIT_BADGE_COLOURS.length];
        const cnt = roomCounts[c.id] || 0;
        return `<div class="d-flex align-items-center gap-1 mb-1" data-circuit-id="${c.id}">
          <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${colour};flex-shrink:0"></span>
          <span class="small flex-grow-1 text-truncate" title="${escapeHtml(c.id)}">${escapeHtml(c.name)}<span class="text-muted ms-1">(${cnt})</span></span>
          <button class="btn btn-sm btn-link p-0 text-primary" data-action="edit-circuit" data-circuit-id="${c.id}" title="Edit"><i class="fas fa-pencil-alt fa-xs"></i></button>
          <button class="btn btn-sm btn-link p-0 text-danger" data-action="delete-circuit" data-circuit-id="${c.id}" title="Delete"><i class="fas fa-trash fa-xs"></i></button>
        </div>`;
    }).join('');

    wrap.querySelectorAll('[data-action="edit-circuit"]').forEach(btn => {
        btn.addEventListener('click', () => editCircuit(btn.dataset.circuitId));
    });
    wrap.querySelectorAll('[data-action="delete-circuit"]').forEach(btn => {
        btn.addEventListener('click', () => deleteCircuit(btn.dataset.circuitId));
    });
}

async function addCircuit() {
    const name = await window.zbmPrompt({
        title: 'Add circuit',
        label: 'Circuit name (e.g. "Living"):',
        confirmText: 'Add'
    });
    if (!name || !name.trim()) return;
    const id = 'circuit_' + Math.random().toString(36).slice(2, 8);
    _state.plan.circuits = _state.plan.circuits || [];
    _state.plan.circuits.push({ id, name: name.trim(), receiver_command: 'thermostat' });
    renderCircuitList();
    renderScene();
    editCircuit(id);
}

function editCircuit(circuitId) {
    const c = (_state.plan.circuits || []).find(x => x.id === circuitId);
    if (!c) return;

    // Filter out receivers already assigned to *other* circuits
    const usedRecv = new Set((_state.plan.circuits || [])
        .filter(x => x.id !== circuitId && x.receiver_ieee)
        .map(x => x.receiver_ieee));

    const receivers = _availableDevices.receivers || [];
    const receiverOpts = ['<option value="">— No receiver —</option>']
        .concat(receivers
            .filter(r => !usedRecv.has(r.ieee) || r.ieee === c.receiver_ieee)
            .map(r => {
                const modeStr = r.system_mode ? ` [${r.system_mode}]` : '';
                return `<option value="${escapeAttr(r.ieee)}" ${c.receiver_ieee === r.ieee ? 'selected' : ''}>
                    ${escapeHtml(r.name)}${modeStr} (${escapeHtml(r.ieee.slice(-8))})
                </option>`;
            })
        ).join('');

    const propsDiv = document.getElementById('fpProps');
    propsDiv.innerHTML = `
      <div class="text-muted small text-uppercase mb-2">Circuit</div>
      <div class="mb-2"><label class="form-label small">Name</label>
        <input class="form-control form-control-sm" id="fpCircuitName" value="${escapeAttr(c.name)}"/></div>
      <div class="mb-2"><label class="form-label small">Boiler receiver</label>
        <select class="form-select form-select-sm" id="fpCircuitReceiver">${receiverOpts}</select>
        ${receivers.length === 0 ? '<div class="form-text small text-warning"><i class="fas fa-info-circle me-1"></i>No receivers found. Configure them in the Heating Controller, then reopen the floor plan editor.</div>' : ''}
      </div>
      <div class="mb-2"><label class="form-label small">Receiver command</label>
        <select class="form-select form-select-sm" id="fpCircuitCmd">
          <option value="thermostat" ${c.receiver_command === 'thermostat' ? 'selected' : ''}>thermostat</option>
          <option value="switch" ${c.receiver_command === 'switch' ? 'selected' : ''}>switch</option>
        </select></div>
      <div class="mb-2 form-check form-switch small">
        <input type="checkbox" class="form-check-input" id="fpCircuitOH" ${c.operating_hours ? 'checked' : ''}/>
        <label class="form-check-label" for="fpCircuitOH">Respect operating hours</label></div>
      <div class="mb-2 form-check form-switch small">
        <input type="checkbox" class="form-check-input" id="fpCircuitWS" ${c.weather_suppression ? 'checked' : ''}/>
        <label class="form-check-label" for="fpCircuitWS">Weather suppression</label></div>
      ${c.receiver_command === 'thermostat' ? `
      <div class="row g-1 mb-2">
        <div class="col-6"><label class="form-label small text-muted mb-0">Call setpoint °C</label>
          <input type="number" step="0.5" min="4" max="90" class="form-control form-control-sm" id="fpCircuitCallSp" value="${c.receiver_call_setpoint ?? 30}"/></div>
        <div class="col-6"><label class="form-label small text-muted mb-0">Idle setpoint °C</label>
          <input type="number" step="0.5" min="4" max="90" class="form-control form-control-sm" id="fpCircuitIdleSp" value="${c.receiver_idle_setpoint ?? 7}"/></div>
      </div>` : ''}
      <div class="small text-muted mb-2">ID: <code>${escapeHtml(c.id)}</code></div>
      <div class="small text-info"><i class="fas fa-info-circle me-1"></i>Assign rooms to this circuit via the room properties panel.</div>`;

    const update = () => {
        c.name = document.getElementById('fpCircuitName').value.trim() || c.name;
        const recVal = document.getElementById('fpCircuitReceiver').value;
        if (recVal) c.receiver_ieee = recVal; else delete c.receiver_ieee;
        const prevCmd = c.receiver_command;
        c.receiver_command = document.getElementById('fpCircuitCmd').value;
        c.operating_hours = document.getElementById('fpCircuitOH').checked;
        c.weather_suppression = document.getElementById('fpCircuitWS').checked;
        const callEl = document.getElementById('fpCircuitCallSp');
        if (callEl) {
            const v = parseFloat(callEl.value);
            if (!Number.isNaN(v)) c.receiver_call_setpoint = v; else delete c.receiver_call_setpoint;
        }
        const idleEl = document.getElementById('fpCircuitIdleSp');
        if (idleEl) {
            const v = parseFloat(idleEl.value);
            if (!Number.isNaN(v)) c.receiver_idle_setpoint = v; else delete c.receiver_idle_setpoint;
        }
        renderCircuitList();
        renderScene();
        // Switching command toggles the setpoint fields — re-render the panel.
        if (prevCmd !== c.receiver_command) editCircuit(circuitId);
    };
    propsDiv.querySelectorAll('input, select').forEach(el => el.addEventListener('change', update));
}

async function deleteCircuit(circuitId) {
    const c = (_state.plan.circuits || []).find(x => x.id === circuitId);
    if (!c) return;
    const assigned = (_state.plan.levels || []).reduce((n, lvl) =>
        n + (lvl.rooms || []).filter(r => r.circuit_id === circuitId).length, 0);
    if (!await window.zbmConfirm({
        title: 'Delete circuit',
        message: `Delete circuit "${c.name}"?`,
        detail: assigned > 0 ? `${assigned} room(s) will become unassigned.` : undefined,
        confirmText: 'Delete',
        variant: 'danger'
    })) return;
    _state.plan.circuits = (_state.plan.circuits || []).filter(x => x.id !== circuitId);
    // Unassign rooms across all levels
    (_state.plan.levels || []).forEach(lvl => {
        (lvl.rooms || []).forEach(r => { if (r.circuit_id === circuitId) delete r.circuit_id; });
    });
    renderCircuitList();
    renderScene();
}

// feature creators


function finishRoom() {
    const lvl = currentLevel();
    const points = _state.drawBuffer.points;
    if (points.length < 3) {
        _state.drawBuffer = null; renderOverlay(); return;
    }
    const closing = roomLegs(lvl, points[points.length - 1], points[0]);
    const closed = closing.problem ? points : points.concat(
        closing.via.filter(q => !points.some(r => samePoint(q, r))));
    const problem = roomPolygonProblem(lvl, closed);
    if (problem) { toast('warn', "Can't close the room", problem); return; }
    points.splice(0, points.length, ...closed);
    const id = genId('room');
    const r = {
        id, name: `Room ${lvl.rooms.length + 1}`,
        polygon: points.map(p => [p.x, p.y]),
    };
    lvl.rooms.push(r);
    _state.drawBuffer = null;
    _state.selection = { kind: 'room', id };
    renderScene(); renderOverlay(); renderProps();
}

/**
 * Commit the in-progress wall chain. Each consecutive pair of vertices
 * becomes one wall with type `unknown` (the user classifies in the props
 * panel afterwards — supports the "draw layout first, classify later"
 * workflow). Segments shorter than 0.2 m are skipped.
 */
function finishWallChain() {
    const lvl = currentLevel();
    const pts = _state.drawBuffer?.points || [];
    if (pts.length < 2) {
        _state.drawBuffer = null; renderOverlay(); return;
    }
    const newIds = [];
    for (let i = 0; i < pts.length - 1; i++) {
        const a = pts[i], b = pts[i + 1];
        if (Math.hypot(b.x - a.x, b.y - a.y) < 0.04) continue;
        const id = genId('w');
        lvl.walls.push({ id, x1: a.x, y1: a.y, x2: b.x, y2: b.y, type: 'unknown' });
        newIds.push(id);
    }
    _state.drawBuffer = null;
    if (newIds.length > 0) {
        // Where a new wall stops just short of (or past) another, make them
        // meet, so the junction is a corner rooms can use. Alt means "leave
        // it where I put it".
        const joined = _altDown ? 0 : joinWallEnds(lvl);
        // Select the first new wall so the user can classify it immediately.
        _state.selection = { kind: 'wall', id: newIds[0] };
        toast('success', 'Walls added',
            `${newIds.length} wall${newIds.length === 1 ? '' : 's'} drawn — classify in the panel.`
            + (joined ? ` ${joined} end${joined === 1 ? '' : 's'} joined onto the wall${joined === 1 ? ' it meets' : 's they meet'}.` : ''));
    }
    renderScene(); renderOverlay(); renderProps();
}

/** Cancel an in-progress chain without committing anything. */
function cancelDrawing() {
    if (!_state.drawBuffer) return;
    _state.drawBuffer = null;
    renderOverlay();
}

function addPointFeature(tool, m) {
    const lvl = currentLevel();
    // If the click lands inside a room polygon, auto-bind to that room as a
    // convenience. Otherwise leave room_id empty — the user binds via the
    // property panel. This mirrors the manual flow where a radiator can be
    // assigned to a room without a drawn floor-plan polygon yet.
    const room = (lvl.rooms || []).find(r => pointInPolygon(m, r.polygon));
    const room_id = room ? room.id : '';
    if (tool === 'radiator') {
        // Wall-snap behaviour: if the click is close to a wall (within 0.5 m
        // perpendicular distance), create a wall-mounted radiator. Otherwise
        // create a freestanding one at the click position. The user can
        // convert between modes later via the property panel.
        const RAD_WALL_SNAP_M = 0.5;
        const snap = nearestWallWithProjection(lvl, m, RAD_WALL_SNAP_M);
        const id = genId('rad');
        if (snap) {
            // Wall-mounted: store wall_id + offset_m. No x/y — render computes
            // the on-wall point from (wall, offset_m, room centroid for side).
            lvl.radiators.push({
                id, room_id,
                wall_id: snap.wall.id,
                offset_m: snap.offset_m,
                watts_at_dt50: 1000, length_m: 0.6, height_m: 0.6,
            });
        } else {
            // Freestanding: store x/y as before.
            lvl.radiators.push({
                id, room_id, x: m.x, y: m.y,
                watts_at_dt50: 1000, length_m: 0.6, height_m: 0.6,
            });
        }
        _state.selection = { kind: 'radiator', id };
    } else if (tool === 'sensor') {
        const id = genId('sens');
        lvl.sensors.push({
            id, room_id, ieee: '', kind: 'temp_sensor',
            x: m.x, y: m.y, height_m: 1.5, primary: false,
        });
        _state.selection = { kind: 'sensor', id };
    }
    if (!room) {
        toast('info', 'No room', 'Marker placed; pick a room in the panel on the right.');
    }
    renderScene(); renderProps();
}

/**
 * Find the wall whose projected distance from `p` is smallest, returning the
 * projection details if within `maxDist` metres. Returns null otherwise.
 * { wall, offset_m, projected: {x,y}, perpDist }
 */
function nearestWallWithProjection(lvl, p, maxDist) {
    let best = null, bestD = maxDist;
    for (const w of (lvl.walls || [])) {
        const proj = projectPointOntoSegment(p, w);
        const d = Math.hypot(p.x - proj.point.x, p.y - proj.point.y);
        if (d < bestD) {
            bestD = d;
            best = { wall: w, offset_m: proj.t, projected: proj.point, perpDist: d };
        }
    }
    return best;
}

function addContact(opening) {
    const lvl = currentLevel();
    const id = genId('con');
    lvl.contacts.push({
        id,
        opening_id: opening ? opening.id : '',
        ieee: '',
        debounce_open_seconds: 30,
        require_temp_drop_c: 0.5,
        max_close_minutes: 60,
        enabled: true,
    });
    _state.selection = { kind: 'contact', id };
    renderScene(); renderProps();
}

function pointInPolygon(p, poly) {
    if (!poly || poly.length < 3) return false;
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
        const [xi, yi] = poly[i], [xj, yj] = poly[j];
        const intersect = (yi > p.y) !== (yj > p.y) &&
                          p.x < ((xj - xi) * (p.y - yi)) / (yj - yi + 1e-12) + xi;
        if (intersect) inside = !inside;
    }
    return inside;
}

// room topology — a room's corners are the walls' corners, so its outline
// lies exactly on the walls the backend matches it against (a window counts
// for a room only when its wall is one of the room's edges), and no two
// rooms overlap.

//: Two corners this close are one corner.
const CORNER_EPS_M = 1e-3;
//: How far off a line a point may be and still count as on it. The server
//: keeps coordinates to the millimetre, so a corner that was exactly on a
//: slanted wall comes back a fraction of a millimetre off it; exact geometry
//: then reads a room edge along that wall as cutting through it.
const GEOM_TOL_M = 0.01;

function samePoint(a, b) {
    return Math.abs(a.x - b.x) < CORNER_EPS_M && Math.abs(a.y - b.y) < CORNER_EPS_M;
}

/** Signed distance of p from the line through a and b (left is positive). */
function sideOf(p, a, b) {
    const L = Math.hypot(b.x - a.x, b.y - a.y) || 1;
    return ((b.x - a.x) * (p.y - a.y) - (b.y - a.y) * (p.x - a.x)) / L;
}

/** Where segments a–b and c–d cross each other's middles, or null. Each
 *  must have its ends clearly (> GEOM_TOL_M) on opposite sides of the
 *  other: touching at an end, a T-junction, or running along each other
 *  within the tolerance is not a crossing. */
function segmentCrossing(a, b, c, d) {
    const opposite = (u, v) => (u > GEOM_TOL_M && v < -GEOM_TOL_M) || (u < -GEOM_TOL_M && v > GEOM_TOL_M);
    const ca = sideOf(c, a, b), da = sideOf(d, a, b);
    const ac = sideOf(a, c, d), bc = sideOf(b, c, d);
    if (!opposite(ca, da) || !opposite(ac, bc)) return null;
    const t = ac / (ac - bc);
    return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
}

function wallEnds(w) { return [{ x: w.x1, y: w.y1 }, { x: w.x2, y: w.y2 }]; }

/** Every point a room corner may sit on: the ends of walls, where walls
 *  cross, and (unless `wallsOnly`) other rooms' corners, so neighbours share
 *  them exactly. */
function planCorners(lvl, { excludeRoomId = null, wallsOnly = false } = {}) {
    const out = [];
    const add = p => { if (!out.some(c => samePoint(c, p))) out.push({ x: p.x, y: p.y }); };
    const walls = lvl.walls || [];
    for (const w of walls) wallEnds(w).forEach(add);
    for (let i = 0; i < walls.length; i++) {
        for (let j = i + 1; j < walls.length; j++) {
            const p = segmentCrossing(...wallEnds(walls[i]), ...wallEnds(walls[j]));
            if (p) add(p);
        }
    }
    if (!wallsOnly) {
        for (const r of lvl.rooms || []) {
            if (r.id === excludeRoomId) continue;
            for (const [x, y] of r.polygon || []) add({ x, y });
        }
    }
    return out;
}

function nearestPoint(points, p, radius) {
    let best = null, bestD = radius;
    for (const c of points) {
        const d = Math.hypot(p.x - c.x, p.y - c.y);
        if (d < bestD) { bestD = d; best = c; }
    }
    return best ? { x: best.x, y: best.y } : null;
}

/** Inside the polygon and not on its outline: rooms may share edges. */
function pointStrictlyInPolygon(p, poly) {
    for (let i = 0; i < poly.length; i++) {
        const [ax, ay] = poly[i], [bx, by] = poly[(i + 1) % poly.length];
        const dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy || 1;
        const t = Math.max(0, Math.min(1, ((p.x - ax) * dx + (p.y - ay) * dy) / L2));
        if (Math.hypot(p.x - (ax + dx * t), p.y - (ay + dy * t)) < GEOM_TOL_M) return false;
    }
    return pointInPolygon(p, poly);
}

/** A point well inside the polygon (its centroid when that is, else a point
 *  just inside one of its edges) — for telling a room inside another. */
function interiorPoint(poly) {
    const c = polygonCentroid(poly);
    if (pointStrictlyInPolygon(c, poly)) return c;
    for (let i = 0; i < poly.length; i++) {
        const [ax, ay] = poly[i], [bx, by] = poly[(i + 1) % poly.length];
        const L = Math.hypot(bx - ax, by - ay) || 1;
        for (const s of [1, -1]) {
            const p = { x: (ax + bx) / 2 - s * (by - ay) / L * 0.05,
                        y: (ay + by) / 2 + s * (bx - ax) / L * 0.05 };
            if (pointStrictlyInPolygon(p, poly)) return p;
        }
    }
    return c;
}

function polygonArea(poly) {
    let a = 0;
    for (let i = 0; i < poly.length; i++) {
        const [x1, y1] = poly[i], [x2, y2] = poly[(i + 1) % poly.length];
        a += x1 * y2 - x2 * y1;
    }
    return Math.abs(a) / 2;
}

/** Why a room edge from a to b can't be drawn, or null when it can. */
function roomEdgeProblem(lvl, a, b, excludeRoomId = null) {
    for (const w of lvl.walls || []) {
        if (segmentCrossing(a, b, ...wallEnds(w))) return 'That edge cuts through a wall.';
    }
    for (const r of lvl.rooms || []) {
        const poly = r.polygon || [];
        if (r.id === excludeRoomId || poly.length < 3) continue;
        const name = escapeHtml(r.name || 'another room');
        for (let i = 0; i < poly.length; i++) {
            const [cx, cy] = poly[i], [dx, dy] = poly[(i + 1) % poly.length];
            if (segmentCrossing(a, b, { x: cx, y: cy }, { x: dx, y: dy })) {
                return `That edge crosses into ${name}.`;
            }
        }
        for (const t of [0.25, 0.5, 0.75]) {
            if (pointStrictlyInPolygon({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t }, poly)) {
                return `That edge runs through ${name}.`;
            }
        }
    }
    return null;
}

/** Why `pts` can't be a room, or null when it can. */
function roomPolygonProblem(lvl, pts, excludeRoomId = null) {
    if (pts.length < 3) return 'A room needs at least three corners.';
    const poly = pts.map(p => [p.x, p.y]);
    if (polygonArea(poly) < 0.05) return 'Those corners enclose no floor.';
    const n = pts.length;
    for (let i = 0; i < n; i++) {
        const a = pts[i], b = pts[(i + 1) % n];
        const problem = roomEdgeProblem(lvl, a, b, excludeRoomId);
        if (problem) return problem;
        for (let j = i + 2; j < n; j++) {
            if (i === 0 && j === n - 1) continue;   // the closing edge meets the first
            if (segmentCrossing(a, b, pts[j], pts[(j + 1) % n])) return 'The outline crosses itself.';
        }
    }
    for (const r of lvl.rooms || []) {
        const other = r.polygon || [];
        if (r.id === excludeRoomId || other.length < 3) continue;
        const name = escapeHtml(r.name || 'another room');
        if (other.some(([x, y]) => pointStrictlyInPolygon({ x, y }, poly))
            || pointStrictlyInPolygon(interiorPoint(other), poly)) {
            return `It would take in ${name}.`;
        }
        if (pointStrictlyInPolygon(interiorPoint(poly), other)) return `It sits inside ${name}.`;
    }
    return null;
}

// properties pane

function renderProps() {
    const el = document.getElementById('fpProps');
    if (!_state.selection) {
        el.innerHTML = renderLevelProps(currentLevel());
        bindLevelProps();
        return;
    }
    // Auto-surface the properties drawer on a phone — tapping a wall/room/etc.
    // on the canvas is a clear "show me its properties" gesture, and the
    // drawer is closed by default there (see the @media block in floor-plan.css).
    if (window.matchMedia('(max-width: 767.98px)').matches) setMobileDrawer('props', true);

    const { kind, id } = _state.selection;
    const lvl = currentLevel();
    let html = '';
    switch (kind) {
        case 'wall':     html = renderWallProps(lvl.walls.find(w => w.id === id)); break;
        case 'opening':  html = renderOpeningProps(lvl.openings.find(o => o.id === id)); break;
        case 'room':     html = renderRoomProps(lvl.rooms.find(r => r.id === id)); break;
        case 'radiator': html = renderRadiatorProps(lvl.radiators.find(r => r.id === id)); break;
        case 'sensor':   html = renderSensorProps(lvl.sensors.find(s => s.id === id)); break;
        case 'contact':  html = renderContactProps(lvl.contacts.find(c => c.id === id)); break;
        case 'device':   html = renderDeviceProps(id); break;
    }
    el.innerHTML = html;
    bindPropsHandlers();
}

/**
 * Solar insights: rank this level's rooms by estimated daily solar energy
 * admitted through their windows, with plain-language mitigation hints
 * (reflective film, shading, glazing, loft insulation). Clear-sky estimate —
 * meant to show WHERE the sun loads the house, not exact numbers.
 */
function renderSolarInsights(lvl) {
    if (!_state.sunData || (!_state.showSun && (_state.thermalMode || 'rad') === 'rad')) return '';
    const daylightH = daylightMinutes() / 60;
    if (!daylightH) return '';
    const topIndex = Math.max(...(_state.plan.levels || []).map(l => l.index));
    const rows = [];
    for (const room of (lvl.rooms || [])) {
        if (!room.polygon || room.polygon.length < 3) continue;
        const centroid = polygonCentroid(room.polygon);
        let watts = 0, sunMin = 0, worstGlazing = null;
        for (const { opening } of openingsOnRoomBoundary(room, lvl)) {
            if (opening.kind !== 'window') continue;
            const wall = (lvl.walls || []).find(w => w.id === opening.wall_id);
            if (!wall) continue;
            const w = windowSolarWatts(opening, wall, centroid);
            if (w <= 0) continue;
            watts += w;
            sunMin = Math.max(sunMin, windowSunMinutes(opening, wall, centroid));
            const rank = { single: 3, double: 2, triple: 1 };
            if (!worstGlazing || (rank[opening.glazing] || 2) > (rank[worstGlazing] || 2)) {
                worstGlazing = opening.glazing || 'double';
            }
        }
        if (watts < 15) continue;
        // Measured telemetry, when the backend has enough heating-off data
        const impact = roomSolarImpact(room);
        const ratio = roomSolarRatio(room);
        const kwh = watts * ratio * daylightH / 1000;
        const hints = [];
        if (worstGlazing === 'single') hints.push('upgrade glazing or fit reflective film');
        else if (kwh >= 1.5) hints.push('reflective film / external shading');
        if (lvl.index === topIndex && kwh >= 1.0) hints.push('check loft insulation');
        rows.push({ name: room.name || room.id, kwh, sunH: sunMin / 60, hints, impact, ratio });
    }
    if (!rows.length) return '';
    rows.sort((a, b) => b.kwh - a.kwh);
    const anyMeasured = rows.some(r => r.ratio !== 1.0);
    const items = rows.map(r => {
        const measured = r.ratio !== 1.0;
        let meta = '';
        if (measured) {
            const mw = (r.impact.solar || {}).measured_w_median;
            meta = `<div class="fp-solar-measured">✓ measured: ${mw != null ? '~' + Math.round(mw) + ' W in sun, ' : ''}`
                 + `×${r.ratio.toFixed(2)} vs clear-sky (${escapeHtml(r.impact.confidence)} confidence)</div>`;
        } else if (r.impact && r.impact.status && r.impact.status !== 'ok') {
            const why = {
                no_sensor: 'no temperature sensor bound',
                no_telemetry: 'no sensor history yet',
                no_cooldown_windows: 'no heating-off periods recorded yet',
                insufficient_baseline: 'still collecting night-time baseline',
                no_sunlit_windows: 'no sunny heating-off periods yet',
                location_missing: 'set latitude/longitude in weather settings',
            }[r.impact.status] || r.impact.status;
            meta = `<div class="text-muted fst-italic">measuring — ${escapeHtml(why)}</div>`;
        }
        return `
        <div class="fp-solar-row">
          <div class="d-flex justify-content-between">
            <strong>${escapeHtml(r.name)}</strong>
            <span>~${r.kwh.toFixed(1)} kWh/day${measured ? ' ✓' : ''}</span>
          </div>
          <div class="text-muted">${r.sunH.toFixed(1)} h direct sun${r.hints.length ? ' — ' + escapeHtml(r.hints.join('; ')) : ''}</div>
          ${meta}
        </div>`;
    }).join('');
    return `
      <hr/>
      <div class="text-muted small text-uppercase mb-1">Solar insights (today)</div>
      <div class="small">${items}</div>
      <div class="form-text small mt-1">${anyMeasured
          ? '✓ = calibrated with measured heating-off telemetry; others are clear-sky estimates.'
          : 'Clear-sky estimate from window size, glazing and sun exposure. Values calibrate automatically once enough heating-off telemetry accumulates.'}
        Use the <em>Solar gain only</em> thermal mode to see where it lands.</div>`;
}

function renderLevelProps(lvl) {
    if (!lvl) return '<div class="text-muted small">No level selected.</div>';
    return `
      <div class="text-muted small text-uppercase mb-2">Level properties</div>
      <div class="mb-2"><label class="form-label small">Name</label>
        <input class="form-control form-control-sm" data-prop="level.name" value="${escapeAttr(lvl.name || '')}"/></div>
      <div class="row g-2 mb-2">
        <div class="col-6"><label class="form-label small">Index</label>
          <input type="number" class="form-control form-control-sm" data-prop="level.index" value="${lvl.index}"/></div>
        <div class="col-6"><label class="form-label small">Height (m)</label>
          <input type="number" step="0.05" class="form-control form-control-sm" data-prop="level.ceiling_height_m" value="${lvl.ceiling_height_m}"/></div>
      </div>
      <div class="mb-2"><label class="form-label small">Floor above ground (m)</label>
        <input type="number" step="0.1" class="form-control form-control-sm" data-prop="level.floor_above_ground_m" value="${lvl.floor_above_ground_m || 0}"/></div>
      ${_state.plan.levels.length > 1 ? `<button class="btn btn-sm btn-outline-danger w-100 mt-2" id="fpDeleteLevel"><i class="fas fa-trash me-1"></i>Delete level</button>` : ''}
      ${renderSolarInsights(lvl)}
      <hr/>
      <div class="text-muted small">
        <div><strong>Pan/zoom:</strong> Shift+drag (or middle-mouse) to pan, wheel to zoom.</div>
        <div class="mt-1"><strong>Walls:</strong> click to start a chain, click again to add each vertex. Press <kbd>Enter</kbd> or right-click or double-click to finish, <kbd>Esc</kbd> to cancel, <kbd>Backspace</kbd> to undo last vertex. Click on the first vertex to close back into it.</div>
        <div class="mt-1"><strong>Rooms:</strong> click the marked wall corners in turn — edges follow the walls between them — and click the first corner again to close. Rooms can't overlap or cut through a wall; draw an internal wall where an open-plan room splits.</div>
        <div class="mt-1"><strong>Precision:</strong> set the Snap and Angle steps in the Draw panel; hold <kbd>Alt</kbd> to disable snapping entirely, or <kbd>Ctrl</kbd> while drawing walls to lock the bearing to the Angle step. Endpoint merging follows the zoom — zoom in to place points close together without them joining.</div>
        <div class="mt-1"><strong>Radiator/Sensor:</strong> place anywhere; pick the room from the panel. <strong>Contact:</strong> place near a window/door (or anywhere) and pick the opening from the panel.</div>
      </div>`;
}

function renderWallProps(w) {
    if (!w) return '';
    return `
      <div class="text-muted small text-uppercase mb-2">Wall</div>
      <div class="mb-2"><label class="form-label small">Type</label>
        <select class="form-select form-select-sm" data-prop="wall.type">
          ${['external','party','internal','unknown'].map(t =>
            `<option value="${t}" ${w.type === t ? 'selected' : ''}>${t}</option>`).join('')}
        </select></div>
      <div class="small text-muted mb-2">Length: ${Math.hypot(w.x2-w.x1, w.y2-w.y1).toFixed(2)} m</div>
      <button class="btn btn-sm btn-outline-secondary w-100 mb-1" data-action="join-wall-ends">
        <i class="fas fa-link me-1"></i>Join wall ends</button>
      <div class="form-text small text-muted mb-2">Closes the small gaps where walls on this level were
        meant to meet, so rooms have real corners to snap to.</div>
      <button class="btn btn-sm btn-outline-danger w-100" data-action="delete-wall"><i class="fas fa-trash me-1"></i>Delete wall</button>`;
}

function renderOpeningProps(o) {
    if (!o) return '';
    const lvl = currentLevel();
    const roomOpts = lvl.rooms.map(r =>
        `<option value="${r.id}" ${o.room_id === r.id ? 'selected' : ''}>${escapeHtml(r.name || r.id)}</option>`).join('');
    return `
      <div class="text-muted small text-uppercase mb-2">${o.kind === 'window' ? 'Window' : 'Door'}</div>
      <div class="row g-2 mb-2">
        <div class="col-6"><label class="form-label small">Width (m)</label>
          <input type="number" step="0.05" class="form-control form-control-sm" data-prop="opening.width_m" value="${o.width_m}"/></div>
        <div class="col-6"><label class="form-label small">Height (m)</label>
          <input type="number" step="0.05" class="form-control form-control-sm" data-prop="opening.height_m" value="${o.height_m}"/></div>
      </div>
      <div class="mb-2"><label class="form-label small">Belongs to room</label>
        <select class="form-select form-select-sm" data-prop="opening.room_id">
          <option value="">— unassigned —</option>${roomOpts}
        </select></div>
      ${o.kind === 'window' ? `
      <div class="mb-2"><label class="form-label small">Glazing</label>
        <select class="form-select form-select-sm" data-prop="opening.glazing">
          ${['single','double','triple'].map(g => `<option value="${g}" ${o.glazing === g ? 'selected' : ''}>${g}</option>`).join('')}
        </select></div>` : `
      <div class="mb-2"><label class="form-label small">Door type</label>
        <select class="form-select form-select-sm" data-prop="opening.door_type">
          ${['external','internal'].map(d => `<option value="${d}" ${o.door_type === d ? 'selected' : ''}>${d}</option>`).join('')}
        </select></div>`}
      <button class="btn btn-sm btn-outline-danger w-100" data-action="delete-opening"><i class="fas fa-trash me-1"></i>Delete</button>`;
}

function renderFpScheduleSlots(slots) {
    if (!slots || !slots.length) return '<div class="text-muted small fst-italic mb-1">No slots — room uses operating hours defaults.</div>';
    const days = ['mon','tue','wed','thu','fri','sat','sun'];
    return slots.map((slot, i) => `
      <div class="fp-slot border rounded p-1 mb-1" data-slot-idx="${i}">
        <div class="d-flex flex-wrap gap-1 mb-1">
          ${days.map(d => `<label class="fp-day-label"><input type="checkbox" data-slot-day="${d}" data-slot-idx="${i}" ${(slot.days||[]).includes(d) ? 'checked' : ''}><span>${d.charAt(0).toUpperCase()}</span></label>`).join('')}
        </div>
        <div class="row g-1 align-items-center">
          <div class="col-auto"><input type="time" class="form-control form-control-sm fp-slot-start" data-slot-idx="${i}" value="${slot.start||'07:00'}"/></div>
          <div class="col-auto text-muted small">→</div>
          <div class="col-auto"><input type="time" class="form-control form-control-sm fp-slot-end" data-slot-idx="${i}" value="${slot.end||'22:00'}"/></div>
          <div class="col"><input type="number" step="0.5" min="5" max="32" class="form-control form-control-sm fp-slot-temp" data-slot-idx="${i}" value="${slot.temp??20}" placeholder="°C"/></div>
          <div class="col-auto"><button class="btn btn-sm btn-outline-danger fp-del-slot" data-slot-idx="${i}" title="Delete slot"><i class="fas fa-times"></i></button></div>
        </div>
      </div>`).join('');
}

function renderRoomProps(r) {
    if (!r) return '';
    const planCircuits = _state.plan.circuits || [];
    const circuitOpts = ['<option value="">— Unassigned —</option>']
        .concat(planCircuits.map(c =>
            `<option value="${c.id}" ${r.circuit_id === c.id ? 'selected' : ''}>${escapeHtml(c.name)}</option>`
        )).join('');
    const circuitSection = planCircuits.length > 0
        ? `<div class="mb-2"><label class="form-label small">Circuit</label>
             <select class="form-select form-select-sm" data-prop="room.circuit_id">${circuitOpts}</select></div>`
        : `<div class="mb-2 small text-muted fst-italic"><i class="fas fa-info-circle me-1"></i>Add a circuit in the Circuits panel to assign this room.</div>`;
    const oohAction = r.out_of_hours_action || 'setback';
    const etMode = r.external_temp_mode || 'advisory';
    return `
      <div class="text-muted small text-uppercase mb-2">Room</div>
      <div class="mb-2"><label class="form-label small">Name</label>
        <input class="form-control form-control-sm" data-prop="room.name" value="${escapeAttr(r.name || '')}"/></div>
      ${circuitSection}
      <div class="mb-2"><label class="form-label small">Temperature targets</label>
        <div class="row g-1">
          <div class="col-4">
            <label class="form-label small text-muted mb-0">Target (°C)</label>
            <input type="number" step="0.5" min="5" max="32" class="form-control form-control-sm" data-prop="room.target_temp" value="${r.target_temp??20}"/>
          </div>
          <div class="col-4">
            <label class="form-label small text-muted mb-0">Setback (°C)</label>
            <input type="number" step="0.5" min="5" max="32" class="form-control form-control-sm" data-prop="room.night_setback" value="${r.night_setback??17}"/>
          </div>
          <div class="col-4">
            <label class="form-label small text-muted mb-0">Min (°C)</label>
            <input type="number" step="0.5" min="5" max="32" class="form-control form-control-sm" data-prop="room.min_temp" value="${r.min_temp??16}"/>
          </div>
        </div>
      </div>
      <div class="mb-2">
        <label class="form-label small">Schedule <span class="badge bg-secondary">${(r.schedule||[]).length}</span></label>
        <div class="fp-schedule-slots" data-room-id="${r.id}">${renderFpScheduleSlots(r.schedule||[])}</div>
        <button class="btn btn-sm btn-outline-secondary w-100 mt-1 fp-add-slot"><i class="fas fa-plus me-1"></i>Add time slot</button>
      </div>
      <div class="mb-2"><label class="form-label small">Outside scheduled hours</label>
        <div class="row g-1">
          <div class="col-7">
            <select class="form-select form-select-sm" data-prop="room.out_of_hours_action">
              <option value="setback" ${oohAction==='setback'?'selected':''}>Setback (lower target)</option>
              <option value="min_only" ${oohAction==='min_only'?'selected':''}>Frost-protect only</option>
              <option value="off" ${oohAction==='off'?'selected':''}>Off (no heat call)</option>
            </select>
          </div>
          <div class="col-5">
            <input type="number" step="0.5" min="-10" max="0" class="form-control form-control-sm"
                   placeholder="Offset °C" data-prop="room.night_setback_offset_c"
                   value="${r.night_setback_offset_c??-3}"/>
          </div>
        </div>
      </div>
      <div class="mb-2"><label class="form-label small">External temperature sensor</label>
        <select class="form-select form-select-sm" data-prop="room.external_temp_mode" data-rerender-on-change>
          <option value="advisory" ${etMode==='advisory'?'selected':''}>Advisory (correct readings only)</option>
          <option value="push" ${etMode==='push'?'selected':''}>Push (write to TRVs)</option>
          <option value="off" ${etMode==='off'?'selected':''}>Off</option>
        </select>
        ${etMode==='push' ? `
        <div class="mt-1"><label class="form-label small text-muted mb-0">Push interval (sec)</label>
          <input type="number" step="30" min="30" max="86400" class="form-control form-control-sm"
                 data-prop="room.external_temp_push_interval_sec" value="${r.external_temp_push_interval_sec??300}"/></div>` : ''}
      </div>
      <div class="mb-2"><label class="form-label small">Floor type</label>
        <select class="form-select form-select-sm" data-prop="room.floor_type">
          <option value="">—</option>
          ${['solid','suspended','carpet_over_concrete','tile_over_concrete','wooden','carpet_over_wooden','unknown']
            .map(f => `<option value="${f}" ${r.floor_type === f ? 'selected' : ''}>${f}</option>`).join('')}
        </select></div>
      <div class="mb-2"><label class="form-label small">Ceiling type</label>
        <select class="form-select form-select-sm" data-prop="room.ceiling_type">
          <option value="">—</option>
          ${['insulated','uninsulated','flat_roof','unknown']
            .map(ct => `<option value="${ct}" ${r.ceiling_type === ct ? 'selected' : ''}>${ct}</option>`).join('')}
        </select></div>
      ${(currentLevel()?.walls || []).length ? `
      <button class="btn btn-sm btn-outline-secondary w-100 mb-1" data-action="rooms-snap-to-walls">
        <i class="fas fa-vector-square me-1"></i>Snap rooms to walls</button>
      <div class="form-text small text-muted mb-2">Joins up the walls, then moves every room on this
        level onto its wall corners, so its windows and outside walls are counted.</div>` : ''}
      <button class="btn btn-sm btn-outline-danger w-100" data-action="delete-room"><i class="fas fa-trash me-1"></i>Delete room</button>`;
}

function renderRadiatorProps(r) {
    if (!r) return '';
    const lvl = currentLevel();
    const trvUsed = new Set(lvl.radiators.filter(x => x.id !== r.id && x.trv_ieee).map(x => x.trv_ieee));
    const trvOpts = ['<option value="">— No TRV (fixed valve) —</option>']
      .concat(_availableDevices.trvs.map(t =>
        `<option value="${t.ieee}" ${r.trv_ieee === t.ieee ? 'selected' : ''} ${trvUsed.has(t.ieee) ? 'disabled' : ''}>${escapeHtml(t.name || t.ieee)}${trvUsed.has(t.ieee) ? ' (used)' : ''}</option>`)).join('');
    const roomOpts = ['<option value="">— Select room —</option>']
      .concat(lvl.rooms.map(rm =>
        `<option value="${rm.id}" ${r.room_id === rm.id ? 'selected' : ''}>${escapeHtml(rm.name || rm.id)}</option>`)).join('');

    // Circuit context — derived from `room_id` via the caller-provided
    // circuits list. Read-only: circuit membership is set in the heating-
    // controller config, not here, because it's a behavioural binding (which
    // receiver fires for this room), not a geometric one. We just show it
    // so the user knows what controller behaviour this radiator triggers.
    const circuit = r.room_id ? findCircuitForRoom(r.room_id) : null;
    const circuitInfo = circuit
        ? `<div class="mb-2 small text-muted">
             <i class="fas fa-stream me-1"></i>Heats on circuit
             <strong>${escapeHtml(circuit.name || circuit.id)}</strong>
             ${circuit.receiver_ieee
               ? `<span class="text-muted" title="${escapeHtml(circuit.receiver_ieee)}"> · receiver <code>${escapeHtml(circuit.receiver_ieee.slice(-8))}</code></span>`
               : '<span class="text-warning ms-1">(no receiver assigned)</span>'}
           </div>`
        : (_availableCircuits.length && r.room_id
            ? `<div class="mb-2 small text-warning">
                 <i class="fas fa-exclamation-triangle me-1"></i>
                 Room <code>${escapeHtml(r.room_id)}</code> isn't in any circuit yet.
                 Add it in the heating controller settings.
               </div>`
            : '');

    // Mounting section: tells the user whether this radiator is wall-mounted
    // or freestanding, and offers a single button to convert. Wall ID is
    // shown read-only because it's set geometrically (drag/place); changing
    // it through a dropdown without geometric feedback is error-prone.
    const wall = r.wall_id ? lvl.walls.find(w => w.id === r.wall_id) : null;
    const wallLen = wall ? Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) : 0;
    const mountSection = wall
        ? `<div class="mb-2 p-2 border rounded bg-light">
             <div class="small text-muted text-uppercase mb-1">Mounting</div>
             <div class="small mb-1">
               <i class="fas fa-link me-1"></i>
               Wall-mounted on <code>${escapeHtml(r.wall_id)}</code>
               <span class="text-muted">(${wallLen.toFixed(2)} m long)</span>
             </div>
             <div class="row g-2">
               <div class="col-12">
                 <label class="form-label small">Offset from wall start (m)</label>
                 <input type="number" step="0.05" min="0" max="${wallLen.toFixed(2)}"
                        class="form-control form-control-sm"
                        data-prop="radiator.offset_m"
                        value="${(r.offset_m ?? 0).toFixed(2)}"/>
               </div>
             </div>
             <button class="btn btn-sm btn-outline-secondary w-100 mt-2" data-action="rad-to-freestanding">
               <i class="fas fa-arrows-alt me-1"></i>Convert to freestanding
             </button>
           </div>`
        : `<div class="mb-2 p-2 border rounded bg-light">
             <div class="small text-muted text-uppercase mb-1">Mounting</div>
             <div class="small mb-2">
               <i class="fas fa-arrows-alt me-1"></i>Freestanding at
               <code>(${(r.x ?? 0).toFixed(2)}, ${(r.y ?? 0).toFixed(2)})</code>
             </div>
             ${(lvl.walls || []).length > 0 ? `
               <button class="btn btn-sm btn-outline-secondary w-100" data-action="rad-snap-to-wall">
                 <i class="fas fa-link me-1"></i>Snap to nearest wall
               </button>` : `
               <div class="form-text small text-muted">No walls on this level to snap to.</div>`}
           </div>`;

    return `
      <div class="text-muted small text-uppercase mb-2">Radiator</div>
      <div class="mb-2"><label class="form-label small">Room</label>
        <select class="form-select form-select-sm" data-prop="radiator.room_id">${roomOpts}</select>
        ${lvl.rooms.length === 0 ? '<div class="form-text small text-warning">No rooms drawn on this level — draw one with the Room tool, then return here.</div>' : ''}
      </div>
      ${circuitInfo}
      ${mountSection}
      <div class="row g-2 mb-2">
        <div class="col-6"><label class="form-label small">Watts @ ΔT50</label>
          <input type="number" class="form-control form-control-sm" data-prop="radiator.watts_at_dt50" value="${r.watts_at_dt50 || 0}"/></div>
        <div class="col-6"><label class="form-label small">Length (m)</label>
          <input type="number" step="0.1" min="0.1" max="10" class="form-control form-control-sm" data-prop="radiator.length_m" value="${r.length_m || 0.6}"/></div>
      </div>
      <div class="row g-2 mb-2">
        <div class="col-6"><label class="form-label small" title="Physical panel height — used for sizing calcs, not shown on plan view">Panel height (m)</label>
          <input type="number" step="0.05" min="0.05" max="3" class="form-control form-control-sm" data-prop="radiator.height_m" value="${r.height_m || 0.6}"/></div>
        <div class="col-6"><label class="form-label small">Approx. surface area</label>
          <input type="text" class="form-control form-control-sm" readonly
                 value="${((r.length_m || 0.6) * (r.height_m || 0.6)).toFixed(2)} m²"/></div>
      </div>
      <div class="mb-2"><label class="form-label small">Type</label>
        <select class="form-select form-select-sm" data-prop="radiator.type">
          <option value="">—</option>
          ${['single_panel','double_panel_single_conv','double_panel_double_conv','triple_panel','column','towel_rail','underfloor']
            .map(t => `<option value="${t}" ${r.type === t ? 'selected' : ''}>${t}</option>`).join('')}
        </select></div>
      <div class="mb-2"><label class="form-label small">Bound TRV</label>
        <select class="form-select form-select-sm" data-prop="radiator.trv_ieee" data-rerender-on-change>${trvOpts}</select></div>
      ${r.trv_ieee ? `
      <div class="mb-2 ps-2 border-start">
        <div class="small text-muted text-uppercase mb-1">TRV behaviour</div>
        <div class="form-check form-switch small">
          <input type="checkbox" class="form-check-input" id="fpTrvWin" data-prop="radiator.window_detection" ${r.window_detection ? 'checked' : ''}/>
          <label class="form-check-label" for="fpTrvWin">Window/open detection</label></div>
        <div class="form-check form-switch small">
          <input type="checkbox" class="form-check-input" id="fpTrvLock" data-prop="radiator.child_lock" ${r.child_lock ? 'checked' : ''}/>
          <label class="form-check-label" for="fpTrvLock">Child lock</label></div>
        <div class="form-check form-switch small">
          <input type="checkbox" class="form-check-input" id="fpTrvValve" data-prop="radiator.valve_detection" ${r.valve_detection ? 'checked' : ''}/>
          <label class="form-check-label" for="fpTrvValve">Valve detection</label></div>
      </div>` : ''}
      <div class="mb-2 form-check form-switch small">
        <input type="checkbox" class="form-check-input" id="fpRadRefl" data-prop="radiator.reflective_panel" ${r.reflective_panel ? 'checked' : ''}/>
        <label class="form-check-label" for="fpRadRefl">Reflective panel behind</label></div>
      <button class="btn btn-sm btn-outline-danger w-100" data-action="delete-radiator"><i class="fas fa-trash me-1"></i>Delete</button>`;
}

function renderSensorProps(s) {
    if (!s) return '';
    const lvl = currentLevel();
    const roomOpts = ['<option value="">— Select room —</option>']
      .concat(lvl.rooms.map(rm =>
        `<option value="${rm.id}" ${s.room_id === rm.id ? 'selected' : ''}>${escapeHtml(rm.name || rm.id)}</option>`)).join('');
    const ieeeUsed = new Set(lvl.sensors.filter(x => x.id !== s.id && x.ieee).map(x => x.ieee));
    const sensorOpts = ['<option value="">— Select device —</option>']
      .concat(_availableDevices.sensors.map(d =>
        `<option value="${d.ieee}" ${s.ieee === d.ieee ? 'selected' : ''} ${ieeeUsed.has(d.ieee) ? 'disabled' : ''}>${escapeHtml(d.name || d.ieee)}${d.temperature != null ? ` (${Number(d.temperature).toFixed(1)}°C)` : ''}</option>`)).join('');
    return `
      <div class="text-muted small text-uppercase mb-2">Temperature sensor</div>
      <div class="mb-2"><label class="form-label small">Room</label>
        <select class="form-select form-select-sm" data-prop="sensor.room_id">${roomOpts}</select>
        ${lvl.rooms.length === 0 ? '<div class="form-text small text-warning">No rooms drawn on this level — draw one with the Room tool, then return here.</div>' : ''}
      </div>
      <div class="mb-2"><label class="form-label small">Device</label>
        <select class="form-select form-select-sm" data-prop="sensor.ieee">${sensorOpts}</select></div>
      <div class="mb-2"><label class="form-label small">Kind</label>
        <select class="form-select form-select-sm" data-prop="sensor.kind">
          ${['temp_sensor','thermostat','room_stat'].map(k => `<option value="${k}" ${s.kind === k ? 'selected' : ''}>${k}</option>`).join('')}
        </select></div>
      <div class="mb-2"><label class="form-label small">Mounting height (m)</label>
        <input type="number" step="0.05" min="0" max="5" class="form-control form-control-sm" data-prop="sensor.height_m" value="${s.height_m ?? 1.5}"/>
        <div class="form-text small">Used to correct for warm-air stratification when reading the room.</div></div>
      <div class="mb-2 form-check form-switch small">
        <input type="checkbox" class="form-check-input" id="fpSensPrim" data-prop="sensor.primary" ${s.primary ? 'checked' : ''}/>
        <label class="form-check-label" for="fpSensPrim">Primary sensor for this room</label></div>
      <button class="btn btn-sm btn-outline-danger w-100" data-action="delete-sensor"><i class="fas fa-trash me-1"></i>Delete</button>`;
}

function renderContactProps(c) {
    if (!c) return '';
    const lvl = currentLevel();
    const ieeeUsed = new Set(lvl.contacts.filter(x => x.id !== c.id && x.ieee).map(x => x.ieee));
    const contactOpts = ['<option value="">— Select device —</option>']
      .concat(_availableDevices.contacts.map(d =>
        `<option value="${d.ieee}" ${c.ieee === d.ieee ? 'selected' : ''} ${ieeeUsed.has(d.ieee) ? 'disabled' : ''}>${escapeHtml(d.name || d.ieee)}</option>`)).join('');

    // Build the opening picker. Each entry shows kind + room (if known) so
    // similarly-named openings can be told apart.
    const openingUsed = new Set(lvl.contacts.filter(x => x.id !== c.id && x.opening_id).map(x => x.opening_id));
    const roomById = new Map((lvl.rooms || []).map(r => [r.id, r]));
    const openingOpts = ['<option value="">— Select opening —</option>']
      .concat((lvl.openings || []).map(o => {
        const room = o.room_id ? roomById.get(o.room_id) : null;
        const roomLabel = room ? ` · ${escapeHtml(room.name || room.id)}` : '';
        const used = openingUsed.has(o.id) ? ' (used)' : '';
        const w = (o.width_m || 0).toFixed(2);
        return `<option value="${o.id}" ${c.opening_id === o.id ? 'selected' : ''} ${openingUsed.has(o.id) ? 'disabled' : ''}>${o.kind} (${w} m)${roomLabel}${used}</option>`;
      })).join('');

    return `
      <div class="text-muted small text-uppercase mb-2">Contact sensor</div>
      <div class="mb-2"><label class="form-label small">Bound to opening</label>
        <select class="form-select form-select-sm" data-prop="contact.opening_id">${openingOpts}</select>
        ${(lvl.openings || []).length === 0 ? '<div class="form-text small text-warning">No windows or doors on this level yet — draw some first.</div>' : ''}
      </div>
      <div class="mb-2"><label class="form-label small">Device</label>
        <select class="form-select form-select-sm" data-prop="contact.ieee">${contactOpts}</select></div>
      <div class="row g-2 mb-2">
        <div class="col-6"><label class="form-label small">Debounce (s)</label>
          <input type="number" class="form-control form-control-sm" data-prop="contact.debounce_open_seconds" value="${c.debounce_open_seconds}"/></div>
        <div class="col-6"><label class="form-label small">Drop (°C)</label>
          <input type="number" step="0.1" class="form-control form-control-sm" data-prop="contact.require_temp_drop_c" value="${c.require_temp_drop_c}"/></div>
      </div>
      <div class="mb-2"><label class="form-label small">Max close-suppress (min)</label>
        <input type="number" class="form-control form-control-sm" data-prop="contact.max_close_minutes" value="${c.max_close_minutes}"/></div>
      <div class="mb-2 form-check form-switch small">
        <input type="checkbox" class="form-check-input" id="fpConEn" data-prop="contact.enabled" ${c.enabled ? 'checked' : ''}/>
        <label class="form-check-label" for="fpConEn">Enabled</label></div>
      <button class="btn btn-sm btn-outline-danger w-100" data-action="delete-contact"><i class="fas fa-trash me-1"></i>Delete</button>`;
}

function bindLevelProps() {
    const root = document.getElementById('fpProps');
    root.querySelectorAll('[data-prop]').forEach(el => {
        el.addEventListener('change', () => {
            const lvl = currentLevel();
            const key = el.dataset.prop.split('.')[1];
            const val = el.type === 'number' ? parseFloat(el.value) : el.value;
            if (key === 'index') lvl.index = parseInt(el.value, 10) || 0;
            else lvl[key] = val;
            renderLevelList(); renderScene();
        });
    });
    document.getElementById('fpDeleteLevel')?.addEventListener('click', async () => {
        if (!await window.zbmConfirm({
            title: 'Delete level',
            message: `Delete level "${currentLevel().name}"?`,
            detail: 'This removes everything on it.',
            confirmText: 'Delete',
            variant: 'danger'
        })) return;
        _state.plan.levels = _state.plan.levels.filter(l => l.id !== _state.currentLevelId);
        _state.currentLevelId = _state.plan.levels[0].id;
        renderAll();
    });
}

function bindPropsHandlers() {
    const lvl = currentLevel();
    const sel = _state.selection;
    if (!sel) return;
    const root = document.getElementById('fpProps');

    root.querySelectorAll('[data-prop]').forEach(el => {
        el.addEventListener('change', () => {
            const [scope, key] = el.dataset.prop.split('.');
            let target;
            if (scope === 'wall')      target = lvl.walls.find(w => w.id === sel.id);
            else if (scope === 'opening')  target = lvl.openings.find(o => o.id === sel.id);
            else if (scope === 'room')     target = lvl.rooms.find(r => r.id === sel.id);
            else if (scope === 'radiator') target = lvl.radiators.find(r => r.id === sel.id);
            else if (scope === 'sensor')   target = lvl.sensors.find(s => s.id === sel.id);
            else if (scope === 'contact')  target = lvl.contacts.find(c => c.id === sel.id);
            if (!target) return;

            let val;
            if (el.type === 'checkbox') val = el.checked;
            else if (el.type === 'number') val = parseFloat(el.value);
            else val = el.value;
            if (val === '' || (typeof val === 'number' && Number.isNaN(val))) val = undefined;

            if (val === undefined) delete target[key]; else target[key] = val;

            // Heating now holds this device's position: one position only.
            if (typeof val === 'string' && val
                && ((scope === 'radiator' && key === 'trv_ieee')
                    || ((scope === 'sensor' || scope === 'contact') && key === 'ieee'))) {
                unplaceDevice(val.toLowerCase());
            }

            // Primary sensor: enforce one-per-room
            if (scope === 'sensor' && key === 'primary' && val) {
                lvl.sensors.forEach(s2 => { if (s2.id !== sel.id && s2.room_id === target.room_id) s2.primary = false; });
            }
            // Circuit assignment: refresh sidebar room count
            if (scope === 'room' && key === 'circuit_id') {
                renderCircuitList();
            }
            renderScene();
        });
    });

    // Selects that show/hide dependent fields re-render the panel after their
    // model update (this listener is registered after the generic one above,
    // so the value is already written when renderProps() rebuilds).
    root.querySelectorAll('[data-rerender-on-change]').forEach(el => {
        el.addEventListener('change', () => renderProps());
    });

    // Schedule slot handlers (room props panel)
    const fpAddSlot = root.querySelector('.fp-add-slot');
    if (fpAddSlot) {
        fpAddSlot.addEventListener('click', () => {
            const lvl = currentLevel();
            const room = lvl.rooms.find(r => r.id === sel.id);
            if (!room) return;
            if (!Array.isArray(room.schedule)) room.schedule = [];
            room.schedule.push({ days: ['mon','tue','wed','thu','fri'], start: '07:00', end: '22:00', temp: room.target_temp || 20 });
            renderProps();
        });
    }
    root.querySelectorAll('.fp-del-slot').forEach(btn => {
        btn.addEventListener('click', () => {
            const lvl = currentLevel();
            const room = lvl.rooms.find(r => r.id === sel.id);
            if (!room || !Array.isArray(room.schedule)) return;
            const idx = parseInt(btn.dataset.slotIdx, 10);
            room.schedule.splice(idx, 1);
            renderProps();
        });
    });
    root.querySelectorAll('[data-slot-day]').forEach(cb => {
        cb.addEventListener('change', () => {
            const lvl = currentLevel();
            const room = lvl.rooms.find(r => r.id === sel.id);
            if (!room || !Array.isArray(room.schedule)) return;
            const idx = parseInt(cb.dataset.slotIdx, 10);
            const slot = room.schedule[idx]; if (!slot) return;
            const day = cb.dataset.slotDay;
            if (cb.checked) { if (!slot.days.includes(day)) slot.days.push(day); }
            else { slot.days = slot.days.filter(d => d !== day); }
        });
    });
    root.querySelectorAll('.fp-slot-start, .fp-slot-end, .fp-slot-temp').forEach(inp => {
        inp.addEventListener('change', () => {
            const lvl = currentLevel();
            const room = lvl.rooms.find(r => r.id === sel.id);
            if (!room || !Array.isArray(room.schedule)) return;
            const idx = parseInt(inp.dataset.slotIdx, 10);
            const slot = room.schedule[idx]; if (!slot) return;
            if (inp.classList.contains('fp-slot-start')) slot.start = inp.value;
            else if (inp.classList.contains('fp-slot-end')) slot.end = inp.value;
            else slot.temp = parseFloat(inp.value) || slot.temp;
        });
    });

    root.querySelector('[data-action="delete-wall"]')?.addEventListener('click', () => deleteSelected('walls'));
    root.querySelector('[data-action="delete-opening"]')?.addEventListener('click', () => deleteSelected('openings'));
    root.querySelector('[data-action="delete-room"]')?.addEventListener('click', () => deleteSelected('rooms'));
    root.querySelector('[data-action="rooms-snap-to-walls"]')?.addEventListener('click', () => {
        const lvl = currentLevel();
        const { joined, snapped, problems } = snapLevelToWalls(lvl);
        const also = joined ? ` ${joined} wall end${joined === 1 ? '' : 's'} joined up first.` : '';
        const failed = [...problems].map(([id, why]) =>
            `${escapeHtml(lvl.rooms.find(r => r.id === id)?.name || id)}: ${escapeHtml(why)}`);
        if (failed.length) {
            toast('warn', `${snapped} of ${lvl.rooms.length} rooms snapped`,
                  `Left as drawn — ${failed.join(' ')}${also}`);
        } else {
            toast('success', 'Rooms snapped', `All ${snapped} rooms now sit on their walls — save to keep it.${also}`);
        }
        renderScene(); renderOverlay(); renderProps();
    });
    root.querySelector('[data-action="join-wall-ends"]')?.addEventListener('click', () => {
        const joined = joinWallEnds(currentLevel());
        if (joined) toast('success', 'Walls joined',
                          `${joined} wall end${joined === 1 ? '' : 's'} moved onto the wall${joined === 1 ? ' it meets' : 's they meet'} — save to keep it.`);
        else toast('info', 'Nothing to join', 'Every wall end already meets another wall.');
        renderScene(); renderOverlay(); renderProps();
    });
    root.querySelector('[data-action="delete-radiator"]')?.addEventListener('click', () => deleteSelected('radiators'));
    root.querySelector('[data-action="delete-sensor"]')?.addEventListener('click', () => deleteSelected('sensors'));
    root.querySelector('[data-action="delete-contact"]')?.addEventListener('click', () => deleteSelected('contacts'));
    if (sel.kind === 'device') bindDeviceProps(sel.id);

    // Radiator mounting-mode conversions
    root.querySelector('[data-action="rad-to-freestanding"]')?.addEventListener('click', () => {
        const lvl = currentLevel();
        const r = lvl.radiators.find(x => x.id === sel.id);
        if (!r || !r.wall_id) return;
        const wall = lvl.walls.find(w => w.id === r.wall_id);
        // Compute the radiator's current ON-WALL centre point and write it
        // as x/y so the freestanding render lands in the same place.
        if (wall) {
            const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) || 1;
            const ux = (wall.x2 - wall.x1) / wlen, uy = (wall.y2 - wall.y1) / wlen;
            const len = r.length_m || 0.6;
            const t = (r.offset_m ?? wlen / 2) + len / 2;
            r.x = wall.x1 + ux * t;
            r.y = wall.y1 + uy * t;
        } else if (r.x == null || r.y == null) {
            r.x = 0; r.y = 0;
        }
        delete r.wall_id;
        delete r.offset_m;
        renderScene(); renderProps();
    });
    root.querySelector('[data-action="rad-snap-to-wall"]')?.addEventListener('click', () => {
        const lvl = currentLevel();
        const r = lvl.radiators.find(x => x.id === sel.id);
        if (!r) return;
        const p = { x: r.x ?? 0, y: r.y ?? 0 };
        const snap = nearestWallWithProjection(lvl, p, 100);  // any distance — this is explicit user intent
        if (!snap) {
            toast('warn', 'No walls', 'No walls available on this level.');
            return;
        }
        r.wall_id = snap.wall.id;
        r.offset_m = snap.offset_m;
        delete r.x; delete r.y;
        renderScene(); renderProps();
    });
}

function deleteSelected(arrKey) {
    const lvl = currentLevel();
    const sel = _state.selection;
    if (!sel) return;
    lvl[arrKey] = lvl[arrKey].filter(x => x.id !== sel.id);
    // Cascade: removing a wall removes its openings; removing an opening removes its contacts
    if (arrKey === 'walls') {
        const removedOpenings = lvl.openings.filter(o => !lvl.walls.find(w => w.id === o.wall_id)).map(o => o.id);
        lvl.openings = lvl.openings.filter(o => !removedOpenings.includes(o.id));
        lvl.contacts = lvl.contacts.filter(c => !removedOpenings.includes(c.opening_id));
    } else if (arrKey === 'openings') {
        lvl.contacts = lvl.contacts.filter(c => c.opening_id !== sel.id);
    } else if (arrKey === 'rooms') {
        lvl.radiators = lvl.radiators.filter(r => r.room_id !== sel.id);
        lvl.sensors = lvl.sensors.filter(s => s.room_id !== sel.id);
    }
    _state.selection = null;
    renderScene(); renderProps();
}

// levels

function addLevel() {
    const idx = (_state.plan.levels.reduce((m, l) => Math.max(m, l.index), -1)) + 1;
    const l = {
        id: genId('level'),
        name: `Level ${idx}`,
        index: idx,
        ceiling_height_m: DEFAULT_LEVEL_HEIGHT,
        floor_above_ground_m: idx * DEFAULT_LEVEL_HEIGHT,
        walls: [], openings: [], rooms: [],
        radiators: [], sensors: [], contacts: [],
    };
    _state.plan.levels.push(l);
    _state.currentLevelId = l.id;
    renderAll();
}

// zoom/pan

function zoomBy(factor, cx, cy) {
    const oldZ = _state.zoom;
    const newZ = Math.max(10, Math.min(400, oldZ * factor));
    if (cx != null && cy != null) {
        const wrap = document.getElementById('fpCanvasWrap').getBoundingClientRect();
        const px = cx - wrap.left, py = cy - wrap.top;
        const before = { x: (px - _state.pan.x) / oldZ, y: (py - _state.pan.y) / oldZ };
        _state.zoom = newZ;
        _state.pan.x = px - before.x * newZ;
        _state.pan.y = py - before.y * newZ;
    } else {
        _state.zoom = newZ;
    }
    renderScene(); renderOverlay();
}

function zoomFit() {
    const lvl = currentLevel();
    const wrap = document.getElementById('fpCanvasWrap');
    if (!wrap) return;
    const rect = wrap.getBoundingClientRect();
    if (rect.width < 50 || rect.height < 50) return;
    _needsFit = false;

    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    const consider = p => {
        if (p.x < minX) minX = p.x; if (p.x > maxX) maxX = p.x;
        if (p.y < minY) minY = p.y; if (p.y > maxY) maxY = p.y;
    };
    for (const w of lvl.walls) { consider({x: w.x1, y: w.y1}); consider({x: w.x2, y: w.y2}); }
    for (const r of lvl.rooms) for (const p of r.polygon) consider({x: p[0], y: p[1]});
    // Nothing drawn yet: frame the background image instead, so a freshly
    // imported plan can't land off-screen with no way back to it.
    if (!Number.isFinite(minX) && _state.showBackground && lvl.background?.present) {
        for (const c of [[0, 0], [1, 0], [1, 1], [0, 1]]) {
            consider(bgPoint(lvl.background, c[0], c[1]));
        }
    }
    // The sun arc is part of what the user is looking at when it is on.
    if (_state.showSun && _state.sunData) {
        const { origin, r } = sunArc(lvl);
        consider({ x: origin.x - r, y: origin.y - r });
        consider({ x: origin.x + r, y: origin.y + r });
    }

    if (!Number.isFinite(minX)) {
        // Empty level: centre on origin
        _state.zoom = PIXELS_PER_METRE_DEFAULT;
        _state.pan = { x: rect.width / 2, y: rect.height / 2 };
        renderScene(); renderOverlay();
        return;
    }
    const w = maxX - minX, h = maxY - minY;
    const pad = 1.0;
    const z = Math.min(rect.width / (w + pad * 2), rect.height / (h + pad * 2));
    _state.zoom = Math.max(20, Math.min(300, z));
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const svgC = modelToSvg({ x: cx, y: cy });
    _state.pan = {
        x: rect.width / 2 - svgC.x * _state.zoom,
        y: rect.height / 2 - svgC.y * _state.zoom,
    };
    renderScene(); renderOverlay();
}

// sun overlay

async function loadSunData() {
    try {
        const r = await fetch('/api/sun/day?step_minutes=20').then(r => r.json());
        if (r && r.success) _state.sunData = r.data;
    } catch (e) { /* swallow */ }
}

// device layer — docs/floor-plan.md § Placing devices

function round3(v) { return Math.round(v * 1000) / 1000; }

/** Every ieee with a position anywhere on the plan, however it is held. */
function placedIeees() {
    const out = new Set();
    for (const l of _state.plan.levels) {
        (l.devices || []).forEach(d => out.add(d.ieee));
        l.radiators.forEach(r => r.trv_ieee && out.add(r.trv_ieee));
        l.sensors.forEach(x => x.ieee && out.add(x.ieee));
        l.contacts.forEach(c => c.ieee && out.add(c.ieee));
    }
    return out;
}

function unplaceDevice(ieee) {
    for (const l of _state.plan.levels) l.devices = (l.devices || []).filter(d => d.ieee !== ieee);
    renderPalette();
}

function placeDevice(ieee, m) {
    ieee = String(ieee).toLowerCase();
    if (placedIeees().has(ieee)) {
        toast('info', 'Already placed', 'That device is already on the plan.');
        return;
    }
    const p = snapPt(m);
    currentLevel().devices.push({ ieee, x: round3(p.x), y: round3(p.y) });
    _state.placing = null;
    _state.placeCursor = null;
    _state.tool = 'select';
    _state.selection = { kind: 'device', id: ieee };
    renderToolbar(); renderPalette(); renderScene(); renderProps();
    if (_state.showMesh) syncMeshControls();
}

function deviceInfo(ieee) {
    return _catalogue.get(ieee) || { name: ieee, kind: _heatingIeees.has(ieee) ? 'heating' : 'other' };
}

// A map-style pin, so what you drag looks like where it will land rather than
// a screenshot of the button. One element, re-coloured per drag.
let _dragGhost = null;

function dragGhost(kind, name) {
    if (!_dragGhost) {
        _dragGhost = document.createElement('div');
        _dragGhost.className = 'fp-drag-ghost';
        document.body.appendChild(_dragGhost);
    }
    _dragGhost.innerHTML = `
      <svg width="40" height="52" viewBox="0 0 40 52">
        <path class="fp-pin-body fp-device-${kind}" d="M20 51 L8 26 A14 14 0 1 1 32 26 Z"/>
        <circle class="fp-pin-hole" cx="20" cy="18" r="5"/>
      </svg>
      <div class="fp-pin-label">${escapeHtml(name)}</div>`;
    return _dragGhost;
}

function renderPalette() {
    const wrap = document.getElementById('fpPalette');
    if (!wrap || !_state) return;
    const q = (document.getElementById('fpPaletteSearch')?.value || '').trim().toLowerCase();
    const placed = placedIeees();
    const groups = {};
    for (const [ieee, info] of _catalogue) {
        if (placed.has(ieee)) continue;
        if (q && !info.name.toLowerCase().includes(q) && !ieee.includes(q)) continue;
        (groups[info.kind] ||= []).push([ieee, info]);
    }
    const order = ['light', 'heating', 'router', 'coordinator', 'other'];
    const html = order.filter(k => groups[k]).map(k => `
        <div class="text-muted mt-2 mb-1">${KIND_LABEL[k]} <span class="badge bg-secondary">${groups[k].length}</span></div>
        ${groups[k].sort((a, b) => a[1].name.localeCompare(b[1].name)).map(([ieee, info]) => `
          <button type="button" class="fp-palette-item btn btn-sm btn-outline-secondary w-100 text-start mb-1
                  ${_state.placing === ieee ? 'active' : ''}" draggable="true" data-ieee="${escapeAttr(ieee)}">
            <span class="fp-device-dot fp-device-${k}"></span>${escapeHtml(info.name)}
          </button>`).join('')}`).join('');
    wrap.innerHTML = html || `<div class="text-muted">${_catalogue.size ? 'Everything is placed.' : 'No devices found.'}</div>`;
    wrap.querySelectorAll('[data-ieee]').forEach(el => {
        el.addEventListener('dragstart', e => {
            e.dataTransfer.setData('text/x-zmm-ieee', el.dataset.ieee);
            e.dataTransfer.effectAllowed = 'copy';
            const info = deviceInfo(el.dataset.ieee);
            // Hotspot at the pin's point: the tip is where the device lands.
            e.dataTransfer.setDragImage(dragGhost(info.kind, info.name), 20, 52);
        });
        el.addEventListener('click', () => {
            const arm = _state.placing !== el.dataset.ieee;
            _state.placing = arm ? el.dataset.ieee : null;
            _state.tool = arm ? 'place' : 'select';
            renderToolbar(); renderPalette();
            if (arm) { closeMobileDrawers(); toast('info', 'Place it', 'Tap the plan where it is.'); }
        });
    });
}

function renderDeviceParts(lvl) {
    const parts = [];
    for (const d of lvl.devices || []) {
        const info = deviceInfo(d.ieee);
        // Heating's view is heating's devices only; the rest stay on the plan.
        if (_view === 'heating' && info.kind !== 'heating') continue;
        const p = modelToSvg(d);
        const sel = isSelected('device', d.ieee);
        const offline = _state.showMesh && _state.mesh?.nodes?.[d.ieee]?.online === false;
        const cls = `fp-device fp-device-${info.kind} ${sel ? 'fp-selected' : ''}`;
        // The coordinator is the mesh's root: square, and larger.
        const marker = info.kind === 'coordinator'
            ? `<rect class="${cls}" x="${p.x - 0.2}" y="${p.y - 0.2}" width="0.4" height="0.4" rx="0.06"
                     stroke-width="${sel ? 0.05 : 0.03}"/>`
            : `<circle class="${cls}" cx="${p.x}" cy="${p.y}" r="0.14" stroke-width="${sel ? 0.05 : 0.025}"/>`;
        parts.push(`
          <g data-kind="device" data-id="${escapeAttr(d.ieee)}" style="cursor:grab"
             class="${offline ? 'fp-offline' : ''}">
            ${marker}
            <text class="fp-device-label" x="${p.x}" y="${p.y + 0.32}" font-size="0.13"
                  text-anchor="middle" pointer-events="none">${escapeHtml(info.name)}</text>
          </g>`);
    }
    return parts;
}

function renderDeviceProps(ieee) {
    const lvl = currentLevel();
    const d = (lvl.devices || []).find(x => x.ieee === ieee);
    if (!d) return '';
    const info = deviceInfo(ieee);
    const room = (lvl.rooms || []).find(r => pointInPolygon(d, r.polygon));
    const isSensor = _availableDevices.sensors.some(x => String(x.ieee).toLowerCase() === ieee);
    const isContact = _availableDevices.contacts.some(x => String(x.ieee).toLowerCase() === ieee);
    const isTrv = _availableDevices.trvs.some(x => String(x.ieee).toLowerCase() === ieee);
    return `
      <div class="text-muted small text-uppercase mb-2">Placed device</div>
      <div class="fw-semibold mb-1"><span class="fp-device-dot fp-device-${info.kind}"></span>${escapeHtml(info.name)}</div>
      <div class="small text-muted mb-2">${escapeHtml(KIND_LABEL[info.kind] || 'Other')} · <code>${escapeHtml(ieee)}</code></div>
      <div class="small mb-2"><i class="fas fa-door-closed me-1"></i>${room
          ? `In <strong>${escapeHtml(room.name || room.id)}</strong>`
          : '<span class="text-warning">Not inside a room — drag it into one.</span>'}</div>
      <div class="mb-2"><label class="form-label small">Mounting height (m)</label>
        <input type="number" step="0.1" min="0" max="10" class="form-control form-control-sm"
               id="fpDevHeight" value="${d.height_m ?? ''}" placeholder="Not set"/></div>
      ${isSensor ? `<button class="btn btn-sm btn-outline-primary w-100 mb-2" data-action="device-to-sensor" ${room ? '' : 'disabled'}>
          <i class="fas fa-thermometer-half me-1"></i>Use as this room's heating sensor</button>` : ''}
      ${isContact ? `<button class="btn btn-sm btn-outline-primary w-100 mb-2" data-action="device-to-contact">
          <i class="fas fa-link me-1"></i>Attach to the nearest window or door</button>` : ''}
      ${isTrv ? `<div class="form-text small mb-2">To use it for heating, select its radiator and pick it as the TRV.
          It will then sit on the radiator.</div>` : ''}
      <button class="btn btn-sm btn-outline-danger w-100" data-action="device-unplace">
        <i class="fas fa-xmark me-1"></i>Take off the plan</button>`;
}

function bindDeviceProps(ieee) {
    const lvl = currentLevel();
    const d = (lvl.devices || []).find(x => x.ieee === ieee);
    if (!d) return;
    const root = document.getElementById('fpProps');
    root.querySelector('#fpDevHeight')?.addEventListener('change', e => {
        const v = parseFloat(e.target.value);
        if (Number.isFinite(v)) d.height_m = v; else delete d.height_m;
    });
    root.querySelector('[data-action="device-unplace"]')?.addEventListener('click', () => {
        unplaceDevice(ieee);
        _state.selection = null;
        renderScene(); renderProps();
    });
    root.querySelector('[data-action="device-to-sensor"]')?.addEventListener('click', () => {
        const room = lvl.rooms.find(r => pointInPolygon(d, r.polygon));
        if (!room) return;
        const id = genId('sens');
        lvl.sensors.push({ id, room_id: room.id, ieee, kind: 'temp_sensor', x: d.x, y: d.y,
                           height_m: d.height_m ?? 1.5,
                           primary: !lvl.sensors.some(x => x.room_id === room.id) });
        unplaceDevice(ieee);
        _state.selection = { kind: 'sensor', id };
        renderScene(); renderProps();
    });
    root.querySelector('[data-action="device-to-contact"]')?.addEventListener('click', () => {
        const op = nearestOpening(lvl, d);
        if (!op) { toast('warn', 'No window or door nearby', 'Drag it within 60 cm of one first.'); return; }
        addContact(op);
        lvl.contacts[lvl.contacts.length - 1].ieee = ieee;
        unplaceDevice(ieee);
        renderScene(); renderProps();
    });
}

function bindDeviceLayerEvents() {
    document.getElementById('fpPaletteSearch')?.addEventListener('input', renderPalette);
    const wrap = document.getElementById('fpCanvasWrap');
    new ResizeObserver(() => { if (_needsFit && _state) zoomFit(); }).observe(wrap);
    wrap.addEventListener('dragover', e => {
        if (e.dataTransfer.types.includes('text/x-zmm-ieee')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; }
    });
    wrap.addEventListener('drop', e => {
        const ieee = e.dataTransfer.getData('text/x-zmm-ieee');
        if (!ieee || !_state) return;
        e.preventDefault();
        placeDevice(ieee, clientToSvgModel(e));
    });
    document.getElementById('fpToggleCoverage').addEventListener('change', e => {
        _state.showCoverage = e.target.checked;
        if (_state.showCoverage) loadCoverage();
        else _coverageToken++;                      // drop any answer still on its way
        syncCoverageControls(); renderScene();
    });
    document.getElementById('fpCoverageSource').addEventListener('change', e => showCoverageSource(e.target.value));
    document.getElementById('fpCoverageCompare').addEventListener('change', e => compareCoverageWith(e.target.value));
    document.getElementById('fpToggleMesh').addEventListener('change', async e => {
        _state.showMesh = e.target.checked;
        if (_state.showMesh) await loadMesh();
        syncMeshControls(); renderScene();
    });
    document.getElementById('fpToggleDaylight').addEventListener('change', async e => {
        _state.showDaylight = e.target.checked;
        if (_state.showDaylight) await loadDaylight();
        syncDaylightControls(); renderScene();
    });
    document.getElementById('fpDaylightTime').addEventListener('input', e => {
        _state.daylightIndex = parseInt(e.target.value, 10) || 0;
        syncDaylightControls(); renderScene();
    });
    document.getElementById('fpToggleMap').addEventListener('change', e => {
        _state.showMap = e.target.checked;
        syncMapControls(); renderScene();
    });
    document.getElementById('fpMapOpacity').addEventListener('input', e => {
        _state.plan.map = { anchor_x_m: 0, anchor_y_m: 0, ...(_state.plan.map || {}),
                            opacity: parseFloat(e.target.value) };
        renderScene();
    });
    document.getElementById('fpMapAnchor').addEventListener('click', () => {
        setTool('map-anchor');
        closeMobileDrawers();
        toast('info', 'Home pin', 'Click the spot on the plan where the map pin for your address sits.');
    });
}

function syncMapControls() {
    if (!_state) return;
    const on = !!_state.showMap && !!_home;
    document.getElementById('fpToggleMap').checked = !!_state.showMap;
    document.getElementById('fpToggleMap').disabled = !_home;
    document.getElementById('fpMapControls').classList.toggle('d-none', !on);
    document.getElementById('fpMapMissing').classList.toggle('d-none', !!_home);
    document.getElementById('fpMapOpacity').value = String(_state.plan.map?.opacity ?? 0.6);
}

const MAP_ZOOM = 19;
const MAP_RADIUS_TILES = 2;   // a 5×5 block: ~150 m across at UK latitudes

/**
 * OSM tiles around the home pin, in plan metres, north turned to the compass.
 * The pin sits at ``plan.map.anchor_*``; true north is ``north_offset_deg``
 * clockwise from plan-up, so the tile block is rotated by that much.
 */
function renderMapParts() {
    if (!_home) return [];
    const { lat, lon } = _home;
    const n = 2 ** MAP_ZOOM;
    const latRad = lat * Math.PI / 180;
    const fx = (lon + 180) / 360 * n;
    const fy = (1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2 * n;
    const tileM = 40075016.686 * Math.cos(latRad) / n;      // metres per tile edge
    const anchor = modelToSvg({ x: _state.plan.map?.anchor_x_m || 0, y: _state.plan.map?.anchor_y_m || 0 });
    const opacity = _state.plan.map?.opacity ?? 0.6;
    const tx0 = Math.floor(fx), ty0 = Math.floor(fy);
    const tiles = [];
    for (let dy = -MAP_RADIUS_TILES; dy <= MAP_RADIUS_TILES; dy++) {
        for (let dx = -MAP_RADIUS_TILES; dx <= MAP_RADIUS_TILES; dx++) {
            const tx = tx0 + dx, ty = ty0 + dy;
            const x = anchor.x + (tx - fx) * tileM, y = anchor.y + (ty - fy) * tileM;
            tiles.push(`<image href="/api/map/tiles/${MAP_ZOOM}/${tx}/${ty}.png" x="${x}" y="${y}"
                               width="${tileM}" height="${tileM}" preserveAspectRatio="none"/>`);
        }
    }
    const rot = _state.plan.north_offset_deg || 0;
    return [`<g class="fp-map" opacity="${opacity}" pointer-events="none"
               transform="rotate(${rot} ${anchor.x} ${anchor.y})">${tiles.join('')}
               <circle class="fp-map-pin" cx="${anchor.x}" cy="${anchor.y}" r="0.2"/></g>`];
}

// signal coverage — docs/signal-coverage.md

// The last estimate shows at once, from the hub's snapshots, while a fresh
// one is worked out; each fresh one is kept as a snapshot, and any two can be
// compared. One device's own coverage can be shown instead of the best from
// every router. docs/signal-coverage.md § Snapshots.

let _coverageToken = 0;   // bumped to drop answers that arrive after the user moved on

async function getJson(url) {
    const res = await fetch(url);
    const r = await res.json().catch(() => null);
    if (!res.ok || !r?.success) throw new Error(r?.error || r?.detail || `The hub said ${res.status}.`);
    return r;
}

async function loadCoverage() {
    const token = ++_coverageToken;
    _state.coverageMeta = { ..._state.coverageMeta, refreshing: true, error: null };
    syncCoverageControls();
    // 1. What was last worked out, straight away.
    if (!_state.coverage) {
        _state.coverageMeta.lookingUp = true;
        syncCoverageControls();
        try {
            const r = await getJson('/api/floor-plan/coverage/latest');
            if (token !== _coverageToken) return;
            if (r.snapshot) {
                _state.coverage = r.snapshot;
                _state.coverageMeta = { id: r.snapshot.id, taken_at: r.snapshot.taken_at,
                                        checked_at: r.snapshot.checked_at, stale: true, refreshing: true,
                                        planChanged: r.snapshot.plan_hash !== r.plan_hash };
                syncCoverageControls(); renderScene();
            }
        } catch (e) {
            log.warn('No saved heatmap', e);
        }
        if (token !== _coverageToken) return;
        _state.coverageMeta.lookingUp = false;
        syncCoverageControls();
    }
    // 2. A fresh estimate, which the hub keeps as the next snapshot.
    try {
        const r = await getJson('/api/floor-plan/coverage');
        if (token !== _coverageToken) return;
        const before = _state.coverageMeta;
        _state.coverage = r;
        _state.coverageMeta = { id: r.snapshot?.id, taken_at: r.snapshot?.taken_at ?? Date.now() / 1000,
                                checked_at: r.snapshot?.checked_at, stale: false, refreshing: false,
                                changed: !!(r.snapshot?.new && before?.id && before.id !== r.snapshot.id),
                                previousId: before?.id && before.id !== r.snapshot?.id ? before.id : null };
        // A single device's view is redrawn from the new learning.
        if (_state.coverageSource) showCoverageSource(_state.coverageSource);
    } catch (e) {
        if (token !== _coverageToken) return;
        _state.coverageMeta = { ..._state.coverageMeta, refreshing: false, error: e.message };
        if (!_state.coverage) toast('warn', 'Signal heatmap', escapeHtml(e.message));
    }
    await loadCoverageHistory(token);
    if (token !== _coverageToken) return;
    // Keep a chosen comparison; the diff is against whatever is shown now.
    if (_state.coverageCompare) _state.coverageCompare = buildComparison(_state.coverageCompare.then);
    syncCoverageControls(); renderScene();
}

async function loadCoverageHistory(token) {
    try {
        const r = await getJson('/api/floor-plan/coverage/history');
        if (token === _coverageToken) _state.coverageHistory = r.snapshots || [];
    } catch (e) {
        log.warn('No heatmap history', e);
    }
}

async function showCoverageSource(ieee) {
    _state.coverageSource = ieee || '';
    if (!ieee) { _state.coverageView = null; syncCoverageControls(); renderScene(); return; }
    const token = _coverageToken;
    _state.coverageMeta = { ..._state.coverageMeta, sourceLoading: true };
    syncCoverageControls();
    try {
        const r = await getJson(`/api/floor-plan/coverage?source=${encodeURIComponent(ieee)}`);
        if (token !== _coverageToken || _state.coverageSource !== ieee) return;
        _state.coverageView = { source: ieee, levels: r.levels };
    } catch (e) {
        toast('warn', 'Device coverage', escapeHtml(e.message));
        _state.coverageSource = ''; _state.coverageView = null;
    }
    _state.coverageMeta = { ..._state.coverageMeta, sourceLoading: false };
    syncCoverageControls(); renderScene();
}

async function compareCoverageWith(id) {
    if (!id) { _state.coverageCompare = null; syncCoverageControls(); renderScene(); return; }
    try {
        const r = await getJson(`/api/floor-plan/coverage/snapshots/${encodeURIComponent(id)}`);
        _state.coverageCompare = buildComparison(r.snapshot);
    } catch (e) {
        toast('warn', 'Compare', escapeHtml(e.message));
        _state.coverageCompare = null;
    }
    syncCoverageControls(); renderScene();
}

/** What changed from snapshot `then` to what is shown now: a per-cell dB
 *  difference where the two grids line up, the headline figures, each
 *  device's signal, and what the model learned about the walls. */
function buildComparison(then) {
    const now = _state.coverage;
    const fields = {};
    let aligned = true;
    for (const entry of now.levels || []) {
        const old = (then.levels || []).find(l => l.level_id === entry.level_id);
        const a = entry.field, b = old?.field;
        if (!b || a.nx !== b.nx || a.ny !== b.ny || a.h !== b.h || a.x0 !== b.x0 || a.y0 !== b.y0) {
            aligned = false; continue;
        }
        fields[entry.level_id] = { ...a, data: a.data.map((v, k) => v - b.data[k]), urls: {} };
    }
    const was = new Map((then.device_signal || []).map(d => [d.ieee, d]));
    const devices = (now.device_signal || []).map(d => ({ now: d, then: was.get(d.ieee) }));
    const gone = (then.device_signal || []).filter(d => !devices.some(x => x.now.ieee === d.ieee));
    return { then, fields, aligned, devices, gone, planChanged: now.plan_hash && then.plan_hash
             ? now.plan_hash !== then.plan_hash : null };
}

function when(ts) {
    const d = new Date(ts * 1000);
    const mins = Math.round((Date.now() - d) / 60000);
    const ago = mins < 1 ? 'just now' : mins < 60 ? `${mins} min ago`
        : mins < 48 * 60 ? `${Math.round(mins / 60)} h ago` : `${Math.round(mins / 1440)} days ago`;
    const sameDay = d.toDateString() === new Date().toDateString();
    const at = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return `${sameDay ? at : `${d.toLocaleDateString([], { day: 'numeric', month: 'short' })} ${at}`} (${ago})`;
}

function signed(v, unit = '') {
    const r = Math.round(v * 10) / 10;
    return `${r > 0 ? '+' : r < 0 ? '−' : '±'}${Math.abs(r)}${unit}`;
}

function syncCoverageControls() {
    const box = document.getElementById('fpCoverageControls');
    box.classList.toggle('d-none', !_state.showCoverage);
    if (!_state.showCoverage) return;
    const cov = _state.coverage, meta = _state.coverageMeta || {};
    const spin = '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>';
    const status = document.getElementById('fpCoverageStatus');
    if (!cov) {
        status.innerHTML = meta.refreshing
            ? `${spin}${meta.lookingUp ? 'Loading the last heatmap…' : 'Working out the signal across the house…'}`
            : `<span class="text-warning-emphasis">${escapeHtml(meta.error || 'No estimate yet.')}</span>`;
    } else if (meta.stale) {
        status.innerHTML = `<div><i class="fas fa-clock-rotate-left me-1"></i>Showing the heatmap from ${when(meta.taken_at)}.</div>`
            + (meta.planChanged ? '<div class="text-muted">The plan has changed since.</div>' : '')
            + (meta.refreshing ? `<div>${spin}Checking for changes…</div>`
               : `<div class="text-warning-emphasis">Couldn't refresh: ${escapeHtml(meta.error || 'unknown error')}</div>`);
    } else {
        status.innerHTML = `<i class="fas fa-circle-check text-success me-1"></i>Up to date`
            + (meta.changed ? ' — it has changed since the last one.'
               : meta.checked_at && meta.taken_at && meta.checked_at - meta.taken_at > 60
                 ? ` — no change since ${when(meta.taken_at)}.` : '.')
            + (meta.sourceLoading ? `<div>${spin}Working out that device's coverage…</div>` : '');
    }
    document.getElementById('fpCoveragePickers').classList.toggle('d-none', !cov);
    if (!cov) {
        document.getElementById('fpCoverageModel').innerHTML = '';
        document.getElementById('fpCoverageAdvice').innerHTML = '';
        return;
    }

    // Show: the best from every router, or one device's own coverage.
    const devices = cov.devices || [];
    const group = (label, list) => list.length ? `<optgroup label="${label}">${list.map(d =>
        `<option value="${escapeHtml(d.ieee)}" ${d.ieee === _state.coverageSource ? 'selected' : ''}>${
            escapeHtml(d.name)}${d.online ? '' : ' (offline)'}</option>`).join('')}</optgroup>` : '';
    document.getElementById('fpCoverageSource').innerHTML =
        `<option value="">Best signal from every router</option>`
        + group('Coordinator', devices.filter(d => d.role === 'Coordinator'))
        + group('Routers', devices.filter(d => d.role === 'Router'))
        + group('Other devices — where each would reach', devices.filter(d => d.role !== 'Coordinator' && d.role !== 'Router'));

    // Compare: only the whole-mesh view is kept, so only it compares.
    const cmpSel = document.getElementById('fpCoverageCompare');
    const past = (_state.coverageHistory || []).filter(h => h.id !== meta.id);
    cmpSel.disabled = !!_state.coverageSource || !past.length;
    cmpSel.innerHTML = `<option value="">${past.length ? 'Nothing' : 'No earlier snapshots yet'}</option>`
        + past.map(h => `<option value="${escapeHtml(h.id)}" ${h.id === _state.coverageCompare?.then.id ? 'selected' : ''}>${
            new Date(h.taken_at * 1000).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}${
            h.summary?.usable_pct != null ? ` · ${h.summary.usable_pct}% usable` : ''} · ${h.weak} struggling</option>`).join('');
    document.getElementById('fpCoverageCompareResult').innerHTML =
        _state.coverageCompare && !_state.coverageSource ? comparisonHtml(_state.coverageCompare) : '';

    const { model, calibration, weak, suggestions, thresholds } = cov;
    const learned = model.samples
        ? `Learned from ${model.samples} link readings (±${model.rmse_db} dB):`
        : 'No links to learn from yet — textbook values:';
    document.getElementById('fpCoverageModel').innerHTML = `
        <div>${learned}</div>
        <div>Inside wall <strong>${model.int} dB</strong> · outside/party wall
             <strong>${model.ext} dB</strong> · floor <strong>${model.floor} dB</strong></div>
        <div>Falls off at n=${model.n}${calibration.fitted
            ? '' : ' · LQI→dBm not calibrated, so dBm are rough'}</div>
        ${model.rough ? `<div class="text-warning-emphasis"><i class="fas fa-triangle-exclamation me-1"></i>
             Too few readings to be sure — treat this as a rough guide.</div>` : ''}`;
    document.getElementById('fpCoverageAdvice').innerHTML = suggestions.length
        ? `<div class="fw-semibold">${weak.length} device${weak.length === 1 ? '' : 's'} struggling
             (below ${thresholds.weak_dbm} dBm)</div>`
          + suggestions.map((s, i) => `
            <div class="border rounded p-1 mt-1">
              <div><i class="fas fa-tower-broadcast me-1"></i><strong>Repeater ${i + 1}</strong>
                   in ${escapeHtml(s.room_name || s.room_id)}</div>
              <div class="text-muted">Would lift ${s.fixes.map(f =>
                  `${escapeHtml(f.name)} (${f.before_dbm} → ${f.after_dbm} dBm)`).join(', ')}</div>
              <div class="text-muted">It would hear the mesh at ${s.uplink_dbm} dBm.</div>
            </div>`).join('')
        : (weak.length
            ? `<div>${weak.length} device${weak.length === 1 ? ' is' : 's are'} struggling, but no one
                 spot both hears the mesh and reaches them. Try moving the coordinator, or a repeater
                 closer to each.</div>`
            : '<div class="text-success">Every placed device has a usable signal.</div>');
}

function comparisonHtml(cmp) {
    const now = _state.coverage, then = cmp.then;
    const a = then.summary || {}, b = now.summary || {};
    const row = (label, x, y, unit, better = 1) => {
        if (x == null || y == null) return '';
        const d = y - x, cls = Math.abs(d) < 0.05 ? 'text-muted' : (d * better > 0 ? 'text-success' : 'text-danger');
        return `<div>${label}: ${x}${unit} → ${y}${unit} <span class="${cls}">(${signed(d, unit)})</span></div>`;
    };
    const names = list => list.map(d => escapeHtml(d.name)).join(', ');
    const newlyWeak = cmp.devices.filter(d => d.now.weak && d.then && !d.then.weak).map(d => d.now);
    const recovered = cmp.devices.filter(d => !d.now.weak && d.then?.weak).map(d => d.now);
    const moved = cmp.devices.filter(d => d.then && Math.abs(d.now.dbm - d.then.dbm) >= 3)
        .sort((p, q) => Math.abs(q.now.dbm - q.then.dbm) - Math.abs(p.now.dbm - p.then.dbm)).slice(0, 6);
    const added = cmp.devices.filter(d => !d.then).map(d => d.now);
    const m0 = then.model || {}, m1 = now.model || {};
    const wall = (label, k) => m0[k] !== m1[k] ? `${label} ${m0[k]} → ${m1[k]} dB` : null;
    const walls = [wall('inside wall', 'int'), wall('outside/party wall', 'ext'), wall('floor', 'floor')].filter(Boolean);
    return `<div class="border rounded p-2 mb-2">
        <div class="fw-semibold mb-1">Since ${escapeHtml(when(then.taken_at))}</div>
        ${row('Usable floor', a.usable_pct, b.usable_pct, '%')}
        ${row('Median signal', a.median_dbm, b.median_dbm, ' dBm')}
        ${row('Struggling devices', (then.weak || []).length, (now.weak || []).length, '', -1)}
        ${newlyWeak.length ? `<div class="text-danger">Now struggling: ${names(newlyWeak)}</div>` : ''}
        ${recovered.length ? `<div class="text-success">Recovered: ${names(recovered)}</div>` : ''}
        ${moved.length ? `<div class="mt-1">Biggest changes:</div><ul class="mb-1 ps-3">${moved.map(d =>
            `<li>${escapeHtml(d.now.name)}: ${d.then.dbm} → ${d.now.dbm} dBm
               <span class="${d.now.dbm > d.then.dbm ? 'text-success' : 'text-danger'}">(${signed(d.now.dbm - d.then.dbm)})</span>${
               d.now.measured ? '' : ' <span class="text-muted">predicted</span>'}</li>`).join('')}</ul>`
          : '<div class="text-muted">No device moved by 3 dB or more.</div>'}
        ${added.length ? `<div class="text-muted">New since: ${names(added)}</div>` : ''}
        ${cmp.gone.length ? `<div class="text-muted">Offline or removed since: ${names(cmp.gone)}</div>` : ''}
        ${walls.length ? `<div class="mt-1 text-muted">Learned: ${walls.join(' · ')}
            (${m0.samples ?? 0} → ${m1.samples ?? 0} readings)</div>` : ''}
        ${cmp.planChanged ? `<div class="mt-1 text-warning-emphasis">The plan changed between them${
            cmp.aligned ? '.' : ', so the map can\'t be compared cell by cell.'}</div>` : ''}
        ${Object.keys(cmp.fields).length ? `<div class="mt-1 d-flex align-items-center gap-1">
            <span class="fp-delta-key fp-delta-worse"></span>worse
            <span class="fp-delta-key fp-delta-better ms-2"></span>better
            <span class="text-muted ms-1">(map shows the difference)</span></div>` : ''}
      </div>`;
}

function coverageField(lvl) {
    const cmp = !_state.coverageSource && _state.coverageCompare;
    if (cmp && cmp.fields[lvl.id]) return { f: cmp.fields[lvl.id], mode: 'delta' };
    const levels = (_state.coverageSource && _state.coverageView?.source === _state.coverageSource)
        ? _state.coverageView.levels : _state.coverage.levels;
    const entry = (levels || []).find(l => l.level_id === lvl.id);
    if (!entry) return null;
    // fieldToImage caches its PNGs on the field object, so keep the one we build.
    if (!entry._f) entry._f = { ...entry.field, urls: {} };
    return { f: entry._f, mode: 'rssi' };
}

function renderCoverageParts(lvl) {
    const got = coverageField(lvl);
    if (!got) return [];
    const { f, mode } = got;
    // Clipped to the rooms: the field is computed past the walls so the weak
    // contour follows the signal, but only inside the house is worth showing.
    const clip = `cov_${lvl.id.replace(/[^a-z0-9]/gi, '_')}`;
    const out = [`<defs><clipPath id="${clip}">${
        lvl.rooms.map(r => `<path d="${polygonToPath(r.polygon)}"/>`).join('')}</clipPath></defs>`,
        `<image href="${fieldToImage(f, mode, 1)}"
                    x="${f.x0}" y="${-(f.y0 + f.ny * f.h)}"
                    width="${f.nx * f.h}" height="${f.ny * f.h}"
                    clip-path="url(#${clip})" preserveAspectRatio="none" pointer-events="none"/>`];
    if (mode === 'rssi') {
        const weakLine = fieldContourPath(f, _state.coverage.thresholds.weak_dbm);
        if (weakLine) out.push(`<path class="fp-weak-edge" d="${weakLine}"
                                      clip-path="url(#${clip})" pointer-events="none"/>`);
    }
    // One device's view: ring the device it is from.
    const src = _state.coverageSource && (_state.coverage.devices || []).find(d => d.ieee === _state.coverageSource);
    if (src && src.level_id === lvl.id) {
        const p = modelToSvg(src);
        out.push(`<circle class="fp-coverage-source" cx="${p.x}" cy="${p.y}" r="0.38" pointer-events="none"/>
          <text class="fp-repeater-label" x="${p.x}" y="${p.y - 0.5}" font-size="0.16"
                text-anchor="middle" pointer-events="none">${escapeHtml(src.name)}</text>`);
    }
    for (const w of _state.coverage.weak) {
        if (w.level_id !== lvl.id) continue;
        const p = modelToSvg(w);
        out.push(`<circle class="fp-weak-ring" cx="${p.x}" cy="${p.y}" r="0.3" pointer-events="none"/>
          <text class="fp-weak-label" x="${p.x}" y="${p.y - 0.42}" font-size="0.14"
                text-anchor="middle" pointer-events="none">${w.dbm} dBm${w.lqi != null ? ` · LQI ${w.lqi}` : ''}</text>`);
    }
    if (_state.coverageSource || mode === 'delta') return out;
    (_state.coverage.suggestions || []).forEach((s, i) => {
        if (s.level_id !== lvl.id) return;
        const p = modelToSvg(s);
        out.push(`<g pointer-events="none">
            <circle class="fp-repeater" cx="${p.x}" cy="${p.y}" r="0.32"/>
            <text class="fp-repeater-label" x="${p.x}" y="${p.y + 0.09}" font-size="0.26"
                  text-anchor="middle">${i + 1}</text>
            <text class="fp-repeater-label" x="${p.x}" y="${p.y + 0.62}" font-size="0.15"
                  text-anchor="middle">repeater here</text></g>`);
    });
    return out;
}

// mesh layer — docs/signal-coverage.md

async function loadMesh() {
    try {
        const r = await fetch('/api/floor-plan/mesh').then(r => r.json());
        if (!r?.success) throw new Error(r?.error || r?.detail || 'Mesh unavailable');
        _state.mesh = r;
    } catch (e) {
        _state.mesh = null;
        toast('warn', 'Mesh', e.message);
    }
}

function syncMeshControls() {
    const on = !!_state.showMesh && !!_state.mesh;
    document.getElementById('fpMeshControls').classList.toggle('d-none', !on);
    if (!on) return;
    const placed = livePositions();
    const nodes = Object.keys(_state.mesh.nodes || {});
    const missing = nodes.filter(i => !placed.has(i)).length;
    document.getElementById('fpMeshNote').textContent = missing
        ? `${missing} of ${nodes.length} devices aren't placed; their links end in a stub.`
        : 'Every device on the mesh is placed.';
}

function openingCentre(lvl, openingId) {
    const op = lvl.openings.find(o => o.id === openingId);
    const wall = op && lvl.walls.find(w => w.id === op.wall_id);
    if (!wall) return null;
    const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) || 1;
    const t = op.offset_m + op.width_m / 2;
    return { x: wall.x1 + (wall.x2 - wall.x1) * t / wlen, y: wall.y1 + (wall.y2 - wall.y1) * t / wlen };
}

/**
 * ieee → { levelId, x, y } for the plan being edited, by the one-position
 * rule: the radiator for a fitted TRV, the sensor entry, the opening for a
 * contact, else devices[]. Mirrors floor_plan.placed_devices on the server.
 */
function livePositions() {
    const out = new Map();
    for (const lvl of _state.plan.levels) {
        const add = (ieee, p) => { if (ieee && p && !out.has(ieee)) out.set(ieee, { levelId: lvl.id, ...p }); };
        lvl.radiators.forEach(r => r.trv_ieee && add(r.trv_ieee, radiatorCenter(r, lvl)));
        lvl.sensors.forEach(x => x.ieee && x.x != null && add(x.ieee, { x: x.x, y: x.y }));
        lvl.contacts.forEach(c => c.ieee && add(c.ieee, openingCentre(lvl, c.opening_id)));
        (lvl.devices || []).forEach(d => add(d.ieee, { x: d.x, y: d.y }));
    }
    return out;
}

function renderMeshParts(lvl) {
    const pos = livePositions();
    const nodes = _state.mesh.nodes || {};
    const name = i => nodes[i]?.name || i;
    const levelName = id => _state.plan.levels.find(l => l.id === id)?.name || id;
    const parts = [];
    const stubCount = new Map();
    for (const link of _state.mesh.links || []) {
        if (!link.online) continue;
        const pa = pos.get(link.a), pb = pos.get(link.b);
        const hereA = pa?.levelId === lvl.id, hereB = pb?.levelId === lvl.id;
        if (!hereA && !hereB) continue;
        const title = `${escapeHtml(name(link.a))} ↔ ${escapeHtml(name(link.b))} · LQI ${link.lqi_ab ?? '–'} / ${link.lqi_ba ?? '–'}`
            + (link.rel_ab ? ` · ${escapeHtml(name(link.b))} is its ${escapeHtml(link.rel_ab)}` : '');
        if (hereA && hereB) {
            const A = modelToSvg(pa), B = modelToSvg(pb);
            parts.push(`<g class="fp-link fp-link-${link.band}">
                <line x1="${A.x}" y1="${A.y}" x2="${B.x}" y2="${B.y}" stroke-width="0.06"><title>${title}</title></line>
                <text class="fp-link-label" x="${(A.x + B.x) / 2}" y="${(A.y + B.y) / 2 - 0.08}" font-size="0.13"
                      text-anchor="middle" pointer-events="none">${link.lqi ?? ''}</text></g>`);
            continue;
        }
        // One end here: a short stub fanning out from it, labelled with the far end.
        const [near, far, farPos] = hereA ? [link.a, link.b, pb] : [link.b, link.a, pa];
        const k = stubCount.get(near) || 0;
        stubCount.set(near, k + 1);
        const angle = (-60 + k * 35) * Math.PI / 180;
        const P = pos.get(near), end = { x: P.x + 0.9 * Math.cos(angle), y: P.y + 0.9 * Math.sin(angle) };
        const A = modelToSvg(P), B = modelToSvg(end);
        const where = farPos ? `↕ ${levelName(farPos.levelId)}` : 'not placed';
        parts.push(`<g class="fp-link fp-link-stub fp-link-${link.band}">
            <line x1="${A.x}" y1="${A.y}" x2="${B.x}" y2="${B.y}" stroke-width="0.05"><title>${title}</title></line>
            <text class="fp-link-label" x="${B.x + 0.05}" y="${B.y}" font-size="0.12"
                  pointer-events="none">${escapeHtml(name(far))} (${escapeHtml(where)}) · ${link.lqi ?? ''}</text></g>`);
    }
    return parts;
}

// daylight layer — docs/daylight.md §7

async function loadDaylight() {
    _state.daylightError = null;
    try {
        const res = await fetch('/api/floor-plan/daylight?step_minutes=30');
        const r = await res.json().catch(() => null);
        if (!res.ok) throw new Error(r?.detail || `The hub said ${res.status}.`);
        if (!r?.success) throw new Error(r?.error || 'No estimate');
        _state.daylight = r;
        _state.daylightIndex = daylightNowIndex(r);   // start at the step nearest now
    } catch (e) {
        _state.daylight = null;
        _state.daylightError = e.message;
    }
}

function syncDaylightControls() {
    const box = document.getElementById('fpDaylightControls');
    const note = document.getElementById('fpDaylightNote');
    const on = !!_state.showDaylight && !!_state.daylight;
    // Say why rather than going quiet: an empty layer with no explanation is
    // indistinguishable from a broken one.
    box.classList.toggle('d-none', !_state.showDaylight);
    document.getElementById('fpDaylightTime').classList.toggle('d-none', !on);
    document.getElementById('fpDaylightReadout').classList.toggle('d-none', !on);
    if (!on) {
        note.innerHTML = _state.daylightError
            ? `<span class="text-warning-emphasis">${escapeHtml(_state.daylightError)}</span>`
            : 'Working it out…';
        return;
    }
    const lit = (_state.daylight.rooms || []).length;
    note.innerHTML = lit
        ? `Estimated from the saved plan's windows and today's weather — ${lit} room`
          + `${lit === 1 ? '' : 's'} with a window to the outside.`
        : `<span class="text-warning-emphasis">No room has a window on an outside wall.</span>
           Draw the windows on the outside walls, set the compass, then save — the estimate
           reads the saved plan.`;
    const d = _state.daylight, i = _state.daylightIndex;
    const slider = document.getElementById('fpDaylightTime');
    slider.max = String(d.times.length - 1);
    slider.value = String(i);
    document.getElementById('fpDaylightClock').textContent =
        new Date(d.times[i] * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    document.getElementById('fpDaylightOutdoor').textContent = `${formatLux(d.outdoor[i])}`;
}

function formatLux(v) {
    return v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}k lx` : `${Math.round(v)} lx`;
}

/** Night blue through to sunlit yellow, on a log scale from 1 to ~3000 lux. */
function daylightFill(lux) {
    const t = Math.max(0, Math.min(1, Math.log10(Math.max(1, lux)) / 3.5));
    const lerp = (a, b) => Math.round(a + (b - a) * t);
    return `rgba(${lerp(30, 250)},${lerp(41, 204)},${lerp(90, 21)},0.45)`;
}

function renderDaylightParts(lvl) {
    const d = _state.daylight, i = _state.daylightIndex;
    const byRoom = new Map(d.rooms.filter(r => r.level_id === lvl.id).map(r => [r.room_id, r]));
    const sky = d.sky?.[i] || null;
    const parts = [], defs = [];
    for (const room of lvl.rooms) {
        const est = byRoom.get(room.id);
        const c = modelToSvg(polygonCentroid(room.polygon));
        if (!est) {
            parts.push(`<path d="${polygonToPath(room.polygon)}" class="fp-daylight-none" pointer-events="none"/>
              <text class="fp-daylight-label" x="${c.x}" y="${c.y - 0.3}" font-size="0.14"
                    text-anchor="middle" pointer-events="none">no outside window</text>`);
            continue;
        }
        const lux = est.lux[i] || 0;
        // Light across the room: bright by the glass and in the sun patch,
        // falling away with distance and behind walls. docs/daylight.md §8.
        const lf = sky ? roomLightFieldParts(room, lvl, sky, lux) : null;
        if (!lf) {
            parts.push(`<path d="${polygonToPath(room.polygon)}" fill="${daylightFill(lux)}" pointer-events="none"/>
              <text class="fp-daylight-label" x="${c.x}" y="${c.y - 0.3}" font-size="0.16"
                    text-anchor="middle" pointer-events="none">${est.sun[i] ? '☀ ' : ''}${formatLux(lux)}</text>`);
            continue;
        }
        defs.push(...lf.defs);
        parts.push(...lf.parts);
        parts.push(`<text class="fp-daylight-label" x="${c.x}" y="${c.y - 0.3}" font-size="0.16"
                text-anchor="middle" pointer-events="none">${est.sun[i] ? '☀ ' : ''}${formatLux(lux)} avg</text>
          <text class="fp-daylight-label fp-daylight-range" x="${c.x}" y="${c.y - 0.1}" font-size="0.12"
                text-anchor="middle" pointer-events="none">${formatLux(lf.f.max)} → ${formatLux(lf.f.min)}</text>`);
    }
    return defs.length ? [`<defs>${defs.join('')}</defs>`, ...parts] : parts;
}

/** The daylight estimate's time step nearest now. */
function daylightNowIndex(d) {
    return d.times.reduce((best, t, k) =>
        Math.abs(t - d.now) < Math.abs(d.times[best] - d.now) ? k : best, 0);
}

/**
 * A room's light field (image + iso-lux lines, clipped to the room) for one
 * sky, or null when the room has no outside window to draw it from.
 */
function roomLightFieldParts(room, lvl, sky, avgLux) {
    const geo = roomDaylightGeometry(room, lvl);
    if (!geo || !geo.windows.length) return null;
    const f = roomDaylightField(geo, sky, avgLux, _state.plan.north_offset_deg || 0);
    const uid = room.id.replace(/[^a-z0-9]/gi, '_');
    const parts = [`<image href="${daylightFieldImage(f)}"
                        x="${f.x0}" y="${-(f.y0 + f.ny * f.h)}"
                        width="${f.nx * f.h}" height="${f.ny * f.h}"
                        clip-path="url(#dl_${uid})" preserveAspectRatio="none"
                        pointer-events="none"/>`];
    for (const lx of DL_ISO_LUX) {
        if (lx <= f.min || lx >= f.max) continue;
        const path = fieldContourPath(f, Math.log10(lx));
        if (path) parts.push(`<path class="fp-daylight-iso" d="${path}" clip-path="url(#dl_${uid})"
                                    pointer-events="none"/>`);
    }
    return { f, parts, defs: [`<clipPath id="dl_${uid}"><path d="${polygonToPath(room.polygon)}"/></clipPath>`] };
}

// daylight across a room — docs/daylight.md §8
//
// Illuminance on the working plane, cell by cell:
//   E(P) = E_sun(P) + E_sky(P) + E_irc
// E_sun: the beam through the glazing, where the ray from P toward the sun
//   leaves through the window aperture unblocked by other walls.
// E_sky: the window, cut into patches, each seen from P with the sky's
//   luminance in that direction (CIE clear sky, whose cos²γ term is Rayleigh
//   scattering, blended to CIE overcast by cloud) × cosθ_P cosθ_W dA / r².
// E_irc: the inter-reflected light, uniform — split-flux on the server's
//   average: E_avg = Φ / (A(1−ρ²)) and IRC = Φρ / (A(1−ρ)) give ρ(1+ρ)·E_avg.

const DL_WORK_PLANE_M = 0.85;          // desk height, where a lux sensor sits
const DL_SILL_M = 0.9;                 // window sill when the plan has none
const DL_OBSTRUCTION_DEG = 20;         // 90° − the server's 70° sky angle
const DL_OBSTRUCTION_REFLECTANCE = 0.2;
const DL_SURFACE_REFLECTANCE = 0.5;    // as the server's SURFACE_REFLECTANCE
const DL_GLAZING_T = { single: 0.85, double: 0.75, triple: 0.65 };
const DL_PATCHES_U = 5, DL_PATCHES_V = 4;
const DL_WAVELENGTHS_UM = [0.610, 0.550, 0.465];   // R, G, B
const DL_AEROSOL_WHITE = 0.45;         // share of skylight not from Rayleigh
const DL_ISO_LUX = [100, 300, 1000, 3000, 10000];
let _daylightCache = new Map();        // room.id → { key, geo }

/** Rayleigh optical depth of the whole atmosphere at λ (µm), ∝ λ⁻⁴ (Hansen & Travis). */
function rayleighDepth(um) {
    const l2 = 1 / (um * um);
    return 0.008569 * l2 * l2 * (1 + 0.0113 * l2 + 0.00013 * l2 * l2);
}

/** Relative optical air mass at a solar elevation (Kasten & Young 1989). */
function airMass(elDeg) {
    const e = Math.max(0, elDeg);
    return 1 / (Math.sin(e * Math.PI / 180) + 0.50572 * Math.pow(e + 6.07995, -1.6364));
}

/**
 * RGB tints, max channel 1: the beam after Rayleigh extinction exp(−τ_λ m)
 * (warmer as the sun drops) and the skylight it scattered, 1 − exp(−τ_λ m)
 * (blue). Cloud scatters all colours alike, so it greys both.
 */
function daylightTints(elDeg, cloud) {
    const m = airMass(elDeg), c = Math.max(0, Math.min(1, cloud || 0));
    const norm = v => { const mx = Math.max(...v); return v.map(x => x / mx); };
    const grey = (v, k) => v.map(x => x + (1 - x) * k);
    const tau = DL_WAVELENGTHS_UM.map(rayleighDepth);
    const sun = norm(tau.map(t => Math.exp(-t * m)));
    const sky = grey(norm(tau.map(t => 1 - Math.exp(-t * m))), DL_AEROSOL_WHITE);
    return { sun: grey(sun, c), sky: grey(sky, c) };
}

/**
 * Relative sky luminance at altitude `alt` (rad), `cosG` = cos of the angle
 * to the sun. CIE clear sky: the 0.45·cos²γ term is the Rayleigh phase
 * function, 10·e^(−3γ) the aerosol glow round the sun, and the gradation
 * 1 − e^(−0.32/sin α) brightens toward the horizon. CIE overcast is brighter
 * at the zenith, (1 + 2 sin α)/3.
 */
function skyLuminanceClear(alt, cosG) {
    const g = Math.acos(Math.max(-1, Math.min(1, cosG)));
    return (0.91 + 10 * Math.exp(-3 * g) + 0.45 * cosG * cosG)
         * (1 - Math.exp(-0.32 / Math.max(0.01, Math.sin(alt))));
}
function skyLuminanceOvercast(alt) { return (1 + 2 * Math.sin(alt)) / 3; }

/**
 * The sky at one time, as a luminance function whose horizontal illuminance
 * over the whole hemisphere is `diffuse` lux. `sunPlanAz` is in plan degrees.
 */
function daylightSky(sky, northOffsetDeg) {
    const el = sky.elevation * Math.PI / 180;
    const sunAz = ((sky.azimuth + northOffsetDeg) % 360 + 360) % 360 * Math.PI / 180;
    const c = Math.max(0, Math.min(1, sky.cloud ?? 0));
    const cosG = (alt, az) => Math.sin(alt) * Math.sin(el) + Math.cos(alt) * Math.cos(el) * Math.cos(az - sunAz);
    // Horizontal illuminance of each model: ∫∫ L sinα cosα dα dφ.
    let nClear = 0, nOver = 0;
    const NA = 18, NP = 36, dA = (Math.PI / 2) / NA, dP = (2 * Math.PI) / NP;
    for (let a = 0; a < NA; a++) {
        const alt = (a + 0.5) * dA, w = Math.sin(alt) * Math.cos(alt) * dA * dP;
        nOver += skyLuminanceOvercast(alt) * w * NP;
        for (let p = 0; p < NP; p++) nClear += skyLuminanceClear(alt, cosG(alt, (p + 0.5) * dP)) * w;
    }
    const diffuse = Math.max(0, sky.diffuse || 0);
    return {
        el, sunAz, beamN: Math.max(0, sky.beam_n || 0), diffuse,
        luminance: (alt, az) => diffuse * ((1 - c) * skyLuminanceClear(alt, cosG(alt, az)) / nClear
                                          + c * skyLuminanceOvercast(alt) / nOver),
        obstruction: DL_OBSTRUCTION_REFLECTANCE * diffuse / Math.PI,
        tints: daylightTints(sky.elevation, c),
    };
}

/** Does P→Q cross any wall other than `hostId`? Walls cast the shadows. */
function daylightBlocked(px, py, qx, qy, walls, hostId) {
    const rx = qx - px, ry = qy - py;
    for (const w of walls) {
        if (w.id === hostId) continue;
        const sx = w.x2 - w.x1, sy = w.y2 - w.y1;
        const den = rx * sy - ry * sx;
        if (Math.abs(den) < 1e-12) continue;
        const ax = w.x1 - px, ay = w.y1 - py;
        const t = (ax * sy - ay * sx) / den, u = (ax * ry - ay * rx) / den;
        if (t > 1e-6 && t < 1 - 1e-6 && u > 1e-6 && u < 1 - 1e-6) return true;
    }
    return false;
}

/**
 * Everything about a room's daylight that does not change with the time:
 * its outside windows, the grid, and for every cell the window patches it
 * can see (view factor g, altitude and plan bearing of the ray). Cached on
 * the geometry, so the time slider only re-weights.
 */
function roomDaylightGeometry(room, lvl) {
    if (!room.polygon || room.polygon.length < 3) return null;
    const allWalls = lvl.walls || [];
    const others = (lvl.rooms || []).filter(r => r.id !== room.id && r.polygon?.length >= 3);
    const centroid = polygonCentroid(room.polygon);
    const bops = (lvl.openings || []).filter(o => o.kind === 'window' && (o.room_id
        ? o.room_id === room.id
        : openingsOnRoomBoundary(room, { ...lvl, openings: [o] }).length > 0));
    const key = JSON.stringify([room.polygon, others.map(r => r.polygon),
        allWalls.map(w => [w.id, w.x1, w.y1, w.x2, w.y2, w.type]),
        bops.map(o => [o.id, o.wall_id, o.offset_m, o.width_m, o.height_m, o.sill_height_m, o.glazing])]);
    const hit = _daylightCache.get(room.id);
    if (hit && hit.key === key) return hit.geo;   // geo.byTime keeps each time step's field

    const windows = [];
    for (const o of bops) {
        const wall = allWalls.find(w => w.id === o.wall_id);
        if (!wall || wall.type === 'internal' || wall.type === 'party') continue;
        const len = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1);
        if (len < 1e-6 || !(o.width_m > 0)) continue;
        const ux = (wall.x2 - wall.x1) / len, uy = (wall.y2 - wall.y1) / len;
        const ax = wall.x1 + ux * (o.offset_m || 0), ay = wall.y1 + uy * (o.offset_m || 0);
        const mx = ax + ux * o.width_m / 2, my = ay + uy * o.width_m / 2;
        let nx = -uy, ny = ux;                                  // outward
        if ((centroid.x - mx) * nx + (centroid.y - my) * ny > 0) { nx = -nx; ny = -ny; }
        // Only glass with the outdoors behind it lets daylight in.
        const probe = { x: mx + nx * 0.4, y: my + ny * 0.4 };
        if (others.some(r => pointInPolygon(probe, r.polygon))) continue;
        const sill = Number.isFinite(o.sill_height_m) ? o.sill_height_m : DL_SILL_M;
        windows.push({ id: o.id, hostId: wall.id, ax, ay, ux, uy, nx, ny, width: o.width_m,
                       sill, head: sill + (o.height_m || 1.2),
                       T: DL_GLAZING_T[o.glazing] ?? DL_GLAZING_T.double });
    }

    const xs = room.polygon.map(p => p[0]), ys = room.polygon.map(p => p[1]);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const h = Math.max(0.08, Math.min(0.25, Math.max(maxX - minX, maxY - minY) / 60));
    const nxC = Math.max(2, Math.ceil((maxX - minX) / h) + 2);
    const nyC = Math.max(2, Math.ceil((maxY - minY) / h) + 2);
    const x0 = minX - h, y0 = minY - h;
    // Walls near enough to shade this room.
    const pad = 0.5;
    const walls = allWalls.filter(w => Math.max(w.x1, w.x2) >= minX - pad && Math.min(w.x1, w.x2) <= maxX + pad
                                     && Math.max(w.y1, w.y2) >= minY - pad && Math.min(w.y1, w.y2) <= maxY + pad);

    const inside = new Uint8Array(nxC * nyC);
    const start = new Int32Array(nxC * nyC + 1);
    const samples = [];                                       // g, alt, planAz, T
    for (let j = 0; j < nyC; j++) {
        for (let i = 0; i < nxC; i++) {
            const k = j * nxC + i;
            start[k] = samples.length / 4;
            const px = x0 + (i + 0.5) * h, py = y0 + (j + 0.5) * h;
            if (!pointInPolygon({ x: px, y: py }, room.polygon)) continue;
            inside[k] = 1;
            for (const w of windows) {
                const dA = (w.width / DL_PATCHES_U) * ((w.head - w.sill) / DL_PATCHES_V);
                for (let a = 0; a < DL_PATCHES_U; a++) {
                    const s = (a + 0.5) / DL_PATCHES_U * w.width;
                    const qx = w.ax + w.ux * s, qy = w.ay + w.uy * s;
                    const dx = qx - px, dy = qy - py;
                    const perp = dx * w.nx + dy * w.ny;
                    if (perp <= 1e-3) continue;
                    if (daylightBlocked(px, py, qx, qy, walls, w.hostId)) continue;
                    const horiz = Math.hypot(dx, dy);
                    const planAz = Math.atan2(dx, dy);
                    for (let b = 0; b < DL_PATCHES_V; b++) {
                        const dz = w.sill + (b + 0.5) / DL_PATCHES_V * (w.head - w.sill) - DL_WORK_PLANE_M;
                        if (dz <= 0) continue;                // below the desk: not seen from above
                        const r2 = horiz * horiz + dz * dz, r = Math.sqrt(r2);
                        samples.push((dz / r) * (perp / r) * dA / r2, Math.atan2(dz, horiz), planAz, w.T);
                    }
                }
            }
        }
    }
    start[nxC * nyC] = samples.length / 4;
    const geo = { windows, walls, nx: nxC, ny: nyC, x0, y0, h, inside, start,
                  samples: Float32Array.from(samples), byTime: new Map() };
    _daylightCache.set(room.id, { key, geo });
    return geo;
}

/**
 * The room's illuminance field at one time step, as log10(lux) in `data`
 * (the shape fieldContourPath expects), with the sun / sky / reflected
 * shares of each cell for colouring. `avgLux` is the server's room average.
 * Cached on `geo` per time step.
 */
function roomDaylightField(geo, sky, avgLux, northOffsetDeg) {
    const ck = JSON.stringify([sky, avgLux, northOffsetDeg]);
    if (geo.byTime.has(ck)) return geo.byTime.get(ck);

    const S = daylightSky(sky, northOffsetDeg);
    const { nx, ny, x0, y0, h, inside, start, samples, windows, walls } = geo;
    const n = nx * ny;
    const sun = new Float32Array(n), skyL = new Float32Array(n), data = new Float32Array(n);
    const rho = DL_SURFACE_REFLECTANCE;
    const irc = Math.max(1, avgLux * rho * (1 + rho));
    const shx = Math.sin(S.sunAz), shy = Math.cos(S.sunAz);  // toward the sun, plan
    const tanEl = Math.tan(S.el), sinEl = Math.sin(S.el), cosEl = Math.cos(S.el);
    const obstructionAlt = DL_OBSTRUCTION_DEG * Math.PI / 180;
    let min = Infinity, max = 0, sunSum = 0, skySum = 0;
    for (let k = 0; k < n; k++) {
        if (!inside[k]) continue;
        const px = x0 + (k % nx + 0.5) * h, py = y0 + (Math.floor(k / nx) + 0.5) * h;
        let es = 0;
        if (S.beamN > 0) {
            for (const w of windows) {
                const facing = shx * w.nx + shy * w.ny;
                if (facing <= 0.02) continue;
                const dist = (w.ax - px) * w.nx + (w.ay - py) * w.ny;
                if (dist <= 0) continue;
                const t = dist / facing;
                const cx = px + shx * t, cy = py + shy * t;
                const u = (cx - w.ax) * w.ux + (cy - w.ay) * w.uy;
                const z = DL_WORK_PLANE_M + t * tanEl;
                if (u < 0 || u > w.width || z < w.sill || z > w.head) continue;
                if (daylightBlocked(px, py, cx, cy, walls, w.hostId)) continue;
                // Glass reflects more at grazing angles (ASHRAE incidence modifier).
                const cosI = cosEl * facing;
                const iam = Math.max(0, 1 - 0.1 * (1 / Math.max(cosI, 0.05) - 1));
                es += S.beamN * sinEl * w.T * iam;
            }
        }
        let ek = 0;
        for (let s = start[k]; s < start[k + 1]; s++) {
            const o = s * 4, alt = samples[o + 1];
            const L = alt < obstructionAlt ? S.obstruction : S.luminance(alt, samples[o + 2]);
            ek += samples[o] * samples[o + 3] * L;
        }
        sun[k] = es; skyL[k] = ek;
        const e = es + ek + irc;
        data[k] = Math.log10(e);
        min = Math.min(min, e); max = Math.max(max, e);
        sunSum += es; skySum += ek;
    }
    // Outside the room, carry the nearest inside value so iso-lines don't
    // trace the room's own edge.
    let frontier = [];
    for (let k = 0; k < n; k++) if (inside[k]) frontier.push(k);
    const done = Uint8Array.from(inside);
    while (frontier.length) {
        const next = [];
        for (const k of frontier) {
            const i = k % nx, j = Math.floor(k / nx);
            for (const [di, dj] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
                const ii = i + di, jj = j + dj;
                if (ii < 0 || jj < 0 || ii >= nx || jj >= ny) continue;
                const kk = jj * nx + ii;
                if (done[kk]) continue;
                done[kk] = 1; data[kk] = data[k]; next.push(kk);
            }
        }
        frontier = next;
    }
    // Reflected light takes the colour of what came in, softened by the walls.
    const tot = sunSum + skySum || 1;
    const ircTint = [0, 1, 2].map(c => 0.5 + 0.5 * (S.tints.sun[c] * sunSum + S.tints.sky[c] * skySum) / tot);
    const f = { nx, ny, x0, y0, h, data, inside, sun, sky: skyL, irc, ircTint, tints: S.tints,
                min: Number.isFinite(min) ? min : irc, max, urls: {} };
    geo.byTime.set(ck, f);
    return f;
}

/**
 * Rasterise a daylight field. Brightness is log lux, from shade (navy) to
 * lit; hue is the mix of beam (warm, Rayleigh-reddened) and sky (blue).
 */
function daylightFieldImage(f) {
    if (f.urls.img) return f.urls.img;
    const { nx, ny, data, inside, sun, sky, irc, ircTint, tints } = f;
    const cnv = document.createElement('canvas');
    cnv.width = nx; cnv.height = ny;
    const ctx = cnv.getContext('2d');
    const img = ctx.createImageData(nx, ny);
    const shade = [30, 41, 90];
    for (let j = 0; j < ny; j++) {
        for (let i = 0; i < nx; i++) {
            const k = j * nx + i;
            if (!inside[k]) continue;
            const e = sun[k] + sky[k] + irc;
            const ws = sun[k] / e, wk = sky[k] / e, wr = irc / e;
            const t = Math.max(0, Math.min(1, (data[k] - 1) / 3.7));   // 10 lx → 0, 50 klx → 1
            const px = (((ny - 1 - j) * nx) + i) * 4;
            for (let c = 0; c < 3; c++) {
                const tint = ws * tints.sun[c] + wk * tints.sky[c] + wr * ircTint[c];
                const lit = 255 * (0.2 + 0.8 * Math.max(0, 1 - (1 - tint) * 1.5));
                img.data[px + c] = Math.round(shade[c] + (lit - shade[c]) * t);
            }
            img.data[px + 3] = Math.round((0.42 + 0.2 * ws) * 255);
        }
    }
    ctx.putImageData(img, 0, 0);
    const up = document.createElement('canvas');
    up.width = nx * 6; up.height = ny * 6;
    const uctx = up.getContext('2d');
    uctx.imageSmoothingEnabled = true;
    uctx.imageSmoothingQuality = 'high';
    uctx.drawImage(cnv, 0, 0, up.width, up.height);
    f.urls.img = up.toDataURL('image/png');
    return f.urls.img;
}

// save

async function save() {
    const btn = document.getElementById('fpSave');
    const status = document.getElementById('fpSaveStatus');
    btn.disabled = true;
    status.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>Saving plan…`;
    try {
        // Strip drawBuffer / view-state — not part of the plan
        const payload = {
            version: FP_VERSION,
            north_offset_deg: _state.plan.north_offset_deg,
            scale_pixels_per_metre: _state.plan.scale_pixels_per_metre,
            // Plan-level circuit definitions — MUST be sent so the backend
            // can persist them and use plan-native projection mode.
            ...(_state.plan.map ? { map: _state.plan.map } : {}),
            circuits: (_state.plan.circuits || []).map(c => ({
                id: c.id,
                name: c.name,
                ...(c.receiver_ieee   ? { receiver_ieee:     c.receiver_ieee }   : {}),
                ...(c.receiver_command? { receiver_command:  c.receiver_command } : {}),
                ...(c.receiver_endpoint != null ? { receiver_endpoint: c.receiver_endpoint } : {}),
            })),
            levels: _state.plan.levels.map(l => {
                const out = {
                    id: l.id, name: l.name, index: l.index,
                    ceiling_height_m: l.ceiling_height_m,
                    floor_above_ground_m: l.floor_above_ground_m,
                    walls: l.walls, openings: l.openings, rooms: l.rooms,
                    radiators: l.radiators, sensors: l.sensors, contacts: l.contacts,
                    // Every placed device, whatever this view shows.
                    devices: l.devices || [],
                };
                if (l.background?.present) {
                    // Strip view-only fields (cache-buster) before sending
                    const { _cb, ...bg } = l.background;
                    out.background = bg;
                }
                return out;
            }),
        };
        const r = await fetch('/api/floor-plan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        }).then(r => r.json());
        if (!r.success) throw new Error(r.error || 'Save failed');
        status.innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>Saved.</span>`;
        if (typeof _onSaveCallback === 'function') {
            try { await _onSaveCallback(r); } catch (e) { log.error(e); }
        }
        const modalEl = _root.closest('#floorPlanModal');
        if (modalEl) {
            setTimeout(() => bootstrap.Modal.getOrCreateInstance(modalEl).hide(), 600);
        } else if (r.plan) {
            // The server's copy: it drops a devices[] entry heating now holds.
            const keep = _state.currentLevelId;
            _state.plan = { circuits: [], ...r.plan };
            for (const l of _state.plan.levels) if (!Array.isArray(l.devices)) l.devices = [];
            _state.currentLevelId = _state.plan.levels.some(l => l.id === keep)
                ? keep : _state.plan.levels[0]?.id;
            _state.selection = null;
            // The estimate reads the saved plan, which has just changed.
            if (_state.showDaylight || _state.showSun) { await loadDaylight(); syncDaylightControls(); }
            // The heatmap is worked out from the saved plan too.
            if (_state.showCoverage) loadCoverage();
            renderScene(); renderProps(); renderPalette();
        }
    } catch (e) {
        status.innerHTML = `<span class="text-danger"><i class="fas fa-times-circle me-1"></i>${escapeHtml(e.message)}</span>`;
    } finally {
        btn.disabled = false;
    }
}

/**
 * Switch the heating controller back to manual configuration. Posts the
 * mode change directly — the backend strips floor_plan_ref from rooms so
 * the manual UI is fully editable. The saved plan stays on disk as a
 * backup; the user can switch back later and the plan will be re-applied.
 */
async function switchToManual() {
    const confirmed = await window.zbmConfirm({
        title: 'Switch to manual configuration',
        message: 'Switch the heating controller to manual configuration?',
        detail: 'Your floor plan stays saved as a backup. The rooms will become '
            + 'freely editable in the manual UI. You can switch back later — '
            + 'the plan will be re-applied to the rooms.',
        confirmText: 'Switch'
    });
    if (!confirmed) return;

    const status = document.getElementById('fpSaveStatus');
    status.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>Switching…`;
    try {
        const r = await fetch('/api/heating/controller/config-mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: 'manual' }),
        }).then(r => r.json());
        if (!r.success) throw new Error(r.error || 'Switch failed');

        status.innerHTML = `<span class="text-success">Switched to manual.</span>`;
        // Trigger the editor's onSave callback so the parent page refreshes.
        if (typeof _onSaveCallback === 'function') {
            try { await _onSaveCallback(r); } catch (e) { log.error(e); }
        }
        setTimeout(() => {
            const modalEl = document.getElementById('floorPlanModal');
            if (modalEl) bootstrap.Modal.getOrCreateInstance(modalEl).hide();
        }, 600);
    } catch (e) {
        status.innerHTML = `<span class="text-danger"><i class="fas fa-times-circle me-1"></i>${escapeHtml(e.message)}</span>`;
    }
}

// background image

const PDF_JS_URL = '/static/js/vendor/pdf.min.js';
const PDF_WORKER_URL = '/static/js/vendor/pdf.worker.min.js';

async function ensurePdfJs() {
    if (window.pdfjsLib) return window.pdfjsLib;
    await new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = PDF_JS_URL;
        s.onload = resolve;
        s.onerror = () => reject(new Error('pdf.js not found at ' + PDF_JS_URL));
        document.head.appendChild(s);
    });
    if (!window.pdfjsLib) throw new Error('pdf.js loaded but window.pdfjsLib missing');
    window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDF_WORKER_URL;
    return window.pdfjsLib;
}

async function pdfFirstPageToPngBlob(file, scale = 2.0) {
    const lib = await ensurePdfJs();
    const buf = await file.arrayBuffer();
    const pdf = await lib.getDocument({ data: buf }).promise;
    const page = await pdf.getPage(1);
    const viewport = page.getViewport({ scale });
    const canvas = document.createElement('canvas');
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
    return await new Promise(res => canvas.toBlob(res, 'image/png'));
}

async function onImageFileChosen(e) {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;

    const lvl = currentLevel();
    const status = document.getElementById('fpSaveStatus');
    status.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>Importing image…`;

    try {
        let uploadBlob = file;
        let contentType = file.type || '';
        if (file.type === 'application/pdf' || /\.pdf$/i.test(file.name)) {
            uploadBlob = await pdfFirstPageToPngBlob(file, 2.0);
            contentType = 'image/png';
        } else if (!['image/png', 'image/jpeg'].includes(file.type)) {
            throw new Error('Unsupported file. Use PNG, JPEG, or PDF.');
        }

        // Read dimensions client-side so we can compute pixels-per-metre
        const dims = await readImageDimensions(uploadBlob);

        // POST to server
        const fd = new FormData();
        fd.append('file', uploadBlob, contentType === 'image/png' ? 'plan.png' : 'plan.jpg');
        const r = await fetch(`/api/floor-plan/image/${encodeURIComponent(lvl.id)}`, {
            method: 'POST', body: fd,
        }).then(r => r.json());
        if (!r.success) throw new Error(r.error || 'upload failed');

        // A file carries no scale, so the placement has to be inferred. Note
        // the OLD px/m is deliberately not reused: it belongs to the old
        // image's pixel dimensions, and a file of a different resolution at
        // that px/m lands tiny (or huge) and nowhere near the walls.
        const prev = (lvl.background?.present
                      && lvl.background.pixels_per_metre > 0
                      && lvl.background.image_width_px > 0)
            ? { ...lvl.background } : null;
        lvl.background = {
            present: true,
            pixels_per_metre: 50,       // placeholder, replaced just below
            image_width_px: dims.width,
            image_height_px: dims.height,
            origin_x_m: 0,
            origin_y_m: 0,
            rotation_deg: prev?.rotation_deg || 0,
            opacity: prev?.opacity ?? 0.5,
            content_type: contentType,
            _cb: Date.now(),    // cache-buster for SVG <image>
        };

        let note = 'Use the <strong>Calibrate</strong> tool to set the scale.';
        if (prev) {
            // Replacing an image: same physical size and centre as the one it
            // replaces, so a re-export of the same plan stays on the tracing.
            bgResizeAnchored(lvl.background, prev.image_width_px / prev.pixels_per_metre,
                             0.5, 0.5, bgPoint(prev, 0.5, 0.5));
            note = 'Kept the size and position of the image it replaced.';
        } else if (fitBackgroundToDrawing({ silent: true, level: lvl })) {
            note = 'Fitted to the walls already drawn — line it up with '
                 + '<strong>Adjust image</strong>, then <strong>Calibrate</strong>.';
        }

        syncBackgroundControls();
        status.innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>Image imported. ${note}</span>`;
        renderScene();
        zoomFit();
    } catch (err) {
        status.innerHTML = `<span class="text-danger"><i class="fas fa-times-circle me-1"></i>${escapeHtml(err.message)}</span>`;
    }
}

function readImageDimensions(blob) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        const url = URL.createObjectURL(blob);
        img.onload = () => {
            URL.revokeObjectURL(url);
            resolve({ width: img.naturalWidth, height: img.naturalHeight });
        };
        img.onerror = () => {
            URL.revokeObjectURL(url);
            reject(new Error('could not read image dimensions'));
        };
        img.src = url;
    });
}

async function removeBackgroundImage() {
    const lvl = currentLevel();
    if (!lvl.background?.present) return;
    if (!await window.zbmConfirm({
        title: 'Remove background image',
        message: 'Remove background image for this level?',
        confirmText: 'Remove',
        variant: 'danger'
    })) return;
    try {
        await fetch(`/api/floor-plan/image/${encodeURIComponent(lvl.id)}`, { method: 'DELETE' });
    } catch { /* swallow */ }
    delete lvl.background;
    document.getElementById('fpRemoveImage').disabled = true;
    renderScene();
}

/**
 * Scale everything drawn on a level by `s` about `about`, so a plan traced
 * over a background image keeps lining up when the image is recalibrated.
 *
 * Only what was drawn moves. Typed physical sizes — a radiator's length, a
 * window's height, the ceiling — are the user's measurements, not the
 * tracing's, so they stay as they are.
 */
function scaleLevelGeometry(lvl, s, about, map) {
    const pt = p => ({ x: about.x + (p.x - about.x) * s, y: about.y + (p.y - about.y) * s });
    for (const w of lvl.walls || []) {
        const a = pt({ x: w.x1, y: w.y1 }), b = pt({ x: w.x2, y: w.y2 });
        w.x1 = round3(a.x); w.y1 = round3(a.y); w.x2 = round3(b.x); w.y2 = round3(b.y);
    }
    for (const r of lvl.rooms || []) {
        r.polygon = (r.polygon || []).map(([x, y]) => {
            const q = pt({ x, y });
            return [round3(q.x), round3(q.y)];
        });
    }
    for (const o of lvl.openings || []) {
        o.offset_m = round3((o.offset_m || 0) * s);
        o.width_m = round3((o.width_m || 0) * s);
    }
    for (const r of lvl.radiators || []) {
        if (r.x != null && r.y != null) { const q = pt(r); r.x = round3(q.x); r.y = round3(q.y); }
        if (r.offset_m != null) r.offset_m = round3(r.offset_m * s);
    }
    for (const list of [lvl.sensors || [], lvl.devices || []]) {
        for (const d of list) {
            if (d.x == null || d.y == null) continue;
            const q = pt(d);
            d.x = round3(q.x); d.y = round3(q.y);
        }
    }
    // The map pin is a point in the same plan coordinates. With one level
    // those are the coordinates being rescaled; with more, the other levels
    // keep theirs, so moving the shared pin would be a guess.
    if (map && map.anchor_x_m != null) {
        const q = pt({ x: map.anchor_x_m, y: map.anchor_y_m });
        map.anchor_x_m = round3(q.x); map.anchor_y_m = round3(q.y);
    }
}

function levelHasDrawing(lvl) {
    return !!((lvl.walls || []).length || (lvl.rooms || []).length
              || (lvl.radiators || []).length || (lvl.sensors || []).length
              || (lvl.devices || []).length);
}

async function promptCalibrationDistance(p1, p2, drawnDist) {
    const lvl = currentLevel();
    if (!lvl.background?.present) {
        toast('warn', 'No image', 'Import a background image before calibrating.');
        return;
    }
    if (drawnDist < 1e-6) {
        toast('warn', 'Too short', 'Pick two distinct points.');
        return;
    }
    const ans = await window.zbmPrompt({
        title: 'Calibrate scale',
        message: `You drew a line measuring ${drawnDist.toFixed(2)} m at the current scale.`,
        label: 'What is its real-world length (metres)?',
        value: drawnDist.toFixed(2),
        type: 'number',
        confirmText: 'Calibrate'
    });
    if (ans == null) return;
    const realM = parseFloat(ans);
    if (!Number.isFinite(realM) || realM <= 0) {
        toast('warn', 'Invalid', 'Enter a positive number of metres.');
        return;
    }

    // Re-scale the image so that drawnDist (currently in current model metres)
    // equals realM. New ppm = old_ppm * (drawnDist / realM).
    const oldPpm = lvl.background.pixels_per_metre;
    if (!Number.isFinite(oldPpm) || oldPpm <= 0) {
        // Stale/invalid state — recover with a sane default rather than
        // propagating NaN/0 through the calibration math.
        lvl.background.pixels_per_metre = 50.0;
        toast('warn', 'Recovered scale',
            'Image had no valid scale; reset to 50 px/m. Calibrating now…');
        renderScene();
        // Re-enter calibration with the recovered ppm
        promptCalibrationDistance(p1, p2, drawnDist);
        return;
    }
    const newPpm = oldPpm * (drawnDist / realM);
    if (!Number.isFinite(newPpm) || newPpm <= 0) {
        toast('warn', 'Invalid', 'Calibration produced an invalid scale.');
        return;
    }

    // The image is about to change size by this much; the tracing on top of
    // it has to change by the same amount to stay on the walls it traced.
    const factor = realM / drawnDist;
    let scaleDrawing = false;
    if (levelHasDrawing(lvl) && Math.abs(factor - 1) > 0.001) {
        const bigger = factor > 1;
        scaleDrawing = await window.zbmConfirm({
            title: 'Scale the drawing too?',
            message: `The image is about to get ${bigger ? 'bigger' : 'smaller'} by `
                + `${(bigger ? factor : 1 / factor).toFixed(2)}×.`,
            detail: 'Walls, rooms, windows, radiators, sensors and placed devices on this '
                + 'level can scale with it, so a plan traced over the image stays lined up '
                + "and its measurements become right. Choose “Only the image” if the drawing's "
                + 'measurements are already correct and it is the image that is wrong. '
                + 'Nothing is saved until you press Save plan.',
            confirmText: 'Scale the drawing',
            cancelText: 'Only the image',
        });
    }

    // Re-anchor: keep p1 at the same MODEL coordinate after rescale.
    // Image origin in model space shifts so that the pixel under p1 stays at p1.
    const bg = lvl.background;
    const wOldM = bg.image_width_px / oldPpm;
    const hOldM = bg.image_height_px / oldPpm;
    // Pixel position of p1 within the current image (top-left of image is at
    // (origin_x, origin_y + h) in model coords, with +y up):
    const u = (p1.x - bg.origin_x_m) / wOldM;       // 0..1 across width
    const v = (bg.origin_y_m + hOldM - p1.y) / hOldM; // 0..1 down from top
    bg.pixels_per_metre = newPpm;
    const wNewM = bg.image_width_px / newPpm;
    const hNewM = bg.image_height_px / newPpm;
    bg.origin_x_m = p1.x - u * wNewM;
    bg.origin_y_m = p1.y - (hNewM - v * hNewM);

    if (scaleDrawing) {
        scaleLevelGeometry(lvl, factor, p1,
                           _state.plan.levels.length === 1 ? _state.plan.map : null);
    }

    toast('success', 'Calibrated',
        `${realM.toFixed(2)} m = ${drawnDist.toFixed(3)} drawn — scale ${newPpm.toFixed(1)} px/m.`
        + (scaleDrawing ? ' The drawing scaled with it.' : ''));
    renderScene();
    renderOverlay();
    renderProps();
    // A big change can leave the plan off-screen; bring it back into view.
    zoomFit();
}


function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }
// The shared toasts take (message, { title }) and know 'warning', not 'warn'.
// Both parts are rendered as HTML, so anything user-named must be escaped.
function toast(level, title, body) {
    const kind = { warn: 'warning', danger: 'error' }[level] || level;
    const fn = window.toast?.[kind] || window.toast?.info;
    if (fn) fn(body || title, body ? { title } : {});
    else log.log(`[${level}] ${title}: ${body}`);
}