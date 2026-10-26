"""Campaign-level queue loop and proposal-only dry-queue behaviour."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Condition, Event, Lock, Thread

from autobuild.application.dependencies import WorkflowPorts
from autobuild.application.item import (
    COORDINATOR_ACTOR,
    HeavySlots,
    ItemWorkflow,
    UnfinishedItem,
    UnfinishedItems,
    UnpublishedWrites,
    run_heavy,
    unpublished_write,
)
from autobuild.application.progress import render_progress_line
from autobuild.domain import (
    CampaignContext,
    CampaignOutcome,
    CampaignRef,
    CampaignReport,
    CampaignStopReason,
    ClaimTaken,
    DeliveryMode,
    ItemDisposition,
    ItemExecutionSpec,
    ItemOutcome,
    ItemState,
    LeaseGrant,
    LeaseHeld,
    LeaseRecord,
    LeaseRequest,
    LeaseSurface,
    PhaseMarker,
    RefillPlan,
    ResumePlan,
    RunEvent,
    RunRecordRef,
    ScopeFenceViolation,
    SurfaceKind,
    WorkItem,
    WorktreeStatus,
)
from autobuild.domain.models import (
    GIB,
    BatchValidationPolicy,
    CommandRequest,
    HoldCondition,
    HostReading,
    PressureLevel,
    PressurePolicy,
    TrackerRecovery,
    bytes_to_gib,
)
from autobuild.enforcement.pressure import assess_pressure, initial_pressure
from autobuild.enforcement.selection import admission_limit
from autobuild.enforcement.triage import exact_declared_paths, share_declared_path


# The marker states a claimed item can be resumed from. A ``closed`` marker is a
# terminal value that is never resumed; any other value on a still-claimed item
# is a tracker mismatch.
_RESUMABLE_MARKER_STATES = frozenset(
    {
        ItemState.CLAIMED.value,
        ItemState.ISOLATED.value,
        ItemState.BUILT.value,
        ItemState.VALIDATED.value,
        ItemState.REVIEWED.value,
        ItemState.CORRECTING.value,
        ItemState.ESCALATED.value,
        ItemState.FINALISED.value,
    }
)
# The marker states whose recorded workspace revision must still match the live
# worktree digest for a good resume, so a worktree changed after the marker is
# never resumed as if it were the recorded one.
_DIGEST_CHECK_STATES = frozenset(
    {ItemState.BUILT.value, ItemState.VALIDATED.value, ItemState.REVIEWED.value}
)

# Seconds between retries of a hold: a pending tracker write is retried at a
# fixed interval, an unreachable tracker with a growing backoff, and lanes at
# the earliest cooling reset within these bounds.
_PENDING_RETRY_SECONDS = 60.0
_UNREACHABLE_BACKOFF_SECONDS = (30.0, 60.0, 120.0, 240.0, 480.0, 600.0)
_LANES_RETRY_BOUNDS = (1.0, 600.0)
# Seconds between renewal attempts while a failed tracker-lease renewal holds
# admissions with nothing running.
_LEASE_RETRY_SECONDS = 30.0
# Seconds between checks of a batch-failure hold for a moved target head.
_BATCH_RETRY_SECONDS = 600.0


@dataclass(slots=True)
class _Hold:
    """One hold on admissions: what it waits for, when it began on the
    schedule clock, when its condition is probed next and how often it was."""

    condition: HoldCondition
    started: float
    retry_at: float
    detail: str
    retries: int = 0


@dataclass(frozen=True, slots=True)
class _HoldRecord:
    """A hold that ended, for the campaign report."""

    condition: HoldCondition
    seconds: float
    ended: str


@dataclass(frozen=True, slots=True)
class _ResumeAssessment:
    good: bool
    reason: str


@dataclass(frozen=True, slots=True)
class _ResumeCandidate:
    """An interrupted claim found at launch, with the phase marker read then."""

    item: WorkItem
    marker: PhaseMarker | None


@dataclass(slots=True)
class _Flight:
    """One admitted item: its declared exact paths, its worker and its start."""

    item: WorkItem
    paths: frozenset[str]
    worker: Thread
    started: float


@dataclass(frozen=True, slots=True)
class _Completion:
    """A finished worker's result. ``outcome`` is None when the claim was taken
    by another writer; ``taken`` then carries the tracker's message."""

    item_id: str
    outcome: ItemOutcome | None
    taken: str
    finished: float


class _Completions:
    """Finished workers waiting for the scheduler, and nudges from the
    pressure monitor and the lease heartbeat that ask it to decide again.

    Its condition covers only the queue, the nudge flag and the idle flag. A
    worker appends after its item has ended; the scheduler drains, or waits for
    the next append or nudge, so one that arrives between two checks is never
    missed. ``idle`` is True only while the scheduler waits with nothing to
    take: every append or nudge clears it under the same condition."""

    __slots__ = ("_queue", "_nudged", "_idle", "_changed")

    def __init__(self) -> None:
        self._queue: list[_Completion] = []
        self._nudged = False
        self._idle = False
        self._changed = Condition()

    def post(self, completion: _Completion) -> None:
        with self._changed:
            self._queue.append(completion)
            self._idle = False
            self._changed.notify_all()

    def nudge(self) -> None:
        with self._changed:
            self._nudged = True
            self._idle = False
            self._changed.notify_all()

    def take(self) -> list[_Completion]:
        with self._changed:
            taken, self._queue = self._queue, []
        return taken

    def wait(self) -> list[_Completion]:
        with self._changed:
            while not self._queue and not self._nudged:
                self._idle = True
                self._changed.wait()
            self._idle = False
            self._nudged = False
            taken, self._queue = self._queue, []
        return taken

    def idle(self) -> bool:
        with self._changed:
            return self._idle


# Consecutive failed heartbeat renewals after which admissions are withheld.
_LEASE_FAILURES_WITHHELD = 3


class _TrackerLease:
    """The campaign's tracker lease grant, renewed by the scheduler between
    items and by the heartbeat on its timer, with the count of consecutive
    failed heartbeat renewals. The lock covers only reading and replacing the
    grant reference and the count, never a renewal."""

    __slots__ = ("_grant", "_failures", "_guard")

    def __init__(self, grant: LeaseGrant | None) -> None:
        self._grant = grant
        self._failures = 0
        self._guard = Lock()

    def get(self) -> LeaseGrant | None:
        with self._guard:
            return self._grant

    def renewed(self, grant: LeaseGrant | None) -> bool:
        """Record a successful renewal. Returns True when it ends a hold."""

        with self._guard:
            lifted = self._failures >= _LEASE_FAILURES_WITHHELD
            self._grant = grant
            self._failures = 0
            return lifted

    def failed(self) -> int:
        """Record a failed heartbeat renewal; returns the consecutive count."""

        with self._guard:
            self._failures += 1
            return self._failures

    def withheld(self) -> bool:
        with self._guard:
            return self._failures >= _LEASE_FAILURES_WITHHELD


@dataclass(frozen=True, slots=True)
class _PressureView:
    """What the scheduler reads from the pressure monitor: the level, the
    effective item cap and the level's episode number."""

    level: PressureLevel
    cap: int
    episode: int


_NO_PRESSURE_EPISODE = -1
# Consecutive failed host reads after which the monitor records its fallback.
_PRESSURE_FAILURES_FALLBACK = 3


class _PressureMonitor:
    """Watches system-wide host pressure for one campaign.

    A sampler thread reads the host port every ``sample_seconds`` and hands
    each reading to the pressure rule in ``enforcement.pressure``. The rule's
    state belongs to the sampler thread alone. The lock covers only the shared
    view (level, cap, episode) and the time spent at each level, never a
    sample. After each reading the sampler publishes the view, tells the heavy
    slots whether heavy commands are withheld and nudges the scheduler. It
    never touches a running seat or command. A first reading without CPU busy
    or available memory ends the monitor with one warning at the static
    capacity. After a measured reading, a bad or failing read is skipped
    without changing anything (see ``_bad_read``), and a record that cannot be
    written is dropped, so neither ends pressure control. Stopping the monitor
    lifts its heavy-command hold."""

    def __init__(
        self,
        port,
        policy: PressurePolicy,
        capacity: int,
        clock: Callable[[], float],
        wait: Callable[[Event, float], bool],
        heavy: HeavySlots,
        emit: Callable[[RunEvent], None],
        nudge: Callable[[], None],
    ) -> None:
        self._port = port
        self._policy = policy
        self._capacity = capacity
        self._clock = clock
        self._wait = wait
        self._heavy = heavy
        self._emit = emit
        self._nudge = nudge
        self._state = initial_pressure(capacity)
        # Sampler-thread state: whether a reading was ever assessed and how
        # many reads in a row have failed since the last measured one.
        self._measured = False
        self._failures = 0
        self._view = _PressureView(PressureLevel.NORMAL, capacity, 0)
        self._seconds = {level: 0.0 for level in PressureLevel}
        self._since: float | None = None
        self._guard = Lock()
        self._stop = Event()
        self._thread: Thread | None = None

    def view(self) -> _PressureView:
        with self._guard:
            return self._view

    def level_seconds(self, now: float) -> dict[PressureLevel, float] | None:
        """Seconds at each level from the first assessed reading to ``now``,
        or None when no reading was assessed."""

        with self._guard:
            seconds = dict(self._seconds)
            level, since = self._view.level, self._since
        if since is None:
            return None
        seconds[level] += max(0.0, now - since)
        return seconds

    def start(self) -> None:
        thread = Thread(target=self._run, name="autobuild-pressure-monitor", daemon=True)
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        """Stop sampling and lift the heavy-command hold, so slots shared past
        this campaign never inherit it."""

        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self._heavy.withhold(False)

    def _run(self) -> None:
        while True:
            started = self._clock()
            try:
                reading = self._port.read(self._policy.read_seconds)
            except Exception as exc:
                reading = HostReading.unmeasured(
                    f"host reading failed: {type(exc).__name__}: {exc}"
                )
            try:
                if not self._observe(reading):
                    return
            except Exception:
                # A fault while publishing one reading must not end pressure
                # control: the next sample tries again.
                pass
            elapsed = self._clock() - started
            if self._wait(self._stop, max(0.0, self._policy.sample_seconds - elapsed)):
                return

    def _say(self, event: RunEvent) -> None:
        """Record one monitor event. A record that cannot be written is
        dropped; it never ends the monitor."""

        try:
            self._emit(event)
        except Exception:
            pass

    def _observe(self, reading: HostReading) -> bool:
        """Assess one reading. Returns False only when the monitor ends."""

        unmeasured = [
            name
            for name in ("cpu_busy_percent", "memory_available_bytes", "memory_total_bytes")
            if getattr(reading, name) is None
        ]
        if unmeasured:
            return self._bad_read(unmeasured, reading.diagnostics)
        self._failures = 0
        self._measured = True
        now = self._clock()
        previous = self._state
        state = assess_pressure(previous, now, reading, self._policy)
        self._state = state
        with self._guard:
            if self._since is not None:
                self._seconds[previous.level] += max(0.0, now - self._since)
            self._since = now
            self._view = _PressureView(state.level, state.cap, state.episode)
        self._heavy.withhold(state.level.holds_heavy, at=now)
        if state.level is not previous.level:
            self._say(
                RunEvent(
                    event_type="campaign.pressure_changed",
                    payload={
                        "level": state.level.value,
                        "previous": previous.level.value,
                        "cpu_busy_percent": _rounded(state.cpu_mean_percent, 1),
                        "memory_available_gib": _rounded(
                            bytes_to_gib(state.memory_available_bytes), 2
                        ),
                        "memory_high_line_gib": _rounded(
                            None
                            if state.high_line_bytes is None
                            else state.high_line_bytes / GIB,
                            2,
                        ),
                        "cap": state.cap,
                    },
                )
            )
        if state.cap != previous.cap:
            self._say(
                RunEvent(
                    event_type="campaign.cap_changed",
                    payload={
                        "cap": state.cap,
                        "previous": previous.cap,
                        "capacity": self._capacity,
                        "level": state.level.value,
                    },
                )
            )
        self._nudge()
        return True

    def _bad_read(self, unmeasured: list[str], diagnostics: tuple[str, ...]) -> bool:
        """Handle a reading without CPU busy or memory, or a failed read.

        Before any measured reading the host is taken as unmeasurable: one
        warning, and the monitor ends at the static capacity it started with.
        After one, the sample is skipped and the level, cap, heavy hold and
        window stay as they were. The first failure of a run of failures
        records one warning; the third in a row records one fallback event
        and the monitor keeps the current values, never raising the cap or
        lifting a hold: only measured readings move them again."""

        if not self._measured:
            self._say(
                RunEvent(
                    event_type="campaign.pressure_unmeasured",
                    payload={
                        "unmeasured": unmeasured,
                        "diagnostics": list(diagnostics),
                        "concurrency": self._capacity,
                    },
                )
            )
            self._nudge()
            return False
        self._failures += 1
        if self._failures == 1:
            self._say(
                RunEvent(
                    event_type="campaign.pressure_read_failed",
                    payload={
                        "consecutive": self._failures,
                        "unmeasured": unmeasured,
                        "diagnostics": list(diagnostics),
                    },
                )
            )
        if self._failures == _PRESSURE_FAILURES_FALLBACK:
            view = self.view()
            self._say(
                RunEvent(
                    event_type="campaign.pressure_fallback",
                    payload={
                        "consecutive": self._failures,
                        "level": view.level.value,
                        "cap": view.cap,
                        "capacity": self._capacity,
                    },
                )
            )
        return True


def _rounded(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


@dataclass(frozen=True, slots=True)
class _BatchResult:
    """One finished batch validation of the delivery target head."""

    number: int
    revision: str
    commits: tuple[str, ...]
    deliveries: int
    final: bool
    passed: bool
    exit_code: int | None = None
    stdout_ref: str = ""
    patient: bool = False
    error: str = ""


@dataclass(slots=True)
class _BatchState:
    """Batch validation state. The scheduler thread alone reads and writes it,
    except ``done``, which a batch thread appends to under ``guard``; the lock
    covers only that list."""

    policy: BatchValidationPolicy
    branch: str | None
    repository: Path
    # Deliveries since the last batch was triggered, in all, and the merged
    # commits delivered since the last batch that passed.
    since: int = 0
    deliveries: int = 0
    unproven: list[str] = field(default_factory=list)
    runs: int = 0
    running: Thread | None = None
    # A trigger that arrived while a batch ran; it starts when that one ends.
    pending: bool = False
    # The head whose batch failed, until a later batch passes.
    failed_revision: str | None = None
    done: list[_BatchResult] = field(default_factory=list)
    guard: Lock = field(default_factory=Lock)

    def post(self, result: _BatchResult) -> None:
        with self.guard:
            self.done.append(result)

    def take(self) -> list[_BatchResult]:
        with self.guard:
            taken, self.done = self.done, []
        return taken


@dataclass(slots=True)
class _Schedule:
    """The admission state of one campaign. Only the scheduler thread reads or
    writes it, so it needs no lock; workers report through ``_Completions``."""

    resumes: list[_ResumeCandidate]
    statuses: dict[Path, WorktreeStatus]
    orphan_roots: list[Path]
    outcomes: list[ItemOutcome] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    parked_signatures: dict[str, str] = field(default_factory=dict)
    taken: set[str] = field(default_factory=set)
    # Items that failed before their claim; they stay ready in the tracker
    # and are not picked again this campaign.
    failed: set[str] = field(default_factory=set)
    running: dict[str, _Flight] = field(default_factory=dict)
    # The hold on admissions in force, the holds that ended, and the timer
    # that wakes the scheduler for the next probe.
    hold: _Hold | None = None
    holds: list[_HoldRecord] = field(default_factory=list)
    hold_timer: Thread | None = None
    hold_deadline: float | None = None
    hold_limit: float = 21600.0
    # Ready items the tracker passed over, with the reason, recorded once.
    skipped: dict[str, str] = field(default_factory=dict)
    stop: CampaignStopReason | None = None
    orphans_reported: bool = False
    draining: bool = False
    last_wait: tuple[object, ...] | None = None
    peak: int = 0
    first_start: float | None = None
    last_finish: float | None = None
    item_seconds: float = 0.0
    # The effective item cap of the latest admission decision, True when that
    # decision held back an admissible item for host pressure or the tracker
    # lease, and the pressure episode a ``pressure`` wait was last recorded for.
    cap: int = 1
    held: bool = False
    pressure_episode: int = _NO_PRESSURE_EPISODE
    monitor: _PressureMonitor | None = None
    lease: _TrackerLease | None = None
    heavy: HeavySlots | None = None
    batch: _BatchState | None = None

    def pressure_view(self, capacity: int) -> _PressureView:
        if self.monitor is None:
            return _PressureView(PressureLevel.NORMAL, capacity, 0)
        return self.monitor.view()

    def observe_stop(self, reason: CampaignStopReason) -> bool:
        """Record the first stop observed; a later one never replaces it."""

        if self.stop is not None:
            return False
        self.stop = reason
        return True

    def skip(self) -> frozenset[str]:
        return (
            frozenset(self.parked_signatures)
            | frozenset(self.taken)
            | frozenset(self.failed)
            | frozenset(self.running)
        )

    @property
    def makespan(self) -> float:
        if self.first_start is None or self.last_finish is None:
            return 0.0
        return max(0.0, self.last_finish - self.first_start)


class CampaignRunner:
    def __init__(self, ports: WorkflowPorts, items: ItemWorkflow | None = None) -> None:
        self._ports = ports
        self._unpublished = UnpublishedWrites()
        self._unfinished = UnfinishedItems()
        self._items = items or ItemWorkflow(ports, self._unpublished, self._unfinished)
        self._completions: _Completions | None = None
        # Set when the campaign ends, so no hold timer outlives it.
        self._hold_stop = Event()

    def run(
        self,
        campaign: CampaignRef,
        spec_for: Callable[[WorkItem], ItemExecutionSpec],
        refill: RefillPlan = RefillPlan(),
        *,
        context: CampaignContext | None = None,
    ) -> CampaignOutcome:
        context = context or self._default_context(campaign)
        record = self._ports.records.create(campaign)
        self._ports.progress.begin(record)
        self._append(
            record,
            RunEvent(
                event_type="campaign.started",
                payload={
                    "harness": context.harness,
                    "models": dict(context.models),
                    "efforts": {
                        seat: effort.value if effort is not None else None
                        for seat, effort in context.efforts.items()
                    },
                    "item_bound": campaign.max_items,
                    "delivery_mode": context.delivery_mode.value,
                    "validator_id": context.validator_id,
                    "manifest_path": str(record.root / "manifest.json"),
                },
            ),
        )
        tracker_grant, reclaims = self._acquire_tracker_lease(campaign, context, record)
        lease = _TrackerLease(tracker_grant)
        completions = _Completions()
        self._completions = completions
        self._hold_stop = Event()
        heartbeat_stop = Event()
        heartbeat: Thread | None = None
        monitor: _PressureMonitor | None = None
        try:
            heartbeat = self._start_heartbeat(lease, heartbeat_stop, context, record, completions)
            # Fresh heavy slots for every campaign, so a reused item workflow
            # never inherits another campaign's limit or hold.
            heavy = HeavySlots(campaign.heavy_concurrency if campaign.concurrency > 1 else None)
            bind = getattr(self._items, "use_heavy_slots", None)
            if bind is not None:
                bind(heavy)
            if campaign.concurrency > 1:
                monitor = self._start_monitor(campaign, context, record, heavy, completions)
            schedule = self._schedule(
                campaign, spec_for, refill, record, context, lease, completions, monitor, heavy
            )
            # The last batch validation runs before the monitor stops, so it
            # still sees the pressure level it starts at.
            self._finish_batches(schedule, record)
            # Every item has ended, so nothing is left to admit: stop sampling
            # before the report, so no pressure event follows the campaign's end.
            if monitor is not None:
                monitor.stop()
            if schedule.hold is not None:
                self._end_hold(record, schedule, "campaign ended")
            self._record_skips(record, schedule)
            stop = schedule.stop or CampaignStopReason.QUEUE_DRY
            outcomes = schedule.outcomes
            follow_ups = schedule.follow_ups
            orphans = [(root.name, "resume:orphan-worktree") for root in schedule.orphan_roots]

            next_ref = self._next_ready(
                campaign,
                stop,
                frozenset(schedule.parked_signatures) | frozenset(schedule.taken),
            )
            unbuilt = self._allowed_unbuilt(campaign, outcomes, stop)
            relative_report = f"docs/campaigns/{campaign.campaign_id}.md"
            repository_report_ref = self._deliver_report(
                campaign,
                context,
                outcomes,
                stop,
                next_ref,
                follow_ups,
                unbuilt,
                reclaims,
                orphans,
                record,
                relative_report,
                schedule,
            )
            counts = self._counts(outcomes)
            self._append(
                record,
                RunEvent(
                    event_type="campaign.completed",
                    payload={
                        "stop_reason": stop.value,
                        "accepted": counts[ItemDisposition.ACCEPTED],
                        "parked": counts[ItemDisposition.PARKED],
                        "failed": counts[ItemDisposition.FAILED],
                        "report": relative_report,
                        "report_path": repository_report_ref,
                    },
                ),
            )
            summary = (
                f"campaign={campaign.campaign_id}; items={len(outcomes)}; "
                f"stop={stop.value}; report={repository_report_ref}"
            )
            report = self._ports.records.complete(record, summary)
            return CampaignOutcome(
                campaign.campaign_id,
                tuple(outcomes),
                stop,
                report,
                repository_report_ref,
                progress_ref=str(record.root / "progress.log"),
                peak_concurrency=schedule.peak,
                makespan_seconds=schedule.makespan,
                item_seconds=schedule.item_seconds,
            )
        finally:
            self._hold_stop.set()
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join()
            if monitor is not None:
                monitor.stop()
            self._release_tracker_lease(lease.get(), record)

    def scheduler_idle(self) -> bool:
        """True while the running campaign's scheduler waits with no completion
        or nudge to take, so a test can move a fake clock on only once every
        decision for the previous instant is made."""

        completions = self._completions
        return completions is not None and completions.idle()

    def _start_monitor(
        self,
        campaign: CampaignRef,
        context: CampaignContext,
        record: RunRecordRef,
        heavy: HeavySlots,
        completions: _Completions,
    ) -> _PressureMonitor | None:
        """Start the host pressure monitor when the host port is wired."""

        if self._ports.host is None:
            return None
        monitor = _PressureMonitor(
            self._ports.host,
            context.pressure or PressurePolicy(),
            campaign.concurrency,
            self._ports.schedule_clock,
            self._ports.pressure_wait,
            heavy,
            lambda event: self._append(record, event),
            completions.nudge,
        )
        monitor.start()
        return monitor

    # -- scheduling ------------------------------------------------------------

    def _schedule(
        self,
        campaign: CampaignRef,
        spec_for: Callable[[WorkItem], ItemExecutionSpec],
        refill: RefillPlan,
        record: RunRecordRef,
        context: CampaignContext,
        lease: _TrackerLease,
        completions: _Completions,
        monitor: _PressureMonitor | None = None,
        heavy: HeavySlots | None = None,
    ) -> _Schedule:
        """Admit items until a stop, the item bound or a dry queue, then drain.

        Each admitted item runs on its own worker thread. The scheduler admits
        while an admission is possible, then waits for the next completion.
        After every completion (a delivery, a park or a failure) it records the
        outcome and admits again, so dependants unlock as their blockers merge.
        A stop withholds admissions and waits for every running item to reach
        its own terminal state. An error in the scheduler itself is raised only
        after the running items have ended. With a concurrency of one the
        scheduler admits one item after each completion.

        The pressure monitor and the lease heartbeat nudge the scheduler to
        decide again. A decision held back for pressure with nothing running
        waits for the next nudge rather than ending the campaign."""

        schedule = self._scan_resumes(campaign, record, context)
        schedule.monitor = monitor
        schedule.lease = lease
        schedule.cap = campaign.concurrency
        schedule.hold_limit = context.hold_limit_seconds
        schedule.heavy = heavy if heavy is not None else HeavySlots()
        if context.batch is not None:
            branch = (
                context.target_branch or None
                if context.delivery_mode is DeliveryMode.CURRENT_BRANCH_PR
                else None
            )
            schedule.batch = _BatchState(context.batch, branch, campaign.repository)
        failure: BaseException | None = None
        while True:
            try:
                self._absorb_batches(campaign, schedule, record)
            except BaseException as exc:  # drain before raising, never abandon a worker
                if failure is None:
                    failure = exc
            error = self._absorb_all(campaign, schedule, completions.take(), record, lease)
            if failure is None:
                failure = error
            try:
                if schedule.stop is None and failure is None:
                    if self._admit_next(
                        campaign, spec_for, refill, record, context, schedule, completions
                    ):
                        continue
            except BaseException as exc:  # drain before raising, never abandon a worker
                if failure is None:
                    failure = exc
            if not schedule.running:
                if schedule.stop is None and failure is None and schedule.held:
                    error = self._absorb_all(campaign, schedule, completions.wait(), record, lease)
                    if failure is None:
                        failure = error
                    continue
                if schedule.stop is None and failure is None:
                    failure = RuntimeError("the scheduler held an admission with nothing running")
                break
            if (schedule.stop is not None or failure is not None) and not schedule.draining:
                schedule.draining = True
                self._announce(
                    campaign,
                    record,
                    RunEvent(
                        event_type="campaign.draining",
                        payload={
                            "stop_reason": schedule.stop.value if schedule.stop else "error",
                            "running": len(schedule.running),
                        },
                    ),
                )
            error = self._absorb_all(campaign, schedule, completions.wait(), record, lease)
            if failure is None:
                failure = error
        if not schedule.orphans_reported:
            self._report_orphans(record, schedule)
        if failure is not None:
            if schedule.batch is not None and schedule.batch.running is not None:
                schedule.batch.running.join()
            raise failure
        return schedule

    def _admit_next(
        self,
        campaign: CampaignRef,
        spec_for: Callable[[WorkItem], ItemExecutionSpec],
        refill: RefillPlan,
        record: RunRecordRef,
        context: CampaignContext,
        schedule: _Schedule,
        completions: _Completions,
    ) -> bool:
        """Make one admission decision. Returns True when it admitted an item
        or recorded an outcome, so the caller decides again at once; False when
        the scheduler must wait for a completion or a nudge. With nothing
        running a False comes with a stop reason, so the campaign ends, or with
        ``held`` set for host pressure, so the scheduler waits for the monitor.

        The effective cap is the smaller of the concurrency and the pressure
        monitor's cap. A level other than ``normal`` admits nothing; the first
        time it holds back an admissible item while a slot is free, it records
        one ``pressure`` wait for that episode.

        A hold on admissions (lanes exhausted, a pending tracker write or an
        unreachable tracker) admits nothing until its probe clears; running
        items go on. A hold longer than ``hold_limit_seconds`` ends the
        campaign."""

        schedule.held = False
        if (
            schedule.hold is None
            and schedule.batch is not None
            and schedule.batch.failed_revision is not None
        ):
            # A batch failure found while another hold was in force holds now.
            self._begin_hold(
                record,
                schedule,
                HoldCondition.BATCH_VALIDATION_FAILED,
                f"validator failed on {schedule.batch.failed_revision}",
            )
        if (
            schedule.hold is not None
            and len(schedule.outcomes) + len(schedule.running) < campaign.max_items
            and not self._hold_released(campaign, record, schedule)
        ):
            return False
        view = schedule.pressure_view(campaign.concurrency)
        cap = min(campaign.concurrency, view.cap)
        schedule.cap = cap
        if schedule.resumes:
            if len(schedule.running) >= cap:
                self._waiting(campaign, record, schedule, "cap")
                return False
            if self._held_back(campaign, record, schedule, view):
                return False
            candidate = schedule.resumes.pop(0)
            self._admit_resume(campaign, spec_for, record, context, schedule, completions, candidate)
            return True
        if not schedule.orphans_reported:
            self._report_orphans(record, schedule)
        if len(schedule.outcomes) + len(schedule.running) >= campaign.max_items:
            if not schedule.running:
                schedule.observe_stop(CampaignStopReason.ITEM_BOUND)
            return False
        at_warning = campaign.concurrency > 1 and self._usage_at_warning()
        limit = admission_limit(cap, at_warning)
        if len(schedule.running) >= limit:
            reason = "usage" if limit < cap else "cap"
            self._waiting(campaign, record, schedule, reason)
            return False
        if schedule.lease is not None and schedule.lease.withheld():
            if schedule.running:
                schedule.held = True
                self._waiting(campaign, record, schedule, "lease")
                return False
            # Nothing runs: renew here. A renewal that fails holds admissions
            # until a renewal succeeds, within the hold limit.
            renew_error = self._renew_between_items(schedule.lease, record)
            if renew_error is not None:
                self._begin_hold(
                    record, schedule, HoldCondition.LEASE_RENEW_FAILED, str(renew_error)
                )
                return self._hold_released(campaign, record, schedule)
        if not view.level.admits and schedule.pressure_episode == view.episode:
            # This episode's wait is already recorded and nothing can be
            # admitted until the level changes: skip reading the tracker.
            schedule.held = True
            return False
        if not self._lanes_available():
            self._append(
                record,
                RunEvent(
                    event_type="campaign.lanes_exhausted",
                    payload={"lanes": [lane.name for lane in self._ports.lanes]},
                ),
            )
            self._begin_hold(record, schedule, HoldCondition.LANES_EXHAUSTED, "")
            # Deciding the new hold arms the timer for its first probe.
            return self._hold_released(campaign, record, schedule)
        held = frozenset().union(*(flight.paths for flight in schedule.running.values()))
        prepared: dict[str, ItemExecutionSpec | None] = {}
        overlapped: list[str] = []

        def admits(candidate: WorkItem) -> bool:
            if not held:
                return True
            spec = self._prepare(spec_for, candidate)
            prepared[candidate.item_id] = spec
            if spec is not None and share_declared_path(
                exact_declared_paths(spec.brief_text), held
            ):
                overlapped.append(candidate.item_id)
                return False
            return True

        try:
            item = self._select_item(
                campaign, schedule.skip(), admits, peek=not schedule.running
            )
        except ScopeFenceViolation as exc:
            # The out-of-fence item is never claimed and no allowed item is
            # ready, so for this campaign the fenced queue is dry.
            self._append(
                record,
                RunEvent(
                    event_type="campaign.scope_fence_violation",
                    payload={"error": str(exc)},
                ),
            )
            item = None
        except Exception as exc:
            # The queue could not be read: hold admissions and read it again.
            self._begin_hold(
                record,
                schedule,
                HoldCondition.TRACKER_READ_FAILED,
                f"{type(exc).__name__}: {exc}",
            )
            return self._hold_released(campaign, record, schedule)
        self._record_skips(record, schedule)
        if item is None:
            if schedule.running:
                if overlapped:
                    self._waiting(campaign, record, schedule, "path_overlap", overlapped)
                return False
            try:
                self._refill(campaign, refill, record, schedule)
            except Exception as exc:
                self._append(
                    record,
                    RunEvent(event_type="campaign.refill_failed", payload={"error": str(exc)}),
                )
            schedule.observe_stop(CampaignStopReason.QUEUE_DRY)
            return False
        if self._held_back(campaign, record, schedule, view):
            return False
        self._start(
            campaign,
            item,
            spec_for,
            record,
            schedule,
            completions,
            spec=prepared.get(item.item_id),
        )
        return True

    def _admit_resume(
        self,
        campaign: CampaignRef,
        spec_for: Callable[[WorkItem], ItemExecutionSpec],
        record: RunRecordRef,
        context: CampaignContext,
        schedule: _Schedule,
        completions: _Completions,
        candidate: _ResumeCandidate,
    ) -> None:
        """Resume one interrupted claim from its marker on a worker, or park it
        at once when it is not in a good state to resume."""

        item, marker = candidate.item, candidate.marker
        assessment = self._assess_resume(campaign, marker, schedule.statuses, context)
        if not assessment.good:
            schedule.outcomes.append(
                self._park_resume(
                    campaign, item, marker, record, assessment.reason, schedule.statuses
                )
            )
            return
        assert marker is not None
        self._append(
            record,
            RunEvent(
                event_type="item.resume_selected",
                item_id=item.item_id,
                payload={
                    "prior_run_id": marker.run_id,
                    "marker_state": marker.state,
                    "correction_count": marker.correction_count,
                },
            ),
        )
        plan = ResumePlan(marker.state, marker.worktree_root, marker.correction_count, marker.run_id)
        self._start(campaign, item, spec_for, record, schedule, completions, resume=plan)

    def _start(
        self,
        campaign: CampaignRef,
        item: WorkItem,
        spec_for: Callable[[WorkItem], ItemExecutionSpec],
        record: RunRecordRef,
        schedule: _Schedule,
        completions: _Completions,
        *,
        resume: ResumePlan | None = None,
        spec: ItemExecutionSpec | None = None,
    ) -> None:
        """Start one item on its own worker thread.

        With a concurrency above one the item's spec is prepared here, so its
        declared exact paths hold back an overlapping item for as long as it
        runs. A serial campaign prepares the spec on the worker."""

        paths: frozenset[str] = frozenset()
        if campaign.concurrency > 1:
            if spec is None:
                spec = self._prepare(spec_for, item)
            if spec is not None:
                paths = exact_declared_paths(spec.brief_text)
        worker = Thread(
            target=self._work,
            args=(campaign, item, spec_for, spec, resume, record, completions),
            name=f"autobuild-item-{item.item_id}",
        )
        started = self._ports.schedule_clock()
        schedule.running[item.item_id] = _Flight(item, paths, worker, started)
        schedule.peak = max(schedule.peak, len(schedule.running))
        if schedule.first_start is None:
            schedule.first_start = started
        schedule.last_wait = None
        try:
            self._announce(
                campaign,
                record,
                RunEvent(
                    event_type="item.admitted",
                    item_id=item.item_id,
                    payload={
                        "running": len(schedule.running),
                        "concurrency": schedule.cap,
                        "resume": resume is not None,
                    },
                ),
            )
        finally:
            # A flight in the running set always has a started worker, so the
            # scheduler never waits for a completion that cannot arrive. A
            # worker that cannot start has claimed nothing and holds no
            # worktree or lease, so removing its flight releases everything.
            try:
                worker.start()
            except BaseException:
                schedule.running.pop(item.item_id, None)
                raise

    def _work(
        self,
        campaign: CampaignRef,
        item: WorkItem,
        spec_for: Callable[[WorkItem], ItemExecutionSpec],
        spec: ItemExecutionSpec | None,
        resume: ResumePlan | None,
        record: RunRecordRef,
        completions: _Completions,
    ) -> None:
        """Run one item and always report its completion, so no failure leaks
        a slot. A claim taken by another writer is reported as taken; any other
        error is a structural failure of the item."""

        outcome: ItemOutcome | None = None
        taken = ""
        try:
            prepared = spec if spec is not None else spec_for(item)
            if resume is None:
                outcome = self._items.run(campaign, prepared, record)
            else:
                outcome = self._items.run(campaign, prepared, record, resume=resume)
        except ClaimTaken as exc:
            if resume is None:
                taken = str(exc) or "claim taken"
            else:
                outcome = self._worker_failure(item, resume, exc)
        except BaseException as exc:
            outcome = self._worker_failure(item, resume, exc)
        finally:
            completions.post(
                _Completion(item.item_id, outcome, taken, self._ports.schedule_clock())
            )

    @staticmethod
    def _worker_failure(
        item: WorkItem, resume: ResumePlan | None, exc: BaseException
    ) -> ItemOutcome:
        return ItemOutcome(
            item_id=item.item_id,
            disposition=ItemDisposition.FAILED,
            states=(ItemState.READY,) if resume is None else (ItemState.CLAIMED,),
            reason=str(exc),
            structural_failure=True,
            title=item.title,
        )

    @staticmethod
    def _prepare(
        spec_for: Callable[[WorkItem], ItemExecutionSpec], item: WorkItem
    ) -> ItemExecutionSpec | None:
        """The item's spec, or None when it cannot be built; the worker then
        builds it again and records the failure on the item."""

        try:
            return spec_for(item)
        except Exception:
            return None

    def _absorb_all(
        self,
        campaign: CampaignRef,
        schedule: _Schedule,
        batch: list[_Completion],
        record: RunRecordRef,
        lease: _TrackerLease,
    ) -> BaseException | None:
        """Record every completion of a batch, each on its own, and return the
        first error. A failure while recording one never keeps a later one in
        the running set, so the scheduler never waits for an item that ended."""

        first: BaseException | None = None
        for done in batch:
            try:
                self._absorb(campaign, schedule, done, record, lease)
            except BaseException as exc:  # recorded, raised after the drain
                if first is None:
                    first = exc
            finally:
                schedule.running.pop(done.item_id, None)
        return first

    def _absorb(
        self,
        campaign: CampaignRef,
        schedule: _Schedule,
        done: _Completion,
        record: RunRecordRef,
        lease: _TrackerLease,
    ) -> None:
        """Record one finished item and observe the stop it carries."""

        flight = schedule.running.pop(done.item_id)
        flight.worker.join()
        schedule.last_finish = done.finished
        schedule.item_seconds += max(0.0, done.finished - flight.started)
        schedule.last_wait = None
        outcome = done.outcome
        if outcome is None:
            schedule.taken.add(done.item_id)
            self._append(
                record,
                RunEvent(
                    event_type="item.claim_taken",
                    item_id=done.item_id,
                    payload={"error": done.taken},
                ),
            )
            return
        schedule.outcomes.append(outcome)
        schedule.follow_ups.extend(outcome.follow_ups)
        self._note_delivery(schedule, record, outcome)
        if outcome.lane_signature is not None:
            schedule.parked_signatures[outcome.item_id] = outcome.lane_signature
        if (
            outcome.disposition is ItemDisposition.FAILED
            and ItemState.CLAIMED not in outcome.states
        ):
            schedule.failed.add(outcome.item_id)
        self._renew_between_items(lease, record)
        # An item error ends only the item. A condition that blocks all work
        # holds admissions instead of stopping the campaign.
        if outcome.lanes_exhausted:
            self._begin_hold(record, schedule, HoldCondition.LANES_EXHAUSTED, "")
        elif outcome.tracker_stop is not None:
            self._append(
                record,
                RunEvent(
                    event_type="item.tracker_failed",
                    item_id=outcome.item_id,
                    payload={
                        "kind": outcome.tracker_stop.value,
                        "exit_code": outcome.tracker_exit_code,
                        "error": outcome.reason,
                    },
                ),
            )
            recovery = self._recover_tracker()
            if not recovery.clear:
                condition = recovery.condition or (
                    HoldCondition.TRACKER_UNREACHABLE
                    if outcome.tracker_stop is CampaignStopReason.TRACKER_ENVIRONMENT
                    else HoldCondition.TRACKER_PENDING
                )
                self._begin_hold(
                    record, schedule, condition, recovery.detail or outcome.reason or ""
                )

    # -- batch validation -------------------------------------------------------

    def _note_delivery(
        self, schedule: _Schedule, record: RunRecordRef, outcome: ItemOutcome
    ) -> None:
        """Count one delivered item; every ``every`` deliveries trigger a batch
        validation of the target head. A trigger while a batch runs waits for
        it to end."""

        batch = schedule.batch
        if (
            batch is None
            or outcome.disposition is not ItemDisposition.ACCEPTED
            or outcome.finalise is None
        ):
            return
        batch.deliveries += 1
        batch.since += 1
        if outcome.finalise.merged_commit:
            batch.unproven.append(outcome.finalise.merged_commit)
        if batch.since < batch.policy.every:
            return
        batch.since = 0
        if batch.running is not None:
            batch.pending = True
            return
        self._start_batch(schedule, record)

    def _start_batch(
        self,
        schedule: _Schedule,
        record: RunRecordRef,
        checkout=None,
        final: bool = False,
    ) -> None:
        """Open a detached checkout of the target head, unless the caller has
        one, and validate it on its own thread. The scheduler goes on; the
        result arrives through the batch state and a nudge. A final batch runs
        on the calling thread."""

        batch = schedule.batch
        assert batch is not None
        batch.runs += 1
        number = batch.runs
        commits = tuple(batch.unproven)
        deliveries = batch.deliveries
        if checkout is None:
            try:
                checkout = self._ports.workspace.open_validation_checkout(
                    batch.repository, batch.branch
                )
            except Exception as exc:
                batch.post(
                    _BatchResult(
                        number, "", commits, deliveries, final, False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                return

        def validate() -> None:
            batch.post(
                self._batch_validate(schedule, record, checkout, number, commits, deliveries, final)
            )
            completions = self._completions
            if completions is not None:
                completions.nudge()

        if final:
            validate()
            return
        worker = Thread(target=validate, name=f"autobuild-batch-{number}", daemon=True)
        batch.running = worker
        worker.start()

    def _batch_validate(
        self,
        schedule: _Schedule,
        record: RunRecordRef,
        checkout,
        number: int,
        commits: tuple[str, ...],
        deliveries: int,
        final: bool,
    ) -> _BatchResult:
        """Run the validator once in ``checkout`` in a heavy slot, then remove
        the checkout. A command that raises is a failed batch."""

        batch = schedule.batch
        assert batch is not None and schedule.heavy is not None
        policy = batch.policy
        request = CommandRequest(
            command_id=f"{record.run_id}:batch:{number}",
            argv=policy.argv,
            cwd=checkout.root,
            environment=self._ports.item_environment(f"batch-{number}"),
            timeout_seconds=policy.timeout_seconds,
        )
        try:
            command, _pressured, patient = run_heavy(
                schedule.heavy,
                self._ports.command,
                request,
                self._ports.schedule_clock,
                policy.patient_argv,
            )
        except Exception as exc:
            return _BatchResult(
                number, checkout.revision, commits, deliveries, final, False,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            try:
                self._ports.workspace.close_validation_checkout(checkout)
            except Exception:
                pass
        passed = command.exit_code == 0 and not command.timed_out and not command.cancelled
        return _BatchResult(
            number, checkout.revision, commits, deliveries, final, passed,
            command.exit_code, command.stdout_ref, patient,
        )

    def _absorb_batches(
        self, campaign: CampaignRef, schedule: _Schedule, record: RunRecordRef, at_end: bool = False
    ) -> None:
        """Record every finished batch. A pass proves the commits it covered
        and releases a batch-failure hold; a failure records
        ``campaign.batch_validation_failed`` and holds admissions. Nothing is
        reverted and the campaign goes on."""

        batch = schedule.batch
        if batch is None:
            return
        results = batch.take()
        if not results:
            return
        if batch.running is not None:
            # Only one batch runs at a time and it posts as its last step.
            batch.running.join()
            batch.running = None
        for result in results:
            payload: dict[str, object] = {
                "batch": result.number,
                "revision": result.revision,
                "deliveries": result.deliveries,
                "commits": list(result.commits),
                "final": result.final,
                "exit_code": result.exit_code,
                "stdout_ref": result.stdout_ref,
            }
            if result.patient:
                payload["patient"] = True
            if result.error:
                payload["error"] = result.error
            if result.passed:
                proven = set(result.commits)
                batch.unproven = [commit for commit in batch.unproven if commit not in proven]
                batch.failed_revision = None
                self._append(record, RunEvent(event_type="campaign.batch_validated", payload=payload))
                if (
                    schedule.hold is not None
                    and schedule.hold.condition is HoldCondition.BATCH_VALIDATION_FAILED
                ):
                    self._end_hold(record, schedule, "released")
                continue
            batch.failed_revision = result.revision or batch.failed_revision or "unknown"
            self._append(
                record, RunEvent(event_type="campaign.batch_validation_failed", payload=payload)
            )
            if not at_end:
                self._begin_hold(
                    record,
                    schedule,
                    HoldCondition.BATCH_VALIDATION_FAILED,
                    f"validator failed on {result.revision or 'the target head'}",
                )
        if batch.running is None and batch.pending and not at_end:
            batch.pending = False
            self._start_batch(schedule, record)

    def _finish_batches(self, schedule: _Schedule, record: RunRecordRef) -> None:
        """At campaign end: wait for a running batch, then validate the target
        head once more when anything was delivered since the last trigger."""

        batch = schedule.batch
        if batch is None:
            return
        if batch.running is not None:
            batch.running.join()
        self._absorb_batches(None, schedule, record, at_end=True)
        if batch.since > 0 or batch.pending:
            batch.since = 0
            batch.pending = False
            self._start_batch(schedule, record, final=True)
            self._absorb_batches(None, schedule, record, at_end=True)

    def _probe_batch_hold(self, schedule: _Schedule, record: RunRecordRef) -> TrackerRecovery:
        """The batch-failure hold's probe: when the target head has moved since
        the failing batch, for example after an operator landed a fix, start a
        batch on it. Only a batch that passes releases the hold."""

        condition = HoldCondition.BATCH_VALIDATION_FAILED
        batch = schedule.batch
        if batch is None or batch.failed_revision is None:
            return TrackerRecovery()
        if batch.running is not None:
            return TrackerRecovery(False, condition, "a batch validation is running")
        try:
            # The probe fetches, so a fix pushed to the remote is seen even
            # with nothing in flight to bring it in.
            checkout = self._ports.workspace.open_validation_checkout(
                batch.repository, batch.branch, refresh=True
            )
        except Exception as exc:
            return TrackerRecovery(False, condition, f"{type(exc).__name__}: {exc}")
        if checkout.revision == batch.failed_revision:
            try:
                self._ports.workspace.close_validation_checkout(checkout)
            except Exception:
                pass
            return TrackerRecovery(
                False, condition, f"the target head is still {checkout.revision}"
            )
        self._start_batch(schedule, record, checkout=checkout)
        return TrackerRecovery(False, condition, f"validating the moved head {checkout.revision}")

    def _refill(
        self,
        campaign: CampaignRef,
        refill: RefillPlan,
        record: RunRecordRef,
        schedule: _Schedule,
    ) -> None:
        if not campaign.refill_enabled:
            return
        for proposal in refill.proposals:
            ref = self._ports.tracker.propose(proposal, actor=COORDINATOR_ACTOR)
            if ref.runnable:
                raise ValueError("refill made a workflow proposal runnable")
            if not ref.published:
                self._unpublished.add(f"{ref.proposal_id} proposal")
                self._append(
                    record,
                    unpublished_write(ref.proposal_id, "proposal", ref.publication_note),
                )
            schedule.follow_ups.append(proposal.title)
        for fog in refill.fog:
            self._ports.knowledge.record_fog(fog)

    def _waiting(
        self,
        campaign: CampaignRef,
        record: RunRecordRef,
        schedule: _Schedule,
        reason: str,
        items: list[str] | None = None,
    ) -> None:
        """Record why the scheduler holds back, once per distinct state. The
        ``concurrency`` it reports is the effective cap."""

        key = (reason, len(schedule.running), tuple(items or ()), schedule.cap)
        if key == schedule.last_wait:
            return
        schedule.last_wait = key
        self._announce(
            campaign,
            record,
            RunEvent(
                event_type="campaign.waiting",
                payload={
                    "reason": reason,
                    "running": len(schedule.running),
                    "concurrency": schedule.cap,
                    "items": list(items or ()),
                },
            ),
        )

    def _held_back(
        self,
        campaign: CampaignRef,
        record: RunRecordRef,
        schedule: _Schedule,
        view: _PressureView,
    ) -> bool:
        """True when the pressure level withholds an admission a free slot
        and an admissible item would otherwise allow. The first such hold of
        each pressure episode records one ``pressure`` wait, however many
        items are queued."""

        if view.level.admits:
            return False
        schedule.held = True
        if schedule.pressure_episode != view.episode:
            schedule.pressure_episode = view.episode
            schedule.last_wait = None
            self._waiting(campaign, record, schedule, "pressure")
        return True

    # -- holds -------------------------------------------------------------------

    def _begin_hold(
        self,
        record: RunRecordRef,
        schedule: _Schedule,
        condition: HoldCondition,
        detail: str,
    ) -> None:
        """Hold admissions until ``condition`` clears. One hold is in force at
        a time; a second condition found during a hold is probed again once
        the first one clears, by the next item that meets it."""

        if schedule.hold is not None:
            return
        now = self._ports.schedule_clock()
        delay = self._retry_delay(condition, 0)
        schedule.hold = _Hold(condition, now, now + delay, detail)
        self._append(
            record,
            RunEvent(
                event_type="campaign.hold_started",
                payload={
                    "condition": condition.value,
                    "detail": detail,
                    "retry_seconds": delay,
                    "limit_seconds": schedule.hold_limit,
                },
            ),
        )

    def _hold_released(
        self, campaign: CampaignRef, record: RunRecordRef, schedule: _Schedule
    ) -> bool:
        """Decide the hold in force. Returns True once its condition has
        cleared; False while it holds, with the scheduler woken for the next
        probe, or when it reached the hold limit and ended the campaign."""

        hold = schedule.hold
        assert hold is not None
        now = self._ports.schedule_clock()
        limit_at = hold.started + schedule.hold_limit
        if now >= limit_at:
            self._append(
                record,
                RunEvent(
                    event_type="campaign.hold_limit",
                    payload={
                        "condition": hold.condition.value,
                        "seconds": now - hold.started,
                        "limit_seconds": schedule.hold_limit,
                        "detail": hold.detail,
                    },
                ),
            )
            self._end_hold(record, schedule, "hold limit")
            schedule.observe_stop(CampaignStopReason.HOLD_LIMIT)
            return False
        if now >= hold.retry_at:
            recovery = self._probe_hold(campaign, record, schedule, hold.condition)
            if recovery.clear:
                self._end_hold(record, schedule, "released")
                return True
            hold.retries += 1
            hold.condition = recovery.condition or hold.condition
            hold.detail = recovery.detail or hold.detail
            delay = self._retry_delay(hold.condition, hold.retries)
            hold.retry_at = now + delay
            self._append(
                record,
                RunEvent(
                    event_type="campaign.hold_retry",
                    payload={
                        "condition": hold.condition.value,
                        "retry": hold.retries,
                        "retry_seconds": delay,
                        "detail": hold.detail,
                    },
                ),
            )
        schedule.held = True
        self._arm_hold_timer(schedule, min(hold.retry_at, limit_at))
        return False

    def _end_hold(self, record: RunRecordRef, schedule: _Schedule, ended: str) -> None:
        hold = schedule.hold
        assert hold is not None
        seconds = max(0.0, self._ports.schedule_clock() - hold.started)
        schedule.hold = None
        schedule.holds.append(_HoldRecord(hold.condition, seconds, ended))
        if ended == "released":
            self._append(
                record,
                RunEvent(
                    event_type="campaign.hold_released",
                    payload={
                        "condition": hold.condition.value,
                        "seconds": seconds,
                        "retries": hold.retries,
                    },
                ),
            )

    def _probe_hold(
        self,
        campaign: CampaignRef,
        record: RunRecordRef,
        schedule: _Schedule,
        condition: HoldCondition,
    ) -> TrackerRecovery:
        if condition is HoldCondition.LANES_EXHAUSTED:
            return TrackerRecovery(self._lanes_available(), None, "")
        if condition is HoldCondition.BATCH_VALIDATION_FAILED:
            return self._probe_batch_hold(schedule, record)
        if condition is HoldCondition.TRACKER_READ_FAILED:
            # Read the queue the way admission reads it; an answer clears it.
            try:
                self._select_item(campaign, schedule.skip(), None, peek=False)
            except ScopeFenceViolation:
                pass
            except Exception as exc:
                return TrackerRecovery(False, condition, f"{type(exc).__name__}: {exc}")
            return TrackerRecovery()
        if condition is HoldCondition.LEASE_RENEW_FAILED:
            assert schedule.lease is not None
            error = self._renew_between_items(schedule.lease, record)
            if error is not None:
                return TrackerRecovery(False, condition, str(error))
            return TrackerRecovery()
        return self._recover_tracker()

    def _renew_between_items(
        self, lease: _TrackerLease, record: RunRecordRef
    ) -> Exception | None:
        """Renew the tracker lease between items. Returns the error when the
        renewal raised, else None; the failure is counted as a heartbeat
        failure is, so three in a row withhold admissions until a renewal
        succeeds."""

        try:
            grant = self._renew_tracker_lease(lease.get(), record)
        except Exception as exc:
            self._lease_failed(lease, record, exc)
            return exc
        if lease.renewed(grant):
            self._append(record, RunEvent(event_type="campaign.lease_restored", payload={}))
        return None

    def _lease_failed(self, lease: _TrackerLease, record: RunRecordRef, exc: Exception) -> int:
        """Record one failed renewal; the third in a row records the hold."""

        failures = lease.failed()
        self._append(
            record,
            RunEvent(
                event_type="campaign.lease_renew_failed",
                payload={"error": str(exc), "consecutive": failures},
            ),
        )
        if failures == _LEASE_FAILURES_WITHHELD:
            self._append(
                record,
                RunEvent(
                    event_type="campaign.lease_withheld",
                    payload={"failures": failures, "error": str(exc)},
                ),
            )
        return failures

    def _record_skips(self, record: RunRecordRef, schedule: _Schedule) -> None:
        """Record each ready item the tracker passed over, once per campaign."""

        skipped_items = getattr(self._ports.tracker, "skipped_items", None)
        if skipped_items is None:
            return
        try:
            entries = skipped_items()
        except Exception:
            return
        for entry in entries:
            if entry.item_id in schedule.skipped:
                continue
            schedule.skipped[entry.item_id] = entry.reason
            self._append(
                record,
                RunEvent(
                    event_type="item.skipped",
                    item_id=entry.item_id,
                    payload={"reason": entry.reason},
                ),
            )

    def _recover_tracker(self) -> TrackerRecovery:
        """Ask the tracker to finish a partial write and to answer. A tracker
        without the capability is always clear; a failing call is pending."""

        recover = getattr(self._ports.tracker, "recover", None)
        if recover is None:
            return TrackerRecovery()
        try:
            return recover()
        except Exception as exc:
            return TrackerRecovery(False, HoldCondition.TRACKER_PENDING, str(exc))

    def _retry_delay(self, condition: HoldCondition, retries: int) -> float:
        if condition is HoldCondition.TRACKER_PENDING:
            return _PENDING_RETRY_SECONDS
        if condition is HoldCondition.LEASE_RENEW_FAILED:
            return _LEASE_RETRY_SECONDS
        if condition is HoldCondition.BATCH_VALIDATION_FAILED:
            return _BATCH_RETRY_SECONDS
        if condition in {HoldCondition.TRACKER_UNREACHABLE, HoldCondition.TRACKER_READ_FAILED}:
            return _UNREACHABLE_BACKOFF_SECONDS[
                min(retries, len(_UNREACHABLE_BACKOFF_SECONDS) - 1)
            ]
        low, high = _LANES_RETRY_BOUNDS
        return min(high, max(low, self._seconds_to_lane_reset()))

    def _seconds_to_lane_reset(self) -> float:
        """Seconds from now to the earliest cooling reset among the lanes, or
        0 when none names a readable reset."""

        state = self._ports.lane_state
        if state is None:
            return 0.0
        now = datetime.now(UTC)
        waits: list[float] = []
        for lane in self._ports.lanes:
            try:
                cooling = state.active(lane.name)
            except Exception:
                return 0.0
            if cooling is None:
                return 0.0
            try:
                reset = datetime.fromisoformat(cooling.cooled_until)
            except (TypeError, ValueError):
                continue
            if reset.tzinfo is None:
                reset = reset.replace(tzinfo=UTC)
            waits.append((reset - now).total_seconds())
        return min(waits) if waits else 0.0

    def _arm_hold_timer(self, schedule: _Schedule, deadline: float) -> None:
        """Wake the scheduler at ``deadline`` on the schedule clock, unless a
        live timer already waits for it."""

        timer = schedule.hold_timer
        if timer is not None and timer.is_alive() and schedule.hold_deadline == deadline:
            return
        completions = self._completions
        if completions is None:
            return
        seconds = max(0.0, deadline - self._ports.schedule_clock())
        timer = Thread(
            target=self._hold_wake,
            args=(self._hold_stop, seconds, completions),
            name="autobuild-hold-timer",
            daemon=True,
        )
        schedule.hold_timer = timer
        schedule.hold_deadline = deadline
        timer.start()

    def _hold_wake(self, stop: Event, seconds: float, completions: _Completions) -> None:
        if not self._ports.hold_wait(stop, seconds):
            completions.nudge()

    def _announce(self, campaign: CampaignRef, record: RunRecordRef, event: RunEvent) -> None:
        """Append a scheduling event. A serial campaign records none, so its run
        record and progress lines are exactly the serial ones."""

        if campaign.concurrency > 1:
            self._append(record, event)

    def _start_heartbeat(
        self,
        lease: _TrackerLease,
        stop: Event,
        context: CampaignContext,
        record: RunRecordRef,
        completions: _Completions,
    ) -> Thread | None:
        """Renew the tracker lease every ``lease_heartbeat_seconds`` for as long
        as the campaign runs, not only between items."""

        interval = context.lease_heartbeat_seconds
        if lease.get() is None or self._ports.lease is None or not interval or interval <= 0:
            return None
        beat = Thread(
            target=self._heartbeat,
            args=(lease, stop, float(interval), record, completions),
            name="autobuild-lease-heartbeat",
            daemon=True,
        )
        beat.start()
        return beat

    def _heartbeat(
        self,
        lease: _TrackerLease,
        stop: Event,
        interval: float,
        record: RunRecordRef,
        completions: _Completions,
    ) -> None:
        """A failed renewal is retried on the next beat. The third failure in a
        row records one ``campaign.lease_withheld`` event and withholds new
        admissions until a renewal succeeds; running items are never stopped."""

        port = self._ports.lease
        assert port is not None
        while not self._ports.heartbeat_wait(stop, interval):
            grant = lease.get()
            if grant is None:
                return
            try:
                renewed = port.renew(grant)
            except Exception as exc:
                if self._lease_failed(lease, record, exc) == _LEASE_FAILURES_WITHHELD:
                    completions.nudge()
                continue
            if lease.renewed(renewed):
                self._append(record, RunEvent(event_type="campaign.lease_restored", payload={}))
                completions.nudge()

    def _append(self, record: RunRecordRef, event: RunEvent) -> str:
        """Append one run event and emit its owner progress line.

        The event is stamped here when it arrives without a timestamp, so its
        recorded payload and its rendered progress line carry the same instant.
        An event that renders to nothing (its type carries no owner meaning) is
        recorded without emitting a line."""

        stamped = (
            replace(event, occurred_at=datetime.now(UTC).isoformat())
            if not event.occurred_at.strip()
            else event
        )
        ref = self._ports.records.append(record, stamped)
        line = render_progress_line(stamped)
        if line:
            self._ports.progress.emit(line)
        return ref

    def _acquire_tracker_lease(
        self, campaign: CampaignRef, context: CampaignContext, record
    ) -> tuple[LeaseGrant | None, list[LeaseRecord]]:
        """Hold the tracker-root surface before the first claim.

        A live lease naming another campaign raises ``LeaseHeld`` here, before any
        claim, so a second campaign refuses the surface instead of writing into it.
        A stale lease is reclaimed and its previous holder recorded for the run
        record and the campaign report."""

        lease = self._ports.lease
        surface = context.tracker_surface
        if lease is None or surface is None:
            return None, []
        try:
            grant = lease.acquire(LeaseRequest(surface, campaign.campaign_id))
        except LeaseHeld as held:
            self._append(
                record,
                RunEvent(
                    event_type="campaign.lease_refused",
                    payload=_holder_payload(held.record),
                ),
            )
            raise
        reclaims: list[LeaseRecord] = []
        if grant.reclaimed is not None:
            reclaims.append(grant.reclaimed)
            self._append(
                record,
                RunEvent(
                    event_type="campaign.lease_reclaimed",
                    payload={
                        "surface_kind": surface.kind.value,
                        "surface_path": str(surface.path),
                        "previous_holder": _holder_payload(grant.reclaimed),
                    },
                ),
            )
        else:
            self._append(
                record,
                RunEvent(
                    event_type="campaign.lease_acquired",
                    payload=_holder_payload(grant.record),
                ),
            )
        return grant, reclaims

    def _renew_tracker_lease(
        self, grant: LeaseGrant | None, record
    ) -> LeaseGrant | None:
        if grant is None or self._ports.lease is None:
            return grant
        return self._ports.lease.renew(grant)

    def _release_tracker_lease(self, grant: LeaseGrant | None, record) -> None:
        if grant is None or self._ports.lease is None:
            return
        release = self._ports.lease.release(grant)
        if not release.released:
            self._append(
                record,
                RunEvent(
                    event_type="campaign.lease_release_diagnostic",
                    payload={
                        "surface_kind": grant.surface.kind.value,
                        "surface_path": str(grant.surface.path),
                        "diagnostics": list(release.diagnostics),
                    },
                ),
            )

    def _scan_resumes(
        self, campaign: CampaignRef, record: RunRecordRef, context: CampaignContext
    ) -> _Schedule:
        """Before the first selection, find every interrupted item to resume or
        park, so a killed run continues without a person reconciling first.

        Each item the tracker reports still claimed by the builder actor, inside
        the selection fence, becomes a resume candidate with its phase marker.
        The scheduler admits candidates first, in this order: a good state
        resumes from the marker on a worker, counted in the item bound; a
        mismatch parks with ``resume:<reason>`` and snapshots the worktree in
        place. A registered worktree that no claimed item owns is an orphan,
        reported once the candidates are admitted and left untouched."""

        try:
            claims = self._ports.tracker.resumable_claims(campaign)
        except Exception as exc:
            self._append(
                record,
                RunEvent(event_type="campaign.resume_scan_failed", payload={"error": str(exc)}),
            )
            return _Schedule([], {}, [])
        statuses = {status.root: status for status in self._ports.workspace.list_worktrees(campaign)}
        candidates: list[_ResumeCandidate] = []
        attached: set[Path] = set()
        for item in claims:
            if not campaign.selection.permits(item.item_id):
                continue
            marker = self._ports.records.latest_phase_marker(item.item_id)
            if marker is not None and marker.worktree_root in statuses:
                attached.add(marker.worktree_root)
            candidates.append(_ResumeCandidate(item, marker))
        orphan_roots = [root for root in statuses if root not in attached]
        return _Schedule(candidates, statuses, orphan_roots)

    def _report_orphans(self, record: RunRecordRef, schedule: _Schedule) -> None:
        schedule.orphans_reported = True
        for root in schedule.orphan_roots:
            self._append(
                record,
                RunEvent(
                    event_type="resume.orphan_worktree",
                    payload={"worktree_root": str(root)},
                ),
            )

    def _assess_resume(
        self,
        campaign: CampaignRef,
        marker: PhaseMarker | None,
        statuses: dict[Path, WorktreeStatus],
        context: CampaignContext,
    ) -> _ResumeAssessment:
        """Judge whether an interrupted item is in a good state to resume."""

        if marker is None:
            return _ResumeAssessment(False, "resume:missing-marker")
        if marker.state not in _RESUMABLE_MARKER_STATES:
            return _ResumeAssessment(False, "resume:tracker-mismatch")
        if marker.worktree_root not in statuses:
            return _ResumeAssessment(False, "resume:missing-worktree")
        if self._resume_lease_held(campaign, marker.worktree_root, context):
            return _ResumeAssessment(False, "resume:lease-held")
        status = statuses[marker.worktree_root]
        # A delivery integration merge on top of the recorded head is not a
        # move: the workspace steps over it to the commit the marker pinned.
        if marker.head_commit not in {status.head_commit, status.base_commit or status.head_commit}:
            return _ResumeAssessment(False, "resume:head-moved")
        if (
            marker.state in _DIGEST_CHECK_STATES
            and status.workspace_revision != marker.workspace_revision
        ):
            return _ResumeAssessment(False, "resume:revision-changed")
        return _ResumeAssessment(True, "")

    def _resume_lease_held(
        self, campaign: CampaignRef, worktree_root: Path, context: CampaignContext
    ) -> bool:
        """True when another process holds a live lease on the worktree or the
        tracker root, so this campaign must not attach to it."""

        lease = self._ports.lease
        if lease is None:
            return False
        surfaces = [LeaseSurface(worktree_root, SurfaceKind.WORKTREE)]
        if context.tracker_surface is not None:
            surfaces.append(context.tracker_surface)
        for surface in surfaces:
            try:
                holder = lease.live_holder(surface)
            except Exception:
                # An unreadable lease is no signal: the resumed item takes the
                # worktree lease itself and fails on its own if it is held.
                continue
            if holder is not None and holder.campaign_id != campaign.campaign_id:
                return True
        return False

    def _park_resume(
        self,
        campaign: CampaignRef,
        item: WorkItem,
        marker: PhaseMarker | None,
        record,
        reason: str,
        statuses: dict[Path, WorktreeStatus],
    ) -> ItemOutcome:
        """Park an interrupted item that is not in a good state and continue.

        The tracker records ``resume:<reason>``. When the worktree still exists
        and no other writer holds it, it is snapshotted into the run record and
        left in place so nothing is lost."""

        try:
            publication = self._ports.tracker.park(item.item_id, reason, actor=COORDINATOR_ACTOR)
            if publication is not None and not publication.published:
                self._unpublished.add(f"{item.item_id} park")
                self._append(record, unpublished_write(item.item_id, "park", publication.note))
        except Exception as exc:
            self._append(
                record,
                RunEvent(
                    event_type="item.resume_park_failed",
                    item_id=item.item_id,
                    payload={"reason": reason, "error": str(exc)},
                ),
            )
            return ItemOutcome(
                item_id=item.item_id,
                disposition=ItemDisposition.FAILED,
                states=(ItemState.CLAIMED,),
                reason=str(exc),
                title=item.title,
            )
        snapshot_dir = ""
        if (
            reason in {"resume:head-moved", "resume:revision-changed"}
            and marker is not None
            and marker.worktree_root in statuses
        ):
            snapshot_dir = self._snapshot_resume(campaign, item, marker, record, reason)
        self._append(
            record,
            RunEvent(
                event_type="item.resume_parked",
                item_id=item.item_id,
                payload={
                    "reason": reason,
                    "marker_state": marker.state if marker is not None else "",
                    "snapshot_path": snapshot_dir,
                },
            ),
        )
        return ItemOutcome(
            item_id=item.item_id,
            disposition=ItemDisposition.PARKED,
            states=(ItemState.PARKED,),
            reason=reason,
            title=item.title,
        )

    def _snapshot_resume(
        self,
        campaign: CampaignRef,
        item: WorkItem,
        marker: PhaseMarker,
        record,
        reason: str,
    ) -> str:
        """Snapshot a mismatched worktree into the run record and leave it in
        place. A failure to read the worktree is recorded and does not stop the
        campaign."""

        base = f"{item.item_id}-park"
        try:
            workspace = self._ports.workspace.adopt_worktree(
                campaign, item, marker.worktree_root
            )
            snapshot = self._ports.workspace.snapshot(workspace)
        except Exception as exc:
            self._append(
                record,
                RunEvent(
                    event_type="item.resume_snapshot_failed",
                    item_id=item.item_id,
                    payload={"reason": reason, "error": str(exc)},
                ),
            )
            return ""
        if snapshot.patch:
            self._ports.records.write_evidence_file(
                record, f"{base}/changes.patch", snapshot.patch
            )
        for file in snapshot.files:
            self._ports.records.write_evidence_file(
                record, f"{base}/files/{file.path}", file.content
            )
        manifest = {
            "schema": "autobuild.park-snapshot.v1",
            "item_id": item.item_id,
            "reason": reason,
            "start_commit": snapshot.start_commit,
            "patch": "changes.patch" if snapshot.patch else None,
            "tracked_paths": [
                {"path": entry.path.as_posix(), "kind": entry.kind.value, "digest": entry.digest}
                for entry in snapshot.changed_paths
            ],
            "files": [
                {"path": f"files/{file.path}", "source": file.path, "digest": file.digest}
                for file in snapshot.files
            ],
        }
        self._ports.records.write_evidence_file(
            record,
            f"{base}/snapshot.json",
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
        )
        return f"evidence/{base}"

    def _usage_at_warning(self) -> bool:
        """The shared usage reading at its warning level; an unreadable one
        is no signal, and every seat still judges its own reading."""

        try:
            return self._items.usage_at_warning()
        except Exception:
            return False

    def _lanes_available(self) -> bool:
        """True unless every configured lane is currently cooled.

        Single-lane runs (no lanes or lane state configured) are always
        available; a multi-lane run whose lanes are all cooled has no capable
        lane and holds admissions until the earliest cooling resets."""

        lanes = self._ports.lanes
        state = self._ports.lane_state
        if not lanes or state is None:
            return True
        try:
            return any(state.active(lane.name) is None for lane in lanes)
        except Exception:
            # An unreadable lane state is no signal; each seat's own lane
            # choice reads it again.
            return True

    def _select_item(
        self,
        campaign: CampaignRef,
        skip: frozenset[str] = frozenset(),
        admits: Callable[[WorkItem], bool] | None = None,
        peek: bool = True,
    ) -> WorkItem | None:
        """Choose the next item to build. With an allow-list the closed universe is
        consulted in dispatch order; otherwise the tracker's next item is used. The
        exclude-list is checked at every selection. Items already parked with a lane
        signature this campaign, taken by another writer or in flight are skipped,
        so a lane failure never re-picks the same item. ``admits`` refuses a ready
        item for this admission only (an exact declared path an item in flight
        also declares). A campaign that builds more than one item at a time reads
        the ready listing, so it can pass over a refused item. When an allow-list
        has no admissible ready item, the live queue is peeked so the scoped
        tracker can refuse out-of-fence work rather than let the campaign consult
        the live queue for a build. ``peek`` is False while items are in flight:
        the allowed items may then be running, not missing."""

        selection = campaign.selection

        def allowed(item: WorkItem) -> bool:
            return admits is None or admits(item)

        if selection.allow:
            ready = {item.item_id: item for item in self._ports.tracker.ready_items(campaign)}
            for item_id in selection.allow:
                if item_id in selection.exclude or item_id in skip:
                    continue
                if item_id in ready and allowed(ready[item_id]):
                    return ready[item_id]
            if peek:
                self._ports.tracker.next_item(campaign)
            return None
        if selection.exclude or skip or campaign.concurrency > 1:
            for item in self._ports.tracker.ready_items(campaign):
                if (
                    item.item_id not in selection.exclude
                    and item.item_id not in skip
                    and allowed(item)
                ):
                    return item
            return None
        return self._ports.tracker.next_item(campaign)

    def _deliver_report(
        self,
        campaign: CampaignRef,
        context: CampaignContext,
        outcomes: list[ItemOutcome],
        stop: CampaignStopReason,
        next_ref: str,
        follow_ups: list[str],
        unbuilt: list[tuple[str, str]],
        reclaims: list[LeaseRecord],
        orphans: list[tuple[str, str]],
        record: RunRecordRef,
        relative: str,
        schedule: _Schedule,
    ) -> str:
        content = _render_report(
            campaign,
            outcomes,
            next_ref,
            follow_ups,
            unbuilt,
            reclaims,
            orphans,
            record,
            self._unpublished.entries(),
            schedule,
            None
            if schedule.monitor is None
            else schedule.monitor.level_seconds(self._ports.schedule_clock()),
            self._unfinished.entries(),
        )
        try:
            self._ports.workspace.deliver_report(
                CampaignReport(
                    campaign_id=campaign.campaign_id,
                    repository=campaign.repository,
                    relative_path=relative,
                    content=content,
                    mode=context.delivery_mode,
                    target_branch=context.target_branch,
                    target_revision=context.target_revision,
                    push_current_branch=context.push_current_branch,
                    allow_current_branch_default=context.allow_current_branch_default,
                )
            )
        except Exception as exc:
            return f"report delivery failed: {exc}"
        return str(campaign.repository / relative)

    def _next_ready(
        self,
        campaign: CampaignRef,
        stop: CampaignStopReason,
        skip: frozenset[str] = frozenset(),
    ) -> str:
        if stop is CampaignStopReason.QUEUE_DRY:
            return "queue dry"
        if stop is CampaignStopReason.HOLD_LIMIT:
            return "hold limit"
        try:
            peek = self._select_item(campaign, skip)
        except ScopeFenceViolation:
            return "scope fence violation"
        except Exception as exc:
            return f"tracker read failed: {exc}"
        return peek.item_id if peek is not None else "queue dry"

    def _allowed_unbuilt(
        self,
        campaign: CampaignRef,
        outcomes: list[ItemOutcome],
        stop: CampaignStopReason,
    ) -> list[tuple[str, str]]:
        """Name every allow-list item that was not built and why. An item that ran
        (accepted, parked or failed) already appears in the report body; this list
        covers only allowed items the campaign never dispatched."""

        selection = campaign.selection
        if not selection.allow:
            return []
        accepted = {o.item_id for o in outcomes if o.disposition is ItemDisposition.ACCEPTED}
        processed = {o.item_id for o in outcomes}
        try:
            ready = {item.item_id for item in self._ports.tracker.ready_items(campaign)}
        except Exception:
            # Fence refusal or a read failure: the report then names no
            # allowed item as ready.
            ready = set()
        results: list[tuple[str, str]] = []
        for index, item_id in enumerate(selection.allow):
            if item_id in processed:
                continue
            results.append(
                (item_id, self._unbuilt_reason(item_id, index, selection, accepted, ready, stop))
            )
        return results

    @staticmethod
    def _unbuilt_reason(
        item_id: str,
        index: int,
        selection,
        accepted: set[str],
        ready: set[str],
        stop: CampaignStopReason,
    ) -> str:
        if item_id in selection.exclude:
            return "excluded"
        if stop is CampaignStopReason.ITEM_BOUND:
            return "item bound"
        if item_id in ready:
            if stop is CampaignStopReason.HOLD_LIMIT:
                return "hold limit"
            return "item bound"
        preceding = selection.allow[:index]
        if any(
            pid not in accepted and pid not in selection.exclude for pid in preceding
        ):
            return "blocked"
        return "not ready"

    @staticmethod
    def _counts(outcomes: list[ItemOutcome]) -> dict[ItemDisposition, int]:
        counts = {disposition: 0 for disposition in ItemDisposition}
        for outcome in outcomes:
            counts[outcome.disposition] += 1
        return counts

    @staticmethod
    def _default_context(campaign: CampaignRef) -> CampaignContext:
        return CampaignContext(
            harness="",
            models={},
            delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
            validator_id="",
            target_branch="",
            target_revision="",
        )


def _seat_cell(value: object) -> str:
    return "-" if value is None else str(value)


def _holder_payload(record: LeaseRecord) -> dict[str, object]:
    return {
        "campaign_id": record.campaign_id,
        "process_id": record.process_id,
        "host": record.host,
        "started_at": record.started_at,
        "heartbeat_at": record.heartbeat_at,
        "surface_path": str(record.surface_path),
        "surface_kind": record.surface_kind.value,
    }


def _render_report(
    campaign: CampaignRef,
    outcomes: list[ItemOutcome],
    next_ref: str,
    follow_ups: list[str],
    unbuilt: list[tuple[str, str]],
    reclaims: list[LeaseRecord],
    orphans: list[tuple[str, str]],
    record: RunRecordRef,
    unpublished: tuple[str, ...] = (),
    schedule: _Schedule | None = None,
    pressure_seconds: dict[PressureLevel, float] | None = None,
    unfinished: tuple[UnfinishedItem, ...] = (),
) -> str:
    shipped = [o for o in outcomes if o.disposition is ItemDisposition.ACCEPTED]
    parked = [o for o in outcomes if o.disposition is ItemDisposition.PARKED]
    failed = [o for o in outcomes if o.disposition is ItemDisposition.FAILED]
    lines: list[str] = [f"# Campaign {campaign.campaign_id}", ""]

    lines.append("## Shipped")
    lines.append("")
    if shipped:
        for outcome in shipped:
            item_commit = outcome.finalise.item_commit if outcome.finalise else None
            merged = outcome.finalise.merged_commit if outcome.finalise else None
            lines.append(
                f"- {outcome.item_id} {outcome.title} "
                f"item={_seat_cell(item_commit)} merged={_seat_cell(merged)} verified"
            )
    else:
        lines.append("none")
    lines.append("")

    lines.append("## Parked")
    lines.append("")
    if parked or orphans:
        for outcome in parked:
            lines.append(f"- {outcome.item_id}: {outcome.reason or ''}")
        for name, reason in orphans:
            lines.append(f"- {name}: {reason}")
    else:
        lines.append("none")
    lines.append("")

    lines.append("## Failed")
    lines.append("")
    if failed:
        for outcome in failed:
            lines.append(f"- {outcome.item_id}: {outcome.reason or ''}")
    else:
        lines.append("none")
    lines.append("")

    if unfinished:
        lines.append("## Unfinished")
        lines.append("")
        lines.append("Ended without a recorded park; each worktree is kept as evidence.")
        lines.append("")
        for entry in unfinished:
            kept = f" (worktree {entry.worktree})" if entry.worktree else ""
            lines.append(f"- {entry.item_id}: {entry.error}{kept}")
        lines.append("")

    if schedule is not None and schedule.skipped:
        lines.append("## Skipped")
        lines.append("")
        lines.append("Ready in the tracker and passed over; none of these was claimed.")
        lines.append("")
        for item_id, reason in schedule.skipped.items():
            lines.append(f"- {item_id}: {reason}")
        lines.append("")

    if schedule is not None and schedule.holds:
        lines.append("## Holds")
        lines.append("")
        for hold in schedule.holds:
            lines.append(f"- {hold.condition.value}: {hold.seconds:.1f} seconds, {hold.ended}")
        lines.append("")

    lines.append("## Follow-ups")
    lines.append("")
    if follow_ups:
        for title in follow_ups:
            lines.append(f"- {title}")
    else:
        lines.append("none")
    lines.append("")

    if unpublished:
        lines.append("## Unpublished tracker writes")
        lines.append("")
        lines.append(
            "Committed and not yet published; each goes out with the next push of its branch."
        )
        lines.append("")
        for entry in unpublished:
            lines.append(f"- {entry}")
        lines.append("")

    if campaign.selection.allow:
        lines.append("## Allowed items left unbuilt")
        lines.append("")
        if unbuilt:
            for item_id, reason in unbuilt:
                lines.append(f"- {item_id}: {reason}")
        else:
            lines.append("none")
        lines.append("")

    if reclaims:
        lines.append("## Lease reclaims")
        lines.append("")
        for holder in reclaims:
            lines.append(
                f"- {holder.surface_kind.value} {holder.surface_path} reclaimed from "
                f"campaign {holder.campaign_id} (process {holder.process_id} on {holder.host})"
            )
        lines.append("")

    lines.append("## Next")
    lines.append("")
    lines.append(next_ref)
    lines.append("")

    lines.append("## Seat usage")
    lines.append("")
    lines.append(
        "| item | seat | lane | model | requested effort | duration seconds | "
        "input tokens | output tokens | cost |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    any_seat = False
    for outcome in outcomes:
        for seat in outcome.seats:
            any_seat = True
            lines.append(
                f"| {outcome.item_id} | {seat.seat.value} | {_seat_cell(seat.lane)} | "
                f"{_seat_cell(seat.model)} | "
                f"{_seat_cell(seat.effort.value if seat.effort is not None else None)} | "
                f"{_seat_cell(seat.duration_seconds)} | {_seat_cell(seat.input_tokens)} | "
                f"{_seat_cell(seat.output_tokens)} | {_seat_cell(seat.cost)} |"
            )
    if not any_seat:
        lines.append("| none | - | - | - | - | - | - | - | - |")
    lines.append("")

    if schedule is not None:
        lines.append("## Schedule")
        lines.append("")
        lines.append(f"- concurrency {campaign.concurrency}")
        lines.append(f"- peak running {schedule.peak}")
        lines.append(f"- makespan seconds {schedule.makespan:.1f}")
        lines.append(f"- sum of item seconds {schedule.item_seconds:.1f}")
        if pressure_seconds is not None:
            for level in PressureLevel:
                lines.append(f"- seconds at {level.value} {pressure_seconds[level]:.1f}")
        lines.append("")

    lines.append("## Run record")
    lines.append("")
    lines.append(f"- run report {record.root}")
    lines.append(f"- progress log {record.root / 'progress.log'}")
    lines.append("")
    return "\n".join(lines)
