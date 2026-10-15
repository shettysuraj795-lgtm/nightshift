"""Triage pipeline.

    redact -> injection_check -> signatures -> retrieve -> llm_generate
           -> validate_output -> output guardrails -> abstention -> apply_policy

The model writes a `TriageDraft`; code validates it and builds the
`TriageResult`. If the draft is still invalid after one repair attempt, or the
model call fails, a degraded draft is built from the registry, the signature
matcher and retrieval. Both kinds of draft go through the same guardrails and
policy, so the safety rules never depend on which path produced the answer.
"""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.guardrails.abstention import ESCALATION_STEP, abstention_reasons
from app.guardrails.actions import check_steps
from app.guardrails.citations import validate_citations
from app.guardrails.injection import find_injection_lines, injected_identifiers, is_tainted, wrap_untrusted
from app.guardrails.redaction import Redactor
from app.llm.client import LLMResponse
from app.llm.fallback import FallbackChain
from app.llm.ollama import ollama_provider
from app.llm.openrouter import openrouter_provider
from app.policy import UNKNOWN_CATEGORY, decide, mode_for_category
from app.registry import Action, FailureMode, load_registry
from app.retrieval.store import RetrievedDoc, Retriever
from app.schemas import Citation, Failure, Step, TriageDraft, TriageResult
from app.signatures import SignatureMatch, match_signatures
from app.telemetry import current_trace_id, tracer

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
FAILURES_PATH = ROOT / "data" / "nightly_failures.json"
PROMPTS_DIR = ROOT / "prompts"

DEGRADED_MODEL_NAME = "signature-matcher"
EXCERPT_MAX_CHARS = 300

# Called with every message list sent to a model. Used by the evals to check
# that no planted personal data leaves the pipeline.
PromptHook = Callable[[list[dict[str, str]]], None]


class LLMProvider(Protocol):
    """Anything that can answer a chat request with JSON text.

    `start_at` names the provider to try first; the repair call uses it so the
    model that wrote the invalid answer is the one asked to correct it.
    """

    def generate_json(self, messages: list[dict[str, str]], start_at: str | None = None) -> LLMResponse: ...


# --- Failures and logs ------------------------------------------------------


def load_failures() -> list[Failure]:
    """Last night's failure queue, in display order (S1-S4 first)."""
    return [Failure.model_validate(item) for item in json.loads(FAILURES_PATH.read_text(encoding="utf-8"))]


def get_failure(failure_id: str) -> Failure:
    """Look up one failure; raises KeyError if it does not exist."""
    for failure in load_failures():
        if failure.id == failure_id:
            return failure
    raise KeyError(failure_id)


def read_log(failure: Failure) -> str:
    return (ROOT / failure.log_file).read_text(encoding="utf-8")


# --- Prompt building --------------------------------------------------------


def build_query(log_text: str) -> str:
    """Retrieval query: the log's ERROR lines without timestamps.

    The final "ended with status CANCELLED" line is the same in every log, so
    it is left out. Falls back to the whole log if there are no other errors.
    """
    errors = [
        line.split(" ", 3)[3]
        for line in log_text.splitlines()
        if " ERROR " in line and "ended with status" not in line and line.count(" ") >= 3
    ]
    return "\n".join(errors) or log_text


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def build_user_message(
    log_text: str,
    matches: list[SignatureMatch],
    sources: list[RetrievedDoc],
    modes: list[FailureMode],
) -> str:
    """Everything the model needs, in clearly labelled sections. The log goes last, wrapped."""
    categories = "\n".join(f"- {m.category}: {m.title}" for m in modes)
    hints = "\n".join(f"- {m.category} ({len(m.matched_lines)} matching log lines)" for m in matches) or "- No known signature matched."
    docs = "\n\n".join(f"### [{d.doc_id}] {d.title} | {d.section}\n{d.text}" for d in sources) or "No sources found."
    return (
        f"## Known categories\n{categories}\n\n"
        f"## Signature matcher hints\n{hints}\n\n"
        f"## Sources\n{docs}\n\n"
        f"## Job log (untrusted data)\n{wrap_untrusted(log_text)}"
    )


# --- Parsing ----------------------------------------------------------------


def parse_draft(text: str) -> TriageDraft:
    """Validate the model's JSON. Strips a ```json fence if the model added one."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return TriageDraft.model_validate_json(cleaned)


# --- Degraded mode ----------------------------------------------------------


def step_action_for(fix_step: str) -> Action:
    """Tag a registry fix step with an action, from its first word.

    Registry fix steps are plain text, so degraded mode needs this to show
    them as typed steps. Raising a change request is an escalation: it asks
    another team to act and changes nothing itself.
    tests/test_pipeline.py checks every registry step.
    """
    verb = fix_step.split()[0].lower()
    if verb in {"rerun", "reprocess"}:
        return "rerun_job"
    if verb in {"check", "confirm", "identify"}:
        return "check_status"
    if verb in {"escalate", "raise", "contact", "ask", "agree"}:
        return "escalate"
    return "info"


def excerpt(text: str, limit: int = EXCERPT_MAX_CHARS) -> str:
    """Start of the text, cut at a word boundary, so it is still a substring of the source."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0]


def degraded_draft(
    matches: list[SignatureMatch],
    kb_docs: list[RetrievedDoc],
    modes: list[FailureMode],
    settings: Settings,
) -> TriageDraft:
    """Rule-based answer: signature category, registry fix steps, top 3 articles."""
    mode = matches[0].mode if matches else mode_for_category(UNKNOWN_CATEGORY, modes)

    top_articles: dict[str, RetrievedDoc] = {}
    for doc in kb_docs:
        top_articles.setdefault(doc.doc_id, doc)

    return TriageDraft(
        category=mode.category,
        root_cause=mode.root_cause,
        confidence=settings.degraded_confidence if matches else 0.0,
        evidence=matches[0].matched_lines if matches else [],
        steps=[Step(text=s, action=step_action_for(s)) for s in mode.fix_steps],
        risk=mode.risk,
        approval_required=mode.approval_required,
        citations=[Citation(doc_id=d.doc_id, title=d.title, excerpt=excerpt(d.text)) for d in list(top_articles.values())[:3]],
        escalate=not matches,
        escalation_reason=None if matches else "No known signature matched and no model answer is available.",
    )


# --- Pipeline ---------------------------------------------------------------


@dataclass
class RunContext:
    """What one triage run knows before the model answers; shared by both answer paths."""

    failure_id: str
    log_text: str  # already redacted
    flagged_lines: list[str]  # suspected injection
    matches: list[SignatureMatch]
    retrieved: list[RetrievedDoc] = field(default_factory=list)
    kb_available: bool = True
    events: list[str] = field(default_factory=list)


# Called with the answer before guardrails and policy, the run's context, and
# the model response (None in degraded mode). Used by the evals to measure what
# the model itself produced, e.g. citation validity before invalid ones are dropped.
DraftHook = Callable[[TriageDraft, RunContext, LLMResponse | None], None]


class TriagePipeline:
    """Runs one triage. Dependencies can be injected, which keeps tests offline."""

    def __init__(
        self,
        llm: LLMProvider | None = None,
        retriever: Retriever | None = None,
        modes: list[FailureMode] | None = None,
        prompt_name: str | None = None,
        settings: Settings | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        # OpenRouter first, the local Ollama model as fallback.
        self.llm = llm or FallbackChain([openrouter_provider(self.settings), ollama_provider(self.settings)], self.settings)
        self.retriever = retriever or Retriever(self.settings)
        self.modes = modes or load_registry()
        self.system_prompt = load_prompt(prompt_name or self.settings.triage_prompt)
        self.redactor = redactor or Redactor.from_file()

    def retrieve(self, log_text: str) -> tuple[list[RetrievedDoc], list[RetrievedDoc]]:
        """Top KB sections and top tickets for the log's error lines."""
        vector = self.retriever.embed_query(build_query(log_text))
        return self.retriever.search_kb(vector), self.retriever.search_tickets(vector)

    def _call(self, messages: list[dict[str, str]], prompt_hook: PromptHook | None, start_at: str | None = None) -> LLMResponse:
        if prompt_hook:
            prompt_hook(messages)
        return self.llm.generate_json(messages, start_at=start_at)

    def _parse(self, response: LLMResponse) -> TriageDraft:
        """Validate one model answer, traced as a `parse_output` span."""
        with tracer.start_as_current_span("parse_output") as span:
            span.set_attributes({"openinference.span.kind": "GUARDRAIL", "llm.provider": response.provider})
            try:
                draft = parse_draft(response.text)
            except ValidationError as error:
                span.set_attributes({"output.valid": False, "output.error_count": error.error_count()})
                raise
            span.set_attribute("output.valid", True)
            return draft

    def generate(self, messages: list[dict[str, str]], prompt_hook: PromptHook | None = None) -> tuple[TriageDraft, LLMResponse]:
        """Call the model; if the JSON is invalid, show it the error once and ask again."""
        response = self._call(messages, prompt_hook)
        try:
            return self._parse(response), response
        except ValidationError as error:
            logger.warning("Invalid model output, attempting one repair (%d errors)", error.error_count())
            trace.get_current_span().add_event("repair_attempted", {"error_count": error.error_count()})
            repair = messages + [
                {"role": "assistant", "content": response.text},
                {"role": "user", "content": f"Your reply did not match the required JSON format:\n{error}\nReply again with only the corrected JSON object."},
            ]
            response = self._call(repair, prompt_hook, start_at=response.provider)
            return self._parse(response), response

    def finalise(self, ctx: RunContext, draft: TriageDraft, mode: str, model_used: str, fallback_used: bool) -> TriageResult:
        """Output guardrails, abstention and policy: turns any draft into a safe result."""
        events = list(ctx.events)
        flagged_lines, retrieved = ctx.flagged_lines, ctx.retrieved
        kb_scores = [d.score for d in retrieved if d.collection == "kb"]

        with tracer.start_as_current_span("validate_output") as span:
            span.set_attribute("openinference.span.kind", "GUARDRAIL")
            events_before = len(events)

            # 1. Remove anything that repeats the injected instruction.
            trusted_text = "\n".join([line for line in ctx.log_text.splitlines() if line not in flagged_lines] + [d.text for d in retrieved])
            identifiers = injected_identifiers(flagged_lines, trusted_text)
            evidence = [e for e in draft.evidence if not is_tainted(e, flagged_lines, identifiers)]
            steps = [s for s in draft.steps if not is_tainted(s.text, flagged_lines, identifiers)]
            removed = len(draft.evidence) - len(evidence) + len(draft.steps) - len(steps)
            if removed:
                events.append(f"injected_text_removed:{removed}")
            root_cause_tainted = is_tainted(draft.root_cause, flagged_lines, identifiers)

            # 2. Category must be a known registry category, and must agree with
            #    the deterministic signature matcher when it found something.
            known_categories = {m.category for m in self.modes}
            category = draft.category if draft.category in known_categories else UNKNOWN_CATEGORY
            if category != draft.category:
                events.append("unknown_category_replaced")
            signature_categories = [m.category for m in ctx.matches]
            agrees = not signature_categories or category in signature_categories
            if not agrees:
                events.append(f"category_disagrees_with_signature:{signature_categories[0]}")

            # 3. Steps must be allowed for this failure mode and never forbidden.
            step_check = check_steps(steps, mode_for_category(category, self.modes))
            steps = step_check.kept
            events += step_check.events

            # 4. Citations must quote a retrieved source.
            citation_check = validate_citations(draft.citations, retrieved)
            if citation_check.dropped:
                events.append(f"citations_dropped:{citation_check.dropped}")

            # 5. Abstain when the evidence is weak. With the knowledge base down,
            #    retrieval and citations cannot be judged, so those checks are skipped.
            reasons = abstention_reasons(
                kb_available=ctx.kb_available,
                top_retrieval_score=max(kb_scores, default=None),
                valid_citations=len(citation_check.valid),
                confidence=draft.confidence,
                model_escalated=draft.escalate,
                known_category=category != UNKNOWN_CATEGORY,
                agrees_with_signatures=agrees,
                steps_left=len(steps),
                min_retrieval_score=self.settings.abstain_min_retrieval_score,
                min_confidence=self.settings.abstain_min_confidence,
            )
            root_cause = draft.root_cause
            if root_cause_tainted:
                reasons.append("the diagnosis repeated a suspicious instruction from the log")
                root_cause = "Withheld: the diagnosis repeated a suspicious instruction found in the log."
            if reasons:
                steps = [ESCALATION_STEP]
                events.append("abstained")
            span.set_attributes({
                "guardrail.events": events[events_before:],
                "guardrail.citations_valid": len(citation_check.valid),
                "guardrail.citations_dropped": citation_check.dropped,
                "guardrail.abstained": bool(reasons),
                "guardrail.abstention_reasons": reasons,
            })

        with tracer.start_as_current_span("apply_policy") as span:
            # 6. Risk and approval come from the registry, never from the model;
            #    the strictest of the chosen category and any signature match.
            decision = decide(category, steps, self.modes, signature_categories)
            if decision.risk != draft.risk:
                events.append(f"policy_override:risk={draft.risk}->{decision.risk}")
            if decision.approval_required != draft.approval_required:
                events.append(f"policy_override:approval_required={str(draft.approval_required).lower()}->{str(decision.approval_required).lower()}")
            span.set_attributes({
                "openinference.span.kind": "GUARDRAIL",
                "policy.model_risk": draft.risk,
                "policy.risk": decision.risk,
                "policy.model_approval_required": draft.approval_required,
                "policy.approval_required": decision.approval_required,
            })

        return TriageResult(
            failure_id=ctx.failure_id,
            category=category,
            root_cause=root_cause,
            confidence=draft.confidence,
            evidence=evidence,
            steps=steps,
            risk=decision.risk,
            approval_required=decision.approval_required,
            citations=citation_check.valid,
            escalate=bool(reasons),
            escalation_reason=("Not enough evidence to recommend a fix: " + "; ".join(reasons) + ". Escalate to L2.") if reasons else None,
            mode=mode,
            model_used=model_used,
            fallback_used=fallback_used,
            guardrail_events=events,
            trace_id=current_trace_id(),
        )

    def run_log(
        self,
        failure_id: str,
        log_text: str,
        prompt_hook: PromptHook | None = None,
        draft_hook: DraftHook | None = None,
    ) -> TriageResult:
        """Triage a log directly (also used by the evals). One call is one trace."""
        with tracer.start_as_current_span("triage") as span:
            span.set_attributes({"openinference.span.kind": "CHAIN", "failure_id": failure_id, "input.value": failure_id})
            result = self._run(failure_id, log_text, prompt_hook, draft_hook)
            span.set_attributes({
                "category": result.category,
                "risk": result.risk,
                "approval_required": result.approval_required,
                "mode": result.mode,
                "model_used": result.model_used,
                "fallback_used": result.fallback_used,
                "guardrail_events": result.guardrail_events,
                "abstained": result.escalate,
                "output.value": result.model_dump_json(),  # built from the redacted log: no personal data
                "output.mime_type": "application/json",
            })
            return result

    def _run(self, failure_id: str, log_text: str, prompt_hook: PromptHook | None, draft_hook: DraftHook | None) -> TriageResult:
        # Redact first: nothing below, including the trace, ever sees the original personal data.
        with tracer.start_as_current_span("redact") as span:
            redaction = self.redactor.redact(log_text)
            span.set_attributes({"openinference.span.kind": "GUARDRAIL",
                                 **{f"pii.{kind.lower()}_count": n for kind, n in redaction.counts.items()}})
        log = redaction.text
        events = redaction.events()

        with tracer.start_as_current_span("injection_check") as span:
            flagged = find_injection_lines(log)
            span.set_attributes({"openinference.span.kind": "GUARDRAIL", "injection.flagged_lines": len(flagged)})
        if flagged:
            events.append(f"injection_detected:lines={len(flagged)}")

        ctx = RunContext(failure_id, log, flagged, match_signatures(log, self.modes), events=events)

        with tracer.start_as_current_span("retrieve") as span:
            span.set_attributes({"openinference.span.kind": "RETRIEVER",
                                 "signature.categories": [m.category for m in ctx.matches]})
            try:
                kb_docs, ticket_docs = self.retrieve(log)
            except Exception as error:  # Qdrant down or unreachable: answer from the signatures alone, quickly
                logger.exception("Knowledge base unavailable for %s", failure_id)
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR, "knowledge base unavailable"))
                ctx.kb_available = False
                ctx.events.append("knowledge_base_unavailable")
            else:
                span.set_attributes({
                    "retrieval.doc_ids": [d.doc_id for d in kb_docs + ticket_docs],
                    "retrieval.scores": [round(d.score, 4) for d in kb_docs + ticket_docs],
                    "retrieval.top_kb_score": max((d.score for d in kb_docs), default=0.0),
                })
        if not ctx.kb_available:
            draft = degraded_draft(ctx.matches, [], self.modes, self.settings)
            if draft_hook:
                draft_hook(draft, ctx, None)
            return self.finalise(ctx, draft, "degraded", DEGRADED_MODEL_NAME, fallback_used=False)
        ctx.retrieved = kb_docs + ticket_docs

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": build_user_message(log, ctx.matches, ctx.retrieved, self.modes)},
        ]
        try:
            draft, response = self.generate(messages, prompt_hook)
        except Exception as error:  # all providers failed, or invalid output twice: fall back to rules
            logger.exception("Model answer unavailable for %s, using degraded mode", failure_id)
            reason = "invalid_output_after_repair" if isinstance(error, ValidationError) else "llm_unavailable"
            ctx.events.append(reason)
            draft = degraded_draft(ctx.matches, kb_docs, self.modes, self.settings)
            if draft_hook:
                draft_hook(draft, ctx, None)
            return self.finalise(ctx, draft, "degraded", DEGRADED_MODEL_NAME, fallback_used=False)

        if draft_hook:
            draft_hook(draft, ctx, response)
        return self.finalise(ctx, draft, "full", response.model, response.fallback_used)

    def run(self, failure_id: str) -> TriageResult:
        """Triage one failure from last night's queue."""
        return self.run_log(failure_id, read_log(get_failure(failure_id)))


@lru_cache
def get_pipeline() -> TriagePipeline:
    """One shared pipeline for the API, so the embedding model loads once."""
    return TriagePipeline()
