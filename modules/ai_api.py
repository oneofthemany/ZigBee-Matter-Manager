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


@router.get("/host")
async def host_capability():
    """llmfit-style host assessment: can this box run a local LLM, and which?

    Read-only — measures CPU/RAM/GPU and recommends a model/backend. Gates
    whether the install/serving UI should offer anything at all.
    """
    from modules.llm_host import HostCapabilityAssessor
    return HostCapabilityAssessor().assess()


# ── Ollama enablement (privileged; UI-gated behind explicit confirm) ─────────

def _ollama():
    global _ollama_mgr
    if _ollama_mgr is None:
        from modules.ollama_manager import OllamaManager
        _ollama_mgr = OllamaManager()
    return _ollama_mgr


@router.get("/ollama/status")
async def ollama_status():
    """Detect whether the local Ollama container is installed/running + models."""
    return _ollama().status()


@router.get("/ollama/job")
async def ollama_job():
    """Poll the current install/pull job's status and streamed log."""
    return _ollama().job_status()


@router.post("/ollama/install")
async def ollama_install():
    """Start (or create) the Ollama container. Privileged; runs in background."""
    result = _ollama().install()
    if not result.get("success"):
        raise HTTPException(400, result.get("error"))
    return result


@router.post("/ollama/pull")
async def ollama_pull(request: OllamaPullRequest):
    """Pull a model into the running Ollama container. Runs in background."""
    result = _ollama().pull(request.model)
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
async def ai_context():
    """Preview the device context that would be sent to the LLM (debug)."""
    ai_auto = _get_ai_automations() if _get_ai_automations else None
    if not ai_auto:
        raise HTTPException(503, "AI automations module not available")

    ctx = ai_auto._build_device_context()
    prompt = ai_auto._build_system_prompt()
    return {
        "device_count": ctx.count("\n- ") + (1 if ctx.startswith("- ") else 0),
        "context_chars": len(ctx),
        "prompt_chars": len(prompt),
        "device_context": ctx,
    }