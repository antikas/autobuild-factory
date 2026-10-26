"""Host pressure: the pure rule, the sampler, admission back-off, heavy slots,
the load-suspected rerun and the lease heartbeat, all against fakes.

Campaign-level tests step a fake clock one sample at a time. A step ends only
once the sampler has read, published and gone back to waiting, and the
scheduler waits with nothing left to decide, so every event recorded during a
step carries that step's fake time."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from autobuild.application import CampaignRunner
from autobuild.application.item import HeavySlots, ItemWorkflow
from autobuild.application.progress import render_progress_line
from autobuild.domain import (
    GIB,
    CampaignRef,
    CampaignStopReason,
    CommandResult,
    HostReading,
    ItemDisposition,
    RunEvent,
    Seat,
    SurfaceKind,
)
from autobuild.domain.models import PressureLevel, PressurePolicy
from autobuild.enforcement.policy import ApprovedValidator, EnforcedCommandPort, PolicyConfig
from autobuild.enforcement.pressure import assess_pressure, initial_pressure, memory_lines
from autobuild.testing import FakeClock, FakeCommandAdapter, FakeLeaseAdapter, FakeRunRecordAdapter
from autobuild.testing.fakes import FakeHostResources

from test_parallel_scheduler import (
    DEADLINE,
    _item_thread_alive,
    claimed,
    context,
    events,
    identity,
    item_spec,
    keyed_ports,
    tracker_renewals,
    wait_until,
    work,
)

HIGH, NORMAL, CRITICAL = PressureLevel.HIGH, PressureLevel.NORMAL, PressureLevel.CRITICAL


def reading(cpu: float | None, available_gib: float = 20.0, total_gib: float = 32.0) -> HostReading:
    return HostReading(24, cpu, int(total_gib * GIB), int(available_gib * GIB), None, None, 1.0)


def trace_cpu(t: float) -> float:
    """The scripted trace: 50% before t=100 s, 95% for t=100..490 s, 50% from 500 s."""

    if t < 100:
        return 50.0
    if t < 500:
        return 95.0
    return 50.0


# --- the pure rule -------------------------------------------------------------


def run_rule(policy: PressurePolicy = PressurePolicy(), capacity: int = 6, end: int = 700):
    state = initial_pressure(capacity)
    levels: list[tuple[int, str, float]] = []
    caps: list[tuple[int, int]] = []
    for t in range(0, end + 1, 10):
        new = assess_pressure(state, float(t), reading(trace_cpu(t)), policy)
        if new.level is not state.level:
            levels.append((t, new.level.value, round(new.cpu_mean_percent, 1)))
        if new.cap != state.cap:
            caps.append((t, new.cap))
        state = new
    return levels, caps


def test_the_scripted_trace_gives_exact_level_and_cap_times() -> None:
    levels, caps = run_rule()
    assert levels == [(160, "high", 95.0), (500, "recovering", 88.6), (620, "normal", 50.0)]
    assert caps == [(460, 5), (680, 6)]


@pytest.mark.parametrize(("window", "first_high"), [(50.0, 150), (70.0, 170)])
def test_a_window_one_sample_off_moves_the_high_time(window: float, first_high: int) -> None:
    """The wrong-answer guard: the exact times above fail a window one sample off."""

    levels, _caps = run_rule(PressurePolicy(window_seconds=window))
    assert levels[0][:2] == (first_high, "high")


def test_the_memory_lines_on_32_and_64_gib_hosts() -> None:
    assert memory_lines(32 * GIB, PressurePolicy()) == (4 * GIB, 2 * GIB)
    high, critical = memory_lines(64 * GIB, PressurePolicy())
    assert high == pytest.approx(6.4 * GIB)
    assert critical == pytest.approx(3.2 * GIB)


@pytest.mark.parametrize(
    ("total", "available", "level"),
    [
        (32, 3.9, HIGH),
        (32, 4.1, NORMAL),
        (32, 1.9, CRITICAL),
        (64, 6.3, HIGH),
        (64, 6.5, NORMAL),
        (64, 3.1, CRITICAL),
    ],
)
def test_the_memory_levels(total: float, available: float, level: PressureLevel) -> None:
    state = assess_pressure(
        initial_pressure(6), 0.0, reading(10.0, available, total), PressurePolicy()
    )
    assert state.level is level


def test_recovery_needs_the_memory_margin_above_the_high_line() -> None:
    policy = PressurePolicy()
    state = assess_pressure(initial_pressure(4), 0.0, reading(10.0, 3.9), policy)
    assert state.level is HIGH
    state = assess_pressure(state, 10.0, reading(10.0, 5.9), policy)
    assert state.level is PressureLevel.RECOVERING
    state = assess_pressure(state, 20.0, reading(10.0, 6.0), policy)
    assert state.level is NORMAL


def test_low_commit_headroom_is_high() -> None:
    low = HostReading(24, 10.0, 32 * GIB, 20 * GIB, 100 * GIB, 9 * GIB, 1.0)
    enough = HostReading(24, 10.0, 32 * GIB, 20 * GIB, 100 * GIB, 11 * GIB, 1.0)
    assert assess_pressure(initial_pressure(2), 0.0, low, PressurePolicy()).level is HIGH
    assert assess_pressure(initial_pressure(2), 0.0, enough, PressurePolicy()).level is NORMAL


def test_the_cap_never_falls_below_one() -> None:
    state = initial_pressure(2)
    for t in range(0, 1300, 10):
        state = assess_pressure(state, float(t), reading(10.0, 1.0), PressurePolicy())
    assert (state.level, state.cap) == (CRITICAL, 1)


def test_an_unmeasured_reading_leaves_the_state_unchanged() -> None:
    state = initial_pressure(3)
    assert assess_pressure(state, 0.0, reading(None), PressurePolicy()) is state


# --- campaign harness ----------------------------------------------------------


@dataclass
class TimedRecords(FakeRunRecordAdapter):
    """Run records that note the fake time each event arrived at."""

    clock: FakeClock = field(default_factory=FakeClock)
    times: list[tuple[float, RunEvent]] = field(default_factory=list)

    def append(self, record, event):
        stamp = self.clock.current
        ref = FakeRunRecordAdapter.append(self, record, event)
        with self._guard:
            self.times.append((stamp, event))
        return ref


def timed(ports, event_type: str) -> list[tuple[float, RunEvent]]:
    return [(t, event) for t, event in list(ports.records.times) if event.event_type == event_type]


class Campaign:
    """One campaign on a background thread with the fake clock and host."""

    def __init__(
        self, ports, ref: CampaignRef, items: ItemWorkflow | None = None, ctx=None
    ) -> None:
        self.ports = ports
        self.items = items
        self.context = ctx or context()
        self.runner = CampaignRunner(ports, items)
        self.box: dict[str, object] = {}
        self.thread = threading.Thread(target=self._target, args=(ref,), daemon=True)
        self.thread.start()

    def _target(self, ref: CampaignRef) -> None:
        try:
            self.box["outcome"] = self.runner.run(
                ref, lambda item: item_spec(item, {}), context=self.context
            )
        except BaseException as exc:
            self.box["error"] = exc

    def settled(self, samples_before: int) -> None:
        clock, host = self.ports.schedule_clock, self.ports.host
        wait_until(
            lambda: len(host.samples) > samples_before and clock.waiters >= 1,
            "the sampler to read and wait again",
        )
        wait_until(self.runner.scheduler_idle, "the scheduler to settle")

    def step(self, t: float) -> None:
        before = len(self.ports.host.samples)
        self.ports.schedule_clock.set(float(t))
        self.settled(before)

    def finish(self):
        self.thread.join(DEADLINE * 3)
        assert not self.thread.is_alive(), "the campaign did not finish before its deadline"
        assert "error" not in self.box, self.box.get("error")
        return self.box["outcome"]


def pressure_ports(ids, clock: FakeClock, host: FakeHostResources, **keyed):
    ports = keyed_ports(ids, clock=clock, **keyed)
    return replace(
        ports,
        records=TimedRecords(identity("records"), clock=clock),
        host=host,
        pressure_wait=clock.wait,
    )


def fake_clock() -> FakeClock:
    # Real time never ends a sampler wait during a test; only the test moves time.
    return FakeClock(real_limit=DEADLINE * 6)


def no_monitor_thread() -> bool:
    return not any(thread.name == "autobuild-pressure-monitor" for thread in threading.enumerate())


# --- the scripted trace through the scheduler -----------------------------------


def test_the_scripted_trace_backs_off_admissions_and_never_stops_a_seat() -> None:
    ids = tuple(f"item-{n}" for n in range(1, 10))
    gates = {item_id: threading.Event() for item_id in ids}
    reviewing: set[str] = set()
    guard = threading.Lock()

    def hold_review(request) -> None:
        if request.seat is Seat.REVIEWER:
            with guard:
                reviewing.add(request.item_id)
            assert gates[request.item_id].wait(DEADLINE * 6)

    clock = fake_clock()
    host = FakeHostResources(identity("host"), script=lambda: reading(trace_cpu(clock.current)))
    ports = pressure_ports(ids, clock, host, on_invoke=hold_review)
    ports.tracker.ready.extend(work(item_id) for item_id in ids)
    ref = CampaignRef("campaign", Path("/repo"), max_items=9, concurrency=6)

    run = Campaign(ports, ref)
    wait_until(lambda: len(reviewing) == 6, "six items in review")
    run.settled(0)
    reads: dict[int, int] = {}
    for t in range(10, 690, 10):
        run.step(t)
        reads[t] = ports.tracker.ready_reads
        if t in (200, 520):
            finished = "item-1" if t == 200 else "item-2"
            gates[finished].set()
            wait_until(lambda: not _item_thread_alive(finished), f"{finished} to end")
            wait_until(run.runner.scheduler_idle, "the scheduler to absorb it")

    levels = [(t, event.payload["level"]) for t, event in timed(ports, "campaign.pressure_changed")]
    assert levels == [(160, "high"), (500, "recovering"), (620, "normal")]
    assert timed(ports, "campaign.pressure_changed")[1][1].payload["cpu_busy_percent"] == 88.6
    caps = [(t, event.payload["cap"]) for t, event in timed(ports, "campaign.cap_changed")]
    assert caps == [(460, 5), (680, 6)]
    waits = [
        (t, event.payload["reason"], event.payload["running"], event.payload["concurrency"])
        for t, event in timed(ports, "campaign.waiting")
    ]
    assert waits == [
        (0, "cap", 6, 6),
        (200, "pressure", 5, 6),
        (460, "cap", 5, 5),
        (520, "pressure", 4, 5),
        (620, "cap", 5, 5),
        (680, "cap", 6, 6),
    ]
    admitted = [
        (t, event.item_id, event.payload["running"], event.payload["concurrency"])
        for t, event in timed(ports, "item.admitted")
    ]
    assert admitted[:6] == [(0, f"item-{n}", n, 6) for n in range(1, 7)]
    assert admitted[6:] == [(620, "item-7", 5, 5), (680, "item-8", 6, 6)]
    # Once an episode's wait is recorded, its nudges never re-read the tracker.
    assert reads[450] == reads[210]

    for gate in gates.values():
        gate.set()
    outcome = run.finish()

    assert outcome.stop_reason is CampaignStopReason.ITEM_BOUND
    assert len(outcome.items) == 9
    assert all(item.disposition is ItemDisposition.ACCEPTED for item in outcome.items)
    assert ports.harness.cancelled == []
    assert ports.command.cancelled == []
    report = ports.workspace.reports[0].content
    for line in (
        "- seconds at normal 220.0",
        "- seconds at recovering 120.0",
        "- seconds at high 340.0",
        "- seconds at critical 0.0",
    ):
        assert line in report
    assert no_monitor_thread()


def test_the_pressure_events_render_plain_progress_lines() -> None:
    changed = RunEvent(
        event_type="campaign.pressure_changed",
        payload={
            "level": "high", "previous": "normal", "cpu_busy_percent": 95.0,
            "memory_available_gib": 20.0, "memory_high_line_gib": 4.0, "cap": 6,
        },
    )
    assert render_progress_line(changed) == (
        "host pressure high (was normal): cpu 95.0% over the window, memory available "
        "20.0 GiB; no new admissions or heavy commands"
    )
    cap = RunEvent(
        event_type="campaign.cap_changed",
        payload={"cap": 5, "previous": 6, "capacity": 6, "level": "high"},
    )
    assert render_progress_line(cap) == "item cap 6 -> 5 of 6 at host pressure high"
    waiting = RunEvent(
        event_type="campaign.waiting",
        payload={"reason": "pressure", "running": 5, "concurrency": 6, "items": []},
    )
    assert render_progress_line(waiting) == (
        "waiting to admit: host pressure withholds new admissions; 5 of 6 running"
    )


# --- unmeasured host -------------------------------------------------------------


@pytest.mark.parametrize("missing", ["cpu", "memory"])
def test_an_unmeasured_host_warns_once_and_keeps_the_static_cap(missing: str) -> None:
    ids = ("item-a", "item-b", "item-c")
    unmeasured = (
        HostReading(24, None, 32 * GIB, 20 * GIB, None, None, 1.0, ("cpu busy unmeasured",))
        if missing == "cpu"
        else HostReading(24, 10.0, None, None, None, None, 1.0, ("memory unmeasured",))
    )
    clock = fake_clock()
    host = FakeHostResources(identity("host"), readings=[unmeasured])
    barrier = threading.Barrier(3, timeout=DEADLINE)

    def meet(request) -> None:
        if request.seat is Seat.BUILDER:
            barrier.wait()

    ports = pressure_ports(ids, clock, host, on_invoke=meet)
    ports.tracker.ready.extend(work(item_id) for item_id in ids)

    outcome = Campaign(ports, CampaignRef("campaign", Path("/repo"), max_items=3, concurrency=3)).finish()

    warnings = events(ports, "campaign.pressure_unmeasured")
    assert len(warnings) == 1
    assert warnings[0].payload["concurrency"] == 3
    assert host.samples == [PressurePolicy().read_seconds]
    assert outcome.peak_concurrency == 3
    assert all(item.disposition is ItemDisposition.ACCEPTED for item in outcome.items)
    assert not events(ports, "campaign.pressure_changed")
    assert "- seconds at" not in ports.workspace.reports[0].content
    assert no_monitor_thread()


# --- transient bad reads on a measurable host ------------------------------------


class Refusing(TimedRecords):
    """Run records whose first pressure-level event cannot be written."""

    refused: int = 0

    def append(self, record, event):
        if event.event_type == "campaign.pressure_changed" and not self.refused:
            self.refused = 1
            raise OSError("run record unwritable")
        return TimedRecords.append(self, record, event)


def transient_case(bad_at: frozenset[float], kind: str, policy=None, records=None):
    """Two items held in review on a measurable host. CPU is 50% before
    t=20 s and 95% after, except the reads at ``bad_at``, which are unmeasured
    or raise. Steps to t=200 s, records what happened, then lets the
    campaign end."""

    ids = ("item-1", "item-2")
    gate = threading.Event()
    reviewing: set[str] = set()
    guard = threading.Lock()
    calm = {"now": False}

    def hold_review(request) -> None:
        if request.seat is Seat.REVIEWER:
            with guard:
                reviewing.add(request.item_id)
            assert gate.wait(DEADLINE * 6)

    clock = fake_clock()

    def script() -> HostReading:
        t = clock.current
        if calm["now"]:
            return reading(10.0)
        if t in bad_at and kind == "raise":
            raise OSError("transient read failure")
        if t in bad_at:
            return HostReading.unmeasured("transient counter glitch")
        return reading(50.0 if t < 20 else 95.0)

    host = FakeHostResources(identity("host"), script=script)
    ports = pressure_ports(ids, clock, host, on_invoke=hold_review)
    if records is not None:
        ports = replace(ports, records=records(identity("records"), clock=clock))
    ports.tracker.ready.extend(work(item_id) for item_id in ids)
    items = ItemWorkflow(ports)
    ctx = replace(context(), pressure=policy) if policy is not None else None
    run = Campaign(
        ports, CampaignRef("campaign", Path("/repo"), max_items=2, concurrency=2), items, ctx
    )
    wait_until(lambda: len(reviewing) == 2, "two items in review")
    run.settled(0)
    for t in range(10, 210, 10):
        run.step(t)
    seen = {
        "alive": not no_monitor_thread(),
        "withheld": items.heavy_withheld(),
    }
    calm["now"] = True
    gate.set()
    outcome = run.finish()
    assert all(item.disposition is ItemDisposition.ACCEPTED for item in outcome.items)
    assert ports.harness.cancelled == [] and ports.command.cancelled == []
    assert no_monitor_thread(), [thread.name for thread in threading.enumerate()]
    assert not items.heavy_withheld(), "a finished campaign left the heavy hold in force"
    return ports, seen


def levels_of(ports) -> list[tuple[float, str]]:
    return [(t, event.payload["level"]) for t, event in timed(ports, "campaign.pressure_changed")]


def test_all_valid_reads_go_high_at_the_first_full_window() -> None:
    ports, seen = transient_case(frozenset(), "unmeasured")
    assert levels_of(ports) == [(80.0, "high")]
    assert not events(ports, "campaign.pressure_read_failed")
    assert seen["alive"] and seen["withheld"]


@pytest.mark.parametrize("kind", ["unmeasured", "raise"])
def test_one_bad_read_is_skipped_and_pressure_control_continues(kind: str) -> None:
    ports, seen = transient_case(frozenset({10.0}), kind)

    # The skipped t=10 s read never enters the window, so [10, 70] already
    # holds only 95% samples.
    assert levels_of(ports) == [(70.0, "high")]
    failed = timed(ports, "campaign.pressure_read_failed")
    assert [(t, event.payload["consecutive"]) for t, event in failed] == [(10.0, 1)]
    assert not events(ports, "campaign.pressure_unmeasured")
    assert not events(ports, "campaign.pressure_fallback")
    assert seen["alive"] and seen["withheld"]


def test_three_failed_reads_fall_back_without_raising_the_cap_or_lifting_the_hold() -> None:
    # The cap falls each further 30 s at high: 2 -> 1 at t=110 s.
    policy = PressurePolicy(decrease_seconds=30.0)
    bad = frozenset({120.0, 130.0, 140.0})
    ports, seen = transient_case(bad, "raise", policy)

    assert levels_of(ports) == [(80.0, "high")]
    caps = [(t, event.payload["cap"]) for t, event in timed(ports, "campaign.cap_changed")]
    assert caps == [(110.0, 1)]
    failed = timed(ports, "campaign.pressure_read_failed")
    assert [(t, event.payload["consecutive"]) for t, event in failed] == [(120.0, 1)]
    fallback = timed(ports, "campaign.pressure_fallback")
    assert [(t, event.payload["cap"], event.payload["level"]) for t, event in fallback] == [
        (140.0, 1, "high")
    ]
    assert seen["alive"] and seen["withheld"]


def test_an_unwritable_event_does_not_end_the_monitor() -> None:
    policy = PressurePolicy(decrease_seconds=30.0)
    ports, seen = transient_case(frozenset(), "unmeasured", policy, records=Refusing)

    assert levels_of(ports) == []
    caps = [(t, event.payload["cap"]) for t, event in timed(ports, "campaign.cap_changed")]
    assert caps == [(110.0, 1)]
    assert seen["alive"] and seen["withheld"]


# --- heavy-command limit -------------------------------------------------------


def test_at_most_two_validators_run_with_a_heavy_limit_of_two() -> None:
    ids = tuple(f"item-{n}" for n in range(1, 6))
    release = threading.Event()
    guard = threading.Lock()
    running = {"now": 0, "peak": 0}

    def on_run(_request) -> None:
        with guard:
            running["now"] += 1
            running["peak"] = max(running["peak"], running["now"])
        try:
            assert release.wait(DEADLINE)
        finally:
            with guard:
                running["now"] -= 1

    ports = keyed_ports(ids)
    ports.command.on_run = on_run
    ports.tracker.ready.extend(work(item_id) for item_id in ids)
    items = ItemWorkflow(ports)
    ref = CampaignRef("campaign", Path("/repo"), max_items=5, concurrency=5, heavy_concurrency=2)

    run = Campaign(ports, ref, items)
    wait_until(lambda: items.heavy_commands() == (2, 3), "two validators running, three queued")
    assert running["now"] == 2
    release.set()
    outcome = run.finish()

    assert running["peak"] == 2
    assert len(ports.command.requests) == 5
    assert all(item.disposition is ItemDisposition.ACCEPTED for item in outcome.items)
    assert ports.command.cancelled == []


def test_an_exclusive_heavy_command_runs_alone() -> None:
    slots = HeavySlots(limit=3)
    first = slots.acquire()
    entered = threading.Event()
    box: dict[str, object] = {}

    def exclusive() -> None:
        ticket = slots.acquire(exclusive=True)
        box["counts"] = slots.counts()
        entered.set()
        box["ticket"] = ticket

    thread = threading.Thread(target=exclusive, daemon=True)
    thread.start()
    wait_until(lambda: slots.counts() == (1, 1), "the exclusive command to queue")
    assert not entered.is_set()
    later = threading.Thread(target=lambda: box.setdefault("later", slots.acquire()), daemon=True)
    later.start()
    wait_until(lambda: slots.counts() == (1, 2), "a shared command to queue behind it")
    slots.release(first)
    assert entered.wait(DEADLINE)
    assert box["counts"] == (1, 1)
    assert "later" not in box
    slots.release(box["ticket"])
    later.join(DEADLINE)
    assert slots.counts() == (1, 0)


def test_a_withheld_level_marks_running_commands_and_holds_new_ones() -> None:
    slots = HeavySlots()
    ticket = slots.acquire()
    slots.withhold(True)
    started = threading.Event()
    thread = threading.Thread(
        target=lambda: (slots.acquire(), started.set()), daemon=True
    )
    thread.start()
    wait_until(lambda: slots.counts() == (1, 1), "a command waiting at the withheld level")
    assert not started.is_set()
    slots.withhold(False)
    assert started.wait(DEADLINE)
    assert slots.release(ticket) is True


# --- load-suspected rerun --------------------------------------------------------


TIMED_OUT = CommandResult("command:item-r", None, "out", "err", "start", "end", timed_out=True)
FAILED = CommandResult("command:item-r", 1, "out", "err", "start", "end")
PASSED = CommandResult("command:item-r", 0, "out", "err", "start", "end")


def rerun_case(first: CommandResult, pressure: bool, enforced: bool = False):
    clock = fake_clock()
    memory = {"gib": 20.0}
    host = FakeHostResources(identity("host"), script=lambda: reading(10.0, memory["gib"]))
    ports = pressure_ports(("item-r",), clock, host)
    commands = FakeCommandAdapter(
        identity("command"), results_by_item={"item-r": [first, PASSED]}
    )
    port = commands
    if enforced:
        # The live port: it admits a validator command only when the command
        # id ends with the approved validator id.
        port = EnforcedCommandPort(
            commands,
            PolicyConfig(
                allowed_roots=(Path("/worktrees"),),
                approved_validators=(ApprovedValidator("validator", ("python", "-m", "pytest")),),
                allowed_tools=frozenset({"python"}),
            ),
        )
    ports = replace(ports, command=port)
    items = ItemWorkflow(ports)
    seen: list[tuple[int, int]] = []

    def sample_at(t: float, available_gib: float) -> None:
        memory["gib"] = available_gib
        before = len(host.samples)
        clock.set(t)
        wait_until(lambda: len(host.samples) > before and clock.waiters >= 1, f"the sample at {t}")

    def on_run(_request) -> None:
        seen.append(items.heavy_commands())
        if pressure and len(commands.requests) == 1:
            # Available memory falls under the high line while the validator
            # runs, then recovers before it ends.
            wait_until(lambda: host.samples and clock.waiters >= 1, "the first sample")
            sample_at(10.0, 3.0)
            sample_at(20.0, 20.0)

    commands.on_run = on_run
    ports.tracker.ready.append(work("item-r"))
    run = Campaign(ports, CampaignRef("campaign", Path("/repo"), max_items=1, concurrency=2), items)
    outcome = run.finish()
    return ports, commands, outcome, seen


def test_a_timeout_overlapping_high_is_run_once_more_alone_and_that_verdict_stands() -> None:
    ports, commands, outcome, seen = rerun_case(TIMED_OUT, pressure=True)

    levels = [event.payload["level"] for event in events(ports, "campaign.pressure_changed")]
    assert levels == ["high", "normal"]
    assert len(events(ports, "validation.load_rerun")) == 1
    assert [":rerun:" in request.command_id for request in commands.requests] == [False, True]
    assert all(request.command_id.endswith(":validator") for request in commands.requests)
    assert seen[1] == (1, 0)
    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED
    assert [event.payload["exit_code"] for event in events(ports, "validation.completed")] == [0]
    assert commands.cancelled == [] and ports.harness.cancelled == []


def test_a_load_rerun_passes_the_enforced_command_port() -> None:
    ports, commands, outcome, _seen = rerun_case(TIMED_OUT, pressure=True, enforced=True)

    assert len(events(ports, "validation.load_rerun")) == 1
    assert len(commands.requests) == 2
    assert commands.requests[1].command_id.endswith(":rerun:validator")
    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED, outcome.items[0].reason
    assert [event.payload["exit_code"] for event in events(ports, "validation.completed")] == [0]


def test_a_timeout_at_normal_is_not_run_again() -> None:
    ports, commands, outcome, _seen = rerun_case(TIMED_OUT, pressure=False)

    assert not events(ports, "validation.load_rerun")
    assert len(commands.requests) == 1
    assert outcome.items[0].disposition is ItemDisposition.PARKED


def test_an_assertion_failure_overlapping_high_is_not_run_again() -> None:
    ports, commands, outcome, _seen = rerun_case(FAILED, pressure=True)

    assert [event.payload["level"] for event in events(ports, "campaign.pressure_changed")] == [
        "high",
        "normal",
    ]
    assert not events(ports, "validation.load_rerun")
    assert len(commands.requests) == 1
    assert outcome.items[0].disposition is ItemDisposition.PARKED


# --- lease heartbeat resilience ----------------------------------------------------


def test_three_failed_renewals_withhold_admissions_and_a_renewal_lifts_the_hold() -> None:
    interval = 10.0
    clock = FakeClock(real_limit=DEADLINE * 6)
    attempts = {"heartbeat": 0}
    guard = threading.Lock()
    beats_done = threading.Event()

    def on_renew(surface) -> None:
        heartbeat = threading.current_thread().name == "autobuild-lease-heartbeat"
        if surface.kind is not SurfaceKind.TRACKER or not heartbeat or beats_done.is_set():
            return
        with guard:
            attempts["heartbeat"] += 1
            number = attempts["heartbeat"]
        if number <= 4:
            raise RuntimeError(f"renewal {number} refused")

    lease = FakeLeaseAdapter(identity("lease"), on_renew=on_renew)
    holder: dict[str, object] = {}

    def hook(request) -> None:
        if request.seat is not Seat.BUILDER or request.item_id != "item-a":
            return
        ports = holder["ports"]
        # item-b becomes ready while the scheduler waits, so it sees it only
        # when a nudge wakes it: the withhold, then the renewal that lifts it.
        wait_until(holder["runner"].scheduler_idle, "the scheduler to wait")
        ports.tracker.add_ready(work("item-b"))
        for beat in range(1, 6):
            wait_until(lambda: clock.waiters >= 1, "the heartbeat to wait")
            clock.advance(interval)
            wait_until(lambda: attempts["heartbeat"] >= beat, f"heartbeat {beat}")
            if beat == 3:
                wait_until(
                    lambda: events(ports, "campaign.waiting"), "the lease wait"
                )
                assert "item-b" not in claimed(ports)
        beats_done.set()
        wait_until(lambda: "item-b" in claimed(ports), "item-b admitted once the lease renews")

    ports = keyed_ports(("item-a", "item-b"), on_invoke=hook, clock=clock, lease=lease)
    holder["ports"] = ports
    holder["runner"] = runner = CampaignRunner(ports)
    ports.tracker.ready.append(work("item-a"))

    def target() -> None:
        try:
            holder["outcome"] = runner.run(
                CampaignRef("campaign", Path("/repo"), max_items=2, concurrency=2),
                lambda item: item_spec(item, {}),
                context=context(interval),
            )
        except BaseException as exc:
            holder["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(DEADLINE * 3)
    assert not thread.is_alive()
    assert "error" not in holder, holder.get("error")

    failed = events(ports, "campaign.lease_renew_failed")
    assert [event.payload["consecutive"] for event in failed] == [1, 2, 3, 4]
    assert len(events(ports, "campaign.lease_withheld")) == 1
    assert len(events(ports, "campaign.lease_restored")) == 1
    assert [event.payload["reason"] for event in events(ports, "campaign.waiting")] == ["lease"]
    assert ports.harness.cancelled == []
    outcome = holder["outcome"]
    assert sorted(item.item_id for item in outcome.items) == ["item-a", "item-b"]
    assert all(item.disposition is ItemDisposition.ACCEPTED for item in outcome.items)
    assert tracker_renewals(lease) >= 5
