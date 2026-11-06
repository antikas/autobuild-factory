"""Every long stage of an item writes one progress line when it starts: seats,
validator runs and delivery. The start events never reach the report, and a run
record with or without them reads the same way."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from test_parallel_scheduler import keyed_ports, wait_until, work
from test_pressure_monitor import TIMED_OUT, Campaign, rerun_case
from test_test_economy import _batch_context, _batch_ports, _Deliveries
from test_usage_profile import BASE, load
from test_workflows import (
    builder,
    campaign,
    command,
    failed_builder,
    identity,
    item,
    lane_ports,
    make_ports,
    review,
    spec,
)

from autobuild.adapters.local_records import LocalRunRecordAdapter
from autobuild.application import CampaignRunner, ItemWorkflow
from autobuild.application.dependencies import Lane
from autobuild.application.progress import render_progress_line
from autobuild.bootstrap import composition
from autobuild.cli import main
from autobuild.domain import (
    CampaignContext,
    CampaignRef,
    DeliveryMode,
    EffortLevel,
    ItemDisposition,
    LaneSignal,
    LaneSignalKind,
    ReviewDecision,
    RunEvent,
    Seat,
    SeatOutcome,
    SeatUsage,
    WorktreeStatus,
)
from autobuild.testing import (
    FakeHarnessAdapter,
    FakeLaneStateAdapter,
    FakeProgressPort,
    FakeRunRecordAdapter,
)

START_EVENTS = {"seat.started", "validation.started", "delivery.started"}
BUILDER_STARTED = "item item-1 seat builder started: model class builder-class, effort default"
REVIEWER_STARTED = "item item-1 seat reviewer started: model class reviewer-class, effort default"
VALIDATION_STARTED = "item item-1 validation started: validator validator"
DELIVERY_STARTED = "item item-1 delivery started: target branch main"
MODELS = {"builder-class": "model-b", "reviewer-class": "model-r"}
LANE_ONE_MODELS = {"builder-class": "model-one", "reviewer-class": "reviewer-one"}
LANE_TWO_MODELS = {"builder-class": "model-two", "reviewer-class": "reviewer-two"}


def _context() -> CampaignContext:
    return CampaignContext(
        harness="test",
        models=MODELS,
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        validator_id="validator",
        target_branch="main",
        target_revision="base",
    )


def _with_progress(ports):
    progress = FakeProgressPort()
    return replace(ports, progress=progress), progress


def _bodies(progress: FakeProgressPort) -> list[str]:
    return [line.partition(" ")[2] for line in list(progress.lines)]


def _item_lines(progress: FakeProgressPort) -> list[str]:
    return [body for body in _bodies(progress) if body.startswith("item ")]


def _with_lane_models(ports):
    one, two = ports.lanes
    return replace(
        ports,
        lanes=(replace(one, models=LANE_ONE_MODELS), replace(two, models=LANE_TWO_MODELS)),
    )


def _rendered(ports) -> list[str]:
    lines = (render_progress_line(event) for event in list(ports.records.events))
    return [line.partition(" ")[2] for line in lines if line]


def test_one_item_writes_its_lines_in_order_from_claim_to_delivery() -> None:
    ports, progress = _with_progress(
        make_ports([builder(), review(ReviewDecision.PASS)], [command()])
    )
    ports = replace(ports, lanes=(Lane("", ports.harness, models=MODELS),))
    ports.tracker.queue.append(item())

    def spec_for(selected):
        return replace(spec(selected), lane_efforts=(("", Seat.BUILDER, EffortLevel.HIGH),))

    outcome = CampaignRunner(ports).run(campaign(), spec_for, context=_context())

    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED
    assert _item_lines(progress) == [
        "item item-1 claimed: test item",
        "item item-1 seat builder started: model model-b, effort high",
        "item item-1 seat builder succeeded",
        VALIDATION_STARTED,
        "item item-1 validation passed",
        "item item-1 seat reviewer started: model model-r, effort default",
        "item item-1 seat reviewer succeeded",
        "item item-1 review pass with 0 finding(s)",
        DELIVERY_STARTED,
        "item item-1 delivered: item item-commit, tracker tracker-commit, pushed True",
    ]
    for line in progress.lines:
        assert line.isascii()
    for event in ports.records.events:
        if event.event_type in START_EVENTS:
            assert not {"cost", "outcome", "exit_code"} & set(event.payload)


def test_a_failing_validator_shows_its_start_line_before_its_failure_line() -> None:
    ports, progress = _with_progress(make_ports([builder()], [command(exit_code=1)]))

    ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    lines = _item_lines(progress)
    failure = lines.index("item item-1 validation failed (exit 1)")
    assert lines[failure - 1] == VALIDATION_STARTED


def test_a_seat_that_times_out_shows_its_start_line_before_its_failure_line() -> None:
    timed_out = replace(failed_builder("review"), outcome=SeatOutcome.TIMED_OUT)
    ports, progress = _with_progress(make_ports([builder(), timed_out], [command()]))

    ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    lines = _item_lines(progress)
    failure = lines.index("item item-1 seat reviewer timed_out")
    assert lines[failure - 1] == REVIEWER_STARTED


def test_a_seat_after_a_lane_failover_names_that_lanes_resolved_model() -> None:
    ports, _state = lane_ports(
        [failed_builder("one")],
        [builder("two"), review(ReviewDecision.PASS)],
        [command()],
        diff_count=1,
        one_signal=LaneSignal(LaneSignalKind.RATE_LIMIT),
    )
    ports, progress = _with_progress(_with_lane_models(ports))
    ports.tracker.queue.append(item())

    CampaignRunner(ports).run(campaign(), lambda selected: spec(selected), context=_context())

    assert [line for line in _item_lines(progress) if " started: " in line and "seat" in line] == [
        "item item-1 seat builder started: model model-one, effort default, lane lane-one",
        "item item-1 seat builder started: model model-two, effort default, lane lane-two",
        "item item-1 seat reviewer started: model reviewer-two, effort default, lane lane-two",
    ]


def test_a_first_lane_cooled_before_the_seat_names_the_next_lanes_resolved_model() -> None:
    state = FakeLaneStateAdapter(identity("lane-state"))
    state.cool("lane-one", LaneSignal(LaneSignalKind.PROBE, detail="absent"), "campaign")
    ports, _state = lane_ports(
        [],
        [builder("two"), review(ReviewDecision.PASS)],
        [command()],
        diff_count=1,
        lane_state=state,
    )
    ports, progress = _with_progress(_with_lane_models(ports))
    ports.tracker.queue.append(item())

    CampaignRunner(ports).run(campaign(), lambda selected: spec(selected), context=_context())

    assert (
        "item item-1 seat builder started: model model-two, effort default, lane lane-two"
        in _item_lines(progress)
    )


def test_bootstrap_passes_each_lanes_model_map_to_its_lane(tmp_path: Path) -> None:
    profile = BASE.replace('lanes = ["claude-code"]', 'lanes = ["claude-code", "codex"]') + (
        '\n[lanes.codex]\nbuilder = "gpt-builder"\nreviewer = "gpt-reviewer"\n'
    )
    settings = load(tmp_path, profile)
    expected = {
        lane.name: {
            "builder": lane.builder_model,
            "reviewer": lane.reviewer_model,
            "specialist": lane.specialist_model,
        }
        for lane in settings.lanes
    }
    assert expected["codex"]["builder"] == "gpt-builder"

    base = make_ports([], [])
    lanes_raw = [(name, FakeHarnessAdapter(identity(name))) for name in expected]
    ports = composition._ports(
        settings,
        tmp_path,
        DeliveryMode.PROTECTED_DEFAULT,
        base.tracker,
        base.workspace,
        base.harness,
        base.command,
        base.records,
        base.knowledge,
        None,
        lanes_raw,
        FakeLaneStateAdapter(identity("lane-state")),
        "codex",
        FakeProgressPort(),
    )

    assert {lane.name: dict(lane.models) for lane in ports.lanes} == expected


def test_a_validator_waiting_for_a_heavy_slot_has_no_start_line_until_it_is_granted() -> None:
    ids = ("item-a", "item-b", "item-c")
    release = threading.Event()

    def on_run(_request) -> None:
        assert release.wait(10.0)

    ports, progress = _with_progress(keyed_ports(ids))
    ports.command.on_run = on_run
    ports.tracker.ready.extend(work(item_id) for item_id in ids)
    items = ItemWorkflow(ports)
    ref = CampaignRef("campaign", Path("/repo"), max_items=3, concurrency=2, heavy_concurrency=1)

    run = Campaign(ports, ref, items)
    wait_until(lambda: items.heavy_commands() == (1, 1), "one validator running, one queued")
    wait_until(
        lambda: any("waiting to admit" in body for body in _bodies(progress)), "the wait line"
    )
    held = _bodies(progress)
    release.set()
    outcome = run.finish()

    started = [body for body in held if "validation started" in body]
    assert len(started) == 1
    assert sum("seat builder succeeded" in body for body in held) == 2
    assert "waiting to admit: the concurrency cap is reached" in "\n".join(held)
    assert all(entry.disposition is ItemDisposition.ACCEPTED for entry in outcome.items)
    assert sum("validation started" in body for body in _bodies(progress)) == 3


def test_a_revalidation_at_delivery_says_so_after_the_delivery_start_line() -> None:
    ports, progress = _with_progress(
        make_ports(
            [builder(), review(ReviewDecision.PASS)], [command(), command(suffix="integrated")]
        )
    )
    ports.workspace.base_moved = True

    ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    lines = _item_lines(progress)
    revalidation = lines.index(f"{VALIDATION_STARTED} (revalidation at delivery)")
    assert lines.index(DELIVERY_STARTED) < revalidation
    assert lines[revalidation + 1] == "item item-1 validation passed"
    assert lines.count(VALIDATION_STARTED) == 1


def test_a_load_rerun_writes_its_own_start_line_after_it_is_announced() -> None:
    ports, _commands, _outcome, _seen = rerun_case(TIMED_OUT, pressure=True)

    lines = _rendered(ports)
    first = lines.index("item item-r validation started: validator validator")
    announced = lines.index(
        "item item-r validation timed out under host pressure; running it once more alone"
    )
    rerun = lines.index("item item-r validation started: validator validator (load rerun)")
    assert first < announced < rerun
    assert lines[rerun + 1] == "item item-r validation passed"


@dataclass
class _DropsStartEvents(FakeRunRecordAdapter):
    """A run record as it was before start events existed."""

    def append(self, record, event):
        if event.event_type in START_EVENTS:
            return ""
        return super().append(record, event)


def test_the_report_is_byte_identical_with_and_without_start_events() -> None:
    def report(records_type) -> tuple[str, list[str]]:
        base = make_ports([builder(), review(ReviewDecision.PASS)], [command()])
        ports = replace(base, records=records_type(base.records.identity))
        ports.tracker.queue.append(item())
        CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))
        return (
            ports.workspace.reports[0].content,
            [event.event_type for event in ports.records.events],
        )

    with_starts, events_with = report(FakeRunRecordAdapter)
    without_starts, events_without = report(_DropsStartEvents)

    assert START_EVENTS <= set(events_with)
    assert not START_EVENTS & set(events_without)
    assert with_starts.encode("utf-8") == without_starts.encode("utf-8")
    assert with_starts.count("| item-1 | builder |") == 1
    assert with_starts.count("| item-1 | reviewer |") == 1


class _BatchHeld(_Deliveries):
    """Item work that holds the campaign's only heavy slot from the start, so a
    batch validation has to wait for it."""

    def use_heavy_slots(self, slots) -> None:
        self.slots = slots
        self.ticket = slots.acquire()


def test_a_batch_validation_has_no_start_line_until_its_heavy_slot_is_granted() -> None:
    ports, progress = _with_progress(_batch_ports(1, [command(suffix="b1")]))
    ports.tracker.ready.extend(ports.tracker.queue)
    ports.tracker.queue.clear()
    ports.workspace.validation_revisions = ["head-1"]
    items = _BatchHeld()
    ref = CampaignRef("campaign", Path("/repo"), max_items=1, concurrency=2, heavy_concurrency=1)

    run = Campaign(ports, ref, items, ctx=_batch_context(every=1))
    wait_until(lambda: hasattr(items, "slots") and items.slots.counts() == (1, 1), "a queued batch")
    assert not [body for body in _bodies(progress) if body.startswith("batch validation")]
    assert ports.command.requests == []

    items.slots.release(items.ticket)
    run.finish()

    assert [body for body in _bodies(progress) if body.startswith("batch validation")] == [
        "batch validation 1 started: validator tests on head-1",
        "batch validation 1 passed on head-1 after 1 delivery(ies)",
    ]
    started = [event for event in ports.records.events if event.event_type == "campaign.batch_started"]
    assert [event.payload for event in started] == [
        {"batch": 1, "revision": "head-1", "validator_id": "tests"}
    ]


def test_a_failing_final_batch_shows_its_start_line_before_its_failure_line() -> None:
    ports, progress = _with_progress(_batch_ports(1, [command(exit_code=1, suffix="b1")]))
    ports.workspace.validation_revisions = ["head-1"]

    CampaignRunner(ports, items=_Deliveries()).run(
        campaign(max_items=1), lambda selected: spec(selected), context=_batch_context(every=5)
    )

    batch_lines = [body for body in _bodies(progress) if body.startswith("batch validation")]
    assert batch_lines[0] == "batch validation 1 started: validator tests on head-1"
    assert batch_lines[1].startswith("batch validation 1 failed on head-1")
    assert len(batch_lines) == 2


@dataclass
class _RefusesBatchStart(FakeRunRecordAdapter):
    def append(self, record, event):
        if event.event_type == "campaign.batch_started":
            raise OSError("run record unavailable")
        return super().append(record, event)


def test_a_batch_start_event_that_cannot_be_recorded_does_not_fail_the_batch() -> None:
    base = _batch_ports(1, [command(suffix="b1")])
    ports = replace(base, records=_RefusesBatchStart(base.records.identity))
    ports.workspace.validation_revisions = ["head-1"]

    CampaignRunner(ports, items=_Deliveries()).run(
        campaign(max_items=1), lambda selected: spec(selected), context=_batch_context(every=5)
    )

    types = [event.event_type for event in ports.records.events]
    assert "campaign.batch_validated" in types
    assert "campaign.batch_validation_failed" not in types


def test_a_cost_renders_as_a_rounded_estimate_and_the_event_keeps_the_exact_number() -> None:
    exact = 0.46740859999999995
    usage = SeatUsage(input_tokens=5, output_tokens=3, cost=exact, source="fake")
    ports, progress = _with_progress(make_ports([replace(builder(), usage=usage)], [command()]))

    ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    completed = next(
        event for event in ports.records.events if event.event_type == "seat.completed"
    )
    assert completed.payload["cost"] == exact
    line = next(line for line in _item_lines(progress) if "seat builder succeeded" in line)
    assert line.endswith(", estimated cost 0.47")


@pytest.mark.parametrize(
    ("cost", "suffix"),
    [(0.46740859999999995, ", estimated cost 0.47"), (0.0, ", estimated cost 0.00"), (None, "")],
)
def test_the_seat_completion_line_renders_the_exact_cost_suffix(cost, suffix: str) -> None:
    event = RunEvent(
        "seat.completed", "", "item-1", payload={"seat": "builder", "outcome": "ok", "cost": cost}
    )

    assert render_progress_line(event) == f"item item-1 seat builder ok{suffix}"


def _stamped(event_type: str, payload: dict[str, object]) -> RunEvent:
    return RunEvent(event_type, "2026-11-06T10:00:00+00:00", "item-1", payload=payload)


UNFINISHED = {
    "seat": _stamped(
        "seat.started",
        {"seat": "builder", "model_class": "builder", "model": "m", "effort": None, "lane": ""},
    ),
    "validation": _stamped(
        "validation.started",
        {"validator_id": "validator", "candidate": "reviewed", "rerun": False},
    ),
    "delivery": _stamped("delivery.started", {"target_branch": "main"}),
}
BEFORE_START_EVENTS = _stamped("item.claimed", {"title": "test item"})


def _resume_from(tmp_path: Path, last_event: RunEvent) -> tuple:
    records = LocalRunRecordAdapter(tmp_path / "runs")
    killed = records.create(campaign())
    records.append(killed, last_event)
    records.write_evidence_file(
        killed,
        "item-1-phase.json",
        json.dumps(
            {
                "schema": "autobuild.item-phase.v1",
                "item_id": "item-1",
                "state": "built",
                "worktree_root": "/worktree",
                "branch": "run",
                "head_commit": "base",
                "workspace_revision": "digest-built",
                "correction_count": 0,
            }
        ).encode("utf-8"),
    )
    base = make_ports([review(ReviewDecision.PASS)], [command()])
    ports = replace(base, records=records)
    ports.tracker.resumable = [item()]
    ports.workspace.worktrees = [WorktreeStatus(Path("/worktree"), "run", "base", "digest-built")]
    return ports, CampaignRunner(ports).run(campaign(), lambda selected: spec(selected))


@pytest.mark.parametrize(
    "last_event",
    [*UNFINISHED.values(), BEFORE_START_EVENTS],
    ids=[*UNFINISHED, "before-start-events"],
)
def test_resume_reads_a_killed_run_record_and_one_with_no_start_events(
    tmp_path: Path, last_event: RunEvent
) -> None:
    ports, outcome = _resume_from(tmp_path, last_event)

    assert outcome.items[0].disposition is ItemDisposition.ACCEPTED
    assert [request.seat.value for request in ports.harness.requests] == ["reviewer"]


def _write_progress(scratch: Path, run: str, events: list[RunEvent], last: str = "") -> None:
    run_dir = scratch / "runs" / run
    run_dir.mkdir(parents=True)
    lines = [render_progress_line(event) for event in events]
    (run_dir / "progress.log").write_text(
        "".join(f"{line}\n" for line in [*lines, *([last] if last else [])]),
        encoding="utf-8",
        newline="\n",
    )


@pytest.mark.parametrize("stage", list(UNFINISHED))
def test_watch_prints_a_start_line_with_no_completion_and_times_out(
    tmp_path: Path, capsys, stage: str
) -> None:
    _write_progress(tmp_path, "killed", [UNFINISHED[stage]])

    code = main(
        ["watch", "--run", "killed", "--scratch-root", str(tmp_path), "--timeout-seconds", "0.05"]
    )

    captured = capsys.readouterr()
    assert code == 3
    assert captured.out.splitlines() == [render_progress_line(UNFINISHED[stage])]


def test_watch_reads_a_finished_run_from_before_start_events(tmp_path: Path, capsys) -> None:
    completion = (
        "2026-11-06T10:00:01+00:00 campaign completed: shipped 1, parked 0, failed 0; "
        "report r.md; stop item_bound"
    )
    _write_progress(tmp_path, "old", [BEFORE_START_EVENTS], completion)

    code = main(["watch", "--run", "old", "--scratch-root", str(tmp_path)])

    assert code == 0
    assert capsys.readouterr().out.splitlines() == [
        render_progress_line(BEFORE_START_EVENTS),
        completion,
    ]
