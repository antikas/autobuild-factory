"""The acceptance checklist a review seat is given and the depth it reports."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from test_review_boundaries import SIGN_IN, bounded, brief
from test_workflows import builder, campaign, command, make_ports, spec

from autobuild.application import CampaignRunner, ItemWorkflow
from autobuild.application.prompts import render_seat_instructions
from autobuild.domain import (
    ChangedPath,
    ChangeKind,
    ClauseCheck,
    ClauseOutcome,
    DiffEvidence,
    InspectedPath,
    ItemDisposition,
    ReviewDecision,
    ReviewFinding,
    ReviewVerdict,
    Seat,
    SeatOutcome,
    SeatResult,
    SeatUsage,
    WorkItem,
    review_verdict_rule_error,
)
from autobuild.enforcement.review_depth import (
    SHALLOW_CLAUSES,
    SHALLOW_PATHS,
    acceptance_clauses,
    review_depth,
)
from autobuild.enforcement.review_scope import boundary_findings, combined_verdict

ACCEPTANCE = (
    "## Acceptance\n\n"
    "- `total([2, 3])` returns 5.\n"
    "- The report lists the total,\n"
    "  with its unit.\n"
    "\n## Outside scope\n\n- Anything else.\n"
)
BRIEF = "# Change\n\n## Outcome\n\n- not a clause\n\n" + ACCEPTANCE
CHANGED = ("src/app/auth/login.py", "tests/test_login.py")
WORKTREE = Path("/worktree")


def verdict(
    inspected: tuple[str, ...] = (),
    checks: tuple[tuple[str, ClauseOutcome], ...] = (),
    decision: ReviewDecision = ReviewDecision.PASS,
    findings: tuple[ReviewFinding, ...] = (),
) -> ReviewVerdict:
    return ReviewVerdict(
        "item-1",
        decision,
        findings,
        "verdict",
        tuple(InspectedPath(path, "all") for path in inspected),
        tuple(ClauseCheck(clause, outcome, f"check:{clause}") for clause, outcome in checks),
    )


def seat_result(payload: ReviewVerdict, suffix: str) -> SeatResult:
    return SeatResult(
        f"review-{suffix}",
        SeatOutcome.SUCCEEDED,
        payload,
        f"raw:review:{suffix}",
        SeatUsage(source="fake"),
        "2026-11-19T10:00:00+00:00",
        "2026-11-19T10:00:47+00:00",
    )


def changed(*paths: str) -> tuple[ChangedPath, ...]:
    return tuple(ChangedPath(Path(path), ChangeKind.MODIFIED, "digest") for path in paths)


def depth_of(review: ReviewVerdict, numbers=("A1", "A2")):
    return review_depth(Seat.REVIEWER, None, 47.0, changed(*CHANGED), review, numbers, WORKTREE)


ALL_MET = (("A1", ClauseOutcome.MET), ("A2", ClauseOutcome.MET))

# --- the checklist ------------------------------------------------------------------------


def test_the_clause_reader_returns_the_acceptance_bullets_in_order() -> None:
    assert acceptance_clauses(BRIEF) == (
        "`total([2, 3])` returns 5.",
        "The report lists the total, with its unit.",
    )


def test_the_clause_reader_returns_nothing_without_the_heading() -> None:
    assert acceptance_clauses("# Change\n\n## Outcome\n\n- one\n") == ()


def _work(brief_text: str = BRIEF):
    return spec(brief_text=brief_text)


def test_the_reviewer_and_the_boundary_specialist_list_the_numbered_clauses() -> None:
    reviewer = render_seat_instructions(_work(), Seat.REVIEWER, ())
    specialist = render_seat_instructions(_work(), Seat.SPECIALIST, (), boundary=SIGN_IN)

    for instructions in (reviewer, specialist):
        assert "A1. `total([2, 3])` returns 5." in instructions
        assert "A2. The report lists the total, with its unit." in instructions
        assert "new tests included" in instructions
        assert "blocking finding" in instructions
        assert '"inspected"' in instructions and '"acceptance"' in instructions


def test_the_adjudicator_lists_no_clause_and_returns_both_arrays() -> None:
    adjudicator = render_seat_instructions(_work(), Seat.SPECIALIST, ())

    assert "A1" not in adjudicator.replace('"clause":"A1"', "")
    assert "Acceptance checklist" not in adjudicator
    assert '"inspected"' in adjudicator and '"acceptance"' in adjudicator


def test_a_brief_without_the_heading_numbers_the_tracker_acceptance_lines() -> None:
    work = replace(_work("# Change\n"), item=WorkItem("item-1", "t", "plan", ("passes", "ships")))

    instructions = render_seat_instructions(work, Seat.REVIEWER, ())

    assert "A1. passes" in instructions and "A2. ships" in instructions


# --- the verdict rule ---------------------------------------------------------------------


def test_a_pass_with_an_unmet_clause_is_refused_naming_it() -> None:
    error = review_verdict_rule_error(
        verdict(checks=(("A1", ClauseOutcome.MET), ("A2", ClauseOutcome.UNMET)))
    )

    assert error is not None and "A2" in error


def test_a_pass_with_every_clause_met_is_accepted() -> None:
    assert review_verdict_rule_error(verdict(checks=ALL_MET)) is None


# --- the depth decision -------------------------------------------------------------------


def test_a_review_of_every_changed_path_and_every_clause_is_ok() -> None:
    depth = depth_of(verdict(("./src/app/auth/login.py", "/worktree/tests/test_login.py"), ALL_MET))

    assert depth.shallow is False and depth.reasons == ()
    assert (depth.changed_paths, depth.inspected_changed_paths) == (2, 2)
    assert (depth.clauses, depth.clauses_checked) == (2, 2)
    assert depth.duration_seconds == 47.0


def test_a_review_that_inspected_fewer_changed_paths_is_shallow() -> None:
    depth = depth_of(verdict(("src/app/auth/login.py", "README.md"), ALL_MET))

    assert depth.shallow is True
    assert depth.reasons == (SHALLOW_PATHS,)
    assert depth.inspected_changed_paths == 1


def test_a_review_that_checked_no_clause_is_shallow() -> None:
    every_path = CHANGED
    for checks in (
        (),
        (("A1", ClauseOutcome.NOT_CHECKED),),
        (("A7", ClauseOutcome.MET), ("B1", ClauseOutcome.UNMET)),
    ):
        depth = depth_of(verdict(every_path, checks))

        assert depth.shallow is True
        assert depth.reasons == (SHALLOW_CLAUSES,)
        assert depth.clauses_checked == 0


def test_a_deleted_path_is_not_a_changed_path_to_inspect() -> None:
    paths = changed("src/a.py") + (ChangedPath(Path("src/old.py"), ChangeKind.DELETED, None),)

    depth = review_depth(
        Seat.REVIEWER, None, None, paths, verdict(("src/a.py",), ALL_MET), ("A1", "A2")
    )

    assert depth.changed_paths == 1 and depth.shallow is False


def test_a_specialist_keeps_its_entries_through_the_boundary_and_the_combination() -> None:
    blind = verdict(("src/app/auth/login.py",), (("A1", ClauseOutcome.MET),))
    specialist = boundary_findings(
        verdict(("tests/test_login.py",), (("A2", ClauseOutcome.MET),)), SIGN_IN
    )

    combined = combined_verdict((blind, specialist), "round")

    assert specialist.inspected == (InspectedPath("tests/test_login.py", "all"),)
    assert [entry.path for entry in combined.inspected] == list(CHANGED)
    assert [check.clause for check in combined.acceptance] == ["A1", "A2"]


# --- the run record and the report --------------------------------------------------------


def _ports(reviews: list[SeatResult]):
    ports = make_ports([builder(), *reviews], [command()], 1)
    workspace = ports.workspace.diffs[0].workspace
    ports.workspace.diffs[:] = [DiffEvidence(workspace, "revision-0", changed(*CHANGED), "patch:0")]
    return ports


def _events(ports, event_type: str):
    return [event for event in ports.records.events if event.event_type == event_type]


def test_the_run_record_and_the_outcome_carry_the_review_depth() -> None:
    full = verdict(CHANGED, ALL_MET)
    ports = _ports([seat_result(full, "1")])

    outcome = ItemWorkflow(ports).run(campaign(), _work(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    payload = _events(ports, "review.completed")[0].payload
    assert payload["duration_seconds"] == 47.0
    assert payload["inspected"] == [{"path": path, "lines": "all"} for path in CHANGED]
    assert payload["acceptance"][0] == {"clause": "A1", "outcome": "met", "evidence_ref": "check:A1"}
    assert payload["depth"] == {
        "changed_paths": 2,
        "inspected_changed_paths": 2,
        "clauses": 2,
        "clauses_checked": 2,
        "shallow": False,
        "reasons": [],
    }
    assert [(depth.seat, depth.shallow) for depth in outcome.review_depths] == [
        (Seat.REVIEWER, False)
    ]


def test_a_boundary_specialist_records_its_depth_under_its_boundary() -> None:
    ports = _ports(
        [
            seat_result(verdict(CHANGED, ALL_MET), "1"),
            seat_result(verdict(("src/app/auth/login.py",), ()), "sign-in"),
        ]
    )
    work = replace(_work(brief("src/app/auth/login.py") + ACCEPTANCE), process=bounded())

    outcome = ItemWorkflow(ports).run(campaign(), work, ports.records.create(campaign()))

    payload = _events(ports, "specialist.completed")[0].payload
    assert payload["boundary"] == "sign-in"
    assert payload["depth"]["shallow"] is True
    assert payload["depth"]["reasons"] == [SHALLOW_PATHS, SHALLOW_CLAUSES]
    assert [(depth.seat, depth.boundary) for depth in outcome.review_depths] == [
        (Seat.REVIEWER, None),
        (Seat.SPECIALIST, "sign-in"),
    ]


def test_a_parked_item_keeps_the_depth_of_its_review() -> None:
    finding = ReviewFinding("SCOPE", "out of scope", "f", blocking=True)
    parked = verdict((), (), ReviewDecision.PARK, (finding,))
    ports = _ports([seat_result(parked, "1")])

    outcome = ItemWorkflow(ports).run(campaign(), _work(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.PARKED
    assert [depth.shallow for depth in outcome.review_depths] == [True]


def test_a_failed_item_keeps_the_depth_of_its_review() -> None:
    ports = _ports([seat_result(verdict(CHANGED, ALL_MET), "1")])
    ports.workspace.delivery_parks.append(RuntimeError("delivery broke"))

    def park(*args, **kwargs):
        raise RuntimeError("tracker refused the park")

    ports.tracker.park = park

    outcome = ItemWorkflow(ports).run(campaign(), _work(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.FAILED
    assert "tracker refused the park" in outcome.reason
    assert [depth.seat for depth in outcome.review_depths] == [Seat.REVIEWER]


def test_the_report_shows_a_review_depth_row_after_seat_usage() -> None:
    ports = _ports([seat_result(verdict(("src/app/auth/login.py",), ALL_MET), "1")])
    ports.tracker.queue.append(WorkItem("item-1", "test item", "plan", ("passes",)))

    CampaignRunner(ports).run(campaign(), lambda selected: _work())

    content = ports.workspace.reports[0].content
    assert content.index("## Seat usage") < content.index("## Review depth")
    assert content.index("## Review depth") < content.index("## Run record")
    assert f"| item-1 | reviewer | 47.0 | 1 of 2 | 2 of 2 | shallow | {SHALLOW_PATHS} |" in content


def test_a_campaign_with_no_review_shows_none_under_review_depth() -> None:
    ports = make_ports([], [], diff_count=0)

    CampaignRunner(ports).run(campaign(), lambda selected: _work())

    section = ports.workspace.reports[0].content.split("## Review depth", 1)[1]
    assert section.split("##", 1)[0].count("| none |") == 1
