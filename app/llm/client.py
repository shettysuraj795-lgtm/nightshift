"""One client for any OpenAI-compatible chat API (OpenRouter and Ollama both are).

Using the same client for the hosted model and the local fallback keeps the
two paths identical: only the base URL, key and model ID differ.
"""

import time
from dataclasses import dataclass, field
from functools import cached_property

from openai import OpenAI


@dataclass(frozen=True)
class Attempt:
    """One call to one provider, for traces: what was tried and how it ended."""

    provider: str
    model: str
    attempt: int
    outcome: str  # "ok" or the exception class name
    latency_ms: int


@dataclass(frozen=True)
class LLMResponse:
    """Raw model output plus the metadata the result and traces report."""

    text: str
    model: str  # the model that actually answered, as reported by the API
    provider: str
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    fallback_used: bool = False
    attempts: tuple[Attempt, ...] = field(default_factory=tuple)


class OpenAICompatibleProvider:
    """Calls one model on an OpenAI-compatible endpoint and asks for a JSON object back."""

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        temperature: float,
        max_tokens: int,
        extra_body: dict | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra_body = extra_body or {}

    @cached_property
    def client(self) -> OpenAI:
        """Created on first call, so missing configuration fails that call (and the
        pipeline falls back) instead of stopping the API from starting."""
        if not self.api_key or not self.model:
            raise RuntimeError(f"{self.name}: API key and model ID must be set in .env")
        return OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            max_retries=0,  # retries are handled explicitly in app/llm/fallback.py, not hidden in the SDK
        )

    def generate_json(self, messages: list[dict[str, str]]) -> LLMResponse:
        """One chat completion in JSON mode. Raises on HTTP errors and timeouts."""
        started = time.perf_counter()
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            extra_body=self.extra_body,
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
