# Cast receivers

Two custom Google Cast Web Receivers live here:

- `receiver.html` — album art + synced lyrics (see below).
- `sync_receiver.html` — **sync PoC**: synchronised multi-speaker playback
  without a Google-Home group (see the last section).

# Cast lyrics receiver

`receiver.html` is a custom Google Cast **Web Receiver** that shows album art +
**synced lyrics** on screened Cast devices (Nest Hub). The default Google media
receiver has no lyrics surface, so this is the only way to get lyrics on screen.

It's fully static: ZigBee Manager sends the artwork + LRC-timed lyrics inline as
`media.customData` on each play, so the receiver never calls back to your
(self-signed) local API.

## One-time setup

1. **Host `receiver.html` on HTTPS.** Cast will not load an HTTP or self-signed
   page. Easiest options:
   - GitHub Pages (commit this file to a repo, enable Pages) →
     `https://<you>.github.io/<repo>/receiver.html`
   - Any static host with a valid TLS cert (Netlify, Cloudflare Pages, S3+CloudFront…).

2. **Register it as a Custom Receiver.** Go to
   <https://cast.google.com/publish>, pay the one-time Cast developer
   registration if prompted, **Add new application → Custom Receiver**, paste the
   HTTPS URL from step 1. Copy the generated **Application ID**.

3. **Register your Nest Hub for testing** (until you publish the app):
   same console → *Cast Receiver → Serial number* of your device. Allow ~15 min
   and reboot the Hub.

4. **Tell ZigBee Manager the App ID.** In `config/config.yaml`:
   ```yaml
   media:
     cast:
       lyrics_app_id: "ABCD1234"   # the Application ID from step 2
   ```
   Restart. Now playing a Tidal track (that has lyrics) to a Cast device routes
   to this receiver with art + synced lyrics; everything else uses the default
   receiver unchanged.

## Notes
- Tracks without lyrics, and non-Tidal sources, fall back to the default
  receiver automatically.
- Plain (un-timed) lyrics are shown as a static block; LRC time-tagged lyrics
  scroll and highlight the active line against the media clock.
- If you later publish the app in the Cast console you can drop the per-device
  registration from step 3.

# Sync PoC receiver (`sync_receiver.html`)

Proof-of-concept for **echo-free multi-speaker playback without a Google-Home
group**. ZMM streams timestamped PCM chunks over a WebSocket; the receiver
estimates its offset to the ZMM server clock (NTP-style) and schedules each
chunk sample-accurately with the Web Audio API. A per-speaker trim (±ms)
compensates each device's fixed output latency — tune it once by ear against
the 2-second click track, it stays valid.

Unlike the lyrics receiver, this page **must be served by ZMM itself over
plain HTTP** (config `media.cast.sync.http_port`, default 8010): the receiver
needs a live same-origin `ws://` socket back to ZMM, which an HTTPS-hosted
page can't open (mixed content) and the app's self-signed cert can't provide.
Plain-HTTP receiver URLs work for *unpublished* (development) Cast apps on
devices whose serials are registered in the console.

## One-time setup

All of this is driven from **Settings → Audio** in the UI (it edits
`media.cast.sync` in config.yaml for you). Requires a Google Cast developer
account (one-time $5 registration fee).

1. Enable Speaker Sync, *Save & Restart*, and check
   `http://<zmm-host>:8010/health` answers on the LAN (if ZMM's podman
   container is not on host networking, publish the port).

2. In the [Cast developer console](https://cast.google.com/publish):
   **Add new application → Custom Receiver**, URL
   `http://<zmm-host>:8010/cast/sync_receiver.html` (the Speakers tab shows a
   copy-ready URL). Don't publish it. Make sure each test speaker's **serial
   number is registered** for development (same step you did for the lyrics
   receiver), then reboot the speakers once.

3. Paste the generated Application ID into the Speakers tab and *Save & Restart*.

## Running the experiment

In **Media → Group → Speaker sync**: create a sync group (two or more Cast
speakers), hit **Test**. Both should begin the pad + click test signal within
a few seconds. Stand between them and drag each speaker's trim slider (5 ms
steps) until the clicks fuse into one. Per-device stats (clock offset, RTT,
late/dropped chunks) refresh live — also available raw from
`GET /api/media/sync/status`.

What a *good* result looks like: clicks indistinguishable (≲10 ms), stable
over 15+ minutes (drift is being corrected), `late` staying near zero. That
would green-light building the real feature (ZMM-defined groups playing radio/
Tidal through this pipeline).
