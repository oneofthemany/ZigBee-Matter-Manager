"""
AI API - FastAPI routes for AI-assisted automation generation.

Uses module-level getters (same pattern as routes/ota_routes.py)
to avoid closure scoping issues with FastAPI lifespan.

Endpoints:
  POST /api/ai/automation       — Generate automation rule from natural language
  GET  /api/ai/status           — Check AI provider configuration status
  POST /api/ai/config           — Update AI provider settings
  GET  /api/ai/context          — Preview the device context sent to the LLM (debug)
"""

import asyncio
import logging
from typing import Callable, Optional

from fastapi import FastAPI, APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ============================================================================
# MODULE-LEVEL GETTERS (set by register_ai_routes)
# ============================================================================

_get_ai_assistant = None
_get_ai_automations = None
_get_ai_chat = None
_config_saver = None
_ollama_mgr = None

router = APIRouter(prefix="/api/ai", tags=["ai"])


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class AIGenerateRequest(BaseModel):
    prompt: str = Field(..., description="Natural language automation description",
                        min_length=5, max_length=2000)


class AIChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


class OllamaPullRequest(BaseModel):
    model: str = Field(..., min_length=1, max_length=80)


class AITestRequest(BaseModel):
    # One stage per call so the UI can show live progress between stages.
    stage: str = Field("connectivity",
                       pattern="^(connectivity|json|rule)$")


class SglangInstallRequest(BaseModel):
    model: str = Field(..., min_length=1, max_length=120)
    hf_token: Optional[str] = Field(None, max_length=200)


class AIConfigRequest(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


# ============================================================================
# REGISTRATION
# ============================================================================

def register_ai_routes(app: FastAPI, ai_assistant_getter: Callable,
                       ai_automations_getter: Callable,
                       config_saver: Optional[Callable] = None,
                       ai_chat_getter: Optional[Callable] = None):
    """
    Register AI API routes on the FastAPI app.

    Args:
        app: FastAPI instance
        ai_assistant_getter: callable returning AIAssistant instance
        ai_automations_getter: callable returning AIAutomations instance
        config_saver: optional callable(ai_config_dict) to persist to config.yaml
        ai_chat_getter: optional callable returning AIChat instance
    """
    global _get_ai_assistant, _get_ai_automations, _config_saver, _get_ai_chat
    _get_ai_assistant = ai_assistant_getter
    _get_ai_automations = ai_automations_getter
    _get_ai_chat = ai_chat_getter
    _config_saver = config_saver
    app.include_router(router)
    logger.info("AI API routes registered")


# ============================================================================
# ROUTES
# ============================================================================

@router.post("/automation")
async def generate_automation(request: AIGenerateRequest):
    """Generate an automation rule from natural language.

    Local-first: the deterministic parser runs first (no network, SBC-friendly).
    Only if it cannot fully resolve the sentence do we fall back to the LLM,
    and only when a provider is configured. If neither succeeds we return the
    local parser's helpful, grounded error rather than a bare 503.
    """
    ai_auto = _get_ai_automations() if _get_ai_automations else None
    if not ai_auto:
        raise HTTPException(503, "AI automations module not available")

    # 1. Deterministic local parser.
    local = ai_auto.local.parse(request.prompt)
    if local.get("success"):
        return local

    # 2. LLM fallback (only when configured).
    ai = _get_ai_assistant() if _get_ai_assistant else None
    if ai and ai.is_configured():
        result = await ai_auto.generate_rule(request.prompt)
        result.setdefault("source", "llm")
        # Carry the local parser's hints if the LLM also fails.
        if not result.get("success"):
            result.setdefault("suggestions", local.get("partial"))
            result.setdefault("examples", local.get("examples"))
        return result

    # 3. Neither — return the local parser's grounded guidance.
    local.setdefault("error", "Could not understand the request.")
    local["llm_available"] = False
    return local


@router.get("/nl-help")
async def nl_help():
    """Example phrasings + device names for the local NL builder."""
    ai_auto = _get_ai_automations() if _get_ai_automations else None
    if not ai_auto:
        raise HTTPException(503, "AI automations module not available")
    return ai_auto.local.help()


@router.post("/chat")
async def chat_send(request: AIChatRequest):
    """Send a message to the domain-aware chat (grounded in devices + rules)."""
    chat = _get_ai_chat() if _get_ai_chat else None
    if not chat:
        raise HTTPException(503, "Chat module not available")
    return await chat.send(request.message)


@router.get("/chat/history")
async def chat_history():
    """Return the persisted chat history."""
    chat = _get_ai_chat() if _get_ai_chat else None
    if not chat:
        return {"messages": []}
    return {"messages": chat.get_history()}


@router.delete("/chat/history")
async def chat_clear():
    """Clear the chat history."""
    chat = _get_ai_chat() if _get_ai_chat else None
    if not chat:
        raise HTTPException(503, "Chat module not available")
    return chat.clear()


# Verb templates for the grounded rule-generation test, tried in order.
# The test device must actually support the command it's asked to run —
# "turn off" a blind is nonsense; blinds close.
_TEST_VERBS = [("off", "Turn off {name} after 5 minutes"),
               ("on", "Turn on {name} when the time is after 22:00"),
               ("close", "Close {name} after 5 minutes"),
               ("open", "Open {name} when the time is after 08:00"),
               ("toggle", "Toggle {name} after 5 minutes")]


def _pick_test_intent(ai_auto):
    """Choose a real device + a command it supports for the capability test.

    Returns (prompt, device_name, command) or (None, None, None).
    """
    try:
        devices = ai_auto._engine.get_all_devices_summary() or []
    except Exception:
        return None, None, None
    for cmd, template in _TEST_VERBS:
        for dev in devices:
            ieee = dev.get("ieee", "")
            if not ieee or ieee.startswith("group:"):
                continue
            try:
                actions = ai_auto._engine.get_target_actions(ieee) or []
            except Exception:
                continue
            if any(a.get("command") == cmd for a in actions):
                name = dev.get("friendly_name", ieee)
                return template.format(name=name), name, cmd
    return None, None, None


@router.post("/test")
async def ai_test(request: AITestRequest):
    """One stage of the provider self-test; the UI calls the stages in order.

    Unlike POST /automation (local parser first, full device-context prompt),
    this talks to the provider directly so a failure shows the *actual* reason
    (connection refused, HTTP 404 model-not-found, timeout) instead of a
    generic "No response from AI provider".

    Stages:
      connectivity — tiny prompt, no device context, fails fast (60s cap).
      json         — strict-format compliance probe.
      rule         — real automation generation, grounded: the test device is
                     one that actually supports the command being asked, and
                     the device context is prefiltered to relevant devices.
    """
    ai = _get_ai_assistant() if _get_ai_assistant else None
    if not ai:
        raise HTTPException(503, "AI module not initialised")

    base = {"provider": ai.provider, "model": ai.model, "base_url": ai.base_url}

    if not ai.is_configured():
        return {**base, "name": "configuration", "success": False,
                "error": f"Provider '{ai.provider}' is missing required "
                         f"settings (API key / base URL). Save a valid "
                         f"configuration first."}

    if request.stage == "connectivity":
        ping = await ai.ping()
        return {**base, "name": "connectivity", **ping}

    import json as _json
    import re as _re
    import time as _time

    if request.stage == "json":
        t0 = _time.monotonic()
        raw = await ai.chat(
            "You are a JSON generator. Respond with ONLY a raw JSON object. "
            "No prose, no markdown fences.",
            'Return exactly this JSON object: {"ok": true, "model_check": "pass"}',
            temperature=0.0, timeout=120)
        latency_ms = int((_time.monotonic() - t0) * 1000)
        parsed = None
        if raw:
            cleaned = _re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
            try:
                parsed = _json.loads(cleaned)
            except Exception:
                m = _re.search(r"\{[\s\S]*\}", cleaned)
                if m:
                    try:
                        parsed = _json.loads(m.group())
                    except Exception:
                        parsed = None
        return {**base, "name": "json_compliance",
                "success": isinstance(parsed, dict),
                "latency_ms": latency_ms,
                "reply": (raw or "")[:200],
                "error": None if isinstance(parsed, dict) else
                         (ai.last_error if raw is None else
                          f"Model replied but not with parseable JSON: "
                          f"{(raw or '')[:200]}")}

    # stage == "rule"
    ai_auto = _get_ai_automations() if _get_ai_automations else None
    if not ai_auto:
        return {**base, "name": "rule_generation", "success": None,
                "error": "Skipped — AI automations module not available."}

    prompt, dev_name, cmd = _pick_test_intent(ai_auto)
    if not prompt:
        return {**base, "name": "rule_generation", "success": None,
                "error": "Skipped — no controllable device found to test against."}

    t0 = _time.monotonic()
    gen = await ai_auto.generate_rule(prompt)
    return {**base, "name": "rule_generation",
            "success": bool(gen.get("success")),
            "latency_ms": int((_time.monotonic() - t0) * 1000),
            "prompt": prompt,
            "device": dev_name,
            "command": cmd,
            "context_devices": gen.get("context_devices"),
            "prompt_chars": gen.get("prompt_chars"),
            "explanation": gen.get("explanation"),
            "raw_response": (gen.get("raw_response") or "")[:400] or None,
            "error": gen.get("error")}


@router.get("/host")
async def host_capability():
    """llmfit-style host assessment: can this box run a local LLM, and which?

    Read-only — measures CPU/RAM/GPU and recommends a model/backend. Gates
    whether the install/serving UI should offer anything at all.
    """
    from modules.llm_host import HostCapabilityAssessor
    # nvidia-smi/lspci probes take seconds — keep them off the event loop.
    return await asyncio.to_thread(HostCapabilityAssessor().assess)


# ── Ollama enablement (privileged; UI-gated behind explicit confirm) ─────────

def _ollama():
    global _ollama_mgr
    if _ollama_mgr is None:
        from modules.ollama_manager import OllamaManager
        _ollama_mgr = OllamaManager()
    # Runtime probes run in worker threads; the manager needs the app loop to
    # schedule its install/pull coroutines from there.
    _ollama_mgr.bind_loop(asyncio.get_running_loop())
    return _ollama_mgr


@router.get("/ollama/status")
async def ollama_status():
    """Detect whether the local Ollama container is installed/running + models."""
    # podman/urlopen probes block for seconds — keep them off the event loop.
    return await asyncio.to_thread(_ollama().status)


@router.get("/ollama/job")
async def ollama_job():
    """Poll the current install/pull job's status and streamed log."""
    return _ollama().job_status()


@router.post("/ollama/install")
async def ollama_install():
    """Start (or create) the Ollama container. Privileged; runs in background."""
    result = await asyncio.to_thread(_ollama().install)
    if not result.get("success"):
        raise HTTPException(400, result.get("error"))
    return result


@router.post("/ollama/pull")
async def ollama_pull(request: OllamaPullRequest):
    """Pull a model into the running Ollama container. Runs in background."""
    result = await asyncio.to_thread(_ollama().pull, request.model)
    if not result.get("success"):
        raise HTTPException(400, result.get("error"))
    return result


# ── SGLang enablement (privileged; UI-gated behind host viability) ───────────

_sglang_mgr = None


def _sglang():
    global _sglang_mgr
    if _sglang_mgr is None:
        from modules.sglang_manager import SGLangManager
        _sglang_mgr = SGLangManager()
    _sglang_mgr.bind_loop(asyncio.get_running_loop())
    return _sglang_mgr


@router.get("/sglang/status")
async def sglang_status():
    """Detect whether the local SGLang container is installed/running + model."""
    # podman/urlopen probes block for seconds — keep them off the event loop.
    return await asyncio.to_thread(_sglang().status)


@router.get("/sglang/job")
async def sglang_job():
    """Poll the current SGLang install job's status and streamed log."""
    return _sglang().job_status()


@router.post("/sglang/install")
async def sglang_install(request: SglangInstallRequest):
    """Create/start the SGLang container serving a given HF model.

    Privileged and GPU-gated: refused unless the host assessor marks SGLang
    viable and NVIDIA CDI passthrough exists. Runs in background.
    """
    result = await asyncio.to_thread(_sglang().install, request.model, request.hf_token)
    if not result.get("success"):
        raise HTTPException(400, result.get("error"))
    return result


@router.get("/status")
async def ai_status():
    """Check AI provider configuration status."""
    ai = _get_ai_assistant() if _get_ai_assistant else None
    if not ai:
        return {"configured": False, "provider": None}
    return ai.get_status()


@router.post("/config")
async def update_ai_config(request: AIConfigRequest):
    """Update AI provider settings and persist to config.yaml."""
    ai = _get_ai_assistant() if _get_ai_assistant else None
    if not ai:
        raise HTTPException(503, "AI module not initialised")

    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "Nothing to update")

    # Apply updates to the live instance
    if "provider" in updates:
        ai.provider = updates["provider"]
        from modules.ai_assistant import PROVIDER_DEFAULTS
        defaults = PROVIDER_DEFAULTS.get(ai.provider, {})
        if not request.base_url:
            ai.base_url = defaults.get("base_url", ai.base_url)
        if not request.model:
            ai.model = defaults.get("model", ai.model)
    if "model" in updates:
        ai.model = updates["model"]
    if "api_key" in updates:
        ai.api_key = updates["api_key"]
    if "base_url" in updates:
        ai.base_url = updates["base_url"].rstrip("/")
    if "temperature" in updates:
        ai.temperature = float(updates["temperature"])
    if "max_tokens" in updates:
        ai.max_tokens = int(updates["max_tokens"])

    # Persist to config.yaml
    if _config_saver:
        ai_config = {
            "provider": ai.provider,
            "model": ai.model,
            "api_key": ai.api_key,
            "base_url": ai.base_url,
            "temperature": ai.temperature,
            "max_tokens": ai.max_tokens,
        }
        try:
            _config_saver(ai_config)
        except Exception as e:
            logger.error(f"Failed to persist AI config: {e}")

    return {"success": True, **ai.get_status()}


@router.get("/context")
async def ai_context(intent: Optional[str] = None):
    """Preview the device context that would be sent to the LLM (debug).

    With ?intent=<request text> the context is prefiltered to relevant
    devices, exactly as generate_rule does for that request.
    """
    ai_auto = _get_ai_automations() if _get_ai_automations else None
    if not ai_auto:
        raise HTTPException(503, "AI automations module not available")

    ctx = ai_auto._build_device_context(intent)
    prompt = ai_auto._build_system_prompt(intent)
    return {
        "device_count": ctx.count("\n- ") + (1 if ctx.startswith("- ") else 0),
        "context_chars": len(ctx),
        "prompt_chars": len(prompt),
        "device_context": ctx,
    }