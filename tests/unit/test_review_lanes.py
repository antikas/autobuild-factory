"""Review lanes: in strict mode the boundary map sets the review rounds.

A strict item with a boundary map is Small when no declared or changed path
touches a boundary and Material when one does. Small gets one review and one
correction the validator judges; Material gets two reviews and one final
correction the validator judges. Flow mode, and strict mode with no map, keep
``max_corrections`` and a review after every correction."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from test_process_mode import strict_policy
from test_review_boundaries import SIGN_IN, bounded, brief
from test_workflows import _builder_spec, builder, campaign, command, make_ports, review, spec

from autobuild.application import ItemWorkflow
from autobuild.domain import (
    ChangedPath,
    ChangeKind,
    DiffEvidence,
    EffortLevel,
    ItemDisposition,
    ReviewDecision,
    ReviewVerdict,
    SeatOutcome,
    SeatResult,
    SeatUsage,
)
from autobuild.domain.models import (
    ProcessMode,
    ProcessPolicy,
    ResumePlan,
    ReviewFinding,
    ReviewLane,
)
from autobuild.enforcement.review_scope import review_lane

UNTOUCHED = "src/app/profile/loader.py"
TOUCHED = "src/app/auth/login.py"


def two_findings(suffix: str) -> SeatResult:
    verdict = ReviewVerdict(
        "item-1",
        ReviewDecision.CORRECT,
        (
            ReviewFinding("BUG", "wrong output", f"finding:bug:{suffix}", blocking=True),
            ReviewFinding("STYLE", "rename", f"finding:style:{suffix}", blocking=False),
        ),
        f"verdict:{suffix}",
    )
    return SeatResult(
        f"review-{suffix}", SeatOutcome.SUCCEEDED, verdict, f"raw:{suffix}",
        SeatUsage(source="fake"), "start", "end",
    )


def run(
    process: ProcessPolicy,
    brief_text: str,
    seats: list[SeatResult],
    changed: list[tuple[str, ...]],
    exits: list[int] | None = None,
    work=None,
    resume: ResumePlan | None = None,
):
    exits = exits if exits is not None else [0] * len(changed)
    ports = make_ports(
        seats, [command(code, str(n)) for n, code in enumerate(exits)], len(changed)
    )
    workspace = ports.workspace.diffs[0].workspace
    ports.workspace.diffs[:] = [
        DiffEvidence(
            workspace,
            f"revision-{n}",
            tuple(ChangedPath(Path(path), ChangeKind.MODIFIED, "digest") for path in paths),
            f"patch:{n}",
        )
        for n, paths in enumerate(changed)
    ]
    work = replace(work or spec(), brief_text=brief_text, process=process)
    outcome = ItemWorkflow(ports).run(
        campaign(), work, ports.records.create(campaign()), resume=resume
    )
    return outcome, ports


def seat_names(ports) -> list[str]:
    return [request.seat.value for request in ports.harness.requests]


def events(ports, event_type: str):
    return [event for event in ports.records.events if event.event_type == event_type]


def trajectory(ports) -> str:
    return ports.records.evidence["item-1-trajectory"]


# --- the lane rule ------------------------------------------------------------------------


def test_the_lane_follows_the_mode_the_map_and_the_paths() -> None:
    assert review_lane(bounded(ProcessMode.FLOW), (TOUCHED,)) is None
    assert review_lane(strict_policy(), (TOUCHED,)) is None
    assert review_lane(bounded(), (UNTOUCHED, "README.md")) is ReviewLane.SMALL
    assert review_lane(bounded(), (UNTOUCHED, TOUCHED)) is ReviewLane.MATERIAL
    assert review_lane(bounded(), ("migrations/0004.sql",)) is ReviewLane.MATERIAL


# --- Small --------------------------------------------------------------------------------


def test_a_small_item_gets_one_review_and_one_validator_judged_correction() -> None:
    outcome, ports = run(
        bounded(),
        brief(UNTOUCHED),
        [builder("b1"), two_findings("1"), builder("b2")],
        [(UNTOUCHED,), (UNTOUCHED,)],
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert seat_names(ports) == ["builder", "reviewer", "builder"]
    assert len(events(ports, "validation.completed")) == 2
    close = ports.tracker.closed[0]
    assert close.accepted_at_round_limit is True
    assert close.verdict.evidence_ref == "verdict:1"
    assert close.diff.workspace_revision == close.validation.workspace_revision == "revision-1"
    titles = [proposal.title for proposal in ports.tracker.proposals]
    assert titles == ["Follow-up: item-1 BUG", "Follow-up: item-1 STYLE"]
    assert tuple(record.proposal for record in outcome.follow_ups) == tuple(
        ports.tracker.proposals
    )
    assert all(
        "judged by the validator and not reviewed again" in proposal.rationale
        for proposal in ports.tracker.proposals
    )
    assert "review lane: small" in trajectory(ports).splitlines()
    (finalised,) = events(ports, "item.finalised")
    assert finalised.payload["review_lane"] == "small"


def test_a_small_item_with_a_red_correction_parks() -> None:
    outcome, ports = run(
        bounded(),
        brief(UNTOUCHED),
        [builder("b1"), review(ReviewDecision.CORRECT), builder("b2"), builder("b3")],
        [(UNTOUCHED,), (UNTOUCHED,), (UNTOUCHED,)],
        exits=[0, 1, 1],
        work=_builder_spec(EffortLevel.MEDIUM),
    )

    assert outcome.disposition is ItemDisposition.PARKED
    assert "did not pass" in (outcome.reason or "")
    assert seat_names(ports) == ["builder", "reviewer", "builder", "builder"]
    assert ports.tracker.closed == []
    assert ports.tracker.proposals == []
    assert events(ports, "item.finalised") == []


def test_a_small_escalation_with_a_specialist_correct_gets_one_unreviewed_correction() -> None:
    outcome, ports = run(
        bounded(),
        brief(UNTOUCHED),
        [
            builder("b1"),
            review(ReviewDecision.ESCALATE, "1"),
            review(ReviewDecision.CORRECT, "specialist"),
            builder("b2"),
        ],
        [(UNTOUCHED,), (UNTOUCHED,)],
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert seat_names(ports) == ["builder", "reviewer", "specialist", "builder"]
    assert ports.tracker.closed[0].verdict.evidence_ref == "verdict:specialist"
    assert ports.tracker.closed[0].accepted_at_round_limit is True
    assert [proposal.title for proposal in ports.tracker.proposals] == ["Follow-up: item-1 finding"]


def test_a_small_correction_that_touches_a_boundary_gets_a_second_review_with_its_specialist() -> None:
    outcome, ports = run(
        bounded(),
        brief(UNTOUCHED),
        [
            builder("b1"),
            review(ReviewDecision.CORRECT, "1"),
            builder("b2"),
            review(ReviewDecision.PASS, "2"),
            review(ReviewDecision.PASS, "sign-in"),
        ],
        [(UNTOUCHED,), (TOUCHED,)],
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert seat_names(ports) == ["builder", "reviewer", "builder", "reviewer", "specialist"]
    assert f"{SIGN_IN.name} boundary" in ports.harness.requests[-1].instructions
    assert ports.tracker.closed[0].accepted_at_round_limit is False
    assert "review lane: material" in trajectory(ports).splitlines()
    assert events(ports, "item.finalised")[0].payload["review_lane"] == "material"


# --- Material -----------------------------------------------------------------------------


def test_a_material_item_gets_two_reviews_and_one_final_unreviewed_correction() -> None:
    outcome, ports = run(
        bounded(),
        brief(TOUCHED),
        [
            builder("b1"),
            review(ReviewDecision.CORRECT, "1"),
            review(ReviewDecision.PASS, "sign-in-1"),
            builder("b2"),
            two_findings("2"),
            review(ReviewDecision.PASS, "sign-in-2"),
            builder("b3"),
        ],
        [(TOUCHED,), (TOUCHED,), (TOUCHED,)],
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert seat_names(ports) == [
        "builder", "reviewer", "specialist", "builder", "reviewer", "specialist", "builder",
    ]
    combined = events(ports, "review.combined")
    assert len(combined) == 2
    close = ports.tracker.closed[0]
    assert close.accepted_at_round_limit is True
    assert close.verdict.evidence_ref == combined[1].evidence_refs[0]
    assert close.diff.workspace_revision == close.validation.workspace_revision == "revision-2"
    assert [proposal.title for proposal in ports.tracker.proposals] == [
        "Follow-up: item-1 BUG", "Follow-up: item-1 STYLE",
    ]
    assert "review lane: material" in trajectory(ports).splitlines()


def test_a_material_item_stays_material_when_a_correction_stops_touching_the_boundary() -> None:
    outcome, ports = run(
        bounded(),
        brief(UNTOUCHED),
        [
            builder("b1"),
            review(ReviewDecision.CORRECT, "1"),
            review(ReviewDecision.PASS, "sign-in"),
            builder("b2"),
            review(ReviewDecision.CORRECT, "2"),
            builder("b3"),
        ],
        [(TOUCHED,), (UNTOUCHED,), (UNTOUCHED,)],
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert seat_names(ports) == [
        "builder", "reviewer", "specialist", "builder", "reviewer", "builder",
    ]
    assert "review lane: material" in trajectory(ports).splitlines()


# --- resume -------------------------------------------------------------------------------


def test_a_resumed_small_item_at_its_correction_limit_gets_one_final_review() -> None:
    outcome, ports = run(
        bounded(),
        brief(UNTOUCHED),
        [review(ReviewDecision.CORRECT, "resumed")],
        [(UNTOUCHED,)],
        resume=ResumePlan("built", Path("/worktree"), 1, "run-old"),
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert seat_names(ports) == ["reviewer"]
    assert events(ports, "item.correcting") == []
    assert ports.tracker.closed[0].accepted_at_round_limit is True
    assert [proposal.title for proposal in ports.tracker.proposals] == ["Follow-up: item-1 finding"]
    assert "not reviewed again" not in ports.tracker.proposals[0].rationale
    assert "review lane: small" in trajectory(ports).splitlines()


# --- no lane ------------------------------------------------------------------------------


def _correct_every_round(process: ProcessPolicy, path: str):
    return run(
        process,
        brief(path),
        [
            builder("b1"), review(ReviewDecision.CORRECT, "1"),
            builder("b2"), review(ReviewDecision.CORRECT, "2"),
            builder("b3"), review(ReviewDecision.CORRECT, "3"),
        ],
        [(path,), (path,), (path,)],
    )


def test_flow_and_strict_without_a_map_keep_a_review_after_every_correction() -> None:
    for process in (bounded(ProcessMode.FLOW), strict_policy()):
        outcome, ports = _correct_every_round(process, TOUCHED)

        assert outcome.disposition is ItemDisposition.ACCEPTED
        assert seat_names(ports) == [
            "builder", "reviewer", "builder", "reviewer", "builder", "reviewer",
        ]
        assert len(events(ports, "item.correcting")) == 2
        assert ports.tracker.closed[0].verdict.evidence_ref == "verdict:3"


def test_flow_records_no_review_lane() -> None:
    outcome, ports = run(
        bounded(ProcessMode.FLOW),
        brief(TOUCHED),
        [builder(), review(ReviewDecision.PASS)],
        [(TOUCHED,)],
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert events(ports, "item.finalised")[0].payload["review_lane"] is None
    assert "review lane: none" in trajectory(ports).splitlines()


# --- a resumed finalised item -------------------------------------------------------------


def _resume_finalised(process: ProcessPolicy, brief_text: str):
    return run(
        process,
        brief_text,
        [],
        [()],
        resume=ResumePlan("finalised", Path("/worktree"), 0, "run-old"),
    )


def test_a_resumed_finalised_item_records_no_review_lane_in_flow() -> None:
    outcome, ports = _resume_finalised(bounded(ProcessMode.FLOW), brief(TOUCHED))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    (finalised,) = events(ports, "item.finalised")
    assert finalised.payload["resumed"] is True
    assert "review_lane" in finalised.payload
    assert finalised.payload["review_lane"] is None


def test_a_resumed_finalised_item_takes_its_lane_from_its_declared_paths() -> None:
    outcome, ports = _resume_finalised(bounded(), brief(TOUCHED))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    (finalised,) = events(ports, "item.finalised")
    assert finalised.payload["review_lane"] == "material"
    _, small = _resume_finalised(bounded(), brief(UNTOUCHED))
    assert events(small, "item.finalised")[0].payload["review_lane"] == "small"
