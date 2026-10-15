"""Trace structure, captured in memory instead of being sent to Phoenix."""

import json
from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.registry import load_registry
from app.telemetry import phoenix_trace_url
from app.triage.pipeline import TriagePipeline
from tests.test_fallback import SETTINGS, TIMEOUT, FakeProvider, make_chain
from tests.test_pipeline import LOCK_LOG, PLANTED, S3_LOG, VALID_DRAFT, FakeLLM, FakeRetriever

EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def tracing() -> None:
    """Capture spans in memory. OpenTelemetry allows one global provider per process,
    so reuse it if another test already installed one."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(EXPORTER))


@pytest.fixture(autouse=True)
def clear_spans() -> Iterator[None]:
    EXPORTER.clear()
    yield


def run(llm: object, log: str = LOCK_LOG, retriever: object = None):
    pipeline = TriagePipeline(llm=llm, retriever=retriever or FakeRetriever(), modes=load_registry(), settings=SETTINGS)
    return pipeline.run_log("S2", log)


def spans_by_name() -> dict[str, list[ReadableSpan]]:
    result: dict[str, list[ReadableSpan]] = {}
    for span in EXPORTER.get_finished_spans():
        result.setdefault(span.name, []).append(span)
    return result


def test_one_trace_with_the_planned_span_tree() -> None:
    result = run(FakeLLM(json.dumps(VALID_DRAFT)))
    spans = EXPORTER.get_finished_spans()
    [root] = [s for s in spans if s.parent is None]

    assert root.name == "triage"
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}
    assert result.trace_id == format(root.context.trace_id, "032x")
    children = [s.name for s in sorted(spans, key=lambda s: s.start_time) if s.parent and s.parent.span_id == root.context.span_id]
    assert children == ["redact", "injection_check", "retrieve", "parse_output", "validate_output", "apply_policy"]


def test_root_span_carries_the_outcome() -> None:
    run(FakeLLM(json.dumps(VALID_DRAFT | {"risk": "low"})))
    attributes = spans_by_name()["triage"][0].attributes
    assert attributes["failure_id"] == "S2"
    assert attributes["category"] == "sap_lock_conflict"
    assert attributes["risk"] == "high" and attributes["approval_required"] is True
    assert attributes["mode"] == "full" and attributes["fallback_used"] is False and attributes["abstained"] is False
    assert "policy_override:risk=low->high" in attributes["guardrail_events"]


def test_every_llm_attempt_is_a_span_including_fallback() -> None:
    chain, _ = make_chain(FakeProvider("openrouter", TIMEOUT, TIMEOUT, TIMEOUT), FakeProvider("ollama", json.dumps(VALID_DRAFT)))
    run(chain)
    attempts = sorted(spans_by_name()["llm_generate"], key=lambda s: s.start_time)

    assert [(s.attributes["llm.provider"], s.attributes["llm.attempt"], s.attributes["llm.outcome"]) for s in attempts] == [
        ("openrouter", 1, "APITimeoutError"), ("openrouter", 2, "APITimeoutError"),
        ("openrouter", 3, "APITimeoutError"), ("ollama", 1, "ok"),
    ]
    assert [s.status.is_ok for s in attempts] == [False, False, False, True]
    assert attempts[1].attributes["llm.is_retry"] and attempts[3].attributes["llm.is_fallback"]
    assert attempts[3].attributes["llm.token_count.total"] == 2
    assert spans_by_name()["triage"][0].attributes["fallback_used"] is True


def test_all_providers_failing_still_leaves_every_attempt_in_the_trace() -> None:
    chain, _ = make_chain(FakeProvider("openrouter", *[TIMEOUT] * 3), FakeProvider("ollama", TIMEOUT, retries=0))
    result = run(chain)
    assert result.mode == "degraded"
    assert len(spans_by_name()["llm_generate"]) == 4


def test_knowledge_base_outage_marks_the_retrieve_span() -> None:
    class Down(FakeRetriever):
        def search_kb(self, vector: list[float]) -> list:
            raise ConnectionError("qdrant unreachable")

    run(FakeLLM(), retriever=Down())
    [retrieve] = spans_by_name()["retrieve"]
    assert not retrieve.status.is_ok
    assert "llm_generate" not in spans_by_name()


def test_no_personal_data_in_any_span() -> None:
    run(FakeLLM(json.dumps(VALID_DRAFT)), log=S3_LOG)
    everything = json.dumps([dict(s.attributes) for s in EXPORTER.get_finished_spans()], default=str)
    assert not any(value in everything for value in PLANTED)
    redact = spans_by_name()["redact"][0].attributes
    assert redact["pii.email_count"] == 1 and redact["pii.person_count"] == 1


def test_phoenix_link() -> None:
    assert phoenix_trace_url("abc", SETTINGS) == "http://localhost:6006/redirects/traces/abc"
