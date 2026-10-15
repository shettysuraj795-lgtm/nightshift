"""Abstention: when the evidence is too weak, escalate instead of guessing.

The thresholds are configured in `.env` (ABSTAIN_MIN_RETRIEVAL_SCORE and
ABSTAIN_MIN_CONFIDENCE). Phase 2 measured a top KB score of 0.649 for the
novel failure and at least 0.723 for covered ones; the default of 0.70 sits
between them.
"""

from app.schemas import Step

ESCALATION_STEP = Step(text="Escalate to L2 with the full job log attached", action="escalate")


def abstention_reasons(
    *,
    kb_available: bool,
    top_retrieval_score: float | None,
    valid_citations: int,
    confidence: float,
    model_escalated: bool,
    known_category: bool,
    agrees_with_signatures: bool,
    steps_left: int,
    min_retrieval_score: float,
    min_confidence: float,
) -> list[str]:
    """Plain-English reasons to abstain; an empty list means answer normally."""
    reasons = []
    if model_escalated:
        reasons.append("the model judged the evidence insufficient")
    if not known_category:
        reasons.append("the failure does not match any known failure mode")
    if not agrees_with_signatures:
        reasons.append("the diagnosis disagrees with the known error signature in the log")
    # With the knowledge base down, retrieval and citations cannot be judged,
    # so those two checks are skipped (the result says the KB was unavailable).
    if kb_available:
        if top_retrieval_score is None or top_retrieval_score < min_retrieval_score:
            reasons.append("no knowledge base article is a close match")
        if valid_citations == 0:
            reasons.append("no answer could be backed by a valid source")
    if confidence < min_confidence:
        reasons.append(f"confidence {confidence:.2f} is below {min_confidence:.2f}")
    if steps_left == 0:
        reasons.append("no safe recommended steps remained after checks")
    return reasons
