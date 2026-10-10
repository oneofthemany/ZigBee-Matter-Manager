# Cameras

Live view, snapshots and motion from RTSP/ONVIF cameras, in the **Cameras**
tab and the device list. ZMM does not stream video itself: **go2rtc** runs
beside it as a sidecar and turns RTSP into something a browser can play, and
ZMM proxies that behind its own login. A camera is never reachable from a
browser directly, and nor is go2rtc.

```
camera ──RTSP──▶ go2rtc (sidecar, API only) ◀──HTTP/WS, Basic auth── ZMM ◀──wss, session── browser
   └──ONVIF events (motion)─────────────────────────────────────────▶ ZMM ─▶ rules / alerts / alarm
```

| Path | Role |
|---|---|
| `modules/cameras.py` | registry, credentials, keeping go2rtc in step, snapshots, motion watchers |
| `modules/go2rtc.py` | go2rtc's config and API client |
| `manager/go2rtc.py` | the sidecar's lifecycle, in the ZMM Manager |
| `scripts/sidecar_service.sh go2rtc` | its boot-time service, on the host |
| `modules/onvif.py` | WS-Discovery, profiles and stream URIs, pull-point motion events |
| `routes/camera_routes.py` | API and the stream websocket |
| `static/js/cameras-page.js`, `static/js/camera-player.js` | the tab, the player |
| `data/cameras.json` | cameras — no credentials |
| `config/secrets.yaml` → `cameras`, `go2rtc` | camera credentials; go2rtc's API credentials |
| `data/go2rtc/go2rtc.yaml` | go2rtc's config, written by ZMM (0600) |

## Setting up

1. **ZMM Manager → Services → go2rtc → Enable** (the Cameras tab's Manage
   panel links there). Like Beekeeper and Ollama, go2rtc is an external
   container, so the Manager owns it: it pulls `docker.io/alexxit/go2rtc:1.9.14`
   and starts `zigbee-matter-manager-go2rtc` on host networking, mounting only
   ZMM's `data/go2rtc` folder, where ZMM has already written the config.
2. **Add a camera**: either **Find ONVIF cameras**, pick one, enter its
   username and password and **Read streams** (ZMM asks the camera for its RTSP
   URLs), or paste the RTSP URL. A `user:pass@` in a pasted URL is lifted into
   the credential fields and never stored in the URL.
3. Tick **Use the camera's ONVIF motion events** for motion (needs the ONVIF
   host; the probe says if the camera has no events service).

Discovery is multicast, so it only finds cameras on the hub's own network, and
must be tried from the hub — a laptop on Wi-Fi often misses the replies.

## go2rtc

ZMM writes go2rtc's whole config:

- **Only the API is on**, with a username and a random password ZMM generates
  into `secrets.yaml`, and `local_auth: true` so that applies from localhost
  too (go2rtc otherwise skips auth for local requests).
- **Its RTSP, WebRTC and HomeKit servers are off.** Left on, the RTSP server
  re-publishes every camera on `:8554` with no password.
- **Streams are not in the file.** ZMM puts each camera in through the API
  (as `zmm_<id>`, credentials joined into the URL only there) and, every
  minute, puts back any go2rtc has lost to a restart and removes ones it no
  longer has. Streams not named `zmm_*` are left alone.
- **Only `rtsp`, `rtsps`, `http` and `https` sources are accepted.** go2rtc
  also understands `exec:` and `ffmpeg:` sources, which run commands.

**Where it listens.** Under podman, ZMM runs in a host-network pod, so go2rtc
listens on `127.0.0.1:1984` and nothing else on the LAN can reach it. Under
docker ZMM is not on the host network, so the installer sets go2rtc to `:1984`
and ZMM to reach it at `host.docker.internal:1984`; the API password then
crosses the LAN in Basic auth on any request to it, so firewall `1984` to the
host. Both are editable under **Manage → Address**, which rewrites the config
and tells go2rtc to reload it (its own `/api/restart`).

**After a reboot or outage.** Enabling also asks the host for a boot-time
service — a systemd unit (`zmm-go2rtc.service`) or OpenRC script that runs the
container and restarts it if it stops — written by
`scripts/sidecar_service.sh go2rtc`, the same helper Beekeeper uses, via the
`zmm-go2rtc-service.path` trigger (or the polling watcher on hosts without
systemd). Disabling removes the service before stopping the container, so the
service can't bring it back. The card shows whether autostart is on. On a host
with neither systemd nor OpenRC, the Manager's watchdog starts an enabled
go2rtc that has stopped, and it moves go2rtc to a newly pinned image after a ZMM
upgrade.

### Running go2rtc yourself

If go2rtc already runs elsewhere (Frigate ships one), run it with a config like ZMM's — API only, credentials, `local_auth: true` — and
enter its address and the API username/password under **Manage → Address**.
ZMM only needs the API.

## Live view

The player opens `wss://<hub>/api/cameras/<id>/stream`; ZMM checks the session
and `camera:read`, then relays to go2rtc's `/api/ws`. The browser asks for MSE
with the codecs it can play (`{"type":"mse","value":"avc1…,hvc1…,mp4a…"}`),
go2rtc answers with the MIME type and then sends fMP4 fragments, which go into
a `MediaSource`. iPhones use `ManagedMediaSource` (iOS 17.1+). It works through
the tunnel, since it is an ordinary websocket.

Where that can't work — no MSE, a codec the browser can't play (H.265 on many
browsers), the stream failing, or no reply within 8 s — the tile falls back to a
snapshot every few seconds and says so. Snapshots are cached for 2 s on the hub
so a grid of viewers doesn't multiply requests to the camera, and the grid only
streams tiles that are on screen.

Video is not transcoded, so it costs the hub little CPU; WebRTC is not used.

## ONVIF

`modules/onvif.py` is a small hand-written client (no zeep): WS-Discovery,
`GetCapabilities`, `GetProfiles`, `GetStreamUri`, and the pull-point event
service. Two details that matter with real cameras:

- **Clock skew.** Requests are signed with a WS-UsernameToken digest including
  a timestamp, and cameras reject one more than a few seconds out. ZMM reads
  the camera's clock first (that call needs no login) and signs in its time.
- **Advertised addresses.** Cameras often advertise service URLs on an address
  ZMM can't reach (another interface, a NAT). ZMM keeps their paths but always
  talks to the host and port the camera was added with.

Replies are size-capped and any declaring a DTD is refused.

### Motion

With motion on, ZMM keeps a pull-point subscription open, renewing it every
minute and resubscribing with backoff when the camera drops it. Motion topics
differ by vendor (`RuleEngine/CellMotionDetector/Motion`,
`VideoSource/MotionAlarm`…) and so does the flag's name (`IsMotion`, `State`);
all are read. A camera that only ever reports *motion on* is cleared after two
quiet minutes.

The camera is a device, `camera::<id>`, reporting `motion` and `occupancy`
(the key motion sensors use), so it works anywhere a motion sensor does: rule
triggers and conditions, notification rules (*Motion detected*), and alarm
zones (Settings → Security → Alarm).

## Who can see

| | Needs |
|---|---|
| Watch, snapshots, list | `camera:read` |
| Add, edit, remove, discover, go2rtc | admin |
| A camera's name and motion in the device list | `device:read` (no video) |

`camera:read` is not part of `device:*` (see [auth.md](auth.md)). The default
`users` group has it; `viewers` don't. The stream websocket checks it itself,
since HTTP middleware doesn't see websockets: the session cookie (or a token),
LAN-only accounts staying on the LAN, and a handshake the browser marks
cross-site is refused — the same `Sec-Fetch-Site` rule as other cookie
requests, so it keeps working through the tunnel, where Origin and Host differ.

## Not yet

Recording and event clips, snapshots attached to alerts, a Frames card, PTZ,
WebRTC, Frigate's person/vehicle events, and IAS-style camera sirens.

## API

| | |
|---|---|
| `GET /api/cameras` | cameras (no credentials), online, motion; go2rtc error if any |
| `GET /api/cameras/{id}/snapshot` | JPEG, `no-store` |
| `WS /api/cameras/{id}/stream` | MSE stream, proxied |
| `POST /api/cameras` | add `{name, url, username?, password?, onvif?: {host, port}, motion?, enabled?}` |
| `PUT /api/cameras/{id}` | edit; a blank password keeps the stored one, `clear_credentials` removes them |
| `DELETE /api/cameras/{id}` | remove (and its credentials and stream) |
| `POST /api/cameras/discover` | ONVIF cameras on the hub's network |
| `POST /api/cameras/probe` | `{host, port, username, password}` → profiles with RTSP URLs, events support |
| `GET /api/cameras/go2rtc` | go2rtc address and health |
| `PUT /api/cameras/go2rtc` | `{url, listen, username?, password?}`; rewrites the config, has go2rtc reload it |

In the ZMM Manager (`:8001`; actions need the Manager token):

| | |
|---|---|
| `GET /go2rtc` | container, image, enabled, boot-time service, job log |
| `POST /go2rtc/enable` | pull, create and start (or start); requests the boot-time service |
| `POST /go2rtc/disable` | `{remove?}`; removes the service, then stops |
| `POST /go2rtc/restart` | |
| `POST /go2rtc/service` | `{action: install|remove|check}` |
