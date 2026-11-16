"""The strict-process pre-command check as a Claude Code ``PreToolUse`` hook.

A strict builder seat on a Claude Code lane gets this hook in its flag
settings, matched to the Bash tool. Claude Code runs it before every shell
command with the tool call as JSON on standard input. The hook loads the
seat's guard file, passes the command line to the one refusal rule and, for a
whole-suite or out-of-change test run, answers with a ``deny`` decision whose
reason is the rule's one sentence. Any other command gets no output and runs.

The hook runs as a script under ``python -I``, so the seat's working directory
can never shadow a module; it puts its own source root on the import path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping, TextIO

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from autobuild.domain.models import ProcessMode, ProcessPolicy, TestCoverage, TestGuard  # noqa: E402
from autobuild.enforcement.test_scope import shell_command_refusal  # noqa: E402

HOOK_SCRIPT = Path(__file__).resolve()
_QUOTING = ('"', "$", "`")


def _entry_document(entry: TestCoverage) -> dict[str, Any]:
    return {"pattern": entry.pattern, "tests": list(entry.tests), "command": list(entry.command)}


def _entry(document: Mapping[str, Any]) -> TestCoverage:
    return TestCoverage(
        str(document["pattern"]),
        tests=tuple(document.get("tests", ())),
        command=tuple(document.get("command", ())),
    )


def guard_document(guard: TestGuard) -> dict[str, Any]:
    """The guard as the JSON the hook reads back."""

    return {
        "mode": guard.policy.mode.value,
        "test_command": list(guard.policy.test_command),
        "test_map": [_entry_document(entry) for entry in guard.policy.test_map],
        "covering": [_entry_document(entry) for entry in guard.covering],
    }


def load_guard(path: Path) -> TestGuard:
    document = json.loads(path.read_text(encoding="utf-8"))
    policy = ProcessPolicy(
        ProcessMode(document["mode"]),
        tuple(document["test_command"]),
        tuple(_entry(entry) for entry in document["test_map"]),
    )
    return TestGuard(policy, tuple(_entry(entry) for entry in document["covering"]))


def write_guard(guard: TestGuard, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(guard_document(guard), indent=2), encoding="utf-8")
    return path


def hook_command(guard_path: Path) -> str:
    """The shell line Claude Code runs for the hook. Raises ValueError when a
    path holds a character the shell would read inside double quotes."""

    parts = (Path(sys.executable).as_posix(), HOOK_SCRIPT.as_posix(), guard_path.as_posix())
    for part in parts:
        if any(mark in part for mark in _QUOTING):
            raise ValueError(f"the test hook path contains a shell quoting character: {part}")
    executable, script, guard = parts
    return f'"{executable}" -I "{script}" "{guard}"'


def hook_settings(guard_path: Path) -> dict[str, Any]:
    """The flag settings that install the hook on the seat's Bash tool."""

    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": hook_command(guard_path)}],
                }
            ]
        }
    }


def hook_decision(guard: TestGuard, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """The hook's answer to one tool call: a deny decision, or None to allow."""

    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
    if not isinstance(command, str):
        return None
    refusal = shell_command_refusal(guard, command)
    if refusal is None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": refusal,
        }
    }


def main(arguments: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    """Exit 0 with a decision or nothing; exit 2, which blocks the command,
    when the guard or the tool call cannot be read."""

    if len(arguments) != 1:
        stderr.write("The strict test hook needs one guard file argument.\n")
        return 2
    try:
        guard = load_guard(Path(arguments[0]))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        stderr.write(f"The strict test hook could not read its guard file: {exc}\n")
        return 2
    try:
        payload = json.loads(stdin.read())
    except ValueError as exc:
        stderr.write(f"The strict test hook could not read the tool call: {exc}\n")
        return 2
    decision = hook_decision(guard, payload) if isinstance(payload, Mapping) else None
    if decision is not None:
        stdout.write(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin, sys.stdout, sys.stderr))
