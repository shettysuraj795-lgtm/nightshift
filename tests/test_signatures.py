"""Signature matcher against the labelled eval logs."""

import json
from pathlib import Path

import pytest

from app.registry import load_registry
from app.signatures import match_signatures

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = load_registry()
GOLDEN = [json.loads(line) for line in (ROOT / "data" / "evals" / "golden.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("case", GOLDEN, ids=lambda case: case["id"])
def test_top_candidate_is_the_labelled_category(case: dict) -> None:
    matches = match_signatures((ROOT / case["log_file"]).read_text(encoding="utf-8"), REGISTRY)
    if case["answerable"]:
        assert matches[0].category == case["expected_category"]
    else:
        assert matches == []


def test_matched_lines_are_returned_verbatim() -> None:
    log = "2026-10-14 01:00:00 INFO  start\n2026-10-14 01:01:00 ERROR Read timed out after 30000 ms\n"
    [match] = match_signatures(log, REGISTRY)
    assert match.category == "mes_network_timeout"
    assert match.matched_lines == ["2026-10-14 01:01:00 ERROR Read timed out after 30000 ms"]


def test_more_matching_lines_ranks_first() -> None:
    log = "\n".join([
        "Read timed out after 30000 ms",
        "File a.csv not found in /inbound/x/",
        "SFTP: no matching file for pattern a*.csv",
    ])
    assert [m.category for m in match_signatures(log, REGISTRY)] == ["sftp_file_missing", "mes_network_timeout"]
