# Beekeeper — DNS ad/tracker blocker

Beekeeper is ZMM's own **DNS sinkhole**, the same idea as Pi-hole or AdGuard
Home but built in-house so you own the engine: it answers DNS for your whole
network, quietly drops known ad/tracker domains, and forwards everything else to
a real upstream resolver. Point your router's DNS at the ZMM box and every
device — phones, TVs, IoT — gets ad/tracker blocking with nothing to install on
the clients.

It runs as a **separate always-on sidecar** (`python -m beekeeper`), so
restarting or upgrading the main ZMM app never takes household DNS offline.

- **Own engine.** The DNS server, blocklist matcher, forwarding resolver, stats
  and control API are all in-repo (`beekeeper/`), with **no third-party DNS
  library** — a small hand-rolled wire codec (`beekeeper/wire.py`) does the job
  because a forwarding sinkhole only ever has to *decode* queries and synthesise
  block answers; allowed traffic is relayed byte-for-byte.
- **Public list data, owned locally.** Blocklists are imported as *data* from
  public hosts-format sources into `data/beekeeper/lists/`, where they're plain
  files you can prune, disable, or override — exactly how Pi-hole/AdGuard seed
  their own defaults.

---

## How it works

```
        every device on your LAN
                 │ DNS
                 ▼
   ┌──────────────────────────────┐
   │  Beekeeper  <LAN-IP>:53       │
   │  ├─ blocked?  → 0.0.0.0 / ::  │  (or NXDOMAIN)
   │  └─ allowed?  → forward ──────┼──►  1.1.1.1 / 9.9.9.9
   │      (+ small cache)          │
   └──────────────────────────────┘
```

For each query Beekeeper checks the name (and every parent domain) against the
compiled block set. A hit is answered locally with the sinkhole address
(`0.0.0.0` / `::` by default, or NXDOMAIN); a miss is forwarded to the first
healthy upstream and the answer relayed back unchanged. A small positive cache
(fixed-TTL, id-rewritten per client) cuts repeat upstream traffic.

**Allowlist beats denylist beats blocklists.** An allowlist entry (exact or a
parent domain) always wins, so you can un-break a domain a public list is too
aggressive about.

---

## Enable it (no shell needed)

Enablement lives in the **ZMM Manager** (the always-on supervisor on `:8001`),
which owns container lifecycle. The day-to-day dashboard lives in the main app's
**Beekeeper** tab. Beekeeper ships with ZMM — no extra dependencies, it reuses
the app image's FastAPI/uvicorn/httpx.

1. Open the **ZMM Manager** (`https://<zmm-host>:8001`, or click the *ZMM
   Manager* link on the app's Beekeeper tab). Find the **Beekeeper** card and
   click **Enable**. The manager creates a `zigbee-matter-manager-beekeeper`
   container from the app's own image — reusing its `config.yaml` and data dir —
   on host networking with `Restart=always`. (The action needs the manager token,
   the same one the Manager uses for upgrades/rollback.)
2. Back in the app → **Beekeeper** tab → flip the **Enabled** switch. This binds
   `<LAN-IP>:53` and pulls the blocklists (a few seconds). The switch state
   persists across restarts.
3. Point your **router's DHCP DNS server** at the ZMM box's LAN IP, so every
   device resolves through Beekeeper. (Set it as the *only* DNS server — a
   secondary pointing elsewhere lets devices bypass blocking.)
4. Verify from another machine:
   ```bash
   dig @<LAN-IP> doubleclick.net     # blocked → 0.0.0.0 (or NXDOMAIN)
   dig @<LAN-IP> example.com         # allowed → real answer
   ```

The **Enabled** switch, any **Pause**, and the allow/deny lists all persist
across restarts (in `data/beekeeper/state.json` and the list files), so your
setup survives reboots and upgrades.

> **Running from source (not containerised)?** There's no manager container to
> click, so provision it directly: `sudo bash scripts/install_beekeeper.sh`
> (sets up the sidecar container + a systemd unit), or just run
> `python -m beekeeper` as root under your own process manager. Either way it
> reads the same `config.yaml` and writes to `data/beekeeper/`.

---

## The port 53 / systemd-resolved question

Most Linux hosts run `systemd-resolved`, but its stub listener normally binds
only `127.0.0.53:53` — **not** your LAN address. Beekeeper binds the host's
**LAN IP** on `:53` (auto-detected; override with `beekeeper.listen.address`),
so the two usually coexist with no changes.

**Opening the firewall (the easy way).** DNS on `:53` must be allowed through the
host firewall or other machines get *host unreachable*. The **ZMM Manager →
Beekeeper card** shows the firewall state and has an **Open :53** button that
opens `53/udp`+`53/tcp` for you across firewalld/ufw/nftables/iptables (it runs a
root host helper via the same mechanism as the OS-update actions). By hand:
`sudo firewall-cmd --permanent --add-port=53/udp --add-port=53/tcp && sudo firewall-cmd --reload`.

If Beekeeper reports it *couldn't bind :53*, something is holding the port on
all interfaces (occasionally resolved's stub is configured to `0.0.0.0`).
Disable **only** the stub listener — this does not stop resolved doing local
name resolution:

```bash
sudo mkdir -p /etc/systemd/resolved.conf.d
printf '[Resolve]\nDNSStubListener=no\n' | sudo tee /etc/systemd/resolved.conf.d/beekeeper.conf
sudo systemctl restart systemd-resolved
```

Revert by deleting that file and restarting `systemd-resolved`.

---

## Configuration (`config.yaml` → `beekeeper:`)

```yaml
beekeeper:
  enabled: false                 # bind :53 and start blocking at boot
  listen:
    address: ""                  # "" = auto-detect LAN IPv4; or pin one
    port: 53
  upstreams:                     # forwarded here in failover order
    - 1.1.1.1                    # Cloudflare
    - 9.9.9.9                    # Quad9
  upstream_timeout: 3.0
  sinkhole:
    mode: zero                   # zero = 0.0.0.0/:: ; nxdomain = reply NXDOMAIN
    ipv4: 0.0.0.0
    ipv6: "::"
    ttl: 60
  cache:
    enabled: true
    max_entries: 10000
    ttl: 300                     # fixed positive-cache cap (seconds)
  blocklists:
    - name: StevenBlack unified hosts
      url: https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts
      enabled: true
    - name: OISD small
      url: https://small.oisd.nl/domainswild
      enabled: true
  refresh_interval_hours: 24
  control: { host: 127.0.0.1, port: 8053 }
  log_queries: true              # per-query log for the dashboard
  query_retention_days: 7
```

`sinkhole.mode`: `zero` answers blocked A/AAAA queries with `0.0.0.0`/`::` (and
NODATA for other types) — clients fail fast without a "domain doesn't exist"
error. `nxdomain` replies NXDOMAIN for every type; a little cleaner, but some
apps retry harder on it.

**Blocklists** are any hosts-format (`0.0.0.0 ads.example.com`) or plain
domain-list source (one domain per line, `*.` wildcard prefixes allowed). The
`config.yaml` entries are just the initial seed — **manage lists from the
Beekeeper tab** (add a URL, toggle, or remove), which persists to
`data/beekeeper/sources.json` (authoritative once it exists). Lists refresh every
`refresh_interval_hours` and on demand via **Refresh now**.

### Recommended lists

The shipped defaults are a "block, don't break" pairing that's comprehensive
without breaking sites:

| List | URL | What it's for |
|------|-----|---------------|
| **HaGeZi Multi PRO** | `https://raw.githubusercontent.com/hagezi/dns-blocklists/main/hosts/pro.txt` | Ads + tracking + malware, tuned for low breakage (~540k domains) |
| **OISD Big** | `https://big.oisd.nl/domainswild` | Broad, conservative safety net (~330k domains) |

Good optional add-ons (paste into **Add list**):

- **HaGeZi Threat Intelligence Feeds** (malware/phishing/scam):
  `https://raw.githubusercontent.com/hagezi/dns-blocklists/main/hosts/tif.txt`
- **HaGeZi Multi ULTIMATE** (more aggressive — expect the odd false positive):
  `https://raw.githubusercontent.com/hagezi/dns-blocklists/main/hosts/ultimate.txt`

Avoid stacking the *parental-control / opinionated* lists (NSFW, anti-piracy,
DNS-bypass, safesearch-enforce, dating) unless you specifically want that
behaviour — they cause "why can't I reach X" complaints, not ad-blocking.

When a site breaks, use **Test a domain** (a real dig through the resolver — it
shows exactly what Beekeeper answers) to find the culprit, then add it to the
**Allowlist**.

---

## The dashboard

The Beekeeper tab shows live stats (queries, % blocked, clients, list size), a
24h blocked-vs-allowed chart, top blocked domains and top clients, a recent-query
stream, the blocklist table, and inline allow/deny editors. **Test a domain**
tells you whether a name would be blocked right now. **Pause** stops *blocking*
for 5/15/60 minutes while still resolving — handy when a site breaks and you want
to confirm Beekeeper is the cause.

Query logs and stats live in `data/beekeeper/stats.db` (SQLite, pruned to
`query_retention_days`). Set `log_queries: false` to keep only the in-memory
headline counters and log nothing per-query.

---

## Data & files

```
data/beekeeper/
  lists/                 fetched blocklists, one <slug>.domains file each
  lists/_meta.json       per-list name / count / last-fetched / error
  allowlist.txt          your always-allow domains (allowlist beats everything)
  denylist.txt           your always-block domains
  state.json             enabled / paused / master-switch, persisted
  stats.db               query log + aggregates (SQLite, WAL)
```

Everything under `data/` is yours and survives upgrades.

---

## Troubleshooting

- **Tab says "sidecar not reachable"** — the container isn't running. Check
  `sudo systemctl status zigbee-matter-manager-beekeeper` and
  `podman logs zigbee-matter-manager-beekeeper`.
- **Enabled, but nothing is blocked** — devices aren't using Beekeeper for DNS
  yet. Confirm the router hands out the ZMM box as the *only* DNS server, and
  that a device picked up the new lease (reconnect Wi-Fi). Many phones/TVs also
  hard-code DNS or use DoH — see below.
- **"could not bind DNS port"** — see the systemd-resolved section above.
- **A site is broken** — hit **Pause**; if it works while paused, add the
  domain to the **Allowlist**, then resume.
- **Devices bypass blocking with their own DoH/DoT** (some browsers, Android
  "Private DNS", smart TVs) — Beekeeper can't see queries that don't come to it.
  Block outbound :853 (DoT) and known DoH endpoints at your firewall/router if
  you need to force everything through Beekeeper.

## Credits

Default blocklist data comes from the community-maintained
[StevenBlack/hosts](https://github.com/StevenBlack/hosts) and
[OISD](https://oisd.nl/) projects. Beekeeper only ingests and serves that data —
please support those projects if you rely on them.

## Wire codec

Beekeeper is a *forwarding* sinkhole, which lets `beekeeper/wire.py` be a tiny,
fully-owned wire codec instead of a general DNS library:

- To decide whether to block, only the question section of an incoming query
  needs decoding — the transaction id, the first QNAME and its QTYPE.
- For a **blocked** name the whole response is synthesised here: a sinkhole
  A/AAAA record, or NXDOMAIN/NODATA. A fixed, simple shape.
- For an **allowed** name nothing is ever re-encoded. The raw query bytes are
  relayed to the upstream resolver and the upstream's raw response bytes are
  relayed straight back. Only the 2-byte id may be rewritten (`patch_id`) when a
  cached answer is reused for a new query.

So the module never has to serialise arbitrary upstream RRsets — the part of a
DNS library that is genuinely fiddly: name compression on write, every RR type,
EDNS option round-tripping. It stays a couple of hundred lines with no
third-party dependency, matching Beekeeper's "own the source" goal.

Wire format reference: RFC 1035 §4 (header, question, RRs, name compression).

## Upstream forwarding and cache

Unblocked queries are relayed verbatim to the configured upstream resolvers —
Cloudflare 1.1.1.1 and Quad9 9.9.9.9 by default — with failover: each is tried
in turn until one answers within the timeout. The upstream's response bytes are
returned unchanged; an answer is never re-encoded.

A tiny LRU cache holds recent UDP answers keyed by `(qname, qtype, qclass)`. It
uses a single fixed TTL cap rather than parsing per-record TTLs from the wire —
a deliberate simplification for a home resolver. At worst a name is served from
cache for up to `cache.ttl` seconds (default 300) past its real TTL. Cached
answers are reused across clients by rewriting the 2-byte transaction id.

Only UDP answers are cached, and only the UDP path reads the cache, so a large
TCP-only answer is never squeezed back out over UDP.

## Blocklist ingest and matching

**Ingest.** Public hosts-format and domain-list sources are fetched over HTTPS
with the standard library — no third-party HTTP client, keeping the sidecar
dependency-free and the source fully owned — parsed into bare domains, and
cached as plain `.domains` files under `data/beekeeper/lists/`. **Those files are
yours to edit**; a compile step unions the enabled ones into the live match set.

**Match.** A query name is blocked when it, or any parent domain, is in the block
set, **unless** an allowlist entry (exact or parent) covers it. The user denylist
is an always-on block set layered on top of the fetched lists. Matching is a
short walk up the label suffixes, which is plenty fast at household query rates.
