"""Risk policy. The model advises, code decides.

`risk` and `approval_required` come from the registry, never from the model.
The model also cannot lower risk by choosing a milder category: if the
deterministic signature matcher found other failure modes in the log, the
strictest of all of them applies. Any step that reruns a job or changes
configuration requires approval, whatever the registry risk.
"""

from dataclasses import dataclass

from app.registry import FailureMode, Risk
from app.schemas import Step

UNKNOWN_CATEGORY = "unknown_novel"  # the registry's catch-all mode (FM-008)
ACTIONS_NEEDING_APPROVAL = {"rerun_job", "config_change"}
RISK_ORDER: dict[Risk, int] = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class PolicyDecision:
    risk: Risk
    approval_required: bool


def mode_for_category(category: str, modes: list[FailureMode]) -> FailureMode:
    """Registry entry for a category; unknown categories map to the catch-all."""
    by_category = {m.category: m for m in modes}
    return by_category.get(category, by_category[UNKNOWN_CATEGORY])


def decide(
    category: str,
    steps: list[Step],
    modes: list[FailureMode],
    signature_categories: list[str] | None = None,
) -> PolicyDecision:
    relevant = [mode_for_category(c, modes) for c in [category, *(signature_categories or [])]]
    risk = max((m.risk for m in relevant), key=RISK_ORDER.__getitem__)
    needs_approval = any(m.approval_required for m in relevant) or any(s.action in ACTIONS_NEEDING_APPROVAL for s in steps)
    return PolicyDecision(risk=risk, approval_required=needs_approval)
