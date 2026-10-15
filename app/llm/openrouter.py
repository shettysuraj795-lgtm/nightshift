"""OpenRouter client, using the OpenAI SDK against an OpenAI-compatible API.

OpenRouter is the demo harness only. In a real deployment the same client
points at an EU-hosted or customer-hosted endpoint by changing
OPENROUTER_BASE_URL, OPENROUTER_API_KEY and TRIAGE_MODEL in configuration.
"""

import time
from dataclasses import dataclass
from functools import cached_property

from openai import OpenAI

from app.config import Settings, get_settings


@dataclass(frozen=True)
class LLMResponse:
    """Raw model output plus the metadata the result and traces report."""

    text: str
    model: str  # the model that actually answered, as reported by the API
    provider: str
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None


class OpenRouterProvider:
    """Calls one model on OpenRouter and asks for a JSON object back."""

    name = "openrouter"

    def __init__(self, model: str, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.model = model

    @cached_property
    def client(self) -> OpenAI:
        """Created on first call, so missing configuration fails that call (and the
        pipeline degrades) instead of stopping the API from starting."""
        if not self.settings.openrouter_api_key or not self.model:
            raise RuntimeError("OPENROUTER_API_KEY and the model ID must be set in .env")
        return OpenAI(
            api_key=self.settings.openrouter_api_key,
            base_url=self.settings.openrouter_base_url,
            timeout=self.settings.llm_timeout_seconds,
            max_retries=0,  # retries are handled explicitly in Phase 4, not hidden in the SDK
        )

    def generate_json(self, messages: list[dict[str, str]]) -> LLMResponse:
        """One chat completion in JSON mode. Raises on HTTP errors and timeouts."""
        started = time.perf_counter()
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=self.settings.llm_temperature,
            max_tokens=self.settings.llm_max_tokens,
            # Only route to OpenRouter providers that honour response_format.
            extra_body={"provider": {"require_parameters": True}},
        )
        usage = response.usage
        return LLMResponse(
            text=response.choices[0].message.content or "",
            model=response.model or self.model,
            provider=self.name,
            latency_ms=round((time.perf_counter() - started) * 1000),
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
        )
