"""
Automation Engine - Threshold-based Device Triggers
====================================================
Evaluates device state changes against user-defined thresholds and fires
direct ZigBee commands to target devices, bypassing MQTT for low latency.

Features:
  - Named rules
  - Compound conditions (multiple AND thresholds per rule)
  - Sustain timers (optional — condition must hold for N seconds)
  - Prerequisites (check other device states before firing)
  - Configurable delay before command execution
  - Full evaluation tracing for debugging

Persistence: ./data/automations.json
Hook point:  core.py -> _debounced_device_update (after changed_data computed)
Execution:   device.send_command() (direct zigpy cluster commands)
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
DATA_FILE = "./data/automations.json"
DEFAULT_COOLDOWN = 5

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

        self._trace_log: List[Dict[str, Any]] = []
        self._max_trace_entries = 100

        self._stats = {
            "evaluations": 0, "matches": 0, "executions": 0,
            "execution_successes": 0, "execution_failures": 0, "errors": 0,
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
            migrated = False
            for rule in self.rules:
                if "threshold" in rule and "conditions" not in rule:
                    rule["conditions"] = [rule.pop("threshold")]
                    migrated = True
                # Ensure name field exists
                if "name" not in rule:
                    rule["name"] = ""
            if migrated:
                self._save_rules()
            self._rebuild_index()
            logger.info(f"Loaded {len(self.rules)} automation rule(s)")
        except Exception as e:
            logger.error(f"Failed to load automations: {e}")
            self.rules = []
            self._rebuild_index()

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
        log_msg = f"[AUTOMATION {rule_id}] {msg}"

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
    # VALIDATION HELPERS
    # =========================================================================

    def _validate_conditions(self, conditions: List[Dict]) -> Optional[str]:
        """Validate conditions list. Returns error string or None."""
        if not isinstance(conditions, list) or len(conditions) == 0:
            return "conditions must be a non-empty list"
        if len(conditions) > MAX_CONDITIONS_PER_RULE:
            return f"Maximum {MAX_CONDITIONS_PER_RULE} conditions per rule"
        for i, cond in enumerate(conditions):
            for field in ("attribute", "operator", "value"):
                if field not in cond:
                    return f"Condition {i+1} missing '{field}'"
            if cond["operator"] not in OPERATORS:
                return f"Condition {i+1} invalid operator: {cond['operator']}"
            # Clean sustain: remove if absent/zero/None
            sustain = cond.get("sustain")
            if sustain is not None:
                try:
                    sustain = int(sustain)
                except (ValueError, TypeError):
                    sustain = 0
                if sustain > 0:
                    cond["sustain"] = sustain
                else:
                    cond.pop("sustain", None)
            else:
                cond.pop("sustain", None)
        return None

    def _validate_prerequisites(self, prerequisites: List[Dict]) -> Optional[str]:
        """Validate prerequisites list. Returns error string or None."""
        if len(prerequisites) > MAX_PREREQUISITES_PER_RULE:
            return f"Maximum {MAX_PREREQUISITES_PER_RULE} prerequisites"
        for i, prereq in enumerate(prerequisites):
            for field in ("ieee", "attribute", "operator", "value"):
                if field not in prereq:
                    return f"Prerequisite {i+1} missing '{field}'"
            if prereq["operator"] not in OPERATORS:
                return f"Prerequisite {i+1} invalid operator"
        return None

    # =========================================================================
    # RULE CRUD
    # =========================================================================

    def add_rule(self, rule_data: Dict[str, Any]) -> Dict[str, Any]:
        # --- Build conditions ---
        conditions = rule_data.get("conditions")
        if conditions:
            err = self._validate_conditions(conditions)
            if err:
                return {"success": False, "error": err}
        elif all(k in rule_data for k in ("attribute", "operator", "value")):
            conditions = [{"attribute": rule_data["attribute"],
                           "operator": rule_data["operator"],
                           "value": rule_data["value"]}]
        else:
            return {"success": False, "error": "Provide 'conditions' list or attribute/operator/value"}

        # --- Prerequisites ---
        prerequisites = rule_data.get("prerequisites", [])
        if prerequisites:
            err = self._validate_prerequisites(prerequisites)
            if err:
                return {"success": False, "error": err}

        # --- Top-level validation ---
        for field in ("source_ieee", "target_ieee", "command"):
            if field not in rule_data:
                return {"success": False, "error": f"Missing required field: {field}"}
        if rule_data["command"] not in VALID_COMMANDS:
            return {"success": False, "error": f"Invalid command: {rule_data['command']}"}

        source_ieee = rule_data["source_ieee"]
        if len(self._source_index.get(source_ieee, [])) >= MAX_RULES_PER_DEVICE:
            return {"success": False, "error": f"Maximum {MAX_RULES_PER_DEVICE} rules per device"}

        devices = self._get_devices()
        if source_ieee not in devices:
            return {"success": False, "error": f"Source device not found: {source_ieee}"}
        if rule_data["target_ieee"] not in devices:
            return {"success": False, "error": f"Target device not found: {rule_data['target_ieee']}"}

        # Parse delay safely
        delay = 0
        if rule_data.get("delay"):
            try:
                delay = max(0, int(rule_data["delay"]))
            except (ValueError, TypeError):
                delay = 0

        rule = {
            "id": f"auto_{uuid.uuid4().hex[:8]}",
            "name": rule_data.get("name", ""),
            "enabled": rule_data.get("enabled", True),
            "source_ieee": source_ieee,
            "conditions": conditions,
            "prerequisites": prerequisites if prerequisites else [],
            "target_ieee": rule_data["target_ieee"],
            "action": {
                "command": rule_data["command"],
                "value": rule_data.get("command_value"),
                "endpoint_id": rule_data.get("endpoint_id"),
                "delay": delay,
            },
            "cooldown": rule_data.get("cooldown", DEFAULT_COOLDOWN),
            "created": time.time(),
        }

        self.rules.append(rule)
        self._rebuild_index()
        self._save_rules()

        cond_summary = " AND ".join(
            f"{c['attribute']} {c['operator']} {c['value']}"
            + (f" (sustain {c['sustain']}s)" if c.get('sustain') else "")
            for c in conditions
        )
        logger.info(f"Rule added: {rule['id']} '{rule['name']}' ({cond_summary})")
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
            prereqs = updates["prerequisites"] or []
            if prereqs:
                err = self._validate_prerequisites(prereqs)
                if err:
                    return {"success": False, "error": err}
            rule["prerequisites"] = prereqs

        if "command" in updates:
            if updates["command"] not in VALID_COMMANDS:
                return {"success": False, "error": f"Invalid command: {updates['command']}"}
            rule["action"]["command"] = updates["command"]
        if "command_value" in updates:
            rule["action"]["value"] = updates["command_value"]
        if "endpoint_id" in updates:
            rule["action"]["endpoint_id"] = updates["endpoint_id"]
        if "delay" in updates:
            try:
                rule["action"]["delay"] = max(0, int(updates["delay"]))
            except (ValueError, TypeError):
                rule["action"]["delay"] = 0
        if "target_ieee" in updates:
            rule["target_ieee"] = updates["target_ieee"]
        if "enabled" in updates:
            rule["enabled"] = bool(updates["enabled"])
        if "cooldown" in updates:
            rule["cooldown"] = max(0, int(updates["cooldown"]))

        rule["updated"] = time.time()
        self._rebuild_index()
        self._save_rules()
        logger.info(f"Rule updated: {rule_id}")
        return {"success": True, "rule": rule}

    def delete_rule(self, rule_id: str) -> Dict[str, Any]:
        rule = self._find_rule(rule_id)
        if not rule:
            return {"success": False, "error": f"Rule not found: {rule_id}"}
        self.rules.remove(rule)
        self._cooldowns.pop(rule_id, None)
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
            r["target_name"] = names.get(rule["target_ieee"], rule["target_ieee"])
            if r.get("prerequisites"):
                for prereq in r["prerequisites"]:
                    prereq["device_name"] = names.get(prereq.get("ieee", ""), prereq.get("ieee", ""))
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
    # EVALUATION ENGINE
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
            self._add_trace({
                "rule_id": "-", "level": "ERROR", "phase": "lookup",
                "result": "SOURCE_MISSING",
                "message": f"Source {source_ieee} ({source_name}) not in registry",
                "source_ieee": source_ieee,
            })
            return

        full_state = source_device.state or {}

        self._add_trace({
            "rule_id": "-", "level": "DEBUG", "phase": "entry",
            "result": "EVALUATING",
            "message": f"State change on {source_name}: {list(changed_data.keys())} — {len(rule_ids)} rule(s)",
            "source_ieee": source_ieee,
            "changed_keys": list(changed_data.keys()),
        })

        for rule_id in rule_ids:
            rule = self._find_rule(rule_id)
            if not rule:
                continue
            if not rule.get("enabled", True):
                continue

            conditions = self._get_conditions(rule)
            if not conditions:
                continue

            rule_name = rule.get("name") or rule_id

            # --- Relevance ---
            watched = {c["attribute"] for c in conditions}
            changed = set(changed_data.keys())
            triggered = watched.intersection(changed)
            if not triggered:
                continue

            # ---- CONDITIONS (AND + optional sustain) ----
            all_matched = True
            condition_results = []

            for i, cond in enumerate(conditions):
                attr = cond["attribute"]
                operator = cond["operator"]
                threshold = cond["value"]
                sustain_secs = cond.get("sustain", 0) or 0  # Truly zero if absent/None/0

                if attr in changed_data:
                    current_value = changed_data[attr]
                    value_source = "changed_data"
                elif attr in full_state:
                    current_value = full_state[attr]
                    value_source = "full_state"
                else:
                    condition_results.append({
                        "index": i + 1, "attribute": attr, "operator": operator,
                        "threshold": threshold, "result": "FAIL",
                        "reason": f"'{attr}' not in state",
                    })
                    all_matched = False
                    break

                norm_actual = self._normalise_value(current_value)
                norm_threshold = self._normalise_value(threshold)

                try:
                    matched = self._evaluate_condition(current_value, operator, threshold)
                except Exception as e:
                    condition_results.append({
                        "index": i + 1, "attribute": attr, "result": "ERROR",
                        "reason": str(e),
                        "actual": repr(current_value), "threshold": repr(threshold),
                    })
                    all_matched = False
                    break

                sustain_key = f"{rule_id}_{i}"

                # Only apply sustain if sustain_secs is > 0
                if matched and sustain_secs > 0:
                    if sustain_key not in self._sustain_tracker:
                        self._sustain_tracker[sustain_key] = now
                    elapsed = now - self._sustain_tracker[sustain_key]
                    if elapsed < sustain_secs:
                        condition_results.append({
                            "index": i + 1, "attribute": attr, "operator": operator,
                            "threshold_raw": repr(threshold),
                            "actual_raw": repr(current_value),
                            "actual_type": type(norm_actual).__name__,
                            "value_source": value_source,
                            "result": "SUSTAIN_WAIT",
                            "sustain_required": sustain_secs,
                            "sustain_elapsed": round(elapsed, 1),
                            "reason": f"Sustained {elapsed:.1f}s / {sustain_secs}s",
                        })
                        all_matched = False
                        break
                    # Sustained long enough — pass
                    condition_results.append({
                        "index": i + 1, "attribute": attr, "operator": operator,
                        "threshold_raw": repr(threshold),
                        "actual_raw": repr(current_value),
                        "actual_type": type(norm_actual).__name__,
                        "value_source": value_source,
                        "result": "PASS", "sustain_met": True,
                    })
                elif matched:
                    self._sustain_tracker.pop(sustain_key, None)
                    condition_results.append({
                        "index": i + 1, "attribute": attr, "operator": operator,
                        "threshold_raw": repr(threshold),
                        "threshold_type": type(norm_threshold).__name__,
                        "actual_raw": repr(current_value),
                        "actual_type": type(norm_actual).__name__,
                        "value_source": value_source,
                        "result": "PASS",
                    })
                else:
                    self._sustain_tracker.pop(sustain_key, None)
                    condition_results.append({
                        "index": i + 1, "attribute": attr, "operator": operator,
                        "threshold_raw": repr(threshold),
                        "threshold_type": type(norm_threshold).__name__,
                        "actual_raw": repr(current_value),
                        "actual_type": type(norm_actual).__name__,
                        "value_source": value_source,
                        "result": "FAIL",
                    })
                    all_matched = False
                    break

            target_ieee = rule.get("target_ieee", "")
            target_name = names.get(target_ieee, target_ieee)

            if not all_matched:
                has_sustain = any(c.get("result") == "SUSTAIN_WAIT" for c in condition_results)
                self._add_trace({
                    "rule_id": rule_id, "level": "INFO",
                    "phase": "evaluate",
                    "result": "SUSTAIN_WAIT" if has_sustain else "NO_MATCH",
                    "message": (f"Sustain pending: {source_name} -> {target_name}" if has_sustain
                                else f"Conditions not met: {source_name} -> {target_name}"),
                    "source_ieee": source_ieee, "target_ieee": target_ieee,
                    "triggered_by": list(triggered),
                    "conditions": condition_results,
                })
                continue

            # ---- PREREQUISITES ----
            prerequisites = rule.get("prerequisites", [])
            prereq_results = []
            prereqs_met = True

            for j, prereq in enumerate(prerequisites):
                prereq_ieee = prereq["ieee"]
                prereq_attr = prereq["attribute"]
                prereq_op = prereq["operator"]
                prereq_val = prereq["value"]

                prereq_device = devices.get(prereq_ieee)
                prereq_name = names.get(prereq_ieee, prereq_ieee)

                if not prereq_device:
                    prereq_results.append({
                        "index": j + 1, "ieee": prereq_ieee,
                        "device_name": prereq_name,
                        "attribute": prereq_attr, "result": "FAIL",
                        "reason": "Device not in registry",
                    })
                    prereqs_met = False
                    break

                prereq_state = prereq_device.state or {}
                actual_value = prereq_state.get(prereq_attr)

                if actual_value is None:
                    prereq_results.append({
                        "index": j + 1, "ieee": prereq_ieee,
                        "device_name": prereq_name,
                        "attribute": prereq_attr, "result": "FAIL",
                        "reason": f"'{prereq_attr}' not in state",
                        "available_keys": list(prereq_state.keys()),
                    })
                    prereqs_met = False
                    break

                try:
                    # Use _evaluate_condition which normalises both sides
                    prereq_matched = self._evaluate_condition(actual_value, prereq_op, prereq_val)
                except Exception as e:
                    prereq_results.append({
                        "index": j + 1, "ieee": prereq_ieee,
                        "device_name": prereq_name,
                        "attribute": prereq_attr, "result": "ERROR",
                        "reason": str(e),
                    })
                    prereqs_met = False
                    break

                prereq_results.append({
                    "index": j + 1, "ieee": prereq_ieee,
                    "device_name": prereq_name,
                    "attribute": prereq_attr, "operator": prereq_op,
                    "threshold_raw": repr(prereq_val),
                    "threshold_normalised": repr(self._normalise_value(prereq_val)),
                    "actual_raw": repr(actual_value),
                    "actual_normalised": repr(self._normalise_value(actual_value)),
                    "actual_type": type(actual_value).__name__,
                    "result": "PASS" if prereq_matched else "FAIL",
                })

                if not prereq_matched:
                    prereqs_met = False
                    break

            if not prereqs_met:
                self._add_trace({
                    "rule_id": rule_id, "level": "INFO",
                    "phase": "prerequisite", "result": "PREREQ_FAIL",
                    "message": f"Prerequisites not met: {source_name} -> {target_name}",
                    "source_ieee": source_ieee, "target_ieee": target_ieee,
                    "conditions": condition_results,
                    "prerequisites": prereq_results,
                })
                continue

            self._stats["matches"] += 1

            # ---- COOLDOWN ----
            cooldown = rule.get("cooldown", DEFAULT_COOLDOWN)
            last_fired = self._cooldowns.get(rule_id, 0)
            elapsed = now - last_fired
            if elapsed < cooldown:
                self._add_trace({
                    "rule_id": rule_id, "level": "INFO",
                    "phase": "cooldown", "result": "BLOCKED",
                    "message": f"Cooldown: {elapsed:.1f}s < {cooldown}s",
                    "source_ieee": source_ieee, "target_ieee": target_ieee,
                })
                continue

            # ---- TARGET CHECKS ----
            if target_ieee not in devices:
                self._add_trace({
                    "rule_id": rule_id, "level": "ERROR",
                    "phase": "target", "result": "TARGET_MISSING",
                    "message": f"Target {target_ieee} ({target_name}) not in registry",
                    "source_ieee": source_ieee,
                })
                continue

            target_device = devices[target_ieee]
            if not hasattr(target_device, 'send_command'):
                self._add_trace({
                    "rule_id": rule_id, "level": "ERROR",
                    "phase": "target", "result": "NO_SEND_COMMAND",
                    "message": f"Target {target_name} has no send_command()",
                    "source_ieee": source_ieee,
                })
                continue

            action = rule["action"]
            command = action["command"]
            delay = action.get("delay") or 0

            trigger_summary = ", ".join(
                f"{c['attribute']}={c.get('actual_raw', '?')}" for c in condition_results
            )
            prereq_summary = ""
            if prereq_results:
                prereq_summary = " | prereqs: " + ", ".join(
                    f"{p['device_name']}.{p['attribute']}={p.get('actual_raw','?')}"
                    for p in prereq_results
                )

            self._add_trace({
                "rule_id": rule_id, "level": "INFO",
                "phase": "execute", "result": "FIRING",
                "message": (
                        f"⚡ {rule_name}: {source_name} [{trigger_summary}]{prereq_summary} "
                        f"-> {target_name} {command}={action.get('value')} EP={action.get('endpoint_id')}"
                        + (f" (delay {delay}s)" if delay else "")
                ),
                "source_ieee": source_ieee, "target_ieee": target_ieee,
                "command": command, "command_value": action.get("value"),
                "endpoint_id": action.get("endpoint_id"), "delay": delay,
                "conditions": condition_results, "prerequisites": prereq_results,
            })

            self._cooldowns[rule_id] = now
            for i in range(len(conditions)):
                self._sustain_tracker.pop(f"{rule_id}_{i}", None)

            if delay > 0:
                asyncio.create_task(
                    self._execute_with_delay(rule, target_device, action, source_ieee, delay)
                )
            else:
                asyncio.create_task(
                    self._execute_action_traced(rule, target_device, action, source_ieee)
                )

    # =========================================================================
    # EXECUTION
    # =========================================================================

    async def _execute_with_delay(self, rule, target_device, action, source_ieee, delay):
        rule_id = rule["id"]
        target_name = self._get_names().get(str(target_device.ieee), str(target_device.ieee))
        self._add_trace({
            "rule_id": rule_id, "level": "INFO",
            "phase": "delay", "result": "WAITING",
            "message": f"⏱ Waiting {delay}s before {target_name} {action['command']}",
            "source_ieee": source_ieee,
        })
        await asyncio.sleep(delay)
        await self._execute_action_traced(rule, target_device, action, source_ieee)

    async def _execute_action_traced(self, rule, target_device, action, source_ieee):
        rule_id = rule["id"]
        command = action["command"]
        value = action.get("value")
        endpoint_id = action.get("endpoint_id")
        target_ieee = str(target_device.ieee)
        target_name = self._get_names().get(target_ieee, target_ieee)

        try:
            self._add_trace({
                "rule_id": rule_id, "level": "DEBUG",
                "phase": "sending", "result": "CALLING",
                "message": f"Calling {target_name}.send_command('{command}', {repr(value)}, endpoint_id={endpoint_id})",
                "source_ieee": source_ieee, "target_ieee": target_ieee,
            })

            result = await target_device.send_command(command, value, endpoint_id=endpoint_id)

            if isinstance(result, dict):
                success = result.get("success", True)
                error_detail = result.get("error")
                result_repr = repr(result)
            elif result is None:
                success = True
                error_detail = None
                result_repr = "None (assumed OK)"
            else:
                success = bool(result)
                error_detail = None if success else f"returned falsy: {repr(result)}"
                result_repr = repr(result)

            if success:
                self._stats["executions"] += 1
                self._stats["execution_successes"] += 1
                self._add_trace({
                    "rule_id": rule_id, "level": "INFO",
                    "phase": "result", "result": "SUCCESS",
                    "message": f"✅ {target_name} {command}={value} EP={endpoint_id} — {result_repr}",
                    "source_ieee": source_ieee, "target_ieee": target_ieee,
                    "command": command, "command_value": value,
                    "endpoint_id": endpoint_id, "command_result": result_repr,
                })
            else:
                self._stats["executions"] += 1
                self._stats["execution_failures"] += 1
                self._add_trace({
                    "rule_id": rule_id, "level": "ERROR",
                    "phase": "result", "result": "COMMAND_FAILED",
                    "message": f"❌ {target_name} {command}={value} — {error_detail}",
                    "source_ieee": source_ieee, "target_ieee": target_ieee,
                    "command": command, "error": error_detail,
                })

            if self._event_emitter:
                await self._event_emitter("automation_triggered", {
                    "rule_id": rule_id, "source_ieee": source_ieee,
                    "target_ieee": target_ieee, "target_name": target_name,
                    "command": command, "value": value,
                    "success": success, "error": error_detail, "timestamp": time.time(),
                })

        except Exception as e:
            self._stats["errors"] += 1
            self._stats["execution_failures"] += 1
            tb = traceback.format_exc()
            self._add_trace({
                "rule_id": rule_id, "level": "ERROR",
                "phase": "exception", "result": "EXCEPTION",
                "message": f"💥 {target_name}.send_command('{command}', {repr(value)}): {e}",
                "source_ieee": source_ieee, "target_ieee": target_ieee,
                "error": str(e), "traceback": tb,
            })
            if self._event_emitter:
                try:
                    await self._event_emitter("automation_triggered", {
                        "rule_id": rule_id, "source_ieee": source_ieee,
                        "target_ieee": target_ieee, "command": command,
                        "success": False, "error": str(e), "timestamp": time.time(),
                    })
                except Exception:
                    pass

    # =========================================================================
    # CONDITION HELPERS
    # =========================================================================

    @staticmethod
    def _get_conditions(rule: Dict) -> List[Dict]:
        if "conditions" in rule:
            return rule["conditions"]
        if "threshold" in rule:
            return [rule["threshold"]]
        return []

    def _evaluate_condition(self, actual_value, operator, threshold_value) -> bool:
        """Evaluate with normalisation — case-insensitive for string eq/neq."""
        op_func = OPERATORS.get(operator)
        if not op_func:
            return False
        actual = self._normalise_value(actual_value)
        threshold = self._normalise_value(threshold_value)

        # String eq/neq: always case-insensitive
        if isinstance(actual, str) and isinstance(threshold, str) and operator in ("eq", "neq"):
            if operator == "eq":
                return actual.lower() == threshold.lower()
            return actual.lower() != threshold.lower()

        # Bool vs string: if one is bool and other is ON/OFF string, convert
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
            # Only convert literal "true"/"false" to bool — NOT "on"/"off"
            # Device states like "ON"/"OFF" stay as strings for case-insensitive match
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
        device = devices[ieee]
        state = device.state
        skip = {"last_seen", "available", "manufacturer", "model", "power_source", "lqi", "linkquality"}
        attributes = []
        for key, value in state.items():
            if key in skip or key.endswith("_raw") or key.startswith("attr_"):
                continue
            attr_info = {
                "attribute": key,
                "current_value": value,
                "type": self._classify_value_type(value),
            }
            if isinstance(value, bool):
                attr_info["operators"] = ["eq", "neq"]
                attr_info["value_options"] = ["true", "false"]
            elif isinstance(value, str) and value.upper() in ("ON", "OFF"):
                attr_info["operators"] = ["eq", "neq"]
                attr_info["value_options"] = ["ON", "OFF"]
            elif isinstance(value, (int, float)):
                attr_info["operators"] = ["eq", "neq", "gt", "lt", "gte", "lte"]
            else:
                attr_info["operators"] = ["eq", "neq"]
            attributes.append(attr_info)
        return sorted(attributes, key=lambda a: a["attribute"])

    def get_device_state(self, ieee: str) -> Dict[str, Any]:
        devices = self._get_devices()
        names = self._get_names()
        if ieee not in devices:
            return {}
        device = devices[ieee]
        state = device.state or {}
        attrs = []
        for k, v in state.items():
            if k.endswith("_raw") or k.startswith("attr_"):
                continue
            a = {
                "attribute": k,
                "current_value": v,
                "type": self._classify_value_type(v),
                "operators": ["eq", "neq"] if isinstance(v, (bool, str))
                else ["eq", "neq", "gt", "lt", "gte", "lte"],
            }
            if isinstance(v, bool):
                a["value_options"] = ["true", "false"]
            elif isinstance(v, str) and v.upper() in ("ON", "OFF"):
                a["value_options"] = ["ON", "OFF"]
            attrs.append(a)
        return {
            "ieee": ieee,
            "friendly_name": names.get(ieee, ieee),
            "state": state,
            "attributes": attrs,
        }

    def get_target_actions(self, ieee: str) -> List[Dict[str, Any]]:
        devices = self._get_devices()
        if ieee not in devices:
            return []
        device = devices[ieee]
        if hasattr(device, "get_control_commands"):
            return device.get_control_commands()
        return []

    def get_actuator_devices(self) -> List[Dict[str, Any]]:
        devices = self._get_devices()
        names = self._get_names()
        actuators = []
        for ieee, device in devices.items():
            caps = getattr(device, "capabilities", None)
            if not caps:
                continue
            has_cap = getattr(caps, "has_capability", lambda x: False)
            if not any(has_cap(c) for c in [
                "on_off", "light", "switch", "cover",
                "window_covering", "thermostat", "fan_control"
            ]):
                continue
            actuators.append({
                "ieee": ieee,
                "friendly_name": names.get(ieee, ieee),
                "model": getattr(device, "model", "Unknown"),
                "manufacturer": getattr(device, "manufacturer", "Unknown"),
                "commands": device.get_control_commands() if hasattr(device, "get_control_commands") else [],
            })
        return sorted(actuators, key=lambda d: d.get("friendly_name", ""))

    def get_all_devices_summary(self) -> List[Dict[str, Any]]:
        devices = self._get_devices()
        names = self._get_names()
        result = []
        for ieee, device in devices.items():
            state = device.state or {}
            result.append({
                "ieee": ieee,
                "friendly_name": names.get(ieee, ieee),
                "model": getattr(device, "model", "Unknown"),
                "state_keys": [k for k in state.keys()
                               if not k.endswith("_raw") and not k.startswith("attr_")],
            })
        return sorted(result, key=lambda d: d.get("friendly_name", ""))

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
        }