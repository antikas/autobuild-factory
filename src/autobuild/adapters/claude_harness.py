"""Claude Code CLI adapter."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

from autobuild.adapters.harness_cli import CliHarnessAdapter, _safe_name, result_schema
from autobuild.domain import LaneSignal, Seat, SeatRequest, SeatResult
from autobuild.ports import CommandPort

SEAT_RESULT_FILE = ".autobuild-seat-result.json"


class ClaudeCodeHarnessAdapter(CliHarnessAdapter):
    """Claude Code CLI seats.

    The CLI enforces the result schema through its structured-output stage. That
    stage has a bounded retry budget and has been observed to fail after long
    sessions (79 and 149 turns on 2026-09-03) with `structured_output_retry_exhausted`,
    which discards an otherwise complete build. A builder seat therefore also
    writes its result to SEAT_RESULT_FILE in the workspace; the adapter reads
    that file when the CLI ends in error and removes it in every case so it
    never reaches the diff.
    """

    adapter_name = "claude-code"
    # Source: Claude Code environment variable reference. DISABLE_TELEMETRY and
    # DISABLE_ERROR_REPORTING switch off usage and error reporting; DO_NOT_TRACK is
    # honoured as the cross-tool standard.
    telemetry_environment = (
        ("DO_NOT_TRACK", "1"),
        ("DISABLE_TELEMETRY", "1"),
        ("DISABLE_ERROR_REPORTING", "1"),
    )

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

    # Claude Code `--output-format json` emits one result object carrying
    # ``is_error`` and, when the underlying API call fails, an ``error`` object
    # whose ``type`` follows the Anthropic error taxonomy (``rate_limit_error``,
    # ``overloaded_error``, ``authentication_error``); the subscription usage
    # window surfaces as ``rate_limit_error``. Older builds also carry ``subtype``.
    # A limit reported only in the ``result`` prose is deliberately not scanned.
    _limit_code_keys = ("code", "type", "subtype", "error_type", "error_code", "reason")

    def classify_failure(self, result: SeatResult) -> LaneSignal | None:
        """Read a lane signal from Claude Code's structured result fields only."""

        return self._structural_lane_signal(result)

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
        prompts = self._output_root / "prompts"
        prompts.mkdir(parents=True, exist_ok=True)
        prompt = prompts / f"{_safe_name(run_ref)}.md"
        prompt.write_text(request.instructions, encoding="utf-8")
        argv = (
            *self._command,
            "--print",
            "--safe-mode",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--append-system-prompt",
            self._contract_prompt(request),
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
        )
        return argv, None, str(prompt)

    def _contract_prompt(self, request: SeatRequest) -> str:
        schema = json.dumps(result_schema(request.result_contract), separators=(",", ":"))
        text = (
            "Your final action must be the structured output matching this schema, "
            f"with no extra fields: {schema}. Do not end on a question or a report in prose."
        )
        if request.seat is Seat.BUILDER:
            text += (
                f" Before that final action, write the same JSON object to the file "
                f"{SEAT_RESULT_FILE} at the root of the workspace (overwrite it if present). "
                "The file is removed automatically after your session; do not commit it and "
                "do not mention it in your summary."
            )
        return text

    def _scratch_environment(self) -> tuple[tuple[str, str], ...]:
        return (*super()._scratch_environment(), ("MAX_STRUCTURED_OUTPUT_RETRIES", "5"))

    def _recover_result(self, request: SeatRequest, run_ref: str) -> str | None:
        path = request.workspace.root / SEAT_RESULT_FILE
        if not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        finally:
            path.unlink(missing_ok=True)
        recovered = self._output_root / "recovered"
        recovered.mkdir(parents=True, exist_ok=True)
        (recovered / f"{_safe_name(run_ref)}.json").write_text(content, encoding="utf-8")
        return content
