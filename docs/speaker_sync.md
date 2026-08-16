# Speaker Sync — synchronised multi-speaker casting without Google Home

Play the same audio on several Google Cast speakers in sync (echo-free),
using groups defined **in ZigBee Manager** instead of the Google Home app.
Sessions play either real media — anything ffmpeg can open, optionally through
the server-side EQ — or a generated test signal, which is what the Sync Lab
uses to measure and tune alignment. Both travel the same pipeline; the only
difference is where the samples come from.

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
- The timeline is addressed by absolute sample index, so what a receiver
  renders depends only on *where* it reads, never on when it joined.
- The test signal is a pure function of that index (chord pad + a sharp 1 kHz
  click every 2 s) and is therefore seekable anywhere, in either direction,
  with no buffering. The clicks make even ~10 ms of misalignment audible as a
  flam — that's the tuning "ruler".
- Real media comes from an ffmpeg decoder writing into a ring buffer
  (`sync_source.py`). It is seekable only within what the ring holds, and it
  cannot be served ahead of its own arrival — so a media session runs the whole
  group `source_delay_s` (default 2 s) behind the live edge, and the ring is
  sized for the *spread* of device read positions rather than for the delay
  alone. See open-zone.md §4.1 for the sizing argument; get either wrong and
  the symptom is silence or per-block underruns, not subtle drift.
- EQ is applied once, server-side, before fan-out — so every speaker in the
  group gets identical equalised samples. Settings are keyed
  `syncgroup:<group_id>` in `data/media_eq.json`.

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
hardware model. The reported-position sensor cannot see any of it (§6.1): the
loop can align two devices' reported positions to ~10 ms and a screened device
will still be a fifth of a second late in the air. The ±ms trim (positive =
play later) covers it: tune once by ear, it stays valid.

Because that latency is a property of the **hardware, not the unit**, a trim
set on one device is also recorded against its model — `cast_type/model_name`,
e.g. `cast/Nest Hub` — in `data/cast_sync_model_trims.json`. Any other device
of that model with no trim of its own inherits it, so adding a second display
to a group does not mean measuring the same 200-odd milliseconds again. An
explicit per-device trim in `data/cast_sync_trims.json` always wins, and trims
are pushed live over the WebSocket when changed so slider drags are audible
immediately.

That inheritance rests on the trim measuring the hardware. Two units of one
model trimmed more than `TRIM_MODEL_AGREE_MS` (25 ms) apart disprove it — in
that house the trim is also absorbing placement or distance — so the model
default is **dropped** rather than overwritten, and untrimmed siblings go back
to where the loop puts them. No default beats a wrong one applied to a speaker
nobody touched.

A trim only changes a device's timing through `set_trim()`, which moves the
served timeline and the value the measurement subtracts in the same breath,
and only for that player. Everything else — including a model default learned
while a session is running — lands at the next session start, because each
stream latches the trim it was built with (`_Stream.trim_ms`). Re-reading the
effective trim per poll meant a slider drag on one speaker could shift the
subtracted term on a *different* speaker of the same model with no matching
move of its timeline; the monitor read the difference as error and hard-
resynced audio that was already aligned.

The mic calibrator sets both automatically when it can hear the speakers; by
ear is the documented fallback (§10.4) and feeds the same model learning.

### Queues, expiring URLs, and what the displays show

A zone plays one server timeline, so "a playlist" cannot mean what it means on
a single speaker (a queue the *device* walks). It is the **decoder** that
walks it, and the seam between two tracks is the only place a zone can change
what it is playing without disturbing anyone's alignment.

`MediaSource` therefore asks its URL provider for a URL before every decoder
start, and hands it **why the last decode ended** — that one value is the
whole mechanism:

| `last_rc` | Means | Provider does |
|---|---|---|
| `None` | first start, or the decoder raised | resolve the current item |
| `0` | the item played out cleanly | advance; `""` at the end of the set |
| non-zero | ffmpeg failed — usually an expired signature | re-resolve the **same** item |

Returning `""` after a clean end sets `finished`, the supervisor stops instead
of restarting, and `_source_spent()` waits for the tail already in the delay
line and the devices' own buffers to be *heard* before the session is torn
down. Without that wait, teardown would cut the end off the last track.

The policy lives in the provider `cast_sync` builds, not in the source: a
zone has no queue behind it unless one was expanded for it, so a bare track
that ends cleanly ends the session rather than silently repeating.

**Artwork** rides on the Cast LOAD (`metadataType 3`, `images`, `thumb`), the
same shape a single Cast player gets, so screened members show album art or a
station logo instead of a bare title. It is set once, at launch: changing it
later means re-loading, which restarts the device's buffering and would cost
the zone its alignment. A queue therefore shows the *set* on the speakers,
while `now_playing` in the status payload tracks the individual item for the
app, where it is free.

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
      resampler: rust    # rust | sinc | linear (open-zone.md §4.2)
      # (restart resume lives on the media root, not here:
      #  media.resume_after_restart / media.resume_max_age_s)
      source_delay_s: 2.0    # how far behind the live edge a media session runs
      ring_capacity_s: 20.0  # delay-line depth (§4.1) — size for the spread
```

Requires `media.enabled` and `media.cast.enabled`. The container must expose
`http_port` on the LAN (host networking already does).

`resampler` defaults to `rust`: a stateless windowed-sinc fractional delay in
the `zmm_eq` wheel. Where that wheel is absent the engine falls back to `sinc`
— the identical filter in numpy, ~4x the CPU — automatically, and says so in
the log. `linear` exists only to reproduce earlier measurements and should not
be used on real music.

The `soxr` backend (libsoxr variable-rate via `ctypes`) was removed in
v31.04.07.2026 after it aborted the process with `double free or corruption`;
the name is still accepted here as an alias for `rust`. See open-zone.md §4.2.

`source_delay_s` must stay above `STREAM_AHEAD_S` (1.2 s) — the per-device
serve-ahead is cut from it. Raising `ring_capacity_s` costs ~350 kB per second
and is the right response to underruns on a group whose devices have widely
different startup latencies.

Data files: `data/cast_sync_trims.json` (player_id → trim ms),
`data/cast_sync_groups.json` (gid → {name, members}),
`data/cast_sync_model.json` (learned per-device lag + drift).

---

## UI

- **Settings → Audio** — enablement (toggle / port / App ID) with its own
  Save & Restart, live status badge, and the one-time registration checklist
  with a copy-ready receiver URL. Saves only the `media.cast.sync` config
  slice (merged key-wise server-side, so it can't clobber other tabs).
- **Media → Group** — now two sub-tabs:
  - *WiiM multiroom*: the pre-existing native LinkPlay builder, unchanged.
  - *OpenZone (beta)*: itself split in two — **Zones** (build and run) and
    **Results** (the Sync Lab), because a page of charts under every control
    made both halves hard to read. Zones holds create/delete for named groups
    of ≥ 2 Cast speakers, a **source picker** (sync test signal, favourite
    stations, recently played, the Tidal library, custom URL), session length,
    **Test/Play**, live per-member trim sliders, connection pills and stats
    (offset / RTT / late / resyncs), refreshed in place every 3 s so slider
    drags aren't disturbed.

    Picking **Tidal library…** reveals a second row: which slice (Mixes,
    Playlists, Albums, Artists) and which item. A container becomes the
    session's queue — the engine plays it in order, re-resolving each track's
    signed URL as it reaches it, and stops at the end unless *Repeat* is set.
    Lists load on demand and are cached, so the picker stays one line tall
    next to Play instead of unrolling a library into a select.

    The source choice is remembered per group in `localStorage` — a group tends
    to be "the kitchen radio", so it should survive a reload the way the test
    duration does:

    | Key | Holds |
    |---|---|
    | `zmm.syncsrc.<gid>` | `""` (test signal), `fav:<uuid>`, `url:<url>`, `tidal:<track id>`, `tc:<kind>[:<id>]` (Tidal container, id absent while still being picked) or `custom` |
    | `zmm.openzone.tab` | `zones` or `results` — which sub-tab to open on |
    | `zmm.syncurl.<gid>` | the custom URL text, kept separately so switching away to a favourite and back doesn't lose it |
    | `zmm.syncloop.<gid>` | `1` when the custom source should repeat |

    Favourites are stored by id and resolved server-side at start, so a station
    whose stream URL has moved since it was favourited still plays. **Custom
    URL** takes anything ffmpeg can open *from the server* — a stream, or a
    file path inside the container — with a **Loop** box for finite sources
    (no effect on a live stream, which never ends). Start is disabled until a
    URL is entered rather than quietly falling back to the test signal.

    **Tidal** travels by source id, never by URL: its streams are signed for
    minutes, so a stored URL would be dead before it was next used. The engine
    resolves an id at session start — failing loudly on a bad id or a
    signed-out account rather than falling back to the test tone — and
    re-resolves every time the decoder restarts, which is what lets a session
    outlive the token expiring underneath it. Expiry then costs the same as a
    station dropping: a gap, then it continues. Server-side decoding means the
    plain AAC form is requested, not the Cast-only DASH manifest.

    While a session runs the card shows what is playing and the running
    underrun count.

    Note that a custom URL is opened by ffmpeg *on the server*, so it reaches
    whatever the server can reach. This is the same capability the ordinary
    `/api/media/play` `url` field has always had, not a new one — but it is
    worth knowing before exposing the UI to anyone you would not give config
    access to.

## One-time Cast console registration

Requires a Google Cast developer account (one-time $5 registration fee).

1. Enable in Settings → Audio, Save & Restart, confirm
   `http://<host>:8010/health` answers.
2. [Cast developer console](https://cast.google.com/publish) → *Add new
   application → Custom Receiver* → URL
   `http://<host>:8010/cast/sync_receiver.html`. **Don't publish.** Register
   each test speaker's serial number for development (as for the lyrics
   receiver) and reboot the speakers once.
3. Paste the App ID into Settings → Audio, Save & Restart.

Full steps also in `static/cast/README.md`.

---

## API

| Endpoint | Method | Body / Returns |
|---|---|---|
| `/api/media/sync/status` | GET | `{running, configured, http_port, group_id, elapsed_s, now_playing:{title, artist, artwork_url, index, count}, source:{kind, buffered_s, underruns, restarts, finished, …}, resampler:{kind, backend, rust, kinds}, devices:[{sid, player_id, name, connected, trim_ms, stats}]}` |
| `/api/media/sync/start` | POST | `{group_id}` or `{player_ids:[…]}`; optional `{media:{url \| station_uuid \| source_id+media_type, kind?, title?, artist?, artwork_url?, loop?}}` — omit `media` for the test signal. `station_uuid` is resolved through the radio directory at start (and carries its logo through as `artwork_url`); `kind` is `track` (default) or `album\|playlist\|artist\|mix`, which expands into the session queue; a `media` block that resolves to no URL, or a URL starting with `-`, is rejected rather than passed to the decoder |
| `/api/media/sync/stop` | POST | — |
| `/api/media/sync/trim` | POST | `{player_id, trim_ms}` (±2000, live-pushed) |
| `/api/media/sync/groups` | GET | `{groups:[{id, name, members:[…], active, play:{…}}]}` |
| `/api/media/sync/groups` | POST | `{name, members:[…], id?}` (id = update; the zone's `play` block is preserved) |
| `/api/media/sync/groups/config` | POST | `{id, key?, custom_url?, loop?, media?, duration_s?, crossfade_s?}` — what the zone plays when started without a source. Replaces the block wholesale |
| `/api/media/sync/groups/delete` | POST | `{id}` |

`/api/media/sync/start` also takes `use_saved: true` (with `group_id`), which
fills `media`, `duration_s` and `crossfade_s` from the zone's own config.

---

## What a zone plays, and who can start it

A zone stores its source alongside its members, in the same
`data/cast_sync_groups.json` record:

```json
"a1b2c3d4": {
  "name": "Downstairs",
  "members": ["cast:…", "cast:…"],
  "play": {
    "key": "tc:playlists:12345",     // the Media page's picker state
    "custom_url": "", "loop": false, //   …kept so any browser opens the same
    "media": {"source_id": "12345", "media_type": "tidal", "kind": "playlist"},
    "duration_s": 300, "crossfade_s": 0.4
  }
}
```

Only `media`, `duration_s` and `crossfade_s` are read by the start path;
`key`, `custom_url` and `loop` are the picker's memory. `null` for either
timing field means "never chosen", so the server default stands — distinct
from a deliberate `0`, which means *until stopped* / *no crossfade*.

This is what makes a zone startable by something other than the tab that built
it. `MediaService.start_zone()` is the single entry point — the Media page, an
automation rule and a post-restart resume all go through it, so a station id
resolves the same way and a Tidal container expands the same way for all three.
Sources are stored as **ids, not URLs**: a directory stream URL moves, and a
Tidal URL is signed for minutes.

### In automations

A media step can target `zone:<group_id>` instead of a player id. The step
editor lists zones under an *OpenZone* group in the player picker.

| Action | On a zone |
|---|---|
| `play_zone` | Start the zone's saved source and window. Zone targets only |
| `play_radio` / `play_tidal` | Override the source for this run; the zone's window and crossfade still apply |
| `announce` | Spoken **through** the zone as ordinary finite media — one timeline, one voice, and the session ends itself when the clip has been heard |
| `control` | `stop`, `pause`, `resume`, `next`, `prev` — a zone is an ordinary player (below) |
| `volume`, `volume_adjust`, `volume_fade` | Fanned out to every member — volume is a property of each speaker, not of the timeline |

Tidal's `Radio∞` mode is not offered for a zone: auto-extension belongs to the
controller's queue, and a zone walks a fixed list. Asking for it returns an
error rather than quietly playing the finite seed list.

### A zone is a player

A zone is addressed as `zone:<group_id>` wherever a player id is taken — the
Media page's speaker list, `/api/media/tidal/play`, `/api/media/control`,
`/api/media/queue/*`, the EQ endpoints — and not only in automations. That is
what gives a zone the queue, the lyrics overlay, the favourite button and the
artist actions that the single-speaker path already had: they are written
against the player interface, and a zone now satisfies it
(`modules/media/players/zone.py`; the architecture is open-zone.md §4.1b).

Four differences from a speaker are worth knowing, all of them consequences of
a zone being one server-built timeline rather than a device:

- **Next/prev are heard at arm's length.** The cut is made at the point the
  decoder has reached, and everything already decoded and sent plays first, so
  a skip lands a group latency later — seconds, not milliseconds. Nothing in
  the queue layer can shorten that.
- **Pause stops the timeline.** There is no per-device transport to hold.
  Resume re-issues the same queue at the same item, from that item's start.
- **Shuffle is fixed at the moment you press play.** The order is handed to
  the engine up front; toggling shuffle mid-session takes effect the next time
  playback starts. Repeat-all works (it becomes the engine's loop);
  repeat-one has no equivalent.
- **EQ is the zone's own chain**, keyed `syncgroup:<gid>`, not the per-speaker
  proxy. Turning it on or off rebuilds the session; moving a gain does not.

Receiver stats fields: `offset_ms`, `rtt_ms`, `out_latency_ms`, `ctx_rate`,
`scheduled`, `late`, `resyncs`, `uptime_s`.

---

## Evaluating alignment

Good result = clicks indistinguishable (≲ 10 ms) after trim tuning, stable
over 15+ minutes, `late` ≈ 0, few `resyncs`. Tune on the test signal — the
clicks are the ruler — then switch the group to real media, which travels the
same pipeline from the same timeline.

Failure modes to watch:

| Symptom | Likely cause |
|---|---|
| Speaker never leaves "launching…" | App ID wrong, serial not dev-registered (allow ~15 min + reboot), or :8010 unreachable from the speaker |
| Steady echo that trim fixes, but value changes per session | Clock sync unstable — check `rtt_ms`/offset jitter in stats (Wi-Fi congestion) |
| Rising `late` count | Producer starved or network stall; audio will gap |
| Audible ticks every few seconds | Offset slew fighting a noisy clock estimate — see `resyncs` |
| One speaker consistently late by a fixed amount, and the stats say it is aligned | Output-pipeline latency, which the reported-position sensor cannot see (§6.1). Screened devices are typically worst. Fix with trim — by mic if you have one in the room, otherwise by ear. It is stored per device and re-applied every session |
| Session opens with a few seconds of silence | Delay line primed short — check the "primed only Xs" warning and `source.underruns`; raise `source_delay_s` or `ring_capacity_s` |
| Silence on one member only, others fine | That device's pre-compensation was clamped at launch — see the "clamped … forward" warning |
| `source.restarts` climbing | Station keeps dropping; each restart is a gap, not a permanent offset |

## Surviving a restart

A sync session cannot survive a container restart on its own. Every member is
pulling PCM from a listener inside the process, so when the process goes the
audio goes with it and the devices are left holding a URL that no longer
answers. Nothing can keep that TCP stream alive — the bytes were being produced
by something that no longer exists.

What happens instead is a relaunch. While a session runs, enough to stand it
back up is written into `data/media_sessions.json` under `sync` — group id,
members, the media, and the remaining window. On boot, after device adoption,
the session is started again with the window reduced by however long the outage
lasted; if that leaves nothing, it is not resumed.

The same applies to an ordinary Cast player with EQ on, which is also fetching
its audio from us. That one is recorded under `playback`, and is re-issued only
if the speaker is **not** already playing — a player with EQ *off* is fetching
its source directly, never noticed the restart, and must not be interrupted.

Both are gated on `media.resume_max_age_s` (default 600). Waking the house
because the box was down since midnight is a worse failure than a stream that
stayed stopped. Set `media.resume_after_restart: false` to disable entirely.

**Boot order dominates the gap, not the resume.** The audio listeners are
therefore started at the very top of the lifespan, ahead of everything else.
Measured on the reference deployment before that change, `:8011` came up 67 s
after container start:

| | |
|---|---|
| ~11 s | process start → interpreter up, imports done, loop monitor running |
| ~35 s | telemetry DB auto-rebuild check + `warm()` (DuckDB open/migration) |
| ~20 s | MQTT, Zigbee network, Matter server, bridge, monitors |
| **67 s** | **device-audio listener binds** |

Media depends on none of it — `MediaService` is constructed at import, and
`start()` only binds the two listeners and spawns its own poll loop. Starting
it immediately after the loop monitor puts the listeners up at roughly the
11 s mark instead, which is inside the window a Cast device will still retry a
dropped stream. `start()` is idempotent, so the original call later in
bring-up remains as a safety net for the case where the early one raised.

The relaunch machinery above stays regardless: it is what covers a device that
gave up anyway, or a sync session whose members must be re-launched.

## Known limitations

- One session at a time — starting a second stops the first.
- Cast devices only (WiiM has native multiroom; mixing ecosystems in one sync
  group would need this pipeline on WiiM too).
- Group volume isn't fanned out in the UI — use each speaker's own volume.
  (An automation targeting a zone does fan volume out across its members.)
- A zone has no pause/resume/next/prev: the timeline is built server-side and
  every device is chasing it, so there is nothing local to pause.
- The receiver must stay reachable at the registered URL: changing
  `http_port` or the host IP means re-registering (or updating) the Cast
  console entry.

## Source map

| File | Role |
|---|---|
| `modules/media/cast_sync.py` | Service: HTTP listener, producer, WS protocol, launch, groups/trims |
| `modules/media/sync_source.py` | Master timeline: generated signal, or ffmpeg + EQ into the ring-buffer delay line |
| `modules/media/sync_resample.py` | Variable-ratio resampler backends (Rust windowed sinc, numpy windowed sinc, linear) |
| `static/cast/sync_receiver.html` | CAF receiver: clock sync + Web Audio scheduling |
| `routes/cast_sync_routes.py` | REST endpoints (main app) |
| `static/js/speaker-sync.js` | Settings → Audio tab |
| `static/js/media.js` | Group-builder sub-tabs (WiiM / Speaker sync), zone source picker |
| `modules/media/service.py` | `start_zone()` — the one way a zone is started |
| `modules/automation.py` | `_media_zone()` — media steps targeting `zone:<id>` |
| `static/js/modal/automation.js` | Zone targets and the Play Zone action in the step editor |
| `routes/config_routes.py` | `media.cast.sync` slice merge on save |

## Sync Lab (frontend)

`static/js/sync-lab.js` renders per-session analysis of speaker-sync tests into
`#syncLabHost` (Media → Group → OpenZone → Results), reading the group's own
DuckDB via `/api/media/sync/{sessions,session,model,trend}`.

What it shows:

- three group headline stats, counted after the group locked;
- **one** per-speaker table carrying the session's measurements and the
  corrections applied to it side by side, plus the cross-session learned model.
  This replaced a grid of cards and a separate data table, because the question
  here is always "how do these two compare?";
- a collapsed ledger of when each correction happened;
- the **group spread chart** (the headline): how far apart the speakers are,
  against the ±20 ms "audibly together" band;
- the **convergence chart**: per-speaker playback error vs elapsed time, with
  the ±30 ms slew window, ±100 ms jump threshold, and hard-resync (◆),
  rate-slew (▽) and manual-trim (▲) events;
- the **PLL chart**: per-speaker stream rate correction (ppm) locking onto the
  device's true clock offset.

Colours are fixed per speaker by group-member order, so the colour follows the
entity. The palette is validated for CVD and both themes.

### Live mode and the rendering layer

While the group's session is running the view refreshes every 3 s, and the whole
rendering layer exists to make that invisible. **A live view that reflows under
the reader is worse than one that updates slowly.**

- `_setHtml` writes `innerHTML` only when it actually changed, and returns
  whether the DOM was touched so callers can rebind handlers. It is the blunt
  instrument — it destroys and rebuilds. Keep any scrolling container *out* of
  the replaced HTML so the browser preserves its position.
- `_patch` is the one to reach for on anything a reader is looking at. A 3 s
  tick that rewrites a whole panel costs the reader everything they were doing:
  selection and hover die, and any change in wrapped-line count reflows the page
  under the cursor, making the charts below visibly jump. So structure is built
  once per `key` (the set of speakers, say) and every subsequent tick writes
  only the leaf `[data-v]` nodes whose value actually moved. Values are strings,
  or `{text, cls}` when the node also carries a state colour.

### Payload validation

Emptiness is not the only way a payload can be useless. A series row always
carries the speaker it belongs to; one that does not cannot be plotted or
attributed, and a handful of them render as blank tiles and an empty table —
visually identical to no data, but passing every length test on the way in. The
structural check refuses them at the boundary and says so out loud, rather than
leaving a silent blank to be explained later.

### No guidance panel — by decision

The lab reports; it does not advise.

There was a "What to do next" panel, and it was removed. The sync engine
corrects itself, so on a healthy group every row it could write resolved to
"nothing to do", and the two commonest rows were telemetry wearing advice's
clothes: a settled bias is the rate loop's job, it is already draining it, and
the number is a column in the table below. Cutting it to exceptions only did not
save it either. The charts and the per-speaker table say what happened — a
reader can see four hard resyncs in the resync column without a paragraph
telling them to check their WiFi.

**The related decision, should anyone be tempted: no per-speaker "apply this
trim" button either.** The settled bias it would be computed from is measured
with the trim *excluded* (`cast_sync._measure_lag_once`), so a trim can never
move that number. The suggestion would survive being applied, invite a second
application, and integrate open-loop. A sensor-*visible* bias belongs to the
rate loop, which is already draining it; a sensor-*invisible* one
(output-pipeline latency) can only be seen by the mic, which is what Calibrate
is for.

## Server-side EQ for Cast targets

Cast receivers expose no DSP API, so equalisation has to happen before the audio
reaches the device. When EQ is enabled for a Cast player, `EqStreamEngine`
(`modules/media/eq_stream.py`) takes over the playback path instead of handing
the device the source URL: ffmpeg decodes the source — radio stream, Tidal AAC,
the therapy WAV, anything it can read — to raw PCM, the `zmm_eq` Rust biquad
chain filters it, and the result is served to the device as an endless WAV over
`/api/media/eq/stream/…`, using the same header trick as the therapy stream.

The point of the Rust chain is **live control**: slider changes swap biquad
coefficients atomically on the running stream with filter state preserved, so
dragging a band is heard on the speaker in well under a second, with no playback
restart and no gap.

Only one transition needs the current track restarted: turning EQ **on** while
an un-proxied stream is playing, because the audio path physically changes.
Turning it **off** mid-stream flips the chain to bit-transparent bypass —
seamless — and the next track starts direct again.

Enabled state, preset and gains persist per player in `data/media_eq.json`. The
proxy URL must be reachable **by the device**, so `media.eq.base_url` (falling
back to `media.tidal.manifest_base_url`) has to point at this app on the LAN —
the same rule as the Tidal DASH manifest route.

Costs while EQ is on, by design: the stream is re-encoded, so Tidal lossless
becomes 44.1 kHz/16-bit PCM; the device reports no track duration, since it is an
endless WAV; and the stream dies with the app. EQ off is exactly the old
direct-URL behaviour, byte for byte.

## Therapy TTS engines

The therapy SPA has two backends, chosen by `media.therapy.engine`:

- **kokoro** (default) — in-process `KokoroTTS`, see
  `modules/media/kokoro_tts.py`.
- **wyoming** — `modules/media/therapy_tts.py`, which talks the Wyoming protocol
  to a `wyoming-piper` container (for instance the one an HA voice host already
  runs), assembles the streamed PCM into a WAV, and caches results on disk keyed
  by `(voice, speed, pitch, text)`.

```yaml
media:
  therapy:
    enabled: true
    engine: wyoming       # kokoro (default, in-process) | wyoming
    wyoming:
      host: "127.0.0.1"
      port: 10200
```

Piper applies speech speed via `length_scale`, which `wyoming-piper` does not
expose per request, so a speed other than 1.0 is approximated with a
pitch-preserving WSOLA time-stretch. numpy is required for that; without it audio
comes back at natural speed. Pitch is applied client-side by the SPA via
`playbackRate`, never here.

### KokoroTTS (default engine)

`modules/media/kokoro_tts.py` runs the Kokoro-82M model (Apache-2.0) directly
inside ZMM via `kokoro-onnx` + `onnxruntime` — no sidecar container, no Wyoming
hop. Speed is a native model parameter (length control), so unlike the
wyoming-piper path there is no client-side time-stretch approximation. Pitch is
applied client-side by the SPA via `playbackRate` and only participates in the
cache key.

The ~340 MB model files are **not** shipped in the image. They download on
demand into `data/tts_models/` (a persistent volume) when the operator clicks
"Download voice model" on the therapy page, surfaced via the `/api/tts/setup/*`
endpoints — everything privileged or expensive is user-triggered.

It presents the same duck-typed API as `TherapyTTS`
(`status`/`voices`/`synthesize` + `setup_*`), so routes and the SPA are
engine-agnostic.

## Therapy soundscape streaming

The therapy SPA generates its audio in the browser via the Web Audio API, which
a Cast or WiiM player cannot fetch. `modules/media/therapy_stream.py` ports that
synth graph to numpy and serves it as an endless WAV stream
(`GET /api/therapy/stream`), so therapy casts through the exact same
`/api/media/play` path as radio and Tidal.

Per mode, using the same tables as the SPA: detuned sine pads with slow LFOs, a
sub oscillator, a true-stereo binaural pair, generative scale notes, band-passed
texture noise, a lowpass voicing filter and a feedback-echo tail. Breathwork adds
the inhale/hold/exhale amplitude envelope; anxiety slides the binaural beat and
tempo down over ten minutes (entrainment). Speech overlays come from the therapy
TTS engine on the configured interval, with the bed ducked while the voice plays.

Generation is paced to real time with a small lead, so players buffer seconds
rather than minutes, and each listener gets an independent stream state.

## Acoustic chirp calibration

The stream-mode status sensor aligns what devices *report* playing. It cannot
see each device's output-pipeline latency — the DAC chain and speaker DSP — or,
of course, the speed of sound. `modules/media/sync_chirp.py` measures the audio
in the air instead.

During a running session each device plays a short logarithmic chirp in its own
time slot, a microphone at the server records the room, and GCC-PHAT matched
filtering recovers each chirp's arrival time to sub-millisecond precision.
Differencing arrivals across devices cancels everything common — mic start
latency, mic clock offset, the shared acoustic path — leaving the true
inter-device misalignment, which `cast_sync` converts into trims.

Pure DSP and capture; all session state stays in `cast_sync`. numpy only, no
scipy. `sounddevice` is imported lazily so the module loads on hosts with no
audio stack and fails with a clear message only when calibration is used.

This is the OpenZone §6.2 sensor, and it is the only thing that can see a
sensor-*invisible* bias — which is why there is no per-speaker manual trim
button in the Sync Lab.

## Media service lifecycle

`modules/media/service.py` builds providers from config, owns the
`MediaController`, and runs a poll loop that refreshes player state and pushes a
`media_state` event over the WebSocket so the UI updates live.

```yaml
media:
  enabled: true
  cast:    { enabled: true, app_id: "CC1AD845" }
  wiim:    { enabled: true, devices: ["192.168.1.50"] }
  radio_browser: { enabled: true }
  poll_interval_seconds: 10
  # Cast EQ proxy — base_url must be this app's LAN address so speakers can
  # fetch the processed stream (falls back to tidal.manifest_base_url).
  eq: { base_url: "http://192.168.1.10:8000" }
```

## Media subsystem overview

A self-contained multi-room audio engine: Google Cast (Nest/Home) and
WiiM/LinkPlay players, internet radio via the Radio-Browser directory, and
broadcast to *native* speaker groups (Cast groups created in Google Home, WiiM
multiroom).

Design: thin, stateless provider objects behind clean ABCs, orchestrated by a
provider-agnostic controller. No MQTT or HA dependency, and no ffmpeg stream
server for the ordinary path — radio URLs are handed directly to the devices,
which fetch them natively.

The two-sided provider split (sources vs players) is borrowed from Music
Assistant (Apache-2.0) as a reference, but the abstractions and code are our own,
so we can fix the bugs we do not like.

### WiiM / LinkPlay

`modules/media/players/wiim.py` talks the documented WiiM HTTP API
(`/httpapi.asp?command=…`). The core transport, volume and status commands come
from WiiM's official HTTP API PDF. The multiroom grouping commands are
LinkPlay-platform commands that are **not** in that PDF — community-documented
and semi-official — so they are isolated and degrade gracefully if a device
rejects them.

Discovery is currently a manual list of device IPs from config; mDNS discovery
(LinkPlay advertises `_linkplay._tcp` / UPnP) is a later enhancement.

Newer WiiM firmware serves the API over HTTPS on port 443 with a self-signed
cert and may disable plain HTTP, so each device is probed once and the working
scheme cached. HTTPS uses `verify=False`, since the cert is the device's own.

### Tidal

`modules/media/sources/tidal.py` uses the unofficial `tidalapi`. Phase 2 serves
**AAC** (HIGH quality) via a directly playable URL so Cast and WiiM can fetch it
without our stream server; lossless/HiRes DASH/FLAC manifests parsed and served
server-side are Phase 3 and explicitly not here.

Hard-isolated: `tidalapi` is imported lazily and every failure is swallowed, so a
Tidal breakage never affects Cast, WiiM or radio. The library is blocking
(`requests`), so every call is wrapped in `asyncio.to_thread`.

The session persists to `data/media/tidal_session.json` — a token rather than
user-edited config, hence not in `config.yaml`. Login is a device/OAuth flow: the
UI is handed a `link.tidal.com` URL while a background task waits for
authorisation.

### "This device" (browser) playback

`static/js/local-player.js` plays radio and Tidal in the page itself, presented
as an ordinary player so the Players list treats it like a speaker. Two routing
modes, chosen per track from whether the local EQ is switched on:

| Mode | Path | Trade-off |
|---|---|---|
| plain (default) | `<audio>` → output | Keeps playing with the phone's screen locked |
| eq | `<audio>` → `MediaElementSource` → `eq.js` | Sliders and spectrum work, but a locked/backgrounded phone suspends the page's `AudioContext` and the audio stops with it |

EQ mode's constraint is the browser's, not something the page can opt out of:
routing an element through a `MediaElementSource` makes its audio a product of
the `AudioContext`, and a locked phone suspends that context. So the EQ panel
warns about it, and toggling the EQ mid-stream re-routes the running track
(`eqRoutingChanged()`, which swaps the element — a `MediaElementSource` is
permanent once created, and disconnecting it yields silence, not direct output).

EQ mode also needs `crossorigin="anonymous"`: a `MediaElementSource` on a
cross-origin stream that sends no CORS headers is pure silence, and *with* the
attribute a non-CORS stream refuses to load at all. Hosts that refuse CORS are
remembered for the session so the next track skips the failing attempt.

The Media Session API is kept current in both modes — metadata, artwork,
transport handlers, `playbackState`, and a position state for tracks (skipped
for radio, whose duration is `Infinity`). Beyond the lock-screen controls, this
is what marks the tab as an audio session rather than one the browser may freeze.

#### Getting the bytes to the element

Sources are tried in this order, and the *stage* is chosen up front rather than
purely on failure:

1. **direct** — the stream URL on the element.
2. **proxy** — `/api/media/local/proxy`, same-origin, so neither CORS nor mixed
   content applies. Chosen up front for the two cases known to fail: the app is
   HTTPS-only, so the directory's many `http://` stations can never load direct,
   and hosts already recorded as refusing CORS.
3. **plain** (EQ mode only) — direct with the EQ routing dropped, so a stream
   that defeats both of the above still plays, unprocessed.

**HLS** is separate: much of the directory (the BBC among it) publishes HLS, and
no browser but Safari plays a playlist from an `<audio>` element. Those go to
hls.js (`static/js/vendor/hls.min.js`), vendored but loaded lazily — half a
megabyte nothing else needs — and warmed as soon as a search or the favourites
strip contains an HLS station, so the click that starts playback isn't waiting
on the download and losing its user-gesture privilege. Fatal network/media
errors are recovered at most three times before the stream is given up on.

Detection is by the directory's own `hls` flag, carried into
`RadioStation.hls` and out as an `application/vnd.apple.mpegurl` content type
(which Cast also needs to route the station to its HLS pipeline); a `.m3u8` URL
is the fallback signal for favourites pinned before the flag existed. Safari
gets the manifest on the element directly; everyone else gets hls.js over MSE,
which the EQ can still process because MSE feeds the element from a blob.

Manifests always go through the proxy, which **rewrites** them rather than
streaming them through: hls.js fetches segments and keys by XHR, so each one
would otherwise need CORS headers the stations don't send. Every URI in the
playlist — segments, `EXT-X-KEY`, `EXT-X-MEDIA`, `EXT-X-MAP`, variant playlists
— is rewritten to come back through the proxy, resolved against the manifest's
post-redirect URL. Rewrites are root-relative, which resolves correctly against
the proxy URL the playlist was served from, and nested variant playlists are
rewritten recursively because they arrive back through the proxy too.

#### Resolving a station

`MediaService.resolve_station()` tries a pinned favourite's stored snapshot,
then the Radio-Browser directory, then the snapshot the page sent with the
request (search results and favourites are already on screen, so a directory
outage should never lose a station the user can see).

The directory itself is volunteer-run and individual mirrors are routinely slow
or down, so `radio_browser` keeps the whole mirror list and walks **distinct**
mirrors per request rather than retrying one that just failed, re-resolving the
list when it's exhausted and falling back to the round-robin host itself when
reverse DNS is blocked. A failed lookup means "try the next mirror", never "the
station doesn't exist". DNS is blocking, so it always runs in a thread.

### Plain-HTTP device listener

The main app is HTTPS-only with a self-signed certificate, which Cast and WiiM
devices refuse — the same reason `cast_sync` runs its own plain-HTTP listener on
8010. `modules/media/device_http.py` (default :8011) serves **only** the
endpoints a speaker fetches by URL: the Cast EQ proxy stream, the Tidal DASH
manifest, and the therapy soundscape. No user data, no control surface, and the
EQ stream is additionally guarded by its per-playback random token.

With this up, `media.eq.base_url` needs no configuration at all — the EQ engine
falls back to `http://<lan-ip>:<this port>`. Setting `base_url` in
Settings → Audio only overrides the auto-detected address, for multi-homed hosts.
