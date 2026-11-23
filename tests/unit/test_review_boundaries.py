"""Review effort that follows what a change touches.

The profile's ``[process.boundaries]`` map names sensitive areas. In strict
mode an item whose declared or changed paths touch a boundary gets one
specialist seat per boundary touched, on top of the one blind reviewer. Flow
mode ignores the map."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_process_mode import BASE, STRICT, documented_profiles, load, strict_policy
from test_workflows import builder, campaign, command, make_ports, review, spec

from autobuild.application import ItemWorkflow
from autobuild.application import item as item_module
from autobuild.application.prompts import BUILDER_REPORT_SENTENCE, NO_BUILDER_REPORT_SENTENCE
from autobuild.bootstrap.profile import ConfigurationError
from autobuild.domain import (
    ChangedPath,
    ChangeKind,
    DiffEvidence,
    ItemDisposition,
    ReviewDecision,
    ReviewVerdict,
    Seat,
)
from autobuild.domain.models import (
    ProcessMode,
    ProcessPolicy,
    ResumePlan,
    ReviewBoundary,
    ReviewFinding,
)
from autobuild.enforcement.review_scope import BLIND_REVIEWER, ReviewSeat, review_seats

SIGN_IN = ReviewBoundary("sign-in", ("src/app/auth/**",), "docs/invariants/sign-in.md")
PAYMENTS = ReviewBoundary("payments", ("src/app/billing/**", "src/app/checkout/*.py"), None)
SCHEMA = ReviewBoundary("schema", ("migrations/**",), "docs/invariants/schema.md")

BOUNDARIES = """
[process.boundaries.sign-in]
paths = ["src/app/auth/**"]
invariants = "docs/invariants/sign-in.md"

[process.boundaries.payments]
paths = ["src/app/billing/**", "src/app/checkout/*.py"]

[process.boundaries.schema]
paths = ["migrations/**"]
invariants = "docs/invariants/schema.md"
"""


def bounded(mode: ProcessMode = ProcessMode.STRICT) -> ProcessPolicy:
    return replace(strict_policy(), mode=mode, boundaries=(SIGN_IN, PAYMENTS, SCHEMA))


def brief(*paths: str) -> str:
    return "# Change\n\n## Declared paths\n\n" + "\n".join(f"`{path}`" for path in paths) + "\n"


# --- the profile map -------------------------------------------------------------------


def test_a_profile_without_a_boundary_map_has_no_boundaries(tmp_path: Path) -> None:
    assert load(tmp_path, BASE + STRICT).process.boundaries == ()


def test_the_boundary_map_loads_in_map_order(tmp_path: Path) -> None:
    settings = load(tmp_path, BASE + STRICT + BOUNDARIES)

    assert settings.process.boundaries == (SIGN_IN, PAYMENTS, SCHEMA)


def test_the_boundary_map_loads_and_is_checked_in_flow(tmp_path: Path) -> None:
    profile = BASE + STRICT.replace('mode = "strict"', 'mode = "flow"') + BOUNDARIES

    assert load(tmp_path, profile).process.boundaries == (SIGN_IN, PAYMENTS, SCHEMA)
    with pytest.raises(ConfigurationError):
        load(tmp_path, profile + '\n[process.boundaries.pii]\npaths = []\n')


def test_the_documented_boundary_example_loads(tmp_path: Path) -> None:
    examples = documented_profiles(
        tmp_path, lambda profile: "boundaries" in profile.get("process", {})
    )

    assert [example.process.boundaries for example in examples] == [
        (ReviewBoundary("payments", ("src/payments/*.py",), "docs/invariants/payments.md"),)
    ]


MALFORMED = [
    (
        '[process]\nboundaries = ["sign-in"]\n',
        "[process.boundaries] must be a table of named boundaries.",
    ),
    (
        '[process.boundaries]\nsign-in = "src/auth/**"\n',
        "process.boundaries.sign-in must be a table with paths and an optional invariants file.",
    ),
    (
        '[process.boundaries.sign-in]\npaths = ["src/auth/**"]\nowner = "kim"\n',
        "process.boundaries.sign-in contains unknown fields: owner",
    ),
    (
        '[process.boundaries.sign-in]\ninvariants = "docs/sign-in.md"\n',
        "process.boundaries.sign-in.paths must be a non-empty list of path pattern strings.",
    ),
    (
        "[process.boundaries.sign-in]\npaths = []\n",
        "process.boundaries.sign-in.paths must be a non-empty list of path pattern strings.",
    ),
    (
        '[process.boundaries.sign-in]\npaths = "src/auth/**"\n',
        "process.boundaries.sign-in.paths must be a non-empty list of path pattern strings.",
    ),
    (
        '[process.boundaries.sign-in]\npaths = ["src/auth/**", 3]\n',
        "process.boundaries.sign-in.paths must be a non-empty list of path pattern strings.",
    ),
    (
        '[process.boundaries.sign-in]\npaths = ["src/auth/**"]\ninvariants = ["a.md"]\n',
        "process.boundaries.sign-in.invariants must be the path of a file, as a string.",
    ),
    (
        '[process.boundaries.sign-in]\npaths = ["src/auth/**"]\ninvariants = ""\n',
        "process.boundaries.sign-in.invariants must be the path of a file, as a string.",
    ),
    (
        '[process.boundaries." "]\npaths = ["src/auth/**"]\n',
        "Every boundary in [process.boundaries] needs a name that is not blank.",
    ),
    (
        '[process.boundaries.""]\npaths = ["src/auth/**"]\n',
        "Every boundary in [process.boundaries] needs a name that is not blank.",
    ),
]


@pytest.mark.parametrize(("table", "sentence"), MALFORMED, ids=[case[1] for case in MALFORMED])
def test_a_malformed_boundary_map_is_refused_with_one_sentence(
    tmp_path: Path, table: str, sentence: str
) -> None:
    profile = BASE + "\n" + table if table.startswith("[process]\n") else BASE + STRICT + "\n" + table

    with pytest.raises(ConfigurationError) as refusal:
        load(tmp_path, profile)

    assert str(refusal.value) == sentence


# --- the choice of review seats -------------------------------------------------------------


def test_no_map_gives_the_one_blind_reviewer() -> None:
    assert review_seats(strict_policy(), ("src/app/auth/login.py",)) == (BLIND_REVIEWER,)
    assert review_seats(ProcessPolicy(), ("src/app/auth/login.py",)) == (BLIND_REVIEWER,)


def test_flow_ignores_the_map() -> None:
    paths = ("src/app/auth/login.py", "src/app/billing/charge.py")

    assert review_seats(bounded(ProcessMode.FLOW), paths) == (BLIND_REVIEWER,)


def test_strict_with_no_boundary_touched_gives_one_reviewer() -> None:
    assert review_seats(bounded(), ("src/app/profile/loader.py", "README.md")) == (BLIND_REVIEWER,)


def test_one_boundary_touched_gives_one_specialist() -> None:
    assert review_seats(bounded(), ("src/app/auth/login.py", "src/app/profile/x.py")) == (
        BLIND_REVIEWER,
        ReviewSeat(Seat.SPECIALIST, SIGN_IN),
    )


def test_two_boundaries_touched_give_two_specialists_in_map_order() -> None:
    seats = review_seats(bounded(), ("migrations/0004_add_column.sql", "src/app/checkout/cart.py"))

    assert seats == (
        BLIND_REVIEWER,
        ReviewSeat(Seat.SPECIALIST, PAYMENTS),
        ReviewSeat(Seat.SPECIALIST, SCHEMA),
    )
    assert [(seat.boundary.name, seat.boundary.invariants) for seat in seats[1:]] == [
        ("payments", None),
        ("schema", "docs/invariants/schema.md"),
    ]


def test_a_boundary_touched_by_several_paths_gives_one_specialist() -> None:
    seats = review_seats(bounded(), ("src/app/auth/a.py", "src/app/auth/b.py"))

    assert seats == (BLIND_REVIEWER, ReviewSeat(Seat.SPECIALIST, SIGN_IN))


def test_a_one_segment_pattern_does_not_reach_into_subdirectories() -> None:
    assert review_seats(bounded(), ("src/app/checkout/deep/cart.py",)) == (BLIND_REVIEWER,)


# --- the workflow -------------------------------------------------------------------------


def run(
    process: ProcessPolicy,
    brief_text: str,
    reviews: list,
    changed: tuple[str, ...] = (),
    rounds: int = 1,
):
    ports = make_ports([builder(), *reviews], [command(suffix=str(n)) for n in range(rounds)], rounds)
    if changed:
        workspace = ports.workspace.diffs[0].workspace
        ports.workspace.diffs[:] = [
            DiffEvidence(
                workspace,
                "revision-0",
                tuple(ChangedPath(Path(path), ChangeKind.MODIFIED, "digest") for path in changed),
                "patch:0",
            )
        ]
    work = replace(spec(brief_text=brief_text), process=process)
    outcome = ItemWorkflow(ports).run(campaign(), work, ports.records.create(campaign()))
    return outcome, ports


def seat_names(ports) -> list[str]:
    return [request.seat.value for request in ports.harness.requests]


def test_flow_reviews_as_today_even_with_a_map_touched() -> None:
    outcome, ports = run(
        bounded(ProcessMode.FLOW), brief("src/app/auth/login.py"), [review(ReviewDecision.PASS)]
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert seat_names(ports) == ["builder", "reviewer"]
    assert not [event for event in ports.records.events if event.event_type == "review.combined"]


def test_strict_with_no_boundary_touched_runs_the_one_reviewer() -> None:
    outcome, ports = run(bounded(), brief("src/app/profile/loader.py"), [review(ReviewDecision.PASS)])

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert seat_names(ports) == ["builder", "reviewer"]


def test_strict_runs_one_specialist_per_boundary_with_its_name_and_invariants_file() -> None:
    outcome, ports = run(
        bounded(),
        brief("src/app/auth/login.py", "migrations/0004.sql"),
        [
            review(ReviewDecision.PASS),
            review(ReviewDecision.PASS, "sign-in"),
            review(ReviewDecision.PASS, "schema"),
        ],
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert seat_names(ports) == ["builder", "reviewer", "specialist", "specialist"]
    _, reviewer, sign_in, schema = ports.harness.requests
    assert "Specialist review" not in reviewer.instructions
    assert "Specialist review for the sign-in boundary." in sign_in.instructions
    assert "Read the invariants file docs/invariants/sign-in.md first" in sign_in.instructions
    assert "docs/invariants/sign-in.md" in sign_in.context_refs
    assert "Specialist review for the schema boundary." in schema.instructions
    assert "docs/invariants/schema.md" in schema.instructions
    assert "sign-in" not in schema.instructions
    assert sign_in.test_guard is None and schema.test_guard is None
    completed = [e for e in ports.records.events if e.event_type == "specialist.completed"]
    assert [event.payload["boundary"] for event in completed] == ["sign-in", "schema"]


def test_a_boundary_specialist_gets_the_builder_report_its_blind_reviewer_got() -> None:
    _, ports = run(
        bounded(),
        brief("src/app/auth/login.py"),
        [review(ReviewDecision.PASS), review(ReviewDecision.PASS, "sign-in")],
    )

    _, reviewer, sign_in = ports.harness.requests
    report = reviewer.context_refs[2]
    assert "builder-report" in report
    assert report in sign_in.context_refs
    assert "docs/invariants/sign-in.md" in sign_in.context_refs
    assert "raw:builder" not in sign_in.context_refs
    assert BUILDER_REPORT_SENTENCE in sign_in.instructions
    assert report in sign_in.instructions
    assert NO_BUILDER_REPORT_SENTENCE not in sign_in.instructions


def test_a_boundary_without_an_invariants_file_says_so() -> None:
    _, ports = run(
        bounded(),
        brief("src/app/billing/charge.py"),
        [review(ReviewDecision.PASS), review(ReviewDecision.PASS, "payments")],
    )

    payments = ports.harness.requests[-1]
    assert "Specialist review for the payments boundary." in payments.instructions
    assert "No invariants file is named for this boundary" in payments.instructions


def test_a_changed_path_touches_a_boundary_the_brief_did_not_declare() -> None:
    _, ports = run(
        bounded(),
        brief("src/app/profile/loader.py"),
        [review(ReviewDecision.PASS), review(ReviewDecision.PASS, "sign-in")],
        changed=("src/app/auth/session.py",),
    )

    assert seat_names(ports) == ["builder", "reviewer", "specialist"]
    assert "sign-in boundary" in ports.harness.requests[-1].instructions


def test_the_combined_verdict_is_checked_by_the_verdict_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(verdicts, evidence_ref):
        finding = ReviewFinding("BUG", "wrong output", "finding:bug", blocking=True)
        return ReviewVerdict(verdicts[0].item_id, ReviewDecision.PASS, (finding,), evidence_ref)

    monkeypatch.setattr(item_module, "combined_verdict", broken)

    outcome, ports = run(
        bounded(),
        brief("src/app/auth/login.py"),
        [review(ReviewDecision.PASS), review(ReviewDecision.PASS, "sign-in")],
    )

    assert outcome.disposition is ItemDisposition.PARKED
    assert "pass verdict must not carry a blocking finding" in (outcome.reason or "")
    assert not [e for e in ports.records.events if e.event_type == "review.combined"]
    assert ports.tracker.closed == []


def test_a_specialist_correction_starts_a_correction_round_with_the_combined_evidence() -> None:
    outcome, ports = run(
        bounded(),
        brief("src/app/auth/login.py"),
        [
            review(ReviewDecision.PASS),
            review(ReviewDecision.CORRECT, "sign-in"),
            builder("builder-2"),
            review(ReviewDecision.PASS, "2"),
            review(ReviewDecision.PASS, "sign-in-2"),
        ],
        rounds=2,
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert seat_names(ports) == [
        "builder", "reviewer", "specialist", "builder", "reviewer", "specialist",
    ]
    combined = [e for e in ports.records.events if e.event_type == "review.combined"]
    assert [event.payload["decision"] for event in combined] == ["correct", "pass"]
    assert combined[0].payload["boundaries"] == ["sign-in"]
    assert ports.harness.requests[3].context_refs == combined[0].evidence_refs


def test_the_combined_evidence_of_the_first_review_is_named_round_one() -> None:
    outcome, ports = run(
        bounded(),
        brief("src/app/auth/login.py"),
        [review(ReviewDecision.PASS), review(ReviewDecision.PASS, "sign-in")],
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    combined = [e for e in ports.records.events if e.event_type == "review.combined"]
    item_id = spec().item.item_id
    assert [event.evidence_refs[0] for event in combined] == [
        f"evidence:{item_id}-review-round-1"
    ]


def test_a_resumed_item_past_the_build_keeps_its_marker_round_count() -> None:
    ports = make_ports(
        [review(ReviewDecision.PASS), review(ReviewDecision.PASS, "sign-in")],
        [command()],
        1,
    )
    work = replace(spec(brief_text=brief("src/app/auth/login.py")), process=bounded())

    outcome = ItemWorkflow(ports).run(
        campaign(),
        work,
        ports.records.create(campaign()),
        resume=ResumePlan("built", Path("/worktree"), 1, "run-old"),
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert seat_names(ports) == ["reviewer", "specialist"]
    combined = [e for e in ports.records.events if e.event_type == "review.combined"]
    item_id = spec().item.item_id
    assert [event.evidence_refs[0] for event in combined] == [
        f"evidence:{item_id}-review-round-2"
    ]
    assert f"{item_id}-review-round-1" not in ports.records.evidence


def test_the_review_after_one_correction_is_named_round_two() -> None:
    _, ports = run(
        bounded(),
        brief("src/app/auth/login.py"),
        [
            review(ReviewDecision.PASS),
            review(ReviewDecision.CORRECT, "sign-in"),
            builder("builder-2"),
            review(ReviewDecision.PASS, "2"),
            review(ReviewDecision.PASS, "sign-in-2"),
        ],
        rounds=2,
    )

    combined = [e for e in ports.records.events if e.event_type == "review.combined"]
    item_id = spec().item.item_id
    assert [event.evidence_refs[0] for event in combined] == [
        f"evidence:{item_id}-review-round-1",
        f"evidence:{item_id}-review-round-2",
    ]
