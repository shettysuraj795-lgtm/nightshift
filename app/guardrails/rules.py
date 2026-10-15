"""Loads `rules.yaml` once and compiles its patterns."""

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

RULES_PATH = Path(__file__).resolve().parent / "rules.yaml"


@dataclass(frozen=True)
class Rules:
    injection: list[re.Pattern[str]]
    forbidden: dict[str, list[re.Pattern[str]]]
    clause_separator: re.Pattern[str]
    negation_start: re.Pattern[str]
    action_text: dict[str, list[re.Pattern[str]]]

    def clauses(self, text: str) -> list[str]:
        """Split a step into clauses that are not warnings."""
        parts = self.clause_separator.split(text)
        return [p for p in parts if p.strip() and not self.negation_start.search(p)]


@lru_cache
def load_rules(path: Path = RULES_PATH) -> Rules:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    flags = re.IGNORECASE

    def compile_map(entries: dict[str, list[str]]) -> dict[str, list[re.Pattern[str]]]:
        return {key: [re.compile(p, flags) for p in patterns] for key, patterns in entries.items()}

    return Rules(
        injection=[re.compile(p, flags) for p in raw["injection_patterns"]],
        forbidden=compile_map(raw["forbidden_action_patterns"]),
        clause_separator=re.compile(raw["clause_separator"], flags),
        negation_start=re.compile(raw["negation_start"], flags),
        action_text=compile_map(raw["action_text_patterns"]),
    )
