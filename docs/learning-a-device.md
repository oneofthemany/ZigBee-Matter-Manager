# Teaching ZMM a new device

When a device joins that ZMM has never seen, it still works, but some of
what it can do may show up as plain numbers with codes for names, or not
show up at all. *Learn this device* fixes that: ZMM asks the device what it
has, you show ZMM what a few things mean, and the result is saved so every
device of that model works the same way from then on.

You do not need to know anything about Zigbee to do this.

---

## Before you start

- Pair the device and give it a minute to settle.
- Have the device in front of you. Some steps ask you to press its button,
  walk past it, or plug something into it.
- If it runs on batteries, it is asleep most of the time. Whenever a step
  says *wake it*, press its button once and start the step straight away.

## The simple way

Open the device, go to the **Identity** tab and press **Learn this device**.
The **Simple** view lists only the steps you need, in order. Work down it:

1. **Find everything.** Press the device's button if it runs on batteries,
   then **Start**. ZMM asks the device what it has, reads every value, and
   checks which ones can be changed. It takes up to a couple of minutes; a
   spinner shows while it works. Nothing about how the device behaves is
   changed. When it finishes it tells you how many values it found and how
   many can be changed.
2. **The steps for this kind of device.** These depend on what the device
   is, and each tells you exactly what to do. For example:
   - a presence or motion sensor: *Walk in*, then *Walk out*;
   - a button or remote: *Press the button* (once per kind of press);
   - a smart plug: *Load test* (plug in something you know the wattage of).

   Press **Start**, do what it says, and wait for the step to finish. ZMM
   shows what it found. Proposals it is sure of are already ticked; press
   **Accept selected**. If it found nothing, it says why: do it again with
   **Redo** (or **Run again**).
3. **Values I know** (optional). If the device's own app, screen or manual
   shows you some of its settings, type them in as `name: number` pairs,
   for example `absence delay: 30, sensitivity: 2`. ZMM finds where each is
   kept and offers it as a setting. Where two of the device's values match
   the same number, it lists both instead of guessing.
4. **Review & save.** ZMM shows what saving will change. Press **Save**. It
   applies to every device of this model on your system. **Roll back my
   profile**, in the same place, undoes the last save.

That is all most people need.

## What you get

- **Readings by name** in the device card and in Home Assistant (for
  example *Occupancy* instead of `opple 0x0142`).
- **Settings** in the device's **Overview → Configuration**, under **Device
  Settings**. Ones that have a name are under **Named**. Everything else the
  device lets you change is under **Unnamed**, labelled by its code until
  something names it: you can still change it there.
- The battery level, wherever the device keeps it.

Saving in Configuration only sends the settings you changed.

## If something goes wrong

| What you see | What to do |
|---|---|
| *the device did not answer: wake it* | Press the device's button and press **Start** again within a second or two. |
| *no two-state reading changed* (presence) | Make sure nobody was in view before you started, then try again. |
| *nothing switched* or *no button press arrived* | Use the device itself, not its app, while the step is running. |
| *more than one fit, so not proposed* | Two of the device's values hold the same number. Change the setting on the device if you can, then run *Values I know* again with the new number. |
| A reading stays at its old value | Make the device report again (trigger it once). Values a device has not sent since it last joined appear when it next sends them. |

## What ZMM cannot do

A device tells ZMM *that* a value exists, what kind of number it is, and
whether it can be changed. It never says what the value is *for*. Names come
from you: from a step you demonstrate, or from values you type in. A value no
one has named still works under **Unnamed**; it just keeps its code as a name.

---

## Advanced

Switch to **Advanced** at the top of the learning panel to see every step,
grouped under a tab per kind of step.

### Find everything, in parts

*Find everything* is two steps you can also run on their own:

- **Find its attributes** asks the device which of its maker's attributes it
  has (with their types and whether each is listed as writable) and reads
  them. Some devices report attributes they leave out of that list, often
  their real functions; those are read too and reported as *not listed*.
- **Check which can be set** settles, for each of those unlisted attributes,
  whether it can be written, the only way the device allows: it reads the
  value, writes the same value back, and notes whether the device accepts.
  Only number and on/off attributes are tried, never ones known to start an
  action. Silence records nothing, so a sleeping device cannot produce a
  wrong answer.

### Device settings steps

- **Look back over its history**: proposes attributes that changed every
  time a known value did (a target temperature, say). Nothing to do; it gets
  better the longer the device has been in use.
- **Change an on/off setting on the device** / **Set a number on the
  device**: change one setting on the device itself and ZMM finds which
  attribute moved, with its scale.
- **Let ZMM flip one (visible / behaviour)**: for settings you cannot change
  on the device, ZMM flips one candidate at a time while you watch, and puts
  it back when you answer or after a timeout.

### Editing the entry directly

**Review & save** (or **Edit its entry** outside a session) opens the entry
editor: per endpoint, its type, name, what its power reading measures and
whether it sends button presses; measurement scaling; and **blob tags**.

Blob tags are values some makers (Aqara) pack into one status report. The
table lists every tag the device has sent with its last value, so you can
match one against something you know (a battery at 3.2 V reads `3211` in
millivolts) and name it. Give it a scale to turn the raw number into its
unit (`0.001` for mV to V). An empty name drops a tag.

The JSON box below the form holds the whole entry, including settings the
form does not cover.

### Your edits and the shipped entry

If ZMM already ships an entry for the model, what you save is laid over it:
what you set wins, what you left alone keeps the shipped value. Deleting your
version (rolling back past your first save) returns to the shipped entry.

### Sharing

**Export** writes the entry with a summary of what settled each part, and no
addresses or file names. **Import** previews someone else's export for your
model and saves it if you choose. A well-tested export can become a shipped
entry in `zmm_quirks/`.

### For developers

The design is in [`docs/plans/device-learning.md`](plans/device-learning.md)
(steps, recipes, inference operations) and
[`docs/device-profiles.md`](device-profiles.md) (entries, overlays, battery,
the unnamed-attribute controls). Recipes are JSON in `learning_recipes/`; a
user's own go in `data/learning_recipes/`.
