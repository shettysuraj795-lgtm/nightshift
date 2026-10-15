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
    negation: re.Pattern[str]


@lru_cache
def load_rules(path: Path = RULES_PATH) -> Rules:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    flags = re.IGNORECASE
    return Rules(
        injection=[re.compile(p, flags) for p in raw["injection_patterns"]],
        forbidden={action: [re.compile(p, flags) for p in patterns] for action, patterns in raw["forbidden_action_patterns"].items()},
        negation=re.compile(raw["negation_pattern"], flags),
    )
