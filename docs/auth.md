# Users, Groups & Tokens

ZMM ships with a built-in identity system so the gateway can be safely
shared between household members and used by mobile apps without exposing
the whole API. It supports:

- **Username/password login** for the web UI (browser cookie session).
- **Bearer tokens** for programmatic access (curl, Android companion app, scripts).
- **Groups** to bundle scopes into reusable roles.
- **Scopes** to limit what each user, token, or group can do.

## Concepts

### Users

A human identity. Every login uses a username and (usually) a password.
Users can belong to zero or more groups, and additionally have direct
scope grants for fine-tuning.

### Groups

A named bundle of scopes. Default groups created on first run:

| Group     | Purpose                                                        |
|-----------|----------------------------------------------------------------|
| `admins`  | Full control. Has the implicit `admin` super-scope.            |
| `users`   | Day-to-day household members — can use devices and automations.|
| `viewers` | Read-only. Can see state, can't change anything.               |
| `mobile`  | For phone-issued tokens — minimal default scopes.              |

You can edit these or add your own from Settings → Users.

### Scopes

Permissions are expressed as dotted strings like `device:write` or
`presence:write:user`. Built-in scopes:

| Scope                  | Allows                                                   |
|------------------------|----------------------------------------------------------|
| `admin`                | Everything. Implies every other scope.                   |
| `device:read`          | View device state, configs, lists.                       |
| `device:write`         | Send commands, change settings.                          |
| `automation:read`      | View automations.                                        |
| `automation:write`     | Create, modify, delete automations.                      |
| `group:read`           | View Zigbee groups.                                      |
| `group:write`          | Modify Zigbee groups.                                    |
| `matter:read`          | View Matter nodes.                                       |
| `matter:write`         | Commission / remove / control Matter devices.            |
| `system:read`          | System status, telemetry, logs.                          |
| `system:write`         | Restart services, edit config, run upgrades.             |
| `presence:read`        | Read presence-user state.                                |
| `presence:write`       | Update **any** user's presence.                          |
| `presence:write:<id>`  | Update **only** the named user's presence (mobile-app token). |

Wildcards work at any segment: `device:*` matches all device permissions,
`presence:write:*` matches all per-user presence writes.

### Tokens

Long-lived bearer credentials. A token is owned by one user, can be a
subset of that user's scopes, has an optional expiry, and an optional
device-id label so you can revoke a stolen phone without affecting the
other devices the user owns.

Token plaintext is shown ONCE at issue time. Copy it immediately — ZMM
only stores its SHA-256 hash, so a forgotten token can't be recovered;
you'd need to revoke it and issue a new one.

## First run

On first boot, ZMM creates an `admin` user with a random password and
prints it to the logs at `WARNING` level. To find it:

```bash
podman logs zigee-matter-manager 2>&1 | grep -A3 "FIRST-RUN AUTH"
```

or

```bash
tail -n 100 -f /opt/.zigbee-matter-manager/logs/zigbee.log | grep -A3 "FIRST-RUN AUTH"
```

You'll see:

```
======================================================================
FIRST-RUN AUTH BOOTSTRAP
  Admin username: admin
  Admin password: 8sKr-X3yG2qN
  Change it via Settings → Users as soon as possible.
======================================================================
```

Log in with those credentials, then go to **Settings → Users** and:
1. Edit `admin` and set a password you'll remember.
2. Create a personal account for yourself in the `admins` group.
3. Disable the `admin` account (or leave it for break-glass).

## Adding household members

For each person:

1. Settings → Users → **New User**.
2. Username (e.g. `alice`), password, and add them to `users` (not `admins`).
3. They can now log in to the UI on their own devices.

## Issuing tokens for the mobile app

Each phone gets its own scoped token. Because the only thing the companion
app needs to do is report **its owner's** location, give it the narrowest
possible scope.

1. Settings → Users → Tokens tab → **Issue Token**.
2. Pick the user (e.g. `user`).
3. Label: e.g. "User's Pixel 8".
4. Device ID (optional but recommended): a stable identifier from the
   phone — the companion app shows this in its settings screen.
5. **Don't tick any built-in scope checkboxes.**
6. In the "custom scope" field, enter exactly:
   ```
   presence:write:user
   ```
   (replacing `user` with the user_id of the presence user this phone
   should report for — see [presence_users.md](presence_users.md)).
7. Optional expiry: 365 days is reasonable; the token can be revoked
   anytime regardless.
8. Click Issue. The plaintext token appears once — copy it into the
   phone's app.

If the phone is later lost or the person leaves the household, revoke the
token from the same screen. The phone loses access immediately on its
next request.

## Self-service

Non-admin users see a stripped-down Settings → Tokens screen where they
can:
- Change their own password.
- Issue tokens for themselves (within the scope of their groups).
- Revoke their own tokens.

They cannot see other users' tokens, change groups, or modify the user
list.

## Soft mode (for migration)

If you have an existing ZMM install with scripts or homemade integrations
that hit unauthenticated endpoints, set `enforce=False` in the
`AuthMiddleware(...)` constructor in `main.py`. The middleware will log
warnings on anonymous requests but not block them, giving you time to
audit your scripts and add `Authorization: Bearer ...` headers. Switch
back to `enforce=True` once you're confident.

## Security notes

- **Passwords** are stored as PBKDF2-HMAC-SHA256, 200 000 iterations,
  per-password 16-byte salt.
- **Tokens** are stored as SHA-256 hashes. Plaintext exists only on the
  client (or briefly in the issue response).
- **Session cookies** are HMAC-SHA256-signed with a secret derived from
  the auth file's inode + mtime. Replacing the file (e.g. backup
  restore) invalidates all sessions — by design.
- **No JWT, OIDC, or OAuth** is used. Tokens are static until revoked
  or expired. This is appropriate for a home gateway; it would not be
  appropriate for a multi-tenant SaaS.
- **TLS** is your responsibility. The session cookie is set with
  `httponly` and `samesite=lax` but **not** `secure` because ZMM may
  be deployed over plain HTTP on a LAN. If you expose ZMM beyond your
  LAN, enable HTTPS in the existing web SSL settings — without it,
  bearer tokens and cookies are visible to anyone on the wire.

## Backup & restore

`auth.yaml` is included in ZMM backups by default. Restoring a backup
restores users, groups, and tokens — but invalidates all session cookies
since the file's inode changes. Existing bearer tokens continue to work.
## Model

| Concept | Meaning |
| --- | --- |
| **User** | A human identity: a username, optionally a password (for browser login), zero or more group memberships, and zero or more issued API tokens. |
| **Group** | A named bag of scopes. Users inherit the union of scopes from every group they belong to, plus any directly assigned scopes on the user. |
| **Token** | An opaque bearer token (32 bytes, base64url) belonging to one user. Has a label ("Sean's Pixel"), optional expiry, an optional scope subset narrower than the owning user, and an optional free-form `device_id` (e.g. an Android `Settings.Secure.ANDROID_ID`) for revocation UX. |
| **Scope** | A dotted string like `presence:write:sean` or `device:*`. Wildcards match any segment at that position. |

`network:lan_only` is a special scope: principals holding it may only act from
the LAN. It is checked by exact membership, never wildcard- or admin-implied, so
an admin account can be LAN-restricted too. Enforced per request by the auth
middleware and at login by `SecureAuthManager`.

## Threat model

This is **not** a public auth provider. The gateway sits on a home LAN with
optional remote exposure, and the bar is: an attacker on the network cannot
spoof presence, and a stolen device token can be revoked individually.

- Tokens are stored hashed (SHA-256). The plaintext is shown **once**, at issue.
- Tokens carry 256 bits of entropy from `secrets.token_urlsafe(32)`.
- Passwords are stored as PBKDF2-HMAC-SHA256, 200 000 iterations, 16-byte salt,
  base64-encoded. No external password-hashing dependency is needed.
- There is deliberately **no** OAuth, OIDC, JWT, refresh token or rotation.
  Tokens are static until revoked or expired: simple enough to reason about, and
  sufficient for the threat model.

## Persistence and bootstrap

`data/auth.yaml` is the single source of truth. Atomic writes via temp-file
rename; loaded once at start, mutations save eagerly.

If the file does not exist at start, an `admin` user is created with a random
password printed to the logs **once**, changeable via the UI. This avoids
hardcoded defaults.

## Login flow

**Step 1** — `POST /api/auth/login` with username + password:

| Status | Body | Meaning |
| --- | --- | --- |
| 200 | `{success: true, ...}` | no MFA, fully logged in |
| 200 | `{mfa_required: true, challenge: "..."}` | MFA needed |
| 401 | `{detail: "..."}` | rejected |
| 423 | `{detail: "...", locked_until: ts}` | account or IP locked |
| 403 | `{detail: "...", lan_only_violation: true}` | must be on LAN |

**Step 2** — `POST /api/auth/login/mfa` with challenge + code: 200
`{success: true, ...}` for a valid TOTP or recovery code, 401 otherwise.

## MFA endpoints

Self-service, while already logged in:

| Endpoint | Purpose |
| --- | --- |
| `POST /api/auth/mfa/enrol/start` | returns secret + `otpauth` URI |
| `POST /api/auth/mfa/enrol/finish` | confirm with TOTP, returns recovery codes |
| `POST /api/auth/mfa/disable` | self-disable (re-prompts for password) |
| `POST /api/auth/mfa/recovery-codes/regenerate` | new set, invalidates the old |
| `GET /api/auth/mfa/status` | state for the current user |

Admin:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/auth/lockouts` | list locked accounts |
| `POST /api/auth/lockouts/{username}/unlock` | force-unlock |
| `POST /api/auth/users/{username}/disable-mfa` | clear MFA for a user |
| `GET /api/auth/network` | show network policy |

Plus user, group and token CRUD.

Deleting a user cascades to the matching presence user. Left behind, the orphan
keeps reporting a location for someone with no account, and any policy keyed on
the account — MFA in particular — fails closed against a user record that is not
there.

## Middleware and dependencies

Routes get authorised two ways (`modules/auth_middleware.py`):

1. **Middleware**, on every HTTP request. It bypasses unauthenticated paths
   (login, healthcheck, static assets, the legacy WebSocket where it does not
   yet enforce auth). For everything else it looks for a Bearer token in the
   `Authorization` header or a `zmm_session` cookie, and on success attaches
   `request.state.principal = (User, scopes_set, token_or_None)`. On failure it
   returns 401 unless the route is in the anonymous-allowed list.
2. **`require_scope(scope)`** as a route dependency. The middleware does the
   authn; the dependency does the authz.

Both bearer tokens and session cookies are supported because the browser UI uses
the cookie (set on `/api/auth/login`) while the Android app, curl, MQTT and
anything else programmatic uses bearer tokens.

Cookies are signed with HMAC-SHA256 using a secret derived from the auth file's
mtime and inode. That is enough to prevent forgery without a separate
secret-management story, and the secret rotates automatically when the file is
replaced — so restoring auth from a backup invalidates all sessions, which is
the desired behaviour.

Per-user scopes matter for the companion phone: `presence:read` means *every*
user's location, so handing it to a phone would let a stolen device token track
the whole household. `presence:read:<user_id>` keeps that token to one person.

## MFA and brute-force protection

`modules/auth_mfa.py` implements TOTP (RFC 6238) with no external dependencies —
stdlib `hmac`/`hashlib` only — plus ten single-use recovery codes hashed at rest,
per-account exponential lockout (1 → 5 → 15 → 60 minutes, capped), a per-IP
sliding-window rate limiter, a constant-ish-time login response delay to mask
"user exists" timing, and `otpauth://` URI generation for QR enrolment.

**Why no external deps.** `pyotp`, `qrcode` and friends are well engineered, but
adding dependencies to a self-hosted gateway is friction, and RFC 6238 is thirty
lines once you have HMAC. The QR code is rendered client-side.

MFA records live alongside auth in `data/auth.yaml` under an `mfa` section, one
record per user. Recovery code hashes are plain sha256 — the codes carry enough
entropy to skip salting.
