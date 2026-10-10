# Vision — object detection for cameras (in progress)

Only the hardware probe and the Coral driver setup exist so far. The plan: a sidecar that runs detection
on camera sub-streams and reports `person`, `car` and the like as signals on
the camera device, so rules, notifications and alarm zones use them like any
sensor.

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
