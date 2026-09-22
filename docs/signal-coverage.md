# Signal coverage — the mesh on the floor plan

Devices placed on the home floor plan ([docs/floor-plan.md](floor-plan.md),
shared with Heating) give the Zigbee mesh a geometry. From that the hub draws the
links to scale, learns what this house's walls and floors cost the signal,
predicts the signal everywhere, and says where a repeater would help.

Both layers live in the Topology tab's floor-plan view: **Topology → Floor
plan → Layers**. They are hidden in Heating's view of the same plan, which shows
only heating's devices.

| Part | Where |
|---|---|
| One link per pair, from the neighbour tables | `modules/mesh_plan.py` |
| Calibration, path-loss fit, heatmap, repeater advice | `modules/radio_model.py` |
| `GET /api/floor-plan/mesh`, `GET /api/floor-plan/coverage` | `routes/floor_plan_routes.py` |
| Drawing both layers | `static/js/floor-plan.js` |
| The force-directed graph and connection table | `static/js/mesh.js` (unchanged) |

## The mesh on the plan

In the home view, Layers → *Mesh links* draws the Zigbee mesh over the plan, from
`GET /api/floor-plan/mesh` (`system:read`, like `/api/network`).

`modules/mesh_plan.py` merges the neighbour tables into **one link per pair**. A
link is usually listed from both ends, each with its own LQI and its own
relationship. The merged link keeps both (`lqi_ab`/`lqi_ba`, `rel_ab`/`rel_ba`),
and is coloured by the **worse** of the two, in the same bands and colours as the
Topology graph (200+, 150+, 100+, below 100). Links to devices the hub doesn't
know are dropped. Links with an offline end aren't drawn, and offline devices are
dimmed.

The server sends links by device id only. Positions are resolved in the editor
from the plan being edited, by the one-position rule (`livePositions()`, which
mirrors `floor_plan.placed_devices`), so a link follows a device while you drag
it, before you save. Where only one end is on the level being shown, a short
dashed stub points out from that end, labelled with the far device and either
"↕ <level>" (placed on another floor) or "not placed". The coordinator is drawn
as a square. Hover a link for both LQIs and the relationship. The note under the
toggle counts the devices on the mesh that aren't placed yet.

## Signal coverage

Layers → *Signal heatmap* colours each level by the signal a device would get
there, marks the devices that are struggling, and suggests where a repeater
would help. `modules/radio_model.py` is the model; `GET /api/floor-plan/coverage`
(`system:read`) runs it off the event loop against the saved plan.

**LQI to dBm.** Neighbour tables report LQI, not RSSI, and every radio stack
maps the two differently. So the hub calibrates itself: devices that talk
straight to the coordinator have both a LQI and an RSSI recorded, and a line is
fitted through those pairs (four or more, spanning at least 40 LQI). Failing
that a mid-range default is used and the UI says the dBm are rough.

**What it learns.** For every measured link between two placed devices:

    RSSI = p0 − 10·n·log10(distance) − ext·heavy walls − int·light walls − floor·floors

Each direction of each link is one reading, and the coordinator's own RSSI of a
direct neighbour is another. The five parameters are fitted with their textbook
values as priors (`PRIOR`), each pulled towards the data in proportion to how
much the data says. So three links barely move it, while fifty links teach it
what this house's walls actually cost. Results are clamped to physical bounds,
and reported with the number of readings and the fit error. Fewer than 8
readings, or worse than 10 dB error, is flagged as a rough guide.

Heavy walls are external and party walls (brick); light are internal and
unknown (stud). Walls are counted by intersecting the straight line between two
points with the level's walls; across floors, the two levels' counts are
averaged and the floors between them are counted.

**The heatmap** is the best predicted signal from any online router or the
coordinator, on a grid over the level (0.5 m, coarsened past 5,000 cells). It
uses the editor's thermal-field format and rasteriser. The field is computed
past the walls so the weak-signal contour follows the signal rather than the
rooms, and the image is clipped to the rooms.

**Weak devices** are placed, online, non-coordinator devices whose best measured
link is below LQI 100 (the graph's red band) or whose signal is below −85 dBm.

**Repeater suggestions** are greedy: each candidate spot (room middles, and
points along inside walls where a socket usually is) must hear the mesh at −75
dBm or better and stand 1.5 m clear of anything already relaying. A weak device
counts as lifted if the spot brings it to −80 dBm and gains it at least 6 dB.
The best spot is taken, added to the sources, and the search repeats, up to
three. Each suggestion names its room, what it would lift and by how much.

**Limits.** The model is one number per wall class, so a mirror, a foil-backed
wall or a fridge is invisible to it. Predicted figures are estimates, labelled
as such. Suggesting where to *move the coordinator* is not implemented; when
nothing can be suggested the UI says so rather than guessing.

