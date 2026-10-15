"""Fallback provider: a small model running locally in Ollama.

Nothing leaves the machine on this path. Ollama serves an OpenAI-compatible
API under /v1, so it uses the same client as OpenRouter.
"""

from app.config import Settings
from app.llm.client import OpenAICompatibleProvider


def ollama_provider(settings: Settings) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="ollama",
        base_url=f"{settings.ollama_base_url.rstrip('/')}/v1",
        api_key="ollama",  # Ollama ignores the key, but the SDK requires one
        model=settings.ollama_model,
        timeout_seconds=settings.ollama_timeout_seconds,
        retries=settings.ollama_retries,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )
