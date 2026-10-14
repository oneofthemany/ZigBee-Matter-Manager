"""
Inference operations for device learning (docs/plans/device-learning.md §5).

Pure functions over the frames captured while the user carried out one step.
Each returns proposals: a path in the entry, a value, a confidence and the
evidence in words. A recipe names the operations; it cannot run code.

A capture record: {"t", "ep", "cluster", "attr", "mfr", "value"}. `baseline`
holds each (ep, cluster, attr, mfr)'s last value before the step began.
"""
from __future__ import annotations

import re

from typing import Any, Callable, Dict, List, Optional, Tuple

from modules.device_facts import mfr_hex, vendor_scoped

Key = Tuple[int, int, int, Optional[int]]

POWER = (0x0B04, 0x050B)
ON_OFF = (0x0006, 0x0000)
PRESS = (0x0012, 0x0055)
BLOB_ATTRS = (0x00F7, 0x00DF)
AQARA = 0xFCC0

# A power reading "moved" if its change is at least this share of the largest
# change on the device during the step: well above standby noise (a charging
# controller against a kettle), well below a real share of the load.
MOVED_SHARE = 0.1
# EPs whose moves agree this closely report the same (whole-device) figure.
SAME_FIGURE = 0.15
# A known rating and a measured draw differ (element tolerance, mains voltage);
# a scaling error is a power of ten, so this tolerance cannot straddle two.
KNOWN_TOLERANCE = 0.25
MAINS_V = (200.0, 255.0)


def proposal(path: str, value: Any, confidence: str, evidence: str) -> Dict[str, Any]:
    return {"path": path, "value": value, "confidence": confidence, "evidence": evidence}


def _key(r: Dict[str, Any]) -> Key:
    return r["ep"], r["cluster"], r["attr"], r.get("mfr")


def _in(r: Dict[str, Any], cluster_attr: Tuple[int, int]) -> bool:
    return (r["cluster"], r["attr"]) == cluster_attr


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def which_endpoint_changed(records, baseline, step, ctx) -> List[Dict[str, Any]]:
    """The EP whose On/Off changed during the step drives what the user operated:
    a load (`switch`) unless the step says it is a lamp (`expect_kind`)."""
    watch = step.get("watch_ca") or ON_OFF
    changed = sorted({r["ep"] for r in records if _in(r, watch)
                      and baseline.get(_key(r)) is not None and r["value"] != baseline.get(_key(r))}
                     | {r["ep"] for r in records if _in(r, watch) and _key(r) not in baseline})
    if not changed:
        return [proposal("", None, "none", "nothing switched: was the right control used?")]
    kind = step.get("expect_kind") or "switch"
    return [proposal(f"endpoints.{ep}.kind", kind, "high",
                     f"EP{ep} switched when {step['label']} was operated") for ep in changed]


def press_signature(records, baseline, step, ctx) -> List[Dict[str, Any]]:
    """The value each EP's button sends for the press the user demonstrated."""
    press = (step.get("inputs") or {}).get("press_type")
    seen = [(r["ep"], r["value"]) for r in records if _in(r, PRESS)]
    if not seen or not press:
        return [proposal("", None, "none", "no button press arrived")]
    out = []
    for ep in sorted({ep for ep, _ in seen}):
        values = sorted({v for e, v in seen if e == ep and _num(v) is not None})
        out.append(proposal(f"endpoints.{ep}.actions", "multistate", "high",
                            f"EP{ep} sent a press"))
        if len(values) == 1:
            out.append(proposal(f"zmm.press_names.{int(values[0])}", press, "high",
                                f"a {press} press on EP{ep} sent {int(values[0])}"))
        else:
            out.append(proposal("", None, "low", f"EP{ep} sent several values {values} "
                                                 f"for one {press} press: repeat it once"))
    return out


def _power_moves(records, baseline) -> Dict[int, float]:
    """Largest change of each EP's power reading during the step."""
    moves: Dict[int, float] = {}
    for r in records:
        if not _in(r, POWER) or _num(r["value"]) is None:
            continue
        before = _num(baseline.get(_key(r))) or 0.0
        moves[r["ep"]] = max(moves.get(r["ep"], 0.0), abs(r["value"] - before))
    return moves


def which_endpoints_moved(records, baseline, step, ctx) -> List[Dict[str, Any]]:
    """What each EP's power reading measures, from which EPs moved when a load
    was put on the step's EP."""
    target = step.get("ep")
    moves = _power_moves(records, baseline)
    top = max(moves.values(), default=0.0)
    if top <= 0:
        return [proposal("", None, "none", "no power change seen: was the load switched on?")]
    moved = sorted(ep for ep, m in moves.items() if m >= MOVED_SHARE * top)
    shown = ", ".join(f"EP{ep}" for ep in moved)
    if moved == [target]:
        return [proposal(f"endpoints.{target}.metering", "self", "high",
                         f"only {step['label']}'s reading moved")]
    if len(moved) > 1 and (max(moves[e] for e in moved) - min(moves[e] for e in moved)) \
            <= SAME_FIGURE * top:
        total, rest = moved[0], moved[1:]
        return [proposal(f"endpoints.{total}.metering", "device_total", "medium",
                         f"{shown} all moved by the same amount for one load: each reports the "
                         f"whole device")] + \
               [proposal(f"endpoints.{ep}.metering", "none", "medium",
                         f"EP{ep} repeats EP{total}'s whole-device figure") for ep in rest]
    if target not in moved:
        return [proposal(f"endpoints.{target}.metering", "none", "medium",
                         f"a load on {step['label']} showed on {shown}, not on EP{target}")]
    return [proposal("", None, "low", f"{shown} moved by different amounts: repeat with a "
                                      f"steadier load")]


# Below this a switched-off socket counts as drawing nothing (standby, noise).
STOPPED_W = 1.0


def _watts(ctx, ep: int, raw: float) -> float:
    mult, div = (ctx.get("power_scale") or {}).get(ep, (1, 1))
    return raw * mult / div


def power_follows_switch(records, baseline, step, ctx) -> List[Dict[str, Any]]:
    """With a load on the step's EP switched on then off, its reading must rise
    and fall with it, and no other EP's may: an EP that also shows the load
    would record it twice in history, and an automation waiting for that EP's
    power to stop would wait on another socket's load. Metering proposals come
    from which_endpoints_moved; this adds what the switching showed."""
    target = step.get("ep")
    label = step.get("label") or f"EP{target}"
    notes: List[Dict[str, Any]] = []
    switched = sorted((r for r in records if _in(r, ON_OFF) and r["ep"] == target),
                      key=lambda r: r["t"])
    on_at = next((r["t"] for r in switched if bool(r["value"])), None)
    off_at = next((r["t"] for r in switched if on_at is not None and r["t"] > on_at
                   and not bool(r["value"])), None)
    moves = _power_moves(records, baseline)
    top = max(moves.values(), default=0.0)

    if on_at is None:
        notes.append(proposal("", None, "low", f"{label} was not switched on during the step: "
                                               "judged from the readings alone"))
    else:
        peak = max((_num(r["value"]) or 0 for r in records if _in(r, POWER) and r["ep"] == target
                    and r["t"] >= on_at and (off_at is None or r["t"] <= off_at)), default=0.0)
        if peak <= 0:
            notes.append(proposal("", None, "low", f"{label} switched on but its own reading never "
                                                   "rose: is the load plugged into it?"))
        else:
            notes.append(proposal("", None, "high", f"{label} rose to {_watts(ctx, target, peak):g} W "
                                                     "while switched on"))
        if off_at is not None:
            stopped = next((r["t"] for r in sorted(records, key=lambda r: r["t"])
                            if _in(r, POWER) and r["ep"] == target and r["t"] >= off_at
                            and _watts(ctx, target, _num(r["value"]) or 0) <= STOPPED_W), None)
            notes.append(proposal("", None, "high" if stopped else "low",
                                  f"{label} fell to 0 W {stopped - off_at:.0f} s after switch-off"
                                  if stopped else f"{label} still showed power when the step "
                                                  "ended: automations waiting for it to stop would wait"))
        else:
            notes.append(proposal("", None, "low", f"{label} was not switched off again: "
                                                   "whether its reading falls to 0 W is unchecked"))
    crosstalk = sorted(ep for ep, m in moves.items()
                       if ep != target and top > 0 and m >= MOVED_SHARE * top)
    if crosstalk:
        shown = ", ".join(f"EP{ep}" for ep in crosstalk)
        notes.append(proposal("", None, "high",
                              f"{shown} also showed the load on {label}: left as is, history "
                              f"records it on {len(crosstalk) + 1} EPs and an automation waiting "
                              f"for {shown}'s power to stop waits on {label}'s load"))
    return notes


def moved_endpoints(records, baseline) -> List[int]:
    """EPs whose power moved during a step (for the metering map)."""
    moves = _power_moves(records, baseline)
    top = max(moves.values(), default=0.0)
    return sorted(ep for ep, m in moves.items() if top > 0 and m >= MOVED_SHARE * top)


def metering_map(tests: Dict[int, List[int]], power_eps: List[int],
                 switch_eps: List[int]) -> List[Dict[str, Any]]:
    """What each EP's power reading measures, from every switch test
    ({socket EP loaded: EPs whose power moved}). One test cannot tell a
    whole-device reading from a socket's, so the map needs them all: an EP
    that moves for every socket is the whole device; for its own socket
    only, itself; for other sockets, those; for none, nothing."""
    if not tests:
        return []
    tested = sorted(tests)
    complete = set(tested) >= set(switch_eps) and len(tested) > 1
    conf = "high" if complete else "medium"
    named = lambda eps: ", ".join(f"EP{e}" for e in eps)
    out = []
    for ep in power_eps:
        moved_for = sorted(t for t, moved in tests.items() if ep in moved)
        if len(tested) > 1 and moved_for == tested:
            value, why = "device_total", f"moved for every socket tested ({named(tested)})"
        elif not moved_for:
            value, why = "none", "moved for no socket tested"
        elif moved_for == [ep]:
            value, why = "self", "moved only when its own socket was loaded"
        else:
            value, why = "measures:" + ",".join(map(str, moved_for)), \
                f"moved only when {named(moved_for)} was loaded"
        out.append(proposal(f"endpoints.{ep}.metering", value, conf, f"EP{ep}'s reading {why}"))
    untested = sorted(set(switch_eps) - set(tested))
    if untested:
        out.append(proposal("", None, "low", f"not tested yet: {named(untested)}; the map is "
                                             "provisional until every socket is"))
    return out


def scale_from_known(records, baseline, step, ctx) -> List[Dict[str, Any]]:
    """The power of ten that turns the raw reading into the known load."""
    known = _num((step.get("inputs") or {}).get("rating_w"))
    moves = _power_moves(records, baseline)
    if not known or known <= 0 or not moves:
        return [proposal("", None, "none", "needs a known rating and a power reading")]
    raw = moves.get(step.get("ep")) or max(moves.values())
    for divisor in (1, 10, 100, 1000):
        if abs(raw / divisor - known) <= KNOWN_TOLERANCE * known:
            scaled = raw / divisor
            limit = ctx.get("max_power_w")
            if limit is not None and scaled > limit:
                return [proposal("", None, "low",
                                 f"{scaled:g} W is more than this outlet can carry ({limit:g} W)")]
            return [proposal("zmm.measurements.active_power",
                             {"cluster": "0x0B04", "attr": "0x050B", "multiplier": 1,
                              "divisor": divisor}, "high",
                             f"raw {raw:g} for a {known:g} W load: /{divisor} gives {scaled:g} W")]
    return [proposal("", None, "low", f"raw {raw:g} is not a power of ten from {known:g} W: "
                                      "is the rating right, and was the load at full power?")]


def attribute_that_toggled(records, baseline, step, ctx) -> List[Dict[str, Any]]:
    """The writable manufacturer attribute that changed when the user changed a
    setting on the device."""
    inputs = step.get("inputs") or {}
    sid = str(inputs.get("setting_id") or "").strip()
    writable = ctx.get("writable") or {}          # (ep, cluster, attr, mfr) -> type name
    hits = [r for r in records if vendor_scoped(r["cluster"], r.get("mfr")) and _key(r) in writable
            and _key(r) in baseline and r["value"] != baseline[_key(r)]]
    if not sid:
        return [proposal("", None, "none", "name the setting first")]
    if len({_key(r) for r in hits}) != 1:
        n = len({_key(r) for r in hits})
        return [proposal("", None, "none" if n == 0 else "low",
                         "no writable setting changed" if n == 0
                         else f"{n} settings changed at once: change just one")]
    r = hits[-1]
    ep, cluster, attr, mfr = _key(r)
    old, new = baseline[_key(r)], r["value"]
    values = _value_names(old, new, inputs, writable[_key(r)]) \
        if isinstance(old, (int, float)) and isinstance(new, (int, float)) else {}
    return [proposal("zmm.settings", {
        "id": sid, "label": _label(inputs, sid), "type": writable[_key(r)],
        "ep": ep, "cluster": f"0x{cluster:04X}", "attr": f"0x{attr:04X}", "mfr": mfr_hex(mfr),
        "values": values}, "high",
        f"EP{ep} 0x{cluster:04X}/0x{attr:04X} went {old} -> {new}")]


def _label(inputs: Dict[str, Any], sid: str) -> str:
    return str(inputs.get("label") or "").strip() or sid.replace("_", " ").capitalize()


def _value_names(old, new, inputs: Dict[str, Any], type_name: str) -> Dict[str, str]:
    """{raw: name} for a setting's two states; a bool left unnamed is Off/On."""
    def name(v, given):
        given = str(given or "").strip()
        if given:
            return given
        return ("On" if int(v) else "Off") if type_name == "bool" else str(int(v))
    return {str(int(old)): name(old, inputs.get("from_label")),
            str(int(new)): name(new, inputs.get("to_label"))}


# A setting stored scaled is a power of ten from the number a person sees.
NUMBER_SCALES = (1, 10, 100, 1000)


def attribute_that_became(records, baseline, step, ctx) -> List[Dict[str, Any]]:
    """The writable vendor attribute that took the number the user set on the
    device. Its raw value over that number is the scale, so a temperature held
    as hundredths is learned as one, with no table saying so."""
    inputs = step.get("inputs") or {}
    sid = str(inputs.get("setting_id") or "").strip()
    known = _num(inputs.get("new_value"))
    writable = ctx.get("writable") or {}
    if not sid or known is None:
        return [proposal("", None, "none", "name the setting and give the value you set")]
    if known == 0:
        return [proposal("", None, "none", "set it to a value other than 0: every scale of 0 is 0")]
    changed = {}
    for r in records:
        if vendor_scoped(r["cluster"], r.get("mfr")) and _key(r) in writable \
                and _num(r["value"]) is not None and r["value"] != baseline.get(_key(r)):
            changed[_key(r)] = r["value"]
    matches = [(key, scale) for key, raw in sorted(changed.items(), key=lambda kv: str(kv[0]))
               for scale in NUMBER_SCALES if raw == round(known * scale)]
    if len({key for key, _ in matches}) != 1:
        n = len({key for key, _ in matches})
        return [proposal("", None, "none" if n == 0 else "low",
                         f"no setting became {known:g}" + (
                             f" ({len(changed)} changed to something else)" if changed else "")
                         if n == 0 else f"{n} settings became {known:g}: change just one")]
    (ep, cluster, attr, mfr), scale = matches[0]
    setting = {"id": sid, "label": _label(inputs, sid),
               "type": writable[(ep, cluster, attr, mfr)],
               "ep": ep, "cluster": f"0x{cluster:04X}", "attr": f"0x{attr:04X}",
               "mfr": mfr_hex(mfr), "values": {}}
    if scale != 1:
        setting["scale"] = scale
    if str(inputs.get("unit") or "").strip():
        setting["unit"] = str(inputs["unit"]).strip()
    raw = changed[(ep, cluster, attr, mfr)]
    return [proposal("zmm.settings", setting, "high",
                     f"EP{ep} 0x{cluster:04X}/0x{attr:04X} went "
                     f"{baseline.get((ep, cluster, attr, mfr))} -> {raw} when set to {known:g}"
                     + (f": stored x{scale}" if scale != 1 else ""))]


# How far apart a vendor attribute and the state it mirrors may be recorded.
TRACK_WINDOW_S = 30.0
# Changes that must all agree, over at least two values, before it is no coincidence.
TRACK_MIN_CHANGES = 3
# Link quality moves all the time and means nothing about a setting.
UNTRACKED_STATES = frozenset({"rssi", "lqi", "linkquality", "last_seen"})


def _changes(series: List[Tuple[float, Any]]) -> List[Tuple[float, Any]]:
    """The points where a series took a new value; its first is not a change."""
    out, last = [], object()
    for i, (t, v) in enumerate(sorted(series, key=lambda p: p[0])):
        if i and v != last:
            out.append((t, v))
        last = v
    return out


def _in_force(series: List[Tuple[float, Any]], t: float) -> Any:
    """The series' value as of `t`, allowing it to be recorded slightly later."""
    held = None
    for ts, v in series:
        if ts > t + TRACK_WINDOW_S:
            break
        held = v
    return held


def attribute_that_tracked(records, baseline, step, ctx) -> List[Dict[str, Any]]:
    """Vendor attributes whose every recorded change landed on a state the app
    already knows (a setpoint, say), times a power of ten. Nobody acts: the
    evidence is the device's own history, and says which text states (a
    mode) held throughout, since the attribute may only apply under them."""
    known = {n: sorted(s) for n, s in (ctx.get("known") or {}).items()
             if n not in UNTRACKED_STATES and len({v for _, v in s}) > 1}
    texts = {n: sorted(s) for n, s in (ctx.get("texts") or {}).items()
             if len({v for _, v in s}) > 1}
    writable = {k[:3]: (k[3], t) for k, t in (ctx.get("writable") or {}).items()}
    # States recorded under several names are one state: keep the first name.
    families: Dict[tuple, str] = {}
    for name in sorted(known):
        families.setdefault(tuple(v for _, v in _changes(known[name])), name)
    series: Dict[Tuple[int, int, int], List[Tuple[float, Any]]] = {}
    for r in records:
        if vendor_scoped(r["cluster"], r.get("mfr")) and _num(r["value"]) is not None:
            series.setdefault((r["ep"], r["cluster"], r["attr"]), []).append((r["t"], r["value"]))
    out, used = [], set()
    for key in sorted(series):
        changes = _changes(series[key])
        if not changes:
            continue
        ep, cluster, attr = key
        where = f"EP{ep} 0x{cluster:04X}/0x{attr:04X}"
        for name in sorted(set(families.values())):
            scale = next((s for s in NUMBER_SCALES if all(
                _num(_in_force(known[name], t)) is not None
                and raw == round(_in_force(known[name], t) * s) for t, raw in changes)), None)
            if scale is None:
                continue
            seen = f"{where} took {name}" + (f" x{scale}" if scale != 1 else "") \
                + f" at each of its {len(changes)} changes"
            if len(changes) < TRACK_MIN_CHANGES or len({raw for _, raw in changes}) < 2:
                out.append(proposal("", None, "low", seen + ": too few to rule out chance"))
                break
            if key not in writable:
                out.append(proposal("", None, "low", seen + ": read-only, so not a setting"))
                break
            held = [f"{n} was {v}" for n, ts in sorted(texts.items())
                    for v in [_in_force(ts, changes[0][0])]
                    if v is not None and all(_in_force(ts, t) == v for t, _ in changes)]
            sid = re.sub(r"[^a-z0-9_]", "_", f"{name}_stored".lower())[:40]
            if sid in used:
                sid = f"{sid[:35]}_{attr:04x}"
            used.add(sid)
            mfr, type_name = writable[key]
            setting = {"id": sid, "label": sid.replace("_", " ").capitalize(), "type": type_name,
                       "ep": ep, "cluster": f"0x{cluster:04X}", "attr": f"0x{attr:04X}",
                       "mfr": mfr_hex(mfr), "values": {}}
            if scale != 1:
                setting["scale"] = scale
            out.append(proposal("zmm.settings", setting, "high",
                                seen + (f", all while {' and '.join(held)}" if held else "")))
            break
    return out or [proposal("", None, "none",
                            "no vendor attribute has changed in step with a known state yet")]


def correlate_blob_tags(records, baseline, step, ctx) -> List[Dict[str, Any]]:
    """Aqara blob tags whose value matches a known reading in the same step."""
    from handlers.aqara import parse_xiaomi_struct
    known = _num((step.get("inputs") or {}).get("rating_w"))
    energy = _num(ctx.get("energy_kwh"))
    found: Dict[int, Tuple[str, str]] = {}
    for r in records:
        if r["cluster"] != AQARA or r["attr"] not in BLOB_ATTRS or not isinstance(r["value"], str):
            continue
        try:
            tags = parse_xiaomi_struct(bytes.fromhex(r["value"]))
        except ValueError:
            continue
        for tag, v in tags.items():
            v = _num(v)
            if v is None or v == 0:
                continue
            if known and abs(v - known) <= KNOWN_TOLERANCE * known:
                found[tag] = ("power", f"{v:g} while a {known:g} W load ran")
            elif MAINS_V[0] <= v <= MAINS_V[1]:
                found.setdefault(tag, ("voltage", f"{v:g}, a mains voltage"))
            elif energy and abs(v - energy) <= 0.05 * energy:
                found.setdefault(tag, ("energy", f"{v:g} matches the {energy:g} kWh counter"))
    return [proposal(f"zmm.struct_tags.0x{tag:02X}", {"name": name, "scale": 1}, "medium",
                     f"tag 0x{tag:02X} = {why}") for tag, (name, why) in sorted(found.items())]


def confirmed_write(records, baseline, step, ctx) -> List[Dict[str, Any]]:
    """The attribute ZMM flipped (and put back) while the user watched the
    device change: it is the setting the user named."""
    inputs = step.get("inputs") or {}
    sid = str(inputs.get("setting_id") or "").strip()
    trial = ctx.get("trial") or {}
    if not sid or not trial.get("confirmed"):
        return [proposal("", None, "none", "no flip was confirmed")]
    ep, cluster, attr, mfr = trial["key"]
    old, new = trial["old"], trial["new"]
    return [proposal("zmm.settings", {
        "id": sid, "label": _label(inputs, sid), "type": trial["type"],
        "ep": ep, "cluster": f"0x{cluster:04X}", "attr": f"0x{attr:04X}", "mfr": mfr_hex(mfr),
        "values": _value_names(old, new, inputs, trial["type"])}, "high",
        f"writing EP{ep} 0x{cluster:04X}/0x{attr:04X} {int(old)} -> {int(new)} changed the device; "
        f"put back to {int(old)}")]


# Basic and Power Configuration describe the device, not what it senses.
NOT_A_READING = frozenset({0x0000, 0x0001})
# More two-state readings than this flipping at once is not one walk past.
MAX_OCCUPANCY_CANDIDATES = 3


def _two_state(v: Any) -> Optional[int]:
    return int(v) if isinstance(v, (bool, int)) and v in (0, 1) else None


def occupancy_attribute(records, baseline, step, ctx) -> List[Dict[str, Any]]:
    """The two-state reading that flipped when the user walked into view (the
    step's `expect` is `occupied`) or out of it (`clear`). The value it took
    names that state, so nothing assumes 1 means occupied. A sleepy sensor
    cannot be read first: a reading seen for the first time is offered at
    lower confidence, and the walk the other way then confirms it."""
    occupied = (step.get("expect") or "occupied") == "occupied"
    writable = ctx.get("writable") or {}
    last: Dict[Key, Tuple[float, int]] = {}
    other = {k for k, v in baseline.items() if _two_state(v) is None}
    for r in sorted(records, key=lambda r: r["t"]):
        key = _key(r)
        if r["cluster"] in NOT_A_READING or _in(r, PRESS) or key in writable:
            continue
        v = _two_state(r["value"])
        if v is None:
            other.add(key)          # took a third value: a measurement, not a state
        else:
            last[key] = (r["t"], v)
    flipped = {k: tv for k, tv in last.items() if k not in other and baseline.get(k) != tv[1]}
    if not flipped:
        return [proposal("", None, "none",
                         "no two-state reading changed: was the view empty before the step began, "
                         "and did the sensor see you?" if occupied else
                         "no two-state reading changed: the sensor may hold 'occupied' for longer "
                         "than the step; stay out of view and redo it")]
    if len(flipped) > MAX_OCCUPANCY_CANDIDATES:
        return [proposal("", None, "low", f"{len(flipped)} readings changed at once: redo it "
                                          "with nothing else happening to the device")]
    t0 = _num(step.get("t0"))
    out = []
    for key, (t, v) in sorted(flipped.items(), key=lambda kv: kv[1][0]):
        ep, cluster, attr, _ = key
        on = v if occupied else 1 - v
        known = key in baseline
        out.append(proposal(
            f"endpoints.{ep}.clusters.0x{cluster:04X}.attributes.0x{attr:04X}",
            {"name": "occupancy", "device_class": "occupancy", "capability": "motion",
             "value_map": {str(on): True, str(1 - on): False}},
            ("high" if known else "medium") if len(flipped) == 1 else "low",
            f"EP{ep} 0x{cluster:04X}/0x{attr:04X} "
            + (f"went {baseline[key]} -> {v}" if known else f"reported {v}, not seen before,")
            + (f" {t - t0:.0f} s into the step" if t0 is not None else "")
            + f" as you walked {'in' if occupied else 'out'}: {on} is occupied"
            + ("" if len(flipped) == 1 else f"; one of {len(flipped)} that changed, accept only one")))
    out.append(proposal("device_type", "motion_sensor", out[0]["confidence"],
                        "it reports whether someone is in view"))
    return out


def parse_known(text: Any) -> Tuple[Dict[str, float], List[str]]:
    """({name: number}, names left out) from what the user typed or pasted: a
    JSON object, or `name: value` pairs split by commas, semicolons or lines.
    Only numbers can be matched; on/off and text values are named as left out."""
    import json
    pairs: Dict[str, Any] = {}
    text = str(text or "").strip()
    try:
        loaded = json.loads(text)
        pairs = loaded if isinstance(loaded, dict) else {}
    except ValueError:
        for part in re.split(r"[,;\n]+", text):
            name, sep, value = part.partition(":") if ":" in part else part.partition("=")
            if sep:
                pairs[name.strip().strip('"')] = value.strip().strip('"')
    known, skipped = {}, []
    for name, value in pairs.items():
        sid = re.sub(r"[^a-z0-9_]", "_", str(name).strip().lower())[:40]
        number = _num(value)
        if number is None and isinstance(value, str):
            try:
                number = float(value)
            except ValueError:
                number = None
        if not re.fullmatch(r"[a-z][a-z0-9_]*", sid) or number is None:
            skipped.append(str(name))
        else:
            known[sid] = number
    return known, skipped


# A raw value this large matching a known one is unlikely to be chance; one
# below the floor matches something on most devices, and is never proposed.
DISTINCTIVE = 1000
MATCH_FLOOR = 100


def attributes_holding_known(records, baseline, step, ctx) -> List[Dict[str, Any]]:
    """The maker's attributes whose last known value is a value the user says
    the device has, times a power of ten. Proposed only where one name fits
    one attribute and that attribute fits no other name; the rest are listed
    with their candidates, since a guess between them would be a coin toss."""
    known, skipped = parse_known((step.get("inputs") or {}).get("known"))
    values = {k: v for k, v in (ctx.get("vendor_values") or {}).items() if _num(v) is not None}
    writable = ctx.get("writable") or {}
    have = set(ctx.get("state_keys") or ())
    if not known:
        return [proposal("", None, "none", "give the values as name: number, separated by commas")]
    if not values:
        return [proposal("", None, "none", "nothing is known of this device's own attributes yet: "
                                           "run 'Find its attributes' (under Wake and discover) first")]
    fits: Dict[str, List[Tuple[Key, int]]] = {}
    for name, number in sorted(known.items()):
        if name in have or number == 0:
            continue                    # already a reading here; every scale of 0 is 0
        fits[name] = [(key, scale) for key, raw in sorted(values.items(), key=lambda kv: str(kv[0]))
                      for scale in NUMBER_SCALES if raw == round(number * scale)][:8]
    claimed: Dict[Key, set] = {}
    for name, found in fits.items():
        for key, _ in found:
            claimed.setdefault(key, set()).add(name)
    where = lambda key: f"EP{key[0]} 0x{key[1]:04X}/0x{key[2]:04X}"
    out, unsure, absent, small = [], [], [], []
    for name, found in fits.items():
        keys = {key for key, _ in found}
        if not keys:
            absent.append(name)
        elif len(keys) == 1 and abs(values[found[0][0]]) < MATCH_FLOOR:
            small.append(f"{name} = {known[name]:g} ({where(found[0][0])} holds {values[found[0][0]]})")
        elif len(keys) > 1 or len(claimed[found[0][0]]) > 1:
            others = sorted(set().union(*(claimed[k] for k in keys)) - {name})
            unsure.append(f"{name} = {known[name]:g} fits {', '.join(sorted(where(k) for k in keys))}"
                          + (f" (as does {', '.join(others)})" if others else ""))
        else:
            key, scale = found[0]
            ep, cluster, attr, mfr = key
            seen = f"{where(key)} holds {values[key]}: {name} = {known[name]:g}" \
                + (f", stored x{scale}" if scale != 1 else "")
            conf = "high" if abs(values[key]) >= DISTINCTIVE else "medium"
            if key in writable:
                setting = {"id": name, "label": name.replace("_", " ").capitalize(),
                           "type": writable[key], "ep": ep, "cluster": f"0x{cluster:04X}",
                           "attr": f"0x{attr:04X}", "mfr": mfr_hex(mfr), "values": {}}
                if scale != 1:
                    setting["scale"] = scale
                out.append(proposal("zmm.settings", setting, conf, seen + "; it can be written"))
            else:
                mapping = {"name": name}
                if scale != 1:
                    mapping["scale"] = scale
                out.append(proposal(f"endpoints.{ep}.clusters.0x{cluster:04X}.attributes.0x{attr:04X}",
                                    mapping, conf, seen + (
                                        "; read-only" if key in (ctx.get("listed") or ())
                                        else "; whether it can be written is not known: run "
                                             "'Check which can be set'")))
    if unsure:
        out.append(proposal("", None, "low", "more than one fit, so not proposed: " + "; ".join(unsure)))
    if small:
        out.append(proposal("", None, "low", "too small a number to single out an attribute: "
                            + "; ".join(small)))
    if absent:
        out.append(proposal("", None, "none", "no attribute holds: " + ", ".join(absent)))
    if skipped:
        out.append(proposal("", None, "none", "not numbers, so not matched: " + ", ".join(skipped[:12])
                            + (f" and {len(skipped) - 12} more" if len(skipped) > 12 else "")))
    return out or [proposal("", None, "none", "every value given is already a reading here")]


OPS: Dict[str, Callable] = {
    "which_endpoint_changed": which_endpoint_changed,
    "press_signature": press_signature,
    "which_endpoints_moved": which_endpoints_moved,
    "scale_from_known": scale_from_known,
    "attribute_that_toggled": attribute_that_toggled,
    "attribute_that_became": attribute_that_became,
    "attribute_that_tracked": attribute_that_tracked,
    "correlate_blob_tags": correlate_blob_tags,
    "confirmed_write": confirmed_write,
    "power_follows_switch": power_follows_switch,
    "occupancy_attribute": occupancy_attribute,
    "attributes_holding_known": attributes_holding_known,
}
