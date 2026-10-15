"""What the UI shows, worked out from API results. No Streamlit here, so it can be unit-tested.

Everything on screen is derived from data the API returned (never invented):
banners come from `guardrail_events`, `mode` and `fallback_used`; the risk
explanation from `risk`, `approval_required`, the steps and policy events.
Wording is for non-technical viewers.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from app.schemas import TriageResult

# Material icons (Streamlit's `:material/name:` syntax) and plain labels per step action.
ACTION_ICONS = {
    "check_status": ":material/search:",
    "rerun_job": ":material/replay:",
    "config_change": ":material/settings:",
    "escalate": ":material/support_agent:",
    "info": ":material/info:",
}
ACTION_LABELS = {
    "check_status": "Check",
    "rerun_job": "Rerun",
    "config_change": "Change",
    "escalate": "Hand over",
    "info": "Note",
}
RISK_COLOURS = {"low": "green", "medium": "orange", "high": "red"}

# Singular and plural names for redacted data types.
PII_NAMES = {
    "email": ("email", "emails"),
    "phone": ("phone number", "phone numbers"),
    "person": ("name", "names"),
    "employee_id": ("employee ID", "employee IDs"),
}


@dataclass(frozen=True)
class Banner:
    kind: str  # "info", "success", "warning" or "error", matching st.info / st.success / ...
    icon: str
    text: str


def risk_badge(risk: str | None) -> str:
    """Markdown for a coloured badge, e.g. ':red-badge[High risk]'. Untriaged failures get a grey one."""
    if risk is None:
        return ":gray-badge[Not triaged]"
    return f":{RISK_COLOURS[risk]}-badge[{risk.capitalize()} risk]"


def card_badge(result: TriageResult | None) -> str:
    """Badge for a failure card: escalation matters more to the operator than the risk level."""
    if result is not None and result.escalate:
        return ":violet-badge[Escalated to L2]"
    return risk_badge(result.risk if result else None)


def clock_time(iso_timestamp: str) -> str:
    return datetime.fromisoformat(iso_timestamp).strftime("%H:%M")


def _event_value(events: list[str], prefix: str) -> str | None:
    return next((e.removeprefix(prefix) for e in events if e.startswith(prefix)), None)


def pii_summary(events: list[str]) -> str | None:
    """'2 emails, 1 phone number, 1 name' from `pii_redacted:<type>=<n>` events."""
    parts = []
    for event in events:
        match = re.fullmatch(r"pii_redacted:(\w+)=(\d+)", event)
        if match:
            kind, count = match.group(1), int(match.group(2))
            singular, plural = PII_NAMES.get(kind, (kind, kind))
            parts.append(f"{count} {singular if count == 1 else plural}")
    return ", ".join(parts) or None


def banners(result: TriageResult) -> list[Banner]:
    """Plain-English status messages for one result, most important first."""
    events = result.guardrail_events
    out: list[Banner] = []

    if result.escalate:
        out.append(Banner("warning", ":material/support_agent:", "Not enough evidence for a confident fix: escalate to L2."))
    if "knowledge_base_unavailable" in events:
        out.append(Banner("error", ":material/cloud_off:", "Knowledge base unavailable: answer based on known error patterns only, no sources checked."))
    elif result.mode == "degraded":
        out.append(Banner("error", ":material/cloud_off:", "AI models unavailable: showing the standard procedure for the recognised error pattern."))
    if result.fallback_used:
        out.append(Banner("info", ":material/swap_horiz:", f"Answered by the backup model ({result.model_used}), running locally."))
    # Redaction happens first in the pipeline, so its banner comes first too.
    if pii := pii_summary(events):
        out.append(Banner("success", ":material/shield_person:", f"Personal data removed before analysis ({pii})."))
    if any(e.startswith("injection_detected") for e in events):
        out.append(Banner("warning", ":material/gpp_maybe:", "Suspicious instruction in the log was ignored."))

    removed_steps = sum(e.startswith("step_removed") for e in events)
    if removed_steps:
        noun = "step" if removed_steps == 1 else "steps"
        out.append(Banner("info", ":material/rule:", f"{removed_steps} suggested {noun} removed by safety checks."))
    dropped = _event_value(events, "citations_dropped:")
    if dropped:
        noun = "source" if dropped == "1" else "sources"
        out.append(Banner("info", ":material/link_off:", f"{dropped} {noun} that could not be verified removed."))
    return out


def risk_explanation(result: TriageResult) -> list[str]:
    """Why the risk level and approval are what they are, in plain English."""
    lines = [f"**{result.risk.capitalize()} risk.** Set by NightShift's rules for this failure type, not by the AI."]
    model_risk = _event_value(result.guardrail_events, "policy_override:risk=")
    if model_risk:
        suggested = model_risk.split("->")[0]
        lines.append(f"The AI suggested {suggested} risk; the policy raised it to {result.risk}.")

    if not result.approval_required:
        lines.append("No approval needed: the steps only check status or hand over to another team.")
        return lines
    reasons = []
    if result.risk == "high":
        reasons.append("this is a high-risk failure type")
    if any(step.action in ("rerun_job", "config_change") for step in result.steps):
        reasons.append("the steps rerun a job or change configuration")
    lines.append(f"**Approval required** because {' and '.join(reasons) or 'the policy says so'}.")
    if "policy_override:approval_required=false->true" in result.guardrail_events:
        lines.append("The AI said no approval was needed; the policy overruled it.")
    return lines


def summary_counts(failure_ids: list[str], results: dict[str, TriageResult], approvals: dict[str, dict]) -> dict[str, int]:
    """The summary strip, computed only from last night's queue and results in this session."""
    triaged = [results[f] for f in failure_ids if f in results]
    return {
        "failures": len(failure_ids),
        "triaged": len(triaged),
        "awaiting_approval": sum(r.approval_required and not r.escalate and r.failure_id not in approvals for r in triaged),
        "escalated": sum(r.escalate for r in triaged),
    }


def group_citations(result: TriageResult) -> dict[str, tuple[str, list[str]]]:
    """One entry per cited document, with all its quoted excerpts (a runbook is often cited per section)."""
    grouped: dict[str, tuple[str, list[str]]] = {}
    for citation in result.citations:
        grouped.setdefault(citation.doc_id, (citation.title, []))[1].append(citation.excerpt)
    return grouped
