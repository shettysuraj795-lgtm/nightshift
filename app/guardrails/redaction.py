"""PII redaction, applied to the log before it goes anywhere near a model.

Regex recognisers for emails, phone numbers and employee IDs, plus a list of
known names. Each distinct value gets a typed, numbered placeholder
(`<EMAIL_1>`, `<PERSON_1>`), so the model can still tell values apart.
Only counts by type are recorded, never the values.

Production would use an NER-based tool such as Microsoft Presidio, which also
finds names that are not on a list. Regexes plus a names list are enough for
the synthetic demo data and are easy to explain.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_NAMES_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "pii_names.txt"

# Order matters: emails first, so the parts of an address are not matched again.
PATTERNS: list[tuple[str, str]] = [
    ("EMAIL", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    ("PHONE", r"\+\d{1,3}(?:[ -]\d{1,4}){2,5}\b"),
    ("EMPLOYEE_ID", r"\bEMP-\d{5}\b"),
]


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)  # distinct values per type

    def events(self) -> list[str]:
        """Guardrail events such as `pii_redacted:email=2`, one per type found."""
        return [f"pii_redacted:{kind.lower()}={n}" for kind, n in self.counts.items()]


class Redactor:
    """Replaces personal data with placeholders."""

    def __init__(self, names: list[str]) -> None:
        patterns = list(PATTERNS)
        if names:
            # Longest names first, so "Anna Maria Berger" wins over "Anna Maria".
            alternatives = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            patterns.append(("PERSON", rf"\b(?:{alternatives})\b"))
        self.patterns = [(kind, re.compile(pattern)) for kind, pattern in patterns]

    @classmethod
    def from_file(cls, path: Path = DEFAULT_NAMES_PATH) -> "Redactor":
        names = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return cls(names)

    def redact(self, text: str) -> RedactionResult:
        counts: dict[str, int] = {}
        for kind, pattern in self.patterns:
            placeholders: dict[str, str] = {}

            def replace(match: re.Match[str]) -> str:
                value = match.group(0)
                if value not in placeholders:
                    placeholders[value] = f"<{kind}_{len(placeholders) + 1}>"
                return placeholders[value]

            text = pattern.sub(replace, text)
            if placeholders:
                counts[kind] = len(placeholders)
        return RedactionResult(text=text, counts=counts)
