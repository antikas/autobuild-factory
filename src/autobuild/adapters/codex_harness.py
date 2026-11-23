"""Codex CLI adapter."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from autobuild.adapters.harness_cli import CliHarnessAdapter, _safe_name
from autobuild.adapters.codex_subscription import (
    CHATGPT_SUBSCRIPTION_METER,
    CodexSubscriptionMeterAdapter,
    _subscription_removed_environment,
)
from autobuild.adapters.usage_cache import SharedUsageMeterCache
from autobuild.domain import CapabilityError, CommandRequest, EffortLevel, LaneSignal, ProbeResult, Seat, SeatRequest, SeatResult, UsageReading
from autobuild.ports import CommandPort


class CodexHarnessAdapter(CliHarnessAdapter):
    adapter_name = "codex"
    # Source: OpenTelemetry SDK environment specification. OTEL_SDK_DISABLED turns
    # off the SDK the CLI uses for traces; DO_NOT_TRACK is the cross-tool standard.
    telemetry_environment = (
        ("DO_NOT_TRACK", "1"),
        ("OTEL_SDK_DISABLED", "true"),
    )
    effort_levels = frozenset(EffortLevel)
    meter_id = None
    # The scratch root whose shared usage reading the sign-in probe uses. The
    # composition sets it; None keeps the probe's own direct read.
    shared_usage_root: Path | None = None
    # The failed reading that made the last sign-in probe refuse the lane.
    meter_failure: UsageReading | None = None
    removed_environment = CliHarnessAdapter.removed_environment
    subscription_removed_environment = (
        *CliHarnessAdapter.removed_environment,
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT",
    )
    subscription_models = frozenset({"gpt-5.6-terra", "gpt-5.6-sol"})

    def __init__(
        self,
        command_port: CommandPort,
        output_root: Path,
        command: tuple[str, ...] = ("codex",),
        model_map=None,
        prepare_argv: tuple[str, ...] = (),
        subscription_only: bool = False,
    ) -> None:
        super().__init__(command_port, output_root, command, model_map)
        self._prepare_argv = prepare_argv
        self._subscription_only = subscription_only
        self.meter_id = CHATGPT_SUBSCRIPTION_METER if subscription_only else None
        if subscription_only:
            self.removed_environment = _subscription_removed_environment(
                self.subscription_removed_environment
            )

    def probe(self) -> ProbeResult:
        if self._subscription_only:
            unsupported = sorted(set(self._model_map.values()) - self.subscription_models)
            if unsupported:
                return ProbeResult.unavailable("Codex subscription lane names a non-included model tier")
        return super().probe()

    def invoke(self, request: SeatRequest) -> SeatResult:
        writable_builder = request.seat is Seat.BUILDER and bool(
            request.tool_policy.allowed_tools & {"write", "shell", "python", "git"}
        )
        if self._prepare_argv and writable_builder:
            result = self._commands.run(
                CommandRequest(
                    f"{request.run_id}:{request.item_id}:prepare",
                    self._prepare_argv,
                    request.workspace.root,
                    environment=self._seat_environment(request.run_id, request.item_id),
                    timeout_seconds=request.timeout_seconds,
                    remove_environment=self.removed_environment,
                )
            )
            if result.exit_code != 0 or result.timed_out or result.stalled:
                raise RuntimeError("Codex prepare command failed before builder admission")
        return super().invoke(request)

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
        if not self._subscription_only:
            result = self._probe_run("auth", (*self._command, "login", "status"))
            output = self._read(result.stdout_ref).casefold()
            authenticated = result.exit_code == 0 and "not logged" not in output
            return authenticated, "Codex authentication is available" if authenticated else "Codex is not authenticated"
        meter = CodexSubscriptionMeterAdapter(
            self._commands, self._output_root / "subscription-preflight"
        )
        if self.shared_usage_root is not None:
            meter = SharedUsageMeterCache(meter, self.shared_usage_root)
        reading = meter.read(CHATGPT_SUBSCRIPTION_METER)
        self.login_reading = reading
        self.meter_failure = None if reading.ok else reading
        return reading.ok, "Codex included ChatGPT subscription is available" if reading.ok else reading.error

    def _effort_option(self, level: str) -> tuple[str, ...]:
        return ("-c", f"model_reasoning_effort={level}")

    def _review_permissions(self):
        # The CLI's built-in read-only mode also reads session histories outside
        # the checkout. Give the sandbox just system tools, its own runtime, and
        # the candidate. The npm launcher keeps its sandbox binaries in its package.
        executable = Path(shutil.which(self._command[0]) or self._command[0]).resolve()
        runtime = executable.parents[1] if executable.name == "codex.js" and executable.parents[1].name == "codex" else executable
        rules = 'permissions.factory-review.filesystem={":minimal"="read",":workspace_roots"="read",' + json.dumps(str(runtime)) + '="read"}'
        return (
            "-c", rules, "-c", "permissions.factory-review.network.enabled=false",
            "-c", 'default_permissions="factory-review"',
            "-c", 'web_search="disabled"', "--disable", "apps", "--disable", "multi_agent",
            "--disable", "hooks",
        )

    def _invocation(self, request: SeatRequest, run_ref: str):
        self._require_known_tools(request.tool_policy.allowed_tools)
        if self._subscription_only and self._model(request.model_class) not in self.subscription_models:
            raise CapabilityError("Codex subscription lane cannot dispatch a non-included model tier")
        effort = self._effort_arguments(request)
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
        sandbox = ("-s", "workspace-write" if writable else "read-only")
        if self._subscription_only and request.seat is not Seat.BUILDER:
            sandbox = self._review_permissions()
            if not request.tool_policy.allowed_tools:
                sandbox += ("--disable", "shell_tool", "--disable", "unified_exec")
        argv = (
            *self._command,
            "-a",
            "never",
            *sandbox,
            "-C",
            str(request.workspace.root),
            "-m",
            self._model(request.model_class),
            *effort,
            "exec",
            "--ephemeral",
            *(("--ignore-user-config",) if self._subscription_only else ()),
            "--ignore-rules",
            "--json",
            "--output-schema",
            str(schema),
            "-o",
            str(last_message),
            "-",
        )
        return argv, last_message, str(prompt)
