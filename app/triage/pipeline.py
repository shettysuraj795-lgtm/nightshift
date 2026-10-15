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
import uuid
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.guardrails.abstention import ESCALATION_STEP, abstention_reasons
from app.guardrails.actions import check_steps
from app.guardrails.citations import validate_citations
from app.guardrails.injection import find_injection_lines, injected_identifiers, is_tainted, wrap_untrusted
from app.guardrails.redaction import Redactor
from app.llm.openrouter import LLMResponse, OpenRouterProvider
from app.policy import UNKNOWN_CATEGORY, decide, mode_for_category
from app.registry import Action, FailureMode, load_registry
from app.retrieval.store import RetrievedDoc, Retriever
from app.schemas import Citation, Failure, Step, TriageDraft, TriageResult
from app.signatures import SignatureMatch, match_signatures

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
    """Anything that can answer a chat request with JSON text."""

    name: str

    def generate_json(self, messages: list[dict[str, str]]) -> LLMResponse: ...


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
        self.llm = llm or OpenRouterProvider(self.settings.triage_model, self.settings)
        self.retriever = retriever or Retriever(self.settings)
        self.modes = modes or load_registry()
        self.system_prompt = load_prompt(prompt_name or self.settings.triage_prompt)
        self.redactor = redactor or Redactor.from_file()

    def retrieve(self, log_text: str) -> tuple[list[RetrievedDoc], list[RetrievedDoc]]:
        """Top KB sections and top tickets for the log's error lines."""
        vector = self.retriever.embed_query(build_query(log_text))
        return self.retriever.search_kb(vector), self.retriever.search_tickets(vector)

    def _call(self, messages: list[dict[str, str]], prompt_hook: PromptHook | None) -> LLMResponse:
        if prompt_hook:
            prompt_hook(messages)
        return self.llm.generate_json(messages)

    def generate(self, messages: list[dict[str, str]], prompt_hook: PromptHook | None = None) -> tuple[TriageDraft, LLMResponse]:
        """Call the model; if the JSON is invalid, show it the error once and ask again."""
        response = self._call(messages, prompt_hook)
        try:
            return parse_draft(response.text), response
        except ValidationError as error:
            logger.warning("Invalid model output, attempting one repair (%d errors)", error.error_count())
            repair = messages + [
                {"role": "assistant", "content": response.text},
                {"role": "user", "content": f"Your reply did not match the required JSON format:\n{error}\nReply again with only the corrected JSON object."},
            ]
            response = self._call(repair, prompt_hook)
            return parse_draft(response.text), response

    def finalise(
        self,
        failure_id: str,
        draft: TriageDraft,
        retrieved: list[RetrievedDoc],
        matches: list[SignatureMatch],
        log_text: str,
        flagged_lines: list[str],
        events: list[str],
        mode: str,
        model_used: str,
    ) -> TriageResult:
        """Output guardrails, abstention and policy: turns any draft into a safe result."""
        events = list(events)
        kb_scores = [d.score for d in retrieved if d.collection == "kb"]

        # 1. Remove anything that repeats the injected instruction.
        trusted_text = "\n".join([line for line in log_text.splitlines() if line not in flagged_lines] + [d.text for d in retrieved])
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
        signature_categories = [m.category for m in matches]
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

        # 5. Abstain when the evidence is weak.
        reasons = abstention_reasons(
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

        # 6. Risk and approval come from the registry, never from the model;
        #    the strictest of the chosen category and any signature match.
        decision = decide(category, steps, self.modes, signature_categories)
        if decision.risk != draft.risk:
            events.append(f"policy_override:risk={draft.risk}->{decision.risk}")
        if decision.approval_required != draft.approval_required:
            events.append(f"policy_override:approval_required={str(draft.approval_required).lower()}->{str(decision.approval_required).lower()}")

        return TriageResult(
            failure_id=failure_id,
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
            fallback_used=False,
            guardrail_events=events,
            trace_id=uuid.uuid4().hex,
        )

    def run_log(self, failure_id: str, log_text: str, prompt_hook: PromptHook | None = None) -> TriageResult:
        """Triage a log directly (also used by the evals)."""
        # Redact first: nothing below ever sees the original personal data.
        redaction = self.redactor.redact(log_text)
        log = redaction.text
        events = redaction.events()

        flagged = find_injection_lines(log)
        if flagged:
            events.append(f"injection_detected:lines={len(flagged)}")

        matches = match_signatures(log, self.modes)
        kb_docs, ticket_docs = self.retrieve(log)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": build_user_message(log, matches, kb_docs + ticket_docs, self.modes)},
        ]
        try:
            draft, response = self.generate(messages, prompt_hook)
            mode, model_used = "full", response.model
        except Exception:  # invalid twice, timeout, HTTP error: fall back to rules
            logger.exception("Model answer unavailable for %s, using degraded mode", failure_id)
            draft = degraded_draft(matches, kb_docs, self.modes, self.settings)
            mode, model_used = "degraded", DEGRADED_MODEL_NAME

        return self.finalise(failure_id, draft, kb_docs + ticket_docs, matches, log, flagged, events, mode, model_used)

    def run(self, failure_id: str) -> TriageResult:
        """Triage one failure from last night's queue."""
        return self.run_log(failure_id, read_log(get_failure(failure_id)))


@lru_cache
def get_pipeline() -> TriagePipeline:
    """One shared pipeline for the API, so the embedding model loads once."""
    return TriagePipeline()
