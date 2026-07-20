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
