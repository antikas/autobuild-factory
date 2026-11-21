"""Immutable semantic requests, results, and evidence for AutoBuild."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from autobuild.domain.errors import AutoBuildError


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


# A failed outcome check raises a seat's effort no further than this; going past
# it is for the next model up, not for the same model.
EFFORT_RAISE_CEILING = EffortLevel.HIGH


def raised_effort(level: EffortLevel | None, raises: int) -> EffortLevel | None:
    """``level`` raised ``raises`` steps, capped at ``EFFORT_RAISE_CEILING``.

    A level at or past the ceiling is kept as it is, and no level (the harness
    default) stays no level, because there is nothing known to raise from."""

    if level is None or raises <= 0:
        return level
    order = list(EffortLevel)
    ceiling = order.index(EFFORT_RAISE_CEILING)
    current = order.index(level)
    if current >= ceiling:
        return level
    return order[min(current + raises, ceiling)]


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
    # A hold on admissions lasted ``run.hold_limit_seconds`` without clearing.
    HOLD_LIMIT = "hold_limit"
    # The operator asked for a pause; the next run on the repository resumes.
    PAUSED = "paused"
    # The tracker could not list its items at launch, so no launch set exists.
    LAUNCH_SET_UNREADABLE = "launch_set_unreadable"


class HoldCondition(str, Enum):
    """A condition that blocks all new work, so the campaign holds admissions
    and retries until it clears instead of stopping.

    ``lanes_exhausted``: every lane is cooling. ``tracker_pending``: a tracker
    write was appended and not committed on the primary checkout, and its
    recovery has not succeeded yet. ``tracker_unreachable``: the tracker's
    remote does not answer. ``tracker_read_failed``: reading the ready queue
    raised. ``lease_renew_failed``: the tracker lease cannot be renewed and
    nothing is running. ``batch_validation_failed``: the validator failed on
    the delivery target head, and no later batch has passed."""

    LANES_EXHAUSTED = "lanes_exhausted"
    TRACKER_PENDING = "tracker_pending"
    TRACKER_UNREACHABLE = "tracker_unreachable"
    TRACKER_READ_FAILED = "tracker_read_failed"
    LEASE_RENEW_FAILED = "lease_renew_failed"
    BATCH_VALIDATION_FAILED = "batch_validation_failed"


@dataclass(frozen=True, slots=True)
class SkippedItem:
    """A ready item the tracker passed over and why, for example ``no_brief``
    for an item without an approved brief reference."""

    item_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class TrackerRecovery:
    """What one tracker recovery left behind. ``clear`` is True when no
    partial tracker write remains on the primary checkout and the tracker
    answers; otherwise ``condition`` names what still blocks work and
    ``detail`` carries the tracker's own explanation."""

    clear: bool = True
    condition: HoldCondition | None = None
    detail: str = ""


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
    # How many items the campaign builds at once. One is the serial schedule.
    concurrency: int = 1
    # How many validator runs may run at once across the campaign's items, or
    # None for no limit beyond the item concurrency.
    heavy_concurrency: int | None = None

    def __post_init__(self) -> None:
        if not self.campaign_id.strip():
            raise ValueError("campaign_id must not be empty")
        if self.max_items < 1:
            raise ValueError("max_items must be positive")
        if self.concurrency < 1:
            raise ValueError("concurrency must be positive")
        if self.heavy_concurrency is not None and self.heavy_concurrency < 1:
            raise ValueError("heavy concurrency must be positive")


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
    phase marker's recorded revision.

    ``base_commit`` is the head with any recorded delivery integration merges
    stepped over, so a worktree whose delivery merged the target branch in still
    matches the marker that recorded its tracker commit. Without such a merge it
    equals ``head_commit``."""

    root: Path
    branch: str
    head_commit: str
    workspace_revision: str
    base_commit: str = ""


@dataclass(frozen=True, slots=True)
class ResumePlan:
    """Instruction to resume an interrupted item from its phase marker.

    ``marker_state`` is the phase to restart from, ``worktree_root`` names the
    worktree to adopt, ``correction_count`` preserves how many correction rounds
    had run, and ``prior_run_id`` names the run the marker came from.
    ``continuations`` counts the builder seats that timed out on this item in
    this campaign; above zero the resumed builder continues their partial work
    in the same worktree. ``builder_raises`` and ``builder_context_refs`` carry
    the effort raises and the context references of a builder attempt that
    timed out, so its continuation is the same attempt; a plan made from a
    phase marker carries neither."""

    marker_state: str
    worktree_root: Path
    correction_count: int
    prior_run_id: str
    continuations: int = 0
    builder_raises: int = 0
    builder_context_refs: tuple[str, ...] = ()


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


class ProcessMode(str, Enum):
    """The build process a profile opts into. ``flow`` is the default;
    ``strict`` limits a builder seat to the tests that cover its change."""

    FLOW = "flow"
    STRICT = "strict"


class ReviewLane(str, Enum):
    """The review lane a strict item with a boundary map runs in, chosen by
    what its paths touch. ``small`` touches no boundary and gets one review
    and one correction; ``material`` touches one and gets two reviews and two
    corrections. Named apart from the harness lane a seat runs on."""

    SMALL = "small"
    MATERIAL = "material"

    @property
    def correction_limit(self) -> int:
        return 1 if self is ReviewLane.SMALL else 2


@dataclass(frozen=True, slots=True)
class TestCoverage:
    """One test-map entry: source paths matching ``pattern`` are covered by
    the test paths in ``tests`` or by the full argument list ``command``."""

    __test__ = False

    pattern: str
    tests: tuple[str, ...] = ()
    command: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.pattern.strip():
            raise ValueError("a test-map pattern must not be empty")
        if bool(self.tests) == bool(self.command):
            raise ValueError("a test-map entry names test paths or a test command")

    def label(self) -> str:
        """How the brief and a refusal name this entry's covering tests."""

        return " ".join(self.command) if self.command else ", ".join(self.tests)


@dataclass(frozen=True, slots=True)
class ReviewBoundary:
    """One named sensitive area of the boundary map: a change to a path that
    matches one of ``patterns`` touches it. ``invariants`` is the repository
    path of the file listing what a change there must preserve, if any."""

    name: str
    patterns: tuple[str, ...]
    invariants: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a boundary name must not be empty")
        if not self.patterns or not all(pattern.strip() for pattern in self.patterns):
            raise ValueError(f"boundary {self.name} needs at least one path pattern")


@dataclass(frozen=True, slots=True)
class ProcessPolicy:
    """The profile's ``[process]`` table. ``test_command`` is the argument list
    that starts the project's test runner; covering test paths follow it.
    ``boundaries`` is the boundary map; only strict mode reads it."""

    mode: ProcessMode = ProcessMode.FLOW
    test_command: tuple[str, ...] = ()
    test_map: tuple[TestCoverage, ...] = ()
    boundaries: tuple[ReviewBoundary, ...] = ()

    @property
    def strict(self) -> bool:
        return self.mode is ProcessMode.STRICT

    def __post_init__(self) -> None:
        if self.strict and not self.test_map:
            raise ValueError("strict process mode needs a test map")
        if any(entry.tests for entry in self.test_map) and not self.test_command:
            raise ValueError("a test map with test paths needs a test command")
        names = [boundary.name for boundary in self.boundaries]
        if len(names) != len(set(names)):
            raise ValueError("boundary names must be unique")


@dataclass(frozen=True, slots=True)
class TestGuard:
    """The pre-command check a strict builder seat carries: the process policy
    and the item's covering entries the refusal rule judges a command against.
    Only a builder seat in strict mode carries one."""

    __test__ = False

    policy: ProcessPolicy
    covering: tuple[TestCoverage, ...] = ()

    def __post_init__(self) -> None:
        if not self.policy.strict:
            raise ValueError("a test guard needs the strict process mode")


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
    # The profile's ``[validator] patient_argv``: the argv a validator that
    # starts under recent host pressure runs instead. Empty means the normal
    # argv with the host-pressure variable set.
    validator_patient_argv: tuple[str, ...] = ()
    # The profile's ``[process]`` table; in strict mode the builder brief names
    # the covering tests for the item's declared paths.
    process: ProcessPolicy = ProcessPolicy()

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


def timeout_with_extension(
    timeout_seconds: float, extension: Callable[[], float] | None
) -> float:
    """``timeout_seconds`` plus the extension granted so far. An extension
    that cannot be read, or reads negative, adds nothing."""

    if extension is None:
        return timeout_seconds
    try:
        added = float(extension())
    except Exception:
        return timeout_seconds
    return timeout_seconds + max(0.0, added)


@dataclass(frozen=True, slots=True)
class SeatLaunch:
    """The launch a harness gave one seat: its argument vector and the names of
    the environment variables it set and removed. It holds names only, so a
    record built from it can never carry an environment value."""

    argv: tuple[str, ...]
    set_environment: tuple[str, ...]
    removed_environment: tuple[str, ...]
    in_process: bool = False


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
    # Seconds added to ``timeout_seconds`` so far, read again on every check:
    # the cap stretches while the host is under recorded pressure.
    timeout_extension: Callable[[], float] | None = field(default=None, compare=False)
    # The strict-process pre-command check; set for a strict builder seat only.
    # A harness that can carry it installs it where the seat's shell commands run.
    test_guard: TestGuard | None = None
    # Called by the harness with the seat's launch just before the seat runs.
    on_launch: Callable[[SeatLaunch], None] | None = field(default=None, compare=False)

    def current_timeout_seconds(self) -> float:
        return timeout_with_extension(self.timeout_seconds, self.timeout_extension)

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("seat timeout must be positive")
        if not self.instructions.strip():
            raise ValueError("seat instructions must not be empty")
        if self.progress_deadline_seconds < 0:
            raise ValueError("seat progress deadline must not be negative")
        if self.test_guard is not None and self.seat is not Seat.BUILDER:
            raise ValueError("only a builder seat carries a test guard")


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


def validator_command_id(*parts: str, validator: str) -> str:
    """The command id of one validator run: ``parts`` joined by ``:`` with the
    validator id as the final segment. The enforced command port and the close
    evidence identify the validator by that final segment, so every validator
    run builds its id here."""

    if not validator.strip():
        raise ValueError("a validator command id needs the validator id")
    return ":".join((*parts, validator))


def identifies_validator(command_id: str, validator_id: str) -> bool:
    """True when ``command_id`` names ``validator_id`` as its final segment."""

    return command_id.endswith(f":{validator_id}")


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
    # with only the wall-clock timeout.
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
    # The seat's stretch of ``timeout_seconds``, carried from its seat request;
    # a supervised command reads it again on every check.
    timeout_extension: Callable[[], float] | None = field(default=None, compare=False)

    def current_timeout_seconds(self) -> float:
        return timeout_with_extension(self.timeout_seconds, self.timeout_extension)

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
class InspectedPath:
    """A path a review seat reports it read, with the line ranges it read
    (``"1-120,300-340"`` or ``"all"``)."""

    path: str
    lines: str


class ClauseOutcome(str, Enum):
    MET = "met"
    UNMET = "unmet"
    NOT_CHECKED = "not_checked"


@dataclass(frozen=True, slots=True)
class ClauseCheck:
    """A review seat's judgement of one numbered acceptance clause (``A1``)."""

    clause: str
    outcome: ClauseOutcome
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class ReviewVerdict:
    item_id: str
    decision: ReviewDecision
    findings: tuple[ReviewFinding, ...]
    evidence_ref: str
    inspected: tuple[InspectedPath, ...] = ()
    acceptance: tuple[ClauseCheck, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewDepth:
    """How deeply one review seat reviewed, as the seat reported it and
    checked against the diff. ``boundary`` names a specialist's boundary."""

    seat: Seat
    boundary: str | None
    duration_seconds: float | None
    changed_paths: int
    inspected_changed_paths: int
    clauses: int
    clauses_checked: int
    shallow: bool
    reasons: tuple[str, ...] = ()


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
        unmet = [
            check.clause for check in verdict.acceptance if check.outcome is ClauseOutcome.UNMET
        ]
        if unmet:
            return f"a pass verdict must not report acceptance clause {unmet[0]} unmet"
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
    # Set when the final round ended on a `correct` verdict over a green
    # validation; the close gate accepts that verdict only with this mark.
    accepted_at_round_limit: bool = False
    # The lane the builder last settled on; empty when no lane is known.
    builder_lane: str = ""


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
    # Runs the item's validator on the integrated candidate in the worktree and
    # returns whether it passed. Delivery calls it only when integrating the
    # target branch changed a path that overlaps the item's own or declared
    # paths. None means the delivery carries no validated product change (a
    # park).
    revalidate: Callable[[], bool] | None = field(default=None, compare=False)
    # The paths the item's brief declares, exact or glob; they count as the
    # item's own paths for the overlap rule.
    declared_paths: tuple[str, ...] = ()
    # Receives every revalidation decision delivery makes, run or skipped, so
    # the workflow records why.
    note_revalidation: Callable[["RevalidationDecision"], None] | None = field(
        default=None, compare=False
    )

    def __post_init__(self) -> None:
        if not self.item_id.strip() or not self.tracker_commit.strip():
            raise ValueError("delivery requires an item and tracker commit")
        if not self.target_branch.strip() or not self.target_revision.strip():
            raise ValueError("delivery requires an explicit target branch and revision")
        if self.mode is DeliveryMode.PROTECTED_DEFAULT and self.push_current_branch:
            raise ValueError("current-branch push is only valid in current-branch-pr mode")


@dataclass(frozen=True, slots=True)
class RevalidationDecision:
    """Whether an integrated delivery candidate is validated again, and why.

    ``reason`` is ``no_product_change``, ``docs_only``, ``no_overlap``,
    ``overlap`` or ``unknown_scope``. ``incoming`` lists the product paths the
    integration changed that can trigger a run; ``overlapping`` the ones that
    did."""

    run: bool
    reason: str
    incoming: tuple[str, ...] = ()
    overlapping: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationCheckout:
    """A detached scratch worktree at a branch head, for a batch validation."""

    root: Path
    revision: str
    repository: Path


@dataclass(frozen=True, slots=True)
class BatchValidationPolicy:
    """Run the validator once on the delivery target head after every
    ``every`` deliveries and at campaign end."""

    every: int
    argv: tuple[str, ...]
    # Required: the enforced command port admits a validator run only by its id.
    validator_id: str
    patient_argv: tuple[str, ...] = ()
    timeout_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.every < 1:
            raise ValueError("run.batch_validate_every must be a positive integer")
        if not self.argv:
            raise ValueError("batch validation needs the validator argv")
        if not self.validator_id.strip():
            raise ValueError("batch validation needs the validator id")
        if self.timeout_seconds <= 0:
            raise ValueError("batch validation timeout must be positive")


class DeliveryParkReason(str, Enum):
    """Why a delivery stopped without pushing. Each is the item's park reason."""

    # Integrating the target branch conflicted outside the tracker projection.
    CONFLICT = "delivery_conflict"
    # The integrated candidate failed the item's validator.
    REVALIDATION_FAILED = "delivery_revalidation_failed"
    # The target moved outside the tracker, or the push was rejected, on every
    # one of the bounded delivery attempts.
    CONTENDED = "delivery_contended"


class DeliveryParked(AutoBuildError):
    """A delivery that ended without pushing and must park the item.

    The workspace adapter raises it; the item workflow parks the item from the
    primary checkout with ``reason`` and keeps the worktree as evidence."""

    def __init__(self, reason: DeliveryParkReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)


class BuilderContinuation(AutoBuildError):
    """A builder seat timed out and the item keeps its claim, its worktree and
    its phase marker. The item workflow raises it instead of parking; the
    campaign re-admits the item with ``plan``, and a fresh builder seat
    continues the partial work in the same worktree."""

    def __init__(self, plan: ResumePlan, reason: str) -> None:
        self.plan = plan
        super().__init__(reason)


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
    # Set for a reviewer-finding follow-up; a refill proposal leaves them empty.
    parent_item: str = ""
    evidence_ref: str = ""
    finding_code: str = ""
    blocking: bool | None = None
    # The campaign report that holds the full text; the note caption points
    # here, and to ``brief_ref`` only when it is empty.
    report_ref: str = ""

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
class FollowUpRecord:
    """One proposal the campaign registered, with the id the tracker gave it."""

    proposal_id: str
    proposal: Proposal


def fenced_block(text: str) -> str:
    """``text`` verbatim inside a Markdown code fence one backtick longer than
    its longest backtick run, and never shorter than three, so no line of the
    text can close the fence or open a section of the surrounding document.
    The line break before the closing fence is always added, so a text that
    ends in one keeps it."""

    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def labelled_value(label: str, value: str) -> list[str]:
    """``value`` after ``label`` as one Markdown list line, or, when it holds a
    line break, the label line followed by ``value`` verbatim in a fenced block,
    so no line of it can open a section of the surrounding document."""

    if "\n" not in value and "\r" not in value:
        return [f"- {label}: {value}"]
    return [f"- {label}:", "", fenced_block(value)]


def heading_text(text: str) -> str:
    """``text`` on one line: each run of whitespace holding a line break
    becomes one space, because a Markdown heading ends at its line break."""

    return re.sub(r"\s*[\r\n]\s*", " ", text)


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
    follow_ups: tuple[FollowUpRecord, ...] = ()
    # Lane failover outcome. ``lane_signature`` is set when a lane signal parked
    # the item, so the selector never re-picks it in this campaign;
    # ``lanes_exhausted`` is true when no capable lane remained, so the campaign
    # holds admissions until the earliest lane cooling resets.
    lane_signature: str | None = None
    lanes_exhausted: bool = False
    # Set when one of the item's tracker writes failed: whether the failure
    # was environmental or a refusal, and the tracker's exit code, when it
    # reported one. The campaign then asks the tracker to recover and holds
    # admissions while the tracker is not clear; it never stops for it.
    tracker_stop: CampaignStopReason | None = None
    tracker_exit_code: int | None = None
    review_depths: tuple[ReviewDepth, ...] = ()


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
    # How often the tracker lease is renewed while the campaign runs, in
    # seconds, or None to renew only between items.
    lease_heartbeat_seconds: float | None = None
    # The ``[pressure]`` thresholds the host pressure monitor applies, or None
    # for the documented defaults.
    pressure: "PressurePolicy | None" = None
    # How long one hold on admissions may last before the campaign ends with
    # the ``hold_limit`` stop reason, in seconds.
    hold_limit_seconds: float = 21600.0
    # Batch validation of the delivery target head, or None for none.
    batch: "BatchValidationPolicy | None" = None


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


class BatchVerdict(str, Enum):
    """Whether the delivery target head is proven at campaign end: the result
    of the last batch validation, or ``none`` when no batch ran."""

    NONE = "none"
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BatchValidationRun:
    """One batch validation of the delivery target head, as the campaign
    reports it. ``revision`` is ``unknown`` when the checkout could not open,
    and ``error`` is set when the validator command or checkout raised."""

    number: int
    final: bool
    revision: str
    passed: bool
    exit_code: int | None = None
    stdout_ref: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class BatchValidationSummary:
    """Every batch validation of a campaign, in the order they finished."""

    runs: tuple[BatchValidationRun, ...] = ()

    @property
    def verdict(self) -> BatchVerdict:
        if not self.runs:
            return BatchVerdict.NONE
        return BatchVerdict.PASSED if self.runs[-1].passed else BatchVerdict.FAILED

    @property
    def last(self) -> BatchValidationRun | None:
        return self.runs[-1] if self.runs else None


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
    # The schedule: most items running at once, seconds from the first
    # admission to the last completion, and the sum of every item's seconds.
    peak_concurrency: int = 0
    makespan_seconds: float = 0.0
    item_seconds: float = 0.0
    batch_validation: BatchValidationSummary = BatchValidationSummary()


# Memory sizes in readings, profile values and output are binary gigabytes.
GIB = 2**30


def bytes_to_gib(value: int | None) -> float | None:
    """A byte count in GiB, or None for an unmeasured field."""

    return None if value is None else value / GIB


@dataclass(frozen=True, slots=True)
class HostReading:
    """One read-only sample of the host's resources.

    A field the host cannot measure is None, so ``measured`` derives the flag
    for each field from its value and the two can never disagree.
    ``cpu_busy_percent`` is the system-wide busy share over ``sample_seconds``.
    ``commit_total_bytes`` is the host's enforced commit limit, where it has
    one, and ``commit_available_bytes`` the headroom left under it."""

    logical_cpus: int | None
    cpu_busy_percent: float | None
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    commit_total_bytes: int | None
    commit_available_bytes: int | None
    sample_seconds: float = 0.0
    diagnostics: tuple[str, ...] = ()

    MEASURED_FIELDS = (
        "logical_cpus",
        "cpu_busy_percent",
        "memory_total_bytes",
        "memory_available_bytes",
        "commit_total_bytes",
        "commit_available_bytes",
    )

    @classmethod
    def unmeasured(cls, *diagnostics: str, logical_cpus: int | None = None) -> "HostReading":
        return cls(logical_cpus, None, None, None, None, None, 0.0, tuple(diagnostics))

    def measured(self) -> dict[str, bool]:
        return {name: getattr(self, name) is not None for name in self.MEASURED_FIELDS}


# The largest explicit ``run.concurrency`` a profile may name.
MAX_EXPLICIT_CONCURRENCY = 64


@dataclass(frozen=True, slots=True)
class CapacityPolicy:
    """The profile's concurrency settings: ``[run] concurrency`` and
    ``heavy_concurrency`` (None means auto) and the ``[capacity]`` terms the
    automatic formula uses. Memory terms are GiB. An explicit concurrency above
    ``MAX_EXPLICIT_CONCURRENCY`` is refused."""

    concurrency: int | None = None
    heavy_concurrency: int | None = None
    reserve_gib: float = 8.0
    per_item_gib: float = 3.0
    max_concurrency: int = 8

    def __post_init__(self) -> None:
        for label, value in (
            ("run.concurrency", self.concurrency),
            ("run.heavy_concurrency", self.heavy_concurrency),
        ):
            if value is not None and value < 1:
                raise ValueError(f'{label} must be "auto" or a positive integer')
        if self.concurrency is not None and self.concurrency > MAX_EXPLICIT_CONCURRENCY:
            raise ValueError(
                f"run.concurrency must be at most {MAX_EXPLICIT_CONCURRENCY}; "
                f"{self.concurrency} was named"
            )
        if self.reserve_gib < 0:
            raise ValueError("capacity.reserve_gb must not be negative")
        if self.per_item_gib <= 0:
            raise ValueError("capacity.per_item_gb must be a positive number")
        if self.max_concurrency < 1:
            raise ValueError("capacity.max_concurrency must be a positive integer")


@dataclass(frozen=True, slots=True)
class HostCapacity:
    """Derived item and heavy-command concurrency, with the terms that set it.

    ``items_source`` and ``heavy_source`` are ``auto`` or ``profile``. The CPU
    and memory terms are the unclamped automatic item terms, reported even when
    a profile value overrides them; a term is None when its field is
    unmeasured."""

    items: int
    heavy: int
    items_source: str
    heavy_source: str
    cpu_term: int | None
    memory_term: int | None


class PressureLevel(str, Enum):
    """System-wide host pressure as the campaign's monitor sees it.

    Only ``normal`` admits new items. ``recovering`` is the span between
    leaving ``high`` and reaching ``normal``: it still admits nothing, but heavy
    commands may start again. ``high`` and ``critical`` start no heavy command."""

    NORMAL = "normal"
    RECOVERING = "recovering"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def admits(self) -> bool:
        return self is PressureLevel.NORMAL

    @property
    def holds_heavy(self) -> bool:
        return self in (PressureLevel.HIGH, PressureLevel.CRITICAL)


@dataclass(frozen=True, slots=True)
class PressurePolicy:
    """The ``[pressure]`` profile table: every threshold and interval of the
    host pressure rule. Times are seconds, memory sizes GiB, shares percent.

    The monitor reads the host every ``sample_seconds``, each reading measuring
    CPU busy over ``read_seconds``. A window at time t holds the samples with
    timestamps in [t - window, t]."""

    sample_seconds: float = 10.0
    read_seconds: float = 1.0
    window_seconds: float = 60.0
    calm_seconds: float = 120.0
    high_cpu_percent: float = 90.0
    calm_cpu_percent: float = 75.0
    high_memory_percent: float = 10.0
    high_memory_gib: float = 4.0
    critical_memory_percent: float = 5.0
    critical_memory_gib: float = 2.0
    high_commit_percent: float = 10.0
    calm_memory_gib: float = 2.0
    decrease_seconds: float = 300.0
    increase_seconds: float = 60.0

    def __post_init__(self) -> None:
        for name in (
            "sample_seconds", "read_seconds", "window_seconds", "calm_seconds",
            "decrease_seconds", "increase_seconds",
        ):
            if not getattr(self, name) > 0:
                raise ValueError(f"pressure.{name} must be a positive number")
        if self.read_seconds > self.sample_seconds:
            raise ValueError("pressure.read_seconds must not exceed pressure.sample_seconds")
        for name in (
            "high_cpu_percent", "calm_cpu_percent", "high_memory_percent",
            "critical_memory_percent", "high_commit_percent",
        ):
            if not 0 <= getattr(self, name) <= 100:
                raise ValueError(f"pressure.{name} must be between 0 and 100")
        if self.calm_cpu_percent > self.high_cpu_percent:
            raise ValueError("pressure.calm_cpu_percent must not exceed pressure.high_cpu_percent")
        for name in ("high_memory_gib", "critical_memory_gib", "calm_memory_gib"):
            if getattr(self, name) < 0:
                raise ValueError(f"pressure.{name.removesuffix('_gib')}_gb must not be negative")


@dataclass(frozen=True, slots=True)
class PressureState:
    """The monitor's state after one assessment.

    ``cap`` is the effective item cap, between 1 and ``capacity``.
    ``samples`` holds the ``(time, cpu busy percent)`` pairs still inside the
    longest window; ``first_at`` is the time of the first sample. ``cap_since``
    is when the cap last moved or the current pressure span began, from which
    the next step is timed. ``episode`` counts level changes, so a continuous
    span at one level is one episode. The reading fields describe the latest
    assessment for events: the CPU window mean, available memory and the high
    memory line, each None when unmeasured."""

    level: PressureLevel
    cap: int
    capacity: int
    samples: tuple[tuple[float, float], ...] = ()
    first_at: float | None = None
    cap_since: float = 0.0
    episode: int = 0
    cpu_mean_percent: float | None = None
    memory_available_bytes: int | None = None
    high_line_bytes: float | None = None
