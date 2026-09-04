from __future__ import annotations

import json
from datetime import datetime, timedelta
from dataclasses import replace
from pathlib import Path

import pytest

from autobuild.adapters import LocalRunRecordAdapter
from autobuild.application import CampaignRunner, ItemWorkflow, Lane, WorkflowPorts
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
    EvidenceError,
    FogRecord,
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
from autobuild.domain import ScopeFenceViolation
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
    assert ports.harness.requests[1].context_refs == ("patch:0", "stdout:1")
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


def test_correction_ceiling_parks_instead_of_thrashing() -> None:
    ports = make_ports(
        [
            builder("initial"), review(ReviewDecision.CORRECT, "1"),
            builder("fold-1"), review(ReviewDecision.CORRECT, "2"),
            builder("fold-2"), review(ReviewDecision.CORRECT, "3"),
        ],
        [command(suffix="1"), command(suffix="2"), command(suffix="3")],
        diff_count=3,
    )

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.PARKED
    assert "correction ceiling" in (outcome.reason or "")
    assert outcome.finalise is not None
    assert outcome.finalise.item_commit is None
    assert outcome.finalise.tracker_commit == "tracker-commit"
    assert len(ports.harness.requests) == 6
    assert ports.workspace.released == ["lease"]


def test_specialist_is_added_only_after_an_escalation() -> None:
    ports = make_ports(
        [builder(), review(ReviewDecision.ESCALATE), review(ReviewDecision.PASS, "specialist")],
        [command()],
        diff_count=1,
    )

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    assert [request.seat.value for request in ports.harness.requests] == ["builder", "reviewer", "specialist"]


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
    assert outcome.follow_ups == ("Follow-up: item-1 STYLE", "Follow-up: item-1 DOCS")
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


@pytest.mark.parametrize(
    "seat_outcome,expected_reason",
    [
        (SeatOutcome.TIMED_OUT, "timeout:builder"),
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


def test_correction_ceiling_snapshots_tracked_patch_and_untracked_files() -> None:
    ports = make_ports(
        [
            builder("initial"), review(ReviewDecision.CORRECT, "1"),
            builder("fold-1"), review(ReviewDecision.CORRECT, "2"),
            builder("fold-2"), review(ReviewDecision.CORRECT, "3"),
        ],
        [command(suffix="1"), command(suffix="2"), command(suffix="3")],
        diff_count=3,
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


def test_incomplete_review_evidence_stops_the_campaign_structurally() -> None:
    incomplete = SeatResult("review", SeatOutcome.SUCCEEDED, None, "raw", SeatUsage(), "start", "end")
    ports = make_ports([builder(), incomplete], [command()], diff_count=1)
    ports.tracker.queue.append(item())

    outcome = CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    assert outcome.stop_reason is CampaignStopReason.STRUCTURAL_FAILURE
    assert outcome.items[0].structural_failure is True
    assert outcome.items[0].disposition is ItemDisposition.PARKED


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


def test_unreachable_merged_commit_is_a_structural_failure() -> None:
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()], diff_count=1)
    ports.workspace.unreachable_merge = True
    ports.tracker.queue.append(item())

    outcome = CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    assert outcome.stop_reason is CampaignStopReason.STRUCTURAL_FAILURE
    assert outcome.items[0].structural_failure is True
    assert "not reachable" in (outcome.items[0].reason or "")


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
                follow_ups=("Follow-up: item-1 STYLE",),
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
    assert "- Follow-up: item-1 STYLE" in content
    # The new run-record section is the final section, after the five existing
    # ones and the seat-usage table, and names the progress log.
    assert content.index("## Seat usage") < content.index("## Run record")
    assert "progress log" in content


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

    assert outcome.stop_reason is CampaignStopReason.STRUCTURAL_FAILURE
    assert outcome.items[0].disposition is ItemDisposition.FAILED
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


def test_out_of_allow_list_next_item_stops_with_scope_fence_violation() -> None:
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

    assert outcome.stop_reason is CampaignStopReason.SCOPE_FENCE_VIOLATION
    assert base.claims == []
    assert outcome.items == ()


def test_report_lists_a_blocked_allowed_item_with_reason_blocked() -> None:
    ready_item = WorkItem("a", "Item A", "plan", ("passes",))
    failed = SeatResult("builder", SeatOutcome.FAILED, None, "raw", SeatUsage(), "start", "end")
    ports = make_ports([failed], [], diff_count=0)
    ports.tracker.ready = [ready_item]

    outcome = CampaignRunner(ports).run(
        campaign(max_items=5, selection=CampaignSelection(allow=("a", "b"))),
        lambda selected: spec(selected),
    )

    assert outcome.items[0].item_id == "a"
    assert outcome.items[0].disposition is ItemDisposition.PARKED
    content = ports.workspace.reports[0].content
    assert "## Allowed items left unbuilt" in content
    assert "- b: blocked" in content


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
    assert ("item-1", "builder@lane-one") in ports.tracker.claims
    assert ("item-1", "builder@lane-two") in ports.tracker.claims
    assert [lane for lane, _signal, _campaign in state.cools] == ["lane-one"]
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


def test_signals_on_both_lanes_park_with_the_signature_and_stop_lanes_exhausted() -> None:
    ports, state = lane_ports(
        [failed_builder("one")],
        [failed_builder("two")],
        [],
        diff_count=0,
        one_signal=LaneSignal(LaneSignalKind.RATE_LIMIT),
        two_signal=LaneSignal(LaneSignalKind.RATE_LIMIT),
    )
    ports.tracker.queue.append(item())

    outcome = CampaignRunner(ports).run(campaign(max_items=2), lambda selected: spec(selected))

    assert outcome.stop_reason is CampaignStopReason.LANES_EXHAUSTED
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
    assert ("item-1", "builder@lane-one") in ports.tracker.claims
    assert ("item-1", "builder@lane-two") not in ports.tracker.claims


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
    assert ports.workspace.adopted == []


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
        "campaign started: harness , models , up to 2 items",
        "item item-1 claimed: test item",
        "item item-1 seat builder succeeded",
        "item item-1 validation passed",
        "item item-1 seat reviewer succeeded in 0.0 min, cost 0.01",
        "item item-1 review pass with 0 finding(s)",
        "item item-1 delivered: item item-commit, tracker tracker-commit, pushed True",
        "item item-2 claimed: second",
        "item item-2 seat builder succeeded",
        "item item-2 validation passed",
        "item item-2 seat reviewer succeeded in 0.0 min, cost 0.01",
        "item item-2 review park with 1 finding(s)",
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
                {"harness": "codex", "models": {"builder": "m-b", "reviewer": "m-r"}, "item_bound": 5},
            ),
            "campaign started: harness codex, models builder m-b, reviewer m-r, up to 5 items",
        ),
        (_event("item.claimed", "item-1", {"title": "Do the thing"}), "item item-1 claimed: Do the thing"),
        (
            _event(
                "seat.completed",
                "item-1",
                {"seat": "builder", "outcome": "succeeded", "duration_seconds": 120.0, "cost": 0.5},
            ),
            "item item-1 seat builder succeeded in 2.0 min, cost 0.5",
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
