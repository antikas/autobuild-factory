"""The parallel item scheduler against keyed thread-safe fakes and a fake clock.

Each test runs a whole campaign. Seats meet at barriers or wait on events the
test sets, so the moments that matter (three items in flight, a stop observed
while two still run, a usage hold) happen in a fixed order. Every wait has a
deadline of a few real seconds, so a scheduler that holds when it must admit
fails instead of hanging."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from autobuild.application import CampaignRunner, Lane, WorkflowPorts
from autobuild.domain import (
    AdapterIdentity,
    BuilderReport,
    CampaignContext,
    CampaignRef,
    CampaignStopReason,
    CommandResult,
    DeliveryMode,
    DiffEvidence,
    ItemDisposition,
    ItemExecutionSpec,
    LeaseSurface,
    ReviewDecision,
    ReviewVerdict,
    Seat,
    SeatOutcome,
    SeatResult,
    SeatUsage,
    SurfaceKind,
    ToolPolicy,
    UsageLimit,
    UsageLimitKind,
    UsagePolicy,
    UsageReading,
    WorkItem,
    WorkspaceRef,
)
from autobuild.domain import CampaignSelection
from autobuild.enforcement import ScopedTrackerPort
from autobuild.enforcement.selection import admission_limit
from autobuild.enforcement.triage import exact_declared_paths, share_declared_path
from autobuild.testing import (
    FakeClock,
    FakeCommandAdapter,
    FakeHarnessAdapter,
    FakeKnowledgeAdapter,
    FakeLeaseAdapter,
    FakeRunRecordAdapter,
    FakeTrackerAdapter,
    FakeUsageMeter,
    FakeWorkspaceAdapter,
)

DEADLINE = 10.0
METER = "meter-a"
TRACKER_SURFACE = LeaseSurface(Path("/repo/.ergon"), SurfaceKind.TRACKER)


def identity(name: str) -> AdapterIdentity:
    return AdapterIdentity(name, "1", frozenset({"test"}))


def work(item_id: str) -> WorkItem:
    return WorkItem(item_id, f"title {item_id}", "plan", ("passes",))


def built(item_id: str) -> SeatResult:
    return SeatResult(
        f"{item_id}:build", SeatOutcome.SUCCEEDED, BuilderReport(f"report:{item_id}", "built"),
        f"raw:{item_id}:build", SeatUsage(source="fake"), "start", "end",
    )


def passed(item_id: str) -> SeatResult:
    return SeatResult(
        f"{item_id}:review", SeatOutcome.SUCCEEDED,
        ReviewVerdict(item_id, ReviewDecision.PASS, (), f"verdict:{item_id}"),
        f"raw:{item_id}:review", SeatUsage(source="fake"), "start", "end",
    )


def incomplete_review(item_id: str) -> SeatResult:
    # A review seat that succeeded without a verdict is a structural failure.
    return SeatResult(
        f"{item_id}:review", SeatOutcome.SUCCEEDED, None, f"raw:{item_id}:review",
        SeatUsage(), "start", "end",
    )


def workspace_of(item_id: str) -> WorkspaceRef:
    return WorkspaceRef(Path(f"/worktrees/{item_id}"), f"autobuild/{item_id}", "base", f"lease-{item_id}")


def declared(*paths: str) -> str:
    listed = ", ".join(f"`{path}`" for path in paths)
    return f"# Brief\n\n## Declared paths\n\n{listed}\n"


def item_spec(item: WorkItem, briefs: dict[str, str]) -> ItemExecutionSpec:
    return ItemExecutionSpec(
        item,
        Path("brief.md"),
        "validator",
        ("python", "-m", "pytest"),
        ToolPolicy(frozenset({"python"}), (Path("/worktree"),)),
        "builder-class",
        "reviewer-class",
        "specialist-class",
        brief_text=briefs.get(item.item_id, ""),
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        delivery_target_branch="main",
        delivery_target_revision="base",
    )


def keyed_ports(
    item_ids: tuple[str, ...],
    *,
    reviews: dict[str, SeatResult] | None = None,
    on_invoke=None,
    meter: FakeUsageMeter | None = None,
    clock: FakeClock | None = None,
    lease: FakeLeaseAdapter | None = None,
) -> WorkflowPorts:
    reviews = reviews or {}
    harness = FakeHarnessAdapter(
        identity("harness"),
        results_by_item={
            item_id: [built(item_id), reviews.get(item_id, passed(item_id))] for item_id in item_ids
        },
        on_invoke=on_invoke,
        meter_id=METER if meter is not None else None,
    )
    tracker = FakeTrackerAdapter(identity("tracker"))
    workspace = FakeWorkspaceAdapter(
        identity("workspace"),
        workspaces_by_item={item_id: workspace_of(item_id) for item_id in item_ids},
        diffs_by_item={
            item_id: [DiffEvidence(workspace_of(item_id), f"revision:{item_id}", (), f"patch:{item_id}")]
            for item_id in item_ids
        },
        on_deliver=lambda item_id: tracker.mark_done(item_id) if item_id else None,
    )
    commands = FakeCommandAdapter(
        identity("command"),
        results_by_item={
            item_id: [CommandResult(f"command:{item_id}", 0, "out", "err", "start", "end")]
            for item_id in item_ids
        },
    )
    fake_clock = clock or FakeClock()
    return WorkflowPorts(
        tracker,
        workspace,
        harness,
        commands,
        FakeRunRecordAdapter(identity("records")),
        FakeKnowledgeAdapter(identity("knowledge")),
        lease=lease or FakeLeaseAdapter(identity("lease")),
        lanes=(Lane("lane", harness, meter_id=harness.meter_id),),
        usage_meter=meter,
        usage_policy=UsagePolicy(warn_percent=80, stop_percent=90) if meter is not None else None,
        usage_retry_wait=lambda seconds: None,
        usage_clock=fake_clock,
        schedule_clock=fake_clock,
        heartbeat_wait=fake_clock.wait,
        hold_wait=fake_clock.wait,
    )


def campaign(concurrency: int, max_items: int) -> CampaignRef:
    return CampaignRef("campaign", Path("/repo"), max_items=max_items, concurrency=concurrency)


def context(heartbeat: float | None = None) -> CampaignContext:
    return CampaignContext(
        harness="fake",
        models={"builder": "b", "reviewer": "r"},
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        validator_id="validator",
        target_branch="main",
        target_revision="base",
        tracker_surface=TRACKER_SURFACE,
        lease_heartbeat_seconds=heartbeat,
    )


def run_in_background(ports: WorkflowPorts, ref: CampaignRef, briefs=None, heartbeat=None):
    """Run the campaign on a daemon thread; returns (thread, result box)."""

    briefs = briefs or {}
    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["outcome"] = CampaignRunner(ports).run(
                ref, lambda item: item_spec(item, briefs), context=context(heartbeat)
            )
        except BaseException as exc:  # surfaced by finish()
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def finish(thread: threading.Thread, box: dict[str, object]):
    thread.join(DEADLINE)
    assert not thread.is_alive(), "the campaign did not finish before its deadline"
    assert "error" not in box, box.get("error")
    return box["outcome"]


def wait_until(predicate, what: str) -> None:
    deadline = time.monotonic() + DEADLINE
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.005)


def events(ports: WorkflowPorts, event_type: str) -> list:
    return [event for event in list(ports.records.events) if event.event_type == event_type]


def event_index(ports: WorkflowPorts, event_type: str, item_id: str) -> int:
    for index, event in enumerate(list(ports.records.events)):
        if event.event_type == event_type and event.item_id == item_id:
            return index
    raise AssertionError(f"no {event_type} event for {item_id}")


def claimed(ports: WorkflowPorts) -> list[str]:
    return [item_id for item_id, _actor in list(ports.tracker.claims)]


def first_builders_meet(count: int):
    """A harness hook: the first ``count`` builder seats wait for each other."""

    barrier = threading.Barrier(count, timeout=DEADLINE)
    seen = {"builders": 0}
    guard = threading.Lock()

    def hook(request) -> None:
        if request.seat is not Seat.BUILDER:
            return
        with guard:
            seen["builders"] += 1
            index = seen["builders"]
        if index <= count:
            barrier.wait()

    return hook


# --- cap -------------------------------------------------------------------


class _DoneOnDeliveryEvent:
    """A progress port that marks an item done in the fake tracker when its
    delivery event is recorded, so a dependant becomes ready only then."""

    def __init__(self, tracker: FakeTrackerAdapter) -> None:
        self._tracker = tracker

    def begin(self, record) -> None:
        return None

    def emit(self, line: str) -> None:
        match = re.search(r"\bitem (\S+) delivered:", line)
        if match:
            self._tracker.mark_done(match.group(1))


def test_cap_three_runs_exactly_three_and_a_dependant_waits_for_its_blocker() -> None:
    independent = ("item-a", "item-c", "item-d", "item-e", "item-f", "item-g")
    ids = (*independent, "item-b")
    ports = keyed_ports(ids, on_invoke=first_builders_meet(3))
    ports.workspace.on_deliver = None
    ports = replace(ports, progress=_DoneOnDeliveryEvent(ports.tracker))
    ports.tracker.ready.extend(work(item_id) for item_id in ids)
    ports.tracker.blocked_by["item-b"] = frozenset({"item-a"})

    outcome = finish(*run_in_background(ports, campaign(concurrency=3, max_items=7)))

    assert outcome.peak_concurrency == 3
    assert max(event.payload["running"] for event in events(ports, "item.admitted")) == 3
    assert event_index(ports, "item.finalised", "item-a") < event_index(
        ports, "item.admitted", "item-b"
    )
    assert event_index(ports, "item.admitted", "item-b") < event_index(
        ports, "item.claimed", "item-b"
    )
    assert sorted(item.item_id for item in outcome.items) == sorted(ids)
    assert all(item.disposition is ItemDisposition.ACCEPTED for item in outcome.items)
    assert sorted(ports.workspace.delivered) == sorted(ids)
    assert outcome.stop_reason is CampaignStopReason.ITEM_BOUND
    report = ports.workspace.reports[0].content
    assert "- concurrency 3" in report
    assert "- peak running 3" in report


def test_a_serial_campaign_records_no_scheduling_events() -> None:
    ids = ("item-a", "item-b")
    ports = keyed_ports(ids)
    ports.tracker.ready.extend(work(item_id) for item_id in ids)
    ports.tracker.queue.extend(work(item_id) for item_id in ids)

    outcome = finish(*run_in_background(ports, campaign(concurrency=1, max_items=2)))

    assert [item.item_id for item in outcome.items] == ["item-a", "item-b"]
    assert outcome.peak_concurrency == 1
    scheduling = {"item.admitted", "campaign.waiting", "campaign.draining", "item.delivery_waiting"}
    assert not [event for event in ports.records.events if event.event_type in scheduling]
    # The serial schedule reads the tracker's next item, never the ready listing.
    assert ports.tracker.ready_reads == 0


def test_an_allow_list_never_peeks_the_live_queue_while_allowed_items_run() -> None:
    """The out-of-fence peek means "no allowed item is ready". While allowed
    items run it would stop the campaign before a dependant is admitted."""

    ids = ("item-a", "item-b", "item-c")
    holder: dict[str, FakeTrackerAdapter] = {}

    def hook(request) -> None:
        fake = holder["tracker"]
        if request.seat is Seat.BUILDER and request.item_id == "item-a":
            # Hold the blocker until the scheduler has selected with both running.
            wait_until(lambda: fake.ready_reads >= 3, "a selection with two running")
        if request.seat is Seat.REVIEWER and request.item_id == "item-b":
            wait_until(lambda: "item-c" in claimed_ids(fake), "the dependant's claim")

    ports = keyed_ports(ids, on_invoke=hook)
    fake = ports.tracker
    holder["tracker"] = fake
    fake.ready.extend(work(item_id) for item_id in ids)
    fake.blocked_by["item-c"] = frozenset({"item-a"})
    fake.queue.append(work("item-z"))
    fence = CampaignSelection(allow=ids)
    ports = replace(ports, tracker=ScopedTrackerPort(fake, fence))
    ref = CampaignRef("campaign", Path("/repo"), max_items=3, selection=fence, concurrency=3)

    outcome = finish(*run_in_background(ports, ref))

    assert outcome.stop_reason is CampaignStopReason.ITEM_BOUND
    assert sorted(item.item_id for item in outcome.items) == list(ids)
    assert all(item.disposition is ItemDisposition.ACCEPTED for item in outcome.items)
    assert "item-z" not in claimed_ids(fake)


def claimed_ids(tracker: FakeTrackerAdapter) -> list[str]:
    return [item_id for item_id, _actor in list(tracker.claims)]


# --- delivery queue ----------------------------------------------------------


def test_deliveries_run_one_at_a_time_and_a_queued_delivery_says_so() -> None:
    ids = ("item-a", "item-b")
    guard = threading.Lock()
    delivering = {"now": 0, "peak": 0, "count": 0}
    holder: dict[str, WorkflowPorts] = {}

    def on_deliver(item_id) -> None:
        with guard:
            delivering["now"] += 1
            delivering["peak"] = max(delivering["peak"], delivering["now"])
            delivering["count"] += 1
            first = delivering["count"] == 1
        if first:
            # The first delivery holds its turn until the second has queued.
            wait_until(
                lambda: events(holder["ports"], "item.delivery_waiting"), "the queued delivery"
            )
        with guard:
            delivering["now"] -= 1

    ports = keyed_ports(ids, on_invoke=first_builders_meet(2))
    ports.workspace.on_deliver = on_deliver
    holder["ports"] = ports
    ports.tracker.ready.extend(work(item_id) for item_id in ids)

    outcome = finish(*run_in_background(ports, campaign(concurrency=2, max_items=2)))

    assert delivering["peak"] == 1
    assert [event.payload for event in events(ports, "item.delivery_waiting")] == [{"ahead": 1}]
    assert all(item.disposition is ItemDisposition.ACCEPTED for item in outcome.items)


# --- path overlap ------------------------------------------------------------


def test_the_overlap_rule_counts_only_exact_declared_paths() -> None:
    brief = declared("src/x.py", "./docs/a.md", "tests/**", "src\\y.py", "src/*.py")
    assert exact_declared_paths(brief) == frozenset({"src/x.py", "docs/a.md", "src/y.py"})
    assert exact_declared_paths(declared("tests/**")) == frozenset()
    assert share_declared_path(frozenset({"src/x.py"}), frozenset({"src/x.py", "b"}))
    assert not share_declared_path(frozenset(), frozenset())
    assert not share_declared_path(frozenset({"a"}), frozenset({"b"}))


def test_two_items_declaring_the_same_file_never_run_at_the_same_time() -> None:
    released = threading.Event()

    def hold_first(request) -> None:
        if request.seat is Seat.BUILDER and request.item_id == "item-p":
            assert released.wait(DEADLINE)

    ports = keyed_ports(("item-p", "item-q"), on_invoke=hold_first)
    ports.tracker.ready.extend([work("item-p"), work("item-q")])
    briefs = {"item-p": declared("src/x.py", "tests/**"), "item-q": declared("src/x.py")}

    thread, box = run_in_background(ports, campaign(concurrency=2, max_items=2), briefs)
    wait_until(lambda: events(ports, "campaign.waiting"), "the path-overlap wait")
    assert "item-q" not in claimed(ports)
    released.set()
    outcome = finish(thread, box)

    waiting = events(ports, "campaign.waiting")
    assert [event.payload["reason"] for event in waiting] == ["path_overlap"]
    assert waiting[0].payload["items"] == ["item-q"]
    assert outcome.peak_concurrency == 1
    assert event_index(ports, "item.finalised", "item-p") < event_index(
        ports, "item.claimed", "item-q"
    )
    assert all(item.disposition is ItemDisposition.ACCEPTED for item in outcome.items)


def test_two_items_declaring_only_a_glob_run_at_the_same_time() -> None:
    ports = keyed_ports(("item-r", "item-s"), on_invoke=first_builders_meet(2))
    ports.tracker.ready.extend([work("item-r"), work("item-s")])
    briefs = {"item-r": declared("tests/**"), "item-s": declared("tests/**")}

    outcome = finish(*run_in_background(ports, campaign(concurrency=2, max_items=2), briefs))

    assert outcome.peak_concurrency == 2
    assert not events(ports, "campaign.waiting")
    assert all(item.disposition is ItemDisposition.ACCEPTED for item in outcome.items)


# --- drain -------------------------------------------------------------------


def test_a_structural_failure_with_three_in_flight_parks_it_and_keeps_admitting() -> None:
    ids = ("item-1", "item-2", "item-3", "item-4", "item-5")
    meet = first_builders_meet(3)
    holder: dict[str, WorkflowPorts] = {}

    def hook(request) -> None:
        meet(request)
        if request.seat is Seat.REVIEWER and request.item_id in {"item-2", "item-3"}:
            wait_until(lambda: "item-4" in claimed(holder["ports"]), "the next admission")

    ports = keyed_ports(
        ids, reviews={"item-1": incomplete_review("item-1")}, on_invoke=hook
    )
    holder["ports"] = ports
    ports.tracker.ready.extend(work(item_id) for item_id in ids)

    outcome = finish(*run_in_background(ports, campaign(concurrency=3, max_items=5)))

    assert sorted(claimed(ports)) == list(ids)
    assert outcome.stop_reason is CampaignStopReason.ITEM_BOUND
    assert not events(ports, "campaign.draining")
    by_id = {item.item_id: item for item in outcome.items}
    assert sorted(by_id) == list(ids)
    assert by_id["item-1"].structural_failure is True
    assert by_id["item-1"].disposition is ItemDisposition.PARKED
    for item_id in ids[1:]:
        assert by_id[item_id].disposition is ItemDisposition.ACCEPTED
    assert sorted(ports.workspace.released) == sorted(f"lease-{i}" for i in ports.workspace.created)
    acquired = sorted(str(surface.path) for surface in ports.lease.acquired)
    released = sorted(str(surface.path) for surface in ports.lease.released)
    assert acquired == released
    assert str(TRACKER_SURFACE.path) in released


# --- usage -------------------------------------------------------------------


def warning_reading() -> UsageReading:
    return UsageReading(
        METER,
        True,
        (UsageLimit(UsageLimitKind.WEEKLY_ALL, 85, "2026-10-26T06:00:00+00:00"),),
        fetched_at="2026-10-25T20:00:00+00:00",
    )


def test_the_admission_limit_at_the_warning_level_is_one() -> None:
    assert admission_limit(6, usage_at_warning=False) == 6
    assert admission_limit(6, usage_at_warning=True) == 1
    assert admission_limit(1, usage_at_warning=True) == 1


def test_a_warning_reading_holds_admissions_until_nothing_runs_then_admits_one() -> None:
    first = ("item-x", "item-u1", "item-u2")
    later = ("item-w1", "item-w2", "item-w3", "item-w4")
    gates = {item_id: threading.Event() for item_id in first}
    meter_open = threading.Event()
    builders = {"in": 0}
    guard = threading.Lock()
    concurrent = {"now": 0, "late_peak": 0}

    def on_read(_meter_id: str) -> None:
        assert meter_open.wait(DEADLINE)

    def hook(request) -> None:
        if request.seat is not Seat.BUILDER:
            return
        if request.item_id in gates:
            with guard:
                builders["in"] += 1
            assert gates[request.item_id].wait(DEADLINE)
            return
        with guard:
            concurrent["now"] += 1
            concurrent["late_peak"] = max(concurrent["late_peak"], concurrent["now"])
        time.sleep(0.01)
        with guard:
            concurrent["now"] -= 1

    meter = FakeUsageMeter(identity("meter"), readings={METER: [warning_reading()]}, on_read=on_read)
    ports = keyed_ports((*first, *later), on_invoke=hook, meter=meter)
    ports.tracker.ready.extend(work(item_id) for item_id in first)

    thread, box = run_in_background(ports, campaign(concurrency=6, max_items=7))
    # No reading exists yet, so nothing is held: all three are admitted.
    wait_until(lambda: len(claimed(ports)) == 3, "three claims")
    meter_open.set()
    wait_until(lambda: builders["in"] == 3, "three builders at the warning level")
    ports.tracker.add_ready(*(work(item_id) for item_id in later))

    gates["item-x"].set()
    wait_until(
        lambda: any(
            event.payload == {"reason": "usage", "running": 2, "concurrency": 6, "items": []}
            for event in events(ports, "campaign.waiting")
        ),
        "a usage wait with two running",
    )
    assert len(claimed(ports)) == 3
    gates["item-u1"].set()
    wait_until(
        lambda: any(
            event.payload["running"] == 1 for event in events(ports, "campaign.waiting")
        ),
        "a usage wait with one running",
    )
    assert len(claimed(ports)) == 3
    gates["item-u2"].set()
    outcome = finish(thread, box)

    assert claimed(ports) == [*claimed(ports)[:3], *later]
    assert sorted(claimed(ports)[:3]) == sorted(first)
    late_admissions = [
        event for event in events(ports, "item.admitted") if event.item_id in later
    ]
    assert [event.payload["running"] for event in late_admissions] == [1, 1, 1, 1]
    assert event_index(ports, "item.finalised", "item-u2") < event_index(
        ports, "item.admitted", "item-w1"
    )
    assert concurrent["late_peak"] == 1
    assert {event.payload["reason"] for event in events(ports, "campaign.waiting")} == {"usage"}
    assert len(outcome.items) == 7
    assert all(item.disposition is ItemDisposition.ACCEPTED for item in outcome.items)


def test_without_a_metered_lane_nothing_is_held() -> None:
    ids = ("item-a", "item-b", "item-c")
    ports = keyed_ports(ids, on_invoke=first_builders_meet(3))
    ports.tracker.ready.extend(work(item_id) for item_id in ids)

    outcome = finish(*run_in_background(ports, campaign(concurrency=3, max_items=3)))

    assert outcome.peak_concurrency == 3
    assert not events(ports, "campaign.waiting")


# --- heartbeat ---------------------------------------------------------------


def test_the_tracker_lease_is_renewed_during_one_long_item() -> None:
    stale = 30.0
    clock = FakeClock()
    lease = FakeLeaseAdapter(identity("lease"))
    during: list[int] = []

    def hook(request) -> None:
        if request.seat is not Seat.BUILDER:
            return
        # The builder lasts two stale windows of fake time. Time moves on only
        # once the heartbeat waits, so each third of a window renews once.
        for round_number in range(1, 7):
            wait_until(lambda: clock.waiters >= 1, "the heartbeat to wait")
            clock.advance(stale / 3)
            wait_until(
                lambda: tracker_renewals(lease) >= round_number,
                f"heartbeat renewal {round_number}",
            )
        during.append(tracker_renewals(lease))

    ports = keyed_ports(("item-a",), on_invoke=hook, clock=clock, lease=lease)
    ports.tracker.queue.append(work("item-a"))

    outcome = finish(
        *run_in_background(ports, campaign(concurrency=1, max_items=1), heartbeat=stale / 3)
    )

    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED
    assert during == [6]
    # The item boundary still renews once more after the item ends.
    assert tracker_renewals(lease) >= during[0] + 1
    assert str(TRACKER_SURFACE.path) in [str(surface.path) for surface in lease.released]


def tracker_renewals(lease: FakeLeaseAdapter) -> int:
    return sum(1 for surface in list(lease.renewed) if surface.kind is SurfaceKind.TRACKER)


# --- errors inside the scheduler ---------------------------------------------


def _item_thread_alive(item_id: str) -> bool:
    return any(thread.name == f"autobuild-item-{item_id}" for thread in threading.enumerate())


def test_an_error_recording_one_completion_never_loses_the_rest_of_its_batch() -> None:
    """The boundary lease renewal is refused while two completions are queued.
    Both still leave the running set, the refusal is recorded, and the
    campaign finishes instead of waiting forever."""

    renewals = {"tracker": 0}
    guard = threading.Lock()

    def on_renew(surface) -> None:
        if surface.kind is not SurfaceKind.TRACKER:
            return
        with guard:
            renewals["tracker"] += 1
            first = renewals["tracker"] == 1
        if first:
            raise RuntimeError("tracker lease renewal refused")

    lease = FakeLeaseAdapter(identity("lease"), on_renew=on_renew)
    ids = ("item-a", "item-b", "item-c")
    c_entered = threading.Event()

    def hook(request) -> None:
        if request.seat is Seat.BUILDER and request.item_id in {"item-a", "item-b"}:
            assert c_entered.wait(DEADLINE)

    ports = keyed_ports(ids, lease=lease, on_invoke=hook)
    ports.tracker.ready.extend(work(item_id) for item_id in ids)

    def spec_for(item):
        if item.item_id == "item-c":
            # Runs on the scheduler thread at admission: hold it until item-a
            # and item-b have both ended, so their completions arrive together.
            c_entered.set()
            wait_until(
                lambda: not _item_thread_alive("item-a")
                and not _item_thread_alive("item-b")
                and len(ports.workspace.delivered) >= 2,
                "item-a and item-b to end",
            )
        return item_spec(item, {})

    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["outcome"] = CampaignRunner(ports).run(
                campaign(concurrency=3, max_items=3), spec_for, context=context()
            )
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(DEADLINE)

    assert not thread.is_alive(), "the scheduler hung after a completion of the batch was lost"
    # A refused boundary renewal is counted, never raised out of the scheduler.
    assert "error" not in box, box.get("error")
    assert sorted(entry.item_id for entry in box["outcome"].items) == sorted(ids)
    failed = events(ports, "campaign.lease_renew_failed")
    assert [event.payload["error"] for event in failed] == ["tracker lease renewal refused"]
    assert sorted(ports.workspace.delivered) == sorted(ids)
    assert str(TRACKER_SURFACE.path) in [str(surface.path) for surface in lease.released]


def test_a_worker_that_cannot_start_leaves_no_flight_and_the_campaign_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autobuild.application import campaign as campaign_module

    class RefusingThread(threading.Thread):
        def start(self) -> None:
            if self.name == "autobuild-item-item-b":
                raise RuntimeError("cannot start a worker thread")
            super().start()

    monkeypatch.setattr(campaign_module, "Thread", RefusingThread)
    ids = ("item-a", "item-b")
    ports = keyed_ports(ids)
    ports.tracker.ready.extend(work(item_id) for item_id in ids)

    thread, box = run_in_background(ports, campaign(concurrency=2, max_items=2))
    thread.join(DEADLINE)

    assert not thread.is_alive(), "the scheduler waited for a worker that never started"
    assert isinstance(box.get("error"), RuntimeError)
    assert "cannot start a worker thread" in str(box["error"])
    assert "item-b" not in claimed(ports)
    assert sorted(ports.workspace.released) == sorted(f"lease-{i}" for i in ports.workspace.created)
    assert str(TRACKER_SURFACE.path) in [str(surface.path) for surface in ports.lease.released]


# --- domain bound --------------------------------------------------------------


def test_a_campaign_refuses_a_concurrency_below_one() -> None:
    with pytest.raises(ValueError, match="concurrency must be positive"):
        CampaignRef("campaign", Path("/repo"), concurrency=0)
