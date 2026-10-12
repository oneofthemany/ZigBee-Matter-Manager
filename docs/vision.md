# Vision — object detection for cameras

A sidecar watches the cameras you choose and reports **person**, **vehicle**
and **animal** as signals on each camera device (`camera::<id>`), so rules,
notification rules and alarm zones use them like any sensor. Everything runs
on the hub; no picture leaves it.

## Setting up

1. ZMM Manager → Services → Cameras → **Object detection → Enable**. It uses
   the Coral if one is ready (see §Hardware), otherwise the CPU.
2. In the app: Cameras → Settings → **Edit** a camera → **Detection** tab → **Detect objects on this
   camera**, and tick what to look for.
3. The camera device now has `person` / `vehicle` / `animal` (true or false).
   **Last detection** in the camera list shows the frame behind the latest
   one, with its box drawn.

What each signal covers: `person`; `vehicle` — car, truck, bus, motorcycle,
bicycle; `animal` — cat, dog, bird, horse, sheep, cow, bear. There is no
package detection: the model (§Models) doesn't know them. People can be
enrolled for face recognition (§Faces); recognising them on the cameras is not
built yet.

Notification rules have **Person / Vehicle / Animal seen on camera** triggers,
and a notification about a camera can carry the detected frame — see
[notifications.md](notifications.md) §Camera snapshots.

## Pipeline

Per camera, in `vision/worker.py`:

1. **Frames.** ffmpeg decodes the stream to 640×360 raw frames, two a second.
2. **Motion** (`motion.py`). Each frame is compared with the one before, in
   8-pixel cells. Nothing moved → the detector isn't run. The whole picture
   changing at once (exposure, IR cut, a light) is not motion.
3. **Region.** The detector is shown a square around what moved rather than
   the whole frame, so a distant person fills far more of its 320×320 input.
4. **Detect** (`detector.py`). One pass; boxes are mapped back to the frame.
5. **Presence** (`tracker.py`). A group turns on when seen in two passes
   running, and off 12 s after it was last seen. While something is present
   but still, its area is re-checked every 4 s — a person standing still stays
   present without the detector running on every frame.

The motion gate is also what keeps a Coral cool: on a quiet scene it does
nothing.

## Zones

A zone is a polygon on the camera's picture with a name: the drive, the porch,
the lawn. Cameras → Settings → **Edit** a camera → **Zones** tab → **Add zone**, then tap the
picture to place corners. A tap near an edge adds a corner to that edge; drag
a corner to move it. Up to 8 zones per camera, 3–24 corners each, any shape.

- **In a zone** means the object's feet are: the middle of the bottom edge of
  its box. Someone standing on the drive counts even when their head is in
  front of the street.
- Each zone adds its own signals beside the camera's: a zone created as
  "Front drive" gives `person_front_drive`, `vehicle_front_drive` and so on,
  for whichever of person / vehicle / animal it is ticked for.
- A zone's id is fixed when it is first saved and its signals are named for
  the id, not the name. Rename a zone as often as you like: its signals, and
  every rule or alarm zone using them, carry on. Notifications show the
  current name.
- **Ignore anything outside the zones** makes the camera's own `person` /
  `vehicle` / `animal` mean "in at least one zone". It also stops the detector
  looking at movement that is nowhere near a zone, so a busy street costs
  nothing.
- A notification about a camera names the zones the object is in, and a
  notification rule can be limited to one zone
  ([notifications.md](notifications.md) §Camera zones).

Zones are drawn on the camera's live picture (go2rtc's snapshot), whether or
not detection is on yet; they take effect once it is. Points are stored as
fractions of the picture. The detector sees the same picture stretched to
640×360 rather than letterboxed, so a fraction means the same spot in both —
the model resizes to a square anyway, so the stretch costs it nothing. (A
non-16:9 camera's **Last detection** frame looks stretched for the same reason.)

## Faces

Cameras → **People**: who face recognition knows. So far this is enrolment
only — nothing recognises anyone on a camera yet; the plan is
[plans/vision-enrolment.md](plans/vision-enrolment.md).

A person is a name and pictures of their face:

- **Photos** — four posed ones: **Front**, **Left**, **Right** and **Tilt**
  (head tipped down, as a camera above the door sees it). One per slot; a new
  one replaces the old. Taken with the phone's camera or picked from its
  library. The browser shrinks the picture and drops its EXIF before sending,
  so where it was taken never leaves the phone.
- **From cameras** — *Look now* takes the camera's full-resolution still and
  lists every face in it, each with who it looks like; **This is …** adds one to
  the person. Up to 40 each. These teach more than phone photos: the real lens,
  angle and light, by day and by night. *Use a saved picture* does the same
  with an uploaded one.

What a picture is checked for:

| | |
|---|---|
| No face, or the file isn't a picture | refused |
| Two faces of similar size (a posed photo only) | refused — it can't tell whose it is |
| Under 24 px between the eyes | refused: too little face to compare |
| Front photo that's turned; Left / Right that's straight on; Tilt that isn't | kept, with a note |

Pose is measured from the face, not taken from the slot. Left and right are as
the picture shows them — a mirrored selfie swaps them, and recognition needs
both sides, not which is which.

**How well it holds together.** Every picture carries a score against the same
person's other pictures: *strong* (0.5 and up), *ok*, or *weak* (under the
match threshold, 0.363) — a weak one is a poor photo or the wrong person, and
worth replacing. A person whose pictures come close to someone else's gets a
warning naming them; more camera pictures of both is the cure.

**Who.** `vision:enrol` sees and changes faces; `camera:read` alone does
neither, and *Look now* needs both. No built-in group has it
([auth.md](auth.md)).

**Where it's kept.** `data/vision/gallery/`, owned by the sidecar:
`people.json`, and per person `<id>/samples.json` plus `<sample>.jpg` — the
aligned 112×112 face, never the whole photo. The crop is kept so a later model
can re-read it rather than everyone enrolling again. Removing a person removes
the folder. A person's id is fixed when they're added and survives renaming.
The gallery is **not in backups** yet.

**How.** `vision/faces.py`: YuNet finds faces and five landmarks on a copy at
most 640 px across; the face is aligned from the original picture, measured,
and turned into a 128-number vector by SFace. Two vectors are compared by
cosine. About 180 ms a face on the reference hub's CPU. The models download on
the first picture (about 40 MB), which can take a minute.

## Metrics

The sidecar appends a sample every 30 s to `data/vision/metrics.jsonl`: the
backend, looks since the last sample, time per look, cameras online and, for an
M.2 Coral, the chip's temperature and throttle step. A file rather than the
telemetry database, which has a single writer in the app. Kept for a week.

System Overview shows it as **Object detection**, under System History and
following its time range: TPU temperature, ms per look and looks per minute,
with throttled stretches shaded — so "it got slow" lines up with "it got hot".
The card appears only once detection has run.
`GET /api/telemetry/system/detector?hours=&bucket=` serves the series.

## One connection per camera

go2rtc already holds a connection to each camera for live view. Detection
reads the same stream from go2rtc's API (`/api/stream.mp4`), which asks for
the API password even from the hub itself, so:

- the camera is connected to once, however many viewers and whether or not
  detection is on;
- the camera's own login never reaches the detection sidecar.

go2rtc's RTSP server stays off: it would be the obvious way to share a
stream, but it does not ask a loopback client for a password.

A camera's **low-resolution stream for detection** is optional. Set, it is a
second go2rtc stream (`zmmd_<id>`) and a second connection to the camera, in
exchange for much less decoding work — worth it on a small CPU with a
high-resolution main stream. Detection needs go2rtc running either way.

## Backends

| | |
|---|---|
| Coral (M.2, Mini PCIe, USB) | TFLite with the Edge TPU delegate |
| CPU | TFLite, two threads |

If the Coral can't be opened the sidecar falls back to the CPU and says so on
the Object detection card in the app, with the reason. Intel, AMD, NVIDIA and
Hailo hardware is detected (§Hardware) but has no backend yet.

On the M.2 Coral in the reference hub (J3455, Fedora Silverblue), the rootful
sidecar opens `/dev/apex_0`, loads the delegate and reports `detector ready on
coral`. Detection quality on real frames through the Coral hasn't been compared
with the CPU's; the CPU path is exercised end to end.

## Models

SSDLite MobileDet, COCO, 320×320 (Google, Apache-2.0), in CPU and Edge TPU
builds. For faces: YuNet (MIT) and SFace (Apache-2.0), both from OpenCV Zoo,
run by ONNX Runtime on the CPU, fetched only when someone is first enrolled.
Like every face model, SFace was trained on photos of people who were not
asked; why it was still chosen is in
[plans/vision-enrolment.md](plans/vision-enrolment.md) §1. The models, the label list and the Edge TPU library are not in the app
image: `vision/assets.py` downloads them on first start into
`data/vision/models/`, each from a fixed URL and checked against a pinned
SHA-256. The Edge TPU library is a build made for the TFLite version
`ai-edge-litert` ships; the two pins move together.

## Sidecar

The container is `<app>-vision`, created by the ZMM Manager
(`manager/vision.py`) from the app's own image running `python -m vision`, on
host networking, sharing the app's `data` and `logs` (not `config`). It gets
the Coral's device node when there is one. The Manager recreates it when the
app is upgraded or the usable hardware changes, and asks the host for a
boot-time service like the other sidecars; that unit starts after the Coral
driver's.

### Sidecar API

Loopback only (`127.0.0.1:8556`), bearer token from `data/vision/token`
(0600, made by whichever of the app and the sidecar starts first).

| | |
|---|---|
| `GET /status[?after=<version>&wait=<s>]` | backend, per-camera health and presence; with `after`, waits for a change |
| `PUT /config` | `{cameras: [{id, url, labels, threshold, fps?, zones?, zones_only?}]}` — held in memory only, since URLs carry go2rtc's password |
| `GET /snapshot/<id>.jpg` | the latest detection's frame, boxes and zones drawn |
| `GET /frame/<id>.jpg` | the frame the detector sees now, zones outlined |
| `GET /faces` | people, their pictures (never the vectors), each picture's agreement and each person's nearest other |
| `POST /faces` `{name}` · `PATCH /faces/<id>` `{name}` · `DELETE /faces/<id>` | add, rename, remove a person |
| `PUT /faces/<id>/slot/<front\|left\|right\|down>` | image body: the posed photo for that slot; 422 with the reason if it can't be used |
| `POST /faces/detect[?camera=<id>]` | image body: every face, largest first, with a thumbnail and its best match; held 10 minutes under the returned `token` |
| `POST /faces/<id>/samples` `{token, face}` | add one of those faces to a person |
| `DELETE /faces/<id>/samples/<sample>` · `GET /faces/<id>/<sample>.jpg` | remove a picture; its crop |

Presence is keyed `person`, and `person:<zone>` for a zone; the app stores the
latter as `person_<zone>`.

The app (`modules/vision.py`) long-polls `/status`, re-sends the camera list
whenever the sidecar's config hash isn't the one it was given (a restarted
sidecar has none), and clears every object signal if the sidecar goes away.

## API

| | |
|---|---|
| `PUT /api/cameras/{id}` | `detect: {enabled, labels, threshold, url?, zones_only?, zones?: [{id?, name, points: [[x, y]…], labels?}]}`; leaving `zones` out keeps them, and sending a zone's `id` back keeps its signals through a rename |
| `GET /api/cameras/{id}/detection` | latest detection frame (JPEG), `camera:read`; `?view=frame` for what the detector sees now |
| `GET /api/cameras/vision` | sidecar reachability, backend, per-camera health (admin) |
| `/api/vision/faces…` | the sidecar's `/faces` API, `vision:enrol`: `GET`, `POST {name}`, `PATCH`/`DELETE /{id}`, `PUT /{id}/slot/{slot}` (image body), `POST /detect` (image body), `POST /detect/camera/{camera}` (also `camera:read`), `POST /{id}/samples {token, face}`, `DELETE`/`GET /{id}/samples/{sample}` |

In the ZMM Manager (`:8001`; actions need the Manager token): `GET /vision`,
`POST /vision/enable`, `/vision/disable` `{remove?}`, `/vision/restart`,
`/vision/service` `{action}`.

## Not yet

Recognising enrolled people on the cameras, known objects and zone states
([plans/vision-enrolment.md](plans/vision-enrolment.md)); per-object counts,
GPU and Hailo backends, and larger models.

## Hardware

`manager/accelerators.py` reads sysfs (read-only, no privileges) and reports
what detection could run on — ZMM Manager → Host OS → **Detection hardware**,
or `GET /host/accelerators`.

| Found as | Usable when |
|---|---|
| Coral M.2 / Mini PCIe — PCI `1ac1:089a` | the host's `apex` driver is bound, which creates `/dev/apex_0` |
| Coral USB — `1a6e:089a`, or `18d1:9302` once its firmware loads | plugged in (a USB 3 port is much faster) |
| NVIDIA GPU | driver bound and the container toolkit's CDI spec present |
| Intel / AMD graphics | driver bound |
| Hailo — PCI vendor `1e60` | driver bound |

Being on the bus is not being usable: `lspci` lists an M.2 Coral whether or
not its driver is loaded. The M.2 card needs Google's `gasket` and `apex`
kernel modules on the host; nothing in a container can supply them.

The probe prefers Coral, then Hailo, NVIDIA, Intel, AMD, and falls back to the
CPU. A device that is fitted but not ready is never chosen, and is named as
what could be used once it is set up.

## Coral driver

`scripts/coral_driver.sh` builds, loads and keeps loaded the `gasket` and
`apex` modules an M.2 / Mini PCIe Coral needs. It is automatic: whenever a
Coral card is fitted and its driver isn't set to load at boot, the Manager's
watchdog asks the host helper (installed by `install_watcher.sh`) to install
it, and the helper does the work as root. A driver already loaded by hand is
kept, and a copy is built so the next boot has one. After a failure (Secure
Boot, no network for a first build) it tries again every six hours; the
Detection hardware card shows why.

- **Source.** Google's `gasket-driver` is archived and no longer compiles on
  current kernels, so the build uses the maintained `KyleGospo/gasket-dkms`
  fork, pinned to one commit and checked by SHA-256 before anything is built.
  Moving to a newer commit means changing both in the script.
- **Build.** On the host when it has the kernel's headers, `make` and `gcc`;
  otherwise in a throwaway container of the host's own release (Fedora,
  Debian, Ubuntu). Any other distro needs the headers installed on the host.
- **Why not DKMS.** Image-based hosts (rpm-ostree) have a read-only `/usr` and
  no DKMS, so modules live in `/var/lib/zmm-coral/modules/<kernel>/` and are
  `insmod`ed. A distro-packaged `apex` module, where there is one, is used
  instead and nothing is built.
- **Boot.** A `zmm-coral-driver` systemd unit or OpenRC script runs
  `coral_driver.sh load`. With neither, the driver is loaded once and the
  status says it won't come back on its own.
- **Kernel updates.** After a host update, and before any reboot ZMM starts,
  modules are prebuilt for every installed or staged kernel. A kernel that
  arrives some other way is built for on first boot into it, which needs the
  network and delays the Coral by a minute or so.
- **Secure Boot.** The modules are unsigned, so a Secure Boot host refuses
  them; the script says so rather than trying.
- **Access.** `/dev/apex_0` is root-only by default; a udev rule gives it to
  an `apex` group.

Only the Fedora paths (host toolchain and container) have been run for real.

## Coral temperature

The apex driver reads the chip's temperature every 5 s and steps its clock
down at three trip points — 85, 90 and 95 °C by default, each halving the
speed again — and the chip cuts out by itself at 100 °C. Nothing else needs to
watch it.

The card shows the live temperature and whether the chip is currently slowed.
**Start slowing down at** moves the first trip point (50–85 °C) and puts the
other two 5 and 10 °C above it; the setting is kept in `data/coral/thermal`
and re-applied on every load. A lower value runs cooler at the cost of
inference speed once it is reached.
