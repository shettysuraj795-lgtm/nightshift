"""Prompt-injection defence, in three layers.

1. The log is wrapped in <untrusted_log> tags and the system prompt says its
   content is data, never instructions.
2. A heuristic detector flags log lines that look like instructions.
3. After the model answers, any output text that repeats a flagged line is
   removed, so injected text never reaches the operator. "Repeats" means any
   of: it looks like an instruction itself, it quotes 4+ consecutive words of
   a flagged line, or it contains an identifier (such as CANARY-7731) that
   occurs only in the flagged lines.
"""

import re

from app.guardrails.rules import Rules, load_rules

SHINGLE_WORDS = 4  # quoting this many consecutive words of a flagged line counts as quoting it
IDENTIFIER = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b")  # e.g. CANARY-7731, INC0041001 does not match


def wrap_untrusted(log_text: str) -> str:
    return f"<untrusted_log>\n{log_text}\n</untrusted_log>"


def find_injection_lines(text: str, rules: Rules | None = None) -> list[str]:
    """Log lines that match any injection pattern."""
    rules = rules or load_rules()
    return [line for line in text.splitlines() if any(p.search(line) for p in rules.injection)]


def injected_identifiers(flagged_lines: list[str], trusted_text: str) -> set[str]:
    """Identifiers that only the flagged lines contain, not the rest of the log or the sources."""
    found = {token for line in flagged_lines for token in IDENTIFIER.findall(line)}
    return {token for token in found if token not in trusted_text}


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"\S+", text.lower())
    return {tuple(words[i : i + SHINGLE_WORDS]) for i in range(len(words) - SHINGLE_WORDS + 1)}


def is_tainted(text: str, flagged_lines: list[str], identifiers: set[str] = frozenset(), rules: Rules | None = None) -> bool:
    """True if the text looks like an instruction or repeats a flagged line."""
    rules = rules or load_rules()
    if any(p.search(text) for p in rules.injection):
        return True
    if any(token in text for token in identifiers):
        return True
    shingles = _shingles(text)
    return any(shingles & _shingles(line) for line in flagged_lines)
