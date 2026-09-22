# Floor plan

There is **one plan for the whole home**, and it is not a heating feature. It
started as one — the geometry heating needed to work out heat loss — and the
section describing it grew inside `docs/heating.md` long after half the app had
started reading it. This file is that section, moved out and given the other
readers their due. Heating's *use* of the plan stays in
[docs/heating.md](heating.md) § Floor plan → heating.

| Piece | Where |
| --- | --- |
| Data model, geometry, projection | `modules/floor_plan.py` — pure module: no I/O, no FastAPI, no global state |
| The only way to read or write the plan | `modules/floor_plan_store.py` |
| HTTP surface | `routes/floor_plan_routes.py` |
| The editor | `static/js/floor-plan.js`, `static/css/floor-plan.css` |
| Tests | `tests/floor_plan/` (`run_all.py` runs the lot, including the JS) |

## What reads the plan

One drawing, several consumers. This is why it lives in `data/floor_plan.json`
and not under `heating:` in `config.yaml`, and why a change to it is scoped per
*part* rather than per feature (§ [Who may change what](#who-may-change-what)).

| Reader | What it takes | Documented in |
| --- | --- | --- |
| Heating | room dimensions, walls and their types, radiators, TRVs, temperature sensors, window/door contacts | [heating.md](heating.md), [heating-controller.md](heating-controller.md) |
| Daylight | room polygons, window area, glazing and orientation | [daylight.md](daylight.md) §7 |
| Signal coverage | every placed device's position, and walls as attenuators | [signal-coverage.md](signal-coverage.md) |
| Frames and chambers | the room list, adopted as chambers | [frames.md](frames.md) |
| Automations | per-room daylight, and the rooms rules can name | [automations.md](automations.md) |
| Swarm intelligence | the virtual per-room daylight devices | [swarm-intelligence.md](swarm-intelligence.md) |
| Air conditioning | `room_id`, to bind a unit to a room | [air-conditioning.md](air-conditioning.md) |

A device is placed **once** and has **one position**, and every one of those
readers resolves it the same way — see [Placing devices](#placing-devices).

## Where the plan lives

`modules/floor_plan_store.py` is the only way in: `load_plan()` / `save_plan()` /
`delete_plan()`. The plan is stored at `data/floor_plan.json`, and after the
first read it's served from memory, so a read costs no I/O and can be made from
the event loop. Nothing reads a plan from `config.yaml`.

Background images are separate files at `data/floor_plans/{level_id}.{ext}`.
Both the plan and the images are included in backups.

Earlier versions kept the plan at `heating.floor_plan` in `config.yaml`. On such
a hub, the first load (at startup) copies it to `data/floor_plan.json`, and the
old key is never read after that. The key is removed the next time the plan is
saved or deleted, since those writes rewrite `config.yaml` anyway. The move on
its own doesn't rewrite `config.yaml`, which would lose its comments. Restoring a
backup that predates the move replaces `data/floor_plan.json` with the restored
config's plan.

## The data model

A plan is levels; a level is what was drawn on it.

| Key | Holds |
| --- | --- |
| `levels[].walls[]` | `{id, x1, y1, x2, y2, type}` — `type` is external / party / internal / `unknown` |
| `levels[].rooms[]` | `{id, name, polygon}` |
| `levels[].openings[]` | windows and doors: `{id, wall_id, kind, offset_m, width_m, height_m, glazing \| door_type}` — positioned *along* their host wall |
| `levels[].radiators[]` | wall-mounted or freestanding (below), with `trv_ieee` |
| `levels[].sensors[]` | temperature sensors: position, room, height, `primary` |
| `levels[].contacts[]` | bound to an opening by `opening_id` |
| `levels[].devices[]` | everything else placed by position alone |
| `levels[].background` | the image's calibration — see [Background images](#background-images) |
| `plan.circuits[]` | heating circuits and their receivers |
| `plan.map` | the OpenStreetMap backdrop's anchor and opacity |
| `plan.north_offset_deg` | the compass |

Radiators have two modes: wall-mounted (`wall_id` + `offset_m`, clamped to wall
length) and freestanding (`x` + `y`). The effective mode is resolved after walls
are cleaned, and fields not belonging to the chosen mode are stripped so the
saved plan stays tidy.

`clean_floor_plan()` is the gate every save passes through. It fixes up ids,
drops references to things that don't exist, resolves the either/or fields
above, and clamps every number to a sane range — so nothing downstream has to
defend itself against a malformed plan.

### Coordinate convention

`+x` = right, `+y` = up (standard maths). `north_offset_deg` is the clockwise
angle from plan-up to true north, so 0 means plan-up is north and 90 means true
north points to the right of the plan.

Metres in, metres out: the SVG viewBox is in metres and zoom is the scale of the
`<g>`. SVG uses +x right, +y **down**, so y is flipped on both read and write —
`modelToSvg()` / `svgToModel()`, which are each other's inverse and the only
place that flip is allowed to happen.

### Compass and wall-bin convention

A wall's outward-normal bearing relative to true north decides both its
8-point compass label (N/NE/…/NW), which drives opening orientation, and its
legacy 4-bin label, which is the `dimensions.walls` bin:

| Bin | Facing | Bearing range |
| --- | --- | --- |
| `back` | N | −45 .. +45 |
| `right` | E | 45 .. 135 |
| `front` | S | 135 .. 225 |
| `left` | W | 225 .. 315 |

The 4-bin labels are arbitrary. `thermal_profile.py` only cares about the
external/party/internal type stored against each bin.

### Windows and rooms

An opening belongs to a room only if the opening itself lies on one of that
room's edges (`opening_borders_room`). Bordering the opening's wall isn't enough.
Before this, a window on an outside wall shared by two rooms counted in both
rooms' heat loss and solar gain. Plans drawn that way will see those two rooms'
figures change on the next projection.

**Which walls face outside.** An untyped wall is inferred from which *side* its
rooms are on (`rooms_each_side`): rooms on one side only means external, rooms on
both means party — something heated behind it. Counting rooms instead of sides,
as it did before, made one long outside wall that several rooms sit along look
like a party wall, which cost those rooms their daylight and under-counted their
heat loss. The cleaner writes `type: "unknown"` for a wall nobody typed, and that
is treated as the absence of an answer, not as an answer. A type the user sets
explicitly always wins.

## The editor

### Two views of one plan

`static/js/floor-plan.js` is one editor with one DOM (`#fpRoot`), mounted in one of
two places:

| Opened from | Host | View | Shows |
|---|---|---|---|
| **Topology → Floor plan** | inline in the tab | home | every placed device, the *Devices to place* palette, the map backdrop |
| **Heating Controller → floor plan** | the full-screen modal | heating | heating's devices only (TRVs, thermostats, temperature sensors, window/door contacts), the heating tools, circuits and thermal overlays |

Both views show the structure (walls, rooms, windows) and the compass. Both save
the whole plan. The heating view hides lights, plugs and routers but doesn't
remove them. What counts as a heating device comes from heating's own routes
(`/api/heating/controller/devices`, `/sensors`, `/contact-sensors`), so the filter
always agrees with the heating page. When the Heating modal closes, the editor
goes back into Topology if Topology is on screen.

The desktop layout is a fixed three-column rail/canvas/rail; below 768 px the
rails become drawers over the canvas. That, and the theming rule that the SVG
emits classes only and takes every colour from CSS variables, is in
[structure.md](structure.md) § Floor-plan editor.

### Drawing

| Tool | Does |
| --- | --- |
| Select | pick, and drag handles — a wall endpoint, a radiator along its wall, a device marker |
| Wall | chain mode: each click drops a vertex, click the first again (or Enter, Esc, right-click, double-click) to finish; Backspace removes the last one |
| Room | corner to corner on the walls, closed by clicking its first corner — [below](#rooms-sit-on-the-walls) |
| Window / Door | drag along a host wall; the opening is stored as an offset and width along that wall, never as free coordinates |
| Radiator / Sensor / Contact | heating view only; a contact near an opening binds to it automatically |
| Calibrate | two clicks and a real distance — [below](#calibrating-a-background-image) |
| Adjust image | move, resize or rotate the background — [below](#placing-a-background-image) |

**Snapping** has two independent steps, both in the tools rail. *Snap* rounds
every point to a grid (off / 1 / 5 / 10 / 25 / 50 cm, default 10 cm). *Angle*
is the step a wall segment's direction is locked to while Ctrl is held
(1 / 2 / 5 / 15 / 45°, default 1°), with the eight compass spokes made sticky
within a couple of degrees so a wall meant to be straight comes out straight.
Hold **Alt** to suspend all snapping for one click.

Separately from the grid, a new vertex merges with an existing endpoint within
~14 px *on screen* (`snapRadiusM`). Because that radius is screen-space, zooming
in shrinks it in metres — which is what makes a deliberate small gap drawable.

A wall end dropped beside another wall's side lands *on* it (`snapOntoWall`),
and finishing a wall chain joins any end left within 30 cm of the wall it meets
(`joinWallEnds`, skipped while Alt is held). Either way the junction becomes a
real corner that rooms can use.

### Rooms sit on the walls

The backend counts a window, and an outside wall, for a room only when that
wall lies along one of the room's edges, to within 5 cm
(`find_walls_for_room`, `opening_borders_room`). So a room traced by eye
inside the wall thickness loses its daylight and most of its heat loss. The
Room tool rules that out instead of loosening the match:

- **Corners only.** A room corner can only go on a wall end or where two
  walls cross (`roomCorners`). The candidates are drawn while the tool is
  active, the one a click would take is ringed, and a click anywhere else is
  refused. On a level with no walls, a corner goes on the grid or on another
  room's corner.
- **Edges follow the walls.** Between two clicked corners, the edge runs along
  the walls when they join them without a long detour (`roomLegs`), so a jog
  in the walls is picked up without being clicked. Where no wall links them,
  the edge goes straight — though an open-plan split still needs corners, so
  draw an internal wall along it first.
- **No overlap.** An edge that cuts a wall or enters another room is refused
  (the rubber band turns red first), and so is closing a room that overlaps,
  swallows or sits inside another, or crosses itself (`roomPolygonProblem`).
  Neighbours share an edge exactly.
- **Walls are joined first.** Picking the Room tool runs `joinWallEnds` on the
  level, so the near misses left by older drawings become corners.

For rooms drawn before this, **Snap rooms to walls** (room panel) joins the
walls, then moves every room's corners onto the nearest wall corner within
1.5 m and runs its edges along the walls. It repeats while that still fits
more rooms in, because one room may only fit once its neighbour has moved.
Rooms it can't place are left as drawn, and it says why. **Join wall ends**
(wall panel) does the wall half on its own.

**The save holds the same rule.** `POST /api/floor-plan` refuses (422, with
`room_problems` and a sentence naming the rooms) a plan in which two rooms on
one level overlap by more than 0.01 m² or a room's outline crosses itself
(`room_geometry_problems`, using shapely), so no other client can store one
either. Only a problem the saved plan didn't already have is refused
(`new_room_geometry_problems`): a plan drawn before the rule can still be
edited, and has to be fixed only if an edit makes it worse. On an image built
without shapely, the check logs a warning and lets the save through.

### Background images

The image is a tracing aid: draw over it, then calibrate so the drawing's
metres are real. Its placement is three numbers on `levels[].background` —
`pixels_per_metre`, `origin_x_m`, `origin_y_m` — plus `rotation_deg` and the
image's pixel dimensions.

`origin_{x,y}_m` is the bottom-left corner of the *unrotated* image, and
`rotation_deg` turns it anti-clockwise about its own centre — which is a
negative SVG rotation, since SVG's y axis points the other way. So the image's
top-left in model space sits at `(origin_x_m, origin_y_m + height_m)`.

Every route that moves, scales or rotates the image goes through four shared
helpers — `bgGeom()` / `bgPoint()` / `bgFrac()` / `bgResizeAnchored()` — so the
rendered `<image>`, the drag handles, the sidebar's number boxes and Calibrate
cannot disagree about where the image is. Covered by
`tests/floor_plan/js/test_bg_placement.js`.

#### Calibrating a background image

The Calibrate tool takes two clicks and the real distance between them, and
rescales the image so the two agree. The image is re-anchored on the first
click, which stays where it is.

A plan traced over that image was drawn at the old scale, so on its own it would
be left behind — still at its old size, no longer on the walls it traced. So
where the level already has a drawing, the editor asks: **Scale the drawing** or
**Only the image**. Scaling moves walls, rooms, openings (their position along
the wall and their width), radiators, sensors and placed devices by the same
factor about that first click, and with a single-level plan the map pin too.
Typed measurements are left alone — a radiator's length and output, a window's
height, a sensor's mounting height are the user's own figures, not the tracing's.
*Only the image* is the right answer when the drawing's measurements are already
correct and the image is the thing that is wrong. Either way the view zooms to
fit afterwards, and nothing is written until Save. The geometry is
`scaleLevelGeometry()`, covered by `tests/floor_plan/js/test_calibrate.js`.

#### Placing a background image

Calibrate fixes the *scale* of an image that is already roughly where it
belongs. When it is not — a plan imported at the wrong resolution, or one whose
calibration was lost — the **Adjust image** tool moves it: drag the image to
slide it, drag a corner to resize it about the opposite corner (the aspect ratio
is fixed, so a corner drag projects the cursor onto the diagonal it started on),
and arrow keys nudge by one snap step, ten with Shift. The same placement is
typeable in the sidebar — width in metres, left X, bottom Y, rotation — and
**Fit image to walls** scales and centres the image over whatever is already
drawn, sized to contain it, which is the one-click way back from a badly scaled
image.

#### Importing

An import carries no scale of its own, so the editor infers one: replacing an
existing image keeps that image's size and centre (the old px/m is *not* reused
— it belongs to the old file's pixel dimensions, and a different-resolution file
at that px/m lands tiny and nowhere near the walls), otherwise a level that has
already been traced gets the image fitted to its drawing, and only a level with
nothing on it falls back to 50 px/m at the origin. Recovering an orphan image —
one whose bytes are on disk but whose calibration is missing from the plan — is
the same inference. Nothing is written until Save.

Limits: 20 MB per upload, `image/png` and `image/jpeg` only. **PDFs must be
rendered to PNG client-side** (via pdf.js) before upload.

### Placing devices

A device is placed **once** and has **one position**. That position feeds auto
lights (the room a light is in, for per-room daylight), repeater advice (mesh
coverage) and heating (the room a sensor is in).

`levels[].devices[]` holds `{ieee, x, y, height_m?}` for a device placed by
position alone. The room isn't stored; it's derived from the room polygon the
point falls in. A device held by a heating object takes its position from that
object instead:

| Device | Position from |
|---|---|
| TRV fitted to a radiator | the radiator (`radiators[].trv_ieee`) |
| Temperature sensor used by heating | the sensor entry (`sensors[].x/y`) |
| Contact bound to a window or door | the middle of the opening (`contacts[].opening_id`) |
| anything else | `devices[]` |

`clean_floor_plan()` enforces this. It drops a `devices[]` entry for any device a
heating object holds, and keeps only the first of any repeats. So when Heating
fits a placed TRV to a radiator, or uses a placed sensor for a room, the device
moves off `devices[]` in the same save. `floor_plan.placed_devices(plan)` resolves
every device to one `{ieee, level_id, x, y, room_id, source}`. Per-room daylight
and the mesh overlay read positions through it.

In the editor, the palette lists every device the hub knows that isn't placed
anywhere, grouped as lights, heating, routers, coordinator and other. Drag one
onto the plan, or tap it and then tap the plan. Drag a marker to move it; on a
touch screen, select it first and then drag. A placed device's panel says which
room it's in. For heating devices it also offers *Use as this room's heating
sensor* or *Attach to the nearest window or door*, which hand the device to heating.

### The map backdrop

The map backdrop draws OpenStreetMap tiles (`/api/map/tiles`, zoom 19) around the
home location, rotated by `north_offset_deg`. *Mark where the home pin is* sets
`plan.map.anchor_x_m/anchor_y_m`, the point on the plan where the address pin
sits. Turn the compass until the map's buildings line up with the walls.

### Overlays

The editor draws several layers over the same geometry. Each belongs to the
feature it serves, and is documented there:

| Layer | View | Documented in |
| --- | --- | --- |
| Thermal field, isotherm contours, cold zones | heating | [heating.md](heating.md) § Thermal overlays |
| Solar gain per window, sun-path arc | both | [heating.md](heating.md) § Solar Gain |
| Daylight in each room | both | [daylight.md](daylight.md) §7 |
| Mesh links, signal heatmap, repeater advice | home | [signal-coverage.md](signal-coverage.md) |

## Who may change what

A save is split into two parts by `floor_plan.changed_parts(old, new)`. The route
refuses the whole save if the caller lacks the scope for any part it changes:

| Part | Scope | What's in it |
|---|---|---|
| structure | `device:write` | walls, rooms, openings, levels, background images, compass, map, `devices[]`, and the position of heating's sensors |
| heating | `heating:write` | radiators (and their TRVs), sensor roles (room, device, kind, primary, height), contacts, circuits |

Reading needs `device:read` or `heating:read`. The preview needs `heating:read`.
Deleting the whole plan needs both write scopes. Fitting a placed device to a
radiator is a heating change only, even though it removes a `devices[]` entry.
The comparison runs on the cleaned old plan against the cleaned new one, so a
stale copy that would undo someone else's placement reads as a structure change
and is refused. Admins satisfy every scope, and the shipped `users` group holds
both write scopes. A heating-only caller can't move a sensor's position, because
positions are structure.

## API

`routes/floor_plan_routes.py`:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/floor-plan` | read the saved plan, plus `home` (`{lat, lon}`) for the map |
| `POST /api/floor-plan` | save plan, project into circuits, return warnings |
| `GET /api/floor-plan/preview` | dry-run projection |
| `DELETE /api/floor-plan` | clear the plan |
| `POST /api/floor-plan/image/{level_id}` | upload a background image |
| `GET /api/floor-plan/image/{level_id}` | fetch the image |
| `DELETE /api/floor-plan/image/{level_id}` | clear the image |
| `GET /api/floor-plan/daylight` | per-room daylight for a time today ([daylight.md](daylight.md) §7) |
| `GET /api/floor-plan/mesh`, `GET /api/floor-plan/coverage` | the mesh layers ([signal-coverage.md](signal-coverage.md)) |

Every endpoint also answers at its old `/api/heating/floor-plan…` address, served
by the same handler and left out of the OpenAPI schema. Permissions are checked
in the routes, per part of the plan (see [Who may change what](#who-may-change-what)).
The middleware table maps both addresses to any signed-in caller.

## Tests

`python3 tests/floor_plan/run_all.py` runs everything:

| Module | Covers |
| --- | --- |
| `test_store.py` | the store, the `config.yaml` migration, backup restore |
| `test_model.py` | `clean_floor_plan`, geometry, the projection |
| `test_mesh.py` | the mesh layer |
| `test_radio.py` | the path-loss model behind coverage |
| `test_routes.py`, `test_scopes.py` | the endpoints and per-part scoping (need FastAPI + zigpy) |
| `js/test_calibrate.js` | recalibration, and the drawing following it |
| `js/test_bg_placement.js` | background placement: corners, rotation, corner-drag anchoring, fit-to-walls |

The JS tests slice the functions they test straight out of the shipped
`static/js/floor-plan.js` and run them under node, so they test what ships
rather than a copy. They are skipped, not failed, when node isn't installed.
