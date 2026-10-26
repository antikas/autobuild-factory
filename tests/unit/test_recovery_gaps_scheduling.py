"""Campaign recovery against fakes and a fake clock: a timed-out builder
continues in place, and host pressure never takes a campaign's last slot."""

from __future__ import annotations

import threading
from pathlib import Path

from autobuild.application import CampaignRunner
from autobuild.domain import (
    CampaignRef,
    CampaignStopReason,
    ItemDisposition,
    Seat,
    SeatOutcome,
    SeatResult,
    SeatUsage,
)
from autobuild.testing import FakeClock

from test_parallel_scheduler import (
    DEADLINE,
    built,
    claimed,
    context,
    events,
    item_spec,
    keyed_ports,
    passed,
    wait_until,
    work,
    workspace_of,
)
from test_pressure_monitor import Campaign, fake_clock, pressure_ports, reading, timed
from autobuild.testing.fakes import FakeHostResources
from test_parallel_scheduler import identity


# --- 3. A timed-out builder continues in place -----------------------------------


def timed_out(item_id: str, n: int) -> SeatResult:
    return SeatResult(
        f"{item_id}:build:{n}", SeatOutcome.TIMED_OUT, None, f"raw:{item_id}:timeout:{n}",
        SeatUsage(), "start", "end",
    )


def run_one(script: list[SeatResult]):
    ports = keyed_ports(("item-1",), clock=FakeClock())
    ports.harness.results_by_item["item-1"] = script
    ports.tracker.queue.append(work("item-1"))
    outcome = CampaignRunner(ports).run(
        CampaignRef("campaign", Path("/repo"), max_items=1, concurrency=1),
        lambda item: item_spec(item, {}),
        context=context(),
    )
    return ports, outcome


def builder_requests(ports):
    return [request for request in ports.harness.requests if request.seat is Seat.BUILDER]


def test_a_timed_out_builder_keeps_its_claim_and_worktree_and_a_fresh_seat_completes_it() -> None:
    ports, outcome = run_one([timed_out("item-1", 1), built("item-1"), passed("item-1")])

    assert [(item.item_id, item.disposition) for item in outcome.items] == [
        ("item-1", ItemDisposition.ACCEPTED)
    ]
    assert claimed(ports) == ["item-1"]
    assert ports.tracker.parked == []
    assert ports.workspace.snapshotted == []
    assert ports.workspace.adopted == [workspace_of("item-1").root]
    first, second = builder_requests(ports)
    assert first.workspace.root == second.workspace.root == workspace_of("item-1").root
    assert "partial work" not in first.instructions
    assert "A previous builder seat timed out in this workspace" in second.instructions
    assert "Complete that partial work; do not restart" in second.instructions
    continued = events(ports, "item.builder_continued")
    assert [(event.payload["continuation"], event.payload["limit"]) for event in continued] == [(1, 2)]
    assert not events(ports, "item.parked")


def test_a_third_builder_timeout_parks_with_the_snapshot() -> None:
    ports, outcome = run_one([timed_out("item-1", n) for n in (1, 2, 3)])

    assert [(item.item_id, item.disposition, item.reason) for item in outcome.items] == [
        ("item-1", ItemDisposition.PARKED, "timeout:builder")
    ]
    assert len(builder_requests(ports)) == 3
    assert ports.workspace.adopted == [workspace_of("item-1").root] * 2
    assert claimed(ports) == ["item-1"]
    assert [entry[0] for entry in ports.tracker.parked] == ["item-1"]
    assert "timeout:builder" in ports.tracker.parked[0][1]
    assert "snapshot=evidence/item-1-park" in ports.tracker.parked[0][1]
    assert ports.workspace.snapshotted == [workspace_of("item-1").lease_id]
    continued = events(ports, "item.builder_continued")
    assert [event.payload["continuation"] for event in continued] == [1, 2]


# --- 4. Pressure never takes the last slot -----------------------------------------


def test_sustained_high_pressure_with_nothing_running_admits_exactly_one_item() -> None:
    ids = ("item-1", "item-2", "item-3", "item-4")
    gates = {item_id: threading.Event() for item_id in ids}
    reviewing: set[str] = set()
    guard = threading.Lock()

    def hold_review(request) -> None:
        if request.seat is Seat.REVIEWER:
            with guard:
                reviewing.add(request.item_id)
            assert gates[request.item_id].wait(DEADLINE * 6)

    clock = fake_clock()
    # 50% before t=20 s, 95% after: the level is high from t=80 s and stays.
    host = FakeHostResources(
        identity("host"), script=lambda: reading(50.0 if clock.current < 20 else 95.0)
    )
    ports = pressure_ports(ids, clock, host, on_invoke=hold_review)
    ports.tracker.ready.extend(work(item_id) for item_id in ids)
    run = Campaign(ports, CampaignRef("campaign", Path("/repo"), max_items=4, concurrency=2))
    wait_until(lambda: len(reviewing) == 2, "two items in review")
    run.settled(0)
    for t in range(10, 110, 10):
        run.step(t)
    assert [event.payload["level"] for _t, event in timed(ports, "campaign.pressure_changed")] == [
        "high"
    ]

    gates["item-1"].set()
    gates["item-2"].set()
    wait_until(lambda: "item-3" in reviewing, "one item admitted at high pressure")
    wait_until(run.runner.scheduler_idle, "the scheduler to settle")

    assert claimed(ports) == ["item-1", "item-2", "item-3"]
    pressure_waits = [
        event.payload["running"]
        for event in events(ports, "campaign.waiting")
        if event.payload["reason"] == "pressure"
    ]
    assert pressure_waits == [1]
    assert "item-4" not in reviewing

    for gate in gates.values():
        gate.set()
    outcome = run.finish()

    assert outcome.stop_reason is CampaignStopReason.ITEM_BOUND
    assert [item.disposition for item in outcome.items] == [ItemDisposition.ACCEPTED] * 4
    assert outcome.peak_concurrency == 2
