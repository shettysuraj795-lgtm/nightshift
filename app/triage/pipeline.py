"""Triage pipeline: load log -> signature hints -> retrieve -> LLM -> parse.

The model writes a `TriageDraft`; code validates it and builds the
`TriageResult`. If the draft is still invalid after one repair attempt, or
the model call fails, the pipeline returns a degraded result built only from
the registry, the signature matcher and retrieval.
"""

import json
import logging
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.llm.openrouter import LLMResponse, OpenRouterProvider
from app.registry import Action, FailureMode, load_registry
from app.retrieval.store import RetrievedDoc, Retriever
from app.schemas import Citation, Failure, Step, TriageDraft, TriageResult
from app.signatures import SignatureMatch, match_signatures

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
FAILURES_PATH = ROOT / "data" / "nightly_failures.json"
PROMPTS_DIR = ROOT / "prompts"

# The registry's catch-all mode (FM-008): used when nothing else fits.
UNKNOWN_CATEGORY = "unknown_novel"
DEGRADED_MODEL_NAME = "signature-matcher"
EXCERPT_MAX_CHARS = 300


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
    """Everything the model needs, in clearly labelled sections."""
    categories = "\n".join(f"- {m.category}: {m.title}" for m in modes)
    hints = "\n".join(f"- {m.category} ({len(m.matched_lines)} matching log lines)" for m in matches) or "- No known signature matched."
    docs = "\n\n".join(f"### [{d.doc_id}] {d.title} | {d.section}\n{d.text}" for d in sources) or "No sources found."
    return (
        f"## Known categories\n{categories}\n\n"
        f"## Signature matcher hints\n{hints}\n\n"
        f"## Sources\n{docs}\n\n"
        f"## Job log\n{log_text}"
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
    them as typed steps. tests/test_pipeline.py checks every registry step.
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


def degraded_result(
    failure_id: str,
    matches: list[SignatureMatch],
    kb_docs: list[RetrievedDoc],
    modes: list[FailureMode],
    settings: Settings,
) -> TriageResult:
    """Rule-based answer: signature category, registry risk and fix steps, top 3 articles."""
    by_category = {m.category: m for m in modes}
    mode = matches[0].mode if matches else by_category[UNKNOWN_CATEGORY]

    top_articles: dict[str, RetrievedDoc] = {}
    for doc in kb_docs:
        top_articles.setdefault(doc.doc_id, doc)
    citations = [Citation(doc_id=d.doc_id, title=d.title, excerpt=excerpt(d.text)) for d in list(top_articles.values())[:3]]

    return TriageResult(
        failure_id=failure_id,
        category=mode.category,
        root_cause=mode.root_cause,
        confidence=settings.degraded_confidence if matches else 0.0,
        evidence=matches[0].matched_lines if matches else [],
        steps=[Step(text=s, action=step_action_for(s)) for s in mode.fix_steps],
        risk=mode.risk,
        approval_required=mode.approval_required,
        citations=citations,
        escalate=not matches,
        escalation_reason=None if matches else "No known signature matched and no model answer is available.",
        mode="degraded",
        model_used=DEGRADED_MODEL_NAME,
        fallback_used=False,
        guardrail_events=[],
        trace_id=uuid.uuid4().hex,
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
    ) -> None:
        self.settings = settings or get_settings()
        self.llm = llm or OpenRouterProvider(self.settings.triage_model, self.settings)
        self.retriever = retriever or Retriever(self.settings)
        self.modes = modes or load_registry()
        self.system_prompt = load_prompt(prompt_name or self.settings.triage_prompt)

    def retrieve(self, log_text: str) -> tuple[list[RetrievedDoc], list[RetrievedDoc]]:
        """Top KB sections and top tickets for the log's error lines."""
        vector = self.retriever.embed_query(build_query(log_text))
        return self.retriever.search_kb(vector), self.retriever.search_tickets(vector)

    def generate(self, messages: list[dict[str, str]]) -> tuple[TriageDraft, LLMResponse]:
        """Call the model; if the JSON is invalid, show it the error once and ask again."""
        response = self.llm.generate_json(messages)
        try:
            return parse_draft(response.text), response
        except ValidationError as error:
            logger.warning("Invalid model output, attempting one repair: %s", error.error_count())
            repair = messages + [
                {"role": "assistant", "content": response.text},
                {"role": "user", "content": f"Your reply did not match the required JSON format:\n{error}\nReply again with only the corrected JSON object."},
            ]
            response = self.llm.generate_json(repair)
            return parse_draft(response.text), response

    def run_log(self, failure_id: str, log_text: str) -> TriageResult:
        """Triage a log directly (also used by the evals)."""
        matches = match_signatures(log_text, self.modes)
        kb_docs, ticket_docs = self.retrieve(log_text)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": build_user_message(log_text, matches, kb_docs + ticket_docs, self.modes)},
        ]
        try:
            draft, response = self.generate(messages)
        except Exception:  # invalid twice, timeout, HTTP error: fall back to rules
            logger.exception("Model answer unavailable for %s, returning degraded result", failure_id)
            return degraded_result(failure_id, matches, kb_docs, self.modes, self.settings)

        return TriageResult(
            failure_id=failure_id,
            **draft.model_dump(),
            mode="full",
            model_used=response.model,
            fallback_used=False,
            guardrail_events=[],
            trace_id=uuid.uuid4().hex,
        )

    def run(self, failure_id: str) -> TriageResult:
        """Triage one failure from last night's queue."""
        return self.run_log(failure_id, read_log(get_failure(failure_id)))


@lru_cache
def get_pipeline() -> TriagePipeline:
    """One shared pipeline for the API, so the embedding model loads once."""
    return TriagePipeline()
