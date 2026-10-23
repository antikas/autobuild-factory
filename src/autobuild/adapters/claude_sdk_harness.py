"""Claude Agent SDK harness adapter.

Seats run in process through the Claude Agent SDK, which drives its bundled
Claude Code CLI on an API key. The seat contract is the Claude Code lane's own:
the adapter builds the same seat vector the CLI lane would run and translates it
into SDK options, so tools, model, effort, contract prompt and result schema
have one owner. The SDK package is an optional extra and is imported only when a
seat or a probe of this lane runs.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
import threading
import time
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from autobuild.adapters.claude_harness import ClaudeCodeHarnessAdapter
from autobuild.adapters.harness_cli import _contract_object, _kind_for_code, _normalise_usage, _safe_name
from autobuild.domain import (
    AdapterIdentity,
    CapabilityError,
    LaneSignal,
    LaneSignalKind,
    ProbeResult,
    SeatOutcome,
    SeatRequest,
    SeatResult,
)
from autobuild.domain.models import SeatTransport
from autobuild.ports import CommandPort

SDK_MODULE = "claude_agent_sdk"
SDK_EXTRA = "autobuild-factory[sdk]"

# Records the adapter itself writes into a seat's message stream.
_ADAPTER_RECORD = "AutobuildSeatError"
_KEY_FILE_MISSING = "key_file_missing"
_SDK_EXCEPTION = "sdk_exception"

# The API's own error text for an exhausted credit balance and for a reached
# workspace or organisation spend limit. Neither carries a distinct structured
# code (both arrive as an invalid request), so the relayed API message is read,
# and only from a result the CLI marks as an API error.
_QUOTA_MESSAGES = (
    re.compile(r"credit balance is too low", re.IGNORECASE),
    re.compile(r"reached your specified (?:workspace )?api usage limits", re.IGNORECASE),
)
_REGAIN_ACCESS = re.compile(
    r"regain access on (\d{4}-\d{2}-\d{2}) at (\d{2}:\d{2}) UTC", re.IGNORECASE
)
# The SDK's structured assistant-message error values.
_ASSISTANT_ERRORS: dict[str, LaneSignalKind] = {
    "billing_error": LaneSignalKind.QUOTA,
    "rate_limit": LaneSignalKind.RATE_LIMIT,
    "authentication_failed": LaneSignalKind.AUTH,
}

# Seat-vector options and how each reaches the SDK. The SDK always runs its CLI
# in streaming print mode, so the print and output-format options are its own.
_SDK_FLAGS = frozenset({"--safe-mode", "--no-session-persistence"})
_SDK_OWNED = frozenset({"--print"})
_VALUE_OPTIONS = frozenset(
    {
        "--permission-mode",
        "--append-system-prompt",
        "--session-id",
        "--model",
        "--effort",
        "--tools",
        "--allowedTools",
        "--output-format",
        "--json-schema",
    }
)
_POLL_SECONDS = 0.2
_SHUTDOWN_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class SdkSeatSpec:
    """Everything one SDK seat needs, independent of the SDK's own types."""

    prompt: str
    cwd: Path
    model: str
    tools: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    append_system_prompt: str
    json_schema: Mapping[str, Any]
    permission_mode: str
    session_id: str
    effort: str | None
    flags: tuple[str, ...]
    settings: Mapping[str, Any]
    environment: tuple[tuple[str, str], ...]
    setting_sources: tuple[str, ...] = ()


class AgentSdkClient(Protocol):
    """The seam between the adapter and the SDK; tests supply a fake."""

    def version(self) -> str:
        """The SDK version. Raises ImportError when the SDK is not installed."""
        ...

    def run(
        self,
        spec: SdkSeatSpec,
        on_message: Callable[[dict[str, Any]], None],
        on_stderr: Callable[[str], None],
        cancelled: threading.Event,
    ) -> None:
        """Run one seat, passing each message as a plain record, until the
        stream ends or ``cancelled`` is set."""
        ...


def agent_options(sdk: Any, spec: SdkSeatSpec, on_stderr: Callable[[str], None] | None = None) -> Any:
    """Build the SDK's options object for one seat."""

    return sdk.ClaudeAgentOptions(
        tools=list(spec.tools),
        allowed_tools=list(spec.allowed_tools),
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": spec.append_system_prompt,
        },
        setting_sources=list(spec.setting_sources),
        permission_mode=spec.permission_mode,
        model=spec.model,
        effort=spec.effort,
        session_id=spec.session_id,
        cwd=str(spec.cwd),
        env=dict(spec.environment),
        settings=json.dumps(dict(spec.settings), separators=(",", ":")),
        output_format={"type": "json_schema", "schema": dict(spec.json_schema)},
        extra_args={flag: None for flag in spec.flags},
        stderr=on_stderr,
    )


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {"block": type(value).__name__, **{f.name: getattr(value, f.name) for f in fields(value)}}
    return str(value)


def message_record(message: Any) -> dict[str, Any]:
    """One SDK message as a JSON-ready record named by its message class."""

    if is_dataclass(message) and not isinstance(message, type):
        data = {f.name: getattr(message, f.name) for f in fields(message)}
    else:
        data = dict(getattr(message, "__dict__", {}))
    return json.loads(json.dumps({"message": type(message).__name__, **data}, default=_plain))


class ClaudeAgentSdkClient:
    """The real client: imports the SDK lazily and streams one query."""

    def version(self) -> str:
        sdk = importlib.import_module(SDK_MODULE)
        return str(getattr(sdk, "__version__", "unknown"))

    def run(self, spec, on_message, on_stderr, cancelled) -> None:
        sdk = importlib.import_module(SDK_MODULE)
        options = agent_options(sdk, spec, on_stderr)
        asyncio.run(self._consume(sdk, spec.prompt, options, on_message, cancelled))

    @staticmethod
    async def _consume(sdk, prompt, options, on_message, cancelled) -> None:
        async def stream() -> None:
            async for message in sdk.query(prompt=prompt, options=options):
                on_message(message_record(message))

        consumer = asyncio.ensure_future(stream())
        while not consumer.done():
            if cancelled.is_set():
                consumer.cancel()
                break
            await asyncio.wait({consumer}, timeout=_POLL_SECONDS)
        try:
            await consumer
        except asyncio.CancelledError:
            return


def _reset_from(text: str) -> str | None:
    match = _REGAIN_ACCESS.search(text)
    if match is None:
        return None
    return f"{match.group(1)}T{match.group(2)}:00+00:00"


def _is_auth_retry(record: Mapping[str, Any]) -> bool:
    """A CLI retry announcement for a request the API refused the key for."""

    if record.get("message") != "SystemMessage" or record.get("subtype") != "api_retry":
        return False
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    error = record.get("error", data.get("error"))
    status = record.get("error_status", data.get("error_status"))
    return error == "authentication_failed" or status in (401, 403)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ClaudeSdkHarnessAdapter(ClaudeCodeHarnessAdapter):
    """Claude Code seats through the Agent SDK, billed to an API key.

    The key never enters an environment: the SDK's CLI reads it through an
    ``apiKeyHelper`` command named in flag settings, so seat subprocesses do not
    inherit it. The CLI child gets a fresh empty ``CLAUDE_CONFIG_DIR`` per seat,
    loads no setting sources, and has every API-billing and redirect variable
    set empty, which the CLI treats as unset. A missing key file fails the probe;
    no OAuth credential is consulted.
    """

    adapter_name = "claude-sdk"
    meter_id = None
    removed_environment = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
    )

    def __init__(
        self,
        command_port: CommandPort,
        output_root: Path,
        key_file: Path | None,
        model_map: Mapping[str, str] | None = None,
        client: AgentSdkClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # The command names the SDK's bundled CLI for the inherited seat-vector
        # builder; this adapter never runs it through the command port.
        super().__init__(command_port, output_root, (SDK_MODULE,), model_map)
        self._key_file = key_file.expanduser().resolve(strict=False) if key_file else None
        self._client = client if client is not None else ClaudeAgentSdkClient()
        self._clock = clock
        self._active: dict[str, threading.Event] = {}

    def meter_scope_covers(self, scope: str, model_class: str) -> bool:
        return False

    # -- probe -------------------------------------------------------------------

    def _key_problem(self) -> str | None:
        if self._key_file is None:
            return "the claude-sdk lane table sets no key_file"
        if any(mark in str(self._key_file) for mark in ('"', "$", "`")):
            return f"the key file path contains a shell quoting character: {self._key_file}"
        if not self._key_file.is_file():
            return f"the API key file is missing: {self._key_file}"
        try:
            # Read only to confirm a key is present; the value is never kept.
            present = bool(self._key_file.read_text(encoding="utf-8-sig").strip())
        except (OSError, UnicodeDecodeError):
            return f"the API key file is unreadable: {self._key_file}"
        if not present:
            return f"the API key file is empty: {self._key_file}"
        return None

    def probe(self) -> ProbeResult:
        self._output_root.mkdir(parents=True, exist_ok=True)
        problem = self._key_problem()
        if problem is not None:
            return ProbeResult.unavailable(problem)
        try:
            version = self._client.version()
        except ImportError:
            return ProbeResult.unavailable(
                f"the {SDK_MODULE} package is not installed; install {SDK_EXTRA}"
            )
        except Exception as exc:
            return ProbeResult.unavailable(f"the Agent SDK probe failed: {type(exc).__name__}")
        return ProbeResult.ready(
            AdapterIdentity(self.adapter_name, version, self.capabilities),
            "API key file present; no OAuth credential is read",
        )

    # -- seat ----------------------------------------------------------------------

    def _key_helper(self) -> str:
        assert self._key_file is not None
        # Bytes to stdout, so a key file with a byte-order mark or a console code
        # page that cannot encode a character never crashes the helper.
        code = (
            "import sys;sys.stdout.buffer.write("
            "open(sys.argv[1],encoding='utf-8-sig').read().strip().encode('utf-8'))"
        )
        return f'"{sys.executable}" -c "{code}" "{self._key_file.as_posix()}"'

    def _settings(self) -> dict[str, Any]:
        assert self._key_file is not None
        posix = self._key_file.as_posix()
        return {
            "apiKeyHelper": self._key_helper(),
            "disableAllHooks": True,
            "permissions": {"deny": [f"Read({posix})", f"Read(//{posix.lstrip('/')})"]},
        }

    def _child_environment(self, config_dir: Path | None) -> tuple[tuple[str, str], ...]:
        environment = dict(self._scratch_environment())
        if config_dir is not None:
            environment["CLAUDE_CONFIG_DIR"] = str(config_dir)
        for name in self.removed_environment:
            environment[name] = ""
        return tuple(environment.items())

    def _fresh_config_dir(self, run_ref: str) -> Path:
        path = self._output_root / "config" / f"{_safe_name(run_ref)}-{uuid4().hex[:12]}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def seat_spec(self, request: SeatRequest, run_ref: str, config_dir: Path | None) -> SdkSeatSpec:
        """Translate the Claude Code seat vector for ``request`` into SDK options."""

        argv, _extra, stdin_ref = self._invocation(request, run_ref)
        assert stdin_ref is not None
        values: dict[str, str] = {}
        flags: list[str] = []
        options = list(argv[len(self._command) :])
        while options:
            option = options.pop(0)
            if option in _SDK_FLAGS:
                flags.append(option.removeprefix("--"))
            elif option in _SDK_OWNED:
                continue
            elif option in _VALUE_OPTIONS and options:
                values[option] = options.pop(0)
            else:
                raise CapabilityError(f"the claude-sdk lane cannot carry the seat option {option}")
        if values.get("--output-format") != "json":
            raise CapabilityError("the claude-sdk lane needs the seat's structured JSON result")
        tools = tuple(part for part in values.get("--tools", "").split(",") if part)
        allowed = tuple(part for part in values.get("--allowedTools", "").split(",") if part)
        return SdkSeatSpec(
            prompt=Path(stdin_ref).read_text(encoding="utf-8"),
            cwd=request.workspace.root,
            model=values["--model"],
            tools=tools,
            allowed_tools=allowed,
            append_system_prompt=values["--append-system-prompt"],
            json_schema=json.loads(values["--json-schema"]),
            permission_mode=values["--permission-mode"],
            session_id=values["--session-id"],
            effort=values.get("--effort"),
            flags=tuple(flags),
            settings=self._settings() if self._key_file is not None else {},
            environment=self._child_environment(config_dir),
        )

    def seat_transport(self, request: SeatRequest) -> SeatTransport:
        """How a seat reaches the SDK's CLI, for the preflight doctor."""

        argv, _extra, stdin_ref = self._invocation(request, "preflight:doctor")
        settings = json.dumps(self._settings() if self._key_file is not None else {})
        parts = (*argv, settings)
        return SeatTransport(
            max_argv_element=max(len(part.encode("utf-8")) for part in parts),
            delivered_by_file=stdin_ref is not None,
            child_environment=self._child_environment(None),
            in_process=True,
        )

    def invoke(self, request: SeatRequest) -> SeatResult:
        run_ref = f"{request.run_id}:{request.item_id}:{request.seat.value}:{self.adapter_name}"
        prepared = replace(request, instructions=self._materialise_evidence(request))
        streams = self._output_root / "sdk"
        streams.mkdir(parents=True, exist_ok=True)
        raw_path = streams / f"{_safe_name(run_ref)}.jsonl"
        stderr_path = streams / f"{_safe_name(run_ref)}.stderr.txt"
        raw_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        started_at = _now()
        problem = self._key_problem()
        if problem is not None:
            self._append(raw_path, {"message": _ADAPTER_RECORD, "error": {"type": _KEY_FILE_MISSING}})
            outcome = SeatOutcome.FAILED
        else:
            spec = self.seat_spec(prepared, run_ref, self._fresh_config_dir(run_ref))
            outcome = self._run(request, run_ref, spec, raw_path, stderr_path)
        ended_at = _now()
        raw = raw_path.read_text(encoding="utf-8")
        usage = _normalise_usage(raw, self.adapter_name)
        with self._lock:
            self._usage[run_ref] = usage
        diagnostics = [f"stderr={stderr_path}"]
        recovered = self._recover_result(request, run_ref)
        if outcome is not SeatOutcome.SUCCEEDED and recovered is not None:
            outcome = SeatOutcome.SUCCEEDED
            raw = recovered
            diagnostics.append("result recovered from the seat result file")
        payload = None
        if outcome is SeatOutcome.SUCCEEDED:
            data = _contract_object(raw, request.result_contract)
            payload = self._payload(request, data, self._write_normalised(run_ref, data))
        exit_code = {SeatOutcome.SUCCEEDED: 0, SeatOutcome.FAILED: 1}.get(outcome)
        return SeatResult(
            run_ref,
            outcome,
            payload,
            str(raw_path),
            usage,
            started_at,
            ended_at,
            tuple(diagnostics),
            exit_code=exit_code,
            model=self._model(request.model_class),
        )

    def _run(
        self,
        request: SeatRequest,
        run_ref: str,
        spec: SdkSeatSpec,
        raw_path: Path,
        stderr_path: Path,
    ) -> SeatOutcome:
        """Run the seat on a worker thread, enforcing its timeout, stall deadline
        and cancellation, and return its outcome."""

        cancelled = threading.Event()
        state: dict[str, Any] = {"messages": 0, "result": None, "error": None, "auth_stop": False}
        progress = {"at": self._clock()}
        write = threading.Lock()

        def on_message(record: dict[str, Any]) -> None:
            with write:
                self._append(raw_path, record)
                state["messages"] += 1
                if record.get("message") == "ResultMessage":
                    state["result"] = record
                if _is_auth_retry(record):
                    # The CLI would retry a refused key for minutes; stop now.
                    state["auth_stop"] = True
                    cancelled.set()
            progress["at"] = self._clock()

        def on_stderr(line: str) -> None:
            with write, stderr_path.open("a", encoding="utf-8") as handle:
                handle.write(line.rstrip("\n") + "\n")

        def work() -> None:
            try:
                self._client.run(spec, on_message, on_stderr, cancelled)
            except Exception as exc:
                state["error"] = exc

        with self._lock:
            self._active[run_ref] = cancelled
        worker = threading.Thread(target=work, name=f"sdk-seat-{_safe_name(run_ref)}", daemon=True)
        started = self._clock()
        digest = request.progress_digest() if request.progress_digest else None
        stopped: SeatOutcome | None = None
        worker.start()
        try:
            while worker.is_alive():
                worker.join(_POLL_SECONDS)
                if not worker.is_alive():
                    break
                now = self._clock()
                if cancelled.is_set():
                    stopped = SeatOutcome.CANCELLED
                elif now - started > request.timeout_seconds:
                    stopped = SeatOutcome.TIMED_OUT
                elif request.progress_deadline_seconds > 0:
                    current = request.progress_digest() if request.progress_digest else None
                    if current != digest:
                        digest = current
                        progress["at"] = now
                    if now - progress["at"] > request.progress_deadline_seconds:
                        stopped = SeatOutcome.STALLED
                if stopped is not None:
                    cancelled.set()
                    worker.join(_SHUTDOWN_SECONDS)
                    break
        finally:
            with self._lock:
                self._active.pop(run_ref, None)
        if state["auth_stop"]:
            return SeatOutcome.FAILED
        if stopped is None and cancelled.is_set():
            stopped = SeatOutcome.CANCELLED
        if stopped is not None:
            return stopped
        result = state["result"]
        if result is None:
            if state["error"] is not None:
                self._append(
                    raw_path,
                    {
                        "message": _ADAPTER_RECORD,
                        "error": {
                            "type": _SDK_EXCEPTION,
                            "exception": type(state["error"]).__name__,
                            "before_first_message": state["messages"] == 0,
                        },
                    },
                )
            return SeatOutcome.FAILED
        return SeatOutcome.FAILED if result.get("is_error") else SeatOutcome.SUCCEEDED

    @staticmethod
    def _append(path: Path, record: Mapping[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def cancel(self, run_ref: str) -> None:
        with self._lock:
            event = self._active.get(run_ref)
        if event is not None:
            event.set()

    # -- lane classification -------------------------------------------------------

    def classify_failure(self, result: SeatResult) -> LaneSignal | None:
        """Read a lane signal from the seat's structured message records only."""

        return self._structural_lane_signal(result)

    def _limit_signal(self, documents: list[Any]) -> LaneSignal | None:
        # Only top-level message fields are read. Message content (tool input,
        # tool output, file text) is never walked, so a file that mentions an
        # error code cannot cool the lane. A signal that names its reset wins.
        signals = [
            signal
            for document in documents
            if isinstance(document, dict)
            for signal in (self._record_signal(document),)
            if signal is not None
        ]
        timed = [signal for signal in signals if signal.reset_at]
        return (timed or signals or [None])[0]

    def _record_signal(self, record: dict[str, Any]) -> LaneSignal | None:
        kind = record.get("message")
        if kind == _ADAPTER_RECORD:
            return self._adapter_signal(record.get("error"))
        if kind == "AssistantMessage":
            error = record.get("error")
            mapped = _ASSISTANT_ERRORS.get(error) if isinstance(error, str) else None
            return LaneSignal(mapped, detail=error) if mapped is not None else None
        if kind == "ResultMessage":
            return self._api_error_signal(record)
        if _is_auth_retry(record):
            return LaneSignal(LaneSignalKind.AUTH, detail="api_retry authentication_failed")
        return None

    @staticmethod
    def _adapter_signal(error: Any) -> LaneSignal | None:
        if not isinstance(error, dict):
            return None
        if error.get("type") == _KEY_FILE_MISSING:
            return LaneSignal(LaneSignalKind.AUTH, detail=_KEY_FILE_MISSING)
        if error.get("type") == _SDK_EXCEPTION and error.get("before_first_message"):
            return LaneSignal(LaneSignalKind.SPAWN, detail=str(error.get("exception")))
        return None

    @staticmethod
    def _api_error_signal(candidate: dict[str, Any]) -> LaneSignal | None:
        """Classify a result the CLI reports as a failed API call.

        ``api_error_status`` is set only when an API request failed, so the
        ``result`` text is then the API's relayed error message, never the
        seat's own prose."""

        status = candidate.get("api_error_status")
        if not candidate.get("is_error") or not isinstance(status, int):
            return None
        text = candidate.get("result") if isinstance(candidate.get("result"), str) else ""
        if any(pattern.search(text) for pattern in _QUOTA_MESSAGES):
            return LaneSignal(LaneSignalKind.QUOTA, reset_at=_reset_from(text), detail=f"api {status}")
        kind = _kind_for_code(str(status))
        if kind is not None:
            return LaneSignal(kind, detail=f"api {status}")
        return None
