# Security: MFA, Lockout, and LAN-Only Accounts

ZMM's authentication system supports:

- **TOTP-based two-factor authentication** for any user account.
- **Recovery codes** for the case where someone loses their phone.
- **Brute-force protection** with per-account lockout and per-IP rate limiting.
- **LAN-only accounts** for low-friction "kid's tablet" style access.
- **Trusted-proxy support** for Cloudflare Tunnel and other reverse proxies.

## Two-factor authentication (TOTP)

### Enabling it for yourself

1. Sign in to ZMM and go to **Settings → My Account**.
2. Click **Enable Two-factor Authentication**.
3. Scan the QR code with your authenticator app:
    - Google Authenticator
    - Authy
    - 1Password
    - Bitwarden
    - Microsoft Authenticator
    - Or any other TOTP-compatible app.
4. Type the 6-digit code your app shows to confirm.
5. **Save the recovery codes that appear next** — they are shown once and
   you'll need them if you lose your phone.

After this, every login asks for the 6-digit code as a second step.

### Recovery codes

Ten single-use codes, generated when you enable MFA. Each one can stand
in for a TOTP code exactly once — useful if your phone is dead, lost, or
out of sync.

If you've used most of them, regenerate a fresh set from **Settings →
My Account → Regenerate recovery codes**. The previous set is invalidated.

### Disabling MFA

From **Settings → My Account → Disable 2FA**. You'll be asked for your
password again as a confirmation. Recovery codes are wiped at the same
time.

### "I lost both my phone and my recovery codes"

You'll need shell access to ZMM to reset:

```bash
podman exec zmm python3 -c "
    from modules.auth import AuthManager
    from modules.auth_secure import SecureAuthManager
    import asyncio
    a = AuthManager(); a.load()
    s = SecureAuthManager(a)
    asyncio.run(s.disable_mfa('your-username'))
    print('MFA disabled — log in with password and re-enrol')
"
```

If you're not the only admin, ask another admin to:
**Settings → Users → \[your account\] → Edit → Disable MFA**.

## Brute-force protection

Login attempts are tracked per username and per source IP. Failed
attempts trigger increasing lockouts:

| Failures | Lockout       |
|----------|---------------|
| 3        | 1 minute      |
| 5        | 5 minutes     |
| 8        | 15 minutes    |
| 12+      | 1 hour (cap)  |

A successful login clears the failure counter. Lockouts are
**in-memory** — restarting the container clears them. This is
intentional: most attacks happen within minutes, and persisting
lockouts across restarts has more downsides (legitimate user gets
permanently stuck if they forget once and ZMM stays running for months)
than upsides.

There's also a per-IP rate limit: 30 attempts per 5 minutes from any
single IP, regardless of which username they're targeting. This
catches username-spraying attacks where an attacker tries one password
against every account.

### Admin unlock

If a household member legitimately gets locked out, an admin can clear
their lockout from **Settings → Users → Admin → Locked accounts**.

## LAN-only accounts

The `network:lan_only` scope can be added to any user. When set,
every request from that user — login, API calls with a bearer token,
even an already-issued session cookie or WebSocket connection — is
rejected from any IP outside the configured LAN ranges. Tokens inherit
the restriction from their owning user; a token can't opt out of it.

### When to use this

- A kid's tablet that should never need remote access.
- A shared "guest" account on a wall-mounted dashboard.
- Service accounts used by other home automation tools on the LAN.

These accounts can use a simple password without MFA. The threat model
is "someone on my home Wi-Fi" — and someone with that level of access
already has bigger concerns than a kid's account.

### How LAN is detected

By default, ZMM treats these as LAN:

- `127.0.0.0/8` — loopback
- `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` — RFC1918
- `169.254.0.0/16` — IPv4 link-local
- `100.64.0.0/10` — CGNAT (covers Tailscale)
- `::1`, `fc00::/7`, `fe80::/10` — IPv6 equivalents

You can override this in `config.yaml` under `security.network.lan_ranges`.

## Behind a reverse proxy (Cloudflare Tunnel, nginx, Caddy)

When ZMM sits behind a proxy, every request looks like it came from the
proxy itself. We need to tell ZMM which proxies it can trust to report
the real client IP.

### Cloudflare Tunnel

This is the recommended external-access path. To configure:

```yaml
security:
  network:
    cloudflare_tunnel_enabled: true
    trusted_proxies:
      - "127.0.0.0/8"   # cloudflared running locally
```

ZMM will then read the real client IP from `CF-Connecting-IP` headers
(but only when they arrive from a Cloudflare IP or your localhost
cloudflared process).

ZMM can also run the tunnel for you — see the
[Remote Access guide](remote_access.md) (Settings → Security →
Remote Access in the UI). The managed tunnel enables the header trust
above automatically.

### Other reverse proxies (nginx, Caddy, Traefik)

Add the proxy's IP to `trusted_proxies`:

```yaml
security:
  network:
    trusted_proxies:
      - "127.0.0.0/8"
      - "10.0.0.5/32"   # your reverse-proxy host
```

Make sure your proxy sets `X-Forwarded-For` correctly. nginx example:

```nginx
location / {
    proxy_pass http://zmm:8000;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header Host $host;
}
```

### Why this matters

Without configured trusted proxies, an attacker could send
`X-Forwarded-For: 192.168.1.5` to bypass the LAN-only check. ZMM
**ignores** these headers from any IP not in the trusted list,
specifically to prevent this attack.

## Token-based access (mobile app, scripts)

Bearer tokens **bypass the MFA prompt** by design — they ARE a second
factor of sorts, since they're long random strings stored on a specific
device. This matches how every other API service works (GitHub PATs,
AWS access keys, etc.).

Tokens inherit the LAN-only restriction from their owning user, however.
A token issued for a `network:lan_only` account can only be used from
the LAN.

## Logging

Watch for these log lines (`WARNING` level):

```
[bruteforce] sean locked for 60s after 3 failures
[bruteforce] IP 203.0.113.99 rate-limited
[network] kid attempted login from non-LAN (8.8.8.8) but holds network:lan_only
[network] CF-Connecting-IP from untrusted peer 203.0.113.66 — ignoring
[mfa] sean used a recovery code (9 remaining)
```

The last one in particular — recovery code usage — is worth noticing.
If you see it and you didn't expect it, change your password and
regenerate recovery codes immediately.
## Self-signed certificate bootstrap

`modules/ssl_bootstrap.py` is the single source of truth for generating the
app's self-signed HTTPS certificate, used by `main.py`'s entry point. It
auto-generates on first boot so the app serves HTTPS out of the box, which is
what the watchdog, manager and container healthcheck all expect.

HTTPS is always on. There is no HTTP mode and no toggle — plain HTTP only ever
appears as the cert-failure fallback in `main.py`, and that is an error
condition rather than a configuration.

### Design rules

- **Never regenerate an existing pair.** Browsers that already trust the cert
  would break, which is the leading cause of "this site is unsafe" after a
  config tweak.
- **Sensible SANs** (localhost, hostname, 127.0.0.1) so internal probes and
  localhost browsing do not hit name/IP mismatches.
- **Lock the private key to 0600** — openssl writes 0644 by default.

### SANs and the LAN address

A SAN of only localhost/hostname/127.0.0.1 means the cert does *not* cover the
address people actually browse to (`https://192.168.1.x:8000`), so every client
gets a name-mismatch warning. Browsers let you click through it; strict clients
do not — the Android presence companion fails hostname verification outright,
whether or not the cert itself is trusted.

So the bootstrap also adds every non-loopback IPv4 this host can see, plus
anything in the `ZMM_CERT_SANS` environment variable (comma-separated).

`ZMM_CERT_SANS` matters when the app runs in a bridged container: auto-detection
then sees the container's address (10.88.x.x), not the LAN IP the user types.
Set it to the LAN IP or hostname you actually browse to:

```
ZMM_CERT_SANS=192.168.1.1,zmm.local
```

## Map-tile proxy

Presence maps need tiles, and fetching them straight from a public tile server
means every viewer's browser announces the coordinates it is looking at — on
every pan and zoom — to a third party. For a map centred on where your family
live, that is the one request pattern worth avoiding.

Proxying through the hub (`modules/map_tiles.py`) changes what leaks:

- the tile server sees the **hub's** address, once per tile ever, rather than
  each viewer's address on every interaction;
- a cached tile involves no external request at all, so a household that looks
  at the same few square kilometres goes quiet almost immediately;
- the browser talks only to ZMM, so this keeps working over a tunnel or VPN
  where the client may have no direct internet access.

It does **not** make the first fetch private — the hub still asks upstream for
tiles it has never seen. Seeding the cache offline is the only way to avoid that
entirely, and is left to the operator.

The proxy is authenticated deliberately. An open tile proxy is something other
people will find and use, and the traffic would be attributed to this hub's
address by the upstream server — which is exactly how a self-hosted tool gets
blocked. [Place search](place-search.md) rides along with it for the same reason.

**Upstream etiquette.** OpenStreetMap's tile policy requires an identifying
User-Agent and forbids bulk or systematic downloading. This proxy is
demand-driven — it fetches only tiles someone actually looked at — and caps
concurrency so a fast pan cannot turn into a burst. **Do not point a prefetcher
at it.**

## Source-IP resolution and LAN classification

`modules/auth_network.py` handles two distinct concerns.

**1. Get the real client IP**, even when ZMM sits behind a Cloudflare Tunnel
(`CF-Connecting-IP`), a reverse proxy such as nginx/Caddy/Traefik
(`X-Forwarded-For`, `X-Real-IP`, possibly `Forwarded`), Tailscale (no proxy, but
the immediate peer *is* the real client), or direct LAN access.

Headers are trusted **only** when the immediate peer is on a configured
trusted-proxy list. Trusting `X-Forwarded-For` from any source lets an attacker
spoof their IP trivially.

**2. Classify an IP as LAN-or-not**, for the `network:lan_only` scope. Defaults
cover RFC1918, loopback, link-local, CGNAT (which includes Tailscale), and IPv6
ULA and link-local. Users can override.

```yaml
security:
  network:
    trusted_proxies: ["127.0.0.1/8", "172.16.0.0/12", "10.0.0.0/8"]
    cloudflare_tunnel_enabled: true   # also trust CF-Connecting-IP from
                                      # Cloudflare's published ranges; a recent
                                      # snapshot ships, overridable
    lan_ranges: [...]                 # override the default LAN ranges
```

## Physical security providers

`routes/security_routes.py` describes providers by a registry, so the frontend
builds its Security tab dynamically — adding a provider later means a new entry
in `PROVIDERS` plus its own endpoints, with no frontend structural change.

First provider is Nuki, over two channels: **bridge**, the Nuki Bridge HTTP API
on the LAN (`modules/nuki_controller.py`), and **matter**, bridge-less locks
(Smart Lock 3.0 Pro / 4th gen) commissioned through the embedded Matter server
and filtered from the Matter device list by type `Lock`.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/security/providers` | registry + per-channel state |
| `GET /api/security/nuki/status` | bridge `/info` + matter summary |
| `GET /api/security/nuki/locks` | unified lock list across both channels |
| `POST /api/security/nuki/locks/{lock_id}/action` | `lock`, `unlock`, `unlatch`, `lock_n_go`, `lock_n_go_unlatch` |
| `POST /api/security/nuki/bridge/discover` | find bridges via the Nuki cloud |
| `POST /api/security/nuki/bridge/auth` | fetch token (bridge button pressed within 30 s) |

Config is read per request, like `ac_routes`, so Settings edits apply without a
restart. Lock ids are channel-namespaced: `bridge:<nukiId>` / `matter:<node_id>`.

Yale is deliberately Matter-only — see the note in `security_routes.py`.

### Nuki Bridge client

`modules/nuki_controller.py` is an async client for the Nuki Bridge HTTP API
(v1.13), covering the "with bridge" half of the integration. Bridge-less locks
are commissioned through the embedded Matter server instead and handled in
`routes/security_routes.py`.

- All endpoints are plain HTTP GET on the bridge, default port 8080.
- Auth is a token, sent either as `token=` (plain) or as the hashed triple
  `ts` / `rnr` / `hash`, where `hash = sha256("<ts>,<rnr>,<token>")`. **Hashed is
  the default here** — the plain form leaks the token to anything that can see
  LAN traffic. The bridge must have "hashed token only" left on (the factory
  default) for hashed to work; plain is kept as an opt-out for old firmware.
- `/auth` returns a fresh token, but only while the bridge's button has been
  pressed within the last 30 s, and only if auth-enable is on.
- Bridge discovery is a Nuki cloud call to `api.nuki.io` — the bridge phones
  home its LAN ip/port, and no credentials are needed.

Config lives in `config.yaml` under `security.nuki.bridge`:
`{enabled, host, port, token, hashed_token}`.
