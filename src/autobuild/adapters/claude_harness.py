"""Claude Code CLI adapter."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

from autobuild.adapters.harness_cli import CliHarnessAdapter, result_schema
from autobuild.domain import SeatRequest
from autobuild.ports import CommandPort


class ClaudeCodeHarnessAdapter(CliHarnessAdapter):
    adapter_name = "claude-code"

    def __init__(
        self,
        command_port: CommandPort,
        output_root: Path,
        command: tuple[str, ...] = ("claude",),
        model_map=None,
    ) -> None:
        super().__init__(
            command_port,
            output_root,
            self._resolve_windows_native(command),
            model_map,
        )

    @staticmethod
    def _resolve_windows_native(command: tuple[str, ...]) -> tuple[str, ...]:
        if os.name != "nt" or len(command) != 1:
            return command
        resolved = shutil.which(command[0])
        if resolved is None or Path(resolved).suffix.casefold() not in {".cmd", ".bat"}:
            return command
        package = Path(resolved).parent / "node_modules" / "@anthropic-ai" / "claude-code"
        candidates = (package / "bin" / "claude.exe", package / "cli.js")
        for candidate in candidates:
            if candidate.is_file():
                if candidate.suffix.casefold() == ".js":
                    node = shutil.which("node")
                    if node is not None:
                        return (node, str(candidate))
                else:
                    return (str(candidate),)
        return command

    def _version_argv(self) -> tuple[str, ...]:
        return (*self._command, "--version")

    def _probe_authentication(self) -> tuple[bool, str]:
        result = self._probe_run("auth", (*self._command, "auth", "status", "--json"))
        if result.exit_code != 0:
            return False, "Claude Code authentication probe failed"
        try:
            payload = json.loads(self._read(result.stdout_ref))
        except json.JSONDecodeError:
            return False, "Claude Code authentication probe returned invalid JSON"
        authenticated = bool(payload.get("loggedIn") or payload.get("authenticated"))
        return authenticated, "Claude Code authentication is available" if authenticated else "Claude Code is not authenticated"

    def _invocation(self, request: SeatRequest, run_ref: str):
        self._require_known_tools(request.tool_policy.allowed_tools)
        mapping = {
            "read": ("Read", "Glob", "Grep"),
            "write": ("Edit", "Write", "NotebookEdit"),
            "shell": ("Bash",),
            "python": ("Bash(python *)",),
            "git": ("Bash(git *)",),
        }
        tools = sorted(
            {native for tool in request.tool_policy.allowed_tools for native in mapping[tool]}
        )
        argv = (
            *self._command,
            "--print",
            "--safe-mode",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--session-id",
            str(uuid4()),
            "--model",
            self._model(request.model_class),
            "--tools",
            ",".join(tools),
            "--allowedTools",
            ",".join(tools),
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(result_schema(request.result_contract), separators=(",", ":")),
            request.instructions,
        )
        return argv, None
