"""Every seat attempt records the launch its harness reports."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

from test_workflows import builder, campaign, command, identity, make_ports, review, spec

from autobuild.adapters import ClaudeCodeHarnessAdapter
from autobuild.adapters.local_records import LocalRunRecordAdapter
from autobuild.application import CampaignRunner, ItemWorkflow
from autobuild.domain import CommandResult, ItemDisposition, ReviewDecision
from autobuild.testing import FakeCommandAdapter, FakeProgressPort
from autobuild.testing.fakes import FAKE_SEAT_LAUNCH

SENTINEL = "SENTINEL-8f2c"
SENTINEL_NAME = "AUTOBUILD_LAUNCH_PROBE"


class SentinelClaudeAdapter(ClaudeCodeHarnessAdapter):
    unattended_environment = (
        *ClaudeCodeHarnessAdapter.unattended_environment,
        (SENTINEL_NAME, SENTINEL),
    )


def seat_output(tmp_path: Path, name: str, structured: dict) -> CommandResult:
    out = tmp_path / f"{name}-stdout.txt"
    err = tmp_path / f"{name}-stderr.txt"
    out.write_text(json.dumps({"result": "complete", "structured_output": structured}), encoding="utf-8")
    err.write_text("", encoding="utf-8")
    return CommandResult(name, 0, str(out), str(err), "start", "end")


def test_every_seat_start_is_followed_by_its_launch_before_it_completes() -> None:
    ports = make_ports([builder(), review(ReviewDecision.PASS)], [command()])

    outcome = ItemWorkflow(ports).run(campaign(), spec(), ports.records.create(campaign()))

    assert outcome.disposition is ItemDisposition.ACCEPTED
    seat_events = [
        event
        for event in ports.records.events
        if event.event_type in {"seat.started", "seat.launched", "seat.completed"}
    ]
    assert [event.event_type for event in seat_events] == [
        "seat.started", "seat.launched", "seat.completed",
    ] * 2
    for started, launched, completed in zip(*[iter(seat_events)] * 3):
        assert launched.item_id == started.item_id
        assert launched.payload["seat"] == started.payload["seat"] == completed.payload["seat"]
        assert launched.payload["lane"] == started.payload["lane"]
    assert [event.payload["seat"] for event in seat_events[1::3]] == ["builder", "reviewer"]
    assert seat_events[1].payload == {
        "seat": "builder",
        "lane": seat_events[0].payload["lane"],
        "argv": list(FAKE_SEAT_LAUNCH.argv),
        "set_environment": list(FAKE_SEAT_LAUNCH.set_environment),
        "removed_environment": list(FAKE_SEAT_LAUNCH.removed_environment),
        "in_process": False,
    }


def test_a_seat_environment_value_never_reaches_the_record_the_progress_or_the_report(
    tmp_path: Path,
) -> None:
    harness_commands = FakeCommandAdapter(
        identity("harness-command"),
        scripted_results=[
            seat_output(tmp_path, "builder", {"summary": "built", "report_ref": "r"}),
            seat_output(
                tmp_path,
                "reviewer",
                {
                    "decision": "pass",
                    "findings": [],
                    "evidence_ref": "review",
                    "inspected": [],
                    "acceptance": [],
                },
            ),
        ],
    )
    harness = SentinelClaudeAdapter(
        harness_commands, tmp_path / "harness", command=(sys.executable,),
        model_map={"builder-class": "builder-model", "reviewer-class": "reviewer-model"},
    )
    records_root = tmp_path / "runs"
    ports = make_ports([], [command()])
    progress = FakeProgressPort()
    ports = replace(
        ports,
        harness=harness,
        records=LocalRunRecordAdapter(records_root),
        progress=progress,
    )
    ports.tracker.queue.append(spec().item)
    brief = tmp_path / "brief.md"
    brief.write_text("# Brief\n", encoding="utf-8")

    CampaignRunner(ports).run(
        campaign(max_items=1), lambda selected: replace(spec(selected), brief_path=brief)
    )

    assert all(
        SENTINEL in dict(dispatched.environment).values()
        for dispatched in harness_commands.requests
    )
    recorded = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(records_root.rglob("*"))
        if path.is_file()
    )
    launched = [
        json.loads(line)
        for path in records_root.rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if '"seat.launched"' in line
    ]
    assert len(launched) == 2
    assert SENTINEL_NAME in recorded
    report = "\n".join(report.content for report in ports.workspace.reports)
    assert report
    progress_text = "\n".join(progress.lines)
    for text in (recorded, progress_text, report):
        assert SENTINEL not in text
