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
