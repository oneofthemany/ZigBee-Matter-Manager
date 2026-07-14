"""
AI Assistant - LLM Provider Abstraction
========================================
Supports multiple LLM backends via OpenAI-compatible API format.

Providers:
  openai   - OpenAI API (gpt-4o, gpt-4o-mini, etc.)
  ollama   - Local Ollama instance (llama3, mistral, etc.)
  anthropic - Anthropic API (claude-sonnet, etc.)
  custom   - Any OpenAI-compatible endpoint

Configuration in config.yaml:
  ai:
    provider: ollama          # openai | ollama | anthropic | custom
    model: llama3.1           # Model name
    api_key: ""               # Required for openai/anthropic, optional for ollama
    base_url: ""              # Override endpoint (auto-set for known providers)
    temperature: 0.3          # Lower = more deterministic rule generation
    max_tokens: 2000
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("modules.ai_assistant")

# Provider base URL defaults
PROVIDER_DEFAULTS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "requires_key": True,
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "llama3.1:8b-instruct-q4_K_M",
        "requires_key": False,
    },
    "sglang": {
        # SGLang serves an OpenAI-compatible API (default port 30000). GPU-class
        # backend — only surfaced in the UI when the host assessor says it's
        # viable. Model is whatever the SGLang server was launched with.
        "base_url": "http://localhost:30000/v1",
        "model": "",
        "requires_key": False,
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-20250514",
        "requires_key": True,
    },
    "custom": {
        "base_url": "",
        "model": "",
        "requires_key": False,
    },
}


class AIAssistant:
    """Thin async wrapper around OpenAI-compatible chat completion APIs."""

    def __init__(self, config: Dict[str, Any]):
        self.provider = config.get("provider", "ollama")
        defaults = PROVIDER_DEFAULTS.get(self.provider, PROVIDER_DEFAULTS["custom"])

        self.base_url = (config.get("base_url") or defaults["base_url"]).rstrip("/")
        self.model = config.get("model") or defaults["model"]
        self.api_key = config.get("api_key", "")
        self.temperature = float(config.get("temperature", 0.3))
        self.max_tokens = int(config.get("max_tokens", 2000))
        # Human-readable detail of the most recent chat() failure, so callers
        # (test endpoint, UI) can show *why* instead of a bare "no response".
        self.last_error: Optional[str] = None
        # Local model servers process one request at a time; concurrent calls
        # just queue behind each other until the caller's timeout fires. One
        # in-flight request per assistant keeps every call inside its budget.
        self._lock = asyncio.Lock()

    def _is_local(self) -> bool:
        return any(h in self.base_url for h in
                   ("127.0.0.1", "localhost", "10.0.2.2"))

    def _default_timeout(self) -> float:
        # CPU-bound local models legitimately take minutes on a long prompt;
        # remote APIs that haven't answered in 2 minutes never will.
        return 480.0 if self._is_local() else 120.0

        if defaults["requires_key"] and not self.api_key:
            logger.warning(f"AI provider '{self.provider}' requires an API key")

        logger.info(f"AI Assistant initialised: provider={self.provider} "
                    f"model={self.model} base_url={self.base_url}")

    async def chat(self, system_prompt: str, user_message: str,
                   temperature: Optional[float] = None,
                   history: Optional[list] = None,
                   timeout: Optional[float] = None) -> Optional[str]:
        """
        Send a chat completion request and return the text response.
        Uses aiohttp to avoid adding openai SDK as a dependency.

        history: optional list of prior turns [{"role": "user"|"assistant",
        "content": str}, ...] injected between the system prompt and the new
        user message so the model has conversational context.
        """
        import aiohttp

        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}

        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            # Anthropic uses a different header
            if self.provider == "anthropic":
                headers["x-api-key"] = self.api_key
                headers["anthropic-version"] = "2023-06-01"

        prior = [{"role": m["role"], "content": m["content"]}
                 for m in (history or [])
                 if m.get("role") in ("user", "assistant") and m.get("content")]

        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}, *prior,
                         {"role": "user", "content": user_message}],
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": self.max_tokens,
        }

        # Anthropic Messages API uses a slightly different format
        if self.provider == "anthropic":
            url = f"{self.base_url}/messages"
            payload = {
                "model": self.model,
                "system": system_prompt,
                "messages": [*prior, {"role": "user", "content": user_message}],
                "temperature": temperature if temperature is not None else self.temperature,
                "max_tokens": self.max_tokens,
            }

        total = timeout if timeout is not None else self._default_timeout()
        try:
            async with self._lock, aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=total)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        self.last_error = f"HTTP {resp.status} from {url}: {body[:300]}"
                        logger.error(f"AI API error {resp.status}: {body[:500]}")
                        return None

                    data = await resp.json()

                    # OpenAI-compatible format
                    if "choices" in data:
                        self.last_error = None
                        return data["choices"][0]["message"]["content"]

                    # Anthropic format
                    if "content" in data:
                        for block in data["content"]:
                            if block.get("type") == "text":
                                self.last_error = None
                                return block["text"]

                    self.last_error = (f"Unexpected response format from {url} "
                                       f"(keys: {', '.join(list(data.keys())[:8])})")
                    logger.error(f"Unexpected AI response format: {list(data.keys())}")
                    return None

        except asyncio.TimeoutError:
            self.last_error = f"Request to {url} timed out after {int(total)}s"
            logger.error(f"AI API request timed out after {int(total)}s")
            return None
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e} (url: {url})"
            logger.error(f"AI API request failed: {e}")
            return None

    async def ping(self) -> Dict[str, Any]:
        """Minimal round-trip to the provider — no device context, tiny prompt.

        Returns latency and, on failure, the specific reason (connection
        refused, HTTP status + body, timeout) rather than a generic error.
        """
        import time
        t0 = time.monotonic()
        reply = await self.chat(
            "You are a connectivity probe. Reply with exactly: OK",
            "Reply with exactly: OK", temperature=0.0, timeout=60)
        latency_ms = int((time.monotonic() - t0) * 1000)
        if reply is None:
            return {"success": False, "latency_ms": latency_ms,
                    "error": self.last_error or "No response from provider"}
        return {"success": True, "latency_ms": latency_ms,
                "reply": reply.strip()[:120]}

    def is_configured(self) -> bool:
        """Check if the provider has minimum viable configuration."""
        defaults = PROVIDER_DEFAULTS.get(self.provider, PROVIDER_DEFAULTS["custom"])
        if defaults["requires_key"] and not self.api_key:
            return False
        if not self.base_url:
            return False
        return True

    def get_status(self) -> Dict[str, Any]:
        """Return current configuration status (no secrets)."""
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "configured": self.is_configured(),
            "has_api_key": bool(self.api_key),
        }