"""Ground-truth registry of failure modes.

`data/registry.yaml` is the single source of truth. The data generator builds
logs, KB articles, tickets and eval labels from it, and the risk policy reads
risk and approval from it, never from the model.
"""

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "data" / "registry.yaml"

# Step action vocabulary shared with TriageResult.
Action = Literal["check_status", "rerun_job", "config_change", "escalate", "info"]
Risk = Literal["low", "medium", "high"]


class FailureMode(BaseModel):
    """One known failure mode, exactly as written in the registry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^FM-\d{3}$")
    category: str
    title: str
    systems: list[str]
    signatures: list[str]
    root_cause: str
    fix_steps: list[str]
    risk: Risk
    approval_required: bool
    allowed_actions: list[Action]
    forbidden_actions: list[str]
    escalate_to: Literal["L1", "L2"]
    kb_articles: list[str]

    @field_validator("signatures")
    @classmethod
    def _signatures_compile(cls, patterns: list[str]) -> list[str]:
        """Fail early if a signature is not a valid regular expression."""
        for pattern in patterns:
            re.compile(pattern)
        return patterns


def load_registry(path: Path = REGISTRY_PATH) -> list[FailureMode]:
    """Load and validate the registry; IDs and categories must be unique."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    modes = [FailureMode.model_validate(item) for item in raw]

    for field in ("id", "category"):
        values = [getattr(mode, field) for mode in modes]
        duplicates = {value for value in values if values.count(value) > 1}
        if duplicates:
            raise ValueError(f"Duplicate registry {field}: {sorted(duplicates)}")
    return modes
