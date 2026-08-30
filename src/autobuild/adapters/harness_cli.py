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
    ProbeResult,
    ReviewDecision,
    ReviewFinding,
    ReviewVerdict,
    Seat,
    SeatOutcome,
    SeatRequest,
    SeatResult,
    SeatUsage,
)
from autobuild.ports import CommandPort


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
                    "specialist_boundary": {"type": ["string", "null"]},
                },
                "required": [
                    "code",
                    "consequence",
                    "evidence_ref",
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
        invocation, extra_output = self._invocation(prepared, run_ref)
        command = self._commands.run(
            CommandRequest(
                command_id=run_ref,
                argv=invocation,
                cwd=request.workspace.root,
                environment=self._scratch_environment(),
                timeout_seconds=request.timeout_seconds,
            )
        )
        outcome = SeatOutcome.SUCCEEDED
        if command.timed_out:
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
            (f"stderr={command.stderr_ref}",),
        )

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
                        specialist_boundary=(
                            str(value["specialist_boundary"])
                            if value.get("specialist_boundary") is not None
                            else None
                        ),
                    )
                )
            except KeyError as exc:
                raise EvidenceError("review finding is incomplete") from exc
        if decision is not ReviewDecision.PASS and not findings:
            raise EvidenceError("a blocking review decision requires a concrete finding")
        return ReviewVerdict(request.item_id, decision, tuple(findings), evidence_ref)

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
        return scratch_environment(self._output_root / "scratch")

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

    def _invocation(self, request: SeatRequest, run_ref: str) -> tuple[tuple[str, ...], Path | None]:
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
