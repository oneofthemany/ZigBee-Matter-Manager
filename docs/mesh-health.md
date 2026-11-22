# Mesh health

How ZMM keeps battery devices on the mesh when the router they talk through
goes away, and what it records so a device that went silent can be explained.

A battery (sleepy) end device talks through one parent router. When that
parent stops answering (switched off at the wall, a socket bar turned off),
the device should rejoin through another router: an encrypted rejoin with the
network key it holds, which the coordinator accepts. It can still end up
stranded:

- no router in range has a free child slot;
- its firmware gives up: Aqara devices tend to wait for their old parent, and
  some Hue firmware does a full leave after failed rejoins, deleting the key;
- the coordinator refuses it (below).

## 1. Every join verdict on record

The EZSP radio reports each join, rejoin and leave it handles to bellows'
`_handle_tc_join_handler`, which drops a refused one silently.
`modules/join_audit.py` wraps that callback when the network starts, so each
event is logged and put in the logbook (category `connection`) before bellows
sees it:

    [ieee] (name) <join | secured rejoin | unsecured rejoin | leave> via <parent> as <nwk>: accepted | REFUSED by the coordinator (<decision>)

A refusal is a warning. *Secured rejoin via a new parent* is the mesh healing
itself; *unsecured rejoin … REFUSED* is a device that lost its key being
turned away; neither line at all means the device never tried. Only EZSP
radios report these; other radio libraries are left alone.

`logs/zigbee.log` rotates at 10 MB with 5 backups, about two days on a busy
hub. ZMM Manager lists and streams the rotations (`zigbee.log.1` …).

## 2. The coordinator's rules for sleeping children

- **Child timeout.** The coordinator drops a sleeping child it has not heard
  from within `CONFIG_END_DEVICE_POLL_TIMEOUT`. bellows defaults it to 8
  (2^8 = 256 minutes); ZMM sets 14 (2^14 minutes, about 11 days), the radio's
  longest (`core/config_builder.EZSP_DEFAULTS`). A `CONFIG_*` key under the
  zigbee config's `ezsp: config:` overrides it. A router parent applies its
  own timeout.
- **Rejoin policy.** bellows runs trust-center policy `0x01`: joins are allowed
  (only while joining is open) and unsecured rejoins are refused, except
  during a permit-join window (bellows adds `ALLOW_UNSECURED_REJOINS` for its
  length) and for 90 s after start for rejoins using the well-known key. An
  unsecured rejoin asks for the network key in the clear under the device's
  claimed address, and the key goes back encrypted only with the public
  default link key most devices use, so allowing it always would hand the key
  to anything in range that claims a known address. ZMM keeps the default.
