"""Immutable semantic requests, results, and evidence for AutoBuild."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class PortKind(str, Enum):
    TRACKER = "tracker"
    WORKSPACE = "workspace"
    HARNESS = "harness"
    COMMAND = "command"
    RUN_RECORD = "run_record"
    KNOWLEDGE = "knowledge"


class Seat(str, Enum):
    BUILDER = "builder"
    REVIEWER = "reviewer"
    SPECIALIST = "specialist"


class SeatOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    STALLED = "stalled"
    UNAVAILABLE = "unavailable"


class ReviewDecision(str, Enum):
    PASS = "pass"
    CORRECT = "correct"
    ESCALATE = "escalate"
    PARK = "park"


class ChangeKind(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    SYMLINK = "symlink"


class ItemState(str, Enum):
    READY = "ready"
    VERIFIED = "verified"
    CLAIMED = "claimed"
    ISOLATED = "isolated"
    BUILT = "built"
    VALIDATED = "validated"
    REVIEWED = "reviewed"
    CORRECTING = "correcting"
    ESCALATED = "escalated"
    FINALISED = "finalised"
    PARKED = "parked"
    RELEASED = "released"


class ItemDisposition(str, Enum):
    ACCEPTED = "accepted"
    PARKED = "parked"
    FAILED = "failed"


class ItemNature(str, Enum):
    """Whether an item can be built inside the isolated worktree fence.

    ``REPOSITORY`` work lives entirely under the repository and the allowed
    roots. The other classes cannot be built by a fenced builder seat and are
    parked at triage: ``MACHINE`` needs installs, services, global
    configuration, the registry, or scheduled tasks; ``CROSS_REPOSITORY``
    declares a path outside the repository and the allowed roots;
    ``OWNER_GATED`` needs a person or production access."""

    REPOSITORY = "repository"
    MACHINE = "machine"
    CROSS_REPOSITORY = "cross-repository"
    OWNER_GATED = "owner-gated"


class CampaignStopReason(str, Enum):
    QUEUE_DRY = "queue_dry"
    ITEM_BOUND = "item_bound"
    STRUCTURAL_FAILURE = "structural_failure"
    SCOPE_FENCE_VIOLATION = "scope_fence_violation"
    LANES_EXHAUSTED = "lanes_exhausted"


class LaneSignalKind(str, Enum):
    """The structural failure classes a harness adapter can read from a seat.

    ``rate_limit``, ``quota`` and ``auth`` come from the CLI's structured error
    fields; ``spawn`` is the executable exiting before any structured output;
    ``probe`` is a launch-time preflight failure. Every kind cools its lane."""

    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    AUTH = "auth"
    SPAWN = "spawn"
    PROBE = "probe"


@dataclass(frozen=True, slots=True)
class LaneSignal:
    """A structural reason to cool a lane, owned by the harness adapter.

    ``reset_at`` is a UTC timestamp only when the vendor supplies one; otherwise
    the lane state applies the profile's ``run.lane_cool_seconds``. The signature
    is the stable structural fingerprint the tracker parks under and the selector
    refuses to re-pick within one campaign."""

    kind: LaneSignalKind
    reset_at: str | None = None
    detail: str = ""

    @property
    def signature(self) -> str:
        return self.kind.value


@dataclass(frozen=True, slots=True)
class LaneCooling:
    """One lane's machine-local cooling record, shared across campaigns."""

    lane: str
    cooled_until: str
    signature: str
    last_failure_at: str
    campaign_id: str


class DeliveryMode(str, Enum):
    PROTECTED_DEFAULT = "protected-default"
    CURRENT_BRANCH_PR = "current-branch-pr"


@dataclass(frozen=True, slots=True)
class AdapterIdentity:
    name: str
    version: str
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("adapter name must not be empty")
        if not self.version.strip():
            raise ValueError("adapter version must not be empty")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    available: bool
    identity: AdapterIdentity | None = None
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.available and self.identity is None:
            raise ValueError("an available adapter must provide its identity")
        if not self.available and self.identity is not None:
            raise ValueError("an unavailable adapter must not provide an identity")

    @classmethod
    def ready(cls, identity: AdapterIdentity, *diagnostics: str) -> ProbeResult:
        return cls(True, identity, tuple(diagnostics))

    @classmethod
    def unavailable(cls, *diagnostics: str) -> ProbeResult:
        return cls(False, None, tuple(diagnostics))


@dataclass(frozen=True, slots=True)
class PreflightProbe:
    """One preflight check, its pass or fail result, and a human-readable detail."""

    name: str
    passed: bool
    detail: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("preflight probe name must not be empty")


@dataclass(frozen=True, slots=True)
class CampaignSelection:
    """The closed universe and exclusions for one campaign, with their provenance.

    ``allow`` is optional. When present it is the closed universe and its order is
    the dispatch order. ``exclude`` is always checked. ``allow_source`` and
    ``exclude_source`` record where each list came from for the manifest."""

    allow: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    allow_source: str = ""
    exclude_source: str = ""

    @property
    def active(self) -> bool:
        return bool(self.allow or self.exclude)

    def permits(self, item_id: str) -> bool:
        if self.allow and item_id not in self.allow:
            return False
        return item_id not in self.exclude


@dataclass(frozen=True, slots=True)
class CampaignRef:
    campaign_id: str
    repository: Path
    max_items: int = 1
    refill_enabled: bool = False
    selection: CampaignSelection = field(default_factory=CampaignSelection)

    def __post_init__(self) -> None:
        if not self.campaign_id.strip():
            raise ValueError("campaign_id must not be empty")
        if self.max_items < 1:
            raise ValueError("max_items must be positive")


@dataclass(frozen=True, slots=True)
class WorkItem:
    item_id: str
    title: str
    brief_ref: str
    acceptance: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.item_id.strip() or not self.title.strip():
            raise ValueError("work item id and title must not be empty")
        if not self.brief_ref.strip():
            raise ValueError("work item brief_ref must not be empty")
        if not self.acceptance:
            raise ValueError("work item must declare measurable acceptance")


@dataclass(frozen=True, slots=True)
class ClaimReceipt:
    item_id: str
    actor: str
    claimed_at: str


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    root: Path
    default_branch: str
    current_branch: str
    remote: str
    revision: str


@dataclass(frozen=True, slots=True)
class WorkspaceRef:
    root: Path
    branch: str
    start_commit: str
    lease_id: str


@dataclass(frozen=True, slots=True)
class ChangedPath:
    path: Path
    kind: ChangeKind
    digest: str | None = None


@dataclass(frozen=True, slots=True)
class DiffEvidence:
    workspace: WorkspaceRef
    workspace_revision: str
    changed_paths: tuple[ChangedPath, ...]
    patch_ref: str
    head_commit: str = ""


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    """One untracked product file preserved verbatim in a park snapshot."""

    path: str
    content: bytes
    digest: str


@dataclass(frozen=True, slots=True)
class WorktreeSnapshot:
    """The material a worktree yields before a park releases it: a binary patch of
    the tracked product changes against the start commit, the untracked product
    files copied verbatim, and the changed-path digests that describe both."""

    start_commit: str
    patch: bytes
    files: tuple[SnapshotFile, ...]
    changed_paths: tuple[ChangedPath, ...]


@dataclass(frozen=True, slots=True)
class PhaseMarker:
    """The per-item phase marker AutoBuild writes at every state transition and
    reads back at launch to resume an interrupted item.

    ``state`` is an ``ItemState`` value or one of the marker-only terminal values
    ``closed`` and ``parked``. ``worktree_root`` and ``branch`` locate the
    interrupted worktree; ``head_commit`` and ``workspace_revision`` pin the
    branch head and product status digest the last recorded phase was decided
    against, so a resume can compare them to the live worktree. ``run_id`` names
    the run the marker was read from, for the ``item.resumed`` record."""

    item_id: str
    state: str
    worktree_root: Path
    branch: str
    head_commit: str
    workspace_revision: str
    correction_count: int
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class WorktreeStatus:
    """A registered worktree of the repository under the scratch root.

    ``workspace_revision`` is the same product status digest the diff evidence
    records, computed against the worktree head, so a resume can compare it to a
    phase marker's recorded revision."""

    root: Path
    branch: str
    head_commit: str
    workspace_revision: str


@dataclass(frozen=True, slots=True)
class ResumePlan:
    """Instruction to resume an interrupted item from its phase marker.

    ``marker_state`` is the phase to restart from, ``worktree_root`` names the
    worktree to adopt, ``correction_count`` preserves how many correction rounds
    had run, and ``prior_run_id`` names the run the marker came from."""

    marker_state: str
    worktree_root: Path
    correction_count: int
    prior_run_id: str


class SurfaceKind(str, Enum):
    """The kind of writable surface a single-writer lease protects."""

    TRACKER = "tracker"
    WORKTREE = "worktree"


@dataclass(frozen=True, slots=True)
class LeaseSurface:
    """A writable surface that admits one live writer: a tracker root or a worktree."""

    path: Path
    kind: SurfaceKind


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    """The durable identity of a lease holder, persisted per surface.

    ``process_id`` and ``host`` name the writer that holds the surface; the two
    timestamps let a later campaign judge whether the lease is still live or has
    gone stale. The surface path and kind are stored so a lease file names the
    surface it guards without depending on its own file name."""

    campaign_id: str
    process_id: int
    host: str
    started_at: str
    heartbeat_at: str
    surface_path: Path
    surface_kind: SurfaceKind


@dataclass(frozen=True, slots=True)
class LeaseRequest:
    """A request to hold one surface for the named campaign."""

    surface: LeaseSurface
    campaign_id: str


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    """Proof that this process holds a surface, with any reclaimed prior holder.

    ``reclaimed`` is the previous holder's record when this grant took over a
    stale lease, so the campaign can record the reclaim. It is ``None`` when the
    surface was free."""

    surface: LeaseSurface
    record: LeaseRecord
    reclaimed: LeaseRecord | None = None


@dataclass(frozen=True, slots=True)
class LeaseRelease:
    """The outcome of releasing a surface. ``released`` is false when this process
    did not hold the surface; the diagnostics explain a no-op release."""

    surface: LeaseSurface
    released: bool
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    allowed_tools: frozenset[str]
    allowed_roots: tuple[Path, ...]
    allow_destructive: bool = False
    allow_publication: bool = False
    allow_protected_merge: bool = False


@dataclass(frozen=True, slots=True)
class ItemExecutionSpec:
    item: WorkItem
    brief_path: Path
    validator_id: str
    validator_argv: tuple[str, ...]
    tool_policy: ToolPolicy
    builder_model_class: str
    reviewer_model_class: str
    specialist_model_class: str
    brief_text: str = ""
    seat_timeout_seconds: float = 900.0
    command_timeout_seconds: float = 600.0
    max_corrections: int = 2
    reviewer_tool_policy: ToolPolicy | None = None
    delivery_mode: DeliveryMode = DeliveryMode.PROTECTED_DEFAULT
    delivery_target_branch: str = ""
    delivery_target_revision: str = ""
    push_current_branch: bool = False
    allow_current_branch_default: bool = False
    seat_stall_seconds: float = 900.0

    def __post_init__(self) -> None:
        if not self.validator_id.strip() or not self.validator_argv:
            raise ValueError("item execution requires an approved validator")
        if self.seat_timeout_seconds <= 0 or self.command_timeout_seconds <= 0:
            raise ValueError("execution timeouts must be positive")
        if self.seat_stall_seconds <= 0:
            raise ValueError("seat stall deadline must be positive")
        if self.max_corrections < 0:
            raise ValueError("max_corrections must not be negative")


@dataclass(frozen=True, slots=True)
class SeatRequest:
    run_id: str
    item_id: str
    seat: Seat
    model_class: str
    brief_path: Path
    workspace: WorkspaceRef
    tool_policy: ToolPolicy
    instructions: str
    result_contract: str
    timeout_seconds: float
    context_refs: tuple[str, ...] = ()
    # The stall deadline (in seconds) and the workspace-supplied progress digest
    # the command adapter uses to decide whether the seat is making progress. A
    # zero deadline disables stall detection; the digest is a zero-argument
    # callable the item workflow binds to the seat's own workspace.
    progress_deadline_seconds: float = 0.0
    progress_digest: Callable[[], str] | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("seat timeout must be positive")
        if not self.instructions.strip():
            raise ValueError("seat instructions must not be empty")
        if self.progress_deadline_seconds < 0:
            raise ValueError("seat progress deadline must not be negative")


@dataclass(frozen=True, slots=True)
class SeatUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None
    source: str = "unavailable"


@dataclass(frozen=True, slots=True)
class BuilderReport:
    report_ref: str
    summary: str


@dataclass(frozen=True, slots=True)
class SeatResult:
    run_ref: str
    outcome: SeatOutcome
    payload: BuilderReport | ReviewVerdict | None
    raw_output_ref: str
    usage: SeatUsage
    started_at: str
    ended_at: str
    diagnostics: tuple[str, ...] = ()
    exit_code: int | None = None
    model: str = ""
    # Per-signal last-progress times (seconds since the seat started) when the
    # seat was killed for stalling, carried through to the park payload.
    stall_sample_times: tuple[tuple[str, float], ...] = ()
    # The lane this seat ran on, stamped by the item workflow's lane router.
    lane: str = ""


@dataclass(frozen=True, slots=True)
class SeatObservation:
    """What one seat invocation cost and produced, for the run record and report."""

    seat: Seat
    model_class: str
    model: str
    outcome: SeatOutcome
    exit_code: int | None
    started_at: str
    ended_at: str
    duration_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    cost: float | None
    raw_output_ref: str
    stderr_ref: str
    lane: str = ""


@dataclass(frozen=True, slots=True)
class CommandRequest:
    command_id: str
    argv: tuple[str, ...]
    cwd: Path
    environment: tuple[tuple[str, str], ...] = ()
    timeout_seconds: float = 600.0
    shell: Path | None = None
    stdin_ref: str | None = None
    # The progress-deadline cap (seconds) and the workspace progress digest the
    # command adapter samples to detect a stall. A zero deadline runs the command
    # with only the wall-clock timeout, exactly as before.
    progress_deadline_seconds: float = 0.0
    progress_digest: Callable[[], str] | None = None

    def __post_init__(self) -> None:
        if not self.command_id.strip() or not self.argv:
            raise ValueError("command id and argv must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("command timeout must be positive")
        if self.stdin_ref is not None and not self.stdin_ref.strip():
            raise ValueError("command stdin_ref must not be empty")
        if self.progress_deadline_seconds < 0:
            raise ValueError("command progress deadline must not be negative")


@dataclass(frozen=True, slots=True)
class CommandResult:
    command_id: str
    exit_code: int | None
    stdout_ref: str
    stderr_ref: str
    started_at: str
    ended_at: str
    timed_out: bool = False
    cancelled: bool = False
    stalled: bool = False
    # Per-signal last-progress times (seconds since the command started) recorded
    # when the command was killed for stalling; empty otherwise.
    stall_sample_times: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationEvidence:
    validator_id: str
    workspace_revision: str
    command: CommandResult
    changed_paths: tuple[ChangedPath, ...]


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    code: str
    consequence: str
    evidence_ref: str
    blocking: bool
    specialist_boundary: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewVerdict:
    item_id: str
    decision: ReviewDecision
    findings: tuple[ReviewFinding, ...]
    evidence_ref: str


def review_verdict_rule_error(verdict: ReviewVerdict) -> str | None:
    """Return why a verdict breaks the blocking rules, or None when it holds.

    A reviewer blocks only for a concrete consequence it would not merge under
    its own name. `correct` and `escalate` therefore need at least one blocking
    finding, `park` needs at least one finding, and `pass` may carry non-blocking
    findings only. Callers raise EvidenceError with the returned reason."""

    findings = verdict.findings
    has_blocking = any(finding.blocking for finding in findings)
    decision = verdict.decision
    if decision is ReviewDecision.PASS:
        if has_blocking:
            return "a pass verdict must not carry a blocking finding"
        return None
    if decision is ReviewDecision.PARK:
        if not findings:
            return "a park verdict requires at least one finding"
        return None
    if not has_blocking:
        return f"a {decision.value} verdict requires at least one blocking finding"
    return None


@dataclass(frozen=True, slots=True)
class CloseEvidence:
    item_id: str
    workspace_revision: str
    diff: DiffEvidence
    validation: ValidationEvidence
    verdict: ReviewVerdict
    trajectory_ref: str


@dataclass(frozen=True, slots=True)
class FinaliseRequest:
    item_id: str
    evidence: CloseEvidence
    commit_message: str


@dataclass(frozen=True, slots=True)
class DeliveryRequest:
    item_id: str
    item_commit: str | None
    tracker_commit: str
    mode: DeliveryMode
    target_branch: str
    target_revision: str
    push_current_branch: bool = False
    allow_current_branch_default: bool = False

    def __post_init__(self) -> None:
        if not self.item_id.strip() or not self.tracker_commit.strip():
            raise ValueError("delivery requires an item and tracker commit")
        if not self.target_branch.strip() or not self.target_revision.strip():
            raise ValueError("delivery requires an explicit target branch and revision")
        if self.mode is DeliveryMode.PROTECTED_DEFAULT and self.push_current_branch:
            raise ValueError("current-branch push is only valid in current-branch-pr mode")


@dataclass(frozen=True, slots=True)
class FinaliseResult:
    item_commit: str | None
    tracker_commit: str | None
    merged_commit: str | None
    pushed: bool
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Proposal:
    title: str
    question: str
    rationale: str
    brief_ref: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.title, self.question, self.rationale, self.brief_ref)
        ):
            raise ValueError("proposal fields must not be empty")


@dataclass(frozen=True, slots=True)
class ProposalRef:
    proposal_id: str
    runnable: bool = False

    def __post_init__(self) -> None:
        if self.runnable:
            raise ValueError("workflow-created proposals must remain non-runnable")


@dataclass(frozen=True, slots=True)
class FogRecord:
    direction: str
    blocking_question: str
    surface_when: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.direction, self.blocking_question, self.surface_when)
        ):
            raise ValueError("fog fields must not be empty")


@dataclass(frozen=True, slots=True)
class DurableContext:
    query: str
    references: tuple[str, ...]
    excerpts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunRecordRef:
    run_id: str
    root: Path


@dataclass(frozen=True, slots=True)
class RunEvent:
    event_type: str
    occurred_at: str = ""
    item_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RefillPlan:
    proposals: tuple[Proposal, ...] = ()
    fog: tuple[FogRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class ItemOutcome:
    item_id: str
    disposition: ItemDisposition
    states: tuple[ItemState, ...]
    reason: str | None = None
    finalise: FinaliseResult | None = None
    structural_failure: bool = False
    title: str = ""
    seats: tuple[SeatObservation, ...] = ()
    follow_ups: tuple[str, ...] = ()
    # Lane failover outcome. ``lane_signature`` is set when a lane signal parked
    # the item, so the selector never re-picks it in this campaign;
    # ``lanes_exhausted`` is true when no capable lane remained, so the campaign
    # stops with the ``lanes_exhausted`` reason.
    lane_signature: str | None = None
    lanes_exhausted: bool = False


@dataclass(frozen=True, slots=True)
class CampaignContext:
    """Provider and delivery facts the composition root hands to a campaign as data."""

    harness: str
    models: Mapping[str, str]
    delivery_mode: DeliveryMode
    validator_id: str
    target_branch: str
    target_revision: str
    push_current_branch: bool = False
    allow_current_branch_default: bool = False
    tracker_surface: LeaseSurface | None = None


@dataclass(frozen=True, slots=True)
class CampaignReport:
    campaign_id: str
    repository: Path
    relative_path: str
    content: str
    mode: DeliveryMode
    target_branch: str
    target_revision: str
    push_current_branch: bool = False
    allow_current_branch_default: bool = False


@dataclass(frozen=True, slots=True)
class CampaignOutcome:
    campaign_id: str
    items: tuple[ItemOutcome, ...]
    stop_reason: CampaignStopReason
    report_ref: str
    repository_report_ref: str = ""
    # The absolute path of the run record's plain-language progress log, carried
    # into the campaign result JSON beside the run and repository report refs.
    progress_ref: str = ""
