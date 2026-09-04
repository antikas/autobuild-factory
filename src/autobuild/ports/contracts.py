"""Protocols express intent; adapters own every execution mechanism."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from autobuild.domain import (
    CampaignRef,
    CampaignReport,
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
    LaneCooling,
    LaneSignal,
    LeaseGrant,
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
    SeatRequest,
    SeatResult,
    SeatUsage,
    WorkItem,
    WorkspaceRef,
    WorktreeSnapshot,
    WorktreeStatus,
)


class Probeable(Protocol):
    def probe(self) -> ProbeResult: ...


@runtime_checkable
class TrackerPort(Probeable, Protocol):
    def next_item(self, campaign: CampaignRef) -> WorkItem | None: ...
    def ready_items(self, campaign: CampaignRef) -> tuple[WorkItem, ...]: ...
    def resumable_claims(self, campaign: CampaignRef) -> tuple[WorkItem, ...]:
        """Items claimed by the AutoBuild builder actor that are neither done nor
        parked, so a relaunch can resume an interrupted item without a person
        reconciling first. The adapter reads committed tracker state and returns
        the same ``WorkItem`` shape ``next_item`` returns."""
        ...

    def claim(self, item: WorkItem, actor: str) -> ClaimReceipt: ...
    def close(
        self, evidence: CloseEvidence, item_commit: str, workspace: WorkspaceRef, actor: str
    ) -> None: ...
    def park(
        self, item_id: str, reason: str, actor: str, workspace: WorkspaceRef | None = None
    ) -> None: ...
    def propose(self, proposal: Proposal, actor: str) -> ProposalRef: ...


@runtime_checkable
class WorkspacePort(Probeable, Protocol):
    def identify(self, root: Path) -> RepositoryIdentity: ...
    def create_isolated(self, campaign: CampaignRef, item: WorkItem) -> WorkspaceRef: ...
    def list_worktrees(self, campaign: CampaignRef) -> tuple[WorktreeStatus, ...]:
        """Every registered worktree of the repository under the scratch root,
        with its branch, head commit and current product status digest, so a
        relaunch can match a phase marker to a live worktree and spot an orphan
        worktree that no claimed item owns."""
        ...

    def adopt_worktree(
        self, campaign: CampaignRef, item: WorkItem, root: Path
    ) -> WorkspaceRef:
        """Take a single-writer handle on an existing worktree so an interrupted
        item can be resumed or snapshotted. The returned reference pins the
        worktree's current head as its start commit."""
        ...

    def resume_delivery_commits(self, workspace: WorkspaceRef) -> tuple[str | None, str]:
        """The product and tracker commits already present in a finalised
        worktree, so a resume from the ``finalised`` marker can re-run delivery
        against them. Returns ``(item_commit, tracker_commit)``."""
        ...

    def diff(self, workspace: WorkspaceRef) -> DiffEvidence: ...
    def progress_digest(self, workspace: WorkspaceRef) -> str: ...
    def commit_item(self, workspace: WorkspaceRef, request: FinaliseRequest) -> str: ...
    def commit_tracker(
        self, workspace: WorkspaceRef, item_id: str, item_commit: str | None
    ) -> str: ...
    def snapshot(self, workspace: WorkspaceRef) -> WorktreeSnapshot: ...
    def deliver(self, workspace: WorkspaceRef, request: DeliveryRequest) -> FinaliseResult: ...
    def confirm_delivery(
        self, workspace: WorkspaceRef, result: FinaliseResult, target_branch: str
    ) -> None: ...
    def deliver_report(self, request: CampaignReport) -> FinaliseResult: ...
    def release(self, workspace: WorkspaceRef) -> None: ...


@runtime_checkable
class HarnessPort(Probeable, Protocol):
    def invoke(self, request: SeatRequest) -> SeatResult: ...
    def cancel(self, run_ref: str) -> None: ...
    def collect_usage(self, run_ref: str) -> SeatUsage: ...

    def classify_failure(self, result: SeatResult) -> LaneSignal | None:
        """Read a lane signal from a failed seat's structural evidence.

        The adapter inspects the exit code and the CLI's structured error fields
        only, never free text in the event stream. A successful seat or a failure
        with no structured limit evidence returns ``None`` so the lane is never
        cooled by a false kill."""
        ...


@runtime_checkable
class CommandPort(Probeable, Protocol):
    def run(self, request: CommandRequest) -> CommandResult: ...
    def cancel(self, command_id: str) -> None: ...


@runtime_checkable
class RunRecordPort(Probeable, Protocol):
    def create(self, campaign: CampaignRef) -> RunRecordRef: ...
    def append(self, record: RunRecordRef, event: RunEvent) -> str: ...
    def write_evidence(self, record: RunRecordRef, name: str, content: str) -> str: ...
    def write_evidence_file(
        self, record: RunRecordRef, relative_path: str, content: bytes
    ) -> str: ...
    def latest_phase_marker(self, item_id: str) -> PhaseMarker | None:
        """The item's phase marker from the most recent run under the scratch
        root, or ``None`` when no run recorded one, so a relaunch can resume from
        the last recorded phase."""
        ...

    def complete(self, record: RunRecordRef, summary: str) -> str: ...


@runtime_checkable
class KnowledgePort(Probeable, Protocol):
    def retrieve(self, query: str) -> DurableContext: ...
    def record_fog(self, fog: FogRecord) -> str: ...


@runtime_checkable
class ProgressPort(Protocol):
    """The owner-facing progress stream for one campaign.

    ``begin`` is called once, immediately after the run record is created, so a
    file sink can open its log under the run record root. ``emit`` is called with
    one already-rendered, timestamped progress line after every run event is
    appended. No adapter behind this port may raise into the application: a sink
    failure is swallowed by the adapter, never propagated."""

    def begin(self, record: RunRecordRef) -> None: ...
    def emit(self, line: str) -> None: ...


@runtime_checkable
class LeasePort(Probeable, Protocol):
    """The single-writer lease mechanism for one surface (tracker root or worktree).

    ``acquire`` raises ``LeaseHeld`` when a live lease names another holder and
    reclaims a stale one, reporting the previous holder on the grant. ``renew``
    refreshes the heartbeat at an item boundary. ``release`` is idempotent and
    reports a diagnostic when this process did not hold the surface.
    ``live_holder`` returns the current live holder, or ``None`` when the surface
    is free or its lease is stale, so the preflight doctor can name a holder."""

    def acquire(self, request: LeaseRequest) -> LeaseGrant: ...
    def renew(self, grant: LeaseGrant) -> LeaseGrant: ...
    def release(self, grant: LeaseGrant) -> LeaseRelease: ...
    def live_holder(self, surface: LeaseSurface) -> LeaseRecord | None: ...


@runtime_checkable
class LaneStatePort(Probeable, Protocol):
    """Machine-local, cross-campaign lane cooling shared through one file.

    ``active`` returns a lane's cooling only while it is still in force (the
    current time is before ``cooled_until``); ``cool`` records a signal against a
    lane under a file lock so concurrent campaigns see each other's cooling;
    ``snapshot`` returns every current record for the run manifest."""

    def active(self, lane: str) -> LaneCooling | None: ...
    def cool(self, lane: str, signal: LaneSignal, campaign_id: str) -> LaneCooling: ...
    def snapshot(self) -> tuple[LaneCooling, ...]: ...


@runtime_checkable
class NetworkProbePort(Protocol):
    """The reachability mechanism the preflight doctor uses; raises on failure."""

    def resolve(self, host: str) -> None: ...
    def tls_handshake(self, host: str, port: int) -> None: ...
    def closed_local_port(self) -> int: ...


@runtime_checkable
class EnvironmentProbePort(Protocol):
    """Read-only view of the child process environment and executable search."""

    def value(self, name: str) -> str | None: ...
    def resolve_executable(self, name: str) -> str | None: ...


@runtime_checkable
class FilesystemProbePort(Protocol):
    """The filesystem mechanism the scratch and validator probes rely on."""

    def ensure_directory(self, path: Path) -> None: ...
    def probe_write(self, path: Path) -> None: ...
    def foreign_locks(self, path: Path) -> tuple[str, ...]: ...
    def is_file(self, path: Path) -> bool: ...
    def size(self, path: Path) -> int: ...
