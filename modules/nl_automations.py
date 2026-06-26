"""
Local Natural-Language Automation Parser
========================================
Deterministic, dependency-free compiler that turns a constrained-English
sentence into the same rule dict the AutomationEngine consumes — WITHOUT an
LLM. Designed for resource-limited SBCs: a parse is pure-Python string work
(microseconds) and never makes a network call.

It is the *first* path tried by POST /api/ai/automation. Only if it cannot
fully resolve the sentence does the caller fall back to the LLM (when one is
configured). Either way the produced rule is identical in shape.

Grounding: every device, attribute, value and command is resolved against the
LIVE registry via the engine's existing metadata methods
(get_all_devices_summary / get_device_state / get_actuator_devices), so values
are never guessed — "50%" becomes the right 0–254 brightness, "motion" maps to
whichever boolean attribute that specific device actually exposes, etc.

Supported shapes (case-insensitive, order-flexible):
  - "turn on the hall light when the hallway sensor detects motion"
  - "when the front door opens turn on the ensuite lights"
  - "turn off the media socket after 30 minutes"
  - "set the bedroom lights to 50% when motion is detected only if it is dark"
  - "turn on the hallway lights between 08:00 and 23:30"
  - "when kitchen temperature goes above 25 turn on the fan otherwise turn it off"
"""

import re
from typing import Any, Dict, List, Optional, Tuple

# ── Lexicons ────────────────────────────────────────────────────────────────

# Action verb → canonical command. Longer phrases first (matched greedily).
_ACTION_VERBS = [
    (r"turn(?:ed)?\s+on|switch(?:ed)?\s+on|power\s+on|enable", "on"),
    (r"turn(?:ed)?\s+off|switch(?:ed)?\s+off|power\s+off|disable", "off"),
    (r"toggle", "toggle"),
    (r"dim|brighten", "brightness"),
    (r"set", "set"),          # generic — value/keyword decides the real command
    (r"open", "open"),
    (r"close|shut", "close"),
    (r"stop", "stop"),
    (r"unlock", "unlock"),
    (r"lock", "lock"),
]
_ACTION_VERB_RE = re.compile(
    r"\b(" + "|".join(p for p, _ in _ACTION_VERBS) + r")\b", re.I)

_PRONOUNS = {"it", "them", "that", "this", "those", "they"}

# Attribute candidate lists per semantic concept (resolved against real attrs).
_MOTION_ATTRS = ["occupancy", "presence", "motion", "occupied", "presence_state"]
_CONTACT_ATTRS = ["contact", "is_open", "opening", "door", "window", "is_closed"]
_STATE_ATTRS = ["state", "state_1", "state_l1", "on", "on_1", "on_off"]
_BUTTON_ATTRS = ["action", "click", "button_action", "event", "scene"]

_NUMERIC_KEYWORDS = {
    "temperature": ["temperature", "local_temperature", "device_temperature"],
    "temp": ["temperature", "local_temperature"],
    "degrees": ["temperature", "local_temperature"],
    "humidity": ["humidity"],
    "illuminance": ["illuminance", "illuminance_lux"],
    "lux": ["illuminance_lux", "illuminance"],
    "light level": ["illuminance", "illuminance_lux"],
    "battery": ["battery"],
    "co2": ["co2"],
    "power": ["power"],
    "watt": ["power"],
    "voltage": ["voltage"],
    "pressure": ["pressure"],
}

_TIME_OF_DAY = {
    "night": ("22:00", "06:00"),
    "at night": ("22:00", "06:00"),
    "nighttime": ("22:00", "06:00"),
    "evening": ("18:00", "23:59"),
    "morning": ("06:00", "12:00"),
    "afternoon": ("12:00", "18:00"),
    "daytime": ("06:00", "22:00"),
    "during the day": ("06:00", "22:00"),
}

_NEGATION = re.compile(
    r"\b(no|not|n't|stop\w*|clear|clears|leaves|left|empty|"
    r"vacant|away|absent|no longer|closes|closed)\b", re.I)

_EXAMPLES = [
    "turn on the hall light when the hallway sensor detects motion",
    "when the front door opens turn on the ensuite lights",
    "turn off the media socket after 30 minutes",
    "set the bedroom lights to 40% when motion is detected",
    "turn on the hallway lights between 08:00 and 23:30",
    "when the kitchen temperature goes above 25 turn on the fan otherwise turn it off",
]


def _norm(name: str) -> str:
    """Lowercase, strip emoji/punctuation, collapse whitespace."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    return re.sub(r"\s+", " ", s).strip()


class NLAutomationParser:
    def __init__(self, engine):
        self._engine = engine
        self._devices: List[Dict[str, Any]] = []
        self._act_ieees: set = set()

    # ── Public API ──────────────────────────────────────────────────────────

    def parse(self, text: str) -> Dict[str, Any]:
        """Return {success, rule, explanation, source} or
        {success: False, error, partial, suggestions, examples}."""
        if not text or len(text.strip()) < 3:
            return self._fail("Please describe the automation in a few words.")

        self._load_devices()
        if not self._devices:
            return self._fail("No devices are available to build a rule from.")

        t = " " + re.sub(r"\s+", " ", text.lower()).strip() + " "
        matched: Dict[str, Any] = {}

        # 1. Peel off ELSE / prerequisite / delay / time before the main split.
        t, else_text = self._split_keyword(t, r"otherwise|or else|else")
        t, prereq_text = self._split_keyword(
            t, r"only if|provided that|provided|as long as|while")
        t, delay_secs = self._extract_delay(t)
        t, time_win, time_phrase = self._extract_time_window(t)
        if time_phrase:
            matched["time"] = time_phrase

        # 2. Separate the action clause from the trigger clause.
        action_text, trigger_text = self._split_action(t)

        conditions: List[Dict] = []
        prerequisites: List[Dict] = []
        source_ieee: Optional[str] = None

        # 3. Trigger (device + predicate).
        if trigger_text and _norm(trigger_text):
            src, cond, note = self._parse_trigger(trigger_text)
            if src and cond:
                source_ieee = src
                conditions.append(cond)
                matched["trigger"] = note
            elif note:
                return self._fail(note, matched)

        # 4. Time window. With a device trigger it gates (prerequisite);
        #    alone it IS the trigger condition.
        if time_win:
            tw = {"type": "time_window", "time_from": time_win[0],
                  "time_to": time_win[1], "days": list(range(7)), "negate": False}
            if conditions:
                prerequisites.append(tw)
            else:
                conditions.append(tw)

        # 5. THEN action(s).
        then_seq: List[Dict] = []
        if delay_secs and delay_secs > 0:
            then_seq.append({"type": "delay", "seconds": delay_secs})
        if action_text:
            steps, note = self._parse_action(action_text, source_ieee)
            if steps:
                then_seq.extend(steps)
                matched["action"] = note
            elif note and not then_seq:
                return self._fail(note, matched)

        if not then_seq:
            return self._fail(
                "I couldn't find an action to perform (e.g. \"turn on the lamp\").",
                matched)

        # 6. Auto-timer pattern: a delayed action with no trigger/time means
        #    "do X to this device N seconds after it changes to the opposite".
        if not conditions and delay_secs:
            anchor = self._first_command_target(then_seq)
            if anchor:
                source_ieee = anchor
                opp = self._opposite_state_condition(anchor, then_seq)
                if opp:
                    conditions.append(opp)
                    matched.setdefault("trigger",
                                       "auto-timer on the target device")

        # 7. A rule needs a trigger condition.
        if not conditions:
            return self._fail(
                "I need a trigger — say \"when <device> ...\" or give a time "
                "window like \"between 08:00 and 23:30\".", matched)

        # 8. Source must be a real (non-group) device for the engine.
        if not source_ieee:
            source_ieee = self._first_command_target(then_seq) or self._any_source()
        if source_ieee and source_ieee.startswith("group:"):
            source_ieee = self._any_source()
        if not source_ieee:
            return self._fail("Couldn't determine a source device.", matched)

        # 9. Prerequisite clause ("only if ...").
        if prereq_text:
            pre, note = self._parse_prerequisite(prereq_text)
            if pre:
                prerequisites.append(pre)
                matched["prerequisite"] = note

        # 10. ELSE action(s).
        else_seq: List[Dict] = []
        if else_text:
            steps, _ = self._parse_action(else_text, source_ieee)
            if steps:
                else_seq.extend(steps)

        rule = {
            "name": self._make_name(source_ieee, then_seq, conditions),
            "source_ieee": source_ieee,
            "conditions": conditions,
            "prerequisites": prerequisites,
            "then_sequence": then_seq,
            "else_sequence": else_seq,
            "cooldown": 5,
            "enabled": True,
        }
        return {"success": True, "rule": rule, "source": "local",
                "explanation": self._explain(rule)}

    def help(self) -> Dict[str, Any]:
        """Static help payload for the UI (examples + device names)."""
        self._load_devices()
        return {
            "examples": _EXAMPLES,
            "devices": [d["name"] for d in self._devices],
        }

    # ── Device registry ─────────────────────────────────────────────────────

    def _load_devices(self):
        try:
            summ = self._engine.get_all_devices_summary() or []
        except Exception:
            summ = []
        try:
            acts = {a["ieee"]: a.get("commands", [])
                    for a in (self._engine.get_actuator_devices() or [])}
        except Exception:
            acts = {}
        self._act_ieees = set(acts)
        self._devices = []
        for d in summ:
            ieee = d.get("ieee")
            if not ieee:
                continue
            self._devices.append({
                "ieee": ieee,
                "name": d.get("friendly_name", ieee),
                "norm": _norm(d.get("friendly_name", ieee)),
                "is_group": bool(d.get("_is_group")),
                "state_keys": d.get("state_keys", []),
                "commands": acts.get(ieee, []),
                "_attrs": None,
            })

    def _attrs_for(self, ieee: str) -> List[Dict]:
        for d in self._devices:
            if d["ieee"] == ieee:
                if d["_attrs"] is None:
                    try:
                        st = self._engine.get_device_state(ieee) or {}
                        d["_attrs"] = st.get("attributes", []) or []
                    except Exception:
                        d["_attrs"] = []
                return d["_attrs"]
        return []

    def _find_attr_meta(self, ieee: str, candidates: List[str]) -> Optional[Dict]:
        attrs = self._attrs_for(ieee)
        names = {a["attribute"].lower(): a for a in attrs}
        for c in candidates:
            if c in names:
                return names[c]
        # loose contains-match (e.g. "state_1" for "state")
        for c in candidates:
            for an, meta in names.items():
                if an.startswith(c) or c in an:
                    return meta
        return None

    def _match_device(self, text: str,
                      actuators_only: bool = False) -> Optional[Dict]:
        text = _norm(text)
        if not text:
            return None
        pool = [d for d in self._devices
                if (not actuators_only or d["ieee"] in self._act_ieees
                    or d["is_group"])]
        # Best full-name substring match (longest wins).
        best, best_len = None, 0
        for d in pool:
            n = d["norm"]
            if n and n in text and len(n) > best_len:
                best, best_len = d, len(n)
        if best:
            return best
        # Token-overlap fallback: most name tokens present.
        words = set(text.split())
        best, best_score = None, 0
        for d in pool:
            toks = set(d["norm"].split())
            if not toks:
                continue
            score = len(toks & words)
            if score >= max(1, len(toks) - 1) and score > best_score:
                best, best_score = d, score
        return best

    # ── Trigger parsing ─────────────────────────────────────────────────────

    def _parse_trigger(self, text: str
                       ) -> Tuple[Optional[str], Optional[Dict], Optional[str]]:
        dev = self._match_device(text)
        if not dev:
            dev, ambiguous = self._infer_device_by_concept(text)
            if ambiguous:
                return None, None, ambiguous
        if not dev:
            return None, None, (
                "I couldn't find the trigger device in \"" + text.strip() +
                "\". Known devices: " + self._device_hint())
        if dev["is_group"]:
            return None, None, ("Groups can't be a trigger source — name a "
                                "single device for the \"when\" part.")
        ieee = dev["ieee"]
        predicate = text.replace(dev["norm"], " ")
        cond = self._detect_predicate(ieee, predicate, text)
        if not cond:
            return ieee, None, (
                f"I found \"{dev['name']}\" but not what to check on it. "
                f"It exposes: {self._attr_hint(ieee)}.")
        return ieee, cond, f"{dev['name']} {cond['attribute']} "\
                           f"{cond['operator']} {cond['value']}"

    def _detect_predicate(self, ieee: str, predicate: str,
                          full: str) -> Optional[Dict]:
        p = " " + predicate.strip() + " "
        negated = bool(_NEGATION.search(p))

        # 1. Numeric comparison.
        m = re.search(
            r"\b(above|over|greater than|more than|higher than|at least|>=|>|"
            r"below|under|less than|lower than|fewer than|<=|<|"
            r"reaches|hits|equals?|is|=)\b\s*(-?\d+(?:\.\d+)?)", p)
        if m:
            word, num = m.group(1), float(m.group(2))
            if num.is_integer():
                num = int(num)
            op = ("gte" if word in ("at least", ">=") else
                  "gt" if word in ("above", "over", "greater than",
                                   "more than", "higher than", ">") else
                  "lte" if word in ("<=",) else
                  "lt" if word in ("below", "under", "less than",
                                   "lower than", "fewer than", "<") else "eq")
            meta = self._numeric_attr(ieee, full)
            if meta:
                return {"type": "attribute", "attribute": meta["attribute"],
                        "operator": op, "value": num}

        # 2. Motion / presence.
        if re.search(r"\b(motion|movement|presence|occupanc|occupied|someone|"
                     r"somebody|anyone|people)\b", p):
            meta = self._find_attr_meta(ieee, _MOTION_ATTRS)
            if meta:
                return self._bool_cond(meta, not negated)

        # 3. Contact (open / close).
        if re.search(r"\b(open|opens|opened|ajar)\b", p):
            return self._contact_cond(ieee, opened=True)
        if re.search(r"\b(closed?|shut)\b", p):
            return self._contact_cond(ieee, opened=False)

        # 4. On / off.
        if re.search(r"\b(on|active|running)\b", p) and \
                not re.search(r"\b(off|on\s+motion)\b", p):
            meta = self._find_attr_meta(ieee, _STATE_ATTRS)
            if meta:
                return self._bool_cond(meta, True)
        if re.search(r"\b(off|inactive|idle)\b", p):
            meta = self._find_attr_meta(ieee, _STATE_ATTRS)
            if meta:
                return self._bool_cond(meta, False)

        # 5. Button press.
        bm = re.search(r"\b(single|double|triple|press\w*|click\w*|hold|"
                       r"long\s*press)\b", p)
        if bm:
            meta = self._find_attr_meta(ieee, _BUTTON_ATTRS)
            if meta:
                word = bm.group(1)
                val = ("double" if "double" in word else
                       "triple" if "triple" in word else
                       "hold" if "hold" in word or "long" in word else "single")
                vo = [str(v).lower() for v in (meta.get("value_options") or [])]
                if vo and val not in vo:
                    val = meta["value_options"][0]
                return {"type": "attribute", "attribute": meta["attribute"],
                        "operator": "eq", "value": val}

        # 6. Bare value against an attribute's value_options.
        for meta in self._attrs_for(ieee):
            for opt in (meta.get("value_options") or []):
                if re.search(r"\b" + re.escape(str(opt).lower()) + r"\b", p):
                    return {"type": "attribute",
                            "attribute": meta["attribute"],
                            "operator": "eq",
                            "value": self._coerce(meta, str(opt))}
        return None

    def _infer_device_by_concept(self, text: str
                                 ) -> Tuple[Optional[Dict], Optional[str]]:
        """When the trigger names no device, deduce it from the concept
        ("motion", "door", "temperature") — but only if exactly one device
        exposes the matching attribute. Returns (device, ambiguity_error)."""
        cands: List[str] = []
        if re.search(r"\b(motion|movement|presence|occupanc|occupied|someone|"
                     r"somebody|anyone)\b", text):
            cands = _MOTION_ATTRS
        elif re.search(r"\b(door|window|contact|open|close|shut|ajar)\b", text):
            cands = _CONTACT_ATTRS
        else:
            for kw, attrs in _NUMERIC_KEYWORDS.items():
                if kw in text:
                    cands = attrs
                    break
        if not cands:
            return None, None
        matches = [d for d in self._devices
                   if not d["is_group"]
                   and self._find_attr_meta(d["ieee"], cands)]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            names = ", ".join(d["name"] for d in matches)
            return None, (f"Several devices could match that trigger ({names}). "
                          f"Please name the one you mean.")
        return None, None

    def _numeric_attr(self, ieee: str, text: str) -> Optional[Dict]:
        for kw, cands in _NUMERIC_KEYWORDS.items():
            if kw in text:
                meta = self._find_attr_meta(ieee, cands)
                if meta:
                    return meta
        # Sole numeric attribute on the device.
        nums = [a for a in self._attrs_for(ieee)
                if a.get("type") in ("integer", "float")]
        return nums[0] if len(nums) == 1 else None

    def _contact_cond(self, ieee: str, opened: bool) -> Optional[Dict]:
        meta = self._find_attr_meta(ieee, _CONTACT_ATTRS)
        if not meta:
            return None
        name = meta["attribute"].lower()
        vo = [str(v).lower() for v in (meta.get("value_options") or [])]
        if "open" in vo or "closed" in vo:
            return {"type": "attribute", "attribute": meta["attribute"],
                    "operator": "eq", "value": "open" if opened else "closed"}
        if name.startswith("is_open") or name == "opening":
            return self._bool_cond(meta, opened)
        if name.startswith("is_closed"):
            return self._bool_cond(meta, not opened)
        # Zigbee `contact` convention: True == closed.
        return self._bool_cond(meta, not opened)

    def _bool_cond(self, meta: Dict, truthy: bool) -> Dict:
        return {"type": "attribute", "attribute": meta["attribute"],
                "operator": "eq", "value": self._coerce(meta, truthy)}

    # ── Prerequisite parsing ────────────────────────────────────────────────

    def _parse_prerequisite(self, text: str
                            ) -> Tuple[Optional[Dict], Optional[str]]:
        _, tw, _ = self._extract_time_window(" " + text + " ")
        if tw:
            return ({"type": "time_window", "time_from": tw[0],
                     "time_to": tw[1], "days": list(range(7)),
                     "negate": False}, f"time {tw[0]}–{tw[1]}")
        dev = self._match_device(text)
        if not dev:
            return None, None
        negated = bool(re.search(r"\b(not|isn't|n't|no)\b", text))
        pred = self._detect_predicate(dev["ieee"],
                                      text.replace(dev["norm"], " "), text)
        if not pred:
            return None, None
        return ({"type": "device", "ieee": dev["ieee"],
                 "attribute": pred["attribute"], "operator": pred["operator"],
                 "value": pred["value"], "negate": negated},
                f"{dev['name']} {pred['attribute']} {pred['operator']} "
                f"{pred['value']}")

    # ── Action parsing ──────────────────────────────────────────────────────

    def _parse_action(self, text: str, source_ieee: Optional[str]
                      ) -> Tuple[List[Dict], Optional[str]]:
        steps: List[Dict] = []
        notes: List[str] = []
        # Split compound actions on " and ".
        for clause in re.split(r"\band\b", text):
            clause = clause.strip()
            if not clause:
                continue
            step, note = self._parse_single_action(clause, source_ieee)
            if step:
                steps.append(step)
                notes.append(note)
        if not steps:
            return [], ("I couldn't turn \"" + text.strip() +
                        "\" into a command (try \"turn on/off <device>\").")
        return steps, ", ".join(notes)

    def _parse_single_action(self, clause: str, source_ieee: Optional[str]
                             ) -> Tuple[Optional[Dict], Optional[str]]:
        vm = _ACTION_VERB_RE.search(clause)
        if not vm:
            return None, None
        verb = vm.group(1).lower()
        command = None
        for pat, cmd in _ACTION_VERBS:
            if re.fullmatch(pat, verb, re.I):
                command = cmd
                break

        # Value (percent / number).
        value = None
        pm = re.search(r"(\d+)\s*%", clause)
        num = re.search(r"\bto\s+(-?\d+)\b", clause) or \
            re.search(r"\b(\d+)\b", clause[vm.end():])

        # Resolve target device.
        after = clause[vm.end():]
        dev = self._match_device(after, actuators_only=True) or \
            self._match_device(clause, actuators_only=True)
        if not dev and (set(_norm(after).split()) & _PRONOUNS or not after.strip()):
            if source_ieee:
                dev = next((d for d in self._devices
                            if d["ieee"] == source_ieee), None)
        if not dev:
            return None, ("I couldn't find which device to control in \"" +
                          clause.strip() + "\".")

        # Resolve generic "set" → brightness / color_temp / position.
        if command == "set" or command == "brightness":
            if re.search(r"colou?r\s*temp|kelvin|mired|warm|cool", clause):
                command = "color_temp"
            else:
                command = "brightness"
        if command == "brightness":
            if pm:
                value = round(int(pm.group(1)) / 100 * 254)
            elif num:
                value = int(num.group(1))
            else:
                value = 254
            value = max(0, min(254, value))
        elif command == "color_temp":
            value = int(num.group(1)) if num else 370
        elif command == "position":
            value = int(pm.group(1)) if pm else (int(num.group(1)) if num else 50)
        elif command in ("open", "close") and pm:
            command = "position"
            value = int(pm.group(1))

        # Validate the command exists on the target (best-effort).
        endpoint_id = self._command_endpoint(dev, command)

        step = {"type": "command", "target_ieee": dev["ieee"],
                "command": command}
        if value is not None:
            step["value"] = value
        if endpoint_id is not None:
            step["endpoint_id"] = endpoint_id
        label = f"{command}{'=' + str(value) if value is not None else ''} "\
                f"→ {dev['name']}"
        return step, label

    def _command_endpoint(self, dev: Dict, command: str):
        for c in dev.get("commands", []):
            if c.get("command") == command:
                return c.get("endpoint_id")
        return None

    # ── Time / delay extraction ─────────────────────────────────────────────

    def _extract_delay(self, t: str) -> Tuple[str, Optional[int]]:
        m = re.search(r"\bafter\s+(\d+)\s*(second|sec|minute|min|hour|hr)s?\b", t)
        if not m:
            m = re.search(r"\bfor\s+(\d+)\s*(second|sec|minute|min|hour|hr)s?\b", t)
        if not m:
            return t, None
        n, unit = int(m.group(1)), m.group(2)
        secs = n * (3600 if unit.startswith(("hour", "hr")) else
                    60 if unit.startswith("min") else 1)
        return (t[:m.start()] + " " + t[m.end():]), secs

    _CLOCK = r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"

    def _extract_time_window(self, t: str
                             ) -> Tuple[str, Optional[Tuple[str, str]], Optional[str]]:
        C = self._CLOCK
        m = re.search(r"\bbetween\s+" + C + r"\s+and\s+" + C, t) or \
            re.search(r"\bfrom\s+" + C + r"\s+(?:to|until|till)\s+" + C, t)
        if m:
            a, b = self._clock(m.group(1)), self._clock(m.group(2))
            if a and b:
                return (t[:m.start()] + " " + t[m.end():]), (a, b), f"{a}–{b}"
        m = re.search(r"\b(?:after|from|past)\s+" + C, t)
        if m:
            a = self._clock(m.group(1))
            if a:
                return (t[:m.start()] + " " + t[m.end():]), (a, "23:59"), f"after {a}"
        m = re.search(r"\b(?:before|until|till|by)\s+" + C, t)
        if m:
            b = self._clock(m.group(1))
            if b:
                return (t[:m.start()] + " " + t[m.end():]), ("00:00", b), f"before {b}"
        m = re.search(r"\bat\s+" + C, t)
        if m:
            a = self._clock(m.group(1))
            if a:
                return (t[:m.start()] + " " + t[m.end():]), (a, "23:59"), f"at {a}"
        for phrase, win in _TIME_OF_DAY.items():
            if re.search(r"\b" + re.escape(phrase) + r"\b", t):
                return (t.replace(phrase, " "), win, phrase)
        return t, None, None

    @staticmethod
    def _clock(token: str) -> Optional[str]:
        token = token.strip().lower()
        m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)?$", token)
        if m:
            h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
            if ap == "pm" and h < 12:
                h += 12
            if ap == "am" and h == 12:
                h = 0
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return f"{h:02d}:{mi:02d}"
            return None
        m = re.match(r"^(\d{1,2})\s*(am|pm)$", token)
        if m:
            h, ap = int(m.group(1)), m.group(2)
            if ap == "pm" and h < 12:
                h += 12
            if ap == "am" and h == 12:
                h = 0
            if 0 <= h <= 23:
                return f"{h:02d}:00"
        m = re.match(r"^(\d{1,2}):(\d{2})$", token)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return f"{h:02d}:{mi:02d}"
        return None

    # ── Sentence segmentation ───────────────────────────────────────────────

    @staticmethod
    def _split_keyword(t: str, kw_pattern: str) -> Tuple[str, Optional[str]]:
        m = re.search(r"\b(" + kw_pattern + r")\b", t)
        if not m:
            return t, None
        return t[:m.start()].strip(), t[m.end():].strip()

    def _split_action(self, t: str) -> Tuple[Optional[str], Optional[str]]:
        """Return (action_clause, trigger_clause)."""
        vm = _ACTION_VERB_RE.search(t)
        if not vm:
            # No action verb → whole thing is the trigger (time-only or error).
            return None, self._strip_leads(t)
        # Action runs from the verb to the next clause boundary.
        tail = t[vm.start():]
        bm = re.search(r"\b(when|if|whenever)\b|,", tail)
        end = vm.start() + (bm.start() if bm else len(tail))
        action = t[vm.start():end].strip()
        remainder = (t[:vm.start()] + " " + t[end:]).strip()
        trigger = self._strip_leads(remainder)
        return action, trigger

    @staticmethod
    def _strip_leads(t: str) -> str:
        return re.sub(r"^\s*(when|if|whenever|then|,|and)\b\s*", "",
                      t.strip(), flags=re.I).strip()

    # ── Coercion / helpers ──────────────────────────────────────────────────

    @staticmethod
    def _coerce(meta: Dict, logical):
        vo = [str(v).lower() for v in (meta.get("value_options") or [])]
        t = meta.get("type")
        if logical is True or logical is False:
            if "on" in vo or "off" in vo:
                return "ON" if logical else "OFF"
            return bool(logical)
        if isinstance(logical, str):
            lo = logical.lower()
            if lo in ("on", "off"):
                return logical.upper() if ("on" in vo or "off" in vo) \
                    else (lo == "on")
            if t == "boolean":
                return lo in ("true", "open", "on", "yes")
        return logical

    def _first_command_target(self, steps: List[Dict]) -> Optional[str]:
        for s in steps:
            if s.get("type") == "command" and s.get("target_ieee"):
                return s["target_ieee"]
        return None

    def _opposite_state_condition(self, ieee: str,
                                  steps: List[Dict]) -> Optional[Dict]:
        cmd = next((s.get("command") for s in steps
                    if s.get("type") == "command"), None)
        meta = self._find_attr_meta(ieee, _STATE_ATTRS)
        if not meta:
            return None
        # "turn off after N" fires while the device is ON, and vice-versa.
        truthy = (cmd == "off")
        return self._bool_cond(meta, truthy)

    def _any_source(self) -> Optional[str]:
        for d in self._devices:
            if not d["is_group"]:
                return d["ieee"]
        return None

    def _name_for(self, ieee: str) -> str:
        for d in self._devices:
            if d["ieee"] == ieee:
                return d["name"]
        return ieee

    def _make_name(self, source_ieee: str, then_seq: List[Dict],
                   conditions: List[Dict]) -> str:
        tgt = self._first_command_target(then_seq)
        cmd = next((s.get("command") for s in then_seq
                    if s.get("type") == "command"), "run")
        base = self._name_for(tgt) if tgt else self._name_for(source_ieee)
        verb = {"on": "On", "off": "Off", "toggle": "Toggle",
                "brightness": "Dim", "open": "Open", "close": "Close",
                "lock": "Lock", "unlock": "Unlock"}.get(cmd, cmd.title())
        return f"{base} {verb}"[:60]

    def _explain(self, rule: Dict) -> str:
        parts = []
        src = self._name_for(rule["source_ieee"])
        conds = rule["conditions"]
        only_time = all(c.get("type") == "time_window" for c in conds)
        if only_time:
            c = conds[0]
            parts.append(f"While the time is {c['time_from']}–{c['time_to']}")
        else:
            cterms = []
            for c in conds:
                if c.get("type") == "time_window":
                    cterms.append(f"time is {c['time_from']}–{c['time_to']}")
                else:
                    cterms.append(f"{c['attribute']} {c['operator']} {c['value']}")
            parts.append(f"When {src} " + " and ".join(cterms))
        for p in rule.get("prerequisites", []):
            if p.get("type") == "time_window":
                parts.append(f"only between {p['time_from']}–{p['time_to']}")
            else:
                neg = "NOT " if p.get("negate") else ""
                parts.append(f"only if {neg}{self._name_for(p['ieee'])} "
                             f"{p['attribute']} {p['operator']} {p['value']}")
        then = self._steps_text(rule.get("then_sequence", []))
        if then:
            parts.append("then " + then)
        els = self._steps_text(rule.get("else_sequence", []))
        if els:
            parts.append("otherwise " + els)
        return " → ".join(parts)

    def _steps_text(self, steps: List[Dict]) -> str:
        out = []
        for s in steps:
            if s.get("type") == "command":
                v = f" to {s['value']}" if s.get("value") is not None else ""
                out.append(f"{s['command']} {self._name_for(s['target_ieee'])}{v}")
            elif s.get("type") == "delay":
                out.append(f"wait {s['seconds']}s")
        return ", ".join(out)

    def _device_hint(self) -> str:
        names = [d["name"] for d in self._devices[:8]]
        return ", ".join(names) + ("…" if len(self._devices) > 8 else "")

    def _attr_hint(self, ieee: str) -> str:
        attrs = [a["attribute"] for a in self._attrs_for(ieee)[:8]]
        return ", ".join(attrs) if attrs else "(no readable attributes)"

    def _fail(self, error: str, matched: Optional[Dict] = None) -> Dict[str, Any]:
        return {"success": False, "source": "local", "error": error,
                "partial": matched or {}, "examples": _EXAMPLES}
