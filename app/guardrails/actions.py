"""Output check on recommended steps.

A step is removed if its action type is not allowed for the failure mode, or
if its text recommends any action the registry forbids. The forbidden list is
checked across all modes: a "never do" is never acceptable, whatever the
category. `info` steps are always allowed because they change nothing.
"""

from dataclasses import dataclass

from app.guardrails.rules import Rules, load_rules
from app.registry import FailureMode
from app.schemas import Step


@dataclass(frozen=True)
class StepCheck:
    kept: list[Step]
    events: list[str]


def forbidden_action_in(text: str, rules: Rules) -> str | None:
    """The forbidden action a step recommends, or None. Warnings ("Do not ...") don't count."""
    if rules.negation.search(text):
        return None
    for action, patterns in rules.forbidden.items():
        if any(p.search(text) for p in patterns):
            return action
    return None


def check_steps(steps: list[Step], mode: FailureMode, rules: Rules | None = None) -> StepCheck:
    rules = rules or load_rules()
    kept, events = [], []
    for step in steps:
        if step.action != "info" and step.action not in mode.allowed_actions:
            events.append(f"step_removed:not_allowed:{step.action}")
        elif forbidden := forbidden_action_in(step.text, rules):
            events.append(f"step_removed:forbidden:{forbidden}")
        else:
            kept.append(step)
    return StepCheck(kept=kept, events=events)
