"""Risk policy: the registry decides risk and approval, never the model."""

import json

from app.config import Settings
from app.policy import decide, mode_for_category
from app.registry import load_registry
from app.schemas import Step
from app.triage.pipeline import TriagePipeline
from tests.test_pipeline import KB_HIT, LOCK_LOG, VALID_DRAFT, FakeLLM, FakeRetriever

REGISTRY = load_registry()
CHECK = Step(text="Check the folder", action="check_status")
RERUN = Step(text="Rerun the job", action="rerun_job")
CONFIG = Step(text="Change the setting", action="config_change")


def test_risk_comes_from_the_registry() -> None:
    assert decide("sap_lock_conflict", [CHECK], REGISTRY).risk == "high"
    assert decide("sftp_file_missing", [CHECK], REGISTRY).risk == "low"


def test_high_risk_mode_always_needs_approval() -> None:
    assert decide("sap_lock_conflict", [CHECK], REGISTRY).approval_required


def test_rerun_or_config_change_needs_approval_even_at_low_risk() -> None:
    assert not decide("sftp_file_missing", [CHECK], REGISTRY).approval_required
    assert decide("sftp_file_missing", [CHECK, RERUN], REGISTRY).approval_required
    assert decide("mrp_runtime_exceeded", [CONFIG], REGISTRY).approval_required


def test_unknown_category_maps_to_the_catch_all() -> None:
    assert mode_for_category("made_up", REGISTRY).id == "FM-008"


def test_model_cannot_lower_risk_or_remove_approval() -> None:
    """S2 scenario: the model claims the lock conflict is low risk with no approval."""
    lying = VALID_DRAFT | {"risk": "low", "approval_required": False}
    pipeline = TriagePipeline(llm=FakeLLM(json.dumps(lying)), retriever=FakeRetriever(), modes=REGISTRY,
                              settings=Settings(_env_file=None, triage_model="test"))
    result = pipeline.run_log("S2", LOCK_LOG)

    assert result.mode == "full" and not result.escalate
    assert result.risk == "high"
    assert result.approval_required
    assert "policy_override:risk=low->high" in result.guardrail_events
    assert "policy_override:approval_required=false->true" in result.guardrail_events
    assert KB_HIT.doc_id in [c.doc_id for c in result.citations]
