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
| `heating:read`         | View heating and AC state, schedules, zones.             |
| `heating:write`        | Change target temperatures, schedules, modes.            |
| `media:read`           | View players, queues, libraries.                         |
| `media:write`          | Play, pause, group, change volume, announce.             |
| `energy:read`          | View tariffs, consumption and cost.                      |
| `energy:write`         | Change tariff and energy settings.                       |
| `security:read`        | View lock state.                                         |
| `security:write`       | Lock and unlock.                                         |
| `presence:read`        | Read presence-user state.                                |
| `presence:write`       | Update **any** user's presence.                          |
| `presence:write:<id>`  | Update **only** the named user's presence (mobile-app token). |

Wildcards work at any segment: `device:*` matches all device permissions,
`presence:write:*` matches all per-user presence writes.

`security:*` is deliberately not part of `device:*`. Unlocking a door is not
the same capability as switching a lamp, and a token should be able to hold
one without the other.

### How a scope is enforced

`AuthMiddleware` resolves the required scope for every `/api/` request from
the path → scope table in `modules/auth_scopes.py`, and refuses with 403
before the route runs. **The table denies by default**: a path matching no
prefix requires `admin`, so a newly added route is closed until someone maps
it deliberately.

Route-level `require_scope(...)` dependencies still run, as a second and finer
check — they see path parameters the prefix table cannot, which is how
`presence:write:<id>` and the admin-only corners of `/api/auth` are enforced.
Routes whose table entry is `@authenticated` are self-service endpoints that
resolve the caller themselves; **they are the gate**, so a new route added
under one of those prefixes without its own check is open to any principal.

`tests/auth/run_all.py` fails the build if any `/api/` route resolves to no
prefix, or if the shipped `users` / `viewers` groups lose access to ordinary
parts of the app.

**There is no switch to turn this off, by design.** An admin satisfies every
check — `scope_matches` short-circuits on `admin`, and an unmapped path asks
for `admin` — so no table, however wrong, can lock an admin out. Only a
non-admin can be refused, and the refusal names the remedy:

```
[auth] alice denied POST /api/heating/zones: needs heating:write
```

Grant that scope to the user or their group in Settings → Users. A global
"stop enforcing" flag would buy nothing that granting a scope does not, while
leaving the hub silently unauthorised if anyone forgot to put it back.

### Headers

Every response carries `X-Content-Type-Options: nosniff`,
`Referrer-Policy: same-origin`, `X-Frame-Options: SAMEORIGIN` and two
Content-Security-Policy headers (`modules/security_headers.py`):

- **Enforced:** `frame-ancestors`, `base-uri`, `form-action`, `object-src`.
  None touch inline script or style, so they are safe against the SPA as it
  stands, and `frame-ancestors` is what stops the UI being framed on a
  hostile page.
- **Report-Only:** the strict target, including `script-src 'self'`.
  `static/` still has inline handlers and inline `<script>` blocks, so
  enforcing it would break the UI. Violations post to `/api/csp/report`
  (anonymous, rate-limited, size-capped) and log as `[csp] ... blocked ...`,
  which is the list of what is left to fix.

Promoting the target to enforced is a code change, not a setting. It is safe
only once `static/` is clean, and enforcing it early breaks the SPA including
the settings page — leaving no way back in.

There is no `Strict-Transport-Security`. HSTS prevents a downgrade to plain
HTTP, but the first-boot certificate is self-signed, so the header would only
remove the browser's click-through and strand the user on their own hub for
the `max-age`. Behind the tunnel, the edge sends HSTS itself.

### Tokens

Long-lived bearer credentials. A token is owned by one user, can be a
subset of that user's scopes, has an optional expiry, and an optional
device-id label so you can revoke a stolen phone without affecting the
other devices the user owns.

Token plaintext is shown ONCE at issue time. Copy it immediately — ZMM
only stores its SHA-256 hash, so a forgotten token can't be recovered;
you'd need to revoke it and issue a new one.

### Step-up for code execution

Writing to the editor or restoring a backup needs the second factor
re-verified within the last 5 minutes (`STEP_UP_WINDOW_S`), on top of `admin`.
A stolen session cookie carries no second factor, so this is what stops a
borrowed browser turning into arbitrary code execution.

Paths are listed in `STEP_UP_PREFIXES` (`modules/auth_scopes.py`) and enforced
in the middleware. Reads are exempt — browsing a file is not running one.

The refusal is a 403 carrying `step_up_required: true`; the fetch interceptor
in `static/js/auth.js` prompts, posts the code to `/api/auth/step-up` and
replays the request. The step-up is bound to the credential that verified it,
so one browser cannot unlock another, and it is dropped if MFA is disabled.

An admin with no MFA enrolled is refused rather than waved through, and told
to enrol. If that leaves you stuck, see below.

## Locked out

`auth_recover.py` runs inside the container and never over the network. It
needs write access to `data/auth.yaml`, which is already root-equivalent, so
it grants nothing the shell running it does not already have. Every action
logs at `WARNING`.

```bash
podman exec -it zigbee-matter-manager python3 /app/auth_recover.py list
podman exec -it zigbee-matter-manager python3 /app/auth_recover.py reset-password <user>
podman exec -it zigbee-matter-manager python3 /app/auth_recover.py disable-mfa <user>
podman exec -it zigbee-matter-manager python3 /app/auth_recover.py make-admin <user>
podman exec -it zigbee-matter-manager python3 /app/auth_recover.py create-admin rescue
```

`reset-password` and `create-admin` generate a password and print it once.
Delete a rescue account once you are back in.

The lockout this exists for is a lost TOTP device with the recovery codes
gone. Scope enforcement cannot lock an admin out — see above.

## First run

On first boot, ZMM creates an `admin` user with a random password and
prints it to the logs at `WARNING` level. To find it:

```bash
podman logs zigbee-matter-manager 2>&1 | grep -A3 "FIRST-RUN AUTH"
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

## Changing passwords

**Your own** — Settings → My Account → Change Password. You must enter your
current password, then the new one twice. `PATCH /api/auth/users/<you>` with
`{"password": ..., "current_password": ...}`; a wrong current password is a
403, a missing one a 400.

**Someone else's (admin)** — Settings → Users → edit the user → set Password
and Confirm password, then Save. `PATCH /api/auth/users/<them>` with
`{"password": ...}`. No current password is required: an admin resetting an
account they don't own has none to give. Admins editing their *own* account
in that screen still have to confirm, same as anywhere else.

Either way:

- The new password must be at least 8 characters. This is enforced in
  `AuthManager`, so the API, the setup wizard and the UI all agree.
- **Every existing session for that user is invalidated** — the browser you
  are not sitting at, and any live WebSocket, which is hung up rather than
  left streaming until it happens to drop. Clients reconnect on their own and
  land on the login page. The person who made the change keeps their own
  session: they get a fresh cookie in the response.
- Issued **API tokens are not affected**. A token is a separate credential
  with its own lifecycle — revoke those from Settings → Users → Tokens. If
  you are resetting a password because an account was compromised, revoke
  the user's tokens too.
- The change is logged at WARNING with who changed whose password and
  whether it was a self-service change or an admin reset.

Sending `{"password": null}` clears a password, leaving an account reachable
only by token. Admins may do this to other accounts; nobody may do it to
their own.

## Self-service

Non-admin users see a stripped-down Settings → Tokens screen where they
can:
- Change their own password (current password required).
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
- **Session cookies** are HMAC-SHA256-signed with a secret held in
  `data/session_secret.bin`. Deleting that file invalidates every session
  on every account.
- **Changing a password invalidates that user's sessions.** Cookies carry
  their issue time; the account records `pw_changed_at`, and a cookie
  issued before it is refused. Accounts that have never changed a password
  record nothing and are unaffected.
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
  base64-encoded. No external password-hashing dependency is needed. Minimum
  length 8; changing one cuts every session for that account.
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
