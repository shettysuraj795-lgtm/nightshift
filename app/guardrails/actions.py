"""Output check on recommended steps.

For each step:
1. The action is corrected to what the text actually says when that is
   stricter: a rerun tagged as `info` is treated as a rerun, so it cannot slip
   past the allowed-actions check or the approval policy.
2. The step is removed if its action is not allowed for the failure mode.
   `info` is always allowed because it changes nothing.
3. The step is removed if any clause recommends an action the registry
   forbids. Forbidden actions are checked across all modes: a "never do" is
   never acceptable, whatever the category. Clauses that start with a warning
   ("Do not ...") are not recommendations and are skipped.
"""

from dataclasses import dataclass

from app.guardrails.rules import Rules, load_rules
from app.registry import Action, FailureMode
from app.schemas import Step

# Strictest first: the first match in the text wins over a milder tag.
STRICTER_ACTIONS: list[Action] = ["config_change", "rerun_job"]


@dataclass(frozen=True)
class StepCheck:
    kept: list[Step]
    events: list[str]


def implied_action(text: str, rules: Rules) -> Action | None:
    """A stricter action the text describes (rerun or config change), if any."""
    for clause in rules.clauses(text):
        for action in STRICTER_ACTIONS:
            if any(p.search(clause) for p in rules.action_text[action]):
                return action
    return None


def forbidden_action_in(text: str, rules: Rules) -> str | None:
    """The forbidden action a step recommends, or None."""
    for clause in rules.clauses(text):
        for action, patterns in rules.forbidden.items():
            if any(p.search(clause) for p in patterns):
                return action
    return None


def check_steps(steps: list[Step], mode: FailureMode, rules: Rules | None = None) -> StepCheck:
    rules = rules or load_rules()
    kept, events = [], []
    for step in steps:
        implied = implied_action(step.text, rules)
        if implied and step.action not in STRICTER_ACTIONS:
            events.append(f"step_action_corrected:{step.action}->{implied}")
            step = Step(text=step.text, action=implied)

        if step.action != "info" and step.action not in mode.allowed_actions:
            events.append(f"step_removed:not_allowed:{step.action}")
        elif forbidden := forbidden_action_in(step.text, rules):
            events.append(f"step_removed:forbidden:{forbidden}")
        else:
            kept.append(step)
    return StepCheck(kept=kept, events=events)
