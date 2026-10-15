"""Deterministic signature matcher: log text -> candidate failure modes. No LLM.

Used twice: as a hint in the triage prompt, and as the classifier in degraded
mode when no model is available.
"""

import re
from dataclasses import dataclass

from app.registry import FailureMode


@dataclass(frozen=True)
class SignatureMatch:
    """A failure mode whose signatures appear in the log."""

    mode: FailureMode
    matched_lines: list[str]

    @property
    def category(self) -> str:
        return self.mode.category


def match_signatures(log_text: str, modes: list[FailureMode]) -> list[SignatureMatch]:
    """Return matching modes, the one with the most matching lines first.

    Ties keep registry order, so the result is stable.
    """
    lines = log_text.splitlines()
    matches = []
    for mode in modes:
        patterns = [re.compile(signature) for signature in mode.signatures]
        hits = [line for line in lines if any(p.search(line) for p in patterns)]
        if hits:
            matches.append(SignatureMatch(mode=mode, matched_lines=hits))
    return sorted(matches, key=lambda m: len(m.matched_lines), reverse=True)
