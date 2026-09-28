"""
Inference operations for device learning (docs/plans/device-learning.md §5).

Pure functions over the frames captured while the user carried out one step.
Each returns proposals: a path in the entry, a value, a confidence and the
evidence in words. A recipe names the operations; it cannot run code.

A capture record: {"t", "ep", "cluster", "attr", "mfr", "value"}. `baseline`
holds each (ep, cluster, attr, mfr)'s last value before the step began.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

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
    hits = [r for r in records if r.get("mfr") and _key(r) in writable
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
    values = {str(int(old)): str(inputs.get("from_label") or old),
              str(int(new)): str(inputs.get("to_label") or new)} \
        if _num(old) is not None and _num(new) is not None else {}
    return [proposal("zmm.settings", {
        "id": sid, "label": str(inputs.get("label") or sid), "type": writable[_key(r)],
        "ep": ep, "cluster": f"0x{cluster:04X}", "attr": f"0x{attr:04X}", "mfr": f"0x{mfr:04X}",
        "values": values}, "high",
        f"EP{ep} 0x{cluster:04X}/0x{attr:04X} went {old} -> {new}")]


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
        "id": sid, "label": str(inputs.get("label") or sid), "type": trial["type"],
        "ep": ep, "cluster": f"0x{cluster:04X}", "attr": f"0x{attr:04X}", "mfr": f"0x{mfr:04X}",
        "values": {str(int(old)): str(inputs.get("from_label") or old),
                   str(int(new)): str(inputs.get("to_label") or new)}}, "high",
        f"writing EP{ep} 0x{cluster:04X}/0x{attr:04X} {int(old)} -> {int(new)} changed the device; "
        f"put back to {int(old)}")]


OPS: Dict[str, Callable] = {
    "which_endpoint_changed": which_endpoint_changed,
    "press_signature": press_signature,
    "which_endpoints_moved": which_endpoints_moved,
    "scale_from_known": scale_from_known,
    "attribute_that_toggled": attribute_that_toggled,
    "correlate_blob_tags": correlate_blob_tags,
    "confirmed_write": confirmed_write,
    "power_follows_switch": power_follows_switch,
}
