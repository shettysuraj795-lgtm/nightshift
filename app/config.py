"""Application settings, loaded once from environment variables and `.env`.

This is the only place configuration is read. Every model ID, URL, timeout and
threshold lives in `.env` (or YAML files added in later phases), never as a
constant in the code.
"""

from functools import lru_cache

from pydantic import Field
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
    # Retries per provider on timeouts, 429 and 5xx, with exponential backoff and jitter.
    llm_retries: int = Field(default=2, ge=0)
    llm_backoff_initial_seconds: float = 0.5
    llm_backoff_max_seconds: float = 4.0
    llm_temperature: float = 0.0
    llm_max_tokens: int = 2000
    triage_prompt: str = "triage_v1"
    # Degraded mode: a regex match identifies the category but cannot confirm
    # the root cause, so the result reports this fixed, modest confidence.
    degraded_confidence: float = 0.5

    # Abstention: below these, escalate to L2 instead of answering.
    abstain_min_retrieval_score: float = 0.70
    abstain_min_confidence: float = 0.5

    # Local fallback LLM.
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"
    # A small local model is slower than the hosted one, so it gets more time.
    ollama_timeout_seconds: float = 60.0
    # Retrying a local model that timed out rarely helps: it is slow, not flaky.
    ollama_retries: int = Field(default=0, ge=0)

    # Infrastructure.
    qdrant_url: str = "http://localhost:6333"
    qdrant_timeout_seconds: int = 2  # the Qdrant client takes whole seconds
    phoenix_url: str = "http://localhost:6006"
    phoenix_collector_endpoint: str = "http://localhost:6006/v1/traces"  # OTLP over HTTP
    phoenix_project: str = "nightshift"
    tracing_enabled: bool = True

    # Retrieval.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str = ".cache/fastembed"
    retrieval_kb_top_k: int = 5
    retrieval_ticket_top_k: int = 3
    # Hits below this cosine similarity are ignored.
    retrieval_min_score: float = 0.5

    # Chaos flags at start-up (can be changed at runtime via POST /admin/chaos).
    chaos_llm_down: bool = False
    chaos_all_llms_down: bool = False
    chaos_vector_db_down: bool = False

    # Where the UI finds the API.
    api_base_url: str = "http://localhost:8000"


@lru_cache
def get_settings() -> Settings:
    """Return the settings, read once and then reused."""
    return Settings()
