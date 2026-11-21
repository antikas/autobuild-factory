"""A pause request drains the running items, admits nothing new and stops the
campaign with stop reason ``paused``; the next campaign lifts it and resumes."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_parallel_scheduler import (
    campaign,
    claimed,
    context,
    events,
    finish,
    identity,
    item_spec,
    keyed_ports,
    run_in_background,
    wait_until,
    work,
)

from autobuild.application import CampaignRunner
from autobuild.application.campaign import _Completions, _TrackerLease
from autobuild.application.progress import render_progress_line
from autobuild.domain import (
    CampaignRef,
    CampaignSelection,
    CampaignStopReason,
    CommandResult,
    ItemDisposition,
    LaneCooling,
    LeaseRequest,
    LeaseSurface,
    RunEvent,
    Seat,
    SurfaceKind,
)
from autobuild.testing import (
    FakeClock,
    FakeCommandAdapter,
    FakeLaneStateAdapter,
    FakeLeaseAdapter,
    FakeProgressPort,
    FakeRunRecordAdapter,
)

IDS = ("item-1", "item-2", "item-3")


def _pause_at_builder(holder: dict, item_ids: set[str], count: int):
    """A harness hook: once ``count`` builders of ``item_ids`` have started, the
    pause is requested; each of those builders returns only after that."""

    barrier = threading.Barrier(count, timeout=10.0, action=lambda: setattr(
        holder["ports"].records, "paused", True
    ))

    def hook(request) -> None:
        if request.seat is Seat.BUILDER and request.item_id in item_ids:
            barrier.wait()

    return hook


def _report(ports) -> str:
    return ports.workspace.reports[-1].content


def test_a_pause_during_the_first_build_delivers_it_and_claims_no_other() -> None:
    holder: dict = {}
    ports = keyed_ports(IDS, on_invoke=_pause_at_builder(holder, {"item-1"}, 1))
    holder["ports"] = ports
    ports.tracker.ready.extend(work(item_id) for item_id in IDS)
    ports.tracker.queue.extend(work(item_id) for item_id in IDS)

    outcome = finish(*run_in_background(ports, campaign(concurrency=1, max_items=3)))

    assert outcome.stop_reason is CampaignStopReason.PAUSED
    assert [(item.item_id, item.disposition) for item in outcome.items] == [
        ("item-1", ItemDisposition.ACCEPTED)
    ]
    assert claimed(ports) == ["item-1"]
    assert ports.workspace.delivered == ["item-1"]
    assert len(events(ports, "campaign.pause_requested")) == 1
    completed = events(ports, "campaign.completed")[0]
    assert completed.payload["stop_reason"] == "paused"
    assert render_progress_line(completed).endswith("stop paused")
    report = _report(ports)
    assert "paused on request" in report
    assert "`autobuild run` with the same profile" in report
    assert "stop=paused" in ports.records.summaries[-1]


def test_a_pause_while_two_items_run_lets_both_finish_and_admits_no_third() -> None:
    holder: dict = {}
    pause_at_builder = _pause_at_builder(holder, {"item-1", "item-2"}, 2)

    def hook(request) -> None:
        pause_at_builder(request)
        # Without this hold both items can end before the scheduler next
        # decides, and the pause is then seen with nothing running.
        if request.seat is Seat.BUILDER and request.item_id == "item-2":
            wait_until(
                lambda: events(holder["ports"], "campaign.pause_requested"),
                "the pause to be seen",
            )

    ports = keyed_ports(IDS, on_invoke=hook)
    holder["ports"] = ports
    ports = replace(
        ports,
        command=FakeCommandAdapter(
            identity("command"),
            results_by_item={
                "item-1": [CommandResult("command:item-1", 0, "out", "err", "start", "end")],
                "item-2": [CommandResult("command:item-2", 1, "out", "err", "start", "end")],
            },
        ),
    )
    ports.tracker.ready.extend(work(item_id) for item_id in IDS)

    outcome = finish(*run_in_background(ports, campaign(concurrency=2, max_items=3)))

    assert outcome.stop_reason is CampaignStopReason.PAUSED
    by_id = {item.item_id: item.disposition for item in outcome.items}
    assert by_id == {"item-1": ItemDisposition.ACCEPTED, "item-2": ItemDisposition.PARKED}
    assert sorted(claimed(ports)) == ["item-1", "item-2"]
    paused = events(ports, "campaign.pause_requested")
    assert len(paused) == 1
    # The first decision after the pause follows item-1's end or an earlier
    # nudge; item-2 is held until then, so it is always still running.
    assert paused[0].payload["running"] in (["item-2"], ["item-1", "item-2"])
    assert events(ports, "campaign.draining")[0].payload["stop_reason"] == "paused"


def test_a_pause_seen_with_nothing_running_stops_at_once() -> None:
    ports = keyed_ports(IDS)
    ports.records.paused = True
    ports.tracker.ready.extend(work(item_id) for item_id in IDS)
    ports.tracker.queue.extend(work(item_id) for item_id in IDS)

    outcome = finish(*run_in_background(ports, campaign(concurrency=2, max_items=3)))

    assert outcome.stop_reason is CampaignStopReason.PAUSED
    assert outcome.items == ()
    assert claimed(ports) == []
    assert events(ports, "campaign.pause_requested")[0].payload["running"] == []


def test_a_pause_during_a_hold_is_seen_at_the_next_heartbeat_not_the_hold_timer() -> None:
    # The real limit keeps a fake wait from ending on real time within the
    # test deadline, so only a heartbeat beat can wake the held scheduler.
    clock = FakeClock(real_limit=60.0)
    ports = keyed_ports(("item-1",), clock=clock)
    reset = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    state = FakeLaneStateAdapter(identity("lanes"))
    state.cooled["lane"] = LaneCooling("lane", reset, "rate_limit", "earlier", "other")
    ports = replace(ports, lane_state=state)
    ports.tracker.ready.append(work("item-1"))
    box: dict[str, object] = {}
    runner = CampaignRunner(ports)

    def target() -> None:
        try:
            box["outcome"] = runner.run(
                campaign(2, 2),
                lambda item: item_spec(item, {}),
                context=replace(context(heartbeat=10.0), hold_limit_seconds=86400.0),
            )
        except BaseException as exc:  # surfaced by finish()
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    wait_until(lambda: events(ports, "campaign.hold_started"), "the lanes hold")
    assert events(ports, "campaign.hold_started")[0].payload["retry_seconds"] >= 600
    wait_until(runner.scheduler_idle, "the scheduler to hold")
    wait_until(lambda: clock.waiters >= 2, "the heartbeat and the hold timer to wait")

    ports.records.paused = True
    clock.advance(10.0)
    outcome = finish(thread, box)

    assert outcome.stop_reason is CampaignStopReason.PAUSED
    assert not events(ports, "campaign.hold_limit")
    assert claimed(ports) == []


def test_a_paused_campaign_names_paused_for_a_ready_allowed_item_it_did_not_build() -> None:
    holder: dict = {}
    ports = keyed_ports(("item-1", "item-2"), on_invoke=_pause_at_builder(holder, {"item-1"}, 1))
    holder["ports"] = ports
    ports.tracker.ready.extend([work("item-1"), work("item-2")])
    ports.tracker.queue.extend([work("item-1"), work("item-2")])
    ref = CampaignRef(
        "campaign",
        Path("/repo"),
        max_items=2,
        concurrency=1,
        selection=CampaignSelection(allow=("item-1", "item-2")),
    )

    outcome = finish(*run_in_background(ports, ref))

    assert outcome.stop_reason is CampaignStopReason.PAUSED
    assert "- item-2: paused" in _report(ports)


def test_the_next_campaign_lifts_the_prior_pause_and_delivers_the_rest() -> None:
    ports = keyed_ports(("item-2", "item-3"))
    ports.records.prior_paused_run = "campaign-prior-run"
    ports.tracker.ready.extend([work("item-2"), work("item-3")])
    ports.tracker.queue.extend([work("item-2"), work("item-3")])

    outcome = finish(*run_in_background(ports, campaign(concurrency=1, max_items=3)))

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY
    assert ports.records.cleared_pauses == ["campaign-prior-run"]
    resumed = events(ports, "campaign.resumed")
    assert [event.payload for event in resumed] == [{"prior_run_id": "campaign-prior-run"}]
    types = [event.event_type for event in ports.records.events]
    assert types.index("campaign.resumed") < types.index("item.claimed")
    assert sorted(ports.workspace.delivered) == ["item-2", "item-3"]


def test_a_campaign_with_no_prior_pause_records_no_resume() -> None:
    ports = keyed_ports(("item-1",))
    ports.tracker.queue.append(work("item-1"))

    finish(*run_in_background(ports, campaign(concurrency=1, max_items=1)))

    assert not events(ports, "campaign.resumed")
    assert ports.records.cleared_pauses == []


def test_the_pause_and_resume_events_render_plain_lines() -> None:
    stamp = "2026-11-19T12:00:00+00:00"
    running = RunEvent(
        "campaign.pause_requested", payload={"running": ["item-1"]}, occurred_at=stamp
    )
    idle = RunEvent("campaign.pause_requested", payload={"running": []}, occurred_at=stamp)
    resumed = RunEvent("campaign.resumed", payload={"prior_run_id": "run-a"}, occurred_at=stamp)

    assert render_progress_line(running).endswith(
        "pause requested: no new admissions; waiting for item-1 to finish, then the campaign stops"
    )
    assert render_progress_line(idle).endswith(
        "pause requested: nothing is running, the campaign stops now"
    )
    assert render_progress_line(resumed).endswith(
        "resuming paused run run-a: its pause request is lifted"
    )


@dataclass
class _FailingPauseRecords(FakeRunRecordAdapter):
    """Its pause check raises ``pause_failures`` times, then reads normally;
    ``refuse_failure_events`` makes recording that failure raise too."""

    pause_failures: int = 0
    refuse_failure_events: bool = False

    def pause_requested(self, record):
        if self.pause_failures > 0:
            self.pause_failures -= 1
            raise OSError("run record is unreadable")
        return super().pause_requested(record)

    def append(self, record, event):
        if self.refuse_failure_events and event.event_type == "campaign.pause_check_failed":
            raise OSError("run record is unwritable")
        return super().append(record, event)


@dataclass
class _ScriptedPauseRecords(FakeRunRecordAdapter):
    """Its pause check raises on the calls numbered in ``failing_calls``
    (counted from 1) and reads normally on every other call."""

    failing_calls: frozenset[int] = frozenset()
    calls: int = 0

    def pause_requested(self, record):
        self.calls += 1
        if self.calls in self.failing_calls:
            raise OSError("run record is unreadable")
        return super().pause_requested(record)


def _beats(count: int):
    remaining = [count]

    def wait(stop: threading.Event, interval: float) -> bool:
        if remaining[0] == 0:
            return True
        remaining[0] -= 1
        return False

    return wait


def _run_heartbeat(records: FakeRunRecordAdapter, beats: int):
    lease = FakeLeaseAdapter(identity("lease"))
    progress = FakeProgressPort()
    ports = replace(
        keyed_ports(("item-1",), lease=lease),
        records=records,
        progress=progress,
        heartbeat_wait=_beats(beats),
    )
    grant = lease.acquire(
        LeaseRequest(LeaseSurface(Path("tracker"), SurfaceKind.TRACKER), "campaign")
    )
    lease.renewed.clear()
    record = records.create(campaign(1, 1))
    CampaignRunner(ports)._heartbeat(
        _TrackerLease(grant), threading.Event(), 10.0, record, _Completions()
    )
    return lease, progress


def test_a_failed_heartbeat_pause_check_is_recorded_and_the_lease_still_renews() -> None:
    records = _FailingPauseRecords(identity("records"), pause_failures=2)

    lease, progress = _run_heartbeat(records, beats=3)

    failed = [event for event in records.events if event.event_type == "campaign.pause_check_failed"]
    assert [event.payload for event in failed] == [
        {"error": "run record is unreadable", "consecutive": 1},
        {"error": "run record is unreadable", "consecutive": 2},
    ]
    assert len(lease.renewed) == 3
    assert len([line for line in progress.lines if "pause check failed" in line]) == 1


def test_a_successful_pause_check_resets_the_consecutive_failure_count() -> None:
    records = _ScriptedPauseRecords(identity("records"), failing_calls=frozenset({1, 2, 4}))

    lease, progress = _run_heartbeat(records, beats=4)

    failed = [event for event in records.events if event.event_type == "campaign.pause_check_failed"]
    assert [event.payload["consecutive"] for event in failed] == [1, 2, 1]
    assert len(lease.renewed) == 4
    assert len([line for line in progress.lines if "pause check failed" in line]) == 2


def test_a_pause_check_failure_that_cannot_be_recorded_still_renews_the_lease() -> None:
    records = _FailingPauseRecords(
        identity("records"), pause_failures=1, refuse_failure_events=True
    )

    lease, _ = _run_heartbeat(records, beats=1)

    assert len(lease.renewed) == 1


def test_a_failed_pause_check_renders_one_line_per_run_of_failures() -> None:
    first = RunEvent(
        event_type="campaign.pause_check_failed",
        payload={"error": "run record is unreadable", "consecutive": 1},
    )
    second = replace(first, payload={"error": "run record is unreadable", "consecutive": 2})

    assert render_progress_line(first) == (
        "pause check failed: run record is unreadable; "
        "a pause request is seen at the next admission decision"
    )
    assert render_progress_line(second) == ""
