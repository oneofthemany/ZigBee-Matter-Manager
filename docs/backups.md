# Backups

**Settings → Config**: download a backup, restore one, and (admin) schedule one
every night. The ZMM Manager's **Health** tab shows whether the nightly backup
is working.

| Path | Role |
|---|---|
| `modules/backup.py` | the manifest, building and encrypting, targets, retention, the schedule |
| `routes/backup_routes.py` | download, restore, schedule API |
| `static/js/backup-schedule.js` | the schedule card |
| `manager/backups.py` | read-only status for the Manager |
| `data/backup_schedule.json`, `data/backup_status.json` | the schedule; the last result |
| `config/secrets.yaml` → `backup` | passphrase, WebDAV password, S3 secret key |

## What is in one

Everything needed to rebuild the hub: `config.yaml` **and `secrets.yaml`**
(integration logins — without it a restore brings every integration back logged
out), the Zigbee device database, names, automations, groups, zones, floor
plan, Frames, workers, places, accounts (`auth.yaml`), presence, notification
rules and channels, the push identity (`vapid.json` — a new one would silently
unsubscribe every phone), house mode, the alarm and its PIN hashes, cameras,
Shelly and ESPHome devices, HomeKit pairings, Matter storage and the TLS
certificate. The list is `BACKUP_MANIFEST` and `BACKUP_DIRS`.

*Include history* adds the telemetry, Zigbee cache and energy (Octopus)
databases — much larger. The telemetry and energy databases are flushed and
checkpointed first, off the event loop, so their copies are whole.

Not included: caches, the logbook, messages and journeys databases, and
earlier backups.

A backup holds password hashes, API tokens, integration logins and the Zigbee
network key. Treat an unencrypted one like a key to the house.

## Scheduled backups

Every night at the set time; a hub that was off then backs up when it comes
back, once. Each run builds the zip in a worker thread to a temp file,
encrypts it if asked, sends it, and prunes.

**Retention** keeps the newest backup of each of the last *N* days that have
one, and of each of the last *M* weeks (default 7 and 4). It only deletes files
named `zmm_backup_<date>_<time>_(config|full).zip[.enc]`; anything else in the
folder or bucket is left alone.

**Destinations**

- **A folder** — default `data/backups`, on the hub's own disk: it survives a
  broken container, not a dead disk. For a NAS over SMB or NFS, mount the share
  on the host somewhere inside ZMM's data folder and point this at it.
- **WebDAV** — Nextcloud, Synology, most NAS boxes. `PUT`, `PROPFIND`, `DELETE`
  with basic auth; the folder must exist.
- **S3-compatible** — AWS, MinIO, Backblaze B2, Cloudflare R2. Path-style
  requests signed with Signature V4 (checked against AWS's published examples);
  uploads carry the file's SHA-256.

**Test destination** writes, lists and removes a small file, so a wrong
password is found now rather than at 03:30. It fails if the file can't be seen
in a listing, because then old backups could never be pruned.

### Encryption

With a passphrase, the zip is wrapped as
`ZMMBAK1 | salt | nonce | AES-256-GCM ciphertext | tag`, the key from scrypt
(n=2¹⁵). It is streamed, so a large backup isn't held in memory. Restore takes
the passphrase; a wrong one, or a damaged file, restores nothing.

**There is no recovery without the passphrase.** Keep it somewhere that isn't
this hub — the copy in `secrets.yaml` is inside the backups it encrypts.

### Knowing it works

- The schedule card and `GET /api/backup/status` show the last success, file,
  size and destination, or the last error.
- A failed run raises an app alert; so does two days without a success. The
  next success clears both.
- The ZMM Manager's Health tab shows the same from the status file — amber for
  failed or stale, never red: a missed backup is upkeep, not an outage.

A failed run isn't retried until the next night; **Back up now** retries it.

## Restore

Upload a `.zip`, or a `.zip.enc` with its passphrase. Only manifest paths are
written — no absolute paths, no `..`, nothing outside the list — and secrets,
accounts and PIN hashes are restored owner-only. A restart applies it.

## API

| | |
|---|---|
| `GET /api/backup/create?include_telemetry=` | download a backup |
| `POST /api/backup/restore` | multipart `file` (+ `passphrase`), or `url` |
| `GET /api/backup/info` | what a backup would contain |
| `GET /api/backup/status` | last scheduled result (`system:read`) |
| `GET` / `PUT /api/backup/schedule` | the schedule; secrets as `*_set` (admin) |
| `POST /api/backup/schedule/test` | test the destination (admin) |
| `POST /api/backup/schedule/run` | back up now (admin) |
