from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from autobuild.application import CampaignRunner, ItemWorkflow, WorkflowPorts
from autobuild.domain import (
    AdapterIdentity,
    BuilderReport,
    CampaignRef,
    CampaignStopReason,
    CommandResult,
    DiffEvidence,
    DeliveryMode,
    FogRecord,
    ItemDisposition,
    ItemExecutionSpec,
    ItemState,
    Proposal,
    RefillPlan,
    ReviewDecision,
    ReviewFinding,
    ReviewVerdict,
    SeatOutcome,
    SeatResult,
    SeatUsage,
    ToolPolicy,
    WorkItem,
    WorkspaceRef,
)
from autobuild.testing import (
    FakeCommandAdapter,
    FakeHarnessAdapter,
    FakeKnowledgeAdapter,
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
        findings = (ReviewFinding("finding", "material consequence", f"finding:{suffix}"),)
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
    return WorkflowPorts(tracker, workspace, harness, commands, records, knowledge)


def item() -> WorkItem:
    return WorkItem("item-1", "test item", "plan", ("passes",))


def spec(work_item: WorkItem | None = None, max_corrections: int = 2) -> ItemExecutionSpec:
    return ItemExecutionSpec(
        work_item or item(),
        Path("brief.md"),
        "validator",
        ("python", "-m", "pytest", "tests/unit/test_workflows.py"),
        ToolPolicy(frozenset({"python"}), (Path("/worktree"),)),
        "builder-class",
        "reviewer-class",
        "specialist-class",
        max_corrections=max_corrections,
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        delivery_target_branch="main",
        delivery_target_revision="base",
    )


def campaign(*, refill_enabled: bool = False, max_items: int = 1) -> CampaignRef:
    return CampaignRef("campaign", Path("/repo"), max_items=max_items, refill_enabled=refill_enabled)


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


@pytest.mark.parametrize("seat_outcome", [SeatOutcome.TIMED_OUT, SeatOutcome.CANCELLED, SeatOutcome.FAILED])
def test_builder_failure_parks_honestly(seat_outcome: SeatOutcome) -> None:
    failed = SeatResult("builder", seat_outcome, None, "raw", SeatUsage(), "start", "end")
    ports = make_ports([failed], [], diff_count=0)

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.PARKED
    assert seat_outcome.value in (outcome.reason or "")
    assert ports.workspace.released == ["lease"]


def test_incomplete_review_evidence_stops_the_campaign_structurally() -> None:
    incomplete = SeatResult("review", SeatOutcome.SUCCEEDED, None, "raw", SeatUsage(), "start", "end")
    ports = make_ports([builder(), incomplete], [command()], diff_count=1)
    ports.tracker.queue.append(item())

    outcome = CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))

    assert outcome.stop_reason is CampaignStopReason.STRUCTURAL_FAILURE
    assert outcome.items[0].structural_failure is True
    assert outcome.items[0].disposition is ItemDisposition.PARKED


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
    assert len(ports.tracker.queue) == 1
