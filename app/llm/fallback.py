"""Fallback chain: try each provider in order, with retries on transient errors.

    OpenRouter (retry x2) -> Ollama (retry x2) -> raise (the pipeline degrades)

Transient errors (timeouts, connection failures, HTTP 429 and 5xx) are retried
with exponential backoff and jitter. Anything else, such as a 400 or a missing
API key, will not get better by retrying, so the chain moves straight on to
the next provider. Every attempt is recorded so the trace shows what happened.
"""

import logging
import time
from collections.abc import Callable

from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from app.chaos import check_provider
from app.config import Settings
from app.llm.client import Attempt, LLMResponse, OpenAICompatibleProvider

logger = logging.getLogger(__name__)


class AllProvidersFailed(RuntimeError):
    def __init__(self, attempts: list[Attempt]) -> None:
        super().__init__(f"all LLM providers failed after {len(attempts)} attempts")
        self.attempts = attempts


def is_transient(error: BaseException) -> bool:
    """Errors worth retrying on the same provider."""
    if isinstance(error, (APITimeoutError, APIConnectionError, RateLimitError, ConnectionError, TimeoutError)):
        return True
    return isinstance(error, APIStatusError) and error.status_code >= 500


class FallbackChain:
    """Looks like a single provider to the pipeline, but tries several in order."""

    name = "fallback-chain"

    def __init__(
        self,
        providers: list[OpenAICompatibleProvider],
        settings: Settings,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.providers = providers
        self.settings = settings
        self.sleep = sleep  # injectable so tests do not wait for backoff

    def _call_with_retries(self, provider: OpenAICompatibleProvider, messages: list[dict[str, str]], attempts: list[Attempt]) -> LLMResponse:
        retrying = Retrying(
            stop=stop_after_attempt(1 + self.settings.llm_retries),
            wait=wait_exponential_jitter(multiplier=self.settings.llm_backoff_initial_seconds, max=self.settings.llm_backoff_max_seconds),
            retry=retry_if_exception(is_transient),
            sleep=self.sleep,
            reraise=True,
        )
        for attempt in retrying:
            with attempt:
                number = attempt.retry_state.attempt_number
                started = time.perf_counter()
                try:
                    check_provider(provider.name)  # chaos flag: fail as if the network were down
                    response = provider.generate_json(messages)
                except Exception as error:
                    attempts.append(Attempt(provider.name, provider.model, number, type(error).__name__, _ms_since(started)))
                    logger.warning("%s attempt %d failed: %s", provider.name, number, error)
                    raise
                attempts.append(Attempt(provider.name, provider.model, number, "ok", response.latency_ms))
                return response
        raise AssertionError("unreachable: tenacity either returns or re-raises")

    def generate_json(self, messages: list[dict[str, str]]) -> LLMResponse:
        attempts: list[Attempt] = []
        for index, provider in enumerate(self.providers):
            try:
                response = self._call_with_retries(provider, messages, attempts)
            except Exception:
                continue  # this provider is out; try the next one
            # Tell the truth: which provider answered and whether it was a fallback.
            return LLMResponse(
                text=response.text, model=response.model, provider=response.provider,
                latency_ms=response.latency_ms, prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                fallback_used=index > 0, attempts=tuple(attempts),
            )
        raise AllProvidersFailed(attempts)


def _ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
