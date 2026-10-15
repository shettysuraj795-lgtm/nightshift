"""Chaos flags: simulate outages on demand for the demo.

Flags start from configuration (CHAOS_* in .env) and can be changed at
runtime with POST /admin/chaos. They live in memory, so restarting the API
resets them to the configured values.
"""

import threading

from pydantic import BaseModel

from app.config import get_settings


class SimulatedOutage(ConnectionError):
    """Raised instead of a real call while a chaos flag is on. Treated like a network failure."""


class ChaosFlags(BaseModel):
    llm_down: bool = False  # OpenRouter fails, so the local Ollama model takes over
    all_llms_down: bool = False  # both providers fail, so the pipeline degrades
    vector_db_down: bool = False  # Qdrant fails, so retrieval is skipped


class ChaosUpdate(BaseModel):
    """Partial update: only the flags that are sent change."""

    llm_down: bool | None = None
    all_llms_down: bool | None = None
    vector_db_down: bool | None = None


_lock = threading.Lock()
_flags: ChaosFlags | None = None


def _current() -> ChaosFlags:
    """The flags, read from configuration on first use. Call with the lock held."""
    global _flags
    if _flags is None:
        s = get_settings()
        _flags = ChaosFlags(llm_down=s.chaos_llm_down, all_llms_down=s.chaos_all_llms_down, vector_db_down=s.chaos_vector_db_down)
    return _flags


def get_flags() -> ChaosFlags:
    with _lock:
        return _current().model_copy()


def update_flags(update: ChaosUpdate) -> ChaosFlags:
    """Read and write under one lock, so two simultaneous updates cannot lose each other."""
    global _flags
    with _lock:
        _flags = _current().model_copy(update=update.model_dump(exclude_none=True))
        return _flags.model_copy()


def provider_is_down(provider_name: str) -> bool:
    flags = get_flags()
    if provider_name == "openrouter":
        return flags.llm_down or flags.all_llms_down
    return flags.all_llms_down


def check_provider(provider_name: str) -> None:
    if provider_is_down(provider_name):
        raise SimulatedOutage(f"chaos: {provider_name} is down")


def check_vector_db() -> None:
    if get_flags().vector_db_down:
        raise SimulatedOutage("chaos: vector database is down")
