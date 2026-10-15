"""Faithfulness judge: is a diagnosis supported by the excerpts it cites?

A second model, from a different family than the triage model (JUDGE_MODEL),
reads the root cause, the recommended steps, the cited excerpts and the log
lines quoted as evidence, and says whether every claim is supported. Using a
different family reduces the chance that the judge shares the triage model's
blind spots. The judge is a simple, uncalibrated check: good for catching
regressions, not a precise quality score. Each eval run starts with a control
case the judge must reject (`control_rejected`), so a judge that says "yes" to
everything is caught.
"""

import json
import threading
from collections.abc import Callable

from pydantic import BaseModel

from app.config import Settings
from app.llm.client import OpenAICompatibleProvider
from app.llm.fallback import FallbackChain
from app.schemas import Step, TriageDraft

JUDGE_PROMPT = """You check whether an incident diagnosis is supported by its sources.

You receive a root cause, recommended steps, excerpts from runbooks and past tickets that the diagnosis cites, and log lines quoted as evidence.

A claim is supported if a cited excerpt or an evidence line states it or directly implies it. Specific names, IDs and times may come from the evidence lines. General operator advice (checking a status, escalating, obtaining approval) counts as supported when a cited runbook covers the same procedure.

Reply with one JSON object and nothing else:
{"supported": true or false, "unsupported_claims": ["claim that no excerpt or evidence line supports"], "reason": "one sentence"}
"""

# A diagnosis whose root cause and steps contradict the excerpt: the judge must say "no".
CONTROL_DRAFT = TriageDraft(
    category="sftp_file_missing",
    root_cause="The SFTP server's disk is full because of a firmware bug in the storage array.",
    confidence=0.9,
    evidence=["2026-10-14 00:47:14 ERROR SFTP: no matching file for pattern ASN_ZAJAC_20261014*.csv after 3 polls"],
    steps=[Step(text="Replace the storage array firmware and reboot the SFTP server", action="config_change")],
    risk="low",
    approval_required=False,
    citations=[],
    escalate=False,
)
CONTROL_EXCERPTS = ["The usual causes are a late upload, a file name that does not match the pattern, or an upload to the wrong folder."]


class Verdict(BaseModel):
    supported: bool
    unsupported_claims: list[str] = []
    reason: str = ""
    error: str | None = None  # set when the judge itself failed; the case then counts as unsupported


class FaithfulnessJudge:
    def __init__(self, settings: Settings) -> None:
        if not settings.judge_model:
            raise RuntimeError("JUDGE_MODEL must be set in .env to run the faithfulness judge")
        provider = OpenAICompatibleProvider(
            name="openrouter-judge",
            base_url=settings.openrouter_base_url,
            api_key=settings.openrouter_api_key,
            model=settings.judge_model,
            timeout_seconds=settings.llm_timeout_seconds,
            retries=settings.llm_retries,
            temperature=0.0,
            # Reasoning models spend tokens thinking before answering, so allow more room when it is on.
            max_tokens=4000 if settings.judge_reasoning else 500,
            extra_body={"provider": {"require_parameters": True}, "reasoning": {"enabled": settings.judge_reasoning}},
        )
        self.model = settings.judge_model
        self.chain = FallbackChain([provider], settings)  # one provider, but the same retry handling
        self._lock = threading.Lock()  # cases are judged from several threads
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def judge(
        self,
        draft: TriageDraft,
        excerpts: list[str],
        prompt_hook: Callable[[list[dict[str, str]]], None] | None = None,
    ) -> Verdict:
        """Judge one diagnosis against the excerpts that passed citation validation."""
        case = {
            "root_cause": draft.root_cause,
            "steps": [step.text for step in draft.steps],
            "cited_excerpts": excerpts,
            "evidence_lines": draft.evidence,
        }
        messages = [
            {"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": json.dumps(case, ensure_ascii=False, indent=2)},
        ]
        if prompt_hook:
            prompt_hook(messages)  # the judge is a third-party model too: the PII check covers it
        try:
            response = self.chain.generate_json(messages)
            with self._lock:
                self.prompt_tokens += response.prompt_tokens or 0
                self.completion_tokens += response.completion_tokens or 0
            return Verdict.model_validate_json(response.text)
        except Exception as error:  # one failed judgement must not stop the whole eval run
            return Verdict(supported=False, reason="judge failed", error=f"{type(error).__name__}: {error}"[:200])

    def control_rejected(self) -> bool:
        """Sanity check: the judge must reject a diagnosis its excerpt contradicts."""
        verdict = self.judge(CONTROL_DRAFT, CONTROL_EXCERPTS)
        return verdict.error is None and not verdict.supported
