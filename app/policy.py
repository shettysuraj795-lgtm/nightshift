"""Risk policy. The model advises, code decides.

`risk` and `approval_required` come from the registry entry for the result's
category, never from the model, so the model can never lower risk or remove
an approval. Any step that reruns a job or changes configuration also
requires approval, whatever the registry risk.
"""

from dataclasses import dataclass

from app.registry import FailureMode, Risk
from app.schemas import Step

UNKNOWN_CATEGORY = "unknown_novel"  # the registry's catch-all mode (FM-008)
ACTIONS_NEEDING_APPROVAL = {"rerun_job", "config_change"}


@dataclass(frozen=True)
class PolicyDecision:
    risk: Risk
    approval_required: bool


def mode_for_category(category: str, modes: list[FailureMode]) -> FailureMode:
    """Registry entry for a category; unknown categories map to the catch-all."""
    by_category = {m.category: m for m in modes}
    return by_category.get(category, by_category[UNKNOWN_CATEGORY])


def decide(category: str, steps: list[Step], modes: list[FailureMode]) -> PolicyDecision:
    mode = mode_for_category(category, modes)
    needs_approval = mode.approval_required or any(s.action in ACTIONS_NEEDING_APPROVAL for s in steps)
    return PolicyDecision(risk=mode.risk, approval_required=needs_approval)
