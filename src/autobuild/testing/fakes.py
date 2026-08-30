"""Small scripted adapters; truth in tests comes from their recorded calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from autobuild.domain import (
    AdapterIdentity,
    CampaignRef,
    ClaimReceipt,
    CloseEvidence,
    CommandRequest,
    CommandResult,
    DeliveryRequest,
    DiffEvidence,
    DurableContext,
    FinaliseRequest,
    FinaliseResult,
    FogRecord,
    ProbeResult,
    Proposal,
    ProposalRef,
    RepositoryIdentity,
    RunEvent,
    RunRecordRef,
    SeatRequest,
    SeatResult,
    SeatUsage,
    WorkItem,
    WorkspaceRef,
)


@dataclass
class FakeAdapter:
    identity: AdapterIdentity
    available: bool = True
    diagnostics: tuple[str, ...] = ()

    def probe(self) -> ProbeResult:
        if self.available:
            return ProbeResult.ready(self.identity, *self.diagnostics)
        return ProbeResult.unavailable(*self.diagnostics)


@dataclass
class FakeHarnessAdapter(FakeAdapter):
    scripted_results: list[SeatResult] = field(default_factory=list)
    requests: list[SeatRequest] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)

    def invoke(self, request: SeatRequest) -> SeatResult:
        self.requests.append(request)
        if not self.scripted_results:
            raise AssertionError("fake harness has no scripted result")
        return self.scripted_results.pop(0)

    def cancel(self, run_ref: str) -> None:
        self.cancelled.append(run_ref)

    def collect_usage(self, run_ref: str) -> SeatUsage:
        return SeatUsage(source=f"fake:{run_ref}")


@dataclass
class FakeTrackerAdapter(FakeAdapter):
    queue: list[WorkItem] = field(default_factory=list)
    claims: list[tuple[str, str]] = field(default_factory=list)
    closed: list[CloseEvidence] = field(default_factory=list)
    parked: list[tuple[str, str]] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)

    def next_item(self, campaign: CampaignRef) -> WorkItem | None:
        return self.queue.pop(0) if self.queue else None

    def claim(self, item: WorkItem, actor: str) -> ClaimReceipt:
        self.claims.append((item.item_id, actor))
        return ClaimReceipt(item.item_id, actor, "fake-time")

    def close(self, evidence: CloseEvidence, item_commit: str, workspace: WorkspaceRef, actor: str) -> None:
        self.closed.append(evidence)

    def park(
        self, item_id: str, reason: str, actor: str, workspace: WorkspaceRef | None = None
    ) -> None:
        self.parked.append((item_id, reason))

    def propose(self, proposal: Proposal, actor: str) -> ProposalRef:
        self.proposals.append(proposal)
        return ProposalRef(f"proposal-{len(self.proposals)}")


@dataclass
class FakeWorkspaceAdapter(FakeAdapter):
    workspace: WorkspaceRef = field(
        default_factory=lambda: WorkspaceRef(Path("/fake/worktree"), "run", "base", "lease")
    )
    diffs: list[DiffEvidence] = field(default_factory=list)
    finalise_results: list[FinaliseResult] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)

    def identify(self, root: Path) -> RepositoryIdentity:
        return RepositoryIdentity(root, "main", "origin", "base")

    def create_isolated(self, campaign: CampaignRef, item: WorkItem) -> WorkspaceRef:
        self.created.append(item.item_id)
        return self.workspace

    def diff(self, workspace: WorkspaceRef) -> DiffEvidence:
        if not self.diffs:
            raise AssertionError("fake workspace has no scripted diff")
        return self.diffs.pop(0)

    def commit_item(self, workspace: WorkspaceRef, request: FinaliseRequest) -> str:
        return "item-commit"

    def commit_tracker(
        self, workspace: WorkspaceRef, item_id: str, item_commit: str | None
    ) -> str:
        return "tracker-commit"

    def deliver(self, workspace: WorkspaceRef, request: DeliveryRequest) -> FinaliseResult:
        if self.finalise_results:
            return self.finalise_results.pop(0)
        return FinaliseResult(request.item_commit, request.tracker_commit, "merge-commit", True)

    def release(self, workspace: WorkspaceRef) -> None:
        if workspace.lease_id not in self.released:
            self.released.append(workspace.lease_id)


@dataclass
class FakeCommandAdapter(FakeAdapter):
    scripted_results: list[CommandResult] = field(default_factory=list)
    requests: list[CommandRequest] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)

    def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        if not self.scripted_results:
            raise AssertionError("fake command adapter has no scripted result")
        return self.scripted_results.pop(0)

    def cancel(self, command_id: str) -> None:
        self.cancelled.append(command_id)


@dataclass
class FakeRunRecordAdapter(FakeAdapter):
    events: list[RunEvent] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)
    summaries: list[str] = field(default_factory=list)

    def create(self, campaign: CampaignRef) -> RunRecordRef:
        return RunRecordRef(f"run-{campaign.campaign_id}", Path("/fake/record"))

    def append(self, record: RunRecordRef, event: RunEvent) -> str:
        self.events.append(event)
        return f"event-{len(self.events)}"

    def write_evidence(self, record: RunRecordRef, name: str, content: str) -> str:
        self.evidence[name] = content
        return f"evidence:{name}"

    def complete(self, record: RunRecordRef, summary: str) -> str:
        self.summaries.append(summary)
        return "report:final"


@dataclass
class FakeKnowledgeAdapter(FakeAdapter):
    contexts: list[DurableContext] = field(default_factory=list)
    fog: list[FogRecord] = field(default_factory=list)

    def retrieve(self, query: str) -> DurableContext:
        if self.contexts:
            return self.contexts.pop(0)
        return DurableContext(query, ())

    def record_fog(self, fog: FogRecord) -> str:
        self.fog.append(fog)
        return f"fog-{len(self.fog)}"
