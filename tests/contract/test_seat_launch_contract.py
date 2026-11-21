"""The launch a harness adapter reports for a seat: the exact vector it runs
and the names, never the values, of the environment it sets and removes."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

from autobuild.adapters import ClaudeCodeHarnessAdapter
from autobuild.adapters.claude_sdk_harness import ClaudeSdkHarnessAdapter
from autobuild.domain import (
    AdapterIdentity,
    CommandResult,
    Seat,
    SeatRequest,
    ToolPolicy,
    WorkspaceRef,
)
from autobuild.domain.models import SeatLaunch
from autobuild.testing import FakeAgentSdkClient, FakeCommandAdapter

SDK_REMOVED = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


def identity() -> AdapterIdentity:
    return AdapterIdentity("command", "1", frozenset({"test"}))


def command_result(tmp_path: Path, name: str, stdout: str) -> CommandResult:
    out = tmp_path / f"{name}-stdout.txt"
    err = tmp_path / f"{name}-stderr.txt"
    out.write_text(stdout, encoding="utf-8")
    err.write_text("", encoding="utf-8")
    return CommandResult(name, 0, str(out), str(err), "start", "end")


def builder_stdout() -> str:
    return json.dumps(
        {"result": "complete", "structured_output": {"summary": "built", "report_ref": "r"}}
    )


def seat_request(tmp_path: Path, **changes) -> SeatRequest:
    workspace = WorkspaceRef(tmp_path / "worktree", "autobuild/item", "base", "lease")
    workspace.root.mkdir(exist_ok=True)
    request = SeatRequest(
        "run", "item", Seat.BUILDER, "model-class", tmp_path / "brief.md", workspace,
        ToolPolicy(frozenset({"read", "write", "python"}), (workspace.root,)),
        "Seat instructions", "builder-report-v1", 60,
    )
    return replace(request, **changes)


def test_the_cli_adapter_reports_the_vector_and_names_it_hands_the_command_port(
    tmp_path: Path,
) -> None:
    commands = FakeCommandAdapter(
        identity(), scripted_results=[command_result(tmp_path, "invoke", builder_stdout())]
    )
    adapter = ClaudeCodeHarnessAdapter(
        commands, tmp_path / "harness", command=(sys.executable,),
        model_map={"model-class": "model-id"},
    )
    launches: list[SeatLaunch] = []

    adapter.invoke(seat_request(tmp_path, on_launch=launches.append))

    (dispatched,) = commands.requests
    assert launches == [
        SeatLaunch(
            argv=dispatched.argv,
            set_environment=tuple(sorted(name for name, _ in dispatched.environment)),
            removed_environment=dispatched.remove_environment,
        )
    ]
    assert dispatched.remove_environment
    assert dispatched.environment


def test_the_sdk_lane_reports_its_seat_vector_and_the_names_it_blanks(tmp_path: Path) -> None:
    key = tmp_path / "secrets" / "sdk.key"
    key.parent.mkdir(parents=True)
    key.write_text("sk-ant-api03-LAUNCH-KEY-0000", encoding="utf-8")
    launches: list[SeatLaunch] = []

    class Inspecting(FakeAgentSdkClient):
        def run(self, spec, on_message, on_stderr, cancelled):
            assert launches, "the launch is reported before the SDK client runs"
            super().run(spec, on_message, on_stderr, cancelled)

    client = Inspecting(
        scripts=[[{
            "message": "ResultMessage", "subtype": "success", "is_error": False,
            "num_turns": 1, "result": "complete",
            "structured_output": {"summary": "s", "report_ref": "r"},
            "usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.0,
            "api_error_status": None,
        }]]
    )
    adapter = ClaudeSdkHarnessAdapter(
        FakeCommandAdapter(identity()), tmp_path / "harness", key,
        model_map={"model-class": "model-id"}, client=client,
    )

    adapter.invoke(seat_request(tmp_path, on_launch=launches.append))

    (launch,) = launches
    (spec,) = client.specs
    environment = dict(spec.environment)
    assert launch.in_process is True
    assert launch.removed_environment == SDK_REMOVED
    assert not set(launch.set_environment) & set(SDK_REMOVED)
    assert set(launch.set_environment) == set(environment) - set(SDK_REMOVED)
    assert "CLAUDE_CONFIG_DIR" in launch.set_environment
    assert launch.argv[launch.argv.index("--session-id") + 1] == spec.session_id
    assert launch.argv[launch.argv.index("--model") + 1] == "model-id"
    payload = json.dumps(
        [launch.argv, launch.set_environment, launch.removed_environment]
    )
    assert "apiKeyHelper" not in payload
    assert key.resolve().as_posix() not in payload
    assert str(key.resolve()) not in payload
    for value in environment.values():
        if len(value) > 3:
            assert value not in payload
