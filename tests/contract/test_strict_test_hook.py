"""The strict test rule runs where a builder's shell command runs: a strict
builder seat on a Claude Code lane carries a ``PreToolUse`` hook on its Bash
tool that calls the one refusal rule. Flow mode and the other seat kinds get
no hook."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_unattended_seats import MODES, execution_spec, failed_command, seat_request

from autobuild.adapters import ClaudeCodeHarnessAdapter
from autobuild.adapters.claude_harness import refuse_hook_under_safe_mode
from autobuild.adapters.claude_sdk_harness import ClaudeSdkHarnessAdapter
from autobuild.adapters.claude_test_hook import hook_decision, load_guard, main, write_guard
from autobuild.domain import AdapterIdentity, CapabilityError, Seat
from autobuild.domain.models import ProcessMode, ProcessPolicy, TestCoverage, TestGuard
from autobuild.enforcement.test_scope import seat_test_guard
from autobuild.testing import FakeAgentSdkClient, FakeCommandAdapter

FLOW, STRICT = MODES
SEATS = (Seat.BUILDER, Seat.REVIEWER, Seat.SPECIALIST)

POLICY = ProcessPolicy(
    ProcessMode.STRICT,
    ("uv", "run", "pytest"),
    (
        TestCoverage("src/app/profile/**", tests=("tests/unit/test_profile.py",)),
        TestCoverage("src/app/web/**", command=("npm", "test", "--", "web")),
    ),
)
GUARD = TestGuard(POLICY, POLICY.test_map[:1])
REFUSAL = (
    "A builder seat may not run the whole test suite or tests outside its change; "
    "run only the covering tests: tests/unit/test_profile.py."
)


def guarded_request(tmp_path: Path, seat: Seat, process: ProcessPolicy):
    """The seat request the application builds: the guard comes from the one
    function that decides which seat carries it."""

    request = seat_request(tmp_path, seat, process)
    return replace(request, test_guard=seat_test_guard(execution_spec(process), seat))


def claude_argv(tmp_path: Path, request) -> tuple[str, ...]:
    commands = FakeCommandAdapter(
        AdapterIdentity("command", "1"), scripted_results=[failed_command(tmp_path)]
    )
    adapter = ClaudeCodeHarnessAdapter(commands, tmp_path / "harness", command=("claude",))
    adapter.invoke(request)
    return commands.requests[0].argv


def settings_of(argv: tuple[str, ...]) -> dict | None:
    if "--settings" not in argv:
        return None
    return json.loads(argv[argv.index("--settings") + 1])


def sdk_settings(tmp_path: Path, request) -> dict:
    key = tmp_path / "sdk.key"
    key.write_text("key", encoding="utf-8")
    adapter = ClaudeSdkHarnessAdapter(
        FakeCommandAdapter(AdapterIdentity("command", "1")),
        tmp_path / "harness",
        key,
        client=FakeAgentSdkClient(scripts=[]),
    )
    return dict(adapter.seat_spec(request, "run:item:seat", None).settings)


# --- which seat carries the hook ----------------------------------------------------------


def test_a_strict_builder_seat_carries_the_test_hook_on_its_bash_tool(tmp_path: Path) -> None:
    settings = settings_of(claude_argv(tmp_path, guarded_request(tmp_path, Seat.BUILDER, STRICT)))

    assert settings is not None
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash"
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    assert "claude_test_hook.py" in hook["command"]
    guard_file = Path(hook["command"].rsplit('"', 2)[-2])
    assert load_guard(guard_file) == seat_test_guard(execution_spec(STRICT), Seat.BUILDER)


def test_a_flow_builder_seat_carries_no_hook(tmp_path: Path) -> None:
    request = guarded_request(tmp_path, Seat.BUILDER, FLOW)

    assert request.test_guard is None
    assert settings_of(claude_argv(tmp_path, request)) is None


@pytest.mark.parametrize("process", MODES, ids=("flow", "strict"))
@pytest.mark.parametrize("seat", (Seat.REVIEWER, Seat.SPECIALIST), ids=lambda seat: seat.value)
def test_review_seats_carry_no_hook_in_either_mode(tmp_path: Path, seat: Seat, process) -> None:
    request = guarded_request(tmp_path, seat, process)

    assert request.test_guard is None
    assert settings_of(claude_argv(tmp_path, request)) is None


@pytest.mark.parametrize("seat", (Seat.REVIEWER, Seat.SPECIALIST), ids=lambda seat: seat.value)
def test_a_review_seat_request_refuses_a_test_guard(tmp_path: Path, seat: Seat) -> None:
    with pytest.raises(ValueError, match="only a builder seat carries a test guard"):
        replace(seat_request(tmp_path, seat, STRICT), test_guard=GUARD)


def test_the_sdk_lane_installs_the_hook_for_a_strict_builder_and_lifts_the_hook_ban(
    tmp_path: Path,
) -> None:
    settings = sdk_settings(tmp_path, guarded_request(tmp_path, Seat.BUILDER, STRICT))

    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert "disableAllHooks" not in settings
    assert "apiKeyHelper" in settings


@pytest.mark.parametrize(
    ("seat", "process"),
    [(Seat.BUILDER, FLOW), (Seat.REVIEWER, STRICT), (Seat.SPECIALIST, STRICT)],
    ids=("flow-builder", "strict-reviewer", "strict-specialist"),
)
def test_the_sdk_lane_keeps_hooks_disabled_for_every_other_seat(
    tmp_path: Path, seat: Seat, process
) -> None:
    settings = sdk_settings(tmp_path, guarded_request(tmp_path, seat, process))

    assert "hooks" not in settings
    assert settings["disableAllHooks"] is True


# --- how a seat is isolated ---------------------------------------------------------------
#
# --safe-mode switches every hook off, so a seat that carries the test hook is
# isolated by loading no settings source, no MCP server it is not given and no
# skill or slash command instead. Every other seat keeps --safe-mode.

ISOLATING = ("--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands")
ALL_SEATS = [(seat, process) for seat in SEATS for process in MODES]
ALL_IDS = [f"{process.mode.value}-{seat.value}" for seat, process in ALL_SEATS]
UNHOOKED = [pair for pair in ALL_SEATS if pair != (Seat.BUILDER, STRICT)]
UNHOOKED_IDS = [f"{process.mode.value}-{seat.value}" for seat, process in UNHOOKED]


def holds_in_order(argv: tuple[str, ...], part: tuple[str, ...]) -> bool:
    return any(argv[index : index + len(part)] == part for index in range(len(argv)))


def test_a_strict_builder_seat_launches_isolated_without_safe_mode(tmp_path: Path) -> None:
    argv = claude_argv(tmp_path, guarded_request(tmp_path, Seat.BUILDER, STRICT))

    assert "--safe-mode" not in argv
    assert holds_in_order(argv, ISOLATING)
    assert settings_of(argv)["hooks"]["PreToolUse"][0]["matcher"] == "Bash"


@pytest.mark.parametrize(("seat", "process"), UNHOOKED, ids=UNHOOKED_IDS)
def test_every_other_seat_launches_with_safe_mode(tmp_path: Path, seat: Seat, process) -> None:
    argv = claude_argv(tmp_path, guarded_request(tmp_path, seat, process))

    assert "--safe-mode" in argv
    assert not {"--setting-sources", "--strict-mcp-config", "--disable-slash-commands"} & set(argv)
    assert settings_of(argv) is None


@pytest.mark.parametrize(("seat", "process"), ALL_SEATS, ids=ALL_IDS)
def test_no_seat_launches_with_a_hook_and_safe_mode(tmp_path: Path, seat: Seat, process) -> None:
    argv = claude_argv(tmp_path, guarded_request(tmp_path, seat, process))
    settings = settings_of(argv) or {}

    assert not ("--safe-mode" in argv and settings.get("hooks"))


def test_a_vector_with_a_hook_and_safe_mode_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        ClaudeCodeHarnessAdapter, "_isolation_arguments", staticmethod(lambda hooked: ("--safe-mode",))
    )

    with pytest.raises(CapabilityError, match="cannot carry a hook and --safe-mode"):
        claude_argv(tmp_path, guarded_request(tmp_path, Seat.BUILDER, STRICT))


def test_the_refusal_reads_the_settings_value() -> None:
    unhooked = json.dumps({"hooks": {}, "x": 1})
    hooked_with_entry = json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash"}]}})

    refuse_hook_under_safe_mode(("claude", "--safe-mode", "--settings", unhooked))
    refuse_hook_under_safe_mode(("claude", "--settings", hooked_with_entry))
    with pytest.raises(CapabilityError):
        refuse_hook_under_safe_mode(("claude", "--safe-mode", "--settings", hooked_with_entry))


def sdk_spec(tmp_path: Path, request):
    key = tmp_path / "sdk.key"
    key.write_text("key", encoding="utf-8")
    adapter = ClaudeSdkHarnessAdapter(
        FakeCommandAdapter(AdapterIdentity("command", "1")),
        tmp_path / "harness",
        key,
        client=FakeAgentSdkClient(scripts=[]),
    )
    return adapter.seat_spec(request, "run:item:seat", None)


def test_the_sdk_lane_starts_a_strict_builder_isolated_without_safe_mode(tmp_path: Path) -> None:
    spec = sdk_spec(tmp_path, guarded_request(tmp_path, Seat.BUILDER, STRICT))

    assert set(spec.flags) == {"no-session-persistence", "strict-mcp-config", "disable-slash-commands"}
    assert spec.setting_sources == ()
    assert spec.settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"


@pytest.mark.parametrize(("seat", "process"), UNHOOKED, ids=UNHOOKED_IDS)
def test_the_sdk_lane_keeps_safe_mode_for_every_other_seat(tmp_path: Path, seat: Seat, process) -> None:
    spec = sdk_spec(tmp_path, guarded_request(tmp_path, seat, process))

    assert set(spec.flags) == {"safe-mode", "no-session-persistence"}
    assert "hooks" not in spec.settings


def test_the_sdk_lane_refuses_a_hook_with_safe_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("autobuild.adapters.claude_harness.refuse_hook_under_safe_mode", lambda argv: None)
    monkeypatch.setattr(
        ClaudeCodeHarnessAdapter, "_isolation_arguments", staticmethod(lambda hooked: ("--safe-mode",))
    )

    with pytest.raises(CapabilityError, match="cannot carry a hook and --safe-mode"):
        sdk_spec(tmp_path, guarded_request(tmp_path, Seat.BUILDER, STRICT))


# --- the hook's decision ------------------------------------------------------------------


def bash(command: str) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}


def deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


DECISIONS = [
    ("uv run pytest", deny(REFUSAL)),
    ("uv run pytest tests -q", deny(REFUSAL)),
    ("cd /work && uv run pytest . 2>&1 | tail -20", deny(REFUSAL)),
    ("uv run pytest tests/unit/test_profile.py -q", None),
    ("FOO=1 uv run pytest tests/unit/test_profile.py::test_one 2>&1 | tail -5", None),
    ("uv run pytest tests/unit/test_profile.py tests", deny(REFUSAL)),
    ("uv run pytest tests/unit/test_profile.py -q\nuv run pytest tests", deny(REFUSAL)),
    ("git status && ls -la", None),
    ("python -c 'print(1)'", None),
]


@pytest.mark.parametrize(("command", "expected"), DECISIONS, ids=[case[0] for case in DECISIONS])
def test_the_hook_decision(command: str, expected: dict | None) -> None:
    assert hook_decision(GUARD, bash(command)) == expected


def test_the_hook_ignores_tools_other_than_bash() -> None:
    payload = {"tool_name": "Write", "tool_input": {"command": "uv run pytest"}}

    assert hook_decision(GUARD, payload) is None


def run_installed_hook(tmp_path: Path, command: str) -> subprocess.CompletedProcess[str]:
    """Run the hook exactly as Claude Code would: the settings' command line
    through the shell, the tool call as JSON on standard input, from a
    working directory that is not this repository."""

    settings = settings_of(claude_argv(tmp_path, guarded_request(tmp_path, Seat.BUILDER, STRICT)))
    assert settings is not None
    line = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    seat_root = tmp_path / "seat"
    seat_root.mkdir(exist_ok=True)
    return subprocess.run(
        line,
        shell=True,
        input=json.dumps(bash(command)),
        capture_output=True,
        text=True,
        cwd=seat_root,
        timeout=60,
        check=False,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="the hook line is run by a POSIX shell")
@pytest.mark.parametrize(
    ("command", "denied"),
    [
        ("uv run pytest tests -q", True),
        ("uv run pytest tests/unit/test_profile.py -q", False),
        ("uv run pytest tests/unit/test_profile.py tests", True),
        ("git status", False),
    ],
)
def test_the_installed_hook_command_decides_through_the_shell(
    tmp_path: Path, command: str, denied: bool
) -> None:
    result = run_installed_hook(tmp_path, command)

    assert result.returncode == 0, result.stderr
    if denied:
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert decision["permissionDecisionReason"].startswith(
            "A builder seat may not run the whole test suite"
        )
    else:
        assert result.stdout == ""


def test_the_hook_blocks_when_its_guard_file_is_missing(tmp_path: Path) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()

    code = main([str(tmp_path / "missing.json")], io.StringIO(json.dumps(bash("ls"))), stdout, stderr)

    assert code == 2
    assert stdout.getvalue() == ""
    assert "could not read its guard file" in stderr.getvalue()


def test_the_guard_file_round_trips(tmp_path: Path) -> None:
    assert load_guard(write_guard(GUARD, tmp_path / "guard.json")) == GUARD
