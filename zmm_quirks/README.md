# ZMM quirk entries

One JSON file per device model, in the device-profile schema plus the `zmm`
block (`docs/plans/zmm-quirks.md` §6). Loaded as the `zmm` tier: they
override community profiles and are overridden by a user's own.

Every value is traced to one of our probes or a person's confirmation
(`zmm.evidence`); nothing is copied from zhaquirks or Zigbee2MQTT. Start an
entry from a device's Identity tab (*Draft ZMM entry*), settle what the
evidence could not, then add it here.
