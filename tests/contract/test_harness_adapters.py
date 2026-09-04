from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from autobuild.adapters import (
    ClaudeCodeHarnessAdapter,
    CodexHarnessAdapter,
    CopilotCliHarnessAdapter,
)
from autobuild.domain import (
    AdapterIdentity,
    CapabilityError,
    CommandResult,
    EvidenceError,
    LaneSignalKind,
    ReviewDecision,
    Seat,
    SeatOutcome,
    SeatRequest,
    SeatResult,
    SeatUsage,
    ToolPolicy,
    WorkspaceRef,
)
from autobuild.testing import FakeCommandAdapter
from autobuild.adapters.harness_cli import result_schema


def identity() -> AdapterIdentity:
    return AdapterIdentity("command", "1", frozenset({"test"}))


def output(tmp_path: Path, name: str, content: str) -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def command_result(tmp_path: Path, name: str, stdout: str, exit_code: int = 0) -> CommandResult:
    return CommandResult(
        name,
        exit_code,
        output(tmp_path, f"{name}-stdout.txt", stdout),
        output(tmp_path, f"{name}-stderr.txt", ""),
        "start",
        "end",
    )


def request(tmp_path: Path, seat: Seat, contract: str, tools: frozenset[str]) -> SeatRequest:
    workspace = WorkspaceRef(tmp_path / "worktree", "autobuild/item", "base", "lease")
    workspace.root.mkdir(exist_ok=True)
    return SeatRequest(
        "run",
        "item",
        seat,
        "model-class",
        tmp_path / "brief.md",
        workspace,
        ToolPolicy(tools, (workspace.root,)),
        "Seat instructions",
        contract,
        60,
    )


def prompt_from(dispatched) -> str:
    if dispatched.stdin_ref is not None:
        return Path(dispatched.stdin_ref).read_text(encoding="utf-8")
    for value in dispatched.argv:
        if value.startswith("--prompt="):
            return value.removeprefix("--prompt=")
    return dispatched.argv[-1]


def test_claude_normalises_structured_builder_result_and_usage(tmp_path: Path) -> None:
    stdout = json.dumps(
        {
            "result": "complete",
            "structured_output": {"summary": "implemented", "report_ref": "ignored"},
            "usage": {"input_tokens": 11, "output_tokens": 7},
            "total_cost_usd": 0.12,
        }
    )
    commands = FakeCommandAdapter(
        identity(), scripted_results=[command_result(tmp_path, "invoke", stdout)]
    )
    adapter = ClaudeCodeHarnessAdapter(
        commands,
        tmp_path / "harness",
        command=(sys.executable,),
        model_map={"model-class": "model-id"},
    )

    result = adapter.invoke(request(tmp_path, Seat.BUILDER, "builder-report-v1", frozenset({"read", "write", "python"})))

    assert result.outcome is SeatOutcome.SUCCEEDED
    assert result.payload is not None and result.payload.summary == "implemented"
    assert result.payload.report_ref.endswith(".json")
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7
    assert result.usage.cost == 0.12
    assert result.exit_code == 0
    assert result.model == "model-id"
    argv = commands.requests[0].argv
    assert "--json-schema" in argv
    assert "model-id" in argv
    assert "Bash(python *)" in argv[argv.index("--allowedTools") + 1]


def test_codex_uses_read_only_sandbox_for_review_and_normalises_jsonl(tmp_path: Path) -> None:
    verdict = json.dumps({"decision": "pass", "findings": [], "evidence_ref": "review"})
    stdout = json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": verdict}}
    ) + "\n" + json.dumps(
        {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5}}
    )
    commands = FakeCommandAdapter(
        identity(), scripted_results=[command_result(tmp_path, "invoke", stdout)]
    )
    adapter = CodexHarnessAdapter(commands, tmp_path / "harness", command=(sys.executable,))

    result = adapter.invoke(request(tmp_path, Seat.REVIEWER, "review-verdict-v1", frozenset({"read"})))

    assert result.outcome is SeatOutcome.SUCCEEDED
    assert result.payload is not None and result.payload.decision is ReviewDecision.PASS
    argv = commands.requests[0].argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert "--ephemeral" in argv
    assert "--ignore-rules" in argv
    assert "--output-schema" in argv
    assert argv[-1] == "-"
    stdin_ref = commands.requests[0].stdin_ref
    assert stdin_ref is not None
    assert Path(stdin_ref).read_text(encoding="utf-8") == "Seat instructions"


def test_codex_streams_materialised_large_evidence_instead_of_argv(tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    evidence = tmp_path / "changes.patch"
    brief.write_text("APPROVED-BRIEF-CONTENT", encoding="utf-8")
    evidence.write_text("D" * 200_000, encoding="utf-8")
    verdict = json.dumps({"decision": "pass", "findings": [], "evidence_ref": "review"})
    commands = FakeCommandAdapter(
        identity(),
        scripted_results=[
            command_result(
                tmp_path,
                "invoke",
                json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": verdict}}
                ),
            )
        ],
    )
    adapter = CodexHarnessAdapter(commands, tmp_path / "harness", command=(sys.executable,))
    seat = request(tmp_path, Seat.REVIEWER, "review-verdict-v1", frozenset({"read"}))
    seat = SeatRequest(
        seat.run_id,
        seat.item_id,
        seat.seat,
        seat.model_class,
        brief,
        seat.workspace,
        seat.tool_policy,
        "REVIEW-INSTRUCTIONS",
        seat.result_contract,
        seat.timeout_seconds,
        (str(evidence),),
    )

    adapter.invoke(seat)

    dispatched = commands.requests[0]
    assert dispatched.argv[-1] == "-"
    assert max(len(argument) for argument in dispatched.argv) < 5_000
    assert dispatched.stdin_ref is not None
    prompt = Path(dispatched.stdin_ref).read_text(encoding="utf-8")
    assert "APPROVED-BRIEF-CONTENT" in prompt
    assert "D" * 200_000 in prompt


def test_copilot_uses_programmatic_json_mode_and_confined_permissions(tmp_path: Path) -> None:
    verdict = {
        "decision": "correct",
        "findings": [
            {
                "code": "BUG",
                "consequence": "wrong result",
                "evidence_ref": "file:line",
                "blocking": True,
            }
        ],
        "evidence_ref": "review",
    }
    stdout = json.dumps(
        {"type": "assistant.message", "data": {"content": json.dumps(verdict)}}
    )
    commands = FakeCommandAdapter(
        identity(), scripted_results=[command_result(tmp_path, "invoke", stdout)]
    )
    adapter = CopilotCliHarnessAdapter(commands, tmp_path / "harness", command=(sys.executable,))

    result = adapter.invoke(request(tmp_path, Seat.REVIEWER, "review-verdict-v1", frozenset({"read"})))

    assert result.payload is not None and result.payload.decision is ReviewDecision.CORRECT
    argv = commands.requests[0].argv
    assert not any(value.startswith("--prompt=") for value in argv)
    assert argv[argv.index("--prompt") + 1] == "-"
    stdin_ref = commands.requests[0].stdin_ref
    assert stdin_ref is not None
    assert Path(stdin_ref).read_text(encoding="utf-8") == "Seat instructions"
    assert "--output-format=json" in argv
    assert "--disallow-temp-dir" in argv
    assert "--no-ask-user" in argv
    assert "--available-tools=glob,grep,view" in argv
    assert "--allow-tool=read" in argv


def test_cli_failure_is_a_normalised_failed_seat(tmp_path: Path) -> None:
    commands = FakeCommandAdapter(
        identity(), scripted_results=[command_result(tmp_path, "invoke", "", exit_code=2)]
    )
    adapter = CodexHarnessAdapter(commands, tmp_path / "harness", command=(sys.executable,))

    result = adapter.invoke(request(tmp_path, Seat.BUILDER, "builder-report-v1", frozenset({"read", "write"})))

    assert result.outcome is SeatOutcome.FAILED
    assert result.payload is None
    assert result.exit_code == 2


def test_probe_checks_version_and_authentication_without_invoking_a_model(tmp_path: Path) -> None:
    commands = FakeCommandAdapter(
        identity(),
        scripted_results=[
            command_result(tmp_path, "version", "2.1.237"),
            command_result(tmp_path, "auth", '{"loggedIn":true}'),
        ],
    )
    adapter = ClaudeCodeHarnessAdapter(commands, tmp_path / "harness", command=(sys.executable,))

    probe = adapter.probe()

    assert probe.available is True
    assert probe.identity is not None and probe.identity.name == "claude-code"
    assert len(commands.requests) == 2


def test_copilot_probe_reports_a_missing_github_cli_without_dispatching_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "autobuild.adapters.copilot_harness.shutil.which", lambda _: None
    )
    commands = FakeCommandAdapter(
        identity(), scripted_results=[command_result(tmp_path, "version", "1.0")]
    )
    adapter = CopilotCliHarnessAdapter(
        commands, tmp_path / "harness", command=(sys.executable,)
    )

    probe = adapter.probe()

    assert probe.available is False
    assert probe.diagnostics == ("gh executable was not found",)
    assert len(commands.requests) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows npm shim resolution proof")
def test_claude_prefers_the_native_binary_behind_the_windows_npm_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm = tmp_path / "npm"
    native = npm / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    native.parent.mkdir(parents=True)
    native.touch()
    shim = npm / "claude.cmd"
    shim.touch()
    monkeypatch.setattr("autobuild.adapters.claude_harness.shutil.which", lambda _: str(shim))
    commands = FakeCommandAdapter(identity())

    adapter = ClaudeCodeHarnessAdapter(commands, tmp_path / "harness")

    assert adapter._command == (str(native),)


def test_unknown_semantic_tool_is_refused_before_cli_dispatch(tmp_path: Path) -> None:
    commands = FakeCommandAdapter(identity())
    adapter = CopilotCliHarnessAdapter(commands, tmp_path / "harness", command=(sys.executable,))

    with pytest.raises(CapabilityError, match="cannot map semantic tools"):
        adapter.invoke(request(tmp_path, Seat.BUILDER, "builder-report-v1", frozenset({"database"})))

    assert commands.requests == []


@pytest.mark.parametrize(
    "adapter_class",
    [ClaudeCodeHarnessAdapter, CodexHarnessAdapter, CopilotCliHarnessAdapter],
)
def test_large_instruction_reaches_the_seat_by_file_not_argv(
    tmp_path: Path, adapter_class
) -> None:
    commands = FakeCommandAdapter(identity())
    adapter = adapter_class(commands, tmp_path / "harness", command=(sys.executable,))
    seat = request(tmp_path, Seat.BUILDER, "builder-report-v1", frozenset({"read", "write"}))
    seat = SeatRequest(
        seat.run_id,
        seat.item_id,
        seat.seat,
        seat.model_class,
        seat.brief_path,
        seat.workspace,
        seat.tool_policy,
        "z" * 200_000,
        seat.result_contract,
        seat.timeout_seconds,
    )

    argv, _extra, stdin_ref = adapter._invocation(seat, "run:item:builder")

    assert stdin_ref is not None
    assert Path(stdin_ref).read_text(encoding="utf-8") == "z" * 200_000
    assert max(len(part.encode("utf-8")) for part in argv) < 8_192
    assert not any("z" * 8_192 in part for part in argv)


def seat_result(
    tmp_path: Path,
    name: str,
    stdout: str,
    outcome: SeatOutcome,
    exit_code: int | None,
) -> SeatResult:
    ref = output(tmp_path, f"{name}-raw.txt", stdout)
    return SeatResult(
        name, outcome, None, ref, SeatUsage(source="test"), "start", "end", exit_code=exit_code
    )


@pytest.mark.parametrize(
    "adapter_class, limit_document, kind",
    [
        (
            ClaudeCodeHarnessAdapter,
            {"type": "result", "is_error": True, "error": {"type": "rate_limit_error"}},
            LaneSignalKind.RATE_LIMIT,
        ),
        (
            CodexHarnessAdapter,
            {"type": "error", "error": {"type": "rate_limit_exceeded"}},
            LaneSignalKind.RATE_LIMIT,
        ),
        (
            CopilotCliHarnessAdapter,
            {"type": "error", "error": {"code": "quota_exceeded"}},
            LaneSignalKind.QUOTA,
        ),
    ],
)
def test_structured_limit_response_classifies_per_adapter(
    tmp_path: Path, adapter_class, limit_document, kind
) -> None:
    commands = FakeCommandAdapter(identity())
    adapter = adapter_class(commands, tmp_path / "harness", command=(sys.executable,))
    failed = seat_result(
        tmp_path, "limit", json.dumps(limit_document), SeatOutcome.FAILED, exit_code=1
    )

    signal = adapter.classify_failure(failed)

    assert signal is not None
    assert signal.kind is kind


@pytest.mark.parametrize(
    "adapter_class, prose_document",
    [
        (
            ClaudeCodeHarnessAdapter,
            {"type": "result", "is_error": False, "result": "we hit a rate limit but recovered"},
        ),
        (
            CodexHarnessAdapter,
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "the rate limit is documented in the brief"},
            },
        ),
        (
            CopilotCliHarnessAdapter,
            {"type": "assistant.message", "data": {"content": "quota exceeded is discussed here"}},
        ),
    ],
)
def test_prose_mentioning_a_limit_does_not_classify(
    tmp_path: Path, adapter_class, prose_document
) -> None:
    commands = FakeCommandAdapter(identity())
    adapter = adapter_class(commands, tmp_path / "harness", command=(sys.executable,))
    succeeded = seat_result(
        tmp_path, "prose", json.dumps(prose_document), SeatOutcome.SUCCEEDED, exit_code=0
    )

    assert adapter.classify_failure(succeeded) is None


@pytest.mark.parametrize(
    "adapter_class",
    [ClaudeCodeHarnessAdapter, CodexHarnessAdapter, CopilotCliHarnessAdapter],
)
def test_a_failed_seat_without_structured_output_is_a_spawn_signal(
    tmp_path: Path, adapter_class
) -> None:
    commands = FakeCommandAdapter(identity())
    adapter = adapter_class(commands, tmp_path / "harness", command=(sys.executable,))
    spawn = seat_result(tmp_path, "spawn", "", SeatOutcome.FAILED, exit_code=127)

    signal = adapter.classify_failure(spawn)

    assert signal is not None
    assert signal.kind is LaneSignalKind.SPAWN


@pytest.mark.parametrize(
    "adapter_class",
    [ClaudeCodeHarnessAdapter, CodexHarnessAdapter, CopilotCliHarnessAdapter],
)
def test_a_plain_failure_without_a_limit_field_does_not_cool_the_lane(
    tmp_path: Path, adapter_class
) -> None:
    commands = FakeCommandAdapter(identity())
    adapter = adapter_class(commands, tmp_path / "harness", command=(sys.executable,))
    failed = seat_result(
        tmp_path,
        "plain",
        json.dumps({"type": "result", "is_error": True, "subtype": "error_during_execution"}),
        SeatOutcome.FAILED,
        exit_code=1,
    )

    assert adapter.classify_failure(failed) is None


def test_result_schemas_require_every_declared_property() -> None:
    for contract in ("builder-report-v1", "review-verdict-v1"):
        schema = result_schema(contract)
        assert set(schema["required"]) == set(schema["properties"])
    finding = result_schema("review-verdict-v1")["properties"]["findings"]["items"]
    assert set(finding["required"]) == set(finding["properties"])
    assert "blocking" in finding["required"]


def _wrap_claude(verdict: dict) -> str:
    return json.dumps({"result": "complete", "structured_output": verdict})


def _wrap_codex(verdict: dict) -> str:
    return json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(verdict)}}
    )


def _wrap_copilot(verdict: dict) -> str:
    return json.dumps({"type": "assistant.message", "data": {"content": json.dumps(verdict)}})


@pytest.mark.parametrize(
    "adapter_class, wrap",
    [
        (ClaudeCodeHarnessAdapter, _wrap_claude),
        (CodexHarnessAdapter, _wrap_codex),
        (CopilotCliHarnessAdapter, _wrap_copilot),
    ],
)
def test_first_party_adapter_rejects_a_finding_without_blocking(
    tmp_path: Path, adapter_class, wrap
) -> None:
    verdict = {
        "decision": "correct",
        "findings": [
            {"code": "BUG", "consequence": "wrong result", "evidence_ref": "file:line"}
        ],
        "evidence_ref": "review",
    }
    commands = FakeCommandAdapter(
        identity(), scripted_results=[command_result(tmp_path, "invoke", wrap(verdict))]
    )
    adapter = adapter_class(commands, tmp_path / "harness", command=(sys.executable,))

    with pytest.raises(EvidenceError):
        adapter.invoke(request(tmp_path, Seat.REVIEWER, "review-verdict-v1", frozenset({"read"})))


def test_brief_and_evidence_are_materialised_without_builder_transcript(tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    evidence = tmp_path / "changes.patch"
    brief.write_text("APPROVED-BRIEF-CONTENT", encoding="utf-8")
    evidence.write_text("DIFF-AND-VALIDATOR-EVIDENCE", encoding="utf-8")
    verdict = {"decision": "pass", "findings": [], "evidence_ref": "review"}
    stdout = json.dumps(
        {"type": "assistant.message", "data": {"content": json.dumps(verdict)}}
    )
    commands = FakeCommandAdapter(
        identity(), scripted_results=[command_result(tmp_path, "invoke", stdout)]
    )
    adapter = CopilotCliHarnessAdapter(
        commands, tmp_path / "harness", command=(sys.executable,)
    )
    seat = request(tmp_path, Seat.REVIEWER, "review-verdict-v1", frozenset({"read"}))
    seat = SeatRequest(
        seat.run_id,
        seat.item_id,
        seat.seat,
        seat.model_class,
        brief,
        seat.workspace,
        seat.tool_policy,
        "REVIEW-INSTRUCTIONS",
        seat.result_contract,
        seat.timeout_seconds,
        (str(evidence), "builder-transcript:must-not-be-loaded"),
    )

    adapter.invoke(seat)

    prompt = prompt_from(commands.requests[0])
    assert "APPROVED-BRIEF-CONTENT" in prompt
    assert "DIFF-AND-VALIDATOR-EVIDENCE" in prompt
    assert "builder-transcript:must-not-be-loaded" not in prompt


def test_claude_recovers_builder_result_from_the_workspace_file_on_cli_error(tmp_path: Path) -> None:
    stdout = json.dumps(
        {"is_error": True, "terminal_reason": "structured_output_retry_exhausted", "result": None}
    )
    commands = FakeCommandAdapter(
        identity(), scripted_results=[command_result(tmp_path, "invoke", stdout, exit_code=1)]
    )
    adapter = ClaudeCodeHarnessAdapter(commands, tmp_path / "harness", command=(sys.executable,))
    seat = request(tmp_path, Seat.BUILDER, "builder-report-v1", frozenset({"read", "write"}))
    result_file = seat.workspace.root / ".autobuild-seat-result.json"
    result_file.write_text(
        json.dumps({"summary": "built from file", "report_ref": "file"}), encoding="utf-8"
    )

    result = adapter.invoke(seat)

    assert result.outcome is SeatOutcome.SUCCEEDED
    assert result.payload is not None and result.payload.summary == "built from file"
    assert "result recovered from the seat result file" in result.diagnostics
    assert not result_file.exists()
    argv = commands.requests[0].argv
    assert ".autobuild-seat-result.json" in argv[argv.index("--append-system-prompt") + 1]


def test_claude_removes_the_workspace_result_file_after_a_successful_seat(tmp_path: Path) -> None:
    stdout = json.dumps(
        {"result": "complete", "structured_output": {"summary": "done", "report_ref": "cli"}}
    )
    commands = FakeCommandAdapter(
        identity(), scripted_results=[command_result(tmp_path, "invoke", stdout)]
    )
    adapter = ClaudeCodeHarnessAdapter(commands, tmp_path / "harness", command=(sys.executable,))
    seat = request(tmp_path, Seat.BUILDER, "builder-report-v1", frozenset({"read", "write"}))
    result_file = seat.workspace.root / ".autobuild-seat-result.json"
    result_file.write_text(json.dumps({"summary": "stale", "report_ref": "file"}), encoding="utf-8")

    result = adapter.invoke(seat)

    assert result.outcome is SeatOutcome.SUCCEEDED
    assert result.payload is not None and result.payload.summary == "done"
    assert not result_file.exists()
    assert ("MAX_STRUCTURED_OUTPUT_RETRIES", "5") in commands.requests[0].environment


def test_claude_seat_without_a_result_file_still_fails_on_cli_error(tmp_path: Path) -> None:
    stdout = json.dumps({"is_error": True, "result": None})
    commands = FakeCommandAdapter(
        identity(), scripted_results=[command_result(tmp_path, "invoke", stdout, exit_code=1)]
    )
    adapter = ClaudeCodeHarnessAdapter(commands, tmp_path / "harness", command=(sys.executable,))

    result = adapter.invoke(request(tmp_path, Seat.BUILDER, "builder-report-v1", frozenset({"read", "write"})))

    assert result.outcome is SeatOutcome.FAILED
    assert result.payload is None


def test_claude_streams_materialised_large_evidence_instead_of_argv(tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    evidence = tmp_path / "changes.patch"
    brief.write_text("APPROVED-BRIEF-CONTENT", encoding="utf-8")
    evidence.write_text("D" * 200_000, encoding="utf-8")
    stdout = json.dumps(
        {
            "result": "complete",
            "structured_output": {"decision": "pass", "findings": [], "evidence_ref": "review"},
        }
    )
    commands = FakeCommandAdapter(
        identity(), scripted_results=[command_result(tmp_path, "invoke", stdout)]
    )
    adapter = ClaudeCodeHarnessAdapter(commands, tmp_path / "harness", command=(sys.executable,))
    seat = request(tmp_path, Seat.REVIEWER, "review-verdict-v1", frozenset({"read"}))
    seat = SeatRequest(
        seat.run_id,
        seat.item_id,
        seat.seat,
        seat.model_class,
        brief,
        seat.workspace,
        seat.tool_policy,
        "REVIEW-INSTRUCTIONS",
        seat.result_contract,
        seat.timeout_seconds,
        (str(evidence),),
    )

    adapter.invoke(seat)

    dispatched = commands.requests[0]
    assert max(len(argument) for argument in dispatched.argv) < 5_000
    assert dispatched.stdin_ref is not None
    prompt = Path(dispatched.stdin_ref).read_text(encoding="utf-8")
    assert "APPROVED-BRIEF-CONTENT" in prompt
    assert "D" * 200_000 in prompt


def test_oversized_evidence_is_refused_before_cli_dispatch(tmp_path: Path) -> None:
    evidence = tmp_path / "large.patch"
    evidence.write_bytes(b"x" * (1_048_576 + 1))
    commands = FakeCommandAdapter(identity())
    adapter = CodexHarnessAdapter(
        commands, tmp_path / "harness", command=(sys.executable,)
    )
    seat = request(tmp_path, Seat.REVIEWER, "review-verdict-v1", frozenset({"read"}))
    seat = SeatRequest(
        seat.run_id,
        seat.item_id,
        seat.seat,
        seat.model_class,
        seat.brief_path,
        seat.workspace,
        seat.tool_policy,
        seat.instructions,
        seat.result_contract,
        seat.timeout_seconds,
        (str(evidence),),
    )

    with pytest.raises(EvidenceError, match="evidence exceeds 1 MiB"):
        adapter.invoke(seat)

    assert commands.requests == []
