"""Small scripted adapters; truth in tests comes from their recorded calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from autobuild.domain import (
    AdapterIdentity,
    CampaignRef,
    CampaignReport,
    ClaimReceipt,
    CloseEvidence,
    CommandRequest,
    CommandResult,
    DeliveryRequest,
    DiffEvidence,
    DurableContext,
    EvidenceError,
    FinaliseRequest,
    FinaliseResult,
    FogRecord,
    LaneCooling,
    LaneSignal,
    LeaseGrant,
    LeaseHeld,
    LeaseRecord,
    LeaseRelease,
    LeaseRequest,
    LeaseSurface,
    PhaseMarker,
    ProbeResult,
    Proposal,
    ProposalRef,
    RepositoryIdentity,
    RunEvent,
    RunRecordRef,
    SeatOutcome,
    SeatRequest,
    SeatResult,
    SeatUsage,
    WorkItem,
    WorkspaceRef,
    WorktreeSnapshot,
    WorktreeStatus,
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
    # The lane signal this lane's harness reports for any non-succeeded seat; the
    # default of None means a failure never cools the lane.
    signal: LaneSignal | None = None
    classified: list[SeatResult] = field(default_factory=list)

    def invoke(self, request: SeatRequest) -> SeatResult:
        self.requests.append(request)
        if not self.scripted_results:
            raise AssertionError("fake harness has no scripted result")
        return self.scripted_results.pop(0)

    def cancel(self, run_ref: str) -> None:
        self.cancelled.append(run_ref)

    def collect_usage(self, run_ref: str) -> SeatUsage:
        return SeatUsage(source=f"fake:{run_ref}")

    def classify_failure(self, result: SeatResult) -> LaneSignal | None:
        self.classified.append(result)
        if result.outcome is not SeatOutcome.FAILED:
            return None
        return self.signal


@dataclass
class FakeTrackerAdapter(FakeAdapter):
    queue: list[WorkItem] = field(default_factory=list)
    ready: list[WorkItem] = field(default_factory=list)
    resumable: list[WorkItem] = field(default_factory=list)
    claims: list[tuple[str, str]] = field(default_factory=list)
    closed: list[CloseEvidence] = field(default_factory=list)
    parked: list[tuple[str, str]] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)

    def next_item(self, campaign: CampaignRef) -> WorkItem | None:
        return self.queue.pop(0) if self.queue else None

    def ready_items(self, campaign: CampaignRef) -> tuple[WorkItem, ...]:
        claimed = {item_id for item_id, _ in self.claims}
        return tuple(item for item in self.ready if item.item_id not in claimed)

    def resumable_claims(self, campaign: CampaignRef) -> tuple[WorkItem, ...]:
        return tuple(self.resumable)

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
    snapshots: list[WorktreeSnapshot] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    snapshotted: list[str] = field(default_factory=list)
    reports: list[CampaignReport] = field(default_factory=list)
    confirmed: list[tuple[str, str]] = field(default_factory=list)
    release_error: Exception | None = None
    unreachable_merge: bool = False
    progress_digests: list[str] = field(default_factory=list)
    digest_calls: int = 0
    worktrees: list[WorktreeStatus] = field(default_factory=list)
    adopted: list[Path] = field(default_factory=list)
    resume_commits: tuple[str | None, str] = ("item-commit", "tracker-commit")

    def identify(self, root: Path) -> RepositoryIdentity:
        return RepositoryIdentity(root, "main", "feature", "origin", "base")

    def list_worktrees(self, campaign: CampaignRef) -> tuple[WorktreeStatus, ...]:
        return tuple(self.worktrees)

    def adopt_worktree(
        self, campaign: CampaignRef, item: WorkItem, root: Path
    ) -> WorkspaceRef:
        self.adopted.append(root)
        return self.workspace

    def resume_delivery_commits(self, workspace: WorkspaceRef) -> tuple[str | None, str]:
        return self.resume_commits

    def progress_digest(self, workspace: WorkspaceRef) -> str:
        self.digest_calls += 1
        if self.progress_digests:
            return self.progress_digests.pop(0)
        return "digest:flat"

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

    def snapshot(self, workspace: WorkspaceRef) -> WorktreeSnapshot:
        self.snapshotted.append(workspace.lease_id)
        if self.snapshots:
            return self.snapshots.pop(0)
        return WorktreeSnapshot(workspace.start_commit, b"", (), ())

    def deliver(self, workspace: WorkspaceRef, request: DeliveryRequest) -> FinaliseResult:
        if self.finalise_results:
            return self.finalise_results.pop(0)
        return FinaliseResult(request.item_commit, request.tracker_commit, "merge-commit", True)

    def confirm_delivery(
        self, workspace: WorkspaceRef, result: FinaliseResult, target_branch: str
    ) -> None:
        self.confirmed.append((workspace.lease_id, target_branch))
        if self.unreachable_merge:
            raise EvidenceError(
                "merged commit is not reachable from the delivery target branch"
            )

    def deliver_report(self, request: CampaignReport) -> FinaliseResult:
        self.reports.append(request)
        return FinaliseResult(None, "report-commit", "report-commit", True, (request.relative_path,))

    def release(self, workspace: WorkspaceRef) -> None:
        if self.release_error is not None:
            raise self.release_error
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
    evidence_files: dict[str, bytes] = field(default_factory=dict)
    evidence_file_writes: list[tuple[str, bytes]] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    markers: dict[str, PhaseMarker] = field(default_factory=dict)

    def create(self, campaign: CampaignRef) -> RunRecordRef:
        return RunRecordRef(f"run-{campaign.campaign_id}", Path("/fake/record"))

    def append(self, record: RunRecordRef, event: RunEvent) -> str:
        self.events.append(event)
        return f"event-{len(self.events)}"

    def write_evidence(self, record: RunRecordRef, name: str, content: str) -> str:
        self.evidence[name] = content
        return f"evidence:{name}"

    def write_evidence_file(
        self, record: RunRecordRef, relative_path: str, content: bytes
    ) -> str:
        self.evidence_files[relative_path] = content
        self.evidence_file_writes.append((relative_path, content))
        return f"evidence:{relative_path}"

    def latest_phase_marker(self, item_id: str) -> PhaseMarker | None:
        return self.markers.get(item_id)

    def complete(self, record: RunRecordRef, summary: str) -> str:
        self.summaries.append(summary)
        return "report:final"


@dataclass
class FakeNetworkProbe:
    """Scripted reachability: named hosts or targets fail; the rest succeed."""

    unresolvable: frozenset[str] = field(default_factory=frozenset)
    unreachable: frozenset[tuple[str, int]] = field(default_factory=frozenset)
    port: int = 59_999
    resolved: list[str] = field(default_factory=list)
    handshakes: list[tuple[str, int]] = field(default_factory=list)

    def resolve(self, host: str) -> None:
        self.resolved.append(host)
        if host in self.unresolvable:
            raise OSError(f"name resolution failed for {host}")

    def tls_handshake(self, host: str, port: int) -> None:
        self.handshakes.append((host, port))
        if (host, port) in self.unreachable:
            raise OSError(f"tls handshake failed for {host}:{port}")

    def closed_local_port(self) -> int:
        return self.port


@dataclass
class FakeEnvironmentProbe:
    values: dict[str, str] = field(default_factory=dict)
    executables: dict[str, str] = field(default_factory=dict)

    def value(self, name: str) -> str | None:
        return self.values.get(name)

    def resolve_executable(self, name: str) -> str | None:
        return self.executables.get(name)


@dataclass
class FakeFilesystemProbe:
    files: dict[str, int] = field(default_factory=dict)
    directories: set[str] = field(default_factory=set)
    locks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    writable: bool = True
    creatable: bool = True
    created: list[str] = field(default_factory=list)

    def ensure_directory(self, path: Path) -> None:
        if not self.creatable:
            raise OSError(f"cannot create {path}")
        self.directories.add(str(path))
        self.created.append(str(path))

    def probe_write(self, path: Path) -> None:
        if not self.writable:
            raise OSError(f"{path} is read-only")

    def foreign_locks(self, path: Path) -> tuple[str, ...]:
        return self.locks.get(str(path), ())

    def is_file(self, path: Path) -> bool:
        return str(path) in self.files

    def size(self, path: Path) -> int:
        return self.files.get(str(path), 0)


@dataclass
class FakeLeaseAdapter(FakeAdapter):
    """A scripted single-writer lease. ``live_holders`` forces a refusal on the
    matching surface path; ``stale_holders`` forces a reclaim of that holder; and
    ``release_diagnostics`` forces a no-op release with the given diagnostics."""

    process_id: int = 4321
    host: str = "fake-host"
    live_holders: dict[str, LeaseRecord] = field(default_factory=dict)
    stale_holders: dict[str, LeaseRecord] = field(default_factory=dict)
    release_diagnostics: dict[str, tuple[str, ...]] = field(default_factory=dict)
    acquired: list[LeaseSurface] = field(default_factory=list)
    renewed: list[LeaseSurface] = field(default_factory=list)
    released: list[LeaseSurface] = field(default_factory=list)
    grants: list[LeaseGrant] = field(default_factory=list)

    def _record(self, campaign_id: str, surface: LeaseSurface) -> LeaseRecord:
        return LeaseRecord(
            campaign_id, self.process_id, self.host, "start", "beat", surface.path, surface.kind
        )

    def acquire(self, request: LeaseRequest) -> LeaseGrant:
        key = str(request.surface.path)
        if key in self.live_holders:
            raise LeaseHeld(self.live_holders[key])
        reclaimed = self.stale_holders.pop(key, None)
        grant = LeaseGrant(
            request.surface, self._record(request.campaign_id, request.surface), reclaimed
        )
        self.acquired.append(request.surface)
        self.grants.append(grant)
        return grant

    def renew(self, grant: LeaseGrant) -> LeaseGrant:
        self.renewed.append(grant.surface)
        return LeaseGrant(
            grant.surface, self._record(grant.record.campaign_id, grant.surface), None
        )

    def release(self, grant: LeaseGrant) -> LeaseRelease:
        self.released.append(grant.surface)
        diagnostics = self.release_diagnostics.get(str(grant.surface.path), ())
        return LeaseRelease(grant.surface, not diagnostics, diagnostics)

    def live_holder(self, surface: LeaseSurface) -> LeaseRecord | None:
        return self.live_holders.get(str(surface.path))


@dataclass
class FakeLaneStateAdapter(FakeAdapter):
    """Scripted, in-memory lane cooling shared through one instance.

    ``cooled`` holds the lanes currently in force; ``cool`` records a signal and
    keeps the lane cooled for the rest of the test, so the router treats it as
    unavailable. ``cools`` records every call for assertions."""

    cooled: dict[str, LaneCooling] = field(default_factory=dict)
    cools: list[tuple[str, LaneSignal, str]] = field(default_factory=list)

    def active(self, lane: str) -> LaneCooling | None:
        return self.cooled.get(lane)

    def cool(self, lane: str, signal: LaneSignal, campaign_id: str) -> LaneCooling:
        record = LaneCooling(
            lane=lane,
            cooled_until="2999-01-01T00:00:00+00:00",
            signature=signal.signature,
            last_failure_at="2026-09-03T00:00:00+00:00",
            campaign_id=campaign_id,
        )
        self.cooled[lane] = record
        self.cools.append((lane, signal, campaign_id))
        return record

    def snapshot(self) -> tuple[LaneCooling, ...]:
        return tuple(self.cooled.values())


@dataclass
class FakeProgressPort:
    """Records ``begin`` and every ``emit`` in order, so a test can assert the
    exact progress sequence and that ``begin`` preceded the first line."""

    began: list[RunRecordRef] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    def begin(self, record: RunRecordRef) -> None:
        self.began.append(record)

    def emit(self, line: str) -> None:
        self.lines.append(line)


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
