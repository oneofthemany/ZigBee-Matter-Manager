# Speaker Sync — synchronised multi-speaker casting without Google Home

Play the same audio on several Google Cast speakers in sync (echo-free),
using groups defined **in ZigBee Manager** instead of the Google Home app.
Currently a **proof-of-concept**: it plays a generated test signal so sync
quality can be measured and tuned; routing real audio (radio/Tidal) through
the same pipeline is the follow-up once the PoC validates on real hardware.

---

## Why this exists

Google only allows speaker groups to be created in the Google Home app; the
Cast APIs expose no way to create groups or to synchronise independent
playback sessions. Casting the same URL to two devices individually starts
hundreds of ms apart (session-dependent, not constant) and drifts a few ms per
minute because each speaker consumes audio on its own DAC clock. A static
per-speaker delay can't fix either problem at the standard API layer.

The way out is a **custom Web Receiver**: a Chromium page running on each
speaker in which we control the audio pipeline in JavaScript. That flips the
problem — the receiver can share a clock with the server and schedule audio
sample-accurately, the same architecture Snapcast uses.

---

## Architecture

```
                      ┌────────────────────────────────────────────┐
                      │ ZMM (modules/media/cast_sync.py)           │
                      │                                            │
   HTTPS :8000        │  CastSyncPoc                               │
   main app ──────────│   ├─ session control (routes)              │
   (UI + API)         │   ├─ chunk producer (numpy test signal)    │
                      │   └─ plain-HTTP listener :8010 (uvicorn)   │
                      │        ├─ GET /cast/sync_receiver.html     │
                      │        ├─ GET /health                      │
                      │        └─ WS  /ws  (clock + audio + stats) │
                      └───────────────┬────────────────────────────┘
                          launch via  │   ws:// (same-origin)
                          pychromecast│   ┌──────────┴──────────┐
                          custom msg  ▼   ▼                     ▼
                      ┌──────────────────────┐   ┌──────────────────────┐
                      │ Speaker A            │   │ Speaker B            │
                      │ sync_receiver.html   │   │ sync_receiver.html   │
                      │  clock sync + Web    │   │  clock sync + Web    │
                      │  Audio scheduling    │   │  Audio scheduling    │
                      └──────────────────────┘   └──────────────────────┘
```

### Why a second, plain-HTTP port

The receiver page needs a live WebSocket back to ZMM. The main app serves
self-signed HTTPS, which the Cast device's browser rejects; and a page hosted
on HTTPS (e.g. GitHub Pages, like the lyrics receiver) may not open an
insecure `ws://` socket (mixed content). Serving the receiver page **and** the
WebSocket from one plain-HTTP origin (`media.cast.sync.http_port`, default
8010) sidesteps both. Plain-HTTP receiver URLs are accepted for *unpublished*
(development) Cast apps on serial-registered devices.

### Clock sync

The server clock is `time.monotonic()`. Each receiver estimates its offset
NTP-style over the WebSocket: it sends `{type:"ping", t}` (its
`performance.now()`), the server replies `{type:"pong", t, s}` (its clock),
and the receiver computes `offset = (s + rtt/2) − t_arrival`. It pings in a
burst at startup then every 4 s, keeps a 12-sample window, and trusts the
median of the three lowest-RTT samples. On a quiet LAN this lands within a
few ms.

### Audio timeline

- 44.1 kHz stereo s16le, 0.5 s chunks (`CHUNK_SECONDS`).
- Chunk *i* must start playing at `epoch + LEAD(2 s) + i × 0.5 s` (server
  clock). Chunks are produced and fanned out `AHEAD(1.5 s)` before their play
  time; the last 6 are buffered so a late-joining speaker starts immediately.
- Wire format: 8-byte big-endian float64 `play_at` + raw PCM
  (`struct.pack(">d", play_at) + pcm`). ~706 kbps per speaker — trivial on a
  LAN, no codec needed (and old Cast devices lack WebCodecs).
- The PoC signal is a pure function of the absolute sample index (chord pad +
  a sharp 1 kHz click every 2 s), so every receiver renders a bit-identical
  timeline regardless of when it joined. The clicks make even ~10 ms of
  misalignment audible as a flam — that's the tuning "ruler".

### Receiver scheduling

For each chunk the receiver computes the local `performance.now()` target:

```
local_target = play_at − offset + trim_ms/1000 − outputLatency
when         = ctx.currentTime + (local_target − perf_now)
```

and schedules an `AudioBufferSource.start(when)`. Chunks arriving after their
play time are dropped and counted (`late`). The applied offset **slews** at
most 1 ms per chunk toward the latest estimate so chunk boundaries stay
sample-continuous while drift is corrected continuously; a jump > 50 ms snaps
and counts a `resync`. `AudioContext.outputLatency` (or `baseLatency`) is
compensated automatically when the device reports it.

### Per-speaker trim

What remains after clock sync is each device's fixed output-pipeline latency
(DAC/decoder), which — unlike Cast session buffering — is constant per
hardware model. The ±ms trim (positive = play later) covers it: tune once per
speaker by ear, it stays valid. Trims are stored **per device** (not per
group) in `data/cast_sync_trims.json` and pushed live over the WebSocket when
changed, so slider drags are audible immediately.

### Device launch flow

1. `CastPlayerProvider._get_cast()` connects to the device (pychromecast).
2. The sync receiver app (`media.cast.sync.app_id`) is launched via the same
   `_ensure_app` used by the lyrics receiver.
3. A `{type:"start", sid, trim_ms}` message goes out on the custom namespace
   `urn:x-cast:zmm.sync`, **re-sent every 2 s for up to 30 s** — the page may
   still be loading when the first message is sent, and CAF drops messages
   with no registered listener.
4. The receiver opens `ws://<page-origin>/ws`, sends `{type:"hello", sid}`,
   gets `hello_ack` (format + trim) plus any still-future buffered chunks, and
   starts playing. Stop = `quit_app` on every launched device.

The receiver sets `disableIdleTimeout: true` — no CAF media session is used,
so without it the platform would kill the app as "idle" after a few minutes.

---

## Configuration

```yaml
media:
  cast:
    sync:
      enabled: false     # bring up the :8010 listener at boot
      http_port: 8010    # plain-HTTP listener (receiver page + WS)
      app_id: ""         # Cast console App ID of the registered receiver
```

Requires `media.enabled` and `media.cast.enabled`. The container must expose
`http_port` on the LAN (host networking already does).

Data files: `data/cast_sync_trims.json` (player_id → trim ms),
`data/cast_sync_groups.json` (gid → {name, members}).

---

## UI

- **Settings → Speakers** — enablement (toggle / port / App ID) with its own
  Save & Restart, live status badge, and the one-time registration checklist
  with a copy-ready receiver URL. Saves only the `media.cast.sync` config
  slice (merged key-wise server-side, so it can't clobber other tabs).
- **Media → Group** — now two sub-tabs:
  - *WiiM multiroom*: the pre-existing native LinkPlay builder, unchanged.
  - *Speaker sync (beta)*: create/delete named groups of ≥ 2 Cast speakers,
    **Test** (starts the click-track session on all members), live per-member
    trim sliders, connection pills and stats (offset / RTT / late / resyncs),
    refreshed in place every 3 s so slider drags aren't disturbed.

## One-time Cast console registration

Requires a Google Cast developer account (one-time $5 registration fee).

1. Enable in Settings → Speakers, Save & Restart, confirm
   `http://<host>:8010/health` answers.
2. [Cast developer console](https://cast.google.com/publish) → *Add new
   application → Custom Receiver* → URL
   `http://<host>:8010/cast/sync_receiver.html`. **Don't publish.** Register
   each test speaker's serial number for development (as for the lyrics
   receiver) and reboot the speakers once.
3. Paste the App ID into Settings → Speakers, Save & Restart.

Full steps also in `static/cast/README.md`.

---

## API

| Endpoint | Method | Body / Returns |
|---|---|---|
| `/api/media/sync/status` | GET | `{running, configured, http_port, group_id, elapsed_s, devices:[{sid, player_id, name, connected, trim_ms, stats}]}` |
| `/api/media/sync/start` | POST | `{group_id}` or `{player_ids:[…]}` |
| `/api/media/sync/stop` | POST | — |
| `/api/media/sync/trim` | POST | `{player_id, trim_ms}` (±2000, live-pushed) |
| `/api/media/sync/groups` | GET | `{groups:[{id, name, members:[…], active}]}` |
| `/api/media/sync/groups` | POST | `{name, members:[…], id?}` (id = update) |
| `/api/media/sync/groups/delete` | POST | `{id}` |

Receiver stats fields: `offset_ms`, `rtt_ms`, `out_latency_ms`, `ctx_rate`,
`scheduled`, `late`, `resyncs`, `uptime_s`.

---

## Evaluating the PoC

Good result = clicks indistinguishable (≲ 10 ms) after trim tuning, stable
over 15+ minutes, `late` ≈ 0, few `resyncs`. That green-lights the real
feature: sync groups as ordinary play targets for radio/Tidal (server decodes
the source to PCM and feeds this same pipeline).

Failure modes to watch:

| Symptom | Likely cause |
|---|---|
| Speaker never leaves "launching…" | App ID wrong, serial not dev-registered (allow ~15 min + reboot), or :8010 unreachable from the speaker |
| Steady echo that trim fixes, but value changes per session | Clock sync unstable — check `rtt_ms`/offset jitter in stats (Wi-Fi congestion) |
| Rising `late` count | Producer starved or network stall; audio will gap |
| Audible ticks every few seconds | Offset slew fighting a noisy clock estimate — see `resyncs` |

## Known limitations (PoC scope)

- One global session; test signal only (no real sources yet).
- Cast devices only (WiiM has native multiroom; mixing ecosystems in one sync
  group would need this pipeline on WiiM too).
- Group volume isn't fanned out yet — use each speaker's own volume.
- The receiver must stay reachable at the registered URL: changing
  `http_port` or the host IP means re-registering (or updating) the Cast
  console entry.

## Source map

| File | Role |
|---|---|
| `modules/media/cast_sync.py` | Service: HTTP listener, producer, WS protocol, launch, groups/trims |
| `static/cast/sync_receiver.html` | CAF receiver: clock sync + Web Audio scheduling |
| `routes/cast_sync_routes.py` | REST endpoints (main app) |
| `static/js/speaker-sync.js` | Settings → Speakers tab |
| `static/js/media.js` | Group-builder sub-tabs (WiiM / Speaker sync) |
| `routes/config_routes.py` | `media.cast.sync` slice merge on save |
