# Media tests

    python3 tests/media/run_all.py

Plain scripts, no framework — matching `tests/fuel` and `tests/swarm`. Each module
exposes `run()` returning a `Checker`; `run_all.py` drives them and exits non-zero on
failure.

Nothing here imports `tidalapi` or FastAPI, and nothing logs in: the per-user account
registry is plain Python over the filesystem, and the session paths are redirected to
a temporary directory by `harness.TempSessions`. `modules.auth` is used for real
rather than stubbed, so a rename of `User.groups` / `.extra_scopes` / `.disabled`
fails the admin-resolution tests instead of passing quietly.

`modules.media.cast_sync` is the one thing here that cannot be imported: it pulls in
FastAPI at module scope, which a dev box does not have. The zone engine's own
resolver call is therefore verified by reading; what feeds it — the rows from
`sync_queue_items`, and the media block `resolve_zone_media` stamps — is tested.

| Module | Covers |
|---|---|
| `test_tidal_automation.py` | Rules and the end state: `tidal_owner` stamped over the body on create and only into empty steps on update, stripped from non-Tidal steps, both branches covered; the engine passing it on both the player and zone paths; the admin accounts endpoint being admin-only and leaking no token; and that the `__getattr__` shim stays gone |
| `test_tidal_manifest_token.py` | The lossless manifest token: minting, redeeming, expiry and pruning, the bounded live set, that the URL names neither the user nor the track, and that both handlers — the main app's and the loopback listener's — redeem a token rather than trusting a track id off the path |
| `test_tidal_routes.py` | That every Tidal endpoint names its caller and resolves that caller's own account, by AST analysis of the route modules — FastAPI is not installed, so the handlers cannot be driven. The device-fetched manifest route is listed as a deliberate exception, so it stays a decision rather than an oversight |
| `test_tidal_owner.py` | Ownership travelling with the playback: the `owner` field and its queue round-trip (including a queue saved before the field existed), resolution failing closed for a user with no linked account, the controller naming the owner on auto-advance / zone / radio top-up, and the zone media block being stamped server-side rather than read from the request body |
| `test_tidal_accounts.py` | Per-user Tidal accounts: per-user credential files and their permissions, the username guard that keeps a path separator out of a filename, adoption of the pre-multi-user session (config owner, sole admin, parked when ambiguous, skipped once per-user files exist), which account an un-namespaced call resolves to, and the single-user compatibility shim — including that a callable captured at service construction follows the account that appears later |
| `test_sonos_player.py` | The Sonos provider against a fake `soco` (neither the library nor speakers exist on a dev box): transport calls from any group member landing on the coordinator, radio forced into Sonos' `x-rincon-mp3radio` form while finite tracks are not, group state reported from the coordinator's side, per-speaker volume, bonded satellites skipped, speakers that miss a discovery sweep kept, and an unreachable speaker reported unavailable rather than raising |
\n| `test_airplay_player.py` | The AirPlay provider against a fake pyatv but real ffmpeg (skipped without it): a track streaming to its natural end, pause killing the decoder and resume seeking back to the paused position, live streams resuming at the live edge and never counting as ended, track switches winding the old stream down before pyatv's one-stream guard, stream failures dropping the connection, emulated mute, PIN pairing and the 0600 credential store. Where the real pyatv is installed, every name and parameter the provider uses is checked against it |\n
| `test_zone_yield.py` | A device someone else is using is yielded, not re-cast over: a WiiM on HDMI-ARC (read from its httpapi `mode`) is never re-LOADed by the interruption rung, a reload or a re-align, and rejoins only after a settled free run that an input flap restarts; another Cast app is detected mid-session and yielded to, but a deliberate zone start still takes it over; `LinkPlayDirectory` discovery, mode classification by input band, and its fail-closed memory of a recent foreign reading |
| `test_zone_lock.py` | Per-speaker zone policy: a lock keeping a speaker out of session start, recovery and rejoin, persisted with its author and surviving a restart; locking mid-session quitting only the zone's own stream; unlock as a one-shot, time-bounded handback over another input; timed locks expiring; toggle; `sticky` turning an input change into a lock that a TV sleeping does not undo; `reclaim` never yielding but still outranked by a lock; a lock set on `wiim:<ip>` landing on the Cast member; the `zone_lock` automation step (read from source) |
| `test_zone_input.py` | Input polling of a LinkPlay member while a zone plays: a switch to HDMI-ARC caught while Cast still reports PLAYING, after two readings and never one; the mode Cast plays in learned from a live zone (twice, fresh lag) and persisted, while an input or a stale status teaches nothing; reclaim and sticky applied on this path too; one read in flight, none for yielded speakers or during pre-roll, and no input reported for a non-LinkPlay speaker |
