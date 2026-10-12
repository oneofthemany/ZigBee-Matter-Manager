# Chambers

A **chamber** is a room in the home. There is one list of them, and every part
of the app that talks about rooms means an entry in it: the device list,
Frames, groups, heating, the floor plan, swarm and automations.

`modules/chambers.py` holds the registry logic (pure, no FastAPI);
`routes/chamber_routes.py` is the API and the hook the heating and floor-plan
saves call.

## One registry, two facets

`chambers:` in `config.yaml` is the registry. Heating and the floor plan keep
their own room records, because they carry what only they need (targets,
TRVs, polygons), but each one *is* a chamber:

| Record | Facet | Shared with the chamber |
| --- | --- | --- |
| `heating.circuits[].rooms[]` (both copies) | `heated` | `id`, `name` |
| the plan's `levels[].rooms[]` | `mapped` | `id`, `name`; the plan's level becomes the chamber's |

A chamber is at most one heating room and one outline. The id is fixed at
creation and never changes on rename — learned heating data, history, device
assignments and frames are keyed on it.

### Keeping it one list — `reconcile`

Every write that can change rooms runs `chambers.reconcile` before it saves
(`chamber_routes.apply_room_edit` for the heating and floor-plan saves):

1. Each heating or plan room is registered under its own id.
2. A room new to the registry absorbs an **unused** chamber of exactly the
   same name, instead of doubling it. Devices, groups and frames on the old id
   are moved across.
3. The registry's name is written into both records. The exception is the
   side the user just saved: a name that changed there is a rename, and the
   registry takes it. "Changed" is judged against that side's names before
   the save, so a stale copy cannot undo a rename.

`build_registry` also lists rooms not registered yet, so a config from before
the single registry reads complete before its first save.

## Creating a chamber

All three places create the same thing:

| Where | How |
| --- | --- |
| Device list | **Chambers** button, or **+ New chamber…** in the device modal's picker |
| Heating | **Add room** asks which chamber (or names a new one); the room takes its id |
| Topology / floor plan | drawing a room asks which chamber it is; also the **Chambers** button |

Names are unique, compared without case. Creating a name that exists is
refused; the pick-or-create dialog picks the existing chamber instead.

## Duplicates and merging

`GET /api/chambers` returns `duplicates`: pairs that look like one room (same
name, or close — possessives, punctuation and a trailing "room" ignored, a
differing number is a different room). The chamber manager offers each as a
one-tap merge.

`merge_chambers(source, into)` keeps `into`'s name and whichever id heating or
the plan already uses. Two chambers that are both heated or drawn cannot be
merged — remove one of them in heating or on the plan first.

## Devices

Assignment is `device_settings[ieee]["chamber"]`. A TRV or room sensor bound
in heating is in that heating room's chamber: it is put there on each heating
or plan save, and assigning it elsewhere is refused — move it in heating and
its chamber follows.

## Deleting

A chamber that heating or the plan still uses cannot be deleted; it would be
registered again on the next save there. Deleting an unused one unassigns its
devices and groups.

## Heating zones

A heating advisor zone (`heating.zones[]`) is a chamber with advisor settings —
target, setback, minimum, priority, schedule. Its `id` is the chamber id; it
stores no name and no devices:

- its name is the chamber's, so a rename shows up in the advisor;
- its devices are the heating devices whose chamber it is.

One zone per chamber. A merge carries the zone to the surviving id (`into`'s
settings win, as its name does); deleting the chamber removes its zone.

A zone saved from before chambers (own id, name and device list) is adopted
on save: it moves to the chamber of that name, created if missing, and its
devices that are in no chamber yet join it.

## Not chambers

`zones` / `data/zones.yaml` are RSSI presence-detection zones, and stay
separate.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/chambers` | registry, levels, duplicate pairs |
| `POST /api/chambers` | create, or rename with `id` |
| `POST /api/chambers/merge` | `{source, into}` |
| `DELETE /api/chambers/{id}` | delete an unused chamber |
| `GET /api/chambers/assignments` | `{ieee: chamber_id}` |
| `POST /api/chambers/assign` | `{ieee, chamber}` |
| `POST /api/chambers/assign/bulk` | `{ieees, chamber}` |
