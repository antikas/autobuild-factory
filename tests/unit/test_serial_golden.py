"""The serial schedule pinned as a golden file.

Each scenario runs a whole campaign one item at a time on the workflow fixtures
of ``test_workflows`` and records the claim order, the item outcomes, the
progress lines and the run-record events. The golden file pins serial
scheduling, and a serial run must keep producing it exactly.
Event timestamps and the progress-line stamp are dropped, and path separators
are written as ``/``, so the file is the same on every platform."""

from __future__ import annotations

import enum
import json
from dataclasses import fields, is_dataclass, replace
from pathlib import Path, PurePath

from test_workflows import (
    builder,
    campaign,
    command,
    failed_builder,
    item,
    lane_ports,
    make_ports,
    spec,
    verdict_result,
)

from autobuild.application import CampaignRunner
from autobuild.domain import (
    LaneSignal,
    LaneSignalKind,
    ReviewDecision,
    TrackerPublication,
    WorkItem,
)
from autobuild.testing import FakeProgressPort

GOLDEN = Path(__file__).parent / "golden" / "serial_schedule.json"


def _plain(value):
    if isinstance(value, enum.Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _plain(entry) for key, entry in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(entry) for entry in value]
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, str):
        return value.replace("\\", "/")
    return value


def _observed(ports, progress: FakeProgressPort, outcome) -> dict[str, object]:
    events = []
    for event in ports.records.events:
        entry = _plain(event)
        entry.pop("occurred_at")
        events.append(entry)
    return {
        "claims": _plain(ports.tracker.claims),
        "outcomes": _plain(outcome.items),
        "stop_reason": outcome.stop_reason.value,
        "progress": [_plain(line.partition(" ")[2]) for line in progress.lines],
        "events": events,
    }


def _mixed_queue() -> dict[str, object]:
    second = WorkItem("item-2", "second", "plan", ("passes",))
    third = WorkItem("item-3", "third", "plan", ("passes",))
    fourth = WorkItem("item-4", "machine item", "plan", ("passes",))
    ports = make_ports(
        [
            builder("b1"),
            verdict_result("item-1", ReviewDecision.PASS, "1"),
            builder("b2"),
            verdict_result("item-2", ReviewDecision.CORRECT, "2a"),
            builder("b2-fold"),
            verdict_result("item-2", ReviewDecision.PASS, "2b"),
            builder("b3"),
            verdict_result("item-3", ReviewDecision.ESCALATE, "3a"),
            verdict_result("item-3", ReviewDecision.PARK, "3b"),
        ],
        [command(suffix="1"), command(suffix="2a"), command(suffix="2b"), command(suffix="3")],
        diff_count=4,
    )
    progress = FakeProgressPort()
    ports = replace(ports, progress=progress)
    ports.tracker.queue.extend([item(), second, third, fourth])

    def spec_for(selected: WorkItem):
        text = "Item nature: machine" if selected.item_id == "item-4" else ""
        return spec(selected, brief_text=text)

    outcome = CampaignRunner(ports).run(campaign(max_items=6), spec_for)
    return _observed(ports, progress, outcome)


def _lane_failover() -> dict[str, object]:
    second = WorkItem("item-2", "second", "plan", ("passes",))
    ports, _state = lane_ports(
        [failed_builder("one")],
        [
            builder("two"),
            verdict_result("item-1", ReviewDecision.PASS, "1"),
            builder("two-b"),
            verdict_result("item-2", ReviewDecision.PASS, "2"),
        ],
        [command(suffix="1"), command(suffix="2")],
        diff_count=2,
        one_signal=LaneSignal(LaneSignalKind.RATE_LIMIT),
    )
    progress = FakeProgressPort()
    ports = replace(ports, progress=progress)
    ports.tracker.publication = TrackerPublication(False, "committed on the pull request branch")
    ports.tracker.queue.extend([item(), second])

    outcome = CampaignRunner(ports).run(campaign(max_items=3), lambda selected: spec(selected))
    return _observed(ports, progress, outcome)


def serial_schedule() -> dict[str, object]:
    return {"mixed_queue": _mixed_queue(), "lane_failover": _lane_failover()}


def render(schedule: dict[str, object]) -> str:
    return json.dumps(schedule, indent=1, sort_keys=True) + "\n"


def test_the_serial_schedule_matches_the_golden_file() -> None:
    expected = GOLDEN.read_text(encoding="utf-8")

    assert render(serial_schedule()) == expected
