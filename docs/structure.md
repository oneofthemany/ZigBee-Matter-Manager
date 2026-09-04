```
├── __init__.py
│
├── core.py
├── deploy.sh
├── device.py
├── main.py
├── mqtt.py
├── requirements.txt
├── zigbee.db
├── zigbee.db-shm
├── zigbee.db-wal
│
├── config
│    ├── config.yaml
│    └── zones.yaml
│
├── data
│    ├── device_settings.json
│    ├── device_state_cache.json
│    ├── groups.json
│    └── names.json
├── docs
│    ├── announcement.md
│    ├── aqare_cluster_guide.md
│    ├── automations.md
│    ├── debugging.md
│    ├── swarm-intelligence.md
│    ├── mqtt-explorer.md
│    └── onboarding.md
│
├── ha_utils
│    └── zombie_killer.py
│   
├── handlers
│    ├── __init__.py
│    ├── aqara.py
│    ├── base.py
│    ├── basic.py
│    ├── blinds.py
│    ├── fast_path.py
│    ├── general.py
│    ├── hvac.py
│    ├── lighting.py
│    ├── lightlink.py
│    ├── power.py
│    ├── security.py
│    ├── sensors.py
│    ├── sonoff_quirk.py
│    ├── switches.py
│    ├── tuya.py
│    └── zigbee_debug.py
│
├── logs
│    ├── zigbee_debug.log
│    └── zigbee.log
│
├── modules
│    ├── __init__.py
│    ├── automation.py
│    ├── automation_api.py
│    ├── config_enhanced.py
│    ├── device_ban.py
│    ├── device_capabilities.py
│    ├── error_handler.py
│    ├── groups.py
│    ├── json_helpers.py
│    ├── mqtt_explorer.py
│    ├── mqtt_queue.py
│    ├── packets_stats.py
│    ├── reslience.py
│    ├── swarm
│    │   ├── __init__.py
│    │   ├── api.py
│    │   ├── capabilities.py
│    │   ├── compiler.py
│    │   ├── dedupe.py
│    │   ├── diagnostics.py
│    │   ├── doctor.py
│    │   ├── matcher.py
│    │   ├── network.py
│    │   ├── patterns
│    │   │   ├── climate.json
│    │   │   └── core.json
│    │   ├── resolver.py
│    │   ├── stigmergy.py
│    │   ├── suggestions.py
│    │   └── virtual.py
│    ├── touchlink.py
│    ├── zigbee_debug.py
│    ├── zone_device_config.py
│    ├── zones_api.py
│    └── zones.log
│
├── static
│    ├── index.html
│    ├── css
│    │   ├── debug.css
│    │   ├── groups.css
│    │   ├── mesh.css
│    │   ├── mqtt-explorer.css
│    │   └── styles.css
│    └── js
│        ├── actions.js
│        ├── automations-page.js
│        ├── device-modal.js
│        ├── devices.js
│        ├── groups.js
│        ├── logging.js
│        ├── main.js
│        ├── mesh.js
│        ├── packet-analysis.js
│        ├── state.js
│        ├── swarm-suggest.js
│        ├── system.js
│        ├── table-sort.js
│        ├── utils.js
│        ├── websocket.js
│        ├── zones.js
│        └── modal
│              ├── automation.js
│              ├── binding.js
│              ├── clusters.js
│              ├── config.js
│              ├── control.js
│              └── overview.js
└── utils
      └── zombie_killer.py
```

## Shared frontend layers

Two modules that every consumer is expected to go through.

### `static/js/chart-utils.js`

Every chart in the app. One place to register the light/dark themes so charts
follow the app's `data-theme` and re-theme live on `themechange`; auto-resize
against the container so charts fill Bootstrap cards; and a stable wrapper so
callers never hold a disposed instance after a theme swap — ECharts cannot
change theme in place, it must re-init. Requires the global `echarts`, vendored
in `index.html` before the modules.

### `static/js/table-utils.js`

The table analogue. One comparator implementation (natural string / number /
date / boolean) reused by both DOM click-sort and the data-level sorts (devices,
logs); delegated click-to-sort that works on any `<table class="tbl
tbl-sortable">` and survives re-renders, because the listener lives on
`document` rather than the table; a delegated text filter for `<input
class="tbl-filter" data-tbl-target="#id">`; and consistent sticky headers,
density and sort carets via `table.css`.

Markup contract:

```html
<table class="table tbl tbl-sortable">           <!-- opt into click-sort -->
  <thead><tr>
    <th data-sort-type="number">LQI</th>         <!-- number|date|boolean|string -->
    <th data-no-sort>Actions</th>                <!-- excluded from sorting -->
  <td data-sort-value="42">…badge…</td>          <!-- override the sorted value -->
```

Live tables that re-render their own rows (devices, packet log) keep their
data-level sort but import `compareValues()` so the logic stays unified.

### `/frames` is deliberately not on the dashboard's plumbing

`static/js/frames-page.js` bootstraps the standalone mobile front end without
reusing the dashboard's imports. `websocket.js` pulls in 13 modules, and
`actions.js` imports `device-modal.js`, which drags in `control.js` (79 KB) and
`automation.js` (88 KB) — importing either would put the entire admin dashboard
on a phone and defeat the point of a separate surface.

So the page provides only the three things `frames.js` actually needs: the
device cache from one `/api/devices` fetch, a minimal websocket listening for
`device_updated`, and `window.sendCommand` — which `frames.js` depends on as a
*contract*, not as `actions.js`.

## Frontend styling decisions

Rationale extracted from the stylesheets so they stay terse.

### Hive loader

For waits that are **structural rather than incidental**. Starting a sync
session has to fill the delay line before any speaker may read from it, and that
costs as long as the audio it buffers — the group's spread of startup latencies,
so seconds rather than milliseconds. A button that merely goes disabled for that
span reads as a dropped click, which is why the wait gets a face. It is built
from the same hexagon the frames grid uses, so it belongs to the hive rather
than arriving as a generic spinner.

The orbiting bees are children 2–4 (child 1 is the comb cell, so `nth-child`
counts from there). Differing periods keep the three from locking into a
rosette; that variety is carried by the periods rather than the radii, which are
boxed in at both ends. A bee is 15% wide, so a radius above 0.425 puts it
outside the loader and onto whatever sits next to it (the button label), while
below about 0.30 it clips the comb cell on the way past.

### EQ scope

An instrument, not decoration. It is read against a grid, so it commits to a
dark field in **both** themes rather than following the surface. A spectrum on a
light background loses the quiet detail near the floor — which is most of the
picture — and the grid lines end up fighting the plot instead of sitting under
it. Drawing happens in `eq-scope.js`; the CSS is only the housing.

### Floor-plan editor

The editor's SVG emits CSS classes only; every stroke and fill colour is
inherited from CSS variables, so a `[data-theme="dark"]` switch on `<html>`
repaints instantly without re-rendering the SVG.

The desktop editor is a fixed three-column layout: a 240 px tools/levels rail, a
flexible canvas, and a 300 px properties rail. On a phone those two rails alone
(540 px) are wider than the viewport, leaving the canvas zero room. Below 768 px
the rails become off-canvas drawers that slide *over* the canvas instead of
squeezing it, toggled by the header buttons in `floor-plan.js`
(`#fpToggleSidebarBtn` / `#fpTogglePropsBtn`), which add and remove
`.fp-sidebar-open` / `.fp-props-open` on `#floorPlanModal`.

Under `prefers-reduced-motion` the looping overlays stop. Each animates opacity
or transform from a visible resting state, so stopping them leaves every reading
legible — the colour and the badge carry the meaning, the motion was only
emphasis.

### Mobile one-pane-at-a-time patterns

Two tabs collapse to a single visible pane below the `lg` breakpoint:

- **Media.** The Players and Browse cards stack on a phone, so reaching the
  search box meant scrolling past every player card. A segmented switch shows
  one pane at a time. Scoped to `<lg`, so the desktop 7/5 split is never
  affected — `.zmm-pane-hidden` simply does nothing at `lg` and up, whatever JS
  puts on the element.
- **Docs.** Stacked, the 75vh doc list buried the article below the fold, so a
  tapped doc looked like nothing happened. Tapping now swaps to the article
  (`wiki.js` adds `.wiki-doc-open`) and the `d-md-none` "All documents" button
  swaps back.

The pairing dropdown is right-aligned because it would otherwise extend past the
left screen edge (the Pair button sits near the left). With
`data-bs-display="static"` on the toggle there is no Popper inline positioning,
so it is pinned to the full viewport width; `top: auto` keeps it at its static
position, just under the row.

### Device-table skeleton

The skeleton rows use **one cell per column, not a single `colspan=10` cell**.
With `table-layout: auto` the browser sizes columns from their contents, so a
merged cell left all ten widths to be derived from the header text alone, and
every column jumped the moment real rows arrived. The placeholder widths
approximate real content — IEEE and name are the wide ones.

Column widths are also seeded from the last render by an inline script placed
immediately after the table, so the skeleton is already the right shape. It must
run while the parser is still inside the document, i.e. before first paint. The
widths are a hint only: `table-layout` is auto, so real content still wins if a
device name needs more room. Written by `rememberColumnWidths()` in
`js/devices.js`.

### Design tokens and the deliberate duplication

`static/css/hive-tokens.css` is the canonical source of truth for the hive
palette. Load order matters: it loads **after** `dark-mode.css` and re-points the
semantic tokens (`--bg-*`, `--text-*`, `--border-*`) at the hive palette.
Components keep consuming the semantic names; only the values change. A
Bootstrap 5.3 bridge feeds those tokens into Bootstrap's own variable system, so
its components theme themselves without `!important` overrides —
`theme-toggle.js` keeps `data-bs-theme` in sync with `data-theme`.

Two things duplicate parts of this **by convention**, and both must be updated
together with it:

- `manager/dashboard.html` duplicates the raw palette block, because it must
  stay a single self-contained file — it is the disaster-recovery UI.
- The theme-resolution snippet is inlined in both `index.html` and
  `frames.html`. FOUC-critical code must not wait on a network fetch, or the
  page paints light and flips to dark. Keep the storage key and the light/dark
  default in sync with `js/theme-toggle.js`.

### `/frames` scroll behaviour

`hive-components.css` builds the **dashboard** as a non-scrolling app shell:
`html, body { height: 100% }` plus `body { display: flex; overflow: hidden }`,
so only `.tab-content` scrolls. Those rules are not scoped to the dashboard, and
`/frames` loads the same stylesheet — inheriting `overflow: hidden` made the
page physically unable to scroll. `body.frames-page` undoes it: this page is an
ordinary document, so with `height: auto` and visible overflow the body grows
past `html`'s 100% and the viewport scrolls.

The `?view=manager` link tells the server the switch is deliberate, so a user
whose landing page is "frames" is not redirected straight back. It is
deliberately **not** mobile-only, unlike the Frames button in the manager: a
landing=frames user who opens the app on a desktop is redirected here, and this
is their only way out. Hiding it would strand them.

`frames.html` is the PWA `start_url`, so it is *the* phone app — without
`presence.js` on it an installed Frames app reports no presence at all. It
self-bootstraps from localStorage prefs; the prefs UI lives in the manager's
Presence settings.

### On the honeycomb

Frame cells are rectangles, not hexagons — `--hexclip` is used for the icon chip
only. A grid of real hexagons looks the part but cramps labels, sliders and
toggles, and a dashboard you cannot operate is not worth the metaphor.

## Comment-preserving config writes

The obvious way to persist a setting is `safe_load`, mutate, dump. It is also
lossy in a way that does not announce itself: PyYAML's emitter has no concept of
comments, so a round-trip silently deletes every one of them. `config.yaml` is
roughly a third comments — the documentation for every knob in the hub — and
losing that to persist one boolean is not a trade worth making, least of all
when nobody notices until they next go looking for an explanation.

So `modules/config_yaml.py` edits the file **as text**: it finds a top-level
block, rewrites the values of the keys it was given, and leaves every other byte
— comments, ordering, blank lines, indentation style, and keys it has never
heard of — exactly as found.

Its scope is deliberately small: top-level blocks one level deep, which is the
shape of every setting the UI persists. It is **not** a YAML editor. Nested
maps, lists and anchors are out of range, and a caller needing those should read
and write its own file rather than growing this. Anything it cannot place is
appended as a new block rather than guessed at.

A write goes through a temporary file in the same directory and is renamed into
place, so an interrupted save cannot leave the hub with half a config.

Strings matching `_PLAIN_SAFE_RE` are emitted bare, as a hand-written file would
have them; everything else is quoted. That is conservative on purpose — a
needlessly quoted string is ugly, but an unquoted one that reparses as something
else is a bug.
