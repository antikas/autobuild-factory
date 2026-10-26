"""Small scripted adapters; truth in tests comes from their recorded calls."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from autobuild.domain import (
    AdapterIdentity,
    CampaignRef,
    CampaignReport,
    CapabilityError,
    ClaimReceipt,
    CloseEvidence,
    CommandRequest,
    CommandResult,
    DeliveryRequest,
    DiffEvidence,
    DurableContext,
    EffortLevel,
    EvidenceError,
    FinaliseRequest,
    FinaliseResult,
    FogRecord,
    HostReading,
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
    TrackerPublication,
    UsageReading,
    WorkItem,
    WorkspaceRef,
    WorktreeSnapshot,
    WorktreeStatus,
)
from autobuild.domain.models import (
    DeliveryParked,
    DeliveryParkReason,
    RevalidationDecision,
    SeatTransport,
    SkippedItem,
    TrackerRecovery,
    ValidationCheckout,
)


def _lock() -> threading.Lock:
    return threading.Lock()


def _item_script(scripts: dict[str, list], key: str | None):
    """The per-item script for ``key``, or None when the item is not keyed."""

    if key is not None and key in scripts:
        return scripts[key]
    return None


@dataclass
class FakeClock:
    """A thread-safe fake monotonic clock. Each call returns the current time
    and then moves it on by ``step``; ``advance`` moves it explicitly.

    ``wait`` stands in for a timed wait on an event: it returns True as soon as
    the event is set and False once the fake time has moved ``seconds`` past
    the call, and gives up with False after ``real_limit`` real seconds so a
    test never hangs on it. ``waiters`` counts the calls inside ``wait``, so a
    test can move time on only once a waiter has taken its deadline."""

    current: float = 0.0
    step: float = 0.0
    real_limit: float = 5.0
    waiters: int = field(default=0, init=False, compare=False)
    _guard: threading.Condition = field(
        default_factory=threading.Condition, init=False, repr=False, compare=False
    )

    def __call__(self) -> float:
        with self._guard:
            value = self.current
            self.current += self.step
            return value

    def advance(self, seconds: float) -> None:
        with self._guard:
            self.current += seconds
            self._guard.notify_all()

    def set(self, value: float) -> None:
        with self._guard:
            self.current = value
            self._guard.notify_all()

    def wait(self, event: threading.Event, seconds: float) -> bool:
        with self._guard:
            deadline = self.current + seconds
            self.waiters += 1
        try:
            real_deadline = _real_now() + self.real_limit
            while not event.is_set():
                with self._guard:
                    if self.current >= deadline:
                        return False
                    self._guard.wait(0.02)
                if _real_now() >= real_deadline:
                    return False
            return True
        finally:
            with self._guard:
                self.waiters -= 1


def _real_now() -> float:
    return time.monotonic()


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
    # The effort levels this lane's harness can pass. A seat asking for any other
    # level is refused before it is recorded, as a harness adapter refuses it.
    effort_levels: frozenset[EffortLevel] = frozenset(EffortLevel)
    # The opaque usage meter id this lane declares, and the model classes each
    # meter scope label covers, standing in for the adapter's own decision.
    meter_id: str | None = None
    meter_scopes: dict[str, frozenset[str]] = field(default_factory=dict)
    # Results scripted per item id. A seat for a keyed item takes the next
    # result of its own script; any other seat takes the shared script above.
    results_by_item: dict[str, list[SeatResult]] = field(default_factory=dict)
    # Called with each request before its result is returned, so a test can
    # hold one item's seat while another item runs.
    on_invoke: Callable[[SeatRequest], None] | None = None
    _guard: threading.Lock = field(default_factory=_lock, init=False, repr=False, compare=False)

    def meter_scope_covers(self, scope: str, model_class: str) -> bool:
        return model_class in self.meter_scopes.get(scope, frozenset())

    def invoke(self, request: SeatRequest) -> SeatResult:
        if request.effort is not None and request.effort not in self.effort_levels:
            raise CapabilityError(f"fake harness cannot pass effort {request.effort.value}")
        with self._guard:
            self.requests.append(request)
            script = _item_script(self.results_by_item, request.item_id)
            if script is None:
                script = self.scripted_results
            result = script.pop(0) if script else None
        if self.on_invoke is not None:
            self.on_invoke(request)
        if result is None:
            raise AssertionError("fake harness has no scripted result")
        return result

    def cancel(self, run_ref: str) -> None:
        with self._guard:
            self.cancelled.append(run_ref)

    def collect_usage(self, run_ref: str) -> SeatUsage:
        return SeatUsage(source=f"fake:{run_ref}")

    def classify_failure(self, result: SeatResult) -> LaneSignal | None:
        with self._guard:
            self.classified.append(result)
        if result.outcome is not SeatOutcome.FAILED:
            return None
        return self.signal

    def seat_transport(self, request: SeatRequest) -> SeatTransport:
        return SeatTransport(0, True, (("DO_NOT_TRACK", "1"),), in_process=True)


@dataclass
class FakeAgentSdkClient:
    """A scripted Agent SDK client standing in for the SDK package.

    Each ``run`` replays the next script: a list of message records, where an
    exception instance in the list is raised at that point. With
    ``block_until_cancelled`` a run then waits for its cancellation event, as a
    long seat would."""

    sdk_version: str = "0.0-fake"
    installed: bool = True
    scripts: list[list[object]] = field(default_factory=list)
    block_until_cancelled: bool = False
    specs: list[object] = field(default_factory=list)
    version_calls: int = 0
    # Runs whose cancellation event was set while they waited.
    cancelled_runs: int = 0

    def version(self) -> str:
        self.version_calls += 1
        if not self.installed:
            raise ImportError("No module named 'claude_agent_sdk'")
        return self.sdk_version

    def run(self, spec, on_message, on_stderr, cancelled) -> None:
        self.specs.append(spec)
        script = self.scripts.pop(0) if self.scripts else []
        for item in script:
            if isinstance(item, BaseException):
                raise item
            on_message(dict(item))
        if self.block_until_cancelled and cancelled.wait(10):
            self.cancelled_runs += 1


@dataclass
class FakeTrackerAdapter(FakeAdapter):
    queue: list[WorkItem] = field(default_factory=list)
    ready: list[WorkItem] = field(default_factory=list)
    resumable: list[WorkItem] = field(default_factory=list)
    claims: list[tuple[str, str]] = field(default_factory=list)
    closed: list[CloseEvidence] = field(default_factory=list)
    parked: list[tuple[str, str]] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    # A scripted failure raised by ``claim`` for the named item, standing in for
    # a tracker that reports the claim taken or stops the campaign.
    claim_errors: dict[str, Exception] = field(default_factory=dict)
    # The publication every claim, primary park and proposal reports.
    publication: TrackerPublication = field(default_factory=TrackerPublication)
    # A publication scripted per item id, overriding ``publication`` for the
    # claims and primary parks of that item.
    publications_by_item: dict[str, TrackerPublication] = field(default_factory=dict)
    # Dependencies: an item in ``ready`` is listed only once every item it is
    # blocked by is in ``done``. ``mark_done`` records a delivered item.
    blocked_by: dict[str, frozenset[str]] = field(default_factory=dict)
    done: set[str] = field(default_factory=set)
    ready_reads: int = 0
    # The results ``recover`` returns in order, then a clear result once the
    # script runs out; ``recoveries`` counts the calls.
    recovery_results: list[TrackerRecovery] = field(default_factory=list)
    recoveries: int = 0
    # The ready items this tracker reports it passed over.
    skipped: list[SkippedItem] = field(default_factory=list)
    _guard: threading.Lock = field(default_factory=_lock, init=False, repr=False, compare=False)

    def _publication(self, item_id: str) -> TrackerPublication:
        return self.publications_by_item.get(item_id, self.publication)

    def mark_done(self, item_id: str) -> None:
        with self._guard:
            self.done.add(item_id)

    def add_ready(self, *items: WorkItem) -> None:
        with self._guard:
            self.ready.extend(items)

    def next_item(self, campaign: CampaignRef) -> WorkItem | None:
        with self._guard:
            return self.queue.pop(0) if self.queue else None

    def ready_items(self, campaign: CampaignRef) -> tuple[WorkItem, ...]:
        with self._guard:
            self.ready_reads += 1
            claimed = {item_id for item_id, _ in self.claims}
            return tuple(
                item
                for item in self.ready
                if item.item_id not in claimed
                and self.blocked_by.get(item.item_id, frozenset()) <= self.done
            )

    def resumable_claims(self, campaign: CampaignRef) -> tuple[WorkItem, ...]:
        return tuple(self.resumable)

    def claim(self, item: WorkItem, actor: str) -> ClaimReceipt:
        if item.item_id in self.claim_errors:
            raise self.claim_errors[item.item_id]
        with self._guard:
            self.claims.append((item.item_id, actor))
        publication = self._publication(item.item_id)
        return ClaimReceipt(
            item.item_id,
            actor,
            "fake-time",
            published=publication.published,
            publication_note=publication.note,
        )

    def close(self, evidence: CloseEvidence, item_commit: str, workspace: WorkspaceRef, actor: str) -> None:
        with self._guard:
            self.closed.append(evidence)

    def park(
        self, item_id: str, reason: str, actor: str, workspace: WorkspaceRef | None = None
    ) -> TrackerPublication | None:
        with self._guard:
            self.parked.append((item_id, reason))
        return self._publication(item_id) if workspace is None else None

    def propose(self, proposal: Proposal, actor: str) -> ProposalRef:
        with self._guard:
            self.proposals.append(proposal)
            number = len(self.proposals)
        return ProposalRef(
            f"proposal-{number}",
            published=self.publication.published,
            publication_note=self.publication.note,
        )

    def skipped_items(self) -> tuple[SkippedItem, ...]:
        return tuple(self.skipped)

    def recover(self) -> TrackerRecovery:
        with self._guard:
            self.recoveries += 1
            if self.recovery_results:
                return self.recovery_results.pop(0)
        return TrackerRecovery()


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
    # The worktree and the diffs scripted per item id. A keyed item gets its own
    # worktree and takes its own diffs; any other item uses the shared ones.
    workspaces_by_item: dict[str, WorkspaceRef] = field(default_factory=dict)
    diffs_by_item: dict[str, list[DiffEvidence]] = field(default_factory=dict)
    # Raised by ``deliver`` in order, standing in for a delivery that parks.
    delivery_parks: list[DeliveryParked] = field(default_factory=list)
    # When true, ``deliver`` reports that integration moved the base outside the
    # tracker paths and runs the request's revalidation once; a failed one parks.
    base_moved: bool = False
    revalidations: list[bool] = field(default_factory=list)
    # Called with the item id (None for an unkeyed worktree) after a delivery
    # succeeds, so a test can mark the item done in its tracker.
    on_deliver: Callable[[str | None], None] | None = None
    delivered: list[str | None] = field(default_factory=list)
    # The target heads successive validation checkouts see; the last repeats.
    validation_revisions: list[str] = field(default_factory=lambda: ["head"])
    validation_checkouts: list[ValidationCheckout] = field(default_factory=list)
    closed_checkouts: list[Path] = field(default_factory=list)
    # The remote target head a refreshing checkout fast-forwards to, when set,
    # and the refresh flag of every checkout opened.
    remote_head: str | None = None
    refreshes: list[bool] = field(default_factory=list)
    _guard: threading.Lock = field(default_factory=_lock, init=False, repr=False, compare=False)

    def open_validation_checkout(
        self, repository: Path, branch: str | None, refresh: bool = False
    ) -> ValidationCheckout:
        with self._guard:
            self.refreshes.append(refresh)
            revisions = self.validation_revisions
            if refresh and self.remote_head is not None:
                revisions[:] = [self.remote_head]
            revision = revisions.pop(0) if len(revisions) > 1 else revisions[0]
            checkout = ValidationCheckout(
                Path(f"/fake/validation/{len(self.validation_checkouts) + 1}"),
                revision,
                repository,
            )
            self.validation_checkouts.append(checkout)
        return checkout

    def close_validation_checkout(self, checkout: ValidationCheckout) -> None:
        with self._guard:
            self.closed_checkouts.append(checkout.root)

    def _workspace_for(self, item_id: str) -> WorkspaceRef:
        return self.workspaces_by_item.get(item_id, self.workspace)

    def _item_of(self, workspace: WorkspaceRef) -> str | None:
        for item_id, keyed in self.workspaces_by_item.items():
            if keyed == workspace:
                return item_id
        return None

    def identify(self, root: Path) -> RepositoryIdentity:
        return RepositoryIdentity(root, "main", "feature", "origin", "base")

    def list_worktrees(self, campaign: CampaignRef) -> tuple[WorktreeStatus, ...]:
        return tuple(self.worktrees)

    def adopt_worktree(
        self, campaign: CampaignRef, item: WorkItem, root: Path
    ) -> WorkspaceRef:
        with self._guard:
            self.adopted.append(root)
        return self._workspace_for(item.item_id)

    def resume_delivery_commits(self, workspace: WorkspaceRef) -> tuple[str | None, str]:
        return self.resume_commits

    def progress_digest(self, workspace: WorkspaceRef) -> str:
        with self._guard:
            self.digest_calls += 1
            if self.progress_digests:
                return self.progress_digests.pop(0)
        return "digest:flat"

    def create_isolated(self, campaign: CampaignRef, item: WorkItem) -> WorkspaceRef:
        with self._guard:
            self.created.append(item.item_id)
        return self._workspace_for(item.item_id)

    def diff(self, workspace: WorkspaceRef) -> DiffEvidence:
        with self._guard:
            script = _item_script(self.diffs_by_item, self._item_of(workspace))
            if script is None:
                script = self.diffs
            if not script:
                raise AssertionError("fake workspace has no scripted diff")
            return script.pop(0)

    def commit_item(self, workspace: WorkspaceRef, request: FinaliseRequest) -> str:
        return "item-commit"

    def commit_tracker(
        self, workspace: WorkspaceRef, item_id: str, item_commit: str | None
    ) -> str:
        return "tracker-commit"

    def snapshot(self, workspace: WorkspaceRef) -> WorktreeSnapshot:
        with self._guard:
            self.snapshotted.append(workspace.lease_id)
            if self.snapshots:
                return self.snapshots.pop(0)
        return WorktreeSnapshot(workspace.start_commit, b"", (), ())

    def deliver(self, workspace: WorkspaceRef, request: DeliveryRequest) -> FinaliseResult:
        with self._guard:
            parked = self.delivery_parks.pop(0) if self.delivery_parks else None
        if parked is not None:
            raise parked
        if self.base_moved and request.revalidate is not None:
            if request.note_revalidation is not None:
                request.note_revalidation(
                    RevalidationDecision(True, "overlap", ("moved",), ("moved",))
                )
            passed = request.revalidate()
            with self._guard:
                self.revalidations.append(passed)
            if not passed:
                raise DeliveryParked(
                    DeliveryParkReason.REVALIDATION_FAILED, "fake integrated candidate failed"
                )
        with self._guard:
            result = (
                self.finalise_results.pop(0)
                if self.finalise_results
                else FinaliseResult(
                    request.item_commit, request.tracker_commit, "merge-commit", True
                )
            )
            item_id = self._item_of(workspace)
            self.delivered.append(item_id)
        if self.on_deliver is not None:
            self.on_deliver(item_id)
        return result

    def confirm_delivery(
        self, workspace: WorkspaceRef, result: FinaliseResult, target_branch: str
    ) -> None:
        with self._guard:
            self.confirmed.append((workspace.lease_id, target_branch))
        if self.unreachable_merge:
            raise EvidenceError(
                "merged commit is not reachable from the delivery target branch"
            )

    def deliver_report(self, request: CampaignReport) -> FinaliseResult:
        with self._guard:
            self.reports.append(request)
        return FinaliseResult(None, "report-commit", "report-commit", True, (request.relative_path,))

    def release(self, workspace: WorkspaceRef) -> None:
        if self.release_error is not None:
            raise self.release_error
        with self._guard:
            if workspace.lease_id not in self.released:
                self.released.append(workspace.lease_id)


@dataclass
class FakeCommandAdapter(FakeAdapter):
    scripted_results: list[CommandResult] = field(default_factory=list)
    requests: list[CommandRequest] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    # Results scripted per item id. A request whose ``command_id`` names a keyed
    # item as one of its colon-separated parts takes that item's next result.
    results_by_item: dict[str, list[CommandResult]] = field(default_factory=dict)
    # Called with each request, outside the lock, before its result is
    # returned, so a test can hold one command while others run.
    on_run: Callable[[CommandRequest], None] | None = None
    _guard: threading.Lock = field(default_factory=_lock, init=False, repr=False, compare=False)

    def _item_of(self, request: CommandRequest) -> str | None:
        parts = request.command_id.split(":")
        return next((part for part in parts if part in self.results_by_item), None)

    def run(self, request: CommandRequest) -> CommandResult:
        with self._guard:
            self.requests.append(request)
            script = _item_script(self.results_by_item, self._item_of(request))
            if script is None:
                script = self.scripted_results
            if not script:
                raise AssertionError("fake command adapter has no scripted result")
            result = script.pop(0)
        if self.on_run is not None:
            self.on_run(request)
        return result

    def cancel(self, command_id: str) -> None:
        with self._guard:
            self.cancelled.append(command_id)


@dataclass
class FakeRunRecordAdapter(FakeAdapter):
    events: list[RunEvent] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)
    evidence_files: dict[str, bytes] = field(default_factory=dict)
    evidence_file_writes: list[tuple[str, bytes]] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    markers: dict[str, PhaseMarker] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=_lock, init=False, repr=False, compare=False)

    def create(self, campaign: CampaignRef) -> RunRecordRef:
        return RunRecordRef(f"run-{campaign.campaign_id}", Path("/fake/record"))

    def append(self, record: RunRecordRef, event: RunEvent) -> str:
        with self._guard:
            self.events.append(event)
            number = len(self.events)
        return f"event-{number}"

    def write_evidence(self, record: RunRecordRef, name: str, content: str) -> str:
        with self._guard:
            self.evidence[name] = content
        return f"evidence:{name}"

    def write_evidence_file(
        self, record: RunRecordRef, relative_path: str, content: bytes
    ) -> str:
        with self._guard:
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
    # Called with the surface on every renewal, so a test can observe a
    # heartbeat as it happens.
    on_renew: Callable[[LeaseSurface], None] | None = None
    _guard: threading.Lock = field(default_factory=_lock, init=False, repr=False, compare=False)

    def _record(self, campaign_id: str, surface: LeaseSurface) -> LeaseRecord:
        return LeaseRecord(
            campaign_id, self.process_id, self.host, "start", "beat", surface.path, surface.kind
        )

    def acquire(self, request: LeaseRequest) -> LeaseGrant:
        key = str(request.surface.path)
        if key in self.live_holders:
            raise LeaseHeld(self.live_holders[key])
        with self._guard:
            reclaimed = self.stale_holders.pop(key, None)
            grant = LeaseGrant(
                request.surface, self._record(request.campaign_id, request.surface), reclaimed
            )
            self.acquired.append(request.surface)
            self.grants.append(grant)
        return grant

    def renew(self, grant: LeaseGrant) -> LeaseGrant:
        with self._guard:
            self.renewed.append(grant.surface)
        if self.on_renew is not None:
            self.on_renew(grant.surface)
        return LeaseGrant(
            grant.surface, self._record(grant.record.campaign_id, grant.surface), None
        )

    def release(self, grant: LeaseGrant) -> LeaseRelease:
        with self._guard:
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
    unavailable. The record's ``cooled_until`` is the signal's reset when it
    carries one. ``cools`` records every call for assertions."""

    cooled: dict[str, LaneCooling] = field(default_factory=dict)
    cools: list[tuple[str, LaneSignal, str]] = field(default_factory=list)
    _guard: threading.Lock = field(default_factory=_lock, init=False, repr=False, compare=False)

    def active(self, lane: str) -> LaneCooling | None:
        return self.cooled.get(lane)

    def cool(self, lane: str, signal: LaneSignal, campaign_id: str) -> LaneCooling:
        record = LaneCooling(
            lane=lane,
            cooled_until=signal.reset_at or "2999-01-01T00:00:00+00:00",
            signature=signal.signature,
            last_failure_at="2026-09-03T00:00:00+00:00",
            campaign_id=campaign_id,
        )
        with self._guard:
            self.cooled[lane] = record
            self.cools.append((lane, signal, campaign_id))
        return record

    def snapshot(self) -> tuple[LaneCooling, ...]:
        return tuple(self.cooled.values())


@dataclass
class FakeUsageMeter(FakeAdapter):
    """Scripted meter readings. Each ``read`` returns the next scripted reading
    for the meter id and repeats the last one once the script runs out, so a
    test scripts only the readings that change. ``reads`` records every id."""

    readings: dict[str, list[UsageReading]] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)
    # Called with the meter id on every read before the reading is returned, so
    # a test can hold a read in flight.
    on_read: Callable[[str], None] | None = None
    _guard: threading.Lock = field(default_factory=_lock, init=False, repr=False, compare=False)

    def read(self, meter_id: str) -> UsageReading:
        with self._guard:
            self.reads.append(meter_id)
            script = self.readings.get(meter_id)
            if not script:
                reading = UsageReading.failed(meter_id, "fake meter has no scripted reading")
            else:
                reading = script.pop(0) if len(script) > 1 else script[0]
        if self.on_read is not None:
            self.on_read(meter_id)
        return reading


@dataclass
class FakeProgressPort:
    """Records ``begin`` and every ``emit`` in order, so a test can assert the
    exact progress sequence and that ``begin`` preceded the first line."""

    began: list[RunRecordRef] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    _guard: threading.Lock = field(default_factory=_lock, init=False, repr=False, compare=False)

    def begin(self, record: RunRecordRef) -> None:
        self.began.append(record)

    def emit(self, line: str) -> None:
        with self._guard:
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



@dataclass
class FakeHostResources(FakeAdapter):
    """Scripted host readings. Each ``read`` returns the next scripted reading
    and repeats the last one once the script runs out; ``samples`` records every
    requested sample length in seconds. Nothing sleeps."""

    readings: list[HostReading] = field(default_factory=list)
    samples: list[float] = field(default_factory=list)
    # When set, each read returns what it gives instead of the scripted list,
    # so a test can derive a reading from a fake clock.
    script: Callable[[], HostReading] | None = None

    def read(self, sample_seconds: float) -> HostReading:
        self.samples.append(sample_seconds)
        if self.script is not None:
            return self.script()
        if not self.readings:
            return HostReading.unmeasured("fake host has no scripted reading")
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]


@dataclass
class NullHostResources(FakeAdapter):
    """A host that measures nothing: every reading is fully unmeasured."""

    def read(self, sample_seconds: float) -> HostReading:
        return HostReading.unmeasured("null host measures nothing")
