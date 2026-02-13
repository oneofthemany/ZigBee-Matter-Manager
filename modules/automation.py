"""
Automation Engine - State Machine with Action Sequences
=======================================================
Evaluates device state changes and fires action sequences on
state TRANSITIONS (not on every update).

Core concept:
  - Rule conditions are evaluated on every source device state change
  - The engine tracks whether conditions are MATCHED or UNMATCHED
  - Only on TRANSITIONS between states do sequences fire:
      unmatched → matched  →  THEN sequence
      matched → unmatched  →  ELSE sequence
  - This prevents command spam on repeated sensor updates

Action step types:
  command   - Send ZigBee command to a device
  delay     - Wait N seconds before next step
  wait_for  - Pause until a device state matches (with timeout)
  condition - Inline gate: skip remaining steps if false

Persistence: ./data/automations.json
Hook:        core.py -> _debounced_device_update
Execution:   device.send_command() (direct zigpy)
"""

import asyncio
import json
import logging
import os
import time
import traceback
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("modules.automation")

MAX_RULES_PER_DEVICE = 10
MAX_CONDITIONS_PER_RULE = 5
MAX_PREREQUISITES_PER_RULE = 5
MAX_STEPS_PER_SEQUENCE = 10
DATA_FILE = "./data/automations.json"
DEFAULT_COOLDOWN = 5
WAIT_FOR_POLL_INTERVAL = 2  # seconds

OPERATORS = {
    "eq":  lambda a, b: a == b,
    "neq": lambda a, b: a != b,
    "gt":  lambda a, b: float(a) > float(b),
    "lt":  lambda a, b: float(a) < float(b),
    "gte": lambda a, b: float(a) >= float(b),
    "lte": lambda a, b: float(a) <= float(b),
}

VALID_COMMANDS = {
    "on", "off", "toggle", "brightness", "color_temp",
    "open", "close", "stop", "position", "temperature"
}

VALID_STEP_TYPES = {"command", "delay", "wait_for", "condition"}


class AutomationEngine:

    def __init__(self, device_registry_getter: Callable[[], Dict],
                 friendly_names_getter: Callable[[], Dict],
                 event_emitter: Optional[Callable] = None):
        self._get_devices = device_registry_getter
        self._get_names = friendly_names_getter
        self._event_emitter = event_emitter

        self.rules: List[Dict[str, Any]] = []
        self._source_index: Dict[str, List[str]] = {}
        self._cooldowns: Dict[str, float] = {}
        self._sustain_tracker: Dict[str, float] = {}

        # State machine: rule_id -> "matched" | "unmatched" | None (unknown)
        self._rule_states: Dict[str, Optional[str]] = {}

        # Running sequences: rule_id -> asyncio.Task (cancellable)
        self._running_sequences: Dict[str, asyncio.Task] = {}

        # Trace
        self._trace_log: List[Dict[str, Any]] = []
        self._max_trace_entries = 200

        self._stats = {
            "evaluations": 0, "matches": 0, "transitions": 0,
            "executions": 0, "execution_successes": 0,
            "execution_failures": 0, "errors": 0,
        }

        self._load_rules()
        logger.info(f"Automation engine initialised with {len(self.rules)} rule(s)")

    # =========================================================================
    # PERSISTENCE
    # =========================================================================

    def _load_rules(self):
        if not os.path.exists(DATA_FILE):
            self.rules = []
            self._rebuild_index()
            return
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
            self.rules = data.get("rules", [])
            migrated = self._migrate_rules()
            if migrated:
                self._save_rules()
                logger.info(f"Migrated {migrated} rule(s) to sequence format")
            self._rebuild_index()
            logger.info(f"Loaded {len(self.rules)} automation rule(s)")
        except Exception as e:
            logger.error(f"Failed to load automations: {e}")
            self.rules = []
            self._rebuild_index()

    def _migrate_rules(self) -> int:
        """Migrate legacy rule formats to current sequence format."""
        count = 0
        for rule in self.rules:
            if "name" not in rule:
                rule["name"] = ""

            # Legacy: threshold -> conditions
            if "threshold" in rule and "conditions" not in rule:
                rule["conditions"] = [rule.pop("threshold")]
                count += 1

            # Legacy: action dict -> then_sequence
            if "action" in rule and "then_sequence" not in rule:
                action = rule.pop("action")
                target = rule.pop("target_ieee", "")
                steps = []
                delay = action.get("delay", 0) or 0
                if delay > 0:
                    steps.append({"type": "delay", "seconds": delay})
                steps.append({
                    "type": "command",
                    "target_ieee": target,
                    "command": action.get("command", "on"),
                    "value": action.get("value"),
                    "endpoint_id": action.get("endpoint_id"),
                })
                rule["then_sequence"] = steps
                rule["else_sequence"] = rule.get("else_sequence", [])
                count += 1

            # Ensure sequences exist
            if "then_sequence" not in rule:
                rule["then_sequence"] = []
            if "else_sequence" not in rule:
                rule["else_sequence"] = []
            if "prerequisites" not in rule:
                rule["prerequisites"] = []
        return count

    def _save_rules(self):
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        try:
            with open(DATA_FILE, "w") as f:
                json.dump({"rules": self.rules}, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save automations: {e}")

    def _rebuild_index(self):
        self._source_index.clear()
        for rule in self.rules:
            src = rule.get("source_ieee")
            if src:
                self._source_index.setdefault(src, []).append(rule["id"])

    # =========================================================================
    # TRACING
    # =========================================================================

    def _add_trace(self, trace: Dict[str, Any]):
        trace["timestamp"] = time.time()
        self._trace_log.append(trace)
        if len(self._trace_log) > self._max_trace_entries:
            self._trace_log = self._trace_log[-self._max_trace_entries:]

        level = trace.get("level", "DEBUG")
        msg = trace.get("message", "")
        rule_id = trace.get("rule_id", "?")
        log_msg = f"[AUTO {rule_id}] {msg}"

        if level == "ERROR": logger.error(log_msg)
        elif level == "WARNING": logger.warning(log_msg)
        elif level == "INFO": logger.info(log_msg)
        else: logger.debug(log_msg)

        if self._event_emitter:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._event_emitter("automation_trace", trace))
            except RuntimeError:
                pass

    def get_trace_log(self) -> List[Dict[str, Any]]:
        return list(self._trace_log)

    # =========================================================================
    # VALIDATION
    # =========================================================================

    def _validate_conditions(self, conditions: List[Dict]) -> Optional[str]:
        if not isinstance(conditions, list) or len(conditions) == 0:
            return "conditions must be a non-empty list"
        if len(conditions) > MAX_CONDITIONS_PER_RULE:
            return f"Maximum {MAX_CONDITIONS_PER_RULE} conditions"
        for i, c in enumerate(conditions):
            for f in ("attribute", "operator", "value"):
                if f not in c:
                    return f"Condition {i+1} missing '{f}'"
            if c["operator"] not in OPERATORS:
                return f"Condition {i+1} invalid operator"
            s = c.get("sustain")
            if s is not None:
                try:
                    s = int(s)
                except (ValueError, TypeError):
                    s = 0
                if s > 0:
                    c["sustain"] = s
                else:
                    c.pop("sustain", None)
            else:
                c.pop("sustain", None)
        return None

    def _validate_prerequisites(self, prereqs: List[Dict]) -> Optional[str]:
        if len(prereqs) > MAX_PREREQUISITES_PER_RULE:
            return f"Maximum {MAX_PREREQUISITES_PER_RULE} prerequisites"
        for i, p in enumerate(prereqs):
            for f in ("ieee", "attribute", "operator", "value"):
                if f not in p:
                    return f"Prerequisite {i+1} missing '{f}'"
            if p["operator"] not in OPERATORS:
                return f"Prerequisite {i+1} invalid operator"
        return None

    def _validate_sequence(self, steps: List[Dict], label: str) -> Optional[str]:
        if len(steps) > MAX_STEPS_PER_SEQUENCE:
            return f"{label}: maximum {MAX_STEPS_PER_SEQUENCE} steps"
        for i, step in enumerate(steps):
            st = step.get("type")
            if st not in VALID_STEP_TYPES:
                return f"{label} step {i+1}: invalid type '{st}'"
            if st == "command":
                if not step.get("target_ieee"):
                    return f"{label} step {i+1}: command needs target_ieee"
                if step.get("command") not in VALID_COMMANDS:
                    return f"{label} step {i+1}: invalid command '{step.get('command')}'"
            elif st == "delay":
                secs = step.get("seconds", 0)
                if not isinstance(secs, (int, float)) or secs < 0:
                    return f"{label} step {i+1}: delay needs positive seconds"
            elif st == "wait_for":
                for f in ("ieee", "attribute", "operator", "value"):
                    if f not in step:
                        return f"{label} step {i+1}: wait_for needs '{f}'"
                if step["operator"] not in OPERATORS:
                    return f"{label} step {i+1}: invalid operator"
            elif st == "condition":
                for f in ("ieee", "attribute", "operator", "value"):
                    if f not in step:
                        return f"{label} step {i+1}: condition needs '{f}'"
                if step["operator"] not in OPERATORS:
                    return f"{label} step {i+1}: invalid operator"
        return None

    # =========================================================================
    # RULE CRUD
    # =========================================================================

    def add_rule(self, data: Dict[str, Any]) -> Dict[str, Any]:
        # Conditions
        conditions = data.get("conditions")
        if conditions:
            err = self._validate_conditions(conditions)
            if err:
                return {"success": False, "error": err}
        elif all(k in data for k in ("attribute", "operator", "value")):
            conditions = [{"attribute": data["attribute"],
                           "operator": data["operator"],
                           "value": data["value"]}]
        else:
            return {"success": False, "error": "Provide conditions list"}

        # Prerequisites
        prereqs = data.get("prerequisites", [])
        if prereqs:
            err = self._validate_prerequisites(prereqs)
            if err:
                return {"success": False, "error": err}

        # Sequences
        then_seq = data.get("then_sequence", [])
        else_seq = data.get("else_sequence", [])

        if not then_seq and not else_seq:
            return {"success": False, "error": "At least one action step required"}

        err = self._validate_sequence(then_seq, "THEN")
        if err:
            return {"success": False, "error": err}
        err = self._validate_sequence(else_seq, "ELSE")
        if err:
            return {"success": False, "error": err}

        source_ieee = data.get("source_ieee")
        if not source_ieee:
            return {"success": False, "error": "source_ieee required"}

        if len(self._source_index.get(source_ieee, [])) >= MAX_RULES_PER_DEVICE:
            return {"success": False, "error": f"Maximum {MAX_RULES_PER_DEVICE} rules per device"}

        devices = self._get_devices()
        if source_ieee not in devices:
            return {"success": False, "error": f"Source device not found: {source_ieee}"}

        rule = {
            "id": f"auto_{uuid.uuid4().hex[:8]}",
            "name": data.get("name", ""),
            "enabled": data.get("enabled", True),
            "source_ieee": source_ieee,
            "conditions": conditions,
            "prerequisites": prereqs,
            "then_sequence": then_seq,
            "else_sequence": else_seq,
            "cooldown": data.get("cooldown", DEFAULT_COOLDOWN),
            "created": time.time(),
        }

        self.rules.append(rule)
        self._rebuild_index()
        self._save_rules()
        logger.info(f"Rule added: {rule['id']} '{rule['name']}'")
        return {"success": True, "rule": rule}

    def update_rule(self, rule_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        rule = self._find_rule(rule_id)
        if not rule:
            return {"success": False, "error": f"Rule not found: {rule_id}"}

        if "name" in updates:
            rule["name"] = str(updates["name"])[:100]
        if "conditions" in updates:
            err = self._validate_conditions(updates["conditions"])
            if err:
                return {"success": False, "error": err}
            rule["conditions"] = updates["conditions"]
        if "prerequisites" in updates:
            p = updates["prerequisites"] or []
            if p:
                err = self._validate_prerequisites(p)
                if err:
                    return {"success": False, "error": err}
            rule["prerequisites"] = p
        if "then_sequence" in updates:
            err = self._validate_sequence(updates["then_sequence"], "THEN")
            if err:
                return {"success": False, "error": err}
            rule["then_sequence"] = updates["then_sequence"]
        if "else_sequence" in updates:
            err = self._validate_sequence(updates["else_sequence"], "ELSE")
            if err:
                return {"success": False, "error": err}
            rule["else_sequence"] = updates["else_sequence"]
        if "enabled" in updates:
            rule["enabled"] = bool(updates["enabled"])
            if not rule["enabled"]:
                self._cancel_sequence(rule_id)
                self._rule_states.pop(rule_id, None)
        if "cooldown" in updates:
            rule["cooldown"] = max(0, int(updates["cooldown"]))

        rule["updated"] = time.time()
        self._rebuild_index()
        self._save_rules()
        return {"success": True, "rule": rule}

    def delete_rule(self, rule_id: str) -> Dict[str, Any]:
        rule = self._find_rule(rule_id)
        if not rule:
            return {"success": False, "error": f"Rule not found: {rule_id}"}
        self._cancel_sequence(rule_id)
        self.rules.remove(rule)
        self._cooldowns.pop(rule_id, None)
        self._rule_states.pop(rule_id, None)
        for k in [k for k in self._sustain_tracker if k.startswith(rule_id)]:
            del self._sustain_tracker[k]
        self._rebuild_index()
        self._save_rules()
        return {"success": True}

    def get_rules(self, source_ieee: Optional[str] = None) -> List[Dict[str, Any]]:
        names = self._get_names()
        rules = self.rules if not source_ieee else [
            r for r in self.rules if r["source_ieee"] == source_ieee
        ]
        enriched = []
        for rule in rules:
            r = rule.copy()
            r["source_name"] = names.get(rule["source_ieee"], rule["source_ieee"])
            r["_state"] = self._rule_states.get(rule["id"], "unknown")
            r["_running"] = (rule["id"] in self._running_sequences and
                             not self._running_sequences[rule["id"]].done())
            # Enrich prereq names
            if r.get("prerequisites"):
                for p in r["prerequisites"]:
                    p["device_name"] = names.get(p.get("ieee", ""), p.get("ieee", ""))
            # Enrich step target names
            for seq_key in ("then_sequence", "else_sequence"):
                for step in r.get(seq_key, []):
                    if step.get("target_ieee"):
                        step["target_name"] = names.get(step["target_ieee"], step["target_ieee"])
                    if step.get("ieee"):
                        step["device_name"] = names.get(step["ieee"], step["ieee"])
            enriched.append(r)
        return enriched

    def get_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        return self._find_rule(rule_id)

    def _find_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        for rule in self.rules:
            if rule["id"] == rule_id:
                return rule
        return None

    # =========================================================================
    # EVALUATION ENGINE (state machine)
    # =========================================================================

    async def evaluate(self, source_ieee: str, changed_data: Dict[str, Any]):
        rule_ids = self._source_index.get(source_ieee)
        if not rule_ids:
            return

        self._stats["evaluations"] += 1
        now = time.time()
        devices = self._get_devices()
        names = self._get_names()
        source_name = names.get(source_ieee, source_ieee)

        source_device = devices.get(source_ieee)
        if not source_device:
            return

        full_state = source_device.state or {}

        self._add_trace({
            "rule_id": "-", "level": "DEBUG", "phase": "entry",
            "result": "EVALUATING",
            "message": f"State change on {source_name}: {list(changed_data.keys())} — {len(rule_ids)} rule(s)",
            "source_ieee": source_ieee,
        })

        for rule_id in rule_ids:
            rule = self._find_rule(rule_id)
            if not rule or not rule.get("enabled", True):
                continue

            conditions = rule.get("conditions", [])
            if not conditions:
                continue

            rule_name = rule.get("name") or rule_id

            # --- Relevance check ---
            watched = {c["attribute"] for c in conditions}
            changed = set(changed_data.keys())
            if not watched.intersection(changed):
                continue

            # --- EVALUATE CONDITIONS ---
            all_matched = True
            condition_results = []
            has_sustain_wait = False

            for i, cond in enumerate(conditions):
                result = self._eval_single_condition(
                    cond, i, rule_id, changed_data, full_state, now
                )
                condition_results.append(result)
                if result["result"] == "SUSTAIN_WAIT":
                    has_sustain_wait = True
                    all_matched = False
                    break
                elif result["result"] != "PASS":
                    all_matched = False
                    break

            # --- EVALUATE PREREQUISITES (only if conditions matched) ---
            prereq_results = []
            prereqs_met = True
            if all_matched:
                prereqs = rule.get("prerequisites", [])
                for j, prereq in enumerate(prereqs):
                    result = self._eval_prerequisite(prereq, j, devices, names)
                    prereq_results.append(result)
                    if result["result"] != "PASS":
                        prereqs_met = False
                        break

            # --- DETERMINE FINAL STATE ---
            conditions_met = all_matched and prereqs_met
            prev_state = self._rule_states.get(rule_id)

            if has_sustain_wait:
                self._add_trace({
                    "rule_id": rule_id, "level": "INFO", "phase": "evaluate",
                    "result": "SUSTAIN_WAIT",
                    "message": f"Sustain pending: {rule_name}",
                    "conditions": condition_results,
                })
                continue

            if not all_matched:
                new_state = "unmatched"
                self._add_trace({
                    "rule_id": rule_id, "level": "DEBUG", "phase": "evaluate",
                    "result": "NO_MATCH",
                    "message": f"Conditions not met: {rule_name}",
                    "conditions": condition_results,
                })
            elif not prereqs_met:
                new_state = "unmatched"
                self._add_trace({
                    "rule_id": rule_id, "level": "INFO", "phase": "prerequisite",
                    "result": "PREREQ_FAIL",
                    "message": f"Prerequisites not met: {rule_name}",
                    "conditions": condition_results,
                    "prerequisites": prereq_results,
                })
            else:
                new_state = "matched"
                self._stats["matches"] += 1

            # --- TRANSITION DETECTION ---
            self._rule_states[rule_id] = new_state

            if prev_state == new_state:
                # No transition — do nothing
                if new_state == "matched":
                    self._add_trace({
                        "rule_id": rule_id, "level": "DEBUG", "phase": "transition",
                        "result": "STILL_MATCHED",
                        "message": f"Still matched (no transition): {rule_name}",
                    })
                continue

            if prev_state is None:
                # First evaluation — treat as transition
                if new_state == "unmatched":
                    self._add_trace({
                        "rule_id": rule_id, "level": "DEBUG", "phase": "transition",
                        "result": "INIT_UNMATCHED",
                        "message": f"Initial state: unmatched — {rule_name}",
                    })
                    continue
                # First eval, matched → fire THEN

            # --- COOLDOWN CHECK ---
            cooldown = rule.get("cooldown", DEFAULT_COOLDOWN)
            last_fired = self._cooldowns.get(rule_id, 0)
            elapsed = now - last_fired
            if elapsed < cooldown:
                self._add_trace({
                    "rule_id": rule_id, "level": "INFO", "phase": "cooldown",
                    "result": "BLOCKED",
                    "message": f"Cooldown: {elapsed:.1f}s < {cooldown}s — {rule_name}",
                })
                continue

            self._cooldowns[rule_id] = now
            self._stats["transitions"] += 1

            # Clear sustain trackers on transition
            for ci in range(len(conditions)):
                self._sustain_tracker.pop(f"{rule_id}_{ci}", None)

            # --- FIRE SEQUENCE ---
            if new_state == "matched":
                sequence = rule.get("then_sequence", [])
                path = "THEN"
            else:
                sequence = rule.get("else_sequence", [])
                path = "ELSE"

            if not sequence:
                self._add_trace({
                    "rule_id": rule_id, "level": "INFO", "phase": "transition",
                    "result": "NO_SEQUENCE",
                    "message": f"Transition → {new_state} but no {path} sequence: {rule_name}",
                })
                continue

            self._add_trace({
                "rule_id": rule_id, "level": "INFO", "phase": "transition",
                "result": f"{path}_FIRING",
                "message": f"⚡ Transition {prev_state or 'init'}→{new_state}: "
                           f"running {path} ({len(sequence)} steps) — {rule_name}",
                "conditions": condition_results,
                "prerequisites": prereq_results,
            })

            # Cancel any running sequence for this rule
            self._cancel_sequence(rule_id)

            # Start the sequence
            task = asyncio.create_task(
                self._run_sequence(rule_id, rule_name, sequence, path)
            )
            self._running_sequences[rule_id] = task

    # =========================================================================
    # CONDITION / PREREQUISITE EVALUATION
    # =========================================================================

    def _eval_single_condition(self, cond, index, rule_id, changed_data, full_state, now):
        attr = cond["attribute"]
        operator = cond["operator"]
        threshold = cond["value"]
        sustain_secs = cond.get("sustain", 0) or 0

        if attr in changed_data:
            current_value = changed_data[attr]
            src = "changed_data"
        elif attr in full_state:
            current_value = full_state[attr]
            src = "full_state"
        else:
            return {"index": index + 1, "attribute": attr, "operator": operator,
                    "threshold": threshold, "result": "FAIL",
                    "reason": f"'{attr}' not in state"}

        norm_a = self._normalise_value(current_value)
        norm_t = self._normalise_value(threshold)

        try:
            matched = self._evaluate_condition(current_value, operator, threshold)
        except Exception as e:
            return {"index": index + 1, "attribute": attr, "result": "ERROR",
                    "reason": str(e), "actual": repr(current_value)}

        sustain_key = f"{rule_id}_{index}"

        if matched and sustain_secs > 0:
            if sustain_key not in self._sustain_tracker:
                self._sustain_tracker[sustain_key] = now
            elapsed = now - self._sustain_tracker[sustain_key]
            if elapsed < sustain_secs:
                return {"index": index + 1, "attribute": attr, "operator": operator,
                        "threshold_raw": repr(threshold), "actual_raw": repr(current_value),
                        "actual_type": type(norm_a).__name__, "value_source": src,
                        "result": "SUSTAIN_WAIT",
                        "sustain_required": sustain_secs,
                        "sustain_elapsed": round(elapsed, 1),
                        "reason": f"Sustained {elapsed:.1f}s / {sustain_secs}s"}

        if matched:
            self._sustain_tracker.pop(sustain_key, None)
        else:
            self._sustain_tracker.pop(sustain_key, None)

        return {"index": index + 1, "attribute": attr, "operator": operator,
                "threshold_raw": repr(threshold), "threshold_type": type(norm_t).__name__,
                "actual_raw": repr(current_value), "actual_type": type(norm_a).__name__,
                "value_source": src,
                "result": "PASS" if matched else "FAIL"}

    def _eval_prerequisite(self, prereq, index, devices, names):
        ieee = prereq["ieee"]
        attr = prereq["attribute"]
        op = prereq["operator"]
        val = prereq["value"]
        device = devices.get(ieee)
        name = names.get(ieee, ieee)

        if not device:
            return {"index": index + 1, "ieee": ieee, "device_name": name,
                    "attribute": attr, "result": "FAIL", "reason": "Device not found"}

        state = device.state or {}
        actual = state.get(attr)
        if actual is None:
            return {"index": index + 1, "ieee": ieee, "device_name": name,
                    "attribute": attr, "result": "FAIL",
                    "reason": f"'{attr}' not in state",
                    "available_keys": list(state.keys())}

        try:
            matched = self._evaluate_condition(actual, op, val)
        except Exception as e:
            return {"index": index + 1, "ieee": ieee, "device_name": name,
                    "attribute": attr, "result": "ERROR", "reason": str(e)}

        return {"index": index + 1, "ieee": ieee, "device_name": name,
                "attribute": attr, "operator": op,
                "threshold_raw": repr(val),
                "threshold_normalised": repr(self._normalise_value(val)),
                "actual_raw": repr(actual),
                "actual_normalised": repr(self._normalise_value(actual)),
                "actual_type": type(actual).__name__,
                "result": "PASS" if matched else "FAIL"}

    # =========================================================================
    # SEQUENCE EXECUTOR
    # =========================================================================

    def _cancel_sequence(self, rule_id: str):
        task = self._running_sequences.pop(rule_id, None)
        if task and not task.done():
            task.cancel()
            self._add_trace({
                "rule_id": rule_id, "level": "INFO", "phase": "sequence",
                "result": "CANCELLED",
                "message": f"Previous sequence cancelled",
            })

    async def _run_sequence(self, rule_id: str, rule_name: str,
                            steps: List[Dict], path: str):
        """Execute a sequence of action steps in order."""
        names = self._get_names()
        try:
            for i, step in enumerate(steps):
                step_type = step["type"]
                step_num = i + 1
                total = len(steps)

                if step_type == "command":
                    await self._step_command(rule_id, rule_name, step, step_num, total, path)

                elif step_type == "delay":
                    secs = step.get("seconds", 0) or 0
                    if secs > 0:
                        self._add_trace({
                            "rule_id": rule_id, "level": "INFO",
                            "phase": "step", "result": "DELAY",
                            "message": f"[{path} {step_num}/{total}] ⏱ Waiting {secs}s",
                        })
                        await asyncio.sleep(secs)

                elif step_type == "wait_for":
                    met = await self._step_wait_for(rule_id, rule_name, step, step_num, total, path)
                    if not met:
                        self._add_trace({
                            "rule_id": rule_id, "level": "WARNING",
                            "phase": "step", "result": "WAIT_TIMEOUT",
                            "message": f"[{path} {step_num}/{total}] ⏰ Timed out waiting — stopping sequence",
                        })
                        break

                elif step_type == "condition":
                    met = self._step_condition(rule_id, rule_name, step, step_num, total, path)
                    if not met:
                        self._add_trace({
                            "rule_id": rule_id, "level": "INFO",
                            "phase": "step", "result": "CONDITION_FAIL",
                            "message": f"[{path} {step_num}/{total}] Gate condition not met — stopping sequence",
                        })
                        break

            self._add_trace({
                "rule_id": rule_id, "level": "INFO",
                "phase": "sequence", "result": "COMPLETE",
                "message": f"✅ {path} sequence complete — {rule_name}",
            })

        except asyncio.CancelledError:
            self._add_trace({
                "rule_id": rule_id, "level": "INFO",
                "phase": "sequence", "result": "CANCELLED",
                "message": f"{path} sequence cancelled — {rule_name}",
            })
        except Exception as e:
            self._stats["errors"] += 1
            self._add_trace({
                "rule_id": rule_id, "level": "ERROR",
                "phase": "sequence", "result": "EXCEPTION",
                "message": f"💥 {path} sequence failed: {e}",
                "error": str(e), "traceback": traceback.format_exc(),
            })
        finally:
            self._running_sequences.pop(rule_id, None)

    async def _step_command(self, rule_id, rule_name, step, num, total, path):
        """Execute a command step."""
        target_ieee = step["target_ieee"]
        command = step["command"]
        value = step.get("value")
        endpoint_id = step.get("endpoint_id")

        devices = self._get_devices()
        names = self._get_names()
        target_name = names.get(target_ieee, target_ieee)

        target_device = devices.get(target_ieee)
        if not target_device:
            self._stats["execution_failures"] += 1
            self._add_trace({
                "rule_id": rule_id, "level": "ERROR",
                "phase": "step", "result": "TARGET_MISSING",
                "message": f"[{path} {num}/{total}] Target {target_name} ({target_ieee}) not found",
            })
            return

        if not hasattr(target_device, 'send_command'):
            self._stats["execution_failures"] += 1
            self._add_trace({
                "rule_id": rule_id, "level": "ERROR",
                "phase": "step", "result": "NO_SEND_COMMAND",
                "message": f"[{path} {num}/{total}] {target_name} has no send_command()",
            })
            return

        self._add_trace({
            "rule_id": rule_id, "level": "INFO",
            "phase": "step", "result": "SENDING",
            "message": f"[{path} {num}/{total}] → {target_name} {command}={value} EP={endpoint_id}",
        })

        try:
            result = await target_device.send_command(command, value, endpoint_id=endpoint_id)

            if isinstance(result, dict):
                success = result.get("success", True)
                error = result.get("error")
            elif result is None:
                success = True
                error = None
            else:
                success = bool(result)
                error = None if success else repr(result)

            if success:
                self._stats["executions"] += 1
                self._stats["execution_successes"] += 1
                self._add_trace({
                    "rule_id": rule_id, "level": "INFO",
                    "phase": "step", "result": "SUCCESS",
                    "message": f"[{path} {num}/{total}] ✅ {target_name} {command}={value} EP={endpoint_id}",
                    "command_result": repr(result),
                })
            else:
                self._stats["executions"] += 1
                self._stats["execution_failures"] += 1
                self._add_trace({
                    "rule_id": rule_id, "level": "ERROR",
                    "phase": "step", "result": "COMMAND_FAILED",
                    "message": f"[{path} {num}/{total}] ❌ {target_name} {command} — {error}",
                })

            if self._event_emitter:
                await self._event_emitter("automation_triggered", {
                    "rule_id": rule_id, "target_ieee": target_ieee,
                    "command": command, "value": value, "success": success,
                    "timestamp": time.time(),
                })

        except Exception as e:
            self._stats["errors"] += 1
            self._stats["execution_failures"] += 1
            self._add_trace({
                "rule_id": rule_id, "level": "ERROR",
                "phase": "step", "result": "EXCEPTION",
                "message": f"[{path} {num}/{total}] 💥 {target_name} {command}: {e}",
                "traceback": traceback.format_exc(),
            })

    async def _step_wait_for(self, rule_id, rule_name, step, num, total, path) -> bool:
        """Wait for a device state to match. Returns True if met, False if timed out."""
        ieee = step["ieee"]
        attr = step["attribute"]
        operator = step["operator"]
        threshold = step["value"]
        timeout = step.get("timeout", 300) or 300

        names = self._get_names()
        dev_name = names.get(ieee, ieee)

        self._add_trace({
            "rule_id": rule_id, "level": "INFO",
            "phase": "step", "result": "WAITING",
            "message": f"[{path} {num}/{total}] ⏳ Waiting for {dev_name} "
                       f"{attr} {operator} {threshold} (timeout {timeout}s)",
        })

        start = time.time()
        while time.time() - start < timeout:
            devices = self._get_devices()
            device = devices.get(ieee)
            if device:
                state = device.state or {}
                value = state.get(attr)
                if value is not None:
                    try:
                        if self._evaluate_condition(value, operator, threshold):
                            elapsed = time.time() - start
                            self._add_trace({
                                "rule_id": rule_id, "level": "INFO",
                                "phase": "step", "result": "WAIT_MET",
                                "message": f"[{path} {num}/{total}] ✅ {dev_name} {attr}={repr(value)} "
                                           f"met after {elapsed:.1f}s",
                            })
                            return True
                    except Exception:
                        pass
            await asyncio.sleep(WAIT_FOR_POLL_INTERVAL)

        return False

    def _step_condition(self, rule_id, rule_name, step, num, total, path) -> bool:
        """Inline gate condition. Returns True if met."""
        ieee = step["ieee"]
        attr = step["attribute"]
        operator = step["operator"]
        threshold = step["value"]

        devices = self._get_devices()
        names = self._get_names()
        dev_name = names.get(ieee, ieee)

        device = devices.get(ieee)
        if not device:
            return False

        state = device.state or {}
        value = state.get(attr)
        if value is None:
            return False

        try:
            result = self._evaluate_condition(value, operator, threshold)
        except Exception:
            return False

        self._add_trace({
            "rule_id": rule_id, "level": "DEBUG",
            "phase": "step",
            "result": "CONDITION_PASS" if result else "CONDITION_FAIL",
            "message": f"[{path} {num}/{total}] Gate: {dev_name} {attr} {operator} "
                       f"{threshold} → actual: {repr(value)} → {'PASS' if result else 'FAIL'}",
        })
        return result

    # =========================================================================
    # CONDITION HELPERS
    # =========================================================================

    @staticmethod
    def _get_conditions(rule):
        if "conditions" in rule:
            return rule["conditions"]
        if "threshold" in rule:
            return [rule["threshold"]]
        return []

    def _evaluate_condition(self, actual_value, operator, threshold_value) -> bool:
        op_func = OPERATORS.get(operator)
        if not op_func:
            return False
        actual = self._normalise_value(actual_value)
        threshold = self._normalise_value(threshold_value)

        # String eq/neq: case-insensitive
        if isinstance(actual, str) and isinstance(threshold, str) and operator in ("eq", "neq"):
            if operator == "eq":
                return actual.lower() == threshold.lower()
            return actual.lower() != threshold.lower()

        # Bool vs string cross-type
        if isinstance(actual, bool) and isinstance(threshold, str):
            threshold = threshold.lower() in ("on", "true")
        elif isinstance(threshold, bool) and isinstance(actual, str):
            actual = actual.lower() in ("on", "true")

        try:
            return op_func(actual, threshold)
        except (TypeError, ValueError):
            return op_func(str(actual).lower(), str(threshold).lower())

    @staticmethod
    def _normalise_value(value):
        if isinstance(value, str):
            stripped = value.strip().strip("'\"")
            lower = stripped.lower()
            if lower == "true":
                return True
            if lower == "false":
                return False
            try:
                if "." in stripped:
                    return float(stripped)
                return int(stripped)
            except ValueError:
                return stripped
        return value

    # =========================================================================
    # HELPERS (frontend)
    # =========================================================================

    def get_source_attributes(self, ieee: str) -> List[Dict[str, Any]]:
        devices = self._get_devices()
        if ieee not in devices:
            return []
        state = devices[ieee].state
        skip = {"last_seen", "available", "manufacturer", "model",
                "power_source", "lqi", "linkquality"}
        attrs = []
        for key, value in state.items():
            if key in skip or key.endswith("_raw") or key.startswith("attr_"):
                continue
            a = {"attribute": key, "current_value": value,
                 "type": self._classify_value_type(value)}
            if isinstance(value, bool):
                a["operators"] = ["eq", "neq"]
                a["value_options"] = ["true", "false"]
            elif isinstance(value, str) and value.upper() in ("ON", "OFF"):
                a["operators"] = ["eq", "neq"]
                a["value_options"] = ["ON", "OFF"]
            elif isinstance(value, (int, float)):
                a["operators"] = ["eq", "neq", "gt", "lt", "gte", "lte"]
            else:
                a["operators"] = ["eq", "neq"]
            attrs.append(a)
        return sorted(attrs, key=lambda x: x["attribute"])

    def get_device_state(self, ieee: str) -> Dict[str, Any]:
        devices = self._get_devices()
        names = self._get_names()
        if ieee not in devices:
            return {}
        state = devices[ieee].state or {}
        attrs = []
        for k, v in state.items():
            if k.endswith("_raw") or k.startswith("attr_"):
                continue
            a = {"attribute": k, "current_value": v,
                 "type": self._classify_value_type(v),
                 "operators": ["eq", "neq"] if isinstance(v, (bool, str))
                 else ["eq", "neq", "gt", "lt", "gte", "lte"]}
            if isinstance(v, bool):
                a["value_options"] = ["true", "false"]
            elif isinstance(v, str) and v.upper() in ("ON", "OFF"):
                a["value_options"] = ["ON", "OFF"]
            attrs.append(a)
        return {"ieee": ieee, "friendly_name": names.get(ieee, ieee),
                "state": state, "attributes": attrs}

    def get_target_actions(self, ieee: str) -> List[Dict[str, Any]]:
        devices = self._get_devices()
        if ieee not in devices:
            return []
        d = devices[ieee]
        return d.get_control_commands() if hasattr(d, "get_control_commands") else []

    def get_actuator_devices(self) -> List[Dict[str, Any]]:
        devices = self._get_devices()
        names = self._get_names()
        actuators = []
        for ieee, device in devices.items():
            caps = getattr(device, "capabilities", None)
            if not caps:
                continue
            hc = getattr(caps, "has_capability", lambda x: False)
            if not any(hc(c) for c in ["on_off", "light", "switch", "cover",
                                       "window_covering", "thermostat", "fan_control"]):
                continue
            actuators.append({
                "ieee": ieee, "friendly_name": names.get(ieee, ieee),
                "model": getattr(device, "model", "Unknown"),
                "manufacturer": getattr(device, "manufacturer", "Unknown"),
                "commands": device.get_control_commands() if hasattr(device, "get_control_commands") else [],
            })
        return sorted(actuators, key=lambda d: d.get("friendly_name", ""))

    def get_all_devices_summary(self) -> List[Dict[str, Any]]:
        devices = self._get_devices()
        names = self._get_names()
        return sorted([{
            "ieee": ieee, "friendly_name": names.get(ieee, ieee),
            "model": getattr(d, "model", "Unknown"),
            "state_keys": [k for k in (d.state or {}).keys()
                           if not k.endswith("_raw") and not k.startswith("attr_")],
        } for ieee, d in devices.items()], key=lambda x: x.get("friendly_name", ""))

    @staticmethod
    def _classify_value_type(value):
        if isinstance(value, bool): return "boolean"
        if isinstance(value, int): return "integer"
        if isinstance(value, float): return "float"
        return "string"

    def get_stats(self) -> Dict[str, Any]:
        return {
            **self._stats,
            "total_rules": len(self.rules),
            "enabled_rules": sum(1 for r in self.rules if r.get("enabled", True)),
            "trace_entries": len(self._trace_log),
            "active_sustains": len(self._sustain_tracker),
            "running_sequences": sum(1 for t in self._running_sequences.values() if not t.done()),
        }