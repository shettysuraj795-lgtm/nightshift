"""UI wording and counts, derived from results only."""

from app.schemas import Step, TriageResult
from ui.view_model import banners, clock_time, pii_summary, risk_badge, risk_explanation, summary_counts


def result(**overrides) -> TriageResult:
    base = dict(failure_id="S1", category="sftp_file_missing", root_cause="r", confidence=0.9, evidence=[],
                steps=[Step(text="Check the folder", action="check_status")], risk="low", approval_required=False,
                citations=[], escalate=False, escalation_reason=None, mode="full", model_used="m",
                fallback_used=False, guardrail_events=[], trace_id="t")
    return TriageResult(**(base | overrides))


def texts(r: TriageResult) -> list[str]:
    return [b.text for b in banners(r)]


def test_pii_summary_pluralises() -> None:
    events = ["pii_redacted:email=2", "pii_redacted:phone=1", "pii_redacted:person=1", "injection_detected:lines=1"]
    assert pii_summary(events) == "2 emails, 1 phone number, 1 name"
    assert pii_summary([]) is None


def test_s3_style_banners() -> None:
    r = result(guardrail_events=["pii_redacted:email=2", "pii_redacted:person=1", "injection_detected:lines=1"])
    assert texts(r) == [
        "Personal data removed before analysis (2 emails, 1 name).",
        "Suspicious instruction in the log was ignored.",
    ]


def test_fallback_escalation_and_outages() -> None:
    assert "Answered by the backup model (qwen2.5:3b), running locally." in texts(result(fallback_used=True, model_used="qwen2.5:3b"))
    assert texts(result(escalate=True))[0].startswith("Not enough evidence")
    assert texts(result(mode="degraded", guardrail_events=["knowledge_base_unavailable"]))[0].startswith("Knowledge base unavailable")
    assert texts(result(mode="degraded", guardrail_events=["llm_unavailable"]))[0].startswith("AI models unavailable")


def test_removed_steps_and_sources() -> None:
    r = result(guardrail_events=["step_removed:forbidden:delete_lock_entry", "citations_dropped:2"])
    assert texts(r) == ["1 suggested step removed by safety checks.", "2 sources that could not be verified removed."]


def test_clean_result_has_no_banners() -> None:
    assert banners(result()) == []


def test_risk_explanation_names_policy_overrides() -> None:
    r = result(risk="high", approval_required=True, category="sap_lock_conflict",
               steps=[Step(text="Rerun the posting job", action="rerun_job")],
               guardrail_events=["policy_override:risk=low->high", "policy_override:approval_required=false->true"])
    lines = risk_explanation(r)
    assert "The AI suggested low risk; the policy raised it to high." in lines
    assert "**Approval required** because this is a high-risk failure type and the steps rerun a job or change configuration." in lines
    assert "The AI said no approval was needed; the policy overruled it." in lines


def test_no_approval_explanation() -> None:
    assert risk_explanation(result())[-1].startswith("No approval needed")


def test_summary_counts_only_use_session_results() -> None:
    results = {
        "S1": result(failure_id="S1", approval_required=True),
        "S2": result(failure_id="S2", approval_required=True),
        "S4": result(failure_id="S4", escalate=True),
    }
    counts = summary_counts(["S1", "S2", "S3", "S4"], results, approvals={"S2": {"approver": "x"}})
    assert counts == {"failures": 4, "triaged": 3, "awaiting_approval": 1, "escalated": 1}


def test_card_badge_prefers_escalation() -> None:
    from ui.view_model import card_badge
    assert card_badge(result(escalate=True, risk="medium")) == ":violet-badge[Escalated to L2]"
    assert card_badge(result(risk="low")) == ":green-badge[Low risk]"
    assert card_badge(None) == ":gray-badge[Not triaged]"


def test_badges_and_time() -> None:
    assert risk_badge("high") == ":red-badge[High risk]"
    assert risk_badge(None) == ":gray-badge[Not triaged]"
    assert clock_time("2026-10-14T03:14:06") == "03:14"


def test_citations_are_grouped_per_document() -> None:
    from app.schemas import Citation
    from ui.view_model import group_citations
    r = result(citations=[Citation(doc_id="KB-0102", title="T", excerpt="a"), Citation(doc_id="INC1", title="INC1: J", excerpt="b"),
                          Citation(doc_id="KB-0102", title="T", excerpt="c")])
    assert group_citations(r) == {"KB-0102": ("T", ["a", "c"]), "INC1": ("INC1: J", ["b"])}
