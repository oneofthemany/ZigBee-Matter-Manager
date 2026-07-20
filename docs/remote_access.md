# Remote Access

How to reach ZMM from outside your home network — even behind NAT or
CGNAT — without opening a single port on your router.

ZMM ships a **managed Cloudflare Tunnel**: a supervised `cloudflared`
process that dials *out* to Cloudflare's edge. Remote users open a
normal HTTPS URL in a browser and log in with their ZMM account.
Because the connection is outbound, it works behind any NAT, CGNAT, or
firewall.

> **Do not port-forward ZMM directly to the internet.** The built-in
> TLS is self-signed, and raw exposure means every scanner on the
> internet can reach your login page. Use a tunnel or a VPN overlay.

## Option 1 — Managed Cloudflare Tunnel (recommended)

Configured entirely from the UI: **Settings → Security → Remote Access**.

### Prerequisites

- A free [Cloudflare account](https://dash.cloudflare.com/) with a
  domain added to it.
- The `cloudflared` binary visible to the ZMM **process** — where that
  is depends on how you run ZMM:

  **Container install (build.sh — the default).** Images built by a
  current `build.sh` already include `cloudflared`; just upgrade or
  rebuild ZMM. Installing cloudflared on the host does *not* help —
  the container can't see it. To retrofit a running container without
  a rebuild, run this on the host:

  ```bash
  sudo podman exec -u root zigbee-matter-manager bash -c \
    "curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-\$(dpkg --print-architecture) \
     -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared"
  ```

  (swap `podman` for `docker` if that's your runtime; this survives
  restarts but not an image rebuild)

  **Running from source.** Install on the machine itself
  ([downloads](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)):

  ```bash
  # Fedora/RHEL — repo install, dnf keeps it updated
  curl -fsSL https://pkg.cloudflare.com/cloudflared-ascii.repo | sudo tee /etc/yum.repos.d/cloudflared.repo
  sudo dnf install cloudflared

  # Debian/Ubuntu (amd64)
  curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cloudflared.deb
  sudo dpkg -i cloudflared.deb
  ```

  The Remote Access tab detects your OS and whether ZMM is
  containerised, and shows the matching commands whenever the binary
  is missing.

  Do **not** run `cloudflared service install` — ZMM manages the
  process itself.

### Setup

1. Cloudflare dashboard → **Zero Trust → Networks → Tunnels →
   Create a tunnel** (type: Cloudflared). Name it e.g. `zmm`.
2. On the connector install page, copy the long token from the shown
   command (`cloudflared service install eyJh...` — the `eyJh...` part).
3. Add a **Public Hostname**: e.g. `zmm.example.com`, service
   `HTTP://localhost:8000` (match your `web.port`; use HTTPS if you
   enabled ZMM's SSL).
4. In ZMM: **Settings → Security → Remote Access**:
   - Mode: *Cloudflare Tunnel*
   - Paste the token, enter the public hostname
   - Toggle *Enable remote access*, then **Save & Apply**
5. The status card should show *Running* with active edge connections.
   Open `https://zmm.example.com` from a phone on mobile data to verify.

The token is stored in `data/remote_access.yaml` (mode 0600) and is
never sent back to the browser. The tunnel starts automatically with
ZMM from then on, and restarts itself with backoff if it crashes.

ZMM automatically trusts `CF-Connecting-IP` from the local `cloudflared`
while the managed tunnel is running, so remote clients are correctly
classified as non-LAN (see [Security notes](#security-notes)). If you
instead run cloudflared yourself (systemd, docker), set
`security.network.cloudflare_tunnel_enabled: true` in `config.yaml`.

### Quick tunnel (testing only)

Mode *Quick tunnel* needs no account or token: ZMM gets a random
`https://<random>.trycloudflare.com` URL, shown on the status card.
The URL changes on every restart and the service has no uptime
guarantee — use it to try things out, not as your permanent setup.

## Option 2 — Tailscale / WireGuard (private overlay)

If your "end users" are a handful of family devices, a VPN overlay is
the most secure option — ZMM never touches the public internet:

1. Install [Tailscale](https://tailscale.com/download) on the ZMM host
   and on each user's device; invite the users to your tailnet.
2. Users browse to `http://<zmm-tailscale-ip>:8000` (or use MagicDNS:
   `http://zmm:8000`).

No ZMM configuration is needed: Tailscale addresses fall in
`100.64.0.0/10`, which ZMM's defaults classify as LAN — so even
`network:lan_only` accounts work over Tailscale. If you *don't* want
tailnet members treated as LAN, override `security.network.lan_ranges`
in `config.yaml` to exclude `100.64.0.0/10`.

Trade-off: every user must install and stay logged in to the VPN
client, and can't just open a URL from any browser.

## Security notes

When ZMM is remotely reachable, its own login is the wall. The
relevant protections (see [security.md](security.md) for the full
model):

- **MFA**: enable TOTP for every account that is allowed in remotely
  (Settings → User Accounts → My Account). Strongly recommended —
  consider it mandatory for admins.
- **`network:lan_only` scope**: accounts/tokens carrying this scope are
  rejected outside the LAN — enforced on every request (HTTP and
  WebSocket), not just at login. Give it to kids' accounts, wall
  dashboards, and LAN service accounts.
- **Brute force**: per-IP rate limiting and per-account lockout are
  always on; Cloudflare's edge adds its own DDoS protection.
- **Anonymous surface**: the API map (`/routes`, `/api-docs`) and
  `/api/system/status` are only served anonymously to LAN clients;
  internet visitors see the login page and nothing else. The first-run
  setup wizard is LAN-only.
- **Extra wall (optional, not required)**: the tunnel setup above is
  complete on its own — remote users land straight on the ZMM login.
  If you want a second gate in front of it, you can additionally put
  [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/)
  (Zero Trust) in front of the hostname for email-OTP/SSO before a
  request even reaches ZMM (free for up to 50 users) — but skip it
  entirely if ZMM's own password + TOTP MFA is enough. If you add an
  Access application and later want it gone, delete it from **Zero
  Trust → Access controls → Applications**; the tunnel itself keeps
  working unaffected.

## Troubleshooting

- **"cloudflared binary not found"** — if ZMM runs in a container
  (the default), the binary must be *inside the container*; a host
  install is invisible to it. See the container instructions above,
  or set an explicit path under Advanced in the Remote Access settings.
- **Exits immediately / restart loop** — almost always a bad or revoked
  token. Re-copy it from the dashboard. Check `logs/zigbee.log` for
  `[cloudflared]` lines.
- **Tunnel runs but the hostname 404s** — the Public Hostname in the
  Cloudflare dashboard doesn't point at the right local port; it must
  match ZMM's `web.port`.
- **Remote users show as LAN / LAN-only users get in remotely** —
  header trust is misconfigured. With the managed tunnel this is
  automatic; for a manual setup ensure `cloudflare_tunnel_enabled: true`
  and that `trusted_proxies` covers the host cloudflared runs on.
