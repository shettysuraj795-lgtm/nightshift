"""API contracts. Frozen once Phase 2 is committed: the UI builds against them.

`TriageResult` is what the API returns. `TriageDraft` is the part the model is
asked to write; the pipeline validates it and adds the fields only code may set
(mode, model_used, fallback_used, guardrail_events, trace_id).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.registry import Action, Risk


class Step(BaseModel):
    """One recommended step, tagged with what kind of action it is."""

    text: str = Field(min_length=1)
    action: Action


class Citation(BaseModel):
    """A source the diagnosis relies on: a KB article or a past ticket."""

    doc_id: str = Field(min_length=1)
    title: str
    excerpt: str = Field(min_length=1)


class TriageDraft(BaseModel):
    """The model's answer, before guardrails and policy are applied."""

    model_config = ConfigDict(extra="forbid")

    category: str
    root_cause: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str]
    steps: list[Step]
    risk: Risk
    approval_required: bool
    citations: list[Citation]
    escalate: bool
    escalation_reason: str | None = None


class TriageResult(BaseModel):
    """Full triage answer returned by `POST /triage/{failure_id}`."""

    failure_id: str
    category: str
    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str]
    steps: list[Step]
    risk: Risk
    approval_required: bool
    citations: list[Citation]
    escalate: bool
    escalation_reason: str | None = None
    mode: Literal["full", "degraded"]
    model_used: str
    fallback_used: bool
    guardrail_events: list[str]
    trace_id: str


class Failure(BaseModel):
    """One entry in last night's failure queue (`data/nightly_failures.json`)."""

    id: str
    job: str
    plant: str
    system: str
    failed_at: str
    log_file: str
    pinned: bool


class FailureDetail(Failure):
    """A failure plus its raw log, for `GET /failures/{id}`."""

    log_text: str
