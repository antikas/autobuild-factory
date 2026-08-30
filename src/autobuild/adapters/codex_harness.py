"""Codex CLI adapter."""

from __future__ import annotations

from pathlib import Path

from autobuild.adapters.harness_cli import CliHarnessAdapter, _safe_name
from autobuild.domain import Seat, SeatRequest
from autobuild.ports import CommandPort


class CodexHarnessAdapter(CliHarnessAdapter):
    adapter_name = "codex"

    def __init__(
        self,
        command_port: CommandPort,
        output_root: Path,
        command: tuple[str, ...] = ("codex",),
        model_map=None,
    ) -> None:
        super().__init__(command_port, output_root, command, model_map)

    def _version_argv(self) -> tuple[str, ...]:
        return (*self._command, "--version")

    def _probe_authentication(self) -> tuple[bool, str]:
        result = self._probe_run("auth", (*self._command, "login", "status"))
        output = self._read(result.stdout_ref).casefold()
        authenticated = result.exit_code == 0 and "not logged" not in output
        return authenticated, "Codex authentication is available" if authenticated else "Codex is not authenticated"

    def _invocation(self, request: SeatRequest, run_ref: str):
        self._require_known_tools(request.tool_policy.allowed_tools)
        schema = self._write_schema(run_ref, request.result_contract)
        outputs = self._output_root / "last-messages"
        outputs.mkdir(parents=True, exist_ok=True)
        last_message = outputs / f"{_safe_name(run_ref)}.json"
        writable = request.seat is Seat.BUILDER and bool(
            request.tool_policy.allowed_tools & {"write", "shell", "python", "git"}
        )
        sandbox = "workspace-write" if writable else "read-only"
        argv = (
            *self._command,
            "-a",
            "never",
            "-s",
            sandbox,
            "-C",
            str(request.workspace.root),
            "-m",
            self._model(request.model_class),
            "exec",
            "--ephemeral",
            "--ignore-rules",
            "--json",
            "--output-schema",
            str(schema),
            "-o",
            str(last_message),
            request.instructions,
        )
        return argv, last_message
