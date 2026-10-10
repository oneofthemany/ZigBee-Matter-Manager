# Vision — object detection for cameras

A sidecar watches the cameras you choose and reports **person**, **vehicle**
and **animal** as signals on each camera device (`camera::<id>`), so rules,
notification rules and alarm zones use them like any sensor. Everything runs
on the hub; no picture leaves it.

## Setting up

1. ZMM Manager → Services → Cameras → **Object detection → Enable**. It uses
   the Coral if one is ready (see §Hardware), otherwise the CPU.
2. In the app: Cameras → Manage → edit a camera → **Detect objects on this
   camera**, and tick what to look for.
3. The camera device now has `person` / `vehicle` / `animal` (true or false).
   **Last detection** in the camera list shows the frame behind the latest
   one, with its box drawn.

What each signal covers: `person`; `vehicle` — car, truck, bus, motorcycle,
bicycle; `animal` — cat, dog, bird, horse, sheep, cow, bear. There is no
package or face detection: the model (§Models) doesn't know them.

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

**The Coral path has not been run against a real Coral.** Everything up to
loading the delegate has; the CPU path is exercised end to end.

## Models

SSDLite MobileDet, COCO, 320×320 (Google, Apache-2.0), in CPU and Edge TPU
builds. The models, the label list and the Edge TPU library are not in the app
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
| `PUT /config` | `{cameras: [{id, url, labels, threshold, fps?}]}` — held in memory only, since URLs carry go2rtc's password |
| `GET /snapshot/<id>.jpg` | the latest detection's frame, boxes drawn |

The app (`modules/vision.py`) long-polls `/status`, re-sends the camera list
whenever the sidecar's config hash isn't the one it was given (a restarted
sidecar has none), and clears every object signal if the sidecar goes away.

## API

| | |
|---|---|
| `PUT /api/cameras/{id}` | `detect: {enabled, labels, threshold, url?}` |
| `GET /api/cameras/{id}/detection` | latest detection frame (JPEG), `camera:read` |
| `GET /api/cameras/vision` | sidecar reachability, backend, per-camera health (admin) |

In the ZMM Manager (`:8001`; actions need the Manager token): `GET /vision`,
`POST /vision/enable`, `/vision/disable` `{remove?}`, `/vision/restart`,
`/vision/service` `{action}`.

## Not yet

Zones within a frame, clips and recordings, per-object counts, GPU and Hailo backends, and larger models.

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
`apex` modules an M.2 / Mini PCIe Coral needs. **Set up driver** on the
Detection hardware card asks for it; the host helper (installed by
`install_watcher.sh`) does the work as root.

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
