"""Tracing: OpenTelemetry spans, exported to Arize Phoenix over OTLP/HTTP.

Each triage is one trace:

    triage (root)
    ├── redact            PII counts by type, never values
    ├── injection_check   number of suspicious lines
    ├── retrieve          document IDs and scores
    ├── llm_generate      one span per attempt: provider, model, latency, tokens, retry
    ├── parse_output      one per model answer: valid or not
    ├── validate_output   output guardrails and abstention
    └── apply_policy      risk and approval decision

No operator identity in traces or logs (works-council requirement): nothing
here records who asked for a triage. FastAPI is deliberately not
auto-instrumented, because that would add the caller's IP address, and the
API runs without access logs for the same reason (see Makefile). Logs are
redacted before any span sees them, so spans contain no personal data either.

Span attributes follow OpenInference conventions (`openinference.span.kind`,
`llm.model_name`, `llm.token_count.*`) so Phoenix displays them natively.
"""

import json
import uuid

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import Settings, get_settings

tracer = trace.get_tracer("nightshift")
_configured = False


def setup_tracing(settings: Settings | None = None) -> None:
    """Send spans to Phoenix. Safe to call more than once; does nothing if tracing is off."""
    global _configured
    settings = settings or get_settings()
    if _configured or not settings.tracing_enabled:
        return
    provider = TracerProvider(resource=Resource.create({
        "service.name": "nightshift",
        "openinference.project.name": settings.phoenix_project,  # the Phoenix project to file traces under
    }))
    # Batching sends spans in the background, so a Phoenix outage never slows a triage down.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.phoenix_collector_endpoint)))
    trace.set_tracer_provider(provider)
    _configured = True


def flush() -> None:
    """Send any buffered spans now (scripts call this before exiting)."""
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.force_flush()


def current_trace_id() -> str:
    """The active trace ID as 32 hex characters.

    With tracing off there is no trace, so a random ID is returned to keep the
    field filled; a Phoenix link built from it will not find anything.
    """
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else uuid.uuid4().hex


def phoenix_trace_url(trace_id: str, settings: Settings | None = None) -> str:
    """Link that opens this trace in the Phoenix UI."""
    settings = settings or get_settings()
    return f"{settings.phoenix_url.rstrip('/')}/redirects/traces/{trace_id}"


def as_json(value: object) -> str:
    """Span attributes must be primitives; structured values are stored as JSON text."""
    return json.dumps(value, ensure_ascii=False, default=str)
