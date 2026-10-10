# House mode & alarm

The header shows the house mode and, once the alarm has zones, its state. Tap
it to change the mode, arm, or disarm. Setup is in **Settings → Security**
(admin).

| Path | Role |
|---|---|
| `modules/house_mode.py` | which worker is the house mode; presence follow |
| `modules/alarm.py` | the alarm state machine, PINs, sirens, alerts, the rule device |
| `routes/house_routes.py` | API |
| `static/js/house-badge.js` | header badge and panel |
| `static/js/alarm-settings.js` | Settings → Security setup |
| `data/house_mode.json`, `data/alarm.json` | config and state; `alarm.json` holds PIN hashes (0600) |

---

## House mode

House mode is not a new kind of state — it is a **Mode worker**
([workers.md](workers.md)) that the household has designated. The default is
`house_mode` with home / away / night / holiday, the same worker the swarm's
suggestions already create and use, so anything built on it keeps working.
Rules read it, test it and set it exactly as before; this layer adds three
things:

- **The header switch**, for anyone with `device:write` — the same scope that
  sets any worker.
- **Following presence** (off by default). When every tracked person has been
  *away* for the configured minutes, the mode goes **away** — but only from
  **home**: night and holiday are choices someone made. When anyone arrives, an
  **away** or **holiday** house goes **home**. A phone in state *unknown* is
  neither home nor away (see `HouseholdDevice`), so a flat battery never empties
  the house.
- **The link to the alarm**, below.

Any Mode worker can be designated; its options are matched case-insensitively,
and presence follow only acts if it has `home` / `away` options.

## Alarm

### States

```
disarmed ──arm──▶ arming ──exit delay──▶ armed_home | armed_away | armed_night
                                                │
                              zone trips ───────┤
                                                ▼
                       entry zone: pending ──entry delay──▶ triggered
                       instant zone: ─────────────────────▶ triggered
                                                                │
                                         siren time ends ───────┘──▶ armed (still)
disarm (PIN) from any state ──▶ disarmed
```

- **Zones** are sensors, each watched in the armed modes you tick. An *entry*
  zone (the door you come in by) gives the entry delay; every other zone
  triggers at once. A zone trips on a contact opening, motion/occupancy, or
  tamper — so a camera with ONVIF motion is a zone too ([cameras.md](cameras.md)).
- **The exit delay** is per mode (default 60 s away, none for home and night).
  Nothing trips while it runs — leaving the house trips sensors.
- **Triggered** switches the sirens on, and alerts everyone (or the chosen
  people) urgently: Web Push plus every Other channel they have
  ([notifications.md](notifications.md) §Other channels) unless they unticked
  *Alarm* there. After the siren time the sirens go off and the alarm stays
  armed; the cause stays on record until disarmed.
- **Arming with something open** is refused with the list. *Arm anyway*
  bypasses those sensors until they report closed, then watches them again.
- **Restarts**: state is saved on every change with wall-clock deadlines, so an
  armed house is still armed after an upgrade, and a delay that ran out while
  the hub was down has ended.
- **Sirens** are any devices with on/off — a siren plug, a Zigbee siren that
  exposes on/off. IAS WD warning modes (strobe/tone patterns) are not driven.

### Who can do what

| Action | Needs |
|---|---|
| See the alarm | `security:read` |
| Arm | `security:write` (+ own PIN if *arming needs a PIN*) |
| Disarm | `security:write` **and** the caller's own PIN |
| Set your own PIN | `security:write`; changing it needs the current one |
| Setup, clear someone's forgotten PIN | admin |

The alarm sits with locks under `security:*`: disarming it is the same kind of
act as unlocking a door.

**Only a PIN disarms.** That is the rule everything else is built around:

- A house-mode change can **arm** (away/holiday → armed away, night → armed
  night, with *House mode arms the alarm* on) but never disarm. Otherwise
  anyone able to flip a worker — or any rule that sets one — could switch the
  alarm off.
- Arming and disarming set the house mode (arm away → away, disarm → home from
  away or night). An arm caused by the house mode doesn't set it back, so the
  two never ping-pong.
- If a house-mode arm is blocked by an open door, everyone is told why rather
  than the house quietly staying disarmed.

PINs are 4–8 digits, stored as PBKDF2 hashes (the same as account passwords),
checked off the event loop. Five wrong PINs lock that person out for five
minutes; the right PIN doesn't get through during the lockout.

### In rules

The panel is a device, `alarm::panel`, merged into the engine like workers:

- **Trigger / condition** on `state` (`disarmed`, `arming`, `armed_home`,
  `armed_away`, `armed_night`, `pending`, `triggered`), or the 0/1 attributes
  `armed` and `triggered`. "When the alarm is triggered, turn every light on" is
  an ordinary rule.
- **Command** `set` with `armed_home` / `armed_away` / `armed_night` arms it.
  `disarmed` is refused — and not offered in the rule builder — unless the admin
  turns on *Let automations disarm*, which hands that power to anyone who can
  edit rules.

### API

| | |
|---|---|
| `GET /api/house/mode` | mode, options, presence-follow settings |
| `POST /api/house/mode` | `{mode}` |
| `PUT /api/house/mode/config` | `{worker, follow_presence, away_after_minutes}` (admin) |
| `POST /api/house/mode/config/create-worker` | create or adopt `house_mode` (admin) |
| `GET /api/alarm` | state, countdown, cause, zones (online/open/bypassed), `have_pin` |
| `POST /api/alarm/arm` | `{mode, pin?, force?}`; 409 with `open` when something is open |
| `POST /api/alarm/disarm` | `{pin}` |
| `POST /api/alarm/pin` | `{pin, current?}` — your own |
| `DELETE /api/alarm/pin/{user}` | clear a forgotten PIN (admin) |
| `GET` / `PUT /api/alarm/config` | zones, sirens, delays, who is alerted, the rule switches (admin); lists who has a PIN, never a hash |

Live changes go out on the websocket as `alarm_state`.
