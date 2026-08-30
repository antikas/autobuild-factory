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
    ReviewDecision,
    Seat,
    SeatOutcome,
    SeatRequest,
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


def prompt_from(request_argv: tuple[str, ...]) -> str:
    for value in request_argv:
        if value.startswith("--prompt="):
            return value.removeprefix("--prompt=")
    return request_argv[-1]


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


def test_copilot_uses_programmatic_json_mode_and_confined_permissions(tmp_path: Path) -> None:
    verdict = {
        "decision": "correct",
        "findings": [
            {"code": "BUG", "consequence": "wrong result", "evidence_ref": "file:line"}
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
    assert any(value.startswith("--prompt=") for value in argv)
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


def test_result_schemas_require_every_declared_property() -> None:
    for contract in ("builder-report-v1", "review-verdict-v1"):
        schema = result_schema(contract)
        assert set(schema["required"]) == set(schema["properties"])
    finding = result_schema("review-verdict-v1")["properties"]["findings"]["items"]
    assert set(finding["required"]) == set(finding["properties"])


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

    prompt = prompt_from(commands.requests[0].argv)
    assert "APPROVED-BRIEF-CONTENT" in prompt
    assert "DIFF-AND-VALIDATOR-EVIDENCE" in prompt
    assert "builder-transcript:must-not-be-loaded" not in prompt


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
