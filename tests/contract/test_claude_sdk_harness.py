"""The Agent SDK harness adapter against a fake SDK client; no provider runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from autobuild.adapters import ClaudeCodeHarnessAdapter
from autobuild.adapters.claude_sdk_harness import (
    ClaudeAgentSdkClient,
    ClaudeSdkHarnessAdapter,
    agent_options,
    message_record,
)
from autobuild.adapters.harness_cli import result_schema
from autobuild.bootstrap.composition import _REQUIRED_HARNESS_CAPABILITIES
from autobuild.domain import (
    AdapterIdentity,
    CapabilityError,
    CommandResult,
    EffortLevel,
    LaneSignalKind,
    ReviewDecision,
    Seat,
    SeatOutcome,
    SeatRequest,
    ToolPolicy,
    WorkspaceRef,
)
from autobuild.testing import FakeAgentSdkClient, FakeCommandAdapter

KEY_SENTINEL = "sk-ant-api03-U3-KEY-SENTINEL-0000"
REMOVED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN")


def identity() -> AdapterIdentity:
    return AdapterIdentity("command", "1")


def key_file(tmp_path: Path, content: str = KEY_SENTINEL) -> Path:
    path = tmp_path / "secrets" / "sdk.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def seat(tmp_path: Path, kind: Seat = Seat.BUILDER, tools=frozenset({"read", "write", "python"}), **changes) -> SeatRequest:
    workspace = WorkspaceRef(tmp_path / "worktree", "autobuild/item", "base", "lease")
    workspace.root.mkdir(exist_ok=True)
    contract = "builder-report-v1" if kind is Seat.BUILDER else "review-verdict-v1"
    request = SeatRequest(
        "run", "item", kind, "model-class", tmp_path / "brief.md", workspace,
        ToolPolicy(tools, (workspace.root,)), "Seat instructions", contract, 60,
    )
    return replace(request, **changes)


def result_record(**fields) -> dict:
    record = {
        "message": "ResultMessage",
        "subtype": "success",
        "is_error": False,
        "num_turns": 2,
        "result": "complete",
        "structured_output": None,
        "usage": {"input_tokens": 11, "output_tokens": 7},
        "total_cost_usd": 0.12,
        "api_error_status": None,
    }
    record.update(fields)
    return record


def adapter_for(tmp_path: Path, client: FakeAgentSdkClient, key: Path | None = None, commands=None):
    return ClaudeSdkHarnessAdapter(
        commands or FakeCommandAdapter(identity()),
        tmp_path / "harness",
        key if key is not None else key_file(tmp_path),
        model_map={"model-class": "model-id"},
        client=client,
    )


def cli_option(argv: tuple[str, ...], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_a_builder_seat_returns_the_typed_result_usage_and_model(tmp_path: Path) -> None:
    client = FakeAgentSdkClient(
        scripts=[
            [
                {"message": "SystemMessage", "subtype": "init", "data": {"apiKeySource": "apiKeyHelper"}},
                result_record(structured_output={"summary": "implemented", "report_ref": "x"}),
            ]
        ]
    )
    adapter = adapter_for(tmp_path, client)

    result = adapter.invoke(seat(tmp_path))

    assert result.outcome is SeatOutcome.SUCCEEDED
    assert result.payload is not None and result.payload.summary == "implemented"
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7
    assert result.usage.cost == 0.12
    assert adapter.collect_usage(result.run_ref) == result.usage
    assert result.exit_code == 0
    assert result.model == "model-id"
    assert Path(result.raw_output_ref).is_file()


def test_a_reviewer_seat_returns_the_typed_verdict(tmp_path: Path) -> None:
    verdict = {"decision": "pass", "findings": [], "evidence_ref": "review"}
    client = FakeAgentSdkClient(scripts=[[result_record(structured_output=verdict)]])

    result = adapter_for(tmp_path, client).invoke(seat(tmp_path, Seat.REVIEWER, frozenset({"read"})))

    assert result.payload is not None and result.payload.decision is ReviewDecision.PASS


@pytest.mark.parametrize(
    "kind, tools",
    [(Seat.BUILDER, frozenset({"read", "write", "shell", "python", "git"})), (Seat.REVIEWER, frozenset({"read"}))],
)
def test_the_seat_carries_the_claude_code_seat_contract(tmp_path: Path, kind, tools) -> None:
    client = FakeAgentSdkClient(scripts=[[result_record(structured_output={"summary": "s", "report_ref": "r", "decision": "pass", "findings": [], "evidence_ref": "e"})]])
    request = seat(tmp_path, kind, tools, effort=EffortLevel.HIGH)
    cli = ClaudeCodeHarnessAdapter(
        FakeCommandAdapter(identity()), tmp_path / "cli", command=(sys.executable,),
        model_map={"model-class": "model-id"},
    )
    argv, _extra, _stdin = cli._invocation(request, "run:item:cli")

    adapter_for(tmp_path, client).invoke(request)

    spec = client.specs[0]
    assert ",".join(spec.tools) == cli_option(argv, "--tools")
    assert ",".join(spec.allowed_tools) == cli_option(argv, "--allowedTools")
    assert spec.append_system_prompt == cli_option(argv, "--append-system-prompt")
    assert spec.json_schema == result_schema(request.result_contract)
    assert spec.model == "model-id"
    assert spec.effort == "high"
    assert spec.permission_mode == "dontAsk"
    assert spec.setting_sources == ()
    assert set(spec.flags) == {"safe-mode", "no-session-persistence"}
    assert spec.prompt == "Seat instructions"
    assert spec.cwd == request.workspace.root


@dataclass
class _Options:
    kwargs: dict


class _StubSdk:
    """Stands in for the SDK module so the option mapping is checked by name."""

    @staticmethod
    def ClaudeAgentOptions(**kwargs):
        return _Options(kwargs)


def test_the_sdk_options_load_no_setting_sources_and_the_cli_tool_allow_list(tmp_path: Path) -> None:
    client = FakeAgentSdkClient(scripts=[[result_record(structured_output={"summary": "s", "report_ref": "r"})]])
    request = seat(tmp_path)
    cli = ClaudeCodeHarnessAdapter(
        FakeCommandAdapter(identity()), tmp_path / "cli", command=(sys.executable,),
        model_map={"model-class": "model-id"},
    )
    argv, _extra, _stdin = cli._invocation(request, "run:item:cli")
    adapter_for(tmp_path, client).invoke(request)

    options = agent_options(_StubSdk, client.specs[0]).kwargs

    assert options["setting_sources"] == []
    assert options["tools"] == cli_option(argv, "--tools").split(",")
    assert options["allowed_tools"] == cli_option(argv, "--allowedTools").split(",")
    assert options["system_prompt"] == {
        "type": "preset",
        "preset": "claude_code",
        "append": cli_option(argv, "--append-system-prompt"),
    }
    assert options["output_format"] == {"type": "json_schema", "schema": result_schema("builder-report-v1")}
    assert options["permission_mode"] == "dontAsk"
    assert options["extra_args"] == {"safe-mode": None, "no-session-persistence": None}
    settings = json.loads(options["settings"])
    assert settings["disableAllHooks"] is True
    assert key_file(tmp_path).as_posix() in settings["apiKeyHelper"]
    assert KEY_SENTINEL not in options["settings"]


def test_the_child_gets_a_fresh_empty_config_dir_and_no_billing_or_redirect_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in REMOVED:
        monkeypatch.setenv(name, f"PARENT-{name}")
    seen: list[list[str]] = []

    class Inspecting(FakeAgentSdkClient):
        def run(self, spec, on_message, on_stderr, cancelled):
            seen.append(sorted(p.name for p in Path(dict(spec.environment)["CLAUDE_CONFIG_DIR"]).iterdir()))
            super().run(spec, on_message, on_stderr, cancelled)

    client = Inspecting(scripts=[[result_record(structured_output={"summary": "s", "report_ref": "r"})]] * 2)
    adapter = adapter_for(tmp_path, client)

    adapter.invoke(seat(tmp_path))
    adapter.invoke(seat(tmp_path))

    first, second = (dict(spec.environment) for spec in client.specs)
    assert seen == [[], []]
    assert first["CLAUDE_CONFIG_DIR"] != second["CLAUDE_CONFIG_DIR"]
    assert Path(first["CLAUDE_CONFIG_DIR"]).is_relative_to((tmp_path / "harness").resolve())
    for name in REMOVED:
        assert first[name] == ""
    assert first["DISABLE_TELEMETRY"] == "1"
    assert all(KEY_SENTINEL not in value for value in first.values())


def test_a_missing_key_file_fails_the_probe_without_reading_any_oauth_credential(tmp_path: Path) -> None:
    commands = FakeCommandAdapter(identity())
    client = FakeAgentSdkClient()
    adapter = adapter_for(tmp_path, client, key=tmp_path / "secrets" / "absent.key", commands=commands)

    probe = adapter.probe()

    assert probe.available is False
    assert "API key file is missing" in probe.diagnostics[0]
    assert commands.requests == []
    assert client.version_calls == 0

    result = adapter.invoke(seat(tmp_path))

    assert result.outcome is SeatOutcome.FAILED
    assert client.specs == []
    signal = adapter.classify_failure(result)
    assert signal is not None and signal.kind is LaneSignalKind.AUTH


def test_an_empty_key_file_or_a_lane_without_one_fails_the_probe(tmp_path: Path) -> None:
    empty = adapter_for(tmp_path, FakeAgentSdkClient(), key=key_file(tmp_path, ""))
    unset = ClaudeSdkHarnessAdapter(FakeCommandAdapter(identity()), tmp_path / "h2", None, client=FakeAgentSdkClient())

    assert "empty" in empty.probe().diagnostics[0]
    assert "sets no key_file" in unset.probe().diagnostics[0]


def test_a_missing_sdk_package_fails_the_probe_and_names_the_extra(tmp_path: Path) -> None:
    probe = adapter_for(tmp_path, FakeAgentSdkClient(installed=False)).probe()

    assert probe.available is False
    assert "autobuild-factory[sdk]" in probe.diagnostics[0]


def test_a_ready_probe_reports_the_sdk_and_runs_no_command(tmp_path: Path) -> None:
    commands = FakeCommandAdapter(identity())
    adapter = adapter_for(tmp_path, FakeAgentSdkClient(sdk_version="9.9"), commands=commands)

    probe = adapter.probe()

    assert probe.available is True
    assert probe.identity is not None and probe.identity.name == "claude-sdk"
    assert probe.identity.version == "9.9"
    assert _REQUIRED_HARNESS_CAPABILITIES <= adapter.capabilities
    assert commands.requests == []


def test_the_lane_declares_no_meter(tmp_path: Path) -> None:
    adapter = adapter_for(tmp_path, FakeAgentSdkClient())

    assert adapter.meter_id is None
    assert adapter.meter_scope_covers("Fable", "model-class") is False


def test_the_key_never_reaches_a_record_file_or_the_cli_lane_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY_SENTINEL)
    client = FakeAgentSdkClient(scripts=[[result_record(structured_output={"summary": "s", "report_ref": "r"})]])
    adapter = adapter_for(tmp_path, client)

    result = adapter.invoke(seat(tmp_path))

    written = [path for path in (tmp_path / "harness").rglob("*") if path.is_file()]
    assert written
    assert all(KEY_SENTINEL not in path.read_text(encoding="utf-8", errors="replace") for path in written)
    assert KEY_SENTINEL not in repr(result)
    assert KEY_SENTINEL not in repr(client.specs)

    stdout = tmp_path / "cli-stdout.txt"
    stdout.write_text(json.dumps({"structured_output": {"summary": "s", "report_ref": "r"}}), encoding="utf-8")
    commands = FakeCommandAdapter(
        identity(), scripted_results=[CommandResult("c", 0, str(stdout), str(stdout), "s", "e")]
    )
    ClaudeCodeHarnessAdapter(commands, tmp_path / "cli", command=(sys.executable,)).invoke(seat(tmp_path))
    dispatched = commands.requests[0]
    assert all(KEY_SENTINEL not in value for _name, value in dispatched.environment)
    assert "ANTHROPIC_API_KEY" in dispatched.remove_environment


def test_a_seat_past_its_timeout_is_stopped_and_cools_nothing(tmp_path: Path) -> None:
    client = FakeAgentSdkClient(block_until_cancelled=True)
    adapter = adapter_for(tmp_path, client)

    result = adapter.invoke(seat(tmp_path, timeout_seconds=0.3))

    assert result.outcome is SeatOutcome.TIMED_OUT
    assert adapter.classify_failure(result) is None


def test_a_seat_without_progress_past_its_stall_deadline_is_stalled(tmp_path: Path) -> None:
    adapter = adapter_for(tmp_path, FakeAgentSdkClient(block_until_cancelled=True))

    result = adapter.invoke(
        seat(tmp_path, progress_deadline_seconds=0.3, progress_digest=lambda: "unchanged")
    )

    assert result.outcome is SeatOutcome.STALLED


def test_cancel_stops_a_running_seat(tmp_path: Path) -> None:
    adapter = adapter_for(tmp_path, FakeAgentSdkClient(block_until_cancelled=True))
    timer = threading.Timer(0.3, adapter.cancel, args=("run:item:builder:claude-sdk",))
    timer.start()

    result = adapter.invoke(seat(tmp_path))
    timer.join()

    assert result.outcome is SeatOutcome.CANCELLED


def test_a_seat_option_the_sdk_cannot_carry_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = adapter_for(tmp_path, FakeAgentSdkClient())
    original = ClaudeCodeHarnessAdapter._invocation

    def widened(self, request, run_ref):
        argv, extra, stdin_ref = original(self, request, run_ref)
        return (*argv, "--dangerously-skip-permissions"), extra, stdin_ref

    monkeypatch.setattr(ClaudeCodeHarnessAdapter, "_invocation", widened)

    with pytest.raises(CapabilityError, match="cannot carry the seat option --dangerously-skip-permissions"):
        adapter.invoke(seat(tmp_path))


def test_the_seat_transport_is_in_process_and_carries_the_child_environment(tmp_path: Path) -> None:
    adapter = adapter_for(tmp_path, FakeAgentSdkClient())

    transport = adapter.seat_transport(seat(tmp_path, instructions="x" * 200_000))

    environment = dict(transport.child_environment)
    assert transport.in_process is True
    assert transport.delivered_by_file is True
    assert transport.max_argv_element < 8_192
    assert environment["DO_NOT_TRACK"] == "1"
    assert all(environment[name] == "" for name in REMOVED)


def test_a_dataclass_message_becomes_a_plain_record() -> None:
    @dataclass
    class TextBlock:
        text: str

    @dataclass
    class AssistantMessage:
        content: list
        model: str
        error: str | None = None

    record = message_record(AssistantMessage([TextBlock("hi")], "m", "billing_error"))

    assert record == {
        "message": "AssistantMessage",
        "content": [{"block": "TextBlock", "text": "hi"}],
        "model": "m",
        "error": "billing_error",
    }


def test_a_refused_key_stops_the_seat_on_the_first_retry_and_cools_with_auth(tmp_path: Path) -> None:
    retry = {
        "message": "SystemMessage",
        "subtype": "api_retry",
        "data": {"error": "authentication_failed", "error_status": 401, "attempt": 1},
    }
    client = FakeAgentSdkClient(scripts=[[retry]], block_until_cancelled=True)
    adapter = adapter_for(tmp_path, client)

    result = adapter.invoke(seat(tmp_path, timeout_seconds=30))

    assert client.cancelled_runs == 1
    assert result.outcome is SeatOutcome.FAILED
    signal = adapter.classify_failure(result)
    assert signal is not None and signal.kind is LaneSignalKind.AUTH


def test_a_retry_for_another_error_does_not_stop_the_seat(tmp_path: Path) -> None:
    retry = {"message": "SystemMessage", "subtype": "api_retry", "data": {"error": "server_error", "error_status": 529}}
    client = FakeAgentSdkClient(
        scripts=[[retry, result_record(structured_output={"summary": "s", "report_ref": "r"})]]
    )

    result = adapter_for(tmp_path, client).invoke(seat(tmp_path))

    assert result.outcome is SeatOutcome.SUCCEEDED


def _run_helper(adapter: ClaudeSdkHarnessAdapter) -> subprocess.CompletedProcess:
    environment = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    return subprocess.run(adapter._key_helper(), shell=True, capture_output=True, env=environment, check=False)


def test_the_key_helper_prints_a_key_saved_with_a_byte_order_mark(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "bom.key"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbf" + KEY_SENTINEL.encode("utf-8") + b"\r\n")
    plain = adapter_for(tmp_path / "plain", FakeAgentSdkClient(), key=key_file(tmp_path / "plain"))
    bom = adapter_for(tmp_path, FakeAgentSdkClient(), key=path)

    for adapter in (plain, bom):
        completed = _run_helper(adapter)
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == KEY_SENTINEL.encode("utf-8")
    assert bom.probe().available is True


@pytest.mark.parametrize("content", [b"", b"\xef\xbb\xbf", b"  \r\n", b"\xff\xfe\x00bad"])
def test_a_key_file_empty_or_unreadable_after_stripping_fails_the_probe(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "secrets" / "sdk.key"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    probe = adapter_for(tmp_path, FakeAgentSdkClient(), key=path).probe()

    assert probe.available is False
    assert "empty" in probe.diagnostics[0] or "unreadable" in probe.diagnostics[0]


def test_the_seat_settings_deny_reading_the_key_file(tmp_path: Path) -> None:
    client = FakeAgentSdkClient(scripts=[[result_record(structured_output={"summary": "s", "report_ref": "r"})]])
    adapter_for(tmp_path, client).invoke(seat(tmp_path))

    settings = json.loads(agent_options(_StubSdk, client.specs[0]).kwargs["settings"])

    key = key_file(tmp_path).resolve().as_posix()
    assert f"Read({key})" in settings["permissions"]["deny"]
    assert f"Read(//{key.lstrip('/')})" in settings["permissions"]["deny"]


def test_the_sdk_extra_is_bound_to_the_exercised_release() -> None:
    project = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["optional-dependencies"]["sdk"] == ["claude-agent-sdk>=0.2.168,<0.2.169"]
    assert project["project"]["dependencies"] == []


def test_the_real_client_raises_import_error_without_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    with pytest.raises(ImportError):
        ClaudeAgentSdkClient().version()
