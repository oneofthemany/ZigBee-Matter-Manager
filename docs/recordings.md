# Recordings — camera footage and clips

Each camera can keep **clips of events**, or **everything** plus clips. Watch
them under Cameras → **Recordings**.

## Setting up

1. ZMM Manager → Services → Cameras → **Recording → Enable**. (go2rtc must be
   enabled too: the recorder reads its copy of each stream.)
2. In the app: Cameras → Manage → edit a camera → **Recording → Keep**:
   - *Clips of events* — a clip for each event, and nothing else;
   - *Everything, and clips of events* — continuous footage for the hours you
     choose, plus the clips.
3. Tick what counts as an event: motion (needs the camera's ONVIF events),
   person, vehicle, animal (need detection on — [vision.md](vision.md)).

## Where it is kept

`data/recordings/` in ZMM's data folder, on the host's disk — not inside any
container, so it survives upgrades, restarts and a rebuilt container. To put
it on another disk, mount that disk at `data/recordings` on the host.

Recordings are not in backups: they are large, and a backup is for settings.

## It keeps recording while ZMM restarts

Recording runs in its own container (`<app>-recorder`), not in the app. An app
restart, upgrade or crash does not stop it, and it has a boot-time service
like the other sidecars. It also remembers what it was told to record
(`data/recordings/config.json`), so after a reboot it resumes without waiting
for the app.

What does need the app: **starting a clip**. Motion and detection reach the
recorder through the app, so an event that happens while the app is down gets
no clip of its own — on a camera set to keep everything it is still in the
footage.

### Open question: clips across an app restart

**To investigate — nothing decided or built.** Events that happen while the app
is restarting or upgrading get no clip, because motion and detection reach the
recorder through the app; on a camera set to *clips of events* only fifteen
minutes of footage is kept, so a longer outage loses the event outright.
Directions to weigh:

- **Signals straight to the recorder.** The vision sidecar posts
  `/signal` to the recorder as well as reporting to the app, and the recorder
  subscribes to ONVIF motion itself. Removes the app from the path entirely;
  duplicates the ONVIF pull-point code and the zone/label config.
- **Catch-up after restart.** The recorder keeps footage while the app is down
  regardless of mode (extend the buffer while the app is unreachable), and
  the app or vision sidecar replays what it saw — detection could re-scan the
  gap's footage after the fact.
- **Pre-upgrade hand-off.** Before an upgrade, switch every recording camera
  to continuous until the app is back, then cut clips from the gap by
  re-running detection on it.
- **Measure first.** How long a real upgrade leaves the app down on the hub,
  and how often events land in that window.

## How it works

- **No re-encoding.** ffmpeg copies the camera's stream as it is into
  four-second MPEG-TS segments. It costs disk, not CPU, and the quality is the
  camera's own.
- **One connection per camera.** The recorder reads go2rtc's copy of the main
  stream, the same one live view and detection share.
- **A clip** is the segments from *seconds before* the event to *seconds
  after* it, joined into an MP4. Segments end on the camera's keyframes, so a
  clip can run a few seconds over at each end. An event longer than five
  minutes becomes back-to-back clips.
- **The thumbnail** is the detection frame with its box when detection
  started the event, otherwise a frame from the clip.
- **Playing footage** joins the stretch you ask for (five minutes at a time
  in the UI, ten at most) into an MP4 on demand.
- **H.265 cameras** record fine, but many browsers won't play H.265; download
  the clip, or set the camera to H.264.

## Space

Three things delete recordings, in this order:

1. **Each camera's own time limits** — footage after its hours, clips after
   its days. A camera that keeps only clips holds fifteen minutes of footage
   to cut them from.
2. **The space limit** (20 GB unless changed, under Recordings → Change
   limit): when over it, the oldest footage goes first, across all cameras,
   and clips only once there is no footage left to give.
3. **A nearly full disk**: with under 2 GB free the same thing happens,
   whatever the limit says.

A camera that is removed or switched off loses its footage at the next sweep
(every two minutes); its clips stay for the default fourteen days.

Rough sizes: a 2 Mbit/s stream is about 22 GB a day; 8 Mbit/s about 86 GB.

## Who can see

Watching and downloading need `camera:read`; deleting a clip and changing the
space limit are admin. Responses are marked `private, no-store`.

## Sidecar API

Loopback only (`127.0.0.1:8557`), bearer token from `data/recordings/token`.

| | |
|---|---|
| `GET /status` | per-camera recording state and the fingerprint of the camera list it holds |
| `PUT /config` | `{cameras: [{id, url, record}]}` — saved, 0600, so recording resumes after a restart |
| `POST /signal` | `{camera, state}` — a camera's signals; starts, extends or ends its event |
| `PUT /thumb/<id>` | a JPEG for the event under way |

The app (`modules/recordings.py`) re-sends the list whenever the recorder's
fingerprint isn't the one it was given, and reads the folder itself to list,
play and delete — both containers mount it.

## API

| | |
|---|---|
| `PUT /api/cameras/{id}` | `record: {mode: off|events|continuous, events, pre_s, post_s, clip_days, hours}` |
| `GET /api/recordings?camera=&before=&limit=` | clips, newest first |
| `GET /api/recordings/clips/{camera}/{id}.mp4` (`.jpg`) | a clip (range requests work) or its thumbnail; `?download=1` |
| `DELETE /api/recordings/clips/{camera}/{id}` | admin |
| `GET /api/recordings/footage/{camera}?start=&end=` | stretches there is footage for (epoch seconds, two days at most) |
| `GET /api/recordings/footage/{camera}/play?start=&seconds=` | that stretch as one MP4 |
| `GET /api/recordings/status` | what is recording, space used and free |
| `PUT /api/recordings/settings` | `{max_gb}`, admin |

In the ZMM Manager (`:8001`; actions need the Manager token): `GET /recorder`,
`POST /recorder/enable`, `/recorder/disable` `{remove?}`, `/recorder/restart`,
`/recorder/service` `{action}`.

## Not yet

A scrubbing timeline, clips attached to notifications, recording a camera's
sub-stream instead of its main one, and audio from cameras whose audio codec
go2rtc can't put in MP4.
