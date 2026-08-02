# Notifications, Requests & Web Push

Three related things, easiest understood by what each can reach:

| | Reaches | Needs |
|---|---|---|
| **In-app / browser notifications** | a browser with ZMM open | notification permission |
| **Web Push** | a device with the screen off | permission **+ a trusted HTTPS address** |
| **Requests** | a person, and tells the sender if ignored | either channel above |

---

## ⚠️ Push does not work on your LAN address

**This is the single most common reason notifications appear broken.**

Browsers only allow service workers and notifications in a *secure context*.
ZMM's LAN address (`https://192.168.1.x:8000`) uses a **self-signed
certificate**, and an origin with a certificate error is **not** a secure
context — no matter that the URL says `https`, and no matter that you clicked
through the warning.

On that address:

- the service worker will not register,
- notification permission will not stick,
- push subscriptions cannot be created.

**Use your public/tunnel address** (see [remote_access.md](remote_access.md)).
A Cloudflare Tunnel gives you a publicly-issued certificate, which is a proper
secure context, and everything above works.

The alternative — installing the hub's own certificate as a trusted CA on every
device — works but must be repeated per device and redone whenever the
certificate is regenerated.

ZMM detects this and says so rather than failing silently: enabling
notifications on an untrusted address shows *"Notifications need a secure
connection"* with the remedy, and the notification settings dialog shows the
same warning.

> **This is also why enabling notifications is not part of first-run setup.**
> The setup wizard is LAN-only by design, which is exactly where notifications
> cannot be enabled. Enable them later, from your tunnel address.

---

## Requests — asks that need an answer

A notification is fire-and-forget: nobody learns whether it arrived. A
**request** is addressed to a person, expects accept or decline, and escalates
to the sender when neither comes.

```
Sean reaches the shops  ->  "Get milk?" sent to Alex
Alex accepts            ->  Sean sees it was accepted
nobody answers in 20m   ->  Sean is told it lapsed
```

### State is authoritative; delivery is not

A request exists and expires identically whether or not any notification
arrived. A phone that was off, a browser that was closed, and a person who
ignored it all end in the same place — which is what makes the escalation
trustworthy. A failed push never rolls back a request.

### Answering

- from the **header badge** in ZMM (persists until answered, shows time left),
- or straight from the **push notification**, which carries Accept / Decline
  buttons — no need to open the app.

Answering something that already expired returns `409` and says so, rather
than leaving a button that appears to work.

### From an automation

```yaml
type: request
to_user: alex
message: "Get milk?"
timeout_s: 1200        # 20 minutes
from_user: sean        # optional; defaults to "zmm"
```

The rule does **not** wait for the answer. Blocking a sequence for twenty
minutes would tie up the engine and make the outcome depend on the hub staying
up; the escalation closes the loop instead.

### API

| | |
|---|---|
| `GET /api/requests` | yours (sent or received); admin sees all |
| `POST /api/requests` | create one |
| `POST /api/requests/{id}/accept` | accept — addressee only |
| `POST /api/requests/{id}/decline` | decline — addressee only |
| `POST /api/requests/sweep` | force an expiry pass (admin; useful for testing escalation) |

Unanswered requests expire after their timeout; answered ones are kept 24h so
the UI can show what happened, then dropped. This is a scratchpad for asks in
flight, not a message archive.

---

## Web Push

### What the push service can see

Delivery goes through the browser vendor's push service (Google, Mozilla,
Apple). That is unavoidable — the endpoint is issued by the browser.

It is **not** a plaintext exposure. Payloads are encrypted per
[RFC 8291](https://www.rfc-editor.org/rfc/rfc8291) with keys only the
subscriber's browser holds, so the relay carries ciphertext it cannot read. It
learns that a message went to a device, and roughly how big it was. Nothing
else — not the title, not the body, not who it concerns.

[VAPID](https://www.rfc-editor.org/rfc/rfc8292) is the other half: the hub
signs each push with a key pair it generates once, so a stranger holding a
stolen endpoint cannot push to your devices.

### Setup

1. Open ZMM on your **public/tunnel address**.
2. Bell icon → enable notifications, and accept the browser prompt.
   The subscription registers automatically.
3. Verify with `POST /api/push/test` — it sends a push to your own account.

On **iOS**, add ZMM to the home screen first; Safari does not permit web push
from a normal browser tab.

### Keys and subscriptions

The VAPID identity lives in `data/vapid.json` (mode `0600`), generated once.

> **Do not delete it.** Browsers bind a subscription to the key it was created
> with, so a new identity invalidates every existing subscription and every
> device must re-subscribe. If the file is unreadable ZMM refuses to start push
> rather than quietly generating a new one.

Subscriptions live in `data/push_subscriptions.yaml` (mode `0600`) and contain
per-device secrets. They are never returned by the API — `GET
/api/push/subscriptions` lists devices without their keys.

Subscriptions are removed automatically when a push service reports them gone
(`404`/`410`), or after 10 consecutive failures.

### API

| | |
|---|---|
| `GET /api/push/key` | the applicationServerKey a browser needs to subscribe |
| `POST /api/push/subscribe` | register this browser against your account |
| `GET /api/push/subscriptions` | your devices (admin: everyone's), keys stripped |
| `DELETE /api/push/subscriptions/{id}` | remove one |
| `POST /api/push/test` | send yourself a test push |

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| "Notifications need a secure connection" | You are on the LAN self-signed address. Use the tunnel URL. |
| Toggle enables, nothing arrives | Permission granted but no push subscription — usually the same cause. Check `GET /api/push/subscriptions`. |
| Works with ZMM open, not when closed | No push subscription; only the in-app channel is active. |
| Nothing on iOS | ZMM must be installed to the home screen first. |
| `POST /api/push/test` says no subscriptions | That account has not registered a device. Enable notifications in a browser signed in as that account. |
| Requests expire though the phone was on | Push not configured for the recipient — the ask still lapses and the sender is told, which is the intended honest outcome. |
| Accept from the notification does nothing | Session cookie expired; open ZMM and sign in again. |

## Secure context is the usual blocker

`static/js/pwa.js` checks `window.isSecureContext` **first**, before feature
detection. The `Notification` and `serviceWorker` APIs exist on an insecure
origin — they are simply refused at the point of use — so testing for their
presence reports "full support" and delivery then fails with nothing to
explain it.

This is the common case here, not an edge case. Reaching the hub at
`https://192.168.1.x` with its self-signed certificate is a cert error, and a
cert-error origin is **not** a secure context: service worker registration is
blocked and notification permission does not stick. Reached through the tunnel,
with a publicly-issued certificate, everything works.

## Permission vs subscription

These are distinct, and requests need the second:

- `Notification.permission` lets the **page** raise a notification while it is
  running.
- A push **subscription** lets the **hub** raise one when nothing is open.

The whole point is reaching someone who is not looking at ZMM. On the LAN
self-signed address the service worker will not even register, so
`subscribeToPush()` is a no-op there — use the public/tunnel URL.

The subscription is re-asserted on every page load. It otherwise ran only at
the moment permission was granted, so a subscription the browser rotated, a hub
that was rebuilt, or a permission granted before the subscribe code shipped all
left the device silently unreachable while the local test still passed.

## `sendNotification` return value

Returns `false` **only** when the channel is switched off, so a caller can fall
back to its own in-app alert. Callers used to get that fallback for free,
because the function did not exist on pages that never loaded `pwa.js`; now
that it always exists, "disabled" has to be reported rather than silently
swallowed, or every notification rule goes quiet the moment the master toggle
is off.

A suppressed duplicate returns `true` — it was handled, and being quiet is the
point.

## Notification rules

`static/js/notifications.js` backs Settings → Notifications. Users create rules
that fire browser or in-app notifications on device events. Rules live in
`localStorage` so they survive reloads, and delivery goes through
`window.zbmSendNotification` (set up in `pwa.js`) so the service-worker /
native / in-app fallback behaviour comes for free.

`initNotifications()` is called once at boot from `main.js`; the rules list
renders on the sub-tab's `shown.bs.tab`; and a 5-second poll over
`window.state.deviceCache` evaluates the rules.

The rule engine is independent of the four hard-coded toggles in `pwa.js`,
which continue to work via the navbar bell. This module adds *per-device* and
*per-event* rules with cooldowns, time windows and condition logic.

## Web Push

Everything else in this codebase notifies a browser that is already running.
Web Push (`modules/webpush.py`) is the only mechanism that reaches a device with
the screen off, and it is what makes a request ("get milk?") arrive when it
matters rather than when someone next opens the page.

### What the relay can see

Delivery goes through the browser vendor's push service (Google, Mozilla,
Apple). That is unavoidable — the endpoint is baked into the subscription the
browser issues. It is also not a plaintext exposure: RFC 8291 encrypts the
payload with keys only the subscriber's browser holds, so the relay carries
ciphertext it cannot read. It learns that a message went to a device, and its
size. Nothing else.

VAPID (RFC 8292) is the other half: the hub signs each request with a key pair
it generates once, so the push service can attribute traffic to this server and
a stranger cannot push to your subscribers using a stolen endpoint.

### Why implemented here rather than via pywebpush

This is a self-contained transform — ECDH, HKDF, one AES-GCM seal — not a
stateful protocol with sessions and ratchets. Implementing it over vetted
primitives (`cryptography`) is standard practice and costs one dependency
instead of three. The round-trip is unit-tested by decrypting our own output,
which is the property that actually matters.

## Messages

`modules/messages_store.py` backs person-to-person conversations inside ZMM:
plain threads between two users, with history, unread counts, and delivery that
actually lands. Every message goes out over the websocket for anyone with the
app open **and** as a web push for everyone else, and the push wakes the phone.
(Subscription requires the app to have been opened once on a
trusted-certificate origin — see [Web Push](#web-push).)

Automations send through the same store, via the rule builder's "Message" step,
so "user 1 arrived at the shops" and "get some milk" travel the same road and
appear in the same thread.

**Privacy.** A conversation belongs to its two participants. The API never lets
a third user read it — *including admins*. What an admin could do with database
access is not something the API should normalise.

**Storage.** `data/messages.duckdb`, dedicated to this module, all access through
one worker thread that owns the connection.

## Messages API

Access is **strictly participant-only**. Every endpoint resolves "me" from the
authenticated principal and only ever returns threads and messages that
principal is part of.

There is deliberately **no admin read-everything view**: a household chat where
the admin can browse everyone's messages through the API is not a chat anyone
would use for anything real.

Sending as someone else is equally off the table — `from_user` is always the
authenticated username. Automations that speak on the system's behalf go through
the store directly with `source="automation"`.
