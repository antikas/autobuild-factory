"""Immutable semantic requests, results, and evidence for AutoBuild."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
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


class EffortLevel(str, Enum):
    """How much a seat's model thinks before and between actions.

    The levels are harness-neutral. Each harness adapter declares the levels it
    can pass and maps a level onto its own command option."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


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
    TRACKER_ENVIRONMENT = "tracker_environment"
    TRACKER_REFUSED = "tracker_refused"


class LaneSignalKind(str, Enum):
    """The structural failure classes a harness adapter can read from a seat.

    ``rate_limit``, ``quota`` and ``auth`` come from the CLI's structured error
    fields; ``spawn`` is the executable exiting before any structured output;
    ``probe`` is a launch-time preflight failure; ``usage`` is a usage meter
    reading at the stop level. Every kind cools its lane."""

    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    AUTH = "auth"
    SPAWN = "spawn"
    PROBE = "probe"
    # A usage meter reading at or above the profile's stop level.
    USAGE = "usage"


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


class UsageLimitKind(str, Enum):
    """The limit classes a usage meter reports.

    ``session`` is the short rolling window, ``weekly_all`` the weekly limit
    across every model, and ``weekly_scoped`` a weekly limit for the models its
    scope label names."""

    SESSION = "session"
    WEEKLY_ALL = "weekly_all"
    WEEKLY_SCOPED = "weekly_scoped"


@dataclass(frozen=True, slots=True)
class UsageLimit:
    """One normalised meter limit.

    ``resets_at`` is an offset-aware UTC ISO 8601 instant, or None when the meter
    gave no usable reset. ``scope`` is the meter's own opaque label for the models
    a scoped limit covers; only the harness adapter interprets it."""

    kind: UsageLimitKind
    percent: float
    resets_at: str | None = None
    scope: str | None = None

    def describe(self) -> str:
        scope = f" [{self.scope}]" if self.scope else ""
        reset = self.resets_at or "an unknown time"
        return f"{self.kind.value}{scope} {_percent(self.percent)}%, resets {reset}"


def _percent(value: float) -> str:
    return f"{value:g}"


@dataclass(frozen=True, slots=True)
class UsageReading:
    """One read-only reading of a usage meter.

    A failed reading carries ``ok=False`` and a short error that names no
    credential; its limits are empty. ``extra_usage_enabled`` is None when the
    meter does not report the switch."""

    meter_id: str
    ok: bool
    limits: tuple[UsageLimit, ...] = ()
    error: str = ""
    extra_usage_enabled: bool | None = None
    fetched_at: str = ""

    @classmethod
    def failed(cls, meter_id: str, error: str, fetched_at: str = "") -> "UsageReading":
        return cls(meter_id, False, (), error or "meter unavailable", None, fetched_at)


class UsageProbeFailure(str, Enum):
    """What a run does when the meter cannot be read."""

    ALLOW = "allow"
    REFUSE = "refuse"


@dataclass(frozen=True, slots=True)
class UsageAssessment:
    """A usage reading judged against the profile's levels.

    ``lane_stops`` are unscoped limits at their stop level: they stop the lane
    until the latest of their resets. ``scoped_stops`` are model-scoped limits at
    the stop level: they stop only the seats whose model the scope covers.
    ``warnings`` are limits at or above the warning level. ``failure`` is empty
    when the reading can be used, otherwise the reason it cannot."""

    reading: UsageReading
    lane_stops: tuple[UsageLimit, ...] = ()
    scoped_stops: tuple[UsageLimit, ...] = ()
    warnings: tuple[UsageLimit, ...] = ()
    failure: str = ""

    @property
    def stops_lane(self) -> bool:
        return not self.failure and bool(self.lane_stops)

    @property
    def reset_at(self) -> str | None:
        """The latest reset among the binding unscoped limits."""

        resets = [
            (instant, limit.resets_at)
            for limit in self.lane_stops
            if limit.resets_at and (instant := _optional_instant(limit.resets_at)) is not None
        ]
        if not resets:
            return None
        return max(resets)[1]

    def describe_stop(self, policy: "UsagePolicy") -> str:
        return "; ".join(
            f"{limit.describe()} (stop {_percent(policy.stop_for(limit.kind))}%)"
            for limit in self.lane_stops
        )


def _optional_instant(moment: str | None) -> datetime | None:
    """An offset-aware instant, or None for a missing, naive or unreadable one."""

    if not moment:
        return None
    try:
        parsed = datetime.fromisoformat(moment)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass(frozen=True, slots=True)
class UsagePolicy:
    """The profile's usage levels: the single owner of the threshold rule.

    ``five_hour_stop_percent`` binds the ``session`` limit; ``stop_percent``
    binds ``weekly_all`` and ``weekly_scoped``. A limit binds at or above its
    stop level and warns at or above ``warn_percent``."""

    warn_percent: float = 80.0
    stop_percent: float = 90.0
    five_hour_stop_percent: float = 95.0
    on_probe_failure: UsageProbeFailure = UsageProbeFailure.ALLOW

    def __post_init__(self) -> None:
        for label, value in (
            ("warn_percent", self.warn_percent),
            ("stop_percent", self.stop_percent),
            ("five_hour_stop_percent", self.five_hour_stop_percent),
        ):
            if not 0 < value <= 100:
                raise ValueError(f"usage.{label} must be above 0 and at most 100")
        if self.warn_percent > self.stop_percent:
            raise ValueError("usage.warn_percent must not exceed usage.stop_percent")

    def stop_for(self, kind: UsageLimitKind) -> float:
        if kind is UsageLimitKind.SESSION:
            return self.five_hour_stop_percent
        return self.stop_percent

    def assess_meter(
        self, read: Callable[[str], UsageReading], meter_id: str
    ) -> UsageAssessment:
        """Read one meter and judge the reading, never raising: a meter or a
        reading that raises becomes a meter failure naming only the error type."""

        try:
            reading = read(meter_id)
        except Exception as exc:
            reading = UsageReading.failed(meter_id, f"meter read failed: {type(exc).__name__}")
        try:
            return self.assess(reading)
        except Exception as exc:
            error = f"meter reading could not be judged: {type(exc).__name__}"
            return UsageAssessment(UsageReading.failed(meter_id, error), failure=error)

    def assess(self, reading: UsageReading) -> UsageAssessment:
        if not reading.ok:
            return UsageAssessment(reading, failure=reading.error or "meter unavailable")
        lane_stops: list[UsageLimit] = []
        scoped_stops: list[UsageLimit] = []
        warnings: list[UsageLimit] = []
        for limit in reading.limits:
            if not math.isfinite(limit.percent):
                return UsageAssessment(
                    reading, failure=f"{limit.kind.value} has no finite percent"
                )
            if limit.percent >= self.warn_percent:
                warnings.append(limit)
            if limit.percent < self.stop_for(limit.kind):
                continue
            if limit.kind is UsageLimitKind.WEEKLY_SCOPED:
                scoped_stops.append(limit)
            else:
                lane_stops.append(limit)
        # A lane cooled without a future reset would fall back to the long
        # default cooling window, so a binding limit whose reset is missing,
        # unreadable or not after the reading is a meter failure, never a cooling.
        failure = ""
        fetched = _optional_instant(reading.fetched_at)
        for limit in lane_stops:
            reset = _optional_instant(limit.resets_at)
            if reset is None:
                failure = f"{limit.kind.value} is at its stop level with no usable reset time"
                break
            if fetched is not None and reset <= fetched:
                failure = (
                    f"{limit.kind.value} is at its stop level with a reset time "
                    f"{limit.resets_at} that is not after the reading"
                )
                break
        return UsageAssessment(
            reading, tuple(lane_stops), tuple(scoped_stops), tuple(warnings), failure
        )


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
    # False when the tracker committed the claim without publishing it to the
    # remote; ``publication_note`` then carries the tracker's own explanation.
    published: bool = True
    publication_note: str = ""


@dataclass(frozen=True, slots=True)
class TrackerPublication:
    """Whether a tracker write reached the shared remote, with the tracker's
    own explanation when it did not."""

    published: bool = True
    note: str = ""


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
    # One (lane name, seat, level) entry per seat that sets an effort on a lane. A
    # seat runs at the effort of the lane it runs on, so a seat that moves lanes
    # after a limit takes the new lane's effort.
    lane_efforts: tuple[tuple[str, Seat, EffortLevel], ...] = ()

    def seat_effort(self, seat: Seat, lane: str) -> EffortLevel | None:
        """The effort ``seat`` asks for on ``lane``, or None to leave the harness
        default in force."""

        for entry_lane, entry_seat, level in self.lane_efforts:
            if entry_lane == lane and entry_seat is seat:
                return level
        return None

    def __post_init__(self) -> None:
        if not self.validator_id.strip() or not self.validator_argv:
            raise ValueError("item execution requires an approved validator")
        entries = [(lane, seat) for lane, seat, _level in self.lane_efforts]
        if len(entries) != len(set(entries)):
            raise ValueError("lane_efforts sets the same seat more than once on one lane")
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
    # None leaves the harness's own configured effort in force.
    effort: EffortLevel | None = None

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
class SeatTransport:
    """How a harness delivers one seat to its child process, for the preflight.

    ``max_argv_element`` is the largest single argument in bytes,
    ``delivered_by_file`` says the instructions travel by file or stdin rather
    than argv, ``child_environment`` is the environment the harness adds for its
    child, and ``in_process`` marks a harness that runs the seat from inside the
    campaign process."""

    max_argv_element: int
    delivered_by_file: bool
    child_environment: tuple[tuple[str, str], ...] = ()
    in_process: bool = False


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
    effort: EffortLevel | None = None


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
    # Names the command adapter removes from the inherited environment before it
    # applies ``environment``. A name absent from the parent is ignored.
    remove_environment: tuple[str, ...] = ()
    # Text the command adapter writes to the child's standard input through a
    # pipe. It may carry a credential, so it is never written to disk and is left
    # out of the request's repr.
    stdin_text: str | None = field(default=None, repr=False)
    # Values the command adapter replaces in the child's captured output before
    # that output is written, so a credential echoed by the child never reaches
    # disk. Honoured for a request that carries ``stdin_text``.
    redact: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not self.command_id.strip() or not self.argv:
            raise ValueError("command id and argv must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("command timeout must be positive")
        if self.stdin_ref is not None and not self.stdin_ref.strip():
            raise ValueError("command stdin_ref must not be empty")
        if self.stdin_ref is not None and self.stdin_text is not None:
            raise ValueError("command stdin comes from a file or from text, not both")
        if self.redact and self.stdin_text is None:
            raise ValueError("command output redaction needs stdin_text")
        if self.progress_deadline_seconds < 0:
            raise ValueError("command progress deadline must not be negative")
        if any(not name.strip() for name in self.remove_environment):
            raise ValueError("removed environment names must not be empty")
        conflicting = sorted(
            {key for key, _ in self.environment} & set(self.remove_environment)
        )
        if conflicting:
            raise ValueError(
                f"command environment names are both set and removed: {', '.join(conflicting)}"
            )


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
    published: bool = True
    publication_note: str = ""

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
    # Set when a tracker write stopped the campaign: the stop reason and the
    # tracker's exit code, when it reported one.
    tracker_stop: CampaignStopReason | None = None
    tracker_exit_code: int | None = None


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
    efforts: Mapping[str, EffortLevel | None] = field(default_factory=dict)


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
