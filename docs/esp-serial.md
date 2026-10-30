# Speakers on USB serial

**Settings → APIs → Speakers** (add it under *Add API*). Local control and
recovery for ESP32 speakers such as the Home Assistant Voice PE, over USB:
read the log, set Wi-Fi, read device facts, back up, flash and restore.

| File | |
|---|---|
| `modules/esp_serial.py` | ports, cooldown ledger, raw serial, log, Improv, images and backups, jobs |
| `modules/esp_esptool.py` | esptool sessions; parsers for esptool, partition tables and boot logs |
| `routes/esp_serial_routes.py` | `/api/esp-serial/*`, admin |
| `static/js/esp-serial-settings.js` | the sub-tabbed panel; Web Serial for a speaker on this computer |
| `data/esp_serial/` | `restarts.json`, `backups/`, `images/` |
| `tests/esp_serial/` | a pty plays the speaker; esptool is a recording fake |

## Ports and the Zigbee radio

Only USB serial ports whose USB vendor is Espressif (`303a`) are offered, and
never the one `zigbee.port` resolves to — checked again on every request, so
a port named by hand is refused just the same. The container gets every
`/dev/ttyACM*` and `/dev/ttyUSB*` present at start, the radio among them; a
speaker plugged in later needs a container restart to appear.

## Resets

On Espressif's USB-Serial/JTAG the modem lines are the reset wires:

- RTS high with DTR low resets the chip;
- an RTS pulse with DTR high restarts it into the ROM download mode, where
  it waits, silent, instead of running its firmware.

So reading (log, Improv, the boot capture) opens the tty with `os.open`, raw,
without echo and with HUPCL off, and never sets DTR or RTS. pyserial's open
with `dtr=False, rts=False` walks the lines through a reset — don't.

An esptool session connects once (`--after no-reset-stub`), runs its steps
in that stub (`--before no-reset-no-sync`), and leaves through a fresh
`--before default-reset --after hard-reset` connection, whose reset sequence
lowers both lines first and so boots from flash. A hard reset straight from
the kept stub leaves the chip in the stub. A failed step still runs the exit.

## Cooldown and safe mode

ESPHome counts boots that end before 60 s of uptime and starts in safe mode —
no voice or audio, waiting for an update — after 10. One session is one
counted restart; ZMM allows one per speaker per 120 s, persisted by USB
serial (on these chips, the MAC), and records it before the reset since a
failure can reset too. Restarting requests need `acknowledge_restart: true`
(the panel's checkbox); `override_cooldown` is for a speaker already failing.

The port is opened before the final reset and the boot is read for 6 s:
`boot_attempts` (ESPHome's count), `safe_mode_active`, and `booted_from_flash`
from the ROM banner. `false` there means the speaker is in download mode:
unplug and replug it.

## Wi-Fi

Improv over serial (`improv_serial:` in the firmware): the current state and
device info, and sending SSID and password. The password goes to the speaker
only; ZMM neither logs nor stores it.

## Backup, flash, restore

A backup reads the whole detected flash (esptool's stub checks each block
against an MD5 the chip computes) into `data/esp_serial/backups/` with its
SHA-256. It holds the speaker's saved settings, Wi-Fi password included.

Flashing writes a factory or full-flash image (first byte `0xE9`, the
bootloader header) at offset 0, or a backup to restore. It is refused unless
that speaker has a backup, and needs the last six characters of its MAC typed
to confirm. App-only update images belong to the speaker's own update.

## On this computer

With the speaker plugged into the PC running the browser (Chrome or Edge,
over HTTPS), Web Serial reads the log and does Improv. Info, backup and
flashing need it on the hub.
