"""Every instant the core writes comes from the run record's clock: item events,
their progress lines, the phase marker and the lanes-exhausted hold wait."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from test_never_abandon import run
from test_parallel_scheduler import (
    campaign as scheduler_campaign,
    events,
    finish,
    identity,
    keyed_ports,
    wait_until,
    work,
)
from test_workflows import builder, campaign, command, make_ports, review, spec

from autobuild.application import CampaignRunner, ItemWorkflow
from autobuild.domain import CampaignStopReason, LaneCooling, ReviewDecision, RunEvent
from autobuild.domain.models import HoldCondition
from autobuild.testing import FakeClock, FakeLaneStateAdapter, FakeProgressPort, FakeRunRecordAdapter

FROZEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
FROZEN_TEXT = "2026-01-02T03:04:05+00:00"


def _frozen_records(name: str = "records") -> FakeRunRecordAdapter:
    return FakeRunRecordAdapter(identity(name), now=lambda: FROZEN)


def test_an_item_run_stamps_events_lines_and_phase_marker_from_the_run_record_clock() -> None:
    base = make_ports([builder(), review(ReviewDecision.PASS)], [command()])
    progress = FakeProgressPort()
    ports = replace(base, records=_frozen_records(), progress=progress)
    record = ports.records.create(campaign())

    ItemWorkflow(ports).run(campaign(), spec(), record)

    assert ports.records.events
    assert {event.occurred_at for event in ports.records.events} == {FROZEN_TEXT}
    assert progress.lines
    assert all(line.startswith(f"{FROZEN_TEXT} ") for line in progress.lines)
    markers = [
        json.loads(content.decode("utf-8"))
        for path, content in ports.records.evidence_file_writes
        if path == "item-1-phase.json"
    ]
    assert markers
    assert {marker["timestamp"] for marker in markers} == {FROZEN_TEXT}


def _lane_runner(seconds_from_frozen: float) -> CampaignRunner:
    ports = keyed_ports(("item-1",), clock=FakeClock())
    state = FakeLaneStateAdapter(identity("lanes"))
    reset = (FROZEN + timedelta(seconds=seconds_from_frozen)).isoformat()
    state.cooled["lane"] = LaneCooling("lane", reset, "rate_limit", "earlier", "other")
    return CampaignRunner(replace(ports, records=_frozen_records(), lane_state=state))


def test_the_lane_reset_wait_is_measured_from_the_run_record_clock() -> None:
    runner = _lane_runner(90)

    assert runner._seconds_to_lane_reset() == 90.0
    assert runner._retry_delay(HoldCondition.LANES_EXHAUSTED, 0) == 90.0


def test_a_lane_reset_in_the_past_holds_for_the_lower_bound() -> None:
    runner = _lane_runner(-10)

    assert runner._seconds_to_lane_reset() == -10.0
    assert runner._retry_delay(HoldCondition.LANES_EXHAUSTED, 0) == 1.0


def test_the_lanes_exhausted_hold_waits_until_the_reset_on_the_run_record_clock() -> None:
    clock = FakeClock()
    ports = keyed_ports(("item-1",), clock=clock)
    state = FakeLaneStateAdapter(identity("lanes"))
    reset = (FROZEN + timedelta(seconds=90)).isoformat()
    state.cooled["lane"] = LaneCooling("lane", reset, "rate_limit", "earlier", "other")
    ports = replace(ports, records=_frozen_records(), lane_state=state)
    ports.tracker.ready.append(work("item-1"))

    thread, box, runner = run(ports, scheduler_campaign(2, 2))
    wait_until(lambda: events(ports, "campaign.hold_started"), "the lanes hold")
    started = events(ports, "campaign.hold_started")[0]
    assert started.payload["condition"] == HoldCondition.LANES_EXHAUSTED.value
    assert started.payload["retry_seconds"] == 90.0
    assert started.occurred_at == FROZEN_TEXT
    wait_until(runner.scheduler_idle, "the scheduler to hold")

    del state.cooled["lane"]
    clock.advance(90)
    outcome = finish(thread, box)

    assert outcome.stop_reason is CampaignStopReason.QUEUE_DRY


def test_append_keeps_the_time_of_an_event_that_arrives_stamped() -> None:
    base = make_ports([builder(), review(ReviewDecision.PASS)], [command()])
    progress = FakeProgressPort()
    ports = replace(base, records=_frozen_records(), progress=progress)
    record = ports.records.create(campaign())
    runner = CampaignRunner(ports)

    runner._append(
        record,
        RunEvent(
            "campaign.resumed",
            occurred_at="2025-12-31T00:00:00+00:00",
            payload={"prior_run_id": "run-a"},
        ),
    )

    assert [event.occurred_at for event in ports.records.events] == ["2025-12-31T00:00:00+00:00"]
    assert len(progress.lines) == 1
    assert progress.lines[0].startswith("2025-12-31T00:00:00+00:00 ")

    runner._append(record, RunEvent("campaign.resumed", payload={"prior_run_id": "run-a"}))

    assert ports.records.events[-1].occurred_at == FROZEN_TEXT
