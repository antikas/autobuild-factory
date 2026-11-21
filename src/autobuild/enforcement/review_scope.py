"""Review effort that follows what a change touches.

Every review round has the one blind reviewer. In strict mode each boundary in
the profile's boundary map that the item's declared or changed paths touch adds
one specialist seat for that boundary, in map order. Flow mode ignores the map.
``review_seats`` is the one place that choice is made, and ``review_lane`` the
one place the review lane is chosen.
"""

from __future__ import annotations

from dataclasses import dataclass

from autobuild.domain import ReviewDecision, ReviewVerdict, Seat
from autobuild.domain.models import ProcessPolicy, ReviewBoundary, ReviewFinding, ReviewLane
from autobuild.enforcement.test_scope import path_matches


@dataclass(frozen=True, slots=True)
class ReviewSeat:
    """One review seat of a round: the blind reviewer, with no boundary, or a
    specialist for ``boundary``."""

    seat: Seat
    boundary: ReviewBoundary | None = None

    def __post_init__(self) -> None:
        if (self.seat is Seat.SPECIALIST) != (self.boundary is not None):
            raise ValueError("a boundary review seat is a specialist with a boundary")


BLIND_REVIEWER = ReviewSeat(Seat.REVIEWER)


def touched_boundaries(
    policy: ProcessPolicy, paths: tuple[str, ...]
) -> tuple[ReviewBoundary, ...]:
    """The boundaries any of ``paths`` touches, in map order."""

    return tuple(
        boundary
        for boundary in policy.boundaries
        if any(path_matches(pattern, path) for pattern in boundary.patterns for path in paths)
    )


def review_seats(policy: ProcessPolicy, paths: tuple[str, ...]) -> tuple[ReviewSeat, ...]:
    """The review seats for a change to ``paths``: the blind reviewer first,
    then in strict mode one specialist per boundary touched."""

    if not policy.strict:
        return (BLIND_REVIEWER,)
    return (BLIND_REVIEWER,) + tuple(
        ReviewSeat(Seat.SPECIALIST, boundary) for boundary in touched_boundaries(policy, paths)
    )


def review_lane(policy: ProcessPolicy, paths: tuple[str, ...]) -> ReviewLane | None:
    """The review lane for a change to ``paths``: ``material`` when strict and
    a path touches a boundary, ``small`` when strict with a boundary map and no
    path touches one, and no lane in flow mode or with an empty map."""

    if not policy.strict or not policy.boundaries:
        return None
    if touched_boundaries(policy, paths):
        return ReviewLane.MATERIAL
    return ReviewLane.SMALL


_SEVERITY = (
    ReviewDecision.PASS,
    ReviewDecision.CORRECT,
    ReviewDecision.ESCALATE,
    ReviewDecision.PARK,
)


def boundary_findings(verdict: ReviewVerdict, boundary: ReviewBoundary) -> ReviewVerdict:
    """A boundary specialist's verdict with each finding that names no
    boundary stamped with ``boundary``."""

    findings = tuple(
        finding
        if finding.specialist_boundary is not None
        else ReviewFinding(
            finding.code,
            finding.consequence,
            finding.evidence_ref,
            finding.blocking,
            boundary.name,
        )
        for finding in verdict.findings
    )
    return ReviewVerdict(
        verdict.item_id,
        verdict.decision,
        findings,
        verdict.evidence_ref,
        verdict.inspected,
        verdict.acceptance,
    )


def combined_verdict(verdicts: tuple[ReviewVerdict, ...], evidence_ref: str) -> ReviewVerdict:
    """One round's verdict from every review seat's verdict: the most severe
    decision (park, then escalate, then correct, then pass) and every finding,
    in seat order, under ``evidence_ref``. Every seat's inspected paths and
    clause checks are kept, in seat order."""

    if not verdicts:
        raise ValueError("a review round has at least one verdict")
    decision = max((verdict.decision for verdict in verdicts), key=_SEVERITY.index)
    findings = tuple(finding for verdict in verdicts for finding in verdict.findings)
    inspected = tuple(entry for verdict in verdicts for entry in verdict.inspected)
    acceptance = tuple(check for verdict in verdicts for check in verdict.acceptance)
    return ReviewVerdict(
        verdicts[0].item_id, decision, findings, evidence_ref, inspected, acceptance
    )
