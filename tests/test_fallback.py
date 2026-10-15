"""Fallback chain, retries, chaos flags and degraded modes, with mocked providers."""

import json
from collections.abc import Iterator

import httpx2
import openai
import pytest

from app.chaos import ChaosFlags, ChaosUpdate, SimulatedOutage, update_flags
from app.config import Settings
from app.llm.client import LLMResponse
from app.llm.fallback import AllProvidersFailed, FallbackChain, is_transient
from app.registry import load_registry
from app.triage.pipeline import TriagePipeline
from tests.test_pipeline import LOCK_LOG, VALID_DRAFT, FakeRetriever

SETTINGS = Settings(_env_file=None, triage_model="test", llm_retries=2, llm_backoff_initial_seconds=0.5, llm_backoff_max_seconds=4)
REQUEST = httpx2.Request("POST", "https://example.invalid/v1/chat/completions")


def http_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    return cls("error", response=httpx2.Response(status, request=REQUEST), body=None)


TIMEOUT = openai.APITimeoutError(request=REQUEST)
RATE_LIMITED = http_error(openai.RateLimitError, 429)
SERVER_ERROR = http_error(openai.InternalServerError, 500)
BAD_REQUEST = http_error(openai.BadRequestError, 400)


class FakeProvider:
    """Plays back a script of replies: a string is a successful answer, an exception is raised."""

    def __init__(self, name: str, *script: str | Exception, retries: int = 2) -> None:
        self.name = name
        self.model = f"{name}/model"
        self.retries = retries
        self.script = list(script)
        self.calls = 0

    def generate_json(self, messages: list[dict[str, str]]) -> LLMResponse:
        self.calls += 1
        reply = self.script.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return LLMResponse(text=reply, model=self.model, provider=self.name, latency_ms=5, prompt_tokens=1, completion_tokens=1)


@pytest.fixture(autouse=True)
def reset_chaos() -> Iterator[None]:
    update_flags(ChaosUpdate(**ChaosFlags().model_dump()))
    yield
    update_flags(ChaosUpdate(**ChaosFlags().model_dump()))


def make_chain(*providers: FakeProvider) -> tuple[FallbackChain, list[float]]:
    waits: list[float] = []
    return FallbackChain(list(providers), SETTINGS, sleep=waits.append), waits


# --- Error classification --------------------------------------------------


@pytest.mark.parametrize("error, transient", [
    (TIMEOUT, True), (RATE_LIMITED, True), (SERVER_ERROR, True),
    (SimulatedOutage("x"), True), (BAD_REQUEST, False), (RuntimeError("no key"), False),
])
def test_only_transient_errors_are_retried(error: Exception, transient: bool) -> None:
    assert is_transient(error) == transient


# --- Chain behaviour -------------------------------------------------------


def test_primary_success_is_not_a_fallback() -> None:
    chain, _ = make_chain(FakeProvider("openrouter", "{}"), FakeProvider("ollama", "{}"))
    response = chain.generate_json([])
    assert response.provider == "openrouter" and not response.fallback_used
    assert [(a.provider, a.outcome) for a in response.attempts] == [("openrouter", "ok")]


def test_transient_error_is_retried_on_the_same_provider() -> None:
    primary = FakeProvider("openrouter", RATE_LIMITED, "{}")
    chain, waits = make_chain(primary, FakeProvider("ollama", "{}"))
    response = chain.generate_json([])
    assert response.provider == "openrouter" and not response.fallback_used
    assert primary.calls == 2 and len(waits) == 1
    assert 0.5 <= waits[0] <= 0.5 + 1  # initial backoff plus up to 1 s jitter


def test_retries_exhausted_then_fallback_tells_the_truth() -> None:
    primary = FakeProvider("openrouter", TIMEOUT, SERVER_ERROR, TIMEOUT)
    backup = FakeProvider("ollama", "{}")
    chain, waits = make_chain(primary, backup)
    response = chain.generate_json([])
    assert response.provider == "ollama" and response.model == "ollama/model"
    assert response.fallback_used
    assert primary.calls == 3 and len(waits) == 2  # 1 try + 2 retries
    assert [a.outcome for a in response.attempts] == ["APITimeoutError", "InternalServerError", "APITimeoutError", "ok"]


def test_permanent_error_skips_retries() -> None:
    primary = FakeProvider("openrouter", BAD_REQUEST)
    chain, waits = make_chain(primary, FakeProvider("ollama", "{}"))
    assert chain.generate_json([]).fallback_used
    assert primary.calls == 1 and waits == []


def test_all_providers_failing_raises_with_every_attempt() -> None:
    chain, _ = make_chain(FakeProvider("openrouter", *[TIMEOUT] * 3), FakeProvider("ollama", *[TIMEOUT] * 3))
    with pytest.raises(AllProvidersFailed) as failure:
        chain.generate_json([])
    assert len(failure.value.attempts) == 6


def test_chaos_llm_down_forces_the_fallback() -> None:
    update_flags(ChaosUpdate(llm_down=True))
    primary = FakeProvider("openrouter", "{}")
    chain, _ = make_chain(primary, FakeProvider("ollama", "{}"))
    response = chain.generate_json([])
    assert response.provider == "ollama" and response.fallback_used
    assert primary.calls == 0  # the simulated outage happens before the real call
    assert [a.outcome for a in response.attempts][:3] == ["SimulatedOutage"] * 3


def test_chaos_all_llms_down_fails_the_chain() -> None:
    update_flags(ChaosUpdate(all_llms_down=True))
    chain, _ = make_chain(FakeProvider("openrouter", "{}"), FakeProvider("ollama", "{}"))
    with pytest.raises(AllProvidersFailed):
        chain.generate_json([])


def test_chaos_update_is_partial() -> None:
    update_flags(ChaosUpdate(llm_down=True))
    flags = update_flags(ChaosUpdate(vector_db_down=True))
    assert flags == ChaosFlags(llm_down=True, vector_db_down=True)


# --- Pipeline with the chain -----------------------------------------------


def pipeline_with(chain: FallbackChain, retriever: object = None) -> TriagePipeline:
    return TriagePipeline(llm=chain, retriever=retriever or FakeRetriever(), modes=load_registry(), settings=SETTINGS)


def test_pipeline_reports_the_fallback_model() -> None:
    chain, _ = make_chain(FakeProvider("openrouter", *[TIMEOUT] * 3), FakeProvider("ollama", json.dumps(VALID_DRAFT)))
    result = pipeline_with(chain).run_log("S2", LOCK_LOG)
    assert result.mode == "full"
    assert result.model_used == "ollama/model" and result.fallback_used


def test_pipeline_degrades_when_every_model_fails() -> None:
    chain, _ = make_chain(FakeProvider("openrouter", *[TIMEOUT] * 3), FakeProvider("ollama", *[TIMEOUT] * 3))
    result = pipeline_with(chain).run_log("S2", LOCK_LOG)
    assert result.mode == "degraded" and result.model_used == "signature-matcher"
    assert not result.fallback_used
    assert result.category == "sap_lock_conflict" and result.risk == "high" and result.approval_required


class DownRetriever(FakeRetriever):
    def search_kb(self, vector: list[float]) -> list:
        raise ConnectionError("qdrant unreachable")


def test_knowledge_base_down_returns_signature_result_without_calling_a_model() -> None:
    primary = FakeProvider("openrouter", json.dumps(VALID_DRAFT))
    chain, _ = make_chain(primary, FakeProvider("ollama"))
    result = pipeline_with(chain, DownRetriever()).run_log("S2", LOCK_LOG)
    assert primary.calls == 0
    assert result.mode == "degraded" and "knowledge_base_unavailable" in result.guardrail_events
    assert result.category == "sap_lock_conflict" and not result.escalate
    assert result.citations == []
    assert [s.text for s in result.steps][0] == "Confirm the lock holder job has completed"


def test_knowledge_base_down_and_no_signature_still_escalates() -> None:
    chain, _ = make_chain(FakeProvider("openrouter"), FakeProvider("ollama"))
    result = pipeline_with(chain, DownRetriever()).run_log("S4", "2026-10-14 04:00:00 ERROR Something new\n")
    assert result.escalate and result.category == "unknown_novel"


# --- Phase 4 review fixes --------------------------------------------------


def test_provider_without_retries_is_tried_once() -> None:
    backup = FakeProvider("ollama", TIMEOUT, "{}", retries=0)
    chain, waits = make_chain(FakeProvider("openrouter", BAD_REQUEST), backup)
    with pytest.raises(AllProvidersFailed):
        chain.generate_json([])
    assert backup.calls == 1 and waits == []


def test_start_at_skips_earlier_providers() -> None:
    primary = FakeProvider("openrouter", "{}")
    chain, _ = make_chain(primary, FakeProvider("ollama", "{}"))
    response = chain.generate_json([], start_at="ollama")
    assert primary.calls == 0 and response.provider == "ollama" and response.fallback_used


def test_repair_goes_to_the_model_that_wrote_the_invalid_answer() -> None:
    primary = FakeProvider("openrouter", *[TIMEOUT] * 3)
    backup = FakeProvider("ollama", "not json", json.dumps(VALID_DRAFT))
    chain, _ = make_chain(primary, backup)
    result = pipeline_with(chain).run_log("S2", LOCK_LOG)
    assert primary.calls == 3  # not tried again for the repair
    assert backup.calls == 2 and result.model_used == "ollama/model" and result.fallback_used


def test_attempts_are_numbered_per_provider() -> None:
    chain, _ = make_chain(FakeProvider("openrouter", TIMEOUT, TIMEOUT, TIMEOUT), FakeProvider("ollama", TIMEOUT, "{}"))
    attempts = chain.generate_json([]).attempts
    assert [(a.provider, a.attempt) for a in attempts] == [("openrouter", 1), ("openrouter", 2), ("openrouter", 3), ("ollama", 1), ("ollama", 2)]


def test_degraded_results_say_why() -> None:
    down, _ = make_chain(FakeProvider("openrouter", *[TIMEOUT] * 3), FakeProvider("ollama", *[TIMEOUT] * 3))
    assert "llm_unavailable" in pipeline_with(down).run_log("S2", LOCK_LOG).guardrail_events
    invalid, _ = make_chain(FakeProvider("openrouter", "bad", "still bad"), FakeProvider("ollama"))
    assert "invalid_output_after_repair" in pipeline_with(invalid).run_log("S2", LOCK_LOG).guardrail_events
