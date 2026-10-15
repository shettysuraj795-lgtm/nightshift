"""Primary provider: OpenRouter.

OpenRouter is the demo harness only. In a real deployment the same client
points at an EU-hosted or customer-hosted endpoint by changing
OPENROUTER_BASE_URL, OPENROUTER_API_KEY and TRIAGE_MODEL in configuration.
"""

from app.config import Settings
from app.llm.client import OpenAICompatibleProvider


def openrouter_provider(settings: Settings, model: str | None = None) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="openrouter",
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key,
        model=model or settings.triage_model,
        timeout_seconds=settings.llm_timeout_seconds,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        # Only route to OpenRouter providers that honour response_format.
        extra_body={"provider": {"require_parameters": True}},
    )
