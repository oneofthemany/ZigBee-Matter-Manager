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
