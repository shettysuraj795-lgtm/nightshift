"""Cross-file consistency of the generated synthetic data.

These checks guard the ground truth that every later phase relies on: eval
labels, KB coverage, signature uniqueness and the planted traps.
"""

import json
import re
from collections import Counter
from pathlib import Path

import pytest
import yaml

from app.registry import load_registry
from scripts.generate_data import CANARY, INJECTION_TEXT, generate

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

REGISTRY = load_registry()
MODES_BY_CATEGORY = {mode.category: mode for mode in REGISTRY}
GOLDEN = [json.loads(line) for line in (DATA / "evals" / "golden.jsonl").read_text().splitlines()]
LOGS = sorted((DATA / "logs").glob("*.log"))


def read_front_matter(path: Path) -> dict:
    """Parse the YAML block between the first two `---` lines."""
    _, front, _ = path.read_text(encoding="utf-8").split("---\n", 2)
    return yaml.safe_load(front)


KB = {path.stem: path for path in (DATA / "kb").glob("*.md")}


def matching_modes(text: str) -> set[str]:
    """IDs of all registry modes with at least one signature found in the text."""
    return {mode.id for mode in REGISTRY for pattern in mode.signatures if re.search(pattern, text)}


# --- Registry --------------------------------------------------------------


def test_registry_has_eight_modes() -> None:
    assert [mode.id for mode in REGISTRY] == [f"FM-{i:03d}" for i in range(1, 9)]


def test_high_risk_modes_require_approval() -> None:
    for mode in REGISTRY:
        if mode.risk == "high":
            assert mode.approval_required, mode.id


def test_allowed_and_forbidden_actions_do_not_overlap() -> None:
    for mode in REGISTRY:
        assert not set(mode.allowed_actions) & set(mode.forbidden_actions), mode.id


def test_only_the_novel_mode_lacks_coverage() -> None:
    uncovered = [mode.id for mode in REGISTRY if not mode.kb_articles or not mode.signatures]
    assert uncovered == ["FM-008"]


# --- KB --------------------------------------------------------------------


def test_every_registry_kb_article_exists_with_matching_id() -> None:
    for mode in REGISTRY:
        for article_id in mode.kb_articles:
            assert article_id in KB, f"{mode.id} points to missing {article_id}"
            assert read_front_matter(KB[article_id])["id"] == article_id


def test_kb_front_matter_is_complete() -> None:
    assert len(KB) == 12
    for article_id, path in KB.items():
        front = read_front_matter(path)
        assert {"id", "title", "systems", "last_reviewed", "superseded"} <= front.keys(), article_id
        if front["superseded"]:
            assert front["superseded_by"] in KB, article_id


def test_runbooks_contain_registry_text_verbatim() -> None:
    """The guarantee KB enrichment (1f) must keep: signatures and fix steps unchanged."""
    for mode in REGISTRY:
        for article_id in mode.kb_articles:
            text = KB[article_id].read_text(encoding="utf-8")
            for item in mode.signatures + mode.fix_steps:
                assert item in text, f"{article_id} lost: {item}"


# --- Logs ------------------------------------------------------------------


def test_forty_logs() -> None:
    assert len(LOGS) == 40


def test_no_log_matches_more_than_one_mode() -> None:
    for path in LOGS:
        assert len(matching_modes(path.read_text(encoding="utf-8"))) <= 1, path.name


# --- Eval cases ------------------------------------------------------------


def test_eval_group_sizes() -> None:
    groups = Counter(case["tags"][0] for case in GOLDEN)
    assert groups == {"standard": 10, "unanswerable": 4, "high_risk": 4, "pii": 3, "injection": 3}


@pytest.mark.parametrize("case", GOLDEN, ids=lambda case: case["id"])
def test_eval_case_is_consistent(case: dict) -> None:
    log = ROOT / case["log_file"]
    assert log.exists(), case["log_file"]
    text = log.read_text(encoding="utf-8")

    mode = MODES_BY_CATEGORY[case["expected_category"]]
    assert case["expected_kb"] == mode.kb_articles
    assert case["expected_risk"] == mode.risk
    assert case["approval_required"] == mode.approval_required
    assert case["answerable"] == bool(mode.kb_articles)

    # The signature matcher can find the right mode, and only that one.
    assert matching_modes(text) == ({mode.id} if mode.signatures else set())

    for planted in case["planted_pii"]:
        assert planted in text, f"{case['id']}: {planted} not in log"
    assert (INJECTION_TEXT in text) == case["injection"]


def test_injection_only_where_labelled() -> None:
    injected = {path.name for path in LOGS if CANARY in path.read_text(encoding="utf-8")}
    labelled = {Path(case["log_file"]).name for case in GOLDEN if case["injection"]}
    assert injected == labelled


def test_pii_names_appear_in_logs() -> None:
    all_logs = "".join(path.read_text(encoding="utf-8") for path in LOGS)
    for name in (DATA / "pii_names.txt").read_text(encoding="utf-8").splitlines():
        assert name in all_logs, name


# --- Tickets, jobs and the failure queue -----------------------------------


def test_tickets_link_to_existing_kb_and_contain_no_planted_names() -> None:
    tickets = json.loads((DATA / "tickets.json").read_text(encoding="utf-8"))
    names = (DATA / "pii_names.txt").read_text(encoding="utf-8").splitlines()
    assert len(tickets) == 30
    assert len({t["number"] for t in tickets}) == 30
    for ticket in tickets:
        assert ticket["linked_kb"] in KB, ticket["number"]
        assert not any(name in json.dumps(ticket) for name in names), ticket["number"]


def test_nightly_failures_point_to_known_jobs_and_logs() -> None:
    failures = json.loads((DATA / "nightly_failures.json").read_text(encoding="utf-8"))
    jobs = {job["name"] for job in json.loads((DATA / "jobs.json").read_text(encoding="utf-8"))}
    assert [f["id"] for f in failures[:4]] == ["S1", "S2", "S3", "S4"]
    for failure in failures:
        assert (ROOT / failure["log_file"]).exists(), failure["id"]
        assert failure["job"] in jobs, failure["id"]


# --- Reproducibility -------------------------------------------------------


def test_regeneration_is_byte_identical(tmp_path: Path) -> None:
    generate(tmp_path, seed=42)
    generated = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    committed = sorted(
        p.relative_to(DATA)
        for p in DATA.rglob("*")
        if p.is_file() and p.name != "registry.yaml" and not p.name.startswith(".")  # skip .DS_Store etc.
    )
    assert generated == committed
    for relative in generated:
        assert (tmp_path / relative).read_bytes() == (DATA / relative).read_bytes(), str(relative)
