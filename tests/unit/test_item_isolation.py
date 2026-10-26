"""Two items run through one ``ItemWorkflow`` at the same time, against keyed fakes.

Each item's seats meet the other item's seats at a rendezvous, so both runs are
inside the workflow together. An item's outcome and its run-record events must
equal those of the same item run alone."""

from __future__ import annotations

import threading
from pathlib import Path

from autobuild.application import ItemWorkflow, Lane, WorkflowPorts
from autobuild.domain import (
    AdapterIdentity,
    BuilderReport,
    CampaignRef,
    CommandResult,
    DeliveryMode,
    DiffEvidence,
    ItemDisposition,
    ItemExecutionSpec,
    LaneSignal,
    LaneSignalKind,
    ReviewDecision,
    ReviewVerdict,
    Seat,
    SeatOutcome,
    SeatResult,
    SeatUsage,
    ToolPolicy,
    UsageLimit,
    UsageLimitKind,
    UsagePolicy,
    UsageReading,
    WorkItem,
    WorkspaceRef,
)
from autobuild.testing import (
    FakeClock,
    FakeCommandAdapter,
    FakeHarnessAdapter,
    FakeKnowledgeAdapter,
    FakeLaneStateAdapter,
    FakeLeaseAdapter,
    FakeRunRecordAdapter,
    FakeTrackerAdapter,
    FakeUsageMeter,
    FakeWorkspaceAdapter,
)

FIRST = "item-a"
SECOND = "item-b"
METER = "meter-a"
WAIT_SECONDS = 5


def identity(name: str) -> AdapterIdentity:
    return AdapterIdentity(name, "1", frozenset({"test"}))


def work(item_id: str) -> WorkItem:
    return WorkItem(item_id, f"title {item_id}", "plan", ("passes",))


def built(item_id: str, run: str = "build") -> SeatResult:
    return SeatResult(
        f"{item_id}:{run}", SeatOutcome.SUCCEEDED, BuilderReport(f"report:{item_id}:{run}", "built"),
        f"raw:{item_id}:{run}", SeatUsage(source="fake"), "start", "end",
    )


def failed(item_id: str) -> SeatResult:
    return SeatResult(
        f"{item_id}:failed", SeatOutcome.FAILED, None, f"raw:{item_id}:failed", SeatUsage(), "start", "end"
    )


def passed(item_id: str) -> SeatResult:
    return SeatResult(
        f"{item_id}:review", SeatOutcome.SUCCEEDED,
        ReviewVerdict(item_id, ReviewDecision.PASS, (), f"verdict:{item_id}"),
        f"raw:{item_id}:review", SeatUsage(source="fake"), "start", "end",
    )


def validated(item_id: str) -> CommandResult:
    return CommandResult(f"command:{item_id}", 0, f"stdout:{item_id}", f"stderr:{item_id}", "start", "end")


def workspace_of(item_id: str) -> WorkspaceRef:
    return WorkspaceRef(Path(f"/worktrees/{item_id}"), f"autobuild/{item_id}", "base", f"lease-{item_id}")


def item_spec(item_id: str) -> ItemExecutionSpec:
    return ItemExecutionSpec(
        work(item_id),
        Path("brief.md"),
        "validator",
        ("python", "-m", "pytest"),
        ToolPolicy(frozenset({"python"}), (Path("/worktree"),)),
        "builder-class",
        "reviewer-class",
        "specialist-class",
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        delivery_target_branch="main",
        delivery_target_revision="base",
    )


def campaign() -> CampaignRef:
    return CampaignRef("campaign", Path("/repo"), max_items=2)


def keyed_ports(
    item_ids: tuple[str, ...],
    lanes: tuple[tuple[str, FakeHarnessAdapter], ...],
    *,
    lane_state=None,
    meter: FakeUsageMeter | None = None,
    clock: FakeClock | None = None,
) -> WorkflowPorts:
    workspace = FakeWorkspaceAdapter(
        identity("workspace"),
        workspaces_by_item={item_id: workspace_of(item_id) for item_id in item_ids},
        diffs_by_item={
            item_id: [DiffEvidence(workspace_of(item_id), f"revision:{item_id}", (), f"patch:{item_id}")]
            for item_id in item_ids
        },
    )
    commands = FakeCommandAdapter(
        identity("command"), results_by_item={item_id: [validated(item_id)] for item_id in item_ids}
    )
    return WorkflowPorts(
        FakeTrackerAdapter(identity("tracker")),
        workspace,
        lanes[0][1],
        commands,
        FakeRunRecordAdapter(identity("records")),
        FakeKnowledgeAdapter(identity("knowledge")),
        lease=FakeLeaseAdapter(identity("lease")),
        lanes=tuple(
            Lane(name, harness, meter_id=harness.meter_id) for name, harness in lanes
        ),
        lane_state=lane_state,
        usage_meter=meter,
        usage_policy=UsagePolicy(warn_percent=80, stop_percent=90) if meter is not None else None,
        usage_retry_wait=lambda seconds: None,
        usage_clock=clock or FakeClock(),
    )


def item_events(ports: WorkflowPorts, item_id: str) -> list[tuple[object, ...]]:
    return [
        (event.event_type, event.evidence_refs, event.payload)
        for event in ports.records.events
        if event.item_id == item_id
    ]


def run_alone(ports: WorkflowPorts, item_id: str):
    return ItemWorkflow(ports).run(campaign(), item_spec(item_id), ports.records.create(campaign()))


def run_together(ports: WorkflowPorts, item_ids: tuple[str, ...], done: dict[str, threading.Event] | None = None):
    """Run every item on its own thread through one shared workflow instance."""

    workflow = ItemWorkflow(ports)
    record = ports.records.create(campaign())
    outcomes: dict[str, object] = {}
    errors: list[BaseException] = []

    def run(item_id: str) -> None:
        try:
            outcomes[item_id] = workflow.run(campaign(), item_spec(item_id), record)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if done is not None and item_id in done:
                done[item_id].set()

    threads = [threading.Thread(target=run, args=(item_id,)) for item_id in item_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=4 * WAIT_SECONDS)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    return outcomes


def builders_meet(barrier: threading.Barrier):
    def hook(request) -> None:
        if request.seat is Seat.BUILDER:
            barrier.wait()

    return hook


def single_lane(hook=None) -> FakeHarnessAdapter:
    return FakeHarnessAdapter(
        identity("harness"),
        results_by_item={item_id: [built(item_id), passed(item_id)] for item_id in (FIRST, SECOND)},
        on_invoke=hook,
    )


def test_two_items_run_at_once_each_produce_the_outcome_and_events_they_produce_alone() -> None:
    alone = {}
    for item_id in (FIRST, SECOND):
        ports = keyed_ports((FIRST, SECOND), (("lane", single_lane()),), lane_state=FakeLaneStateAdapter(identity("lanes")))
        alone[item_id] = (run_alone(ports, item_id), item_events(ports, item_id))
    barrier = threading.Barrier(2, timeout=WAIT_SECONDS)
    together = keyed_ports(
        (FIRST, SECOND), (("lane", single_lane(builders_meet(barrier))),),
        lane_state=FakeLaneStateAdapter(identity("lanes")),
    )

    outcomes = run_together(together, (FIRST, SECOND))

    for item_id in (FIRST, SECOND):
        assert outcomes[item_id].disposition is ItemDisposition.ACCEPTED
        assert outcomes[item_id] == alone[item_id][0]
        assert item_events(together, item_id) == alone[item_id][1]
    assert sorted(together.tracker.claims) == [(FIRST, "builder@lane"), (SECOND, "builder@lane")]


def failover_lanes(first_builder_waits: threading.Event | None, first_done: threading.Event | None):
    """Two lanes without shared cooling state. The first item's builder fails on
    lane one with a rate limit and moves to lane two; the second item runs every
    seat on lane one. With the events set, the first item's failed builder
    returns only once the second item's builder has started, and the second
    item's builder returns only once the first item has finished."""

    def lane_one_hook(request) -> None:
        if first_builder_waits is None or request.seat is not Seat.BUILDER:
            return
        if request.item_id == FIRST:
            first_builder_waits.wait(WAIT_SECONDS)
        else:
            first_builder_waits.set()
            first_done.wait(WAIT_SECONDS)

    one = FakeHarnessAdapter(
        identity("lane-one"),
        results_by_item={FIRST: [failed(FIRST)], SECOND: [built(SECOND), passed(SECOND)]},
        signal=LaneSignal(LaneSignalKind.RATE_LIMIT),
        on_invoke=lane_one_hook,
    )
    two = FakeHarnessAdapter(
        identity("lane-two"),
        results_by_item={FIRST: [built(FIRST, "lane-two"), passed(FIRST)], SECOND: []},
    )
    return (("lane-one", one), ("lane-two", two))


def test_a_lane_signal_in_one_item_never_moves_the_router_of_a_concurrent_item() -> None:
    alone = {}
    for item_id in (FIRST, SECOND):
        ports = keyed_ports((FIRST, SECOND), failover_lanes(None, None))
        alone[item_id] = (run_alone(ports, item_id), item_events(ports, item_id))
    assert [seat.lane for seat in alone[FIRST][0].seats] == ["lane-one", "lane-two", "lane-two"]
    assert [seat.lane for seat in alone[SECOND][0].seats] == ["lane-one", "lane-one"]
    started, first_done = threading.Event(), threading.Event()
    together = keyed_ports((FIRST, SECOND), failover_lanes(started, first_done))

    outcomes = run_together(together, (FIRST, SECOND), done={FIRST: first_done})

    assert started.is_set()
    for item_id in (FIRST, SECOND):
        assert outcomes[item_id] == alone[item_id][0]
        assert item_events(together, item_id) == alone[item_id][1]
    cooled = [event for event in together.records.events if event.event_type == "lane.cooled"]
    assert [event.item_id for event in cooled] == [FIRST]


def test_two_items_crossing_the_warning_level_together_emit_one_warning() -> None:
    reading = UsageReading(
        METER, True, (UsageLimit(UsageLimitKind.WEEKLY_ALL, 85, "2026-10-26T06:00:00+00:00"),),
        fetched_at="2026-10-25T20:00:00+00:00",
    )
    meter = FakeUsageMeter(identity("meter"), readings={METER: [reading]})
    barrier = threading.Barrier(2, timeout=WAIT_SECONDS)
    harness = FakeHarnessAdapter(
        identity("metered"),
        results_by_item={item_id: [built(item_id), passed(item_id)] for item_id in (FIRST, SECOND)},
        on_invoke=builders_meet(barrier),
        meter_id=METER,
    )
    ports = keyed_ports((FIRST, SECOND), (("metered", harness),), meter=meter, clock=FakeClock())

    outcomes = run_together(ports, (FIRST, SECOND))

    assert all(outcome.disposition is ItemDisposition.ACCEPTED for outcome in outcomes.values())
    warnings = [event for event in ports.records.events if event.event_type == "usage.warning"]
    assert len(warnings) == 1
    assert warnings[0].payload["percent"] == 85
