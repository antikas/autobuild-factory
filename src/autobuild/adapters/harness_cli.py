"""Shared process, contract and evidence machinery for CLI harness adapters."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import Any

from autobuild.domain import (
    AdapterIdentity,
    BuilderReport,
    CapabilityError,
    CommandRequest,
    EvidenceError,
    LaneSignal,
    LaneSignalKind,
    ProbeResult,
    ReviewDecision,
    ReviewFinding,
    ReviewVerdict,
    Seat,
    SeatOutcome,
    SeatRequest,
    SeatResult,
    SeatUsage,
    review_verdict_rule_error,
)
from autobuild.ports import CommandPort


# Structural limit codes mapped to their lane signal kind. The keys are the
# values a CLI reports in its structured error fields, never words scanned from
# prose. Adapters extend recognition through their own ``_limit_code_keys``.
_LIMIT_CODES: dict[str, LaneSignalKind] = {
    "rate_limit": LaneSignalKind.RATE_LIMIT,
    "rate_limit_error": LaneSignalKind.RATE_LIMIT,
    "rate_limit_exceeded": LaneSignalKind.RATE_LIMIT,
    "rate_limited": LaneSignalKind.RATE_LIMIT,
    "ratelimited": LaneSignalKind.RATE_LIMIT,
    "overloaded": LaneSignalKind.RATE_LIMIT,
    "overloaded_error": LaneSignalKind.RATE_LIMIT,
    "usage_limit_reached": LaneSignalKind.RATE_LIMIT,
    "429": LaneSignalKind.RATE_LIMIT,
    "insufficient_quota": LaneSignalKind.QUOTA,
    "quota_exceeded": LaneSignalKind.QUOTA,
    "quota": LaneSignalKind.QUOTA,
    "billing_hard_limit_reached": LaneSignalKind.QUOTA,
    "authentication_error": LaneSignalKind.AUTH,
    "invalid_api_key": LaneSignalKind.AUTH,
    "unauthorized": LaneSignalKind.AUTH,
    "401": LaneSignalKind.AUTH,
}


def _kind_for_code(value: object) -> LaneSignalKind | None:
    if not isinstance(value, str):
        return None
    return _LIMIT_CODES.get(value.strip().casefold())


BUILDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "report_ref": {"type": "string"},
    },
    "required": ["summary", "report_ref"],
    "additionalProperties": False,
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"enum": [decision.value for decision in ReviewDecision]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "minLength": 1},
                    "consequence": {"type": "string", "minLength": 1},
                    "evidence_ref": {"type": "string", "minLength": 1},
                    "blocking": {"type": "boolean"},
                    "specialist_boundary": {"type": ["string", "null"]},
                },
                "required": [
                    "code",
                    "consequence",
                    "evidence_ref",
                    "blocking",
                    "specialist_boundary",
                ],
                "additionalProperties": False,
            },
        },
        "evidence_ref": {"type": "string"},
    },
    "required": ["decision", "findings", "evidence_ref"],
    "additionalProperties": False,
}


def result_schema(contract: str) -> dict[str, Any]:
    if contract == "builder-report-v1":
        return BUILDER_SCHEMA
    if contract == "review-verdict-v1":
        return REVIEW_SCHEMA
    raise CapabilityError(f"unsupported harness result contract: {contract}")


def scratch_environment(root: Path) -> tuple[tuple[str, str], ...]:
    """Create and describe the temporary environment used by child processes."""

    values = {
        "TMPDIR": root / "tmp",
        "TEMP": root / "tmp",
        "TMP": root / "tmp",
        "UV_CACHE_DIR": root / "uv-cache",
        "PIP_CACHE_DIR": root / "pip-cache",
        "PYTHONPYCACHEPREFIX": root / "pycache",
        "XDG_CACHE_HOME": root / "xdg-cache",
        "NPM_CONFIG_CACHE": root / "npm-cache",
        "NO_COLOR": Path("1"),
    }
    for key, value in values.items():
        if key != "NO_COLOR":
            value.mkdir(parents=True, exist_ok=True)
    return tuple((key, str(value)) for key, value in values.items())


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned or "seat"


def _json_value(value: Any) -> Any | None:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip() if len(lines) > 2 else candidate
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _walk(value: Any):
    parsed = _json_value(value)
    if parsed is not None and parsed is not value:
        yield from _walk(parsed)
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _documents(text: str) -> list[Any]:
    documents: list[Any] = []
    whole = _json_value(text)
    if whole is not None:
        documents.append(whole)
    for line in text.splitlines():
        parsed = _json_value(line)
        if parsed is not None:
            documents.append(parsed)
    return documents


def _contract_object(text: str, contract: str) -> dict[str, Any]:
    key = "summary" if contract == "builder-report-v1" else "decision"
    for document in reversed(_documents(text)):
        for candidate in _walk(document):
            if key in candidate:
                return candidate
    raise EvidenceError(f"harness output did not contain {contract}")


def _normalise_usage(text: str, source: str) -> SeatUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None
    for document in _documents(text):
        for candidate in _walk(document):
            for key in ("input_tokens", "inputTokens", "prompt_tokens"):
                value = candidate.get(key)
                if isinstance(value, int):
                    input_tokens = value
            for key in ("output_tokens", "outputTokens", "completion_tokens"):
                value = candidate.get(key)
                if isinstance(value, int):
                    output_tokens = value
            for key in ("total_cost_usd", "cost_usd", "cost"):
                value = candidate.get(key)
                if isinstance(value, (int, float)):
                    cost = float(value)
    available = input_tokens is not None or output_tokens is not None or cost is not None
    return SeatUsage(input_tokens, output_tokens, cost, source if available else f"{source}:unavailable")


class CliHarnessAdapter:
    adapter_name = "cli"
    capabilities = frozenset({"fresh-seat", "cancel", "typed-result", "usage"})
    # Environment that disables outbound telemetry for the child process. Source:
    # the Console Do Not Track standard (https://consoledonottrack.com/), honoured
    # by an increasing number of CLI tools. Subclasses add vendor-specific names.
    telemetry_environment: tuple[tuple[str, str], ...] = (("DO_NOT_TRACK", "1"),)

    def __init__(
        self,
        command_port: CommandPort,
        output_root: Path,
        command: tuple[str, ...],
        model_map: Mapping[str, str] | None = None,
    ) -> None:
        if not command:
            raise ValueError("harness command must not be empty")
        self._commands = command_port
        self._output_root = output_root.resolve(strict=False)
        self._command = command
        self._model_map = dict(model_map or {})
        self._usage: dict[str, SeatUsage] = {}
        self._lock = Lock()

    def probe(self) -> ProbeResult:
        self._output_root.mkdir(parents=True, exist_ok=True)
        if shutil.which(self._command[0]) is None and not Path(self._command[0]).is_file():
            return ProbeResult.unavailable(f"{self._command[0]} executable was not found")
        version = self._probe_run("version", self._version_argv())
        if version.exit_code != 0 or version.timed_out:
            return ProbeResult.unavailable(f"{self.adapter_name} version probe failed")
        authenticated, diagnostic = self._probe_authentication()
        if not authenticated:
            return ProbeResult.unavailable(diagnostic)
        version_text = self._read(version.stdout_ref).strip() or "unknown"
        return ProbeResult.ready(
            AdapterIdentity(self.adapter_name, version_text.splitlines()[0], self.capabilities),
            diagnostic,
        )

    def invoke(self, request: SeatRequest) -> SeatResult:
        run_ref = f"{request.run_id}:{request.item_id}:{request.seat.value}:{self.adapter_name}"
        prepared = replace(request, instructions=self._materialise_evidence(request))
        invocation, extra_output, stdin_ref = self._invocation(prepared, run_ref)
        command = self._commands.run(
            CommandRequest(
                command_id=run_ref,
                argv=invocation,
                cwd=request.workspace.root,
                environment=self._scratch_environment(),
                timeout_seconds=request.timeout_seconds,
                stdin_ref=stdin_ref,
                progress_deadline_seconds=request.progress_deadline_seconds,
                progress_digest=request.progress_digest,
            )
        )
        outcome = SeatOutcome.SUCCEEDED
        if command.stalled:
            outcome = SeatOutcome.STALLED
        elif command.timed_out:
            outcome = SeatOutcome.TIMED_OUT
        elif command.cancelled:
            outcome = SeatOutcome.CANCELLED
        elif command.exit_code != 0:
            outcome = SeatOutcome.FAILED
        raw = self._read(command.stdout_ref)
        if extra_output is not None and extra_output.is_file():
            raw = f"{raw}\n{extra_output.read_text(encoding='utf-8')}"
        usage = _normalise_usage(raw, self.adapter_name)
        with self._lock:
            self._usage[run_ref] = usage
        diagnostics = [f"stderr={command.stderr_ref}"]
        recovered = self._recover_result(request, run_ref)
        if outcome is not SeatOutcome.SUCCEEDED and recovered is not None:
            outcome = SeatOutcome.SUCCEEDED
            raw = recovered
            diagnostics.append("result recovered from the seat result file")
        payload = None
        if outcome is SeatOutcome.SUCCEEDED:
            data = _contract_object(raw, request.result_contract)
            normalised_ref = self._write_normalised(run_ref, data)
            payload = self._payload(request, data, normalised_ref)
        return SeatResult(
            run_ref,
            outcome,
            payload,
            command.stdout_ref,
            usage,
            command.started_at,
            command.ended_at,
            tuple(diagnostics),
            exit_code=command.exit_code,
            model=self._model(request.model_class),
            stall_sample_times=command.stall_sample_times,
        )

    def _recover_result(self, request: SeatRequest, run_ref: str) -> str | None:
        """Return a typed result the seat left behind by another route, or None.

        Adapters that give the seat a second route for its result (a file in the
        workspace) override this. The default has no second route.
        """

        return None

    def _materialise_evidence(self, request: SeatRequest) -> str:
        sections = [request.instructions]
        references = (("Approved brief", str(request.brief_path)),) + tuple(
            ("Evidence", reference) for reference in request.context_refs
        )
        for label, reference in references:
            path = Path(reference).expanduser()
            if not path.is_file():
                continue
            if path.stat().st_size > 1_048_576:
                raise EvidenceError(f"seat evidence exceeds 1 MiB: {path}")
            content = path.read_text(encoding="utf-8", errors="replace")
            sections.append(f"\n{label}: {path}\n\n{content}")
        return "\n".join(sections)

    def cancel(self, run_ref: str) -> None:
        self._commands.cancel(run_ref)

    def collect_usage(self, run_ref: str) -> SeatUsage:
        with self._lock:
            return self._usage.get(run_ref, SeatUsage(source=f"{self.adapter_name}:unavailable"))

    # -- lane classification ---------------------------------------------------
    # The structured error keys this adapter reads. Subclasses extend the tuple
    # to name the CLI-specific fields their JSON output carries; the values are
    # dictionary keys, never words matched against prose.
    _limit_code_keys: tuple[str, ...] = ("code", "type", "error_type", "error_code", "reason")
    _limit_reset_keys: tuple[str, ...] = ("reset_at", "resets_at", "reset_time")

    def classify_failure(self, result: SeatResult) -> LaneSignal | None:
        return self._structural_lane_signal(result)

    def _structural_lane_signal(self, result: SeatResult) -> LaneSignal | None:
        """Return a lane signal from structural evidence, or None.

        Only a seat the process itself ended in error is classified. A successful
        seat, or one the harness killed for stalling, timing out or cancellation,
        never cools a lane. A failure that produced no structured output at all is
        a spawn failure; otherwise the CLI's structured error fields are read for a
        limit or quota code. Free text is never scanned, so a report that mentions
        a limit in prose does not cool the lane."""

        if result.outcome is not SeatOutcome.FAILED:
            return None
        documents = self._output_documents(result)
        if not documents:
            return LaneSignal(LaneSignalKind.SPAWN, detail="no structured output before exit")
        return self._limit_signal(documents)

    def _output_documents(self, result: SeatResult) -> list[Any]:
        try:
            raw = self._read(result.raw_output_ref)
        except EvidenceError:
            return []
        return _documents(raw)

    def _limit_signal(self, documents: list[Any]) -> LaneSignal | None:
        for document in documents:
            for candidate in _walk(document):
                if not isinstance(candidate, dict):
                    continue
                signal = self._candidate_signal(candidate)
                if signal is not None:
                    return signal
        return None

    def _candidate_signal(self, candidate: dict[str, Any]) -> LaneSignal | None:
        containers = [candidate]
        nested = candidate.get("error")
        if isinstance(nested, dict):
            containers.append(nested)
        for container in containers:
            for key in self._limit_code_keys:
                kind = _kind_for_code(container.get(key))
                if kind is not None:
                    return LaneSignal(kind, reset_at=self._candidate_reset(candidate, nested), detail=str(container.get(key)))
        return None

    def _candidate_reset(self, candidate: dict[str, Any], nested: Any) -> str | None:
        for container in (candidate, nested if isinstance(nested, dict) else {}):
            for key in self._limit_reset_keys:
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _payload(self, request: SeatRequest, data: dict[str, Any], evidence_ref: str):
        if request.result_contract == "builder-report-v1":
            summary = data.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                raise EvidenceError("builder report summary is missing")
            return BuilderReport(evidence_ref, summary.strip())
        try:
            decision = ReviewDecision(str(data.get("decision")))
        except ValueError as exc:
            raise EvidenceError("review decision is invalid") from exc
        raw_findings = data.get("findings")
        if not isinstance(raw_findings, list):
            raise EvidenceError("review findings must be a list")
        findings: list[ReviewFinding] = []
        for value in raw_findings:
            if not isinstance(value, dict):
                raise EvidenceError("review finding is not an object")
            try:
                findings.append(
                    ReviewFinding(
                        code=str(value["code"]),
                        consequence=str(value["consequence"]),
                        evidence_ref=str(value["evidence_ref"]),
                        blocking=bool(value["blocking"]),
                        specialist_boundary=(
                            str(value["specialist_boundary"])
                            if value.get("specialist_boundary") is not None
                            else None
                        ),
                    )
                )
            except KeyError as exc:
                raise EvidenceError("review finding is incomplete") from exc
        verdict = ReviewVerdict(request.item_id, decision, tuple(findings), evidence_ref)
        rule_error = review_verdict_rule_error(verdict)
        if rule_error is not None:
            raise EvidenceError(rule_error)
        return verdict

    def _probe_run(self, label: str, argv: tuple[str, ...]):
        return self._commands.run(
            CommandRequest(
                f"probe:{self.adapter_name}:{label}",
                argv,
                self._output_root,
                environment=self._scratch_environment(),
                timeout_seconds=20,
            )
        )

    def _scratch_environment(self) -> tuple[tuple[str, str], ...]:
        return (*scratch_environment(self._output_root / "scratch"), *self.telemetry_environment)

    def _model(self, model_class: str) -> str:
        return self._model_map.get(model_class, model_class)

    def _write_schema(self, run_ref: str, contract: str) -> Path:
        root = self._output_root / "contracts"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{_safe_name(run_ref)}.schema.json"
        path.write_text(json.dumps(result_schema(contract), indent=2), encoding="utf-8")
        return path

    def _write_normalised(self, run_ref: str, data: dict[str, Any]) -> str:
        root = self._output_root / "normalised"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{_safe_name(run_ref)}.json"
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        return str(path)

    @staticmethod
    def _read(reference: str) -> str:
        path = Path(reference)
        if not path.is_file():
            raise EvidenceError(f"harness output is missing: {reference}")
        return path.read_text(encoding="utf-8", errors="replace")

    def _version_argv(self) -> tuple[str, ...]:
        raise NotImplementedError

    def _probe_authentication(self) -> tuple[bool, str]:
        raise NotImplementedError

    def _invocation(
        self, request: SeatRequest, run_ref: str
    ) -> tuple[tuple[str, ...], Path | None, str | None]:
        raise NotImplementedError

    @staticmethod
    def _require_known_tools(tools: frozenset[str]) -> None:
        known = {"read", "write", "shell", "python", "git"}
        unknown = tools - known
        if unknown:
            raise CapabilityError(f"harness cannot map semantic tools: {sorted(unknown)}")

    @staticmethod
    def environment_has_github_token() -> bool:
        return any(os.environ.get(name) for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"))
