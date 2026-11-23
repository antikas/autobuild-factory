from __future__ import annotations

import json
from datetime import datetime, timedelta
from dataclasses import replace
from pathlib import Path

import pytest

from autobuild.adapters import LocalRunRecordAdapter
from autobuild.application import CampaignRunner, ItemWorkflow, Lane, WorkflowPorts, prompts
from autobuild.application.progress import render_progress_line
from autobuild.domain import (
    AdapterError,
    AdapterIdentity,
    BuilderReport,
    CampaignRef,
    CampaignSelection,
    CampaignStopReason,
    ChangeKind,
    ChangedPath,
    CommandResult,
    DiffEvidence,
    DeliveryMode,
    EffortLevel,
    EvidenceError,
    FogRecord,
    FollowUpRecord,
    ItemDisposition,
    ItemExecutionSpec,
    ItemOutcome,
    ItemState,
    LaneSignal,
    LaneSignalKind,
    PhaseMarker,
    Proposal,
    RefillPlan,
    ResumePlan,
    ReviewDecision,
    ReviewFinding,
    ReviewVerdict,
    RunEvent,
    RunRecordRef,
    Seat,
    SeatObservation,
    SeatOutcome,
    SeatResult,
    SeatUsage,
    SnapshotFile,
    ToolPolicy,
    WorkItem,
    WorkspaceRef,
    WorktreeSnapshot,
    WorktreeStatus,
)
from autobuild.domain import ClaimTaken, ScopeFenceViolation, TrackerPublication, TrackerStop
from autobuild.domain.models import (
    BriefDefect,
    BriefDefectCause,
    BuilderContinuation,
    raised_effort,
)
from autobuild.enforcement import ScopedTrackerPort
from autobuild.domain import (
    CampaignContext,
    LeaseHeld,
    LeaseRecord,
    LeaseSurface,
    SurfaceKind,
)
from autobuild.testing import (
    FakeCommandAdapter,
    FakeHarnessAdapter,
    FakeKnowledgeAdapter,
    FakeLaneStateAdapter,
    FakeLeaseAdapter,
    FakeProgressPort,
    FakeRunRecordAdapter,
    FakeTrackerAdapter,
    FakeWorkspaceAdapter,
)
from test_brief_git_fixtures import EXCERPT_C


def identity(name: str) -> AdapterIdentity:
    return AdapterIdentity(name, "1", frozenset({"test"}))


def builder(run: str = "builder") -> SeatResult:
    return SeatResult(
        run,
        SeatOutcome.SUCCEEDED,
        BuilderReport(f"report:{run}", "built"),
        f"raw:{run}",
        SeatUsage(source="fake"),
        "start",
        "end",
    )


def review(decision: ReviewDecision, suffix: str = "1") -> SeatResult:
    findings = ()
    if decision is not ReviewDecision.PASS:
        findings = (
            ReviewFinding("finding", "material consequence", f"finding:{suffix}", blocking=True),
        )
    return SeatResult(
        f"review-{suffix}",
        SeatOutcome.SUCCEEDED,
        ReviewVerdict("item-1", decision, findings, f"verdict:{suffix}"),
        f"raw:review:{suffix}",
        SeatUsage(source="fake"),
        "start",
        "end",
    )


def command(exit_code: int = 0, suffix: str = "1") -> CommandResult:
    return CommandResult(f"command:{suffix}", exit_code, f"stdout:{suffix}", f"stderr:{suffix}", "start", "end")


def make_ports(harness_results: list[SeatResult], command_results: list[CommandResult], diff_count: int = 1):
    workspace_ref = WorkspaceRef(Path("/worktree"), "run", "base", "lease")
    tracker = FakeTrackerAdapter(identity("tracker"))
    workspace = FakeWorkspaceAdapter(
        identity("workspace"),
        workspace=workspace_ref,
        diffs=[DiffEvidence(workspace_ref, f"revision-{index}", (), f"patch:{index}") for index in range(diff_count)],
    )
    harness = FakeHarnessAdapter(identity("harness"), scripted_results=list(harness_results))
    commands = FakeCommandAdapter(identity("command"), scripted_results=list(command_results))
    records = FakeRunRecordAdapter(identity("records"))
    knowledge = FakeKnowledgeAdapter(identity("knowledge"))
    lease = FakeLeaseAdapter(identity("lease"))
    return WorkflowPorts(
        tracker, workspace, harness, commands, records, knowledge, lease=lease
    )


def item() -> WorkItem:
    return WorkItem("item-1", "test item", "plan", ("passes",))


def spec(
    work_item: WorkItem | None = None,
    max_corrections: int = 2,
    brief_text: str = "",
) -> ItemExecutionSpec:
    return ItemExecutionSpec(
        work_item or item(),
        Path("brief.md"),
        "validator",
        ("python", "-m", "pytest", "tests/unit/test_workflows.py"),
        ToolPolicy(frozenset({"python"}), (Path("/worktree"),)),
        "builder-class",
        "reviewer-class",
        "specialist-class",
        brief_text=brief_text,
        max_corrections=max_corrections,
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        delivery_target_branch="main",
        delivery_target_revision="base",
    )


def campaign(
    *,
    refill_enabled: bool = False,
    max_items: int = 1,
    selection: CampaignSelection = CampaignSelection(),
) -> CampaignRef:
    return CampaignRef(
        "campaign",
        Path("/repo"),
        max_items=max_items,
        refill_enabled=refill_enabled,
        selection=selection,
    )


def test_item_accepts_only_after_build_validation_and_review() -> None:
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1)
    record = ports.records.create(campaign())

    outcome = ItemWorkflow(ports).run(campaign(), spec(), record)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert outcome.states == (
        ItemState.READY,
        ItemState.VERIFIED,
        ItemState.CLAIMED,
        ItemState.ISOLATED,
        ItemState.BUILT,
        ItemState.VALIDATED,
        ItemState.REVIEWED,
        ItemState.FINALISED,
        ItemState.RELEASED,
    )
    assert [request.seat.value for request in ports.harness.requests] == ["builder", "reviewer"]
    reviewer_refs = ports.harness.requests[1].context_refs
    assert reviewer_refs[:2] == ("patch:0", "stdout:1") and len(reviewer_refs) == 3
    assert ports.harness.requests[1].tool_policy.allowed_tools == frozenset({"read"})
    assert ports.harness.requests[0].brief_path == Path("/worktree/brief.md")
    assert ports.harness.requests[0].tool_policy.allowed_roots == (Path("/worktree"),)
    assert ports.harness.requests[1].tool_policy.allowed_roots == (Path("/worktree"),)
    assert "Do not ask for or use the builder transcript" in ports.harness.requests[1].instructions
    assert len(ports.tracker.closed) == 1
    assert ports.workspace.released == ["lease"]


def test_external_brief_root_remains_readable_from_the_isolated_workspace() -> None:
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1)
    configured = spec()
    external = Path("/shared-briefs")
    configured = replace(
        configured,
        brief_path=external / "item.md",
        tool_policy=ToolPolicy(
            configured.tool_policy.allowed_tools,
            (Path("/repo"), external),
        ),
        reviewer_tool_policy=ToolPolicy(
            frozenset({"read"}),
            (Path("/repo"), external),
        ),
    )

    outcome = ItemWorkflow(ports).run(
        campaign(), configured, ports.records.create(campaign())
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert ports.harness.requests[0].brief_path == external / "item.md"
    assert ports.harness.requests[0].tool_policy.allowed_roots == (
        Path("/worktree"),
        external,
    )
    assert ports.harness.requests[1].tool_policy.allowed_roots == (
        Path("/worktree"),
        external,
    )


def test_material_finding_gets_one_fresh_correction_then_clean_review() -> None:
    ports = make_ports(
        [builder("first"), review(ReviewDecision.CORRECT, "first"), builder("fold"), review(ReviewDecision.PASS, "clean")],
        [command(suffix="first"), command(suffix="fold")],
        diff_count=2,
    )

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert ItemState.CORRECTING in outcome.states
    assert ports.harness.requests[2].context_refs == ("verdict:first",)
    assert len(ports.command.requests) == 2


def _final_correct(suffix: str) -> SeatResult:
    verdict = ReviewVerdict(
        "item-1",
        ReviewDecision.CORRECT,
        (
            ReviewFinding("BUG", "wrong output", f"finding:bug:{suffix}", blocking=True),
            ReviewFinding("STYLE", "rename for clarity", f"finding:style:{suffix}", blocking=False),
        ),
        f"verdict:{suffix}",
    )
    return _review_result(verdict, f"review-{suffix}")


def _assert_round_limit_follow_ups(ports, outcome, final: str) -> None:
    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert len(ports.tracker.closed) == 1
    close = ports.tracker.closed[0]
    assert close.accepted_at_round_limit is True
    assert close.verdict.evidence_ref == f"verdict:{final}"
    assert outcome.finalise is not None
    assert outcome.finalise.item_commit == "item-commit"
    titles = [proposal.title for proposal in ports.tracker.proposals]
    assert titles == ["Follow-up: item-1 BUG", "Follow-up: item-1 STYLE"]
    assert tuple(record.proposal for record in outcome.follow_ups) == tuple(ports.tracker.proposals)
    blocking, non_blocking = ports.tracker.proposals
    assert "open at the round limit" in blocking.rationale
    assert "open at the round limit" not in non_blocking.rationale
    assert f"verdict:{final}" in blocking.rationale
    assert ports.tracker.parked == []
    trajectory = ports.records.evidence["item-1-trajectory"]
    assert "accepted at the round limit with 1 open blocking finding(s)" in trajectory


def test_a_green_item_at_the_round_limit_merges_and_registers_its_findings() -> None:
    ports = make_ports(
        [
            builder("initial"), review(ReviewDecision.CORRECT, "1"),
            builder("fold-1"), review(ReviewDecision.CORRECT, "2"),
            builder("fold-2"), _final_correct("3"),
        ],
        [command(suffix="1"), command(suffix="2"), command(suffix="3")],
        diff_count=3,
    )
    ports.tracker.queue.append(item())

    campaign_outcome = CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    (outcome,) = campaign_outcome.items
    _assert_round_limit_follow_ups(ports, outcome, "3")
    assert len(ports.harness.requests) == 6
    assert ports.workspace.released == ["lease"]
    content = ports.workspace.reports[0].content
    shipped = content[content.index("## Shipped") : content.index("## Parked")]
    follow_ups = content[content.index("## Follow-ups") :]
    assert "- item-1 test item" in shipped
    assert "### proposal-1 Follow-up: item-1 BUG" in follow_ups
    assert "### proposal-2 Follow-up: item-1 STYLE" in follow_ups


def test_a_specialist_correct_at_the_ceiling_merges_with_its_follow_ups() -> None:
    specialist = _final_correct("specialist")
    ports = make_ports(
        [
            builder("initial"), review(ReviewDecision.CORRECT, "1"),
            builder("fold-1"), review(ReviewDecision.CORRECT, "2"),
            builder("fold-2"), review(ReviewDecision.ESCALATE, "3"),
            specialist,
        ],
        [command(suffix="1"), command(suffix="2"), command(suffix="3")],
        diff_count=3,
    )

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    _assert_round_limit_follow_ups(ports, outcome, "specialist")
    assert ports.harness.requests[-1].seat is Seat.SPECIALIST


def test_a_review_still_correct_after_the_specialist_round_merges_with_its_follow_ups() -> None:
    ports = make_ports(
        [
            builder("initial"), review(ReviewDecision.ESCALATE, "1"),
            review(ReviewDecision.CORRECT, "specialist"),
            builder("fold-1"), _final_correct("2"),
        ],
        [command(suffix="1"), command(suffix="2")],
        diff_count=2,
    )

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    _assert_round_limit_follow_ups(ports, outcome, "2")
    assert [request.seat for request in ports.harness.requests] == [
        Seat.BUILDER, Seat.REVIEWER, Seat.SPECIALIST, Seat.BUILDER, Seat.REVIEWER
    ]


def test_a_red_validator_on_the_final_round_still_parks() -> None:
    ports = make_ports(
        [
            builder("initial"), review(ReviewDecision.CORRECT, "1"),
            builder("fold-1"), review(ReviewDecision.CORRECT, "2"),
            builder("fold-2"),
        ],
        [command(suffix="1"), command(suffix="2"), command(exit_code=1, suffix="3")],
        diff_count=3,
    )

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.PARKED
    assert "did not pass" in (outcome.reason or "")
    assert ports.tracker.closed == []
    assert ports.tracker.proposals == []
    assert outcome.finalise is None or outcome.finalise.item_commit is None


def test_a_first_round_pass_does_not_name_the_round_limit() -> None:
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1)

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert len(ports.tracker.closed) == 1
    assert ports.tracker.proposals == []
    trajectory = ports.records.evidence["item-1-trajectory"]
    assert "accepted after 0 correction round(s)" in trajectory
    assert "round limit" not in trajectory


def test_specialist_is_added_only_after_an_escalation() -> None:
    ports = make_ports(
        [builder(), review(ReviewDecision.ESCALATE), review(ReviewDecision.PASS, "specialist")],
        [command()],
        diff_count=1,
    )

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert [request.seat.value for request in ports.harness.requests] == ["builder", "reviewer", "specialist"]


@pytest.mark.parametrize("seat_outcome", [SeatOutcome.FAILED, SeatOutcome.CANCELLED])
def test_a_failed_escalation_specialist_parks_as_a_seat_failure(
    seat_outcome: SeatOutcome,
) -> None:
    failed = SeatResult("specialist", seat_outcome, None, "raw:s", SeatUsage(), "start", "end")
    ports = make_ports(
        [builder(), review(ReviewDecision.ESCALATE), failed], [command()], diff_count=1
    )

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.reason == f"specialist ended {seat_outcome.value}"
    parked = next(event for event in ports.records.events if event.event_type == "item.parked")
    assert parked.payload["kill_kind"] == seat_outcome.value


def test_a_succeeded_escalation_specialist_without_a_verdict_names_the_missing_verdict() -> None:
    empty = SeatResult("specialist", SeatOutcome.SUCCEEDED, None, "raw:s", SeatUsage(), "start", "end")
    ports = make_ports(
        [builder(), review(ReviewDecision.ESCALATE), empty], [command()], diff_count=1
    )

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.PARKED
    assert "typed ReviewVerdict" in outcome.reason


def _review_result(verdict: ReviewVerdict, run: str = "review-1") -> SeatResult:
    return SeatResult(
        run,
        SeatOutcome.SUCCEEDED,
        verdict,
        f"raw:{run}",
        SeatUsage(source="fake"),
        "start",
        "end",
    )


def test_pass_with_non_blocking_findings_accepts_and_proposes_follow_ups() -> None:
    verdict = ReviewVerdict(
        "item-1",
        ReviewDecision.PASS,
        (
            ReviewFinding("STYLE", "rename for clarity", "finding:style", blocking=False),
            ReviewFinding("DOCS", "note the tradeoff", "finding:docs", blocking=False),
        ),
        "verdict:pass",
    )
    ports = make_ports([builder(), _review_result(verdict)], [command()], diff_count=1)

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert len(ports.tracker.closed) == 1
    titles = [proposal.title for proposal in ports.tracker.proposals]
    assert titles == ["Follow-up: item-1 STYLE", "Follow-up: item-1 DOCS"]
    assert [(record.proposal_id, record.proposal.title) for record in outcome.follow_ups] == [
        ("proposal-1", "Follow-up: item-1 STYLE"),
        ("proposal-2", "Follow-up: item-1 DOCS"),
    ]
    for proposal in ports.tracker.proposals:
        assert proposal.brief_ref == "docs/campaigns/campaign.md"
        assert "verdict:pass" in proposal.rationale
    review_event = next(
        event for event in ports.records.events if event.event_type == "review.completed"
    )
    assert review_event.payload["non_blocking_finding_codes"] == ["STYLE", "DOCS"]
    assert review_event.payload["blocking_finding_codes"] == []


def test_correct_with_only_non_blocking_findings_parks_with_that_reason() -> None:
    verdict = ReviewVerdict(
        "item-1",
        ReviewDecision.CORRECT,
        (ReviewFinding("NIT", "tiny quibble", "finding:nit", blocking=False),),
        "verdict:correct",
    )
    ports = make_ports([builder(), _review_result(verdict)], [command()], diff_count=1)

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.PARKED
    assert "correct verdict requires at least one blocking finding" in (outcome.reason or "")
    assert ports.tracker.proposals == []


def test_pass_with_a_blocking_finding_is_rejected() -> None:
    verdict = ReviewVerdict(
        "item-1",
        ReviewDecision.PASS,
        (ReviewFinding("BUG", "wrong output", "finding:bug", blocking=True),),
        "verdict:bad-pass",
    )
    ports = make_ports([builder(), _review_result(verdict)], [command()], diff_count=1)

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.PARKED
    assert "pass verdict must not carry a blocking finding" in (outcome.reason or "")
    assert ports.tracker.closed == []


def test_a_builder_timeout_hands_the_item_back_with_its_worktree_kept() -> None:
    timed_out = SeatResult("builder", SeatOutcome.TIMED_OUT, None, "raw", SeatUsage(), "start", "end")
    ports = make_ports([timed_out], [], diff_count=0)

    with pytest.raises(BuilderContinuation) as handed:
        ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert str(handed.value) == "timeout:builder"
    plan = handed.value.plan
    assert (plan.marker_state, plan.worktree_root, plan.continuations) == (
        "isolated", Path("/worktree"), 1
    )
    assert ports.workspace.released == []
    assert ports.workspace.snapshotted == []
    assert ports.tracker.parked == []


@pytest.mark.parametrize(
    "seat_outcome,expected_reason",
    [
        (SeatOutcome.CANCELLED, "builder ended cancelled"),
        (SeatOutcome.FAILED, "builder ended failed"),
    ],
)
def test_builder_failure_parks_honestly(
    seat_outcome: SeatOutcome, expected_reason: str
) -> None:
    failed = SeatResult("builder", seat_outcome, None, "raw", SeatUsage(), "start", "end")
    ports = make_ports([failed], [], diff_count=0)

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.reason == expected_reason
    assert ports.workspace.released == ["lease"]


def test_a_stalled_builder_parks_with_a_stall_signature_and_sample_times() -> None:
    stalled = SeatResult(
        "builder",
        SeatOutcome.STALLED,
        None,
        "raw",
        SeatUsage(),
        "start",
        "end",
        stall_sample_times=(("output", 0.0), ("worktree", 0.0), ("cpu", 0.0)),
    )
    ports = make_ports([stalled], [], diff_count=0)

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.reason == "stall:builder:output+worktree+cpu"
    assert "stall:builder" in ports.tracker.parked[-1][1]
    parked = next(event for event in ports.records.events if event.event_type == "item.parked")
    assert parked.payload["kill_kind"] == "stall"
    assert parked.payload["last_sample_times"] == {"output": 0.0, "worktree": 0.0, "cpu": 0.0}


def test_builder_failure_with_product_files_snapshots_before_park() -> None:
    failed = SeatResult("builder", SeatOutcome.FAILED, None, "raw:b", SeatUsage(), "start", "end")
    ports = make_ports([failed], [], diff_count=0)
    ports.workspace.snapshots = [
        WorktreeSnapshot(
            "base",
            b"binary patch",
            (SnapshotFile("rejected.txt", b"work in progress", "sha-1"),),
            (ChangedPath(Path("rejected.txt"), ChangeKind.ADDED, "sha-1"),),
        )
    ]

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.PARKED
    assert ports.workspace.snapshotted == ["lease"]
    files = ports.records.evidence_files
    assert "item-1-park/changes.patch" in files
    assert files["item-1-park/files/rejected.txt"] == b"work in progress"
    manifest = json.loads(files["item-1-park/snapshot.json"].decode("utf-8"))
    assert manifest["files"][0]["source"] == "rejected.txt"
    assert manifest["patch"] == "changes.patch"
    seat_refs = {entry["raw_output_ref"] for entry in manifest["seats"]}
    assert "raw:b" in seat_refs
    parked_reason = ports.tracker.parked[-1][1]
    assert "evidence/item-1-park" in parked_reason
    parked_event = next(event for event in ports.records.events if event.event_type == "item.parked")
    assert parked_event.payload["snapshot_path"] == "evidence/item-1-park"
    assert "files/rejected.txt" in parked_event.payload["snapshotted_paths"]
    assert any(entry["outcome"] == "failed" for entry in parked_event.payload["seat_evidence"])


def test_review_park_snapshots_tracked_patch_and_untracked_files() -> None:
    ports = make_ports(
        [builder("initial"), review(ReviewDecision.PARK, "1")],
        [command(suffix="1")],
        diff_count=1,
    )
    ports.workspace.snapshots = [
        WorktreeSnapshot(
            "base",
            b"diff --git a/tracked.py b/tracked.py",
            (SnapshotFile("new.txt", b"added file", "sha-new"),),
            (
                ChangedPath(Path("tracked.py"), ChangeKind.MODIFIED, "sha-mod"),
                ChangedPath(Path("new.txt"), ChangeKind.ADDED, "sha-new"),
            ),
        )
    ]

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.PARKED
    files = ports.records.evidence_files
    assert files["item-1-park/changes.patch"] == b"diff --git a/tracked.py b/tracked.py"
    assert files["item-1-park/files/new.txt"] == b"added file"
    manifest = json.loads(files["item-1-park/snapshot.json"].decode("utf-8"))
    kinds = {entry["path"]: entry["kind"] for entry in manifest["tracked_paths"]}
    assert kinds == {"tracked.py": "modified", "new.txt": "added"}


def test_release_refusal_on_finally_is_recorded_and_campaign_continues() -> None:
    first = SeatResult("builder", SeatOutcome.FAILED, None, "raw", SeatUsage(), "start", "end")
    second = SeatResult("builder", SeatOutcome.FAILED, None, "raw", SeatUsage(), "start", "end")
    ports = make_ports([first, second], [], diff_count=0)
    ports.workspace.release_error = EvidenceError("uncommitted tracker files remain")
    ports.tracker.queue.extend([item(), WorkItem("item-2", "second", "plan", ("passes",))])

    outcome = CampaignRunner(ports).run(campaign(max_items=2), lambda selected: spec(selected))

    assert outcome.stop_reason is CampaignStopReason.ITEM_BOUND
    assert len(outcome.items) == 2
    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert "workspace release refused" in (outcome.items[0].reason or "")
    refusals = [event for event in ports.records.events if event.event_type == "workspace.release_refused"]
    assert len(refusals) == 2


def test_tracker_park_error_fails_item_nonstructurally_and_campaign_continues() -> None:
    class _ParkRaisingTracker(FakeTrackerAdapter):
        def park(self, item_id, reason, actor, workspace=None):
            raise AdapterError("tracker park exploded")

    failed = SeatResult("builder", SeatOutcome.FAILED, None, "raw", SeatUsage(), "start", "end")
    other = SeatResult("builder", SeatOutcome.FAILED, None, "raw", SeatUsage(), "start", "end")
    ports = make_ports([failed, other], [], diff_count=0)
    ports = WorkflowPorts(
        _ParkRaisingTracker(identity("tracker")),
        ports.workspace,
        ports.harness,
        ports.command,
        ports.records,
        ports.knowledge,
        lease=ports.lease,
    )
    ports.tracker.queue.extend([item(), WorkItem("item-2", "second", "plan", ("passes",))])

    outcome = CampaignRunner(ports).run(campaign(max_items=2), lambda selected: spec(selected))

    assert outcome.stop_reason is CampaignStopReason.ITEM_BOUND
    assert len(outcome.items) == 2
    assert outcome.items[0].disposition is ItemDisposition.FAILED
    assert outcome.items[0].structural_failure is False
    assert "tracker park exploded" in (outcome.items[0].reason or "")
    assert any(event.event_type == "item.park_failed" for event in ports.records.events)
    assert ports.workspace.released == []


def test_incomplete_review_evidence_parks_the_item_structurally_and_the_next_builds() -> None:
    incomplete = SeatResult("review", SeatOutcome.SUCCEEDED, None, "raw", SeatUsage(), "start", "end")
    ports = make_ports(
        [builder(), incomplete, builder("b2"), verdict_result("item-2", ReviewDecision.PASS, "2")],
        [command(), command(suffix="2")],
        diff_count=2,
    )
    ports.tracker.queue.extend([item(), WorkItem("item-2", "second", "plan", ("passes",))])

    outcome = CampaignRunner(ports).run(campaign(max_items=3), lambda selected: spec(selected))

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert outcome.items[0].structural_failure is True
    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert outcome.items[1].disposition is ItemDisposition.ACCEPTED


def _phase_states(ports: WorkflowPorts, item_id: str = "item-1") -> list[str]:
    return [
        json.loads(content.decode("utf-8"))["state"]
        for path, content in ports.records.evidence_file_writes
        if path == f"{item_id}-phase.json"
    ]


def test_phase_marker_records_every_transition_and_closes_after_delivery() -> None:
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1)
    record = ports.records.create(campaign())

    outcome = ItemWorkflow(ports).run(campaign(), spec(), record)

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert _phase_states(ports) == [
        "verified",
        "claimed",
        "isolated",
        "built",
        "validated",
        "reviewed",
        "finalised",
        "released",
        "closed",
    ]
    assert ports.workspace.confirmed == [("lease", "main")]
    final = json.loads(ports.records.evidence_files["item-1-phase.json"].decode("utf-8"))
    assert final["state"] == "closed"
    assert final["workspace_revision"] == "revision-0"
    assert final["correction_count"] == 0


def test_phase_marker_terminal_value_for_a_parked_item_is_parked() -> None:
    failed = SeatResult("builder", SeatOutcome.FAILED, None, "raw", SeatUsage(), "start", "end")
    ports = make_ports([failed], [], diff_count=0)
    record = ports.records.create(campaign())

    outcome = ItemWorkflow(ports).run(campaign(), spec(), record)

    assert outcome.disposition is ItemDisposition.PARKED
    states = _phase_states(ports)
    assert states[:3] == ["verified", "claimed", "isolated"]
    assert states[-1] == "parked"
    assert "closed" not in states


def test_machine_item_parks_at_triage_without_a_workspace_and_next_item_builds() -> None:
    ports = make_ports(
        [builder("b2"), verdict_result("item-2", ReviewDecision.PASS, "2")],
        [command(suffix="2")],
        diff_count=1,
    )
    buildable = WorkItem("item-2", "second", "plan", ("passes",))
    ports.tracker.queue.extend([item(), buildable])

    def spec_for(selected: WorkItem) -> ItemExecutionSpec:
        text = "Item nature: machine" if selected.item_id == "item-1" else ""
        return spec(selected, brief_text=text)

    outcome = CampaignRunner(ports).run(campaign(max_items=2), spec_for)

    assert outcome.items[0].item_id == "item-1"
    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert outcome.items[0].reason == "nature:machine"
    assert outcome.items[1].item_id == "item-2"
    assert outcome.items[1].disposition is ItemDisposition.ACCEPTED
    assert ports.workspace.created == ["item-2"]
    assert [item_id for item_id, _ in ports.tracker.claims] == ["item-2"]
    assert ("item-1", "nature:machine") in ports.tracker.parked
    triaged = next(
        event for event in ports.records.events if event.event_type == "item.triaged"
    )
    assert triaged.payload["nature"] == "machine"


def test_an_item_whose_brief_orders_git_staging_parks_at_triage_and_next_item_builds() -> None:
    ports = make_ports(
        [builder("b2"), verdict_result("item-2", ReviewDecision.PASS, "2")],
        [command(suffix="2")],
        diff_count=1,
    )
    buildable = WorkItem("item-2", "second", "plan", ("passes",))
    ports.tracker.queue.extend([item(), buildable])

    def spec_for(selected: WorkItem) -> ItemExecutionSpec:
        text = EXCERPT_C if selected.item_id == "item-1" else ""
        return spec(selected, brief_text=text)

    outcome = CampaignRunner(ports).run(campaign(max_items=2), spec_for)

    first, second = outcome.items
    assert first.item_id == "item-1"
    assert first.disposition is ItemDisposition.PARKED
    reason = first.reason or ""
    assert reason.startswith("brief:git-staging")
    assert "line 2" in reason and "`git rm`" in reason
    assert len(reason) < 500
    assert ("item-1", reason) in ports.tracker.parked
    assert second.item_id == "item-2"
    assert second.disposition is ItemDisposition.ACCEPTED
    assert ports.workspace.created == ["item-2"]
    assert [item_id for item_id, _ in ports.tracker.claims] == ["item-2"]
    assert len(ports.workspace.delivered) == 1
    assert [request.seat.value for request in ports.harness.requests] == [
        "builder",
        "reviewer",
    ]
    triaged = next(
        event for event in ports.records.events if event.event_type == "item.triaged"
    )
    assert triaged.item_id == "item-1"
    assert triaged.payload["triage"] == "brief:git-staging"
    assert triaged.payload["reason"] == reason
    assert triaged.payload["lines"] == [
        {"line_number": 2, "invocations": ["git rm"], "text": EXCERPT_C.splitlines()[1][:160]}
    ]


@pytest.mark.parametrize(
    "defect, reason",
    [
        (BriefDefect(BriefDefectCause.MISSING), "brief:missing docs/brief.md"),
        (BriefDefect(BriefDefectCause.EMPTY), "brief:empty docs/brief.md"),
        (
            BriefDefect(BriefDefectCause.TOO_LARGE, size=1_048_577),
            "brief:too-large 1048577 bytes, over 1048576 docs/brief.md",
        ),
        (
            BriefDefect(BriefDefectCause.UNREADABLE, error="/home/someone/brief.md"),
            "brief:unreadable docs/brief.md",
        ),
    ],
    ids=lambda value: value.cause.value if isinstance(value, BriefDefect) else "",
)
def test_an_item_with_a_brief_defect_parks_before_the_nature_rule(
    monkeypatch: pytest.MonkeyPatch, defect: BriefDefect, reason: str
) -> None:
    ports = make_ports([], [], diff_count=0)
    work = replace(
        spec(WorkItem("item-1", "test item", "docs/brief.md", ("passes",)), brief_text="Item nature: machine"),
        brief_defect=defect,
    )
    workspaces: list[object] = []
    park = ports.tracker.park

    def recording_park(item_id, reason, actor, workspace=None):
        workspaces.append(workspace)
        return park(item_id, reason, actor=actor, workspace=workspace)

    monkeypatch.setattr(ports.tracker, "park", recording_park)

    outcome = ItemWorkflow(ports).run(campaign(), work, ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.PARKED
    assert outcome.reason == reason
    assert ports.tracker.parked == [("item-1", reason)]
    assert workspaces == [None]
    assert ports.tracker.claims == []
    assert ports.workspace.created == []
    assert ports.harness.requests == []
    events = [
        event for event in ports.records.events if event.event_type in {"item.triaged", "item.parked"}
    ]
    assert [event.event_type for event in events] == ["item.triaged", "item.parked"]
    assert events[0].payload == {"triage": f"brief:{defect.cause.value}", "reason": reason}


def test_a_long_brief_reference_is_cut_to_the_park_reason_limit() -> None:
    ports = make_ports([], [], diff_count=0)
    work = replace(
        spec(WorkItem("item-1", "test item", "docs/" + "b" * 600 + ".md", ("passes",))),
        brief_defect=BriefDefect(BriefDefectCause.MISSING),
    )

    outcome = ItemWorkflow(ports).run(campaign(), work, ports.records.create(campaign()))

    assert (outcome.reason or "").startswith("brief:missing docs/b")
    assert len(outcome.reason or "") == 499


def test_owner_gated_item_offered_by_the_tracker_parks_with_its_class() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.queue.append(item())

    outcome = CampaignRunner(ports).run(
        campaign(max_items=1),
        lambda selected: spec(selected, brief_text="Item nature: owner-gated"),
    )

    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert outcome.items[0].reason == "nature:owner-gated"
    assert ports.workspace.created == []
    assert ports.tracker.claims == []
    assert ("item-1", "nature:owner-gated") in ports.tracker.parked


def test_unreachable_merged_commit_is_a_structural_failure_parked_from_the_primary() -> None:
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1)
    ports.workspace.unreachable_merge = True
    ports.tracker.queue.append(item())

    outcome = CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    assert outcome.stop_reason is CampaignStopReason.ITEM_BOUND
    assert outcome.items[0].structural_failure is True
    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert "not reachable" in (outcome.items[0].reason or "")
    # The branch holds the item's product commit, so the park is recorded from
    # the primary checkout and the worktree is kept, never delivered.
    assert ports.workspace.released == []
    assert len(ports.workspace.delivered) == 1


def test_dry_queue_refill_creates_proposals_and_fog_but_no_work() -> None:
    ports = make_ports([], [], diff_count=0)
    refill = RefillPlan(
        proposals=(Proposal("candidate", "sharp question?", "reason", "brief"),),
        fog=(FogRecord("direction", "what is the question?", "when evidence exists"),),
    )

    outcome = CampaignRunner(ports).run(campaign(refill_enabled=True), lambda selected: spec(selected), refill)

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert outcome.items == ()
    assert len(ports.tracker.proposals) == 1
    assert len(ports.knowledge.fog) == 1


def test_campaign_stops_at_the_declared_item_bound() -> None:
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1)
    ports.tracker.queue.extend([item(), WorkItem("item-2", "second", "plan", ("passes",))])

    outcome = CampaignRunner(ports).run(campaign(max_items=1), lambda selected: spec(selected))

    assert outcome.stop_reason is CampaignStopReason.ITEM_BOUND
    assert len(outcome.items) == 1
    assert [evidence.item_id for evidence in ports.tracker.closed] == ["item-1"]
    assert ports.workspace.reports and "item-2" in ports.workspace.reports[0].content


def verdict_result(item_id: str, decision: ReviewDecision, suffix: str) -> SeatResult:
    findings = ()
    if decision is not ReviewDecision.PASS:
        findings = (
            ReviewFinding("finding", "material consequence", f"finding:{suffix}", blocking=True),
        )
    return SeatResult(
        f"review-{suffix}",
        SeatOutcome.SUCCEEDED,
        ReviewVerdict(item_id, decision, findings, f"verdict:{suffix}"),
        f"raw:review:{suffix}",
        SeatUsage(input_tokens=5, output_tokens=3, cost=0.01, source="fake"),
        "2026-09-03T00:00:00+00:00",
        "2026-09-03T00:00:02+00:00",
        exit_code=0,
        model="resolved-model",
    )


def test_every_event_carries_utc_time_payload_and_a_parked_trajectory(tmp_path: Path) -> None:
    workspace_ref = WorkspaceRef(Path("/worktree"), "run", "base", "lease")
    tracker = FakeTrackerAdapter(
        identity("tracker"),
        queue=[item(), WorkItem("item-2", "second", "plan", ("passes",))],
    )
    workspace = FakeWorkspaceAdapter(
        identity("workspace"),
        workspace=workspace_ref,
        diffs=[DiffEvidence(workspace_ref, f"revision-{index}", (), f"patch:{index}") for index in range(2)],
    )
    harness = FakeHarnessAdapter(
        identity("harness"),
        scripted_results=[
            builder("b1"),
            verdict_result("item-1", ReviewDecision.PASS, "1"),
            builder("b2"),
            verdict_result("item-2", ReviewDecision.PARK, "2"),
        ],
    )
    commands = FakeCommandAdapter(
        identity("command"), scripted_results=[command(suffix="1"), command(suffix="2")]
    )
    records = LocalRunRecordAdapter(tmp_path / "runs")
    knowledge = FakeKnowledgeAdapter(identity("knowledge"))
    lease = FakeLeaseAdapter(identity("lease"))
    ports = WorkflowPorts(
        tracker, workspace, harness, commands, records, knowledge, lease=lease
    )

    CampaignRunner(ports).run(campaign(max_items=2), lambda selected: spec(selected))

    run_dirs = list((tmp_path / "runs").iterdir())
    assert len(run_dirs) == 1
    lines = (run_dirs[0] / "events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert events
    for event in events:
        when = datetime.fromisoformat(event["occurred_at"])
        assert when.tzinfo is not None and when.utcoffset() == timedelta(0)
        assert isinstance(event["payload"], dict)
    parked_seats = [
        event
        for event in events
        if event["event_type"] == "seat.completed" and event["item_id"] == "item-2"
    ]
    assert len(parked_seats) == 2
    assert (run_dirs[0] / "evidence" / "item-2-trajectory.txt").is_file()


class _ScriptedItemWorkflow:
    def __init__(self, outcomes: list[ItemOutcome]) -> None:
        self._outcomes = outcomes

    def run(self, campaign: CampaignRef, spec: ItemExecutionSpec, record: RunRecordRef) -> ItemOutcome:
        return self._outcomes.pop(0)


class _RaisingItemWorkflow:
    def run(self, campaign: CampaignRef, spec: ItemExecutionSpec, record: RunRecordRef) -> ItemOutcome:
        raise RuntimeError("item workflow exploded")


def test_report_names_shipped_parked_and_failed_items(tmp_path: Path) -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.queue.extend(
        [
            WorkItem("item-1", "Ship it", "plan", ("passes",)),
            WorkItem("item-2", "Park it", "plan", ("passes",)),
            WorkItem("item-3", "Fail it", "plan", ("passes",)),
        ]
    )
    scripted = _ScriptedItemWorkflow(
        [
            ItemOutcome(
                "item-1",
                ItemDisposition.ACCEPTED,
                (ItemState.RELEASED,),
                title="Ship it",
                follow_ups=(
                    FollowUpRecord(
                        "proposal-1",
                        Proposal("Follow-up: item-1 STYLE", "rename", "non-blocking", "plan"),
                    ),
                ),
            ),
            ItemOutcome("item-2", ItemDisposition.PARKED, (ItemState.PARKED,), reason="held", title="Park it"),
            ItemOutcome(
                "item-3",
                ItemDisposition.FAILED,
                (ItemState.READY,),
                reason="boom",
                structural_failure=True,
                title="Fail it",
            ),
        ]
    )

    CampaignRunner(ports, items=scripted).run(campaign(max_items=3), lambda selected: spec(selected))

    content = ports.workspace.reports[0].content
    for heading in (
        "## Shipped",
        "## Parked",
        "## Failed",
        "## Follow-ups",
        "## Next",
        "## Seat usage",
        "## Run record",
    ):
        assert heading in content
    assert "- item-1 Ship it" in content
    assert "- item-2: held" in content
    assert "- item-3: boom" in content
    assert "### proposal-1 Follow-up: item-1 STYLE" in content
    # The new run-record section is the final section, after the five existing
    # ones and the seat-usage table, and names the progress log.
    assert content.index("## Seat usage") < content.index("## Run record")
    assert "progress log" in content


def test_report_seat_rows_name_actual_lane_model_and_requested_effort() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.queue.append(item())
    seats = (
        SeatObservation(
            Seat.BUILDER,
            "builder-class",
            "resolved-builder",
            SeatOutcome.SUCCEEDED,
            0,
            "start",
            "end",
            12.0,
            3,
            5,
            0.46740859999999995,
            "raw:builder",
            "",
            lane="first",
            effort=EffortLevel.HIGH,
        ),
        SeatObservation(
            Seat.REVIEWER,
            "reviewer-class",
            "resolved-reviewer",
            SeatOutcome.SUCCEEDED,
            0,
            "start",
            "end",
            None,
            None,
            None,
            None,
            "raw:reviewer",
            "",
            lane="second",
        ),
    )
    scripted = _ScriptedItemWorkflow(
        [ItemOutcome("item-1", ItemDisposition.ACCEPTED, (ItemState.RELEASED,), seats=seats)]
    )

    CampaignRunner(ports, items=scripted).run(campaign(), lambda selected: spec(selected))

    content = ports.workspace.reports[0].content
    assert (
        "| item | seat | lane | model | requested effort | duration seconds | input tokens | "
        "output tokens | estimated cost |" in content
    )
    assert "| item-1 | builder | first | resolved-builder | high | 12.0 | 3 | 5 | 0.47 |" in content
    assert "| item-1 | reviewer | second | resolved-reviewer | - | - | - | - | - |" in content

    empty_ports = make_ports([], [], diff_count=0)
    CampaignRunner(empty_ports).run(campaign(), lambda selected: spec(selected))

    assert "| none | - | - | - | - | - | - | - | - |" in empty_ports.workspace.reports[0].content


def test_a_raising_item_workflow_still_leaves_a_completed_run_record(tmp_path: Path) -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.queue.append(item())
    records = LocalRunRecordAdapter(tmp_path / "runs")
    ports = WorkflowPorts(
        ports.tracker,
        ports.workspace,
        ports.harness,
        ports.command,
        records,
        ports.knowledge,
        lease=ports.lease,
    )

    outcome = CampaignRunner(ports, items=_RaisingItemWorkflow()).run(
        campaign(max_items=1), lambda selected: spec(selected)
    )

    assert outcome.stop_reason is CampaignStopReason.ITEM_BOUND
    assert outcome.items[0].disposition is ItemDisposition.FAILED
    assert outcome.items[0].structural_failure is True
    run_dirs = list((tmp_path / "runs").iterdir())
    assert (run_dirs[0] / "report.txt").is_file()
    events = [
        json.loads(line)
        for line in (run_dirs[0] / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["event_type"] == "campaign.completed" for event in events)


def test_allow_list_dispatches_in_allow_order_and_never_claims_outside() -> None:
    a = WorkItem("a", "Item A", "plan", ("passes",))
    b = WorkItem("b", "Item B", "plan", ("passes",))
    c = WorkItem("c", "Item C", "plan", ("passes",))
    ports = make_ports(
        [
            builder("b"),
            verdict_result("b", ReviewDecision.PASS, "b"),
            builder("a"),
            verdict_result("a", ReviewDecision.PASS, "a"),
        ],
        [command(suffix="b"), command(suffix="a")],
        diff_count=2,
    )
    ports.tracker.ready = [a, b, c]

    outcome = CampaignRunner(ports).run(
        campaign(max_items=5, selection=CampaignSelection(allow=("b", "a"))),
        lambda selected: spec(selected),
    )

    assert [item_id for item_id, _ in ports.tracker.claims] == ["b", "a"]
    assert [item.item_id for item in outcome.items] == ["b", "a"]
    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY


def test_excluded_item_after_refill_is_never_claimed_and_queue_ends_dry() -> None:
    excluded = WorkItem("x", "Excluded", "plan", ("passes",))
    ports = make_ports([], [], diff_count=0)
    ports.tracker.ready = [excluded]
    refill = RefillPlan(proposals=(Proposal("candidate", "sharp question?", "reason", "brief"),))

    outcome = CampaignRunner(ports).run(
        campaign(refill_enabled=True, selection=CampaignSelection(exclude=("x",))),
        lambda selected: spec(selected),
        refill,
    )

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert ports.tracker.claims == []
    assert len(ports.tracker.proposals) == 1


def test_out_of_allow_list_next_item_is_never_claimed_and_the_fenced_queue_is_dry() -> None:
    out_of_scope = WorkItem("c", "Out of scope", "plan", ("passes",))
    ports = make_ports([], [], diff_count=0)
    base = FakeTrackerAdapter(identity("tracker"), queue=[out_of_scope])
    selection = CampaignSelection(allow=("a",))
    scoped = ScopedTrackerPort(base, selection)
    ports = WorkflowPorts(
        scoped,
        ports.workspace,
        ports.harness,
        ports.command,
        ports.records,
        ports.knowledge,
        lease=ports.lease,
    )

    outcome = CampaignRunner(ports).run(
        campaign(selection=selection), lambda selected: spec(selected)
    )

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert base.claims == []
    assert outcome.items == ()
    violations = [
        event for event in ports.records.events if event.event_type == "campaign.scope_fence_violation"
    ]
    assert len(violations) == 1 and "'c'" in violations[0].payload["error"]


def test_report_lists_an_allowed_item_after_a_park_as_not_ready_without_a_tracker_block() -> None:
    ready_item = WorkItem("a", "Item A", "plan", ("passes",))
    failed = SeatResult("builder", SeatOutcome.FAILED, None, "raw", SeatUsage(), "start", "end")
    ports = make_ports([failed], [], diff_count=0)
    ports.tracker.ready = [ready_item]
    ports.tracker.existing.add("b")

    outcome = CampaignRunner(ports).run(
        campaign(max_items=5, selection=CampaignSelection(allow=("a", "b"))),
        lambda selected: spec(selected),
    )

    assert outcome.items[0].item_id == "a"
    assert outcome.items[0].disposition is ItemDisposition.PARKED
    content = ports.workspace.reports[0].content
    assert "## Allowed items left unbuilt" in content
    assert "- b: not ready" in content
    assert "- b: blocked" not in content


def test_report_lists_an_allowed_item_the_tracker_holds_back_as_blocked() -> None:
    a = WorkItem("a", "Item A", "plan", ("passes",))
    b = WorkItem("b", "Item B", "plan", ("passes",))
    ports = make_ports(
        [builder("a"), verdict_result("a", ReviewDecision.PASS, "a")],
        [command(suffix="a")],
        diff_count=1,
    )
    ports.tracker.ready = [a, b]
    ports.tracker.blocked_by = {"b": frozenset({"z"})}

    outcome = CampaignRunner(ports).run(
        campaign(max_items=5, selection=CampaignSelection(allow=("a", "b"))),
        lambda selected: spec(selected),
    )

    assert [(o.item_id, o.disposition) for o in outcome.items] == [
        ("a", ItemDisposition.ACCEPTED)
    ]
    content = ports.workspace.reports[0].content
    assert "- b: blocked" in content


def test_report_lists_an_allowed_item_another_writer_claimed_as_claim_taken() -> None:
    a = WorkItem("a", "Item A", "plan", ("passes",))
    ports = make_ports([], [], diff_count=0)
    ports.tracker.ready = [a]
    ports.tracker.claim_errors["a"] = ClaimTaken("a", "claimed elsewhere")

    outcome = CampaignRunner(ports).run(
        campaign(max_items=5, selection=CampaignSelection(allow=("a",))),
        lambda selected: spec(selected),
    )

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert ports.tracker.claims == []
    assert outcome.items == ()
    content = ports.workspace.reports[0].content
    assert "- a: claim taken" in content
    assert "- a: item bound" not in content


def test_a_taken_allowed_item_absent_from_the_ready_listing_reads_claim_taken() -> None:
    reason = CampaignRunner._unbuilt_reason(
        "a",
        CampaignSelection(allow=("a",)),
        frozenset(),
        frozenset(),
        frozenset({"a"}),
        CampaignStopReason.QUEUE_DRY,
        frozenset({"a"}),
    )

    assert reason == "claim taken"


def test_a_ready_unbuilt_allowed_item_is_labelled_by_the_stop() -> None:
    named = {
        CampaignStopReason.ITEM_BOUND: "item bound",
        CampaignStopReason.HOLD_LIMIT: "hold limit",
        CampaignStopReason.PAUSED: "paused",
        CampaignStopReason.LAUNCH_SET_UNREADABLE: "launch set unreadable",
    }
    selection = CampaignSelection(allow=("a",))

    labels = {
        stop: CampaignRunner._unbuilt_reason(
            "a", selection, frozenset({"a"}), frozenset(), frozenset(), stop, frozenset({"a"})
        )
        for stop in CampaignStopReason
    }

    assert labels == {
        stop: named.get(stop, "ready after the last selection") for stop in CampaignStopReason
    }


def test_an_allowed_item_not_ready_at_the_item_bound_reads_not_ready() -> None:
    reason = CampaignRunner._unbuilt_reason(
        "a",
        CampaignSelection(allow=("a",)),
        frozenset(),
        frozenset(),
        frozenset(),
        CampaignStopReason.ITEM_BOUND,
        frozenset({"a"}),
    )

    assert reason == "not ready"


def test_an_in_fence_item_the_allow_list_peek_offers_is_claimed_and_built() -> None:
    a = WorkItem("a", "Item A", "plan", ("passes",))
    ports = make_ports(
        [builder("a"), verdict_result("a", ReviewDecision.PASS, "a")],
        [command(suffix="a")],
        diff_count=1,
    )
    base = ports.tracker
    base.queue = [a]
    selection = CampaignSelection(allow=("a",))
    ports = replace(ports, tracker=ScopedTrackerPort(base, selection))

    outcome = CampaignRunner(ports).run(
        campaign(max_items=5, selection=selection), lambda selected: spec(selected)
    )

    assert [item_id for item_id, _ in base.claims] == ["a"]
    assert [(o.item_id, o.disposition) for o in outcome.items] == [
        ("a", ItemDisposition.ACCEPTED)
    ]


def test_a_taken_item_the_allow_list_peek_keeps_offering_is_claimed_once() -> None:
    a = WorkItem("a", "Item A", "plan", ("passes",))

    class _RepeatingHeadTracker(FakeTrackerAdapter):
        head_reads = 0
        claim_attempts: list[str]

        def next_item(self, campaign: CampaignRef) -> WorkItem | None:
            # Bounded so a selection that forgets the skip set still ends.
            self.head_reads += 1
            return a if self.head_reads <= 6 else None

        def claim(self, item: WorkItem, actor: str):
            self.claim_attempts.append(item.item_id)
            return super().claim(item, actor)

    ports = make_ports([], [], diff_count=0)
    base = _RepeatingHeadTracker(identity("tracker"), existing={"a"})
    base.claim_attempts = []
    base.claim_errors["a"] = ClaimTaken("a", "claimed elsewhere")
    selection = CampaignSelection(allow=("a",))
    ports = replace(ports, tracker=ScopedTrackerPort(base, selection))

    outcome = CampaignRunner(ports).run(
        campaign(max_items=5, selection=selection), lambda selected: spec(selected)
    )

    assert base.claim_attempts == ["a"]
    assert outcome.items == ()
    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY


# --- Single-writer lease per surface ------------------------------------------


def _tracker_context() -> CampaignContext:
    return CampaignContext(
        harness="",
        models={},
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        validator_id="",
        target_branch="",
        target_revision="",
        tracker_surface=LeaseSurface(Path("/repo/.ergon"), SurfaceKind.TRACKER),
    )


def _worktree_surfaces(ports: WorkflowPorts) -> list[tuple[SurfaceKind, Path]]:
    return [
        (surface.kind, surface.path)
        for surface in ports.lease.released
        if surface.kind is SurfaceKind.WORKTREE
    ]


def test_worktree_lease_is_acquired_at_isolation_and_released_after_accept() -> None:
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1)

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert [(s.kind, s.path) for s in ports.lease.acquired] == [
        (SurfaceKind.WORKTREE, Path("/worktree"))
    ]
    assert _worktree_surfaces(ports) == [(SurfaceKind.WORKTREE, Path("/worktree"))]


def test_worktree_lease_is_released_after_a_park() -> None:
    failed = SeatResult("builder", SeatOutcome.FAILED, None, "raw", SeatUsage(), "start", "end")
    ports = make_ports([failed], [], diff_count=0)

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.PARKED
    assert _worktree_surfaces(ports) == [(SurfaceKind.WORKTREE, Path("/worktree"))]


def test_worktree_lease_is_released_after_a_raised_exception() -> None:
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1)
    ports.workspace.unreachable_merge = True

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.PARKED
    assert "not reachable" in (outcome.reason or "")
    assert _worktree_surfaces(ports) == [(SurfaceKind.WORKTREE, Path("/worktree"))]


def test_a_live_tracker_lease_refuses_the_second_campaign_before_any_claim() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.queue.append(item())
    surface = LeaseSurface(Path("/repo/.ergon"), SurfaceKind.TRACKER)
    ports.lease.live_holders[str(surface.path)] = LeaseRecord(
        "holder-campaign", 555, "holder-host", "s", "b", surface.path, SurfaceKind.TRACKER
    )

    with pytest.raises(LeaseHeld) as excinfo:
        CampaignRunner(ports).run(
            campaign(), lambda selected: spec(selected), context=_tracker_context()
        )

    message = str(excinfo.value)
    assert "holder-campaign" in message and "555" in message and "holder-host" in message
    assert ports.tracker.claims == []
    assert any(
        event.event_type == "campaign.lease_refused" for event in ports.records.events
    )


def test_a_stale_tracker_lease_is_reclaimed_and_recorded_with_the_previous_holder() -> None:
    ports = make_ports([], [], diff_count=0)
    surface = LeaseSurface(Path("/repo/.ergon"), SurfaceKind.TRACKER)
    ports.lease.stale_holders[str(surface.path)] = LeaseRecord(
        "old-campaign", 9999, "old-host", "s", "b", surface.path, SurfaceKind.TRACKER
    )

    outcome = CampaignRunner(ports).run(
        campaign(), lambda selected: spec(selected), context=_tracker_context()
    )

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    reclaimed = next(
        event for event in ports.records.events
        if event.event_type == "campaign.lease_reclaimed"
    )
    assert reclaimed.payload["previous_holder"]["campaign_id"] == "old-campaign"
    assert reclaimed.payload["previous_holder"]["process_id"] == 9999
    content = ports.workspace.reports[0].content
    assert "## Lease reclaims" in content
    assert "reclaimed from campaign old-campaign" in content


def test_the_tracker_lease_is_renewed_at_each_item_boundary_and_released_at_the_end() -> None:
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1)
    ports.tracker.queue.append(item())
    surface = LeaseSurface(Path("/repo/.ergon"), SurfaceKind.TRACKER)

    outcome = CampaignRunner(ports).run(
        campaign(max_items=1), lambda selected: spec(selected), context=_tracker_context()
    )

    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED
    assert [s.path for s in ports.lease.renewed] == [surface.path]
    assert surface.path in [s.path for s in ports.lease.released]
    assert any(
        event.event_type == "campaign.lease_acquired" for event in ports.records.events
    )


# --- Lane failover across two harness lanes ------------------------------------


def failed_builder(run: str = "builder") -> SeatResult:
    return SeatResult(run, SeatOutcome.FAILED, None, f"raw:{run}", SeatUsage(), "start", "end")


def lane_ports(
    lane_one_results: list[SeatResult],
    lane_two_results: list[SeatResult],
    command_results: list[CommandResult],
    *,
    diff_count: int,
    one_signal: LaneSignal | None = None,
    two_signal: LaneSignal | None = None,
    lane_state: FakeLaneStateAdapter | None = None,
) -> tuple[WorkflowPorts, FakeLaneStateAdapter]:
    workspace_ref = WorkspaceRef(Path("/worktree"), "run", "base", "lease")
    tracker = FakeTrackerAdapter(identity("tracker"))
    workspace = FakeWorkspaceAdapter(
        identity("workspace"),
        workspace=workspace_ref,
        diffs=[
            DiffEvidence(workspace_ref, f"revision-{index}", (), f"patch:{index}")
            for index in range(diff_count)
        ],
    )
    one = FakeHarnessAdapter(
        identity("lane-one"), scripted_results=list(lane_one_results), signal=one_signal
    )
    two = FakeHarnessAdapter(
        identity("lane-two"), scripted_results=list(lane_two_results), signal=two_signal
    )
    commands = FakeCommandAdapter(identity("command"), scripted_results=list(command_results))
    records = FakeRunRecordAdapter(identity("records"))
    knowledge = FakeKnowledgeAdapter(identity("knowledge"))
    lease = FakeLeaseAdapter(identity("lease"))
    state = lane_state or FakeLaneStateAdapter(identity("lane-state"))
    ports = WorkflowPorts(
        tracker,
        workspace,
        one,
        commands,
        records,
        knowledge,
        lease=lease,
        lanes=(Lane("lane-one", one), Lane("lane-two", two)),
        lane_state=state,
    )
    return ports, state


def test_builder_rate_limit_reruns_on_lane_two_and_the_item_is_accepted() -> None:
    ports, state = lane_ports(
        [failed_builder("one")],
        [builder("two"), review(ReviewDecision.PASS)],
        [command()],
        diff_count=1,
        one_signal=LaneSignal(LaneSignalKind.RATE_LIMIT),
    )

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert ports.tracker.claims == [("item-1", "builder@lane-one")]
    assert [lane for lane, _signal, _campaign in state.cools] == ["lane-one"]
    changed = [
        event.payload for event in ports.records.events if event.event_type == "item.lane_changed"
    ]
    assert changed == [{"lane": "lane-two", "from_lane": "lane-one"}]
    assert not any(
        event.event_type == "item.lane_reclaim_failed" for event in ports.records.events
    )
    assert [evidence.builder_lane for evidence in ports.tracker.closed] == ["lane-two"]
    accepted_lanes = {observation.lane for observation in outcome.seats if observation.outcome is SeatOutcome.SUCCEEDED}
    assert accepted_lanes == {"lane-two"}
    finalised = next(
        event for event in ports.records.events if event.event_type == "item.finalised"
    )
    delivered = {
        entry["seat"]: entry["lane"]
        for entry in finalised.payload["seat_lanes"]
        if entry["seat"] in {"reviewer"}
    }
    assert delivered["reviewer"] == "lane-two"


def test_signals_on_both_lanes_park_with_the_signature_and_hold_lanes_exhausted() -> None:
    ports, state = lane_ports(
        [failed_builder("one")],
        [failed_builder("two")],
        [],
        diff_count=0,
        one_signal=LaneSignal(LaneSignalKind.RATE_LIMIT),
        two_signal=LaneSignal(LaneSignalKind.RATE_LIMIT),
    )
    ports.tracker.queue.append(item())

    outcome = CampaignRunner(ports).run(campaign(max_items=1), lambda selected: spec(selected))

    # The campaign never stops for exhausted lanes: it holds admissions, and
    # here the item bound ends it first.
    assert outcome.stop_reason is CampaignStopReason.ITEM_BOUND
    held = [event for event in ports.records.events if event.event_type == "campaign.hold_started"]
    assert [event.payload["condition"] for event in held] == ["lanes_exhausted"]
    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert outcome.items[0].lanes_exhausted is True
    assert outcome.items[0].lane_signature == "rate_limit"
    assert [lane for lane, _s, _c in state.cools] == ["lane-one", "lane-two"]
    reason = ports.tracker.parked[-1][1]
    assert reason.startswith("lane-two:rate_limit")


def test_a_parked_lane_signature_is_not_reselected_in_the_same_campaign() -> None:
    class _ReofferTracker(FakeTrackerAdapter):
        def ready_items(self, campaign: CampaignRef) -> tuple[WorkItem, ...]:
            return tuple(self.ready)

    ports = make_ports([], [], diff_count=0)
    tracker = _ReofferTracker(identity("tracker"), ready=[item()])
    ports = WorkflowPorts(
        tracker,
        ports.workspace,
        ports.harness,
        ports.command,
        ports.records,
        ports.knowledge,
        lease=ports.lease,
    )
    parked = ItemOutcome(
        "item-1",
        ItemDisposition.PARKED,
        (ItemState.PARKED,),
        reason="lane-one:rate_limit",
        title="test item",
        lane_signature="rate_limit",
    )
    scripted = _ScriptedItemWorkflow([parked])

    outcome = CampaignRunner(ports, items=scripted).run(
        campaign(max_items=5, selection=CampaignSelection(allow=("item-1",))),
        lambda selected: spec(selected),
    )

    assert [o.item_id for o in outcome.items] == ["item-1"]
    assert len(outcome.items) == 1
    assert [item_id for item_id, _actor in tracker.claims] == []


def test_a_succeeded_seat_never_cools_the_lane_even_when_a_signal_is_available() -> None:
    ports, state = lane_ports(
        [builder("one"), review(ReviewDecision.PASS)],
        [],
        [command()],
        diff_count=1,
        one_signal=LaneSignal(LaneSignalKind.RATE_LIMIT),
    )

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert state.cools == []
    assert ports.tracker.claims == [("item-1", "builder@lane-one")]
    assert not any(event.event_type == "item.lane_changed" for event in ports.records.events)
    assert [evidence.builder_lane for evidence in ports.tracker.closed] == ["lane-one"]


# --- Seat effort ---------------------------------------------------------------


def _seat_payloads(ports: WorkflowPorts) -> list[dict]:
    return [
        dict(event.payload)
        for event in ports.records.events
        if event.event_type == "seat.completed"
    ]


def test_a_spec_without_efforts_sends_and_records_every_seat_without_effort() -> None:
    ports = make_ports(
        [builder(), review(ReviewDecision.ESCALATE), review(ReviewDecision.PASS, "specialist")],
        [command()],
        diff_count=1,
    )

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert [request.effort for request in ports.harness.requests] == [None, None, None]
    assert [observation.effort for observation in outcome.seats] == [None, None, None]
    assert [payload["effort"] for payload in _seat_payloads(ports)] == [None, None, None]


def test_each_seat_request_and_observation_carries_its_own_effort() -> None:
    ports = make_ports(
        [builder(), review(ReviewDecision.ESCALATE), review(ReviewDecision.PASS, "specialist")],
        [command()],
        diff_count=1,
    )
    efforts = (
        ("", Seat.BUILDER, EffortLevel.MEDIUM),
        ("", Seat.REVIEWER, EffortLevel.HIGH),
        ("", Seat.SPECIALIST, EffortLevel.MAX),
    )

    outcome = ItemWorkflow(ports).run(
        campaign(), replace(spec(), lane_efforts=efforts), ports.records.create(campaign())
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    expected = [
        (Seat.BUILDER, EffortLevel.MEDIUM),
        (Seat.REVIEWER, EffortLevel.HIGH),
        (Seat.SPECIALIST, EffortLevel.MAX),
    ]
    assert [(request.seat, request.effort) for request in ports.harness.requests] == expected
    assert [(observation.seat, observation.effort) for observation in outcome.seats] == expected
    assert [
        (payload["seat"], payload["model_class"], payload["effort"])
        for payload in _seat_payloads(ports)
    ] == [
        ("builder", "builder-class", "medium"),
        ("reviewer", "reviewer-class", "high"),
        ("specialist", "specialist-class", "max"),
    ]


def test_a_seat_that_moves_lanes_takes_the_effort_of_the_lane_it_runs_on() -> None:
    ports, _state = lane_ports(
        [failed_builder("one")],
        [builder("two"), review(ReviewDecision.PASS)],
        [command()],
        diff_count=1,
        one_signal=LaneSignal(LaneSignalKind.RATE_LIMIT),
    )
    efforts = (
        ("lane-one", Seat.BUILDER, EffortLevel.MEDIUM),
        ("lane-two", Seat.BUILDER, EffortLevel.HIGH),
    )

    outcome = ItemWorkflow(ports).run(
        campaign(), replace(spec(), lane_efforts=efforts), ports.records.create(campaign())
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    one, two = (lane.harness for lane in ports.lanes)
    assert [request.effort for request in one.requests] == [EffortLevel.MEDIUM]
    assert [(request.seat, request.effort) for request in two.requests] == [
        (Seat.BUILDER, EffortLevel.HIGH),
        (Seat.REVIEWER, None),
    ]
    assert [(observation.lane, observation.effort) for observation in outcome.seats] == [
        ("lane-one", EffortLevel.MEDIUM),
        ("lane-two", EffortLevel.HIGH),
        ("lane-two", None),
    ]


def test_a_lane_that_cannot_pass_the_seat_effort_never_runs_the_seat_without_it() -> None:
    ports = make_ports([builder()], [], diff_count=0)
    ports.harness.effort_levels = frozenset({EffortLevel.LOW})
    efforts = (("", Seat.BUILDER, EffortLevel.HIGH),)

    outcome = ItemWorkflow(ports).run(
        campaign(), replace(spec(), lane_efforts=efforts), ports.records.create(campaign())
    )

    assert outcome.disposition is ItemDisposition.PARKED
    assert "cannot pass effort high" in (outcome.reason or "")
    assert ports.harness.requests == []


def test_a_spec_with_efforts_stays_hashable_and_equal_by_value() -> None:
    efforts = (
        ("lane-one", Seat.BUILDER, EffortLevel.MEDIUM),
        ("lane-two", Seat.REVIEWER, EffortLevel.HIGH),
    )
    first = replace(spec(), lane_efforts=efforts)
    second = replace(spec(), lane_efforts=tuple(efforts))

    assert hash(first) == hash(second)
    assert {first, second} == {first}
    assert first.seat_effort(Seat.BUILDER, "lane-one") is EffortLevel.MEDIUM
    assert first.seat_effort(Seat.REVIEWER, "lane-one") is None
    assert first.seat_effort(Seat.BUILDER, "lane-three") is None


def test_a_spec_that_sets_one_seat_twice_on_a_lane_is_refused() -> None:
    efforts = (
        ("lane-one", Seat.BUILDER, EffortLevel.MEDIUM),
        ("lane-one", Seat.BUILDER, EffortLevel.HIGH),
    )

    with pytest.raises(ValueError, match="same seat more than once on one lane"):
        replace(spec(), lane_efforts=efforts)


# --- A red validator retries the builder one effort level higher ---------------


def _builder_efforts(ports: WorkflowPorts) -> list[EffortLevel | None]:
    return [request.effort for request in ports.harness.requests if request.seat is Seat.BUILDER]


def _builder_spec(level: EffortLevel | None) -> ItemExecutionSpec:
    if level is None:
        return replace(spec(), lane_efforts=(("", Seat.REVIEWER, EffortLevel.LOW),))
    return replace(spec(), lane_efforts=(("", Seat.BUILDER, level),))


def test_a_red_validator_retries_the_builder_one_level_higher_with_its_output() -> None:
    ports = make_ports(
        [builder("first"), builder("retry"), review(ReviewDecision.PASS)],
        [command(exit_code=1, suffix="red"), command(suffix="green")],
        diff_count=2,
    )

    outcome = ItemWorkflow(ports).run(
        campaign(), _builder_spec(EffortLevel.MEDIUM), ports.records.create(campaign())
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert _builder_efforts(ports) == [EffortLevel.MEDIUM, EffortLevel.HIGH]
    retry = ports.harness.requests[1]
    assert retry.seat is Seat.BUILDER
    assert "stdout:red" in retry.context_refs
    assert [
        payload["effort"]
        for payload in _seat_payloads(ports)
        if payload["seat"] == "builder"
    ] == ["medium", "high"]
    raised = [event for event in ports.records.events if event.event_type == "item.effort_raised"]
    assert [event.payload["effort"] for event in raised] == ["high"]
    assert render_progress_line(raised[0]).endswith(
        " item item-1 validator did not pass; a fresh builder retries at effort high "
        "(was medium)"
    )


def test_every_red_validator_runs_medium_then_high_and_parks() -> None:
    ports = make_ports(
        [builder("first"), builder("retry")],
        [command(exit_code=1, suffix="1"), command(exit_code=1, suffix="2")],
        diff_count=2,
    )

    outcome = ItemWorkflow(ports).run(
        campaign(), _builder_spec(EffortLevel.MEDIUM), ports.records.create(campaign())
    )

    assert outcome.disposition is ItemDisposition.PARKED
    assert "validator validator did not pass" in (outcome.reason or "")
    assert _builder_efforts(ports) == [EffortLevel.MEDIUM, EffortLevel.HIGH]


@pytest.mark.parametrize("level", [EffortLevel.HIGH, EffortLevel.XHIGH, EffortLevel.MAX, None])
def test_a_red_validator_at_high_or_without_a_builder_effort_parks_after_one_seat(
    level: EffortLevel | None,
) -> None:
    ports = make_ports([builder()], [command(exit_code=1)], diff_count=1)

    outcome = ItemWorkflow(ports).run(
        campaign(), _builder_spec(level), ports.records.create(campaign())
    )

    assert outcome.disposition is ItemDisposition.PARKED
    assert "did not pass" in (outcome.reason or "")
    assert _builder_efforts(ports) == [level]


def test_a_raised_level_holds_for_a_later_correction_round() -> None:
    ports = make_ports(
        [
            builder("first"), builder("retry"), review(ReviewDecision.CORRECT, "1"),
            builder("fold-1"), review(ReviewDecision.PASS, "2"),
        ],
        [command(exit_code=1, suffix="1"), command(suffix="2"), command(suffix="3")],
        diff_count=3,
    )

    outcome = ItemWorkflow(ports).run(
        campaign(), _builder_spec(EffortLevel.MEDIUM), ports.records.create(campaign())
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert _builder_efforts(ports) == [EffortLevel.MEDIUM, EffortLevel.HIGH, EffortLevel.HIGH]


def test_a_retry_does_not_count_toward_the_correction_ceiling() -> None:
    ports = make_ports(
        [
            builder("first"), builder("retry"), review(ReviewDecision.CORRECT, "1"),
            builder("fold-1"), review(ReviewDecision.PASS, "2"),
        ],
        [command(exit_code=1, suffix="1"), command(suffix="2"), command(suffix="3")],
        diff_count=3,
    )

    outcome = ItemWorkflow(ports).run(
        campaign(),
        replace(_builder_spec(EffortLevel.LOW), max_corrections=1),
        ports.records.create(campaign()),
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    rounds = [e.payload["round"] for e in ports.records.events if e.event_type == "item.correcting"]
    assert rounds == [1]
    assert _builder_efforts(ports) == [EffortLevel.LOW, EffortLevel.MEDIUM, EffortLevel.MEDIUM]


def test_a_red_correction_round_raises_the_level_again_up_to_high() -> None:
    ports = make_ports(
        [
            builder("first"), builder("retry-1"), review(ReviewDecision.CORRECT, "1"),
            builder("fold-1"), builder("retry-2"),
        ],
        [
            command(exit_code=1, suffix="1"), command(suffix="2"),
            command(exit_code=1, suffix="3"), command(exit_code=1, suffix="4"),
        ],
        diff_count=4,
    )

    outcome = ItemWorkflow(ports).run(
        campaign(), _builder_spec(EffortLevel.LOW), ports.records.create(campaign())
    )

    assert outcome.disposition is ItemDisposition.PARKED
    assert "did not pass" in (outcome.reason or "")
    assert _builder_efforts(ports) == [
        EffortLevel.LOW, EffortLevel.MEDIUM, EffortLevel.MEDIUM, EffortLevel.HIGH
    ]


def _started_builder_efforts(ports: WorkflowPorts) -> list[str | None]:
    return [
        event.payload["effort"]
        for event in ports.records.events
        if event.event_type == "seat.started" and event.payload["seat"] == "builder"
    ]


def _effort_raises(ports: WorkflowPorts) -> list[dict]:
    return [
        event.payload for event in ports.records.events if event.event_type == "item.effort_raised"
    ]


def test_a_red_validator_on_resumed_built_work_retries_the_builder_one_level_higher() -> None:
    ports = make_ports(
        [builder("retry"), review(ReviewDecision.PASS)],
        [command(exit_code=1, suffix="red"), command(suffix="green")],
        diff_count=2,
    )

    outcome = ItemWorkflow(ports).run(
        campaign(),
        _builder_spec(EffortLevel.MEDIUM),
        ports.records.create(campaign()),
        resume=ResumePlan("built", Path("/worktree"), 0, "run-old"),
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert _builder_efforts(ports) == [EffortLevel.HIGH]
    retry = next(r for r in ports.harness.requests if r.seat is Seat.BUILDER)
    assert "stdout:red" in retry.context_refs
    raised = _effort_raises(ports)
    assert [(p["from_effort"], p["effort"], p["raises"]) for p in raised] == [
        ("medium", "high", 1)
    ]


def test_a_red_validator_on_resumed_work_at_high_parks_without_a_builder_seat() -> None:
    ports = make_ports([], [command(exit_code=1, suffix="red")], diff_count=1)

    outcome = ItemWorkflow(ports).run(
        campaign(),
        _builder_spec(EffortLevel.HIGH),
        ports.records.create(campaign()),
        resume=ResumePlan("validated", Path("/worktree"), 0, "run-old"),
    )

    assert outcome.disposition is ItemDisposition.PARKED
    assert "validator validator did not pass" in (outcome.reason or "")
    assert _builder_efforts(ports) == []
    assert _effort_raises(ports) == []


_CONTINUATION = "Continuation: A previous builder seat timed out in this workspace"


def test_a_timed_out_raised_builder_continues_at_the_raised_level_with_its_trigger() -> None:
    timed_out = SeatResult(
        "builder", SeatOutcome.TIMED_OUT, None, "raw", SeatUsage(), "start", "end"
    )
    ports = make_ports(
        [builder("first"), timed_out, builder("continued"), review(ReviewDecision.PASS)],
        [command(exit_code=1, suffix="red"), command(suffix="green")],
        diff_count=2,
    )
    item_spec = _builder_spec(EffortLevel.MEDIUM)

    with pytest.raises(BuilderContinuation) as handed:
        ItemWorkflow(ports).run(campaign(), item_spec, ports.records.create(campaign()))
    plan = handed.value.plan
    assert (plan.builder_raises, plan.builder_context_refs) == (1, ("stdout:red",))

    outcome = ItemWorkflow(ports).run(
        campaign(), item_spec, ports.records.create(campaign()), resume=plan
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    continued = [r for r in ports.harness.requests if r.seat is Seat.BUILDER][-1]
    assert continued.effort is EffortLevel.HIGH
    assert "stdout:red" in continued.context_refs
    assert _CONTINUATION in continued.instructions
    assert _started_builder_efforts(ports) == ["medium", "high", "high"]


def test_a_continued_builder_writes_its_report_beside_the_first_builder_report() -> None:
    timed_out = SeatResult(
        "builder", SeatOutcome.TIMED_OUT, None, "raw", SeatUsage(), "start", "end"
    )
    ports = make_ports(
        [builder("first"), timed_out, builder("continued"), review(ReviewDecision.PASS)],
        [command(exit_code=1, suffix="red"), command(suffix="green")],
        diff_count=2,
    )
    item_spec = _builder_spec(EffortLevel.MEDIUM)

    with pytest.raises(BuilderContinuation) as handed:
        ItemWorkflow(ports).run(campaign(), item_spec, ports.records.create(campaign()))
    assert handed.value.plan.builder_reports == 1

    outcome = ItemWorkflow(ports).run(
        campaign(), item_spec, ports.records.create(campaign()), resume=handed.value.plan
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    first = ports.records.evidence["item-1-builder-report-1"]
    continued = ports.records.evidence["item-1-builder-report-2"]
    assert "report:first" in first and "report:continued" not in first
    assert "report:continued" in continued
    reviewer = next(r for r in ports.harness.requests if r.seat is Seat.REVIEWER)
    assert reviewer.context_refs[-1] == "evidence:item-1-builder-report-2"


def test_a_red_validator_retry_after_a_continued_builder_is_not_told_to_continue() -> None:
    timed_out = SeatResult(
        "builder", SeatOutcome.TIMED_OUT, None, "raw", SeatUsage(), "start", "end"
    )
    ports = make_ports(
        [timed_out, builder("continued"), builder("retry"), review(ReviewDecision.PASS)],
        [command(exit_code=1, suffix="red"), command(suffix="green")],
        diff_count=2,
    )
    item_spec = _builder_spec(EffortLevel.LOW)

    with pytest.raises(BuilderContinuation) as handed:
        ItemWorkflow(ports).run(campaign(), item_spec, ports.records.create(campaign()))
    resumed_from = len(ports.harness.requests)

    outcome = ItemWorkflow(ports).run(
        campaign(), item_spec, ports.records.create(campaign()), resume=handed.value.plan
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    continued, retry = [
        r for r in ports.harness.requests[resumed_from:] if r.seat is Seat.BUILDER
    ]
    assert (continued.effort, retry.effort) == (EffortLevel.LOW, EffortLevel.MEDIUM)
    assert _CONTINUATION in continued.instructions
    assert "stdout:red" in retry.context_refs
    assert "Continuation:" not in retry.instructions


def test_a_resume_from_a_launch_time_isolated_marker_starts_at_the_lane_level() -> None:
    ports = make_ports(
        [builder("rebuilt"), review(ReviewDecision.PASS)], [command()], diff_count=1
    )

    outcome = ItemWorkflow(ports).run(
        campaign(),
        _builder_spec(EffortLevel.MEDIUM),
        ports.records.create(campaign()),
        resume=ResumePlan("isolated", Path("/worktree"), 0, "run-old"),
    )

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert _builder_efforts(ports) == [EffortLevel.MEDIUM]
    rebuilt = next(r for r in ports.harness.requests if r.seat is Seat.BUILDER)
    assert rebuilt.context_refs == ()
    assert "Continuation:" not in rebuilt.instructions


def test_the_raised_effort_event_names_the_effort_of_the_next_builder_seat() -> None:
    ports, state = lane_ports(
        [builder("one")],
        [builder("two"), review(ReviewDecision.PASS)],
        [command(exit_code=1, suffix="red"), command(suffix="green")],
        diff_count=2,
    )
    validator_runs: list[str] = []

    def cool_lane_one_on_first_validator(request) -> None:
        validator_runs.append(request.command_id)
        if len(validator_runs) == 1:
            state.cool("lane-one", LaneSignal(LaneSignalKind.RATE_LIMIT), "other-campaign")

    ports.command.on_run = cool_lane_one_on_first_validator
    item_spec = replace(
        spec(),
        lane_efforts=(
            ("lane-one", Seat.BUILDER, EffortLevel.LOW),
            ("lane-two", Seat.BUILDER, EffortLevel.MEDIUM),
        ),
    )

    outcome = ItemWorkflow(ports).run(campaign(), item_spec, ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    raised = _effort_raises(ports)
    assert [(p["from_effort"], p["effort"]) for p in raised] == [("low", "high")]
    assert _started_builder_efforts(ports) == ["low", raised[0]["effort"]]


def test_raised_effort_caps_at_high_and_never_lowers_a_lane_level() -> None:
    assert raised_effort(EffortLevel.LOW, 1) is EffortLevel.MEDIUM
    assert raised_effort(EffortLevel.LOW, 5) is EffortLevel.HIGH
    assert raised_effort(EffortLevel.MEDIUM, 0) is EffortLevel.MEDIUM
    assert raised_effort(EffortLevel.XHIGH, 1) is EffortLevel.XHIGH
    assert raised_effort(EffortLevel.MAX, 2) is EffortLevel.MAX
    assert raised_effort(None, 1) is None


# --- Automatic resume from a good state ---------------------------------------


def _built_marker(
    worktree: Path = Path("/worktree"),
    head: str = "base",
    revision: str = "digest-built",
    state: str = "built",
) -> PhaseMarker:
    return PhaseMarker("item-1", state, worktree, "run", head, revision, 0, "run-old")


def test_a_built_marker_with_a_matching_worktree_resumes_and_is_accepted() -> None:
    ports = make_ports([review(ReviewDecision.PASS)], [command()], diff_count=1)
    ports.tracker.resumable = [item()]
    ports.records.markers = {"item-1": _built_marker()}
    ports.workspace.worktrees = [
        WorktreeStatus(Path("/worktree"), "run", "base", "digest-built")
    ]

    outcome = CampaignRunner(ports).run(campaign(max_items=5), lambda selected: spec(selected))

    assert [o.item_id for o in outcome.items] == ["item-1"]
    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED
    assert any(event.event_type == "item.resumed" for event in ports.records.events)
    assert ports.workspace.adopted == [Path("/worktree")]
    # the good build is not respent: only the reviewer seat runs on resume
    assert [request.seat.value for request in ports.harness.requests] == ["reviewer"]
    assert len(ports.tracker.closed) == 1
    resumed = next(event for event in ports.records.events if event.event_type == "item.resumed")
    assert resumed.payload["prior_run_id"] == "run-old"
    assert resumed.payload["marker_state"] == "built"


def test_a_resume_past_the_build_hands_the_reviewer_no_builder_report() -> None:
    from autobuild.application.prompts import NO_BUILDER_REPORT_SENTENCE

    ports = make_ports([review(ReviewDecision.PASS)], [command()], diff_count=1)
    ports.tracker.resumable = [item()]
    ports.records.markers = {"item-1": _built_marker()}
    ports.workspace.worktrees = [
        WorktreeStatus(Path("/worktree"), "run", "base", "digest-built")
    ]

    CampaignRunner(ports).run(campaign(max_items=5), lambda selected: spec(selected))

    (reviewer,) = ports.harness.requests
    assert reviewer.context_refs == ("patch:0", "stdout:1")
    assert NO_BUILDER_REPORT_SENTENCE in reviewer.instructions
    assert not [name for name in ports.records.evidence if "builder-report" in name]


def test_a_head_moved_marker_parks_with_resume_head_moved_and_campaign_continues() -> None:
    ports = make_ports(
        [builder("b2"), verdict_result("item-2", ReviewDecision.PASS, "2")],
        [command(suffix="2")],
        diff_count=1,
    )
    ports.tracker.resumable = [item()]
    ports.tracker.queue.append(WorkItem("item-2", "second", "plan", ("passes",)))
    ports.records.markers = {"item-1": _built_marker()}
    ports.workspace.worktrees = [
        WorktreeStatus(Path("/worktree"), "run", "moved-head", "digest-built")
    ]

    outcome = CampaignRunner(ports).run(campaign(max_items=5), lambda selected: spec(selected))

    assert outcome.items[0].item_id == "item-1"
    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert outcome.items[0].reason == "resume:head-moved"
    assert ("item-1", "resume:head-moved") in ports.tracker.parked
    assert outcome.items[1].item_id == "item-2"
    assert outcome.items[1].disposition is ItemDisposition.ACCEPTED


def test_a_held_lease_parks_with_resume_lease_held_and_is_not_touched() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.resumable = [item()]
    ports.records.markers = {"item-1": _built_marker()}
    ports.workspace.worktrees = [
        WorktreeStatus(Path("/worktree"), "run", "base", "digest-built")
    ]
    ports.lease.live_holders[str(Path("/worktree"))] = LeaseRecord(
        "other-campaign", 777, "other-host", "s", "b", Path("/worktree"), SurfaceKind.WORKTREE
    )

    outcome = CampaignRunner(ports).run(campaign(max_items=5), lambda selected: spec(selected))

    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert outcome.items[0].reason == "resume:lease-held"
    assert ("item-1", "resume:lease-held") in ports.tracker.parked
    assert ports.workspace.adopted == []


def test_a_revision_changed_marker_parks_and_snapshots_the_worktree_in_place() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.resumable = [item()]
    ports.records.markers = {"item-1": _built_marker(revision="digest-old")}
    ports.workspace.worktrees = [
        WorktreeStatus(Path("/worktree"), "run", "base", "digest-new")
    ]

    outcome = CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert outcome.items[0].reason == "resume:revision-changed"
    assert ports.workspace.adopted == [Path("/worktree")]
    assert ports.workspace.snapshotted == ["lease"]
    assert "item-1-park/snapshot.json" in ports.records.evidence_files
    # the worktree is left in place, not released
    assert ports.workspace.released == []


def test_a_claimed_item_with_no_marker_parks_and_is_not_attached() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.resumable = [item()]

    outcome = CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert outcome.items[0].reason == "resume:missing-marker"
    assert ports.workspace.adopted == []


def test_an_orphan_worktree_is_reported_under_parked_and_left_in_place() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.workspace.worktrees = [
        WorktreeStatus(Path("/scratch/worktrees/orphan-1"), "run", "base", "digest")
    ]

    outcome = CampaignRunner(ports).run(campaign(max_items=5), lambda selected: spec(selected))

    assert outcome.items == ()
    assert ports.workspace.adopted == []
    assert any(
        event.event_type == "resume.orphan_worktree" for event in ports.records.events
    )
    content = ports.workspace.reports[0].content
    assert "- orphan-1: resume:orphan-worktree" in content


def test_a_closed_marker_on_a_still_claimed_item_is_not_resumed() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.resumable = [item()]
    ports.records.markers = {"item-1": _built_marker(state="closed")}
    ports.workspace.worktrees = [
        WorktreeStatus(Path("/worktree"), "run", "base", "digest-built")
    ]

    outcome = CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert outcome.items[0].reason == "resume:tracker-mismatch"
    # adopted only to snapshot it in place: no seat runs and it is not released
    assert ports.workspace.adopted == [Path("/worktree")]
    assert ports.harness.requests == []
    assert ports.workspace.released == []


def test_a_correcting_marker_resumes_by_re_running_the_builder() -> None:
    ports = make_ports(
        [builder("redo"), review(ReviewDecision.PASS)], [command()], diff_count=1
    )
    ports.tracker.resumable = [item()]
    marker = PhaseMarker(
        "item-1", "correcting", Path("/worktree"), "run", "base", "digest-mid", 1, "run-old"
    )
    ports.records.markers = {"item-1": marker}
    ports.workspace.worktrees = [
        WorktreeStatus(Path("/worktree"), "run", "base", "digest-mid")
    ]

    outcome = CampaignRunner(ports).run(campaign(max_items=5), lambda selected: spec(selected))

    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED
    # a correcting marker re-runs the builder before validation and review
    assert [request.seat.value for request in ports.harness.requests] == ["builder", "reviewer"]


# --- Resume parks report each worktree once -----------------------------------


def _owned(ports, item_id: str, *roots: str) -> list[Path]:
    paths = [Path(root) for root in roots]
    for path in paths:
        ports.workspace.worktrees.append(WorktreeStatus(path, "run", "base", "digest"))
        ports.workspace.worktree_owners[path] = item_id
    return paths


def _resume_parked(ports) -> list[dict]:
    return [
        event.payload for event in ports.records.events if event.event_type == "item.resume_parked"
    ]


def _orphan_roots(ports) -> list[str]:
    return [
        event.payload["worktree_root"]
        for event in ports.records.events
        if event.event_type == "resume.orphan_worktree"
    ]


def _parked_section(ports) -> list[str]:
    content = ports.workspace.reports[0].content
    section = content.split("## Parked", 1)[1].split("\n## ", 1)[0]
    return [line for line in section.splitlines() if line.startswith("- ")]


def test_a_missing_marker_item_names_and_snapshots_its_one_worktree_and_no_orphan() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.resumable = [item()]
    (root,) = _owned(ports, "item-1", "/scratch/worktrees/item-1-0a1b2c3d")

    outcome = CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    assert outcome.items[0].reason == "resume:missing-marker"
    (parked,) = _resume_parked(ports)
    assert parked["worktree_roots"] == [str(root)]
    assert parked["snapshot_path"] == "evidence/item-1-park"
    assert ports.workspace.adopted == [root]
    assert "item-1-park/snapshot.json" in ports.records.evidence_files
    assert _orphan_roots(ports) == []
    assert _parked_section(ports) == ["- item-1: resume:missing-marker"]
    assert ports.workspace.released == []


def test_a_missing_marker_item_with_two_worktrees_names_both_and_snapshots_none() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.resumable = [item()]
    roots = _owned(
        ports, "item-1", "/scratch/worktrees/item-1-0a1b2c3d", "/scratch/worktrees/item-1-4e5f6a7b"
    )

    outcome = CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    assert outcome.items[0].reason == "resume:missing-marker"
    (parked,) = _resume_parked(ports)
    assert parked["worktree_roots"] == [str(root) for root in roots]
    assert parked["snapshot_path"] == ""
    assert ports.workspace.adopted == []
    assert _orphan_roots(ports) == []
    assert _parked_section(ports) == ["- item-1: resume:missing-marker"]


def test_an_isolated_marker_with_no_worktree_root_attaches_the_owned_worktree() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.resumable = [item()]
    ports.records.markers = {
        "item-1": PhaseMarker("item-1", "isolated", Path(""), "", "", "", 0, "run-old")
    }
    (root,) = _owned(ports, "item-1", "/scratch/worktrees/item-1-0a1b2c3d")

    outcome = CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    assert outcome.items[0].reason == "resume:missing-worktree"
    (parked,) = _resume_parked(ports)
    assert parked["worktree_roots"] == [str(root)]
    assert parked["snapshot_path"] == "evidence/item-1-park"
    assert _orphan_roots(ports) == []
    assert _parked_section(ports) == ["- item-1: resume:missing-worktree"]


def test_a_worktree_no_claimed_item_owns_is_still_one_orphan() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.resumable = [item()]
    _owned(ports, "item-1", "/scratch/worktrees/item-1-0a1b2c3d")
    (stray,) = _owned(ports, "item-9", "/scratch/worktrees/item-9-0a1b2c3d")

    CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    assert _orphan_roots(ports) == [str(stray)]
    assert _parked_section(ports) == [
        "- item-1: resume:missing-marker",
        "- item-9-0a1b2c3d: resume:orphan-worktree",
    ]


def test_a_claimed_item_outside_the_selection_keeps_its_worktree_off_the_orphan_list() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.resumable = [
        WorkItem("item-in", "inside the fence", "plan", ("passes",)),
        WorkItem("item-out", "outside the fence", "plan", ("passes",)),
    ]
    _owned(ports, "item-in", "/scratch/worktrees/item-in-0a1b2c3d")
    (fenced_out,) = _owned(ports, "item-out", "/scratch/worktrees/item-out-0a1b2c3d")
    stray = Path("/scratch/worktrees/stray-0a1b2c3d")
    ports.workspace.worktrees.append(WorktreeStatus(stray, "run", "base", "digest"))

    CampaignRunner(ports).run(
        campaign(selection=CampaignSelection(allow=("item-in",))),
        lambda selected: spec(selected),
    )

    assert _orphan_roots(ports) == [str(stray)]
    assert fenced_out not in ports.workspace.adopted
    parked = _parked_section(ports)
    assert not any("item-out" in line for line in parked)
    assert parked.count("- stray-0a1b2c3d: resume:orphan-worktree") == 1


def test_a_tracker_mismatch_snapshots_the_marker_worktree() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.resumable = [item()]
    ports.records.markers = {"item-1": _built_marker(state="parked")}
    ports.workspace.worktrees = [
        WorktreeStatus(Path("/worktree"), "run", "base", "digest-built")
    ]

    outcome = CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    assert outcome.items[0].reason == "resume:tracker-mismatch"
    (parked,) = _resume_parked(ports)
    assert parked["worktree_roots"] == [str(Path("/worktree"))]
    assert parked["snapshot_path"] == "evidence/item-1-park"
    assert "item-1-park/snapshot.json" in ports.records.evidence_files
    assert ports.workspace.released == []
    assert _orphan_roots(ports) == []


def test_a_tracker_mismatch_under_a_live_foreign_lease_writes_no_snapshot() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.resumable = [item()]
    ports.records.markers = {"item-1": _built_marker(state="parked")}
    ports.workspace.worktrees = [
        WorktreeStatus(Path("/worktree"), "run", "base", "digest-built")
    ]
    ports.lease.live_holders[str(Path("/worktree"))] = LeaseRecord(
        "other-campaign", 777, "other-host", "s", "b", Path("/worktree"), SurfaceKind.WORKTREE
    )

    outcome = CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    assert outcome.items[0].reason == "resume:tracker-mismatch"
    (parked,) = _resume_parked(ports)
    assert parked["snapshot_path"] == ""
    assert ports.workspace.adopted == []
    assert not any(path.startswith("item-1-park/") for path in ports.records.evidence_files)


def test_a_held_lease_park_is_never_snapshotted() -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.resumable = [item()]
    ports.records.markers = {"item-1": _built_marker()}
    ports.workspace.worktrees = [
        WorktreeStatus(Path("/worktree"), "run", "base", "digest-built")
    ]
    ports.lease.live_holders[str(Path("/worktree"))] = LeaseRecord(
        "other-campaign", 777, "other-host", "s", "b", Path("/worktree"), SurfaceKind.WORKTREE
    )

    CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    (parked,) = _resume_parked(ports)
    assert parked["snapshot_path"] == ""
    assert parked["worktree_roots"] == [str(Path("/worktree"))]
    assert ports.workspace.snapshotted == []


# --- Owner progress lines ------------------------------------------------------


def _progress_ports(
    harness_results: list[SeatResult],
    command_results: list[CommandResult],
    diff_count: int,
) -> tuple[WorkflowPorts, FakeProgressPort]:
    base = make_ports(harness_results, command_results, diff_count=diff_count)
    progress = FakeProgressPort()
    ports = replace(base, progress=progress)
    return ports, progress


def _bodies(progress: FakeProgressPort) -> list[str]:
    """The progress lines with their leading UTC timestamp stripped, after
    asserting each line carries a real UTC ISO 8601 stamp."""

    bodies: list[str] = []
    for line in progress.lines:
        stamp, _, body = line.partition(" ")
        when = datetime.fromisoformat(stamp)
        assert when.tzinfo is not None and when.utcoffset() == timedelta(0)
        bodies.append(body)
    return bodies


def test_progress_sequence_for_one_accepted_and_one_parked_item() -> None:
    ports, progress = _progress_ports(
        [
            builder("b1"),
            verdict_result("item-1", ReviewDecision.PASS, "1"),
            builder("b2"),
            verdict_result("item-2", ReviewDecision.PARK, "2"),
        ],
        [command(suffix="1"), command(suffix="2")],
        diff_count=2,
    )
    ports.tracker.queue.extend([item(), WorkItem("item-2", "second", "plan", ("passes",))])

    outcome = CampaignRunner(ports).run(campaign(max_items=2), lambda selected: spec(selected))

    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED
    assert outcome.items[1].disposition is ItemDisposition.PARKED
    assert len(progress.began) == 1
    # begin runs before the first emit; the first line is the campaign start.
    assert progress.lines and progress.began
    bodies = _bodies(progress)
    assert bodies == [
        "campaign started: harness , models , efforts none, up to 2 items",
        "item item-1 claimed: test item",
        "item item-1 seat builder started: model class builder-class, effort default",
        "item item-1 seat builder succeeded",
        "item item-1 validation started: validator validator",
        "item item-1 validation passed",
        "item item-1 seat reviewer started: model class reviewer-class, effort default",
        "item item-1 seat reviewer succeeded in 0.0 min, estimated cost 0.01",
        "item item-1 review pass with 0 finding(s)",
        "item item-1 delivery started: target branch main",
        "item item-1 delivered: item item-commit, tracker tracker-commit, pushed True",
        "item item-2 claimed: second",
        "item item-2 seat builder started: model class builder-class, effort default",
        "item item-2 seat builder succeeded",
        "item item-2 validation started: validator validator",
        "item item-2 validation passed",
        "item item-2 seat reviewer started: model class reviewer-class, effort default",
        "item item-2 seat reviewer succeeded in 0.0 min, estimated cost 0.01",
        "item item-2 review park with 1 finding(s)",
        "item item-2 delivery started: target branch main",
        "item item-2 parked: review disposition was park",
        "campaign completed: shipped 1, parked 1, failed 0; "
        "report docs/campaigns/campaign.md; stop item_bound",
    ]


def test_progress_ref_names_the_progress_log_under_the_run_record(tmp_path: Path) -> None:
    base = make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1)
    records = LocalRunRecordAdapter(tmp_path / "runs")
    ports = replace(base, records=records)
    ports.tracker.queue.append(item())

    outcome = CampaignRunner(ports).run(campaign(max_items=1), lambda selected: spec(selected))

    run_dir = next((tmp_path / "runs").iterdir())
    assert outcome.progress_ref == str(run_dir / "progress.log")


CORRECTION_EVIDENCE = "verdict:first"


def test_a_correction_round_appends_item_correcting_with_round_and_trigger() -> None:
    ports = make_ports(
        [
            builder("first"),
            review(ReviewDecision.CORRECT, "first"),
            builder("fold"),
            review(ReviewDecision.PASS, "clean"),
        ],
        [command(suffix="first"), command(suffix="fold")],
        diff_count=2,
    )

    ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    correcting = [e for e in ports.records.events if e.event_type == "item.correcting"]
    assert len(correcting) == 1
    assert correcting[0].item_id == "item-1"
    assert correcting[0].payload["round"] == 1
    assert correcting[0].payload["triggering_evidence_ref"] == CORRECTION_EVIDENCE


def _event(event_type: str, item_id: str | None, payload: dict[str, object]) -> RunEvent:
    return RunEvent(
        event_type=event_type,
        occurred_at="2026-09-04T12:00:00+00:00",
        item_id=item_id,
        payload=payload,
    )


@pytest.mark.parametrize(
    "event,expected",
    [
        (
            _event(
                "campaign.started",
                None,
                {
                    "harness": "codex",
                    "models": {"builder": "m-b", "reviewer": "m-r"},
                    "efforts": {"builder": "high", "reviewer": None, "specialist": None},
                    "item_bound": 5,
                },
            ),
            "campaign started: harness codex, models builder m-b, reviewer m-r, efforts builder high, up to 5 items",
        ),
        (_event("item.claimed", "item-1", {"title": "Do the thing"}), "item item-1 claimed: Do the thing"),
        (
            _event(
                "seat.completed",
                "item-1",
                {"seat": "builder", "outcome": "succeeded", "duration_seconds": 120.0, "cost": 0.5},
            ),
            "item item-1 seat builder succeeded in 2.0 min, estimated cost 0.50",
        ),
        (_event("validation.completed", "item-1", {"exit_code": 0}), "item item-1 validation passed"),
        (
            _event("validation.completed", "item-1", {"exit_code": 1}),
            "item item-1 validation failed (exit 1)",
        ),
        (
            _event("review.completed", "item-1", {"decision": "pass", "finding_codes": ["A", "B"]}),
            "item item-1 review pass with 2 finding(s)",
        ),
        (
            _event("item.correcting", "item-1", {"round": 1, "triggering_evidence_ref": "verdict:1"}),
            "item item-1 correction round 1",
        ),
        (_event("item.parked", "item-1", {"reason": "nature:machine"}), "item item-1 parked: nature:machine"),
        (
            _event(
                "item.finalised",
                "item-1",
                {"item_commit": "abc", "tracker_commit": "def", "pushed": True},
            ),
            "item item-1 delivered: item abc, tracker def, pushed True",
        ),
        (
            _event(
                "campaign.completed",
                None,
                {
                    "accepted": 2,
                    "parked": 1,
                    "failed": 0,
                    "report": "docs/campaigns/c.md",
                    "stop_reason": "item_bound",
                },
            ),
            "campaign completed: shipped 2, parked 1, failed 0; "
            "report docs/campaigns/c.md; stop item_bound",
        ),
    ],
)
def test_render_progress_line_is_fixed_and_timestamped(event: RunEvent, expected: str) -> None:
    rendered = render_progress_line(event)
    assert rendered == f"2026-09-04T12:00:00+00:00 {expected}"
    assert rendered.isascii()
    assert "/worktree" not in rendered and ":\\" not in rendered


@pytest.mark.parametrize(
    ("efforts", "expected"),
    [
        (
            {"builder": EffortLevel.HIGH, "reviewer": None, "specialist": None},
            {"builder": "high", "reviewer": None, "specialist": None},
        ),
        (
            {"builder": None, "reviewer": None, "specialist": None},
            {"builder": None, "reviewer": None, "specialist": None},
        ),
    ],
)
def test_campaign_started_serializes_requested_efforts(
    efforts: dict[str, EffortLevel | None], expected: dict[str, str | None]
) -> None:
    ports = make_ports([], [], diff_count=0)
    context = CampaignContext(
        harness="test",
        models={"builder": "model"},
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        validator_id="tests",
        target_branch="main",
        target_revision="base",
        efforts=efforts,
    )

    CampaignRunner(ports).run(campaign(), lambda selected: spec(selected), context=context)

    started = next(event for event in ports.records.events if event.event_type == "campaign.started")
    assert started.payload["efforts"] == expected


def test_campaign_context_keeps_old_positional_optional_fields_with_keyword_efforts() -> None:
    ports = make_ports([], [], diff_count=0)
    surface = LeaseSurface(Path("/repo/.ergon"), SurfaceKind.TRACKER)
    context = CampaignContext(
        "test",
        {"builder": "model"},
        DeliveryMode.PROTECTED_DEFAULT,
        "tests",
        "main",
        "base",
        True,
        True,
        surface,
        efforts={"builder": EffortLevel.HIGH, "reviewer": None, "specialist": None},
    )

    CampaignRunner(ports).run(campaign(), lambda selected: spec(selected), context=context)

    assert context.push_current_branch is True
    assert context.allow_current_branch_default is True
    assert context.tracker_surface == surface
    started = next(event for event in ports.records.events if event.event_type == "campaign.started")
    assert started.payload["efforts"] == {
        "builder": "high",
        "reviewer": None,
        "specialist": None,
    }


# --- Tracker claim taken and tracker stops ---------------------------------------


def test_a_claim_taken_by_another_writer_selects_the_next_item() -> None:
    ports = make_ports(
        [builder("b2"), verdict_result("item-2", ReviewDecision.PASS, "2")],
        [command(suffix="2")],
        diff_count=1,
    )
    second = WorkItem("item-2", "second", "plan", ("passes",))
    ports.tracker.queue.append(item())
    ports.tracker.ready.extend([item(), second])
    ports.tracker.claim_errors["item-1"] = ClaimTaken("item-1", "superseded")

    outcome = CampaignRunner(ports).run(campaign(max_items=2), lambda selected: spec(selected))

    assert [entry.item_id for entry in outcome.items] == ["item-2"]
    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED
    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert [item_id for item_id, _ in ports.tracker.claims] == ["item-2"]
    assert ports.workspace.created == ["item-2"]
    taken = [event for event in ports.records.events if event.event_type == "item.claim_taken"]
    assert [event.item_id for event in taken] == ["item-1"]
    assert render_progress_line(taken[0]).endswith("selecting the next item")


@pytest.mark.parametrize(
    ("environment", "code", "reason"),
    (
        (True, 4, CampaignStopReason.TRACKER_ENVIRONMENT),
        (True, None, CampaignStopReason.TRACKER_ENVIRONMENT),
        (False, 1, CampaignStopReason.TRACKER_REFUSED),
        (False, 2, CampaignStopReason.TRACKER_REFUSED),
        (False, 5, CampaignStopReason.TRACKER_REFUSED),
        (False, 6, CampaignStopReason.TRACKER_REFUSED),
        (False, 7, CampaignStopReason.TRACKER_REFUSED),
    ),
)
def test_a_tracker_failure_on_claim_fails_only_the_item_with_the_code_and_message(
    environment: bool, code: int | None, reason: CampaignStopReason
) -> None:
    ports = make_ports([], [], diff_count=0)
    ports.tracker.queue.append(item())
    message = f"pinax claim exit {code}: tracker message"
    ports.tracker.claim_errors["item-1"] = TrackerStop(message, code=code, environment=environment)

    outcome = CampaignRunner(ports).run(campaign(max_items=1), lambda selected: spec(selected))

    assert outcome.stop_reason is CampaignStopReason.ITEM_BOUND
    assert [entry.item_id for entry in outcome.items] == ["item-1"]
    assert outcome.items[0].disposition is ItemDisposition.FAILED
    assert outcome.items[0].reason == message
    assert outcome.items[0].tracker_stop is reason
    assert ports.tracker.claims == []
    assert ports.tracker.parked == []
    assert ports.workspace.created == []
    failed = next(
        event for event in ports.records.events if event.event_type == "item.tracker_failed"
    )
    assert failed.payload == {"kind": reason.value, "exit_code": code, "error": message}
    assert ports.tracker.recoveries == 1


def test_a_tracker_failure_on_close_parks_from_the_primary_and_keeps_the_worktree() -> None:
    class _CloseRefusingTracker(FakeTrackerAdapter):
        def close(self, evidence, item_commit, workspace, actor):
            raise TrackerStop("pinax done exit 5: push rejected", code=5, environment=False)

    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1)
    ports = WorkflowPorts(
        _CloseRefusingTracker(identity("tracker")),
        ports.workspace,
        ports.harness,
        ports.command,
        ports.records,
        ports.knowledge,
        lease=ports.lease,
    )
    ports.tracker.queue.append(item())

    outcome = CampaignRunner(ports).run(campaign(max_items=1), lambda selected: spec(selected))

    assert outcome.stop_reason is CampaignStopReason.ITEM_BOUND
    assert [entry.item_id for entry in outcome.items] == ["item-1"]
    assert outcome.items[0].disposition is ItemDisposition.PARKED
    assert "exit 5" in (outcome.items[0].reason or "")
    assert outcome.items[0].tracker_stop is CampaignStopReason.TRACKER_REFUSED
    assert [item_id for item_id, _ in ports.tracker.parked] == ["item-1"]
    assert ports.workspace.released == []
    assert ports.workspace.delivered == []
    assert [item_id for item_id, _ in ports.tracker.claims] == ["item-1"]
    states = outcome.items[0].states
    assert states[-1] is ItemState.PARKED
    assert ItemState.CLAIMED in states
    assert _worktree_surfaces(ports) == [(SurfaceKind.WORKTREE, Path("/worktree"))]


def test_unpublished_tracker_writes_are_recorded_in_progress_and_report() -> None:
    note = "pinax: the push was refused because feature is not the origin default branch main"
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1)
    ports.tracker.publication = TrackerPublication(False, note)
    ports.tracker.queue.append(item())

    outcome = CampaignRunner(ports).run(
        campaign(refill_enabled=True, max_items=2),
        lambda selected: spec(selected),
        refill=RefillPlan(
            (Proposal("Candidate", "What next?", "Queue is dry", "docs/candidate.md"),), ()
        ),
    )

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED
    unpublished = [
        event for event in ports.records.events if event.event_type == "tracker.unpublished"
    ]
    assert [(event.item_id, event.payload["operation"]) for event in unpublished] == [
        ("item-1", "claim"),
        ("proposal-1", "proposal"),
    ]
    assert all(event.payload["note"] == note for event in unpublished)
    assert "committed and not yet published" in render_progress_line(unpublished[0])
    report = ports.workspace.reports[0].content
    assert "## Unpublished tracker writes" in report
    assert "- item-1 claim" in report and "- proposal-1 proposal" in report


def test_published_tracker_writes_add_no_unpublished_record() -> None:
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1)
    ports.tracker.queue.append(item())

    CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    assert not any(event.event_type == "tracker.unpublished" for event in ports.records.events)
    assert "Unpublished tracker writes" not in ports.workspace.reports[0].content


def test_a_lane_flip_after_an_unpublished_claim_publishes_no_second_claim() -> None:
    ports, _state = lane_ports(
        [failed_builder("one")],
        [builder("two"), review(ReviewDecision.PASS)],
        [command()],
        diff_count=1,
        one_signal=LaneSignal(LaneSignalKind.RATE_LIMIT),
    )
    ports.tracker.publication = TrackerPublication(False, "committed on the pull request branch")

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    positions = [
        index
        for index, event in enumerate(ports.records.events)
        if event.event_type == "tracker.unpublished" and event.payload["operation"] == "claim"
    ]
    assert len(positions) == 1
    changed = [event.event_type for event in ports.records.events].index("item.lane_changed")
    assert positions[0] < changed
    assert ports.tracker.claims == [("item-1", "builder@lane-one")]


# --- The builder report handed to the reviewer --------------------------------


def _builder_report(ports: WorkflowPorts, reference: str) -> str:
    assert reference.startswith("evidence:")
    return ports.records.evidence[reference.removeprefix("evidence:")]


def _accepted_run() -> WorkflowPorts:
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1)
    ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))
    return ports


def _corrected_run() -> WorkflowPorts:
    ports = make_ports(
        [
            builder("first"),
            review(ReviewDecision.CORRECT, "1"),
            builder("fold"),
            review(ReviewDecision.PASS, "2"),
        ],
        [command(suffix="1"), command(suffix="2")],
        diff_count=2,
    )
    ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))
    return ports


def test_the_reviewer_is_handed_the_builder_report_as_evidence() -> None:
    ports = _accepted_run()

    reviewer = ports.harness.requests[1]
    assert reviewer.seat is Seat.REVIEWER
    assert len(reviewer.context_refs) == 3
    assert reviewer.context_refs[:2] == ("patch:0", "stdout:1")
    content = _builder_report(ports, reviewer.context_refs[2])
    assert "built" in content and "report:builder" in content
    assert json.loads(content) == {"summary": "built", "report_ref": "report:builder"}
    assert prompts.BUILDER_REPORT_SENTENCE in reviewer.instructions
    assert reviewer.context_refs[2] in reviewer.instructions
    assert prompts.NO_BUILDER_REPORT_SENTENCE not in reviewer.instructions


def test_each_reviewer_gets_the_latest_builder_report_of_its_round() -> None:
    ports = _corrected_run()

    reviewers = [r for r in ports.harness.requests if r.seat is Seat.REVIEWER]
    assert len(reviewers) == 2
    first = _builder_report(ports, reviewers[0].context_refs[2])
    second = _builder_report(ports, reviewers[1].context_refs[2])
    assert "report:first" in first
    assert "report:fold" in second and "report:first" not in second
    assert reviewers[0].context_refs[2] != reviewers[1].context_refs[2]
    assert "item-1-builder-report-1" in ports.records.evidence
    assert "item-1-builder-report-2" in ports.records.evidence


def test_no_reviewer_is_handed_a_builder_transcript() -> None:
    transcripts = ("raw:builder", "raw:first", "raw:fold")
    for ports in (_accepted_run(), _corrected_run()):
        for request in ports.harness.requests:
            if request.seat is Seat.BUILDER:
                continue
            assert not set(transcripts) & set(request.context_refs)
            for reference in request.context_refs:
                if "builder-report" in reference:
                    content = _builder_report(ports, reference)
                    assert not [raw for raw in transcripts if raw in content]
