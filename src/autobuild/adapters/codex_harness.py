"""Codex CLI adapter."""

from __future__ import annotations

from pathlib import Path

from autobuild.adapters.harness_cli import CliHarnessAdapter, _safe_name
from autobuild.domain import LaneSignal, Seat, SeatRequest, SeatResult
from autobuild.ports import CommandPort


class CodexHarnessAdapter(CliHarnessAdapter):
    adapter_name = "codex"
    # Source: OpenTelemetry SDK environment specification. OTEL_SDK_DISABLED turns
    # off the SDK the CLI uses for traces; DO_NOT_TRACK is the cross-tool standard.
    telemetry_environment = (
        ("DO_NOT_TRACK", "1"),
        ("OTEL_SDK_DISABLED", "true"),
    )

    def __init__(
        self,
        command_port: CommandPort,
        output_root: Path,
        command: tuple[str, ...] = ("codex",),
        model_map=None,
    ) -> None:
        super().__init__(command_port, output_root, command, model_map)

    # Codex `exec --json` emits JSONL events. A failed turn surfaces an
    # ``{"type": "error", ...}`` event, or a ``turn.failed`` event with a nested
    # ``error`` object, carrying the OpenAI error ``type``/``code``
    # (``rate_limit_exceeded``, ``insufficient_quota``, ``invalid_api_key``).
    # An absolute ``reset_at`` provides the reset time when the vendor supplies one.
    def classify_failure(self, result: SeatResult) -> LaneSignal | None:
        """Read a lane signal from Codex's structured error events only."""

        return self._structural_lane_signal(result)

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
        prompts = self._output_root / "prompts"
        prompts.mkdir(parents=True, exist_ok=True)
        prompt = prompts / f"{_safe_name(run_ref)}.md"
        prompt.write_text(request.instructions, encoding="utf-8")
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
            "-",
        )
        return argv, last_message, str(prompt)
