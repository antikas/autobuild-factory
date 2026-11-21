"""A live check that the reviewer catches a planted defect behind a green validator.

The fixture's brief states an exact outcome, the change delivers another, and
the fixture's own test asserts the wrong outcome, so the validator is green.
Each review shape a campaign runs (the blind reviewer alone, and the blind
reviewer with one boundary specialist) must not pass it. The check calls a
real model, so it runs only when ``AUTOBUILD_LIVE_REVIEW=claude-code``."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from test_git_workspace import git, repository

from autobuild.adapters import (
    ClaudeCodeHarnessAdapter,
    GitWorkspaceAdapter,
    PosixCommandAdapter,
    WindowsCommandAdapter,
)
from autobuild.application.prompts import render_seat_instructions
from autobuild.domain import (
    CampaignRef,
    CommandRequest,
    EffortLevel,
    EvidenceError,
    ItemExecutionSpec,
    ReviewDecision,
    ReviewVerdict,
    Seat,
    SeatOutcome,
    SeatRequest,
    ToolPolicy,
    WorkItem,
    review_verdict_rule_error,
)
from autobuild.domain.models import ReviewBoundary
from autobuild.enforcement.review_scope import boundary_findings, combined_verdict

LIVE_VARIABLE = "AUTOBUILD_LIVE_REVIEW"
MODEL = "claude-opus-5-5"

live = pytest.mark.skipif(
    os.environ.get(LIVE_VARIABLE) != "claude-code",
    reason=f"live planted-defect review runs only with {LIVE_VARIABLE}=claude-code",
)

BRIEF = """# Add a total function

## Outcome

`src/total.py` gains `total(values)`, the sum of a list of numbers.

## Acceptance

- `total([2, 3])` returns 5.
- `tests/test_total.py` checks `total([2, 3])`.

## Declared paths

- `src/total.py`
- `tests/test_total.py`
"""

PLANTED = "def total(values):\n    return max(values) + 1\n"
WRONG_TEST = (
    "import sys\n"
    "from pathlib import Path\n\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n\n"
    "from total import total\n\n\n"
    "def test_total():\n"
    "    assert total([2, 3]) == 4\n\n\n"
    "if __name__ == '__main__':\n"
    "    test_total()\n"
    "    print('1 passed')\n"
)
BOUNDARY = ReviewBoundary("arithmetic", ("src/**",), None)


def _command_adapter(root: Path):
    if os.name == "nt":
        return WindowsCommandAdapter(root)
    return PosixCommandAdapter(root)


def _fixture(tmp_path: Path):
    repo, _ = repository(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "brief.md").write_text(BRIEF, encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "total.py").write_text(
        "def total(values):\n    raise NotImplementedError\n", encoding="utf-8"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "brief")
    workspaces = GitWorkspaceAdapter(tmp_path / "scratch")
    item = WorkItem("planted-1", "Add a total function", "docs/brief.md", ("total sums",))
    workspace = workspaces.create_isolated(CampaignRef("planted", repo), item)
    (workspace.root / "src" / "total.py").write_text(PLANTED, encoding="utf-8")
    (workspace.root / "tests").mkdir()
    (workspace.root / "tests" / "test_total.py").write_text(WRONG_TEST, encoding="utf-8")
    diff = workspaces.diff(workspace)
    commands = _command_adapter(tmp_path / "command output")
    validation = commands.run(
        CommandRequest(
            "run:planted-1:validator",
            (sys.executable, "tests/test_total.py"),
            workspace.root,
            timeout_seconds=120,
        )
    )
    assert validation.exit_code == 0, "the fixture's validator must be green"
    spec = ItemExecutionSpec(
        item,
        workspace.root / "docs" / "brief.md",
        "validator",
        (sys.executable, "tests/test_total.py"),
        ToolPolicy(frozenset({"read"}), (workspace.root,)),
        "builder-class",
        "reviewer-class",
        "specialist-class",
        brief_text=BRIEF,
    )
    harness = ClaudeCodeHarnessAdapter(
        commands,
        tmp_path / "harness output",
        ("claude",),
        {"reviewer-class": MODEL, "specialist-class": MODEL},
    )
    return spec, workspace, harness, (diff.patch_ref, validation.stdout_ref)


def _seat(spec, workspace, harness, seat: Seat, refs, boundary=None) -> ReviewVerdict:
    model_class = "reviewer-class" if seat is Seat.REVIEWER else "specialist-class"
    result = harness.invoke(
        SeatRequest(
            run_id="planted",
            item_id=spec.item.item_id,
            seat=seat,
            model_class=model_class,
            brief_path=spec.brief_path,
            workspace=workspace,
            tool_policy=spec.tool_policy,
            instructions=render_seat_instructions(
                spec, seat, refs, spec.brief_path, boundary=boundary
            ),
            result_contract="review-verdict-v1",
            timeout_seconds=900,
            context_refs=refs,
            effort=EffortLevel.HIGH,
        )
    )
    assert result.outcome is SeatOutcome.SUCCEEDED, result.diagnostics
    assert isinstance(result.payload, ReviewVerdict)
    payload = result.payload
    return boundary_findings(payload, boundary) if boundary is not None else payload


def _caught(review) -> bool:
    """True when the planted defect does not pass: the decision is not pass,
    or the verdict is refused under the pass-with-unmet rule."""

    try:
        verdict = review()
    except EvidenceError as refused:
        if "unmet" in str(refused):
            return True
        raise
    if verdict.decision is not ReviewDecision.PASS:
        return True
    return review_verdict_rule_error(verdict) is not None


def test_an_accepted_pass_is_not_caught_and_a_refused_one_is() -> None:
    passed = ReviewVerdict("planted-1", ReviewDecision.PASS, (), "review")

    def refused() -> ReviewVerdict:
        raise EvidenceError("a pass verdict must not report acceptance clause A1 unmet")

    assert _caught(lambda: passed) is False
    assert _caught(refused) is True


@live
def test_the_blind_reviewer_alone_catches_the_planted_defect(tmp_path: Path) -> None:
    spec, workspace, harness, refs = _fixture(tmp_path)

    assert _caught(lambda: _seat(spec, workspace, harness, Seat.REVIEWER, refs))


@live
def test_the_blind_reviewer_with_a_boundary_specialist_catches_the_planted_defect(
    tmp_path: Path,
) -> None:
    spec, workspace, harness, refs = _fixture(tmp_path)

    def review() -> ReviewVerdict:
        blind = _seat(spec, workspace, harness, Seat.REVIEWER, refs)
        specialist = _seat(spec, workspace, harness, Seat.SPECIALIST, refs, BOUNDARY)
        return combined_verdict((blind, specialist), "planted-round")

    assert _caught(review)
