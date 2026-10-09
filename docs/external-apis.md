# External APIs

Everything under **Settings → APIs** in one place: what each integration
talks to, what it needs from you, where its secrets live, and where to read
more. Integrations with a deep design document link to it; Blueair and
HomeKit are documented in full here.

## The APIs tab

Each integration is a sub-tab. Use **Add API** (just **+** on a phone) to
choose the ones you want; the
choice is stored as `ui.enabled_apis` in `config.yaml`. Removing a tab
switches that integration off. Air Con and Fuel have no on/off switch, so for
them removing the tab only hides it. Until you choose, the tabs shown are the
integrations already enabled, plus those with no on/off switch.

There are two save paths:

- **The main Save** writes the structured config (Weather, the media engine,
  Energy, Security). Media changes take effect after a service restart.
- **The tab's own buttons** are used where a secret or a pairing is involved
  (Blueair, HomeKit, Air Con, Fuel). Those never travel with the main Save,
  and their changes apply immediately.

| Tab | Talks to | Local or cloud | Needs from you | Secrets live in | Deep doc |
| --- | --- | --- | --- | --- | --- |
| Weather | Open-Meteo | cloud, free | nothing | — | [daylight.md](daylight.md), [location.md](location.md) |
| Casting | Google Cast speakers; the media engine | local | nothing | — | [speaker_sync.md](speaker_sync.md), [open-zone.md](open-zone.md) |
| WiiM | WiiM / LinkPlay speakers | local | nothing | — | [speaker_sync.md](speaker_sync.md) |
| Sonos | Sonos speakers | local | nothing | — | [speaker_sync.md](speaker_sync.md) |
| AirPlay | HomePods, Apple TVs, AirPlay speakers | local | pairing, for receivers with access control | `config/secrets.yaml` → `airplay` | [speaker_sync.md](speaker_sync.md) |
| Radio Browser | radio-browser.info | cloud, free | nothing | — | [speaker_sync.md](speaker_sync.md) |
| Tidal | Tidal (unofficial client, personal use only) | cloud | Tidal login | `data/media/tidal/` | [speaker_sync.md](speaker_sync.md) |
| Air Con | Gree and Midea units | local | bind (Midea: one-time token fetch) | `config.yaml` → `ac.units` | [air-conditioning.md](air-conditioning.md) |
| Energy | Octopus Energy | cloud | API key and account number | `config.yaml` → `octopus` | [energy.md](energy.md) |
| Blueair | Blueair cloud (AWS) | cloud | Blueair app email and password | `config/secrets.yaml` → `blueair` | [below](#blueair) |
| HomeKit | HomeKit TVs (e.g. Sky Glass) | local | the code the TV shows when pairing | `data/homekit_pairings.json` | [below](#homekit) |
| Fuel | per-country price feeds; UK Fuel Finder | cloud | region; UK: Fuel Finder client id/secret | `config/secrets.yaml` | [journeys.md](journeys.md) |
| Security | Nuki bridge, Nuki/Yale over Matter | local | bridge host and token | `config.yaml` → `security` | [security.md](security.md) |

`config/secrets.yaml` and `data/homekit_pairings.json` are gitignored.
`config/secrets.yaml.example` documents every key, and environment variables
override that file where noted.

Devices from Air Con, Blueair, HomeKit and the Nuki bridge appear in the main
**Devices** list next to Zigbee and Matter devices. **Manage** on one opens
that integration's own control modal.

## Blueair

Blueair air purifiers and humidifiers, through Blueair's cloud account API via
`blueair_api`, the client behind the Home Assistant Blueair integration.
Blueair has no local protocol, so every read and write goes through Blueair's
servers and is subject to their rate limits (`modules/blueair_controller.py`).

### Account

Blueair accounts are run by Gigya (SAP Customer Data Cloud), and the library
logs in with **email and password only**.

- **Google or Apple sign-in.** These accounts have no password, and the login
  fails the same way a wrong password does. Set one in the Blueair app: sign
  out, choose **Forgot password**, enter the account's email, and set a
  password from the reset email. With Apple's *Hide My Email*, the account
  email is the `@privaterelay.appleid.com` address listed under Apple ID →
  Sign in with Apple → Blueair.
- **Region.** Accounts are per region (`eu`, `us`, `au`, `cn`). A correct
  password on the wrong region fails exactly like a wrong password.
- **Pending registration.** Blueair now requires a profile name on the
  account. Older accounts get a hidden Gigya "pending registration" error
  (`206001`) until that step is finished. `blueair_api` 1.56.3+ completes it
  automatically using the name already on the profile. If the profile has no
  name, set one in the app.

Credentials: `ZMM_BLUEAIR_USERNAME` / `ZMM_BLUEAIR_PASSWORD` take precedence;
otherwise `config/secrets.yaml` → `blueair`, which the Blueair tab writes as a
0600 file. The password is write-only: the API never returns it, and leaving
the field blank keeps the stored one. `config.yaml` holds only
`blueair: {enabled, region, poll_interval_seconds}`.

### Devices

Two device generations share one account and are normalised to one status
shape with a `capabilities` block, so the modal never branches on generation:

- **current** (HealthProtect, DustMagic, Blue Pure 311i+/411i+, humidifiers):
  power is `standby`, and fan speed is a per-model scale.
- **classic** (Classic 280i/480i/680i, Sense+): there is no standby, so fan
  speed 0 is off. Brightness is 0–4.

Reads are cached and polls are floored at 60 s, because Blueair rate-limits
aggressively. When a refresh fails, the last good reading is served, flagged
`stale`, rather than blanking the UI.

### API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/blueair/config` | enablement, region, account source; never the password |
| `POST /api/blueair/config` | save account / region / poll interval |
| `POST /api/blueair/test` | log in with the given (or stored) credentials and list devices |
| `GET /api/blueair/devices` | all devices with status |
| `GET /api/blueair/devices/{id}/status` | one device, `?max_age=` seconds |
| `POST /api/blueair/devices/{id}/control` | `{power, fan_speed_pct, auto, night_mode, child_lock, germ_shield, brightness}` |

## HomeKit

HomeKit accessories on the LAN, with the hub acting as the HomeKit
*controller*: the role an iPhone plays. Scope is televisions (HAP category 31),
such as Sky Glass. It uses `aiohomekit`, the library behind Home Assistant's
HomeKit Controller (`modules/homekit_controller.py`). Everything is local HAP
over IP, with no Apple account or cloud.

This is the opposite direction to exposing ZMM *to* Apple Home; that is not
implemented.

### Pairing

A HomeKit accessory belongs to one home. To pair it with ZMM it must be
**unpaired** (`sf=1` in its `_hap._tcp` advert): if it is in the Apple Home
app, remove it there first. Once ZMM has paired it, the Home app cannot add it
until ZMM unpairs.

1. Settings → APIs → **HomeKit** → enable.
2. **Scan network.** TVs on the same network are listed as *Pair*, *Paired
   here* or *Paired elsewhere*.
3. **Pair.** The TV shows an 8-digit code.
4. Type the code and press **Confirm**. Spaces and dashes are optional.

The code must be entered within five minutes, and a wrong code needs a fresh
**Pair**. Pairing mints long-term Ed25519 keys stored in
`data/homekit_pairings.json` (0600, gitignored). That file is included in
backups, because a restored hub without it cannot reach a TV that still
counts itself paired, and the TV will not accept a new pairing. It is
restored as 0600.

**Unpair** removes the pairing on the TV as well, so the TV can be paired
again by ZMM or Apple Home. If the TV is unreachable, the keys are forgotten
locally anyway and the UI warns you. In that case reset the TV's HomeKit
pairing from its own settings before pairing again.

### Discovery

Accessories are found over multicast DNS (`_hap._tcp`). The hub must be on the
same network segment as the TV. Some Wi-Fi links and VLAN boundaries drop the
TV's multicast adverts even when unicast queries work, so judge discovery
problems from the hub itself:

```sh
avahi-browse -rpt _hap._tcp
```

Once paired, aiohomekit follows the TV's adverts, so a DHCP address change is
picked up without re-pairing.

### Control

The TV's `/accessories` database is mapped to a normalised status with a
`capabilities` block. The remote renders only what the TV exposes.

| Control | HAP characteristic | Notes |
| --- | --- | --- |
| power | `Active` | Some TVs drop off the network in deep standby, so turning on from cold may need the TV's own network-standby setting |
| input | `ActiveIdentifier` | only inputs that are configured and visible are offered |
| remote keys | `RemoteKey` | arrows, select, back, exit, info, play/pause, rewind, fast-forward, next, previous |
| volume up / down | `VolumeSelector` | on the TelevisionSpeaker service |
| volume level | `Volume` | only if the TV exposes it |
| mute | `Mute` | |

Status is cached for 15 s, and the Devices list refreshes it at most every
30 s in the background. When the TV can't be reached, the last state is
served, flagged `stale`.

### API

Pairing endpoints need the `admin` scope; reads need `device:read` and
control needs `device:write`.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/homekit/config` | enablement and last error |
| `POST /api/homekit/config` | `{enabled}` |
| `GET /api/homekit/discover` | HomeKit accessories on the LAN, TVs first |
| `POST /api/homekit/pair/start` | `{id}`: the TV now shows its code |
| `POST /api/homekit/pair/finish` | `{id, pin}`: completes pairing, returns status |
| `DELETE /api/homekit/devices/{id}` | unpair; `accessory_confirmed` says whether the TV agreed |
| `GET /api/homekit/devices` | paired TVs with status |
| `GET /api/homekit/devices/{id}/status` | one TV, `?max_age=` seconds |
| `POST /api/homekit/devices/{id}/control` | `{power, input, key, volume_step: up\|down, volume, mute}` |

### Dependency pin

`aiohomekit` is pinned to 3.2.x. Version 4.x needs newer `cryptography` and
`zeroconf` than python-matter-server 7.x allows, the same constraint that pins
`pyatv`.
