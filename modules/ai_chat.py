"""
Domain-aware chat — a conversational assistant grounded in THIS gateway's live
devices and automation rules.

Deliberately not a generic chatbot: the system prompt is rebuilt from the
registry every turn, so it can explain device state, diagnose why a rule did or
did not fire, and suggest rules in the app's own vocabulary. History persists to
data/ai_chat_history.json, with a bounded window sent to keep token cost
predictable.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List

logger = logging.getLogger("modules.ai_chat")

HISTORY_FILE = "./data/ai_chat_history.json"
MAX_STORED = 60        # messages kept on disk
MAX_CONTEXT_TURNS = 12  # messages sent to the model as history

SYSTEM_PREAMBLE = """You are the assistant built into ZigBee Manager, a local \
Zigbee/Matter home-automation gateway. You help the user understand and run \
their smart home.

You can:
- Explain what a device is doing from its current attributes.
- Diagnose why an automation did or didn't fire (conditions, prerequisites, \
cooldown, time windows, state-machine transitions).
- Suggest automations in the app's own terms: trigger conditions on a source \
device, optional prerequisites (checks on other devices), and THEN/ELSE action \
sequences (command, delay, wait_for, gate, if_then_else, parallel).
- Answer practical questions (e.g. low batteries, offline devices).

Be concise and practical. Use the device friendly-names shown below, not raw \
IEEE addresses. When you propose an automation, describe it plainly and remind \
the user they can type it into the AI Automation Builder to create it. Never \
invent devices or attributes that aren't listed."""


class AIChat:
    def __init__(self, ai_assistant, ai_automations):
        self._ai = ai_assistant
        self._auto = ai_automations          # reused for device context
        self._engine = ai_automations._engine
        self._history: List[Dict[str, Any]] = []
        self._load()


    async def send(self, message: str) -> Dict[str, Any]:
        message = (message or "").strip()
        if not message:
            return {"success": False, "error": "Empty message"}
        if not self._ai or not self._ai.is_configured():
            return {"success": False,
                    "error": "No AI model configured. Configure a provider "
                             "(or install Ollama) in AI settings to use chat."}

        self._append("user", message)
        system = self._system_prompt()
        # Send prior turns (excluding the message we just appended).
        prior = self._history[:-1][-MAX_CONTEXT_TURNS:]
        try:
            reply = await self._ai.chat(system, message, history=prior)
        except Exception as e:
            logger.error(f"Chat call failed: {e}")
            reply = None
        if not reply:
            # Roll back the unanswered user turn so retry isn't duplicated.
            if self._history and self._history[-1]["role"] == "user":
                self._history.pop()
            self._save()
            return {"success": False,
                    "error": "No response from the AI model (check it's running "
                             "and reachable)."}
        self._append("assistant", reply)
        return {"success": True, "reply": reply}

    def get_history(self) -> List[Dict[str, Any]]:
        return list(self._history)

    def clear(self) -> Dict[str, Any]:
        self._history = []
        self._save()
        return {"success": True}

    # System prompt (rebuilt each turn from live state)

    def _system_prompt(self) -> str:
        parts = [SYSTEM_PREAMBLE, "\n## Devices\n"]
        try:
            parts.append(self._auto._build_device_context())
        except Exception as e:
            logger.debug(f"device context failed: {e}")
            parts.append("(device list unavailable)")
        parts.append("\n\n## Automation Rules\n")
        parts.append(self._rules_summary())
        return "".join(parts)

    def _rules_summary(self) -> str:
        try:
            rules = self._engine.get_rules()
        except Exception:
            rules = []
        if not rules:
            return "(no automation rules defined)"
        lines = []
        for r in rules:
            state = r.get("_state", "?")
            en = "enabled" if r.get("enabled", True) else "disabled"
            conds = self._fmt_conditions(r.get("conditions", []))
            then = self._fmt_steps(r.get("then_sequence", []))
            els = self._fmt_steps(r.get("else_sequence", []))
            name = r.get("name") or r.get("id")
            src = r.get("source_name") or r.get("source_ieee", "?")
            line = f"- \"{name}\" [{en}, state={state}] on {src}: IF {conds}"
            if r.get("prerequisites"):
                line += f" CHECK {self._fmt_prereqs(r['prerequisites'])}"
            if then:
                line += f" THEN {then}"
            if els:
                line += f" ELSE {els}"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _fmt_conditions(conds: List[Dict]) -> str:
        out = []
        for c in conds:
            if c.get("type") == "time_window":
                out.append(f"time {c.get('time_from')}-{c.get('time_to')}")
            else:
                out.append(f"{c.get('attribute')} {c.get('operator')} {c.get('value')}")
        return " and ".join(out) or "(none)"

    @staticmethod
    def _fmt_prereqs(prereqs: List[Dict]) -> str:
        out = []
        for p in prereqs:
            if p.get("type") == "time_window":
                out.append(f"time {p.get('time_from')}-{p.get('time_to')}")
            else:
                neg = "NOT " if p.get("negate") else ""
                out.append(f"{neg}{p.get('device_name') or p.get('ieee')} "
                           f"{p.get('attribute')} {p.get('operator')} {p.get('value')}")
        return " and ".join(out)

    @staticmethod
    def _fmt_steps(steps: List[Dict]) -> str:
        out = []
        for s in steps:
            t = s.get("type")
            if t == "command":
                v = f"={s['value']}" if s.get("value") is not None else ""
                out.append(f"{s.get('command')}{v} {s.get('target_name') or s.get('target_ieee')}")
            elif t == "delay":
                out.append(f"wait {s.get('seconds')}s")
            elif t == "wait_for":
                out.append(f"wait for {s.get('device_name') or s.get('ieee')}")
            elif t == "condition":
                out.append("gate")
            elif t == "if_then_else":
                out.append("if/then/else")
            elif t == "parallel":
                out.append("parallel")
        return ", ".join(out)

    # Persistence

    def _append(self, role: str, content: str):
        self._history.append({"role": role, "content": content, "ts": time.time()})
        if len(self._history) > MAX_STORED:
            self._history = self._history[-MAX_STORED:]
        self._save()

    def _load(self):
        if not os.path.exists(HISTORY_FILE):
            return
        try:
            with open(HISTORY_FILE) as f:
                data = json.load(f)
            self._history = data.get("messages", []) if isinstance(data, dict) else []
        except Exception as e:
            logger.error(f"Failed to load chat history: {e}")
            self._history = []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
            with open(HISTORY_FILE, "w") as f:
                json.dump({"messages": self._history}, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save chat history: {e}")
