"""
Swarm Intelligence — stigmergy.

Stigmergy is how a swarm coordinates without a coordinator: each member leaves a
trace in the shared environment, and those traces tell the next member what to
do. Here the environment is the device registry, and a **pattern** is a trace
laid down over it — a handful of slots to fill, a rule template to emit, and the
parameters a user may tune. Patterns are data, not code, so a new one is a JSON
file rather than a release.

Patterns do not decide what is *possible* — network.pairings() already returns
every wiring the swarm supports. What a pattern adds is a name, an intent, and a
shape more complex than a single trigger-to-action pair: a condition drawn from
a second device, an else-branch that turns the light back off, a delay.

Loading mirrors device_profiles: a bundled set ships with the app and a user
directory overrides it by id, so a local edit survives an upgrade.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from modules.swarm.capabilities import CAPABILITIES, PARAMS

logger = logging.getLogger("modules.swarm.stigmergy")

DATA_DIR = os.environ.get("ZMM_DATA_DIR", "./data")

# Bundled patterns ship with the code, not with the data. The container mounts
# the host's persistent directory over /app/data, so anything the image placed
# there is masked at runtime — a bundled file in a runtime volume is a file that
# exists in the image and cannot be read. Resolved from this module's own
# location so it works whatever the working directory is.
BUNDLED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "patterns")

# User patterns are runtime data and belong in the volume, where they survive an
# upgrade and override a bundled pattern of the same id.
USER_DIR = os.path.join(DATA_DIR, "stigmergy_user")

ROLES = ("trigger", "condition", "action")
SCOPES = ("room", "house")
CATEGORIES = ("lighting", "climate", "security", "safety", "energy",
              "presence", "maintenance", "convenience")

SOURCE_BUNDLED = "bundled"
SOURCE_USER = "user"

# Substituted into a literal step at compile time rather than resolved from a
# slot. `$trigger_device` is the device whose edge fired, which a message wants
# to name and no slot can supply.
RESERVED_PLACEHOLDERS = ("$trigger_device", "$trigger_room", "$target_device")


def literal_slot_refs(value: Any) -> List[str]:
    """Slot names referenced as `$slot` anywhere inside a literal step.

    A message step names its recipient with `$who` and an offer splices in an
    action with `{"slot": "cool_it"}`, so a slot used only either way is still
    used — without this the validator calls it dead weight and rejects a pattern
    that is perfectly correct.
    """
    found: List[str] = []
    if isinstance(value, dict):
        # {"slot": id} names a slot whose *step* is spliced in — an offer's
        # accept branch. Distinct from "$slot", which names its address.
        if set(value) == {"slot"} and isinstance(value["slot"], str):
            return [value["slot"]]
        for v in value.values():
            found += literal_slot_refs(v)
    elif isinstance(value, list):
        for v in value:
            found += literal_slot_refs(v)
    elif isinstance(value, str):
        for token in re.findall(r"\$[a-zA-Z_][a-zA-Z0-9_]*", value):
            if token not in RESERVED_PLACEHOLDERS:
                found.append(token[1:])
    return found


# Validation
#
# A pattern that cannot compile is worse than one that does not exist: it
# reaches the user as a suggestion and then fails at save. Everything checkable
# without a live network is checked here, at load.

def validate(bp: Dict[str, Any]) -> List[str]:
    """Every problem with a pattern, as human-readable strings."""
    errors: List[str] = []

    def err(msg: str):
        errors.append(msg)

    bid = bp.get("id")
    if not bid or not isinstance(bid, str):
        err("missing 'id'")
        return errors
    if not bp.get("title"):
        err(f"{bid}: missing 'title'")

    scope = bp.get("scope", "room")
    if scope not in SCOPES:
        err(f"{bid}: scope must be one of {SCOPES}, got {scope!r}")
    category = bp.get("category")
    if category and category not in CATEGORIES:
        err(f"{bid}: unknown category {category!r}")

    slots = bp.get("slots")
    if not isinstance(slots, dict) or not slots:
        err(f"{bid}: needs a non-empty 'slots' object")
        return errors

    for name, spec in slots.items():
        if not isinstance(spec, dict):
            err(f"{bid}.{name}: slot must be an object")
            continue
        role = spec.get("role")
        if role not in ROLES:
            err(f"{bid}.{name}: role must be one of {ROLES}, got {role!r}")
        key = spec.get("offer")
        if not key or ":" not in str(key):
            err(f"{bid}.{name}: 'offer' must be '<capability>:<offer_id>'")
            continue
        cap_id, offer_id = str(key).split(":", 1)
        spec_cap = CAPABILITIES.get(cap_id)
        if not spec_cap:
            err(f"{bid}.{name}: unknown capability {cap_id!r}")
            continue
        if role in ROLES:
            pool = spec_cap.get(role + "s", [])
            # A button's press offers fan out per value, so the declared id is a
            # prefix of the keys the resolver emits.
            if not any(o["id"] == offer_id or o.get("expand") for o in pool):
                err(f"{bid}.{name}: {cap_id} has no {role} {offer_id!r}")
        for pid in (spec.get("params") or {}):
            if pid not in PARAMS:
                err(f"{bid}.{name}: unknown parameter {pid!r}")
        prefer_slot = spec.get("prefer_slot")
        if prefer_slot and prefer_slot not in slots:
            err(f"{bid}.{name}: prefer_slot {prefer_slot!r} is not a slot")
        if spec.get("prefer") and not prefer_slot:
            err(f"{bid}.{name}: 'prefer' needs 'prefer_slot'")

    emits = bp.get("emits")
    if not isinstance(emits, dict):
        err(f"{bid}: needs an 'emits' object")
        return errors

    source = emits.get("source")
    if source not in slots:
        err(f"{bid}.emits: source {source!r} is not a slot")
    elif slots[source].get("role") != "trigger":
        err(f"{bid}.emits: source slot {source!r} must have role 'trigger'")
    elif slots[source].get("optional"):
        err(f"{bid}.emits: source slot {source!r} cannot be optional — "
            f"a rule with no trigger never fires")

    for field, want in (("conditions", ("trigger", "condition")),
                        ("then", ("action",)), ("else", ("action",))):
        for entry in emits.get(field, []) or []:
            if isinstance(entry, dict):
                if not entry.get("type"):
                    err(f"{bid}.emits.{field}: literal step needs a 'type'")
                for ref in literal_slot_refs(entry):
                    if ref not in slots:
                        err(f"{bid}.emits.{field}: literal step references "
                            f"${ref}, which is not a slot")
                continue
            if entry not in slots:
                err(f"{bid}.emits.{field}: {entry!r} is not a slot")
            elif slots[entry].get("role") not in want:
                err(f"{bid}.emits.{field}: slot {entry!r} has role "
                    f"{slots[entry].get('role')!r}, needs one of {want}")

    if not (emits.get("then") or emits.get("else")):
        err(f"{bid}.emits: needs at least one action step")

    logic = emits.get("condition_logic", "and")
    if logic not in ("and", "or"):
        err(f"{bid}.emits: condition_logic must be 'and' or 'or'")

    for pid in (bp.get("params") or {}):
        if pid not in PARAMS:
            err(f"{bid}.params: unknown parameter {pid!r}")

    # Every slot should be reachable from emits, or it is dead weight that
    # silently narrows matching for no effect on the rule.
    referenced = set()
    for field in ("conditions", "then", "else"):
        for e in (emits.get(field) or []):
            if isinstance(e, str):
                referenced.add(e)
            else:
                referenced.update(literal_slot_refs(e))
    referenced.add(source)
    for name in slots:
        if name not in referenced:
            err(f"{bid}.{name}: slot is never used by 'emits'")

    return errors


# Store

class StigmergyStore:
    """Bundled patterns, overridden by id from the user directory."""

    def __init__(self, bundled_dir: str = BUNDLED_DIR,
                 user_dir: str = USER_DIR) -> None:
        self._bundled_dir = bundled_dir
        self._user_dir = user_dir
        self._lock = threading.Lock()
        self._blueprints: Dict[str, Dict[str, Any]] = {}
        self._errors: List[str] = []
        self.reload()

    def reload(self) -> None:
        """Re-read both directories. Invalid patterns are skipped, not fatal."""
        loaded: Dict[str, Dict[str, Any]] = {}
        errors: List[str] = []
        for directory, source in ((self._bundled_dir, SOURCE_BUNDLED),
                                  (self._user_dir, SOURCE_USER)):
            for bp, err in _read_dir(directory):
                if err:
                    errors.append(err)
                    continue
                problems = validate(bp)
                if problems:
                    errors.extend(problems)
                    continue
                bp["source"] = source
                loaded[bp["id"]] = bp
        with self._lock:
            self._blueprints = loaded
            self._errors = errors
        if errors:
            logger.warning(f"{len(errors)} stigmergy problem(s) at load; "
                           f"{len(loaded)} usable")
        else:
            logger.info(f"Loaded {len(loaded)} stigmergy patterns")

    def all(self, include_disabled: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            bps = list(self._blueprints.values())
        if not include_disabled:
            bps = [b for b in bps if b.get("enabled", True)]
        return sorted(bps, key=lambda b: (b.get("category") or "", b["id"]))

    def get(self, bp_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._blueprints.get(bp_id)

    @property
    def errors(self) -> List[str]:
        """Problems found at load. Surfaced by the diagnostics endpoint —
        a pattern that silently failed to load looks identical to one that
        matched nothing, and the two need different fixes."""
        with self._lock:
            return list(self._errors)


def _read_dir(directory: str) -> List[Tuple[Optional[Dict], Optional[str]]]:
    """Every pattern in a directory. A file may hold one object or a list."""
    out: List[Tuple[Optional[Dict], Optional[str]]] = []
    if not os.path.isdir(directory):
        return out
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path) as f:
                payload = json.load(f)
        except (OSError, ValueError) as e:
            out.append((None, f"{path}: {e}"))
            continue
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if isinstance(entry, dict):
                out.append((entry, None))
            else:
                out.append((None, f"{path}: expected an object, got {type(entry).__name__}"))
    return out


_store: Optional[StigmergyStore] = None
_store_lock = threading.Lock()


def get_stigmergy_store() -> StigmergyStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = StigmergyStore()
        return _store
