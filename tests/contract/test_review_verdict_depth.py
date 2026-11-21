"""The review-verdict-v1 depth fields: what a review seat read and the
acceptance clauses it judged."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from test_harness_adapters import identity, request

from autobuild.adapters import ClaudeCodeHarnessAdapter
from autobuild.adapters.harness_cli import REVIEW_SCHEMA
from autobuild.domain import (
    ClauseCheck,
    ClauseOutcome,
    EvidenceError,
    InspectedPath,
    ReviewDecision,
    Seat,
)
from autobuild.testing import FakeCommandAdapter


def _verdict(**overrides) -> dict:
    data = {
        "decision": "pass",
        "findings": [],
        "evidence_ref": "review",
        "inspected": [{"path": "src/total.py", "lines": "all"}],
        "acceptance": [{"clause": "A1", "outcome": "met", "evidence_ref": "src/total.py:3"}],
    }
    data.update(overrides)
    return data


def _parse(tmp_path: Path, data: dict):
    adapter = ClaudeCodeHarnessAdapter(
        FakeCommandAdapter(identity()), tmp_path / "harness", command=(sys.executable,)
    )
    seat = request(tmp_path, Seat.REVIEWER, "review-verdict-v1", frozenset({"read"}))
    return adapter._payload(seat, data, "review")


def test_the_payload_parses_inspected_and_acceptance(tmp_path: Path) -> None:
    verdict = _parse(tmp_path, _verdict())

    assert verdict.inspected == (InspectedPath("src/total.py", "all"),)
    assert verdict.acceptance == (ClauseCheck("A1", ClauseOutcome.MET, "src/total.py:3"),)


@pytest.mark.parametrize("missing", ["inspected", "acceptance"])
def test_the_payload_refuses_a_verdict_missing_a_depth_array(tmp_path: Path, missing: str) -> None:
    data = _verdict()
    del data[missing]

    with pytest.raises(EvidenceError, match=missing):
        _parse(tmp_path, data)


def test_the_payload_refuses_a_pass_with_an_unmet_clause(tmp_path: Path) -> None:
    data = _verdict(
        acceptance=[
            {"clause": "A1", "outcome": "met", "evidence_ref": "a"},
            {"clause": "A2", "outcome": "unmet", "evidence_ref": "b"},
        ]
    )

    with pytest.raises(EvidenceError, match="A2"):
        _parse(tmp_path, data)


def test_the_payload_accepts_a_correct_with_an_unmet_clause(tmp_path: Path) -> None:
    finding = {
        "code": "A2",
        "consequence": "total is wrong",
        "evidence_ref": "src/total.py:3",
        "blocking": True,
        "specialist_boundary": None,
    }
    data = _verdict(
        decision="correct",
        findings=[finding],
        acceptance=[{"clause": "A2", "outcome": "unmet", "evidence_ref": "b"}],
    )

    assert _parse(tmp_path, data).decision is ReviewDecision.CORRECT


def test_the_payload_refuses_an_unknown_clause_outcome(tmp_path: Path) -> None:
    data = _verdict(acceptance=[{"clause": "A1", "outcome": "maybe", "evidence_ref": "a"}])

    with pytest.raises(EvidenceError):
        _parse(tmp_path, data)


def test_the_schema_requires_every_key_of_both_depth_objects() -> None:
    assert {"inspected", "acceptance"} <= set(REVIEW_SCHEMA["required"])
    for name, keys in (
        ("inspected", {"path", "lines"}),
        ("acceptance", {"clause", "outcome", "evidence_ref"}),
    ):
        item = REVIEW_SCHEMA["properties"][name]["items"]
        assert set(item["properties"]) == keys
        assert set(item["required"]) == keys
        assert item["additionalProperties"] is False
    outcome = REVIEW_SCHEMA["properties"]["acceptance"]["items"]["properties"]["outcome"]
    assert outcome["enum"] == ["met", "unmet", "not_checked"]
