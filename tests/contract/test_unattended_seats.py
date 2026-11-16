"""Every seat the application starts runs unattended: ``CLAUDE_UNATTENDED=1``
reaches its environment for every seat kind, every harness lane and both
process modes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from autobuild.adapters import (
    ClaudeCodeHarnessAdapter,
    CodexHarnessAdapter,
    CopilotCliHarnessAdapter,
)
from autobuild.adapters.claude_sdk_harness import ClaudeSdkHarnessAdapter
from autobuild.application.prompts import render_seat_instructions
from autobuild.domain import (
    AdapterIdentity,
    CommandResult,
    DeliveryMode,
    ItemExecutionSpec,
    Seat,
    SeatRequest,
    ToolPolicy,
    WorkItem,
    WorkspaceRef,
)
from autobuild.domain.models import ProcessMode, ProcessPolicy, TestCoverage
from autobuild.testing import FakeAgentSdkClient, FakeCommandAdapter

CLI_ADAPTERS = (ClaudeCodeHarnessAdapter, CodexHarnessAdapter, CopilotCliHarnessAdapter)
SEATS = (Seat.BUILDER, Seat.REVIEWER, Seat.SPECIALIST)
MODES = (
    ProcessPolicy(),
    ProcessPolicy(
        ProcessMode.STRICT,
        ("uv", "run", "pytest"),
        (TestCoverage("src/**", tests=("tests/unit",)),),
    ),
)


def execution_spec(process: ProcessPolicy) -> ItemExecutionSpec:
    return ItemExecutionSpec(
        WorkItem("item", "title", "brief.md", ("passes",)),
        Path("brief.md"),
        "validator",
        ("uv", "run", "pytest"),
        ToolPolicy(frozenset({"read"}), (Path("/worktree"),)),
        "builder",
        "reviewer",
        "specialist",
        delivery_mode=DeliveryMode.PROTECTED_DEFAULT,
        process=process,
    )


def seat_request(tmp_path: Path, seat: Seat, process: ProcessPolicy) -> SeatRequest:
    workspace = WorkspaceRef(tmp_path / "worktree", "autobuild/item", "base", "lease")
    workspace.root.mkdir(exist_ok=True)
    tools = frozenset({"read", "write", "python"}) if seat is Seat.BUILDER else frozenset({"read"})
    contract = "builder-report-v1" if seat is Seat.BUILDER else "review-verdict-v1"
    return SeatRequest(
        "run",
        "item",
        seat,
        "model-class",
        tmp_path / "brief.md",
        workspace,
        ToolPolicy(tools, (workspace.root,)),
        render_seat_instructions(execution_spec(process), seat, ()),
        contract,
        60,
    )


def failed_command(tmp_path: Path) -> CommandResult:
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return CommandResult("invoke", 1, str(stdout), str(stderr), "start", "end")


@pytest.mark.parametrize("process", MODES, ids=("flow", "strict"))
@pytest.mark.parametrize("seat", SEATS, ids=lambda seat: seat.value)
@pytest.mark.parametrize("adapter_class", CLI_ADAPTERS, ids=lambda cls: cls.adapter_name)
def test_every_cli_lane_seat_runs_unattended(
    tmp_path: Path, adapter_class, seat: Seat, process: ProcessPolicy
) -> None:
    commands = FakeCommandAdapter(
        AdapterIdentity("command", "1"), scripted_results=[failed_command(tmp_path)]
    )
    adapter = adapter_class(commands, tmp_path / "harness", command=(sys.executable,))

    adapter.invoke(seat_request(tmp_path, seat, process))

    assert ("CLAUDE_UNATTENDED", "1") in commands.requests[0].environment


@pytest.mark.parametrize("process", MODES, ids=("flow", "strict"))
@pytest.mark.parametrize("seat", SEATS, ids=lambda seat: seat.value)
def test_every_sdk_lane_seat_runs_unattended(
    tmp_path: Path, seat: Seat, process: ProcessPolicy
) -> None:
    key = tmp_path / "sdk.key"
    key.write_text("key", encoding="utf-8")
    payload = (
        {"summary": "done", "report_ref": "r"}
        if seat is Seat.BUILDER
        else {"decision": "pass", "findings": [], "evidence_ref": "e"}
    )
    client = FakeAgentSdkClient(
        scripts=[
            [
                {
                    "message": "ResultMessage",
                    "subtype": "success",
                    "is_error": False,
                    "result": json.dumps(payload),
                    "structured_output": payload,
                }
            ]
        ]
    )
    adapter = ClaudeSdkHarnessAdapter(
        FakeCommandAdapter(AdapterIdentity("command", "1")),
        tmp_path / "harness",
        key,
        client=client,
    )
    adapter.invoke(seat_request(tmp_path, seat, process))

    assert dict(client.specs[0].environment)["CLAUDE_UNATTENDED"] == "1"
