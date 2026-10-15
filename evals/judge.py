"""Faithfulness judge: is a diagnosis supported by the excerpts it cites?

A second model, from a different family than the triage model (JUDGE_MODEL),
reads the root cause, the recommended steps, the cited excerpts and the log
lines quoted as evidence, and says whether every claim is supported. Using a
different family reduces the chance that the judge shares the triage model's
blind spots. The judge is a simple, uncalibrated check: good for catching
regressions, not a precise quality score.
"""

import json

from pydantic import BaseModel

from app.config import Settings
from app.llm.client import OpenAICompatibleProvider
from app.llm.fallback import FallbackChain
from app.schemas import TriageDraft

JUDGE_PROMPT = """You check whether an incident diagnosis is supported by its sources.

You receive a root cause, recommended steps, excerpts from runbooks and past tickets that the diagnosis cites, and log lines quoted as evidence.

A claim is supported if a cited excerpt or an evidence line states it or directly implies it. Specific names, IDs and times may come from the evidence lines. General operator advice (checking a status, escalating, obtaining approval) counts as supported when a cited runbook covers the same procedure.

Reply with one JSON object and nothing else:
{"supported": true or false, "unsupported_claims": ["claim that no excerpt or evidence line supports"], "reason": "one sentence"}
"""


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
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def judge(self, draft: TriageDraft, excerpts: list[str]) -> Verdict:
        """Judge one diagnosis against the excerpts that passed citation validation."""
        case = {
            "root_cause": draft.root_cause,
            "steps": [step.text for step in draft.steps],
            "cited_excerpts": excerpts,
            "evidence_lines": draft.evidence,
        }
        try:
            response = self.chain.generate_json([
                {"role": "system", "content": JUDGE_PROMPT},
                {"role": "user", "content": json.dumps(case, ensure_ascii=False, indent=2)},
            ])
            self.prompt_tokens += response.prompt_tokens or 0
            self.completion_tokens += response.completion_tokens or 0
            return Verdict.model_validate_json(response.text)
        except Exception as error:  # one failed judgement must not stop the whole eval run
            return Verdict(supported=False, reason="judge failed", error=f"{type(error).__name__}: {error}"[:200])
