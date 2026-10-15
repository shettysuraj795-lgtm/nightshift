"""Application settings, loaded once from environment variables and `.env`.

This is the only place configuration is read. Every model ID, URL, timeout and
threshold lives in `.env` (or YAML files added in later phases), never as a
constant in the code.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed view of the variables documented in `.env.example`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM provider. OpenRouter is the demo harness only; a real deployment
    # points the same OpenAI-compatible client at an EU- or customer-hosted
    # endpoint by changing these values.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    triage_model: str = ""
    enrich_model: str = ""
    judge_model: str = ""
    llm_timeout_seconds: float = 30.0

    # Local fallback LLM.
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"

    # Infrastructure.
    qdrant_url: str = "http://localhost:6333"
    phoenix_url: str = "http://localhost:6006"
    phoenix_collector_endpoint: str = "http://localhost:4317"

    # Where the UI finds the API.
    api_base_url: str = "http://localhost:8000"


@lru_cache
def get_settings() -> Settings:
    """Return the settings, read once and then reused."""
    return Settings()
