"""
Local natural-language automation parser — a deterministic, dependency-free
compiler from constrained English to the rule dict AutomationEngine consumes.

No LLM and no network call: a parse is microseconds of pure-Python string work,
and every device, attribute and value is grounded against the live registry
rather than guessed. Tried first by POST /api/ai/automation, with the LLM as
fallback. Supported sentence shapes: docs/automations.md.
"""

import difflib
import re
from typing import Any, Dict, List, Optional, Tuple

# Lexicons

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

# Media verbs that aren't already device verbs, so _split_action can find the
# action clause for "announce …", "pause the lounge speaker", etc. ("stop" is
# already a device verb; media intent there is decided by a matched player.)
_MEDIA_VERB_RE = re.compile(
    r"\b(announce|say|speak|pause|resume|skip|next|previous)\b|\bvolume\b", re.I)

_PRONOUNS = {"it", "them", "that", "this", "those", "they"}

# Attribute candidate lists per semantic concept (resolved against real attrs).
_MOTION_ATTRS = ["occupancy", "presence", "motion", "occupied", "presence_state"]
_CONTACT_ATTRS = ["contact", "is_open", "opening", "door", "window", "is_closed"]
_STATE_ATTRS = ["state", "state_1", "state_l1", "on", "on_1", "on_off"]
_BUTTON_ATTRS = ["action", "click", "button_action", "event", "scene"]
_LUX_ATTRS = ["illuminance", "illuminance_lux", "lux", "light_level", "illumination"]

# Ambient-light thresholds (lux) for word-based "dark"/"bright" triggers.
DARK_LUX = 10
BRIGHT_LUX = 50

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
    "turn on the porch light for 5 minutes when motion is detected",
    "set the bedroom lights to 40% when motion is detected",
    "turn on all the bedroom lights at sunset",
    "when motion is detected and it is dark turn on the hall light",
    "turn on the lamp when it gets dark",
    "turn on the hallway lights between 08:00 and 23:30",
    "when the kitchen temperature goes above 25 turn on the fan otherwise turn it off",
    "when the front door opens announce \"front door opened\" on the kitchen speaker",
    "pause the lounge speaker when motion clears in the lounge",
]

# Words that carry no meaning when matching device names in an action clause.
_STOPWORDS = {"all", "every", "each", "the", "a", "an", "my", "our", "in",
              "of", "to", "at", "on", "off", "and", "then", "please", "room"}

# command → its reverting command, for "for N minutes" auto-revert semantics.
_OPPOSITE_CMD = {"on": "off", "off": "on", "open": "close", "close": "open",
                 "lock": "unlock", "unlock": "lock", "brightness": "off"}

# Spelled-out quantities accepted in delays ("after five minutes").
_WORD_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
                 "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                 "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30,
                 "forty": 40, "fifty": 50, "sixty": 60, "ninety": 90}


def _norm(name: str) -> str:
    """Lowercase, strip emoji/punctuation, collapse whitespace."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    return re.sub(r"\s+", " ", s).strip()


class NLAutomationParser:
    def __init__(self, engine):
        self._engine = engine
        self._devices: List[Dict[str, Any]] = []
        self._act_ieees: set = set()
        self._players: Optional[List[Dict[str, Any]]] = None  # media players, lazily loaded


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
        t, delay_secs, delay_kind = self._extract_delay(t)
        t, temporal_cond, time_phrase = self._extract_temporal(t)
        if time_phrase:
            matched["time"] = time_phrase

        # 2. Separate the action clause from the trigger clause.
        action_text, trigger_text = self._split_action(t)

        conditions: List[Dict] = []
        prerequisites: List[Dict] = []
        source_ieee: Optional[str] = None

        # 3. Trigger (device + predicate). "and" chains a gating clause onto
        #    the trigger ("when motion is detected and it is dark"): the first
        #    segment is the trigger, the rest become prerequisites.
        if trigger_text and _norm(trigger_text):
            segs = [s for s in re.split(r"\band\b", trigger_text) if _norm(s)]
            src, cond, note = self._parse_trigger(segs[0] if segs else trigger_text)
            if len(segs) > 1 and not (src and cond):
                # The "and" may be part of a device name — retry unsplit.
                segs = [trigger_text]
                src, cond, note = self._parse_trigger(trigger_text)
            if src and cond:
                source_ieee = src
                conditions.append(cond)
                matched["trigger"] = note
                for seg in segs[1:]:
                    pre, pnote = self._parse_prerequisite(seg)
                    if pre:
                        prerequisites.append(pre)
                        matched["prerequisite"] = pnote
                    else:
                        return self._fail(
                            "I understood the trigger but not the extra "
                            "condition \"" + seg.strip() + "\".", matched)
            elif note:
                return self._fail(note, matched)

        # 4. Temporal window (clock or sun). With a device trigger it gates
        #    (prerequisite); alone it IS the trigger condition.
        if temporal_cond:
            tw = {**temporal_cond, "days": list(range(7)), "negate": False}
            if conditions:
                prerequisites.append(tw)
            else:
                conditions.append(tw)

        # 5. THEN action(s). "after N" delays the action; "for N" acts now and
        #    schedules the reverting command once the delay elapses.
        then_seq: List[Dict] = []
        if delay_secs and delay_kind == "after":
            then_seq.append({"type": "delay", "seconds": delay_secs})
        action_steps: List[Dict] = []
        if action_text:
            steps, note = self._parse_action(action_text, source_ieee)
            if steps:
                action_steps = steps
                then_seq.extend(steps)
                matched["action"] = note
            elif note and not then_seq:
                return self._fail(note, matched)

        if not then_seq:
            return self._fail(
                "I couldn't find an action to perform (e.g. \"turn on the lamp\").",
                matched)

        # 5b. "for N minutes" — do X, wait, undo X. Without a trigger this is the
        #     classic auto-revert timer, keyed on the device reaching the acted state.
        if delay_secs and delay_kind == "for":
            reverts = self._revert_steps(action_steps)
            anchor = self._first_command_target(action_steps)
            if reverts and conditions:
                then_seq.append({"type": "delay", "seconds": delay_secs})
                then_seq.extend(reverts)
                matched["timer"] = f"revert after {delay_secs}s"
            elif reverts and anchor and not anchor.startswith("group:"):
                acted = self._acted_state_condition(anchor, action_steps)
                if acted:
                    source_ieee = anchor
                    conditions.append(acted)
                    then_seq = [{"type": "delay", "seconds": delay_secs},
                                *reverts]
                    matched["trigger"] = "auto-revert timer on the target device"
                else:
                    then_seq.insert(0, {"type": "delay", "seconds": delay_secs})
            else:
                # No revertable command — fall back to plain delayed action.
                then_seq.insert(0, {"type": "delay", "seconds": delay_secs})

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

    # Device registry

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
        # loose match (e.g. "state_1" for "state"). Containment needs a
        # substantial candidate — "on" ⊂ "position" must NOT match.
        for c in candidates:
            for an, meta in names.items():
                if an.startswith(c) or (len(c) >= 4 and c in an):
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
        if best:
            return best
        # Fuzzy fallback: typo-tolerant per-token match ("hallway lite",
        # "kichen light"). A device matches when (nearly) all of its name
        # tokens have a close counterpart in the text.
        fbest, fbest_score = None, 0.0
        for d in pool:
            toks = d["norm"].split()
            if not toks:
                continue
            hits = sum(1 for tk in toks if self._token_close(tk, words))
            if hits < max(1, len(toks) - 1):
                continue
            score = hits / len(toks) + len(toks) * 0.01  # specific names win
            if score > fbest_score:
                fbest, fbest_score = d, score
        return fbest

    @staticmethod
    def _token_close(token: str, words: set, cutoff: float = 0.8) -> bool:
        """True if a text word equals or is a near-miss of the name token
        (typos, singular/plural)."""
        if token in words:
            return True
        if len(token) < 3:
            return False
        for w in words:
            if len(w) < 3:
                continue
            if w == token + "s" or token == w + "s":
                return True
            if difflib.SequenceMatcher(None, token, w).ratio() >= cutoff:
                return True
        return False

    def _suggest_devices(self, text: str, limit: int = 3) -> List[str]:
        """Closest device names to a clause, for did-you-mean feedback."""
        words = [w for w in _norm(text).split() if w not in _STOPWORDS]
        if not words:
            return []
        scored = []
        for d in self._devices:
            toks = d["norm"].split()
            if not toks:
                continue
            # How well the query words are covered by this device's name.
            s = sum(max((difflib.SequenceMatcher(None, w, tk).ratio()
                         for tk in toks), default=0.0) for w in words) / len(words)
            scored.append((s, d["name"]))
        scored.sort(key=lambda x: -x[0])
        return [n for s, n in scored[:limit] if s >= 0.55]

    # Trigger parsing

    def _parse_trigger(self, text: str
                       ) -> Tuple[Optional[str], Optional[Dict], Optional[str]]:
        dev = self._match_device(text)
        if not dev:
            dev, ambiguous = self._infer_device_by_concept(text)
            if ambiguous:
                return None, None, ambiguous
        if not dev:
            sug = self._suggest_devices(text)
            hint = ("Did you mean: " + ", ".join(sug) + "?") if sug else \
                   ("Known devices: " + self._device_hint())
            return None, None, (
                "I couldn't find the trigger device in \"" + text.strip() +
                "\". " + hint)
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

        # 0. Ambient light by word ("dark" / "bright") → illuminance threshold.
        #    Fully dynamic: the sensor reports real lux, so no seasonal drift.
        if re.search(r"\b(dark|darkness|low light|gloomy|dingy)\b", p):
            meta = self._find_attr_meta(ieee, _LUX_ATTRS)
            if meta:
                return {"type": "attribute", "attribute": meta["attribute"],
                        "operator": "lt", "value": DARK_LUX}
        if re.search(r"\b(bright|daylight|well[- ]?lit|sunny)\b", p):
            meta = self._find_attr_meta(ieee, _LUX_ATTRS)
            if meta:
                return {"type": "attribute", "attribute": meta["attribute"],
                        "operator": "gt", "value": BRIGHT_LUX}

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
        elif re.search(r"\b(dark|darkness|low light|bright|daylight|lux|"
                       r"illuminanc|light level|gloomy)\b", text):
            cands = _LUX_ATTRS
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

    # Prerequisite parsing

    def _parse_prerequisite(self, text: str
                            ) -> Tuple[Optional[Dict], Optional[str]]:
        _, tcond, phrase = self._extract_temporal(" " + text + " ")
        if tcond:
            return ({**tcond, "days": list(range(7)), "negate": False},
                    phrase or "time")
        dev = self._match_device(text)
        if not dev:
            # No device named ("…and it is dark") — infer from the concept
            # when exactly one device can answer it.
            dev, _ambiguous = self._infer_device_by_concept(" " + text + " ")
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

    # Action parsing

    def _parse_action(self, text: str, source_ieee: Optional[str]
                      ) -> Tuple[List[Dict], Optional[str]]:
        steps: List[Dict] = []
        notes: List[str] = []
        # Split compound actions on " and ".
        errors: List[str] = []
        for clause in re.split(r"\band\b", text):
            clause = clause.strip()
            if not clause:
                continue
            csteps, note = self._parse_single_action(clause, source_ieee)
            if csteps:
                steps.extend(csteps)
                notes.append(note)
            elif note:
                errors.append(note)
        if not steps:
            # A clause-level error (with did-you-mean hints) beats the
            # generic fallback.
            return [], (errors[0] if errors else
                        "I couldn't turn \"" + text.strip() +
                        "\" into a command (try \"turn on/off <device>\").")
        return steps, ", ".join(notes)

    def _parse_single_action(self, clause: str, source_ieee: Optional[str]
                             ) -> Tuple[List[Dict], Optional[str]]:
        # Media intent (announce / control / volume on a named player) first —
        # it only succeeds when a real player is matched, so device rules are
        # untouched (e.g. "stop the kitchen light" stays a device command).
        mstep, mnote = self._parse_media_action(clause)
        if mstep:
            return [mstep], mnote

        vm = _ACTION_VERB_RE.search(clause)
        if not vm:
            return [], None
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

        # Resolve target device(s). "all/every/each" fans out to every
        # actuator whose name matches the remaining words ("all the bedroom
        # lights" → every *bedroom light* device, or the matching group).
        after = clause[vm.end():]
        targets: List[Dict] = []
        if re.search(r"\b(all|every|each)\b", clause, re.I):
            targets = self._match_all_devices(after or clause)
        if not targets:
            dev = self._match_device(after, actuators_only=True) or \
                self._match_device(clause, actuators_only=True)
            if not dev and (set(_norm(after).split()) & _PRONOUNS
                            or not after.strip()):
                if source_ieee:
                    dev = next((d for d in self._devices
                                if d["ieee"] == source_ieee), None)
            if dev:
                targets = [dev]
        if not targets:
            sug = self._suggest_devices(after or clause)
            hint = (" Did you mean: " + ", ".join(sug) + "?") if sug else ""
            return [], ("I couldn't find which device to control in \"" +
                        clause.strip() + "\"." + hint)

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

        steps = []
        for dev in targets:
            step = {"type": "command", "target_ieee": dev["ieee"],
                    "command": command}
            if value is not None:
                step["value"] = value
            endpoint_id = self._command_endpoint(dev, command)
            if endpoint_id is not None:
                step["endpoint_id"] = endpoint_id
            steps.append(step)
        names = ", ".join(d["name"] for d in targets)
        label = f"{command}{'=' + str(value) if value is not None else ''} "\
                f"→ {names}"
        return steps, label

    def _match_all_devices(self, text: str) -> List[Dict]:
        """Every actuator whose name matches the clause words ("all the
        bedroom lights"). Prefers a matching group (one command) over
        fanning out to its member devices."""
        words = [w for w in _norm(text).split() if w not in _STOPWORDS]
        # Drop bare numbers (values like "50" aren't name words).
        words = [w for w in words if not w.isdigit()]
        if not words:
            return []
        matches, groups = [], []
        for d in self._devices:
            if not (d["ieee"] in self._act_ieees or d["is_group"]):
                continue
            toks = set(d["norm"].split())
            if not toks:
                continue
            # Every clause word must land in the device name (typo/plural
            # tolerant) — "bedroom lights" matches "Bedroom Light 1", not
            # "Bedroom Socket".
            if all(self._token_close(w, toks) for w in words):
                (groups if d["is_group"] else matches).append(d)
        if groups:
            return groups[:1] if len(groups) > 1 else groups
        return matches

    def _command_endpoint(self, dev: Dict, command: str):
        for c in dev.get("commands", []):
            if c.get("command") == command:
                return c.get("endpoint_id")
        return None

    # Media actions (announce / control / volume on a player)
    def _load_players(self) -> List[Dict[str, Any]]:
        if self._players is not None:
            return self._players
        self._players = []
        try:
            getter = getattr(self._engine, "_get_media_service", None)
            svc = getter() if getter else None
            if svc and getattr(svc, "enabled", False):
                self._players = [{"player_id": p.player_id, "name": p.name}
                                 for p in svc.controller.snapshot()]
                # OpenZone zones answer to their name like any other target;
                # what they accept differs, which _parse_media_action handles.
                sync = getattr(svc, "cast_sync", None)
                if sync is not None:
                    self._players += [
                        {"player_id": "zone:" + g["id"], "name": g["name"],
                         "is_zone": True}
                        for g in sync.list_groups().get("groups", [])]
        except Exception:
            self._players = []
        return self._players

    def _match_player(self, text: str) -> Optional[Dict[str, Any]]:
        nt = _norm(text)
        best = None
        for p in self._load_players():
            pn = _norm(p["name"])
            if pn and pn in nt and (best is None or len(pn) > len(_norm(best["name"]))):
                best = p
        return best

    def _parse_media_action(self, clause: str
                            ) -> Tuple[Optional[Dict], Optional[str]]:
        if not self._load_players():
            return None, None
        player = self._match_player(clause)
        if not player:
            return None, None
        low = " " + clause.lower() + " "
        pid, pname = player["player_id"], player["name"]

        # ANNOUNCE / SAY / SPEAK
        if re.search(r"\b(announce|say|speak)\b", low):
            text = self._extract_announce_text(clause)
            if not text:
                return None, None
            return ({"type": "media", "player_id": pid,
                     "media_action": "announce", "text": text},
                    f'announce "{text[:30]}" → {pname}')

        # VOLUME ("set the kitchen speaker to 30%", "kitchen speaker volume 30%")
        pm = re.search(r"(\d+)\s*%", clause)
        if pm and re.search(r"\b(volume|vol|set)\b", low):
            return ({"type": "media", "player_id": pid, "media_action": "volume",
                     "volume": max(0.0, min(1.0, int(pm.group(1)) / 100))},
                    f"volume {pm.group(1)}% → {pname}")

        # CONTROL (pause / resume / stop / next / previous)
        # A zone has no transport of its own: "play it" means start its saved
        # source, and the queue verbs have nothing to act on.
        zone = bool(player.get("is_zone"))
        for pat, act in ((r"\bpause\b", "pause"), (r"\b(resume|unpause|play)\b", "resume"),
                         (r"\b(skip|next)\b", "next"), (r"\b(previous|prev|back)\b", "prev"),
                         (r"\bstop\b", "stop")):
            if not re.search(pat, low):
                continue
            if zone and act == "resume":
                return ({"type": "media", "player_id": pid,
                         "media_action": "play_zone"}, f"play {pname}")
            if zone and act != "stop":
                return None, None
            return ({"type": "media", "player_id": pid,
                     "media_action": "control", "control_action": act},
                    f"{act} → {pname}")
        return None, None

    def _extract_announce_text(self, clause: str) -> str:
        # Quoted text is unambiguous — prefer it.
        q = re.search(r'["“‘]([^"“”‘’]+)["”’]'
                      r"|'([^']+)'", clause)
        if q:
            return (q.group(1) or q.group(2) or "").strip()
        m = re.search(r"\b(announce|say|speak)\b\s*(?:that\s+|:\s*)?", clause, re.I)
        if not m:
            return ""
        rest = clause[m.end():].strip()
        # Drop a trailing " on <player>" target phrase (target is last).
        idx = rest.lower().rfind(" on ")
        if idx > 0:
            rest = rest[:idx]
        return rest.strip(" .,:\"'")

    # Time / delay extraction

    _QTY = r"(\d+|" + "|".join(_WORD_NUMBERS) + r"|half\s+an?)"
    _UNIT = r"(second|sec|minute|min|hour|hr)s?"

    def _extract_delay(self, t: str) -> Tuple[str, Optional[int], Optional[str]]:
        """Pull "after/for N <unit>" out of the text.

        Returns (text, seconds, kind) where kind is "after" (delay before
        acting) or "for" (act now, revert after the delay) — they mean
        different rules and are handled differently by parse().
        """
        m = re.search(r"\b(after|for)\s+" + self._QTY + r"\s*" + self._UNIT
                      + r"\b", t)
        if not m:
            return t, None, None
        kind, qty, unit = m.group(1), m.group(2), m.group(3)
        if qty.startswith("half"):
            n = 0.5
        elif qty.isdigit():
            n = int(qty)
        else:
            n = _WORD_NUMBERS.get(qty, 0)
        secs = round(n * (3600 if unit.startswith(("hour", "hr")) else
                          60 if unit.startswith("min") else 1))
        if secs <= 0:
            return t, None, None
        return (t[:m.start()] + " " + t[m.end():]), secs, kind

    _CLOCK = r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midday|midnight)"

    def _extract_temporal(self, t: str
                          ) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
        """Pull a temporal window out of the text, returning a complete
        condition dict (type 'sun' or 'time_window') the engine understands."""
        nt, sun, phrase = self._extract_sun_window(t)
        if sun:
            return nt, sun, phrase
        nt, win, phrase = self._extract_clock_window(t)
        if win:
            return nt, {"type": "time_window", "time_from": win[0],
                        "time_to": win[1]}, phrase
        return t, None, None

    def _extract_sun_window(self, t: str
                            ) -> Tuple[str, Optional[Dict[str, str]], Optional[str]]:
        """Emit a SYMBOLIC sun condition ({from,to} ∈ sunrise/sunset/HH:MM).
        The engine resolves these to live local times each evaluation, so the
        rule tracks the seasons instead of freezing to today's clock."""
        if "sunrise" not in t and "sunset" not in t:
            return t, None, None
        patterns = [
            (r"\bbetween\s+sunrise\s+and\s+sunset\b", ("sunrise", "sunset"), "sunrise→sunset"),
            (r"\bbetween\s+sunset\s+and\s+sunrise\b", ("sunset", "sunrise"), "sunset→sunrise"),
            (r"\b(?:after|from|past|at)\s+sunset\b", ("sunset", "sunrise"), "after sunset"),
            (r"\b(?:before|until|till|by)\s+sunset\b", ("00:00", "sunset"), "before sunset"),
            (r"\b(?:after|from|past)\s+sunrise\b", ("sunrise", "23:59"), "after sunrise"),
            (r"\b(?:before|until|till|by)\s+sunrise\b", ("00:00", "sunrise"), "before sunrise"),
            (r"\bat\s+sunrise\b", ("sunrise", "23:59"), "at sunrise"),
        ]
        for pat, (frm, to), phrase in patterns:
            m = re.search(pat, t)
            if m:
                cond = {"type": "sun", "from": frm, "to": to}
                return (t[:m.start()] + " " + t[m.end():]), cond, phrase
        return t, None, None

    def _extract_clock_window(self, t: str
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
        if token in ("noon", "midday"):
            return "12:00"
        if token == "midnight":
            return "00:00"
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

    # Sentence segmentation

    @staticmethod
    def _split_keyword(t: str, kw_pattern: str) -> Tuple[str, Optional[str]]:
        m = re.search(r"\b(" + kw_pattern + r")\b", t)
        if not m:
            return t, None
        return t[:m.start()].strip(), t[m.end():].strip()

    def _split_action(self, t: str) -> Tuple[Optional[str], Optional[str]]:
        """Return (action_clause, trigger_clause)."""
        cands = [m for m in (_ACTION_VERB_RE.search(t), _MEDIA_VERB_RE.search(t)) if m]
        if not cands:
            # No action verb → whole thing is the trigger (time-only or error).
            return None, self._strip_leads(t)
        # Action runs from the earliest matching verb to the next clause boundary.
        start = min(m.start() for m in cands)
        tail = t[start:]
        bm = re.search(r"\b(when|if|whenever)\b|,", tail)
        end = start + (bm.start() if bm else len(tail))
        action = t[start:end].strip()
        remainder = (t[:start] + " " + t[end:]).strip()
        trigger = self._strip_leads(remainder)
        return action, trigger

    @staticmethod
    def _strip_leads(t: str) -> str:
        return re.sub(r"^\s*(when|if|whenever|then|,|and)\b\s*", "",
                      t.strip(), flags=re.I).strip()

    # Coercion / helpers

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

    def _revert_steps(self, steps: List[Dict]) -> List[Dict]:
        """Reverting commands for each command step (on→off, open→close…).
        Returns [] when any command has no clean opposite."""
        out = []
        for s in steps:
            if s.get("type") != "command":
                continue
            opp = _OPPOSITE_CMD.get(s.get("command"))
            if not opp:
                return []
            step = {"type": "command", "target_ieee": s["target_ieee"],
                    "command": opp}
            if s.get("endpoint_id") is not None:
                step["endpoint_id"] = s["endpoint_id"]
            out.append(step)
        return out

    def _acted_state_condition(self, ieee: str,
                               steps: List[Dict]) -> Optional[Dict]:
        """Condition matching the state the action puts the device in —
        the trigger for a standalone "for N minutes" auto-revert rule."""
        cmd = next((s.get("command") for s in steps
                    if s.get("type") == "command"), None)
        meta = self._find_attr_meta(ieee, _STATE_ATTRS)
        if not meta:
            return None
        return self._bool_cond(meta, cmd in ("on", "brightness"))

    def _opposite_state_condition(self, ieee: str,
                                  steps: List[Dict]) -> Optional[Dict]:
        cmd = next((s.get("command") for s in steps
                    if s.get("type") == "command"), None)
        meta = self._find_attr_meta(ieee, _STATE_ATTRS)
        if meta:
            # "turn off after N" fires while the device is ON, and vice-versa.
            return self._bool_cond(meta, cmd == "off")
        # Positional device (blind/cover): "close after N" fires while open.
        if cmd in ("open", "close"):
            meta = self._find_attr_meta(ieee, ["position", "current_position"])
            if meta:
                return {"type": "attribute", "attribute": meta["attribute"],
                        "operator": "gt" if cmd == "close" else "lt",
                        "value": 0 if cmd == "close" else 100}
        return None

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
        only_time = all(c.get("type") in ("time_window", "sun") for c in conds)
        if only_time:
            parts.append("While " + self._temporal_phrase(conds[0]))
        else:
            cterms = []
            for c in conds:
                if c.get("type") in ("time_window", "sun"):
                    cterms.append(self._temporal_phrase(c))
                else:
                    cterms.append(f"{c['attribute']} {c['operator']} {c['value']}")
            parts.append(f"When {src} " + " and ".join(cterms))
        for p in rule.get("prerequisites", []):
            if p.get("type") in ("time_window", "sun"):
                parts.append("only " + self._temporal_phrase(p))
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

    @staticmethod
    def _temporal_phrase(c: Dict) -> str:
        if c.get("type") == "sun":
            def lbl(x):
                return {"sunrise": "sunrise", "sunset": "sunset"}.get(x, x)
            return f"between {lbl(c.get('from'))} and {lbl(c.get('to'))}"
        return f"the time is {c['time_from']}–{c['time_to']}"

    def _device_hint(self) -> str:
        names = [d["name"] for d in self._devices[:8]]
        return ", ".join(names) + ("…" if len(self._devices) > 8 else "")

    def _attr_hint(self, ieee: str) -> str:
        attrs = [a["attribute"] for a in self._attrs_for(ieee)[:8]]
        return ", ".join(attrs) if attrs else "(no readable attributes)"

    def _fail(self, error: str, matched: Optional[Dict] = None) -> Dict[str, Any]:
        return {"success": False, "source": "local", "error": error,
                "partial": matched or {}, "examples": _EXAMPLES}
