"""Read the included ChatGPT Codex allowance through the local app server.

The app-server usage surface is undocumented and may change without notice.
"""

from __future__ import annotations

import json
import math
import os
import queue
import selectors
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from autobuild.domain import (
    AdapterIdentity,
    CommandRequest,
    ProbeResult,
    UsageFailure,
    UsageLimit,
    UsageLimitKind,
    UsageReading,
)
from autobuild.ports import CommandPort
from autobuild.ports.contracts import SignInStatePort

CHATGPT_SUBSCRIPTION_METER = "chatgpt-subscription"
_CHATGPT_INCLUDED_PLANS = frozenset({"plus", "prolite", "pro", "promax", "team", "business", "enterprise"})
_REMOVED = (
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT",
)


def _subscription_removed_environment(base: tuple[str, ...]) -> tuple[str, ...]:
    """Strip inherited alternate-provider routing without reading its values."""

    markers = (
        "API_KEY",
        "AUTH_TOKEN",
        "ACCESS_TOKEN",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "COOKIE",
        "CREDENTIAL",
        "BASE",
        "PROVIDER",
        "ENDPOINT",
        "KEY_HELPER",
    )
    dynamic = (
        name for name in os.environ if any(marker in name.upper() for marker in markers)
    )
    fixed = (
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_API_KEY",
        "CLAUDE_CONFIG_DIR",
        "CODEX_MODEL_PROVIDER",
        "CODEX_PROVIDER",
        "CODEX_API_KEY",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "SSLKEYLOGFILE",
        "SSH_AUTH_SOCK",
        "GIT_ASKPASS",
    )
    return tuple(dict.fromkeys((*base, *fixed, *dynamic)))


class CodexSubscriptionMeterAdapter:
    """A fail-closed reader for the local Codex ChatGPT subscription."""

    meter_ids = frozenset({CHATGPT_SUBSCRIPTION_METER})

    def __init__(self, command: CommandPort, output_root: Path) -> None:
        self._command = command
        self._root = output_root

    def probe(self) -> ProbeResult:
        reading = self.read(CHATGPT_SUBSCRIPTION_METER)
        if not reading.ok:
            return ProbeResult.unavailable(reading.error)
        return ProbeResult.ready(
            AdapterIdentity(
                "codex-subscription", "app-server", frozenset({"read-only"})
            ),
            reading.fetched_at,
        )

    def read(self, meter_id: str) -> UsageReading:
        now = datetime.now(UTC).isoformat()
        if meter_id != CHATGPT_SUBSCRIPTION_METER:
            return UsageReading.failed(
                meter_id, "unknown Codex subscription meter", now
            )
        try:
            documents = self._request()
            return _reading(_response(documents, 2), _response(documents, 3), now)
        except Exception as exc:
            # A missing or malformed answer is a ValueError and can be the
            # provider's refusal; the others mean no answer arrived.
            lost = isinstance(exc, (OSError, TimeoutError, EOFError))
            return UsageReading.failed(
                meter_id,
                f"Codex app-server reading failed: {type(exc).__name__}",
                now,
                UsageFailure.TRANSPORT if lost else None,
            )

    def _request(self) -> list[object]:
        self._root.mkdir(parents=True, exist_ok=True)
        requests = (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "autobuild", "version": "1"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "account/read",
                "params": {"refreshToken": False},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "account/rateLimits/read",
                "params": None,
            },
        )
        removed = set(_subscription_removed_environment(_REMOVED))
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in removed
        }
        process = subprocess.Popen(
            ("codex", "app-server", "--stdio"),
            cwd=self._root,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        deadline = time.monotonic() + 20.0
        buffer = bytearray()
        try:
            answers = []
            for request in requests:
                assert process.stdin is not None
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
                answers.append(_await_response(process, int(request["id"]), deadline, buffer))
                if request["id"] == 1:
                    process.stdin.write(
                        json.dumps(
                            {"jsonrpc": "2.0", "method": "initialized", "params": {}}
                        )
                        + "\n"
                    )
                    process.stdin.flush()
            return answers
        finally:
            if process.stdin is not None:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)


def _response(documents: list[object], request_id: int) -> dict[str, object]:
    for document in documents:
        if (
            isinstance(document, dict)
            and document.get("id") == request_id
            and isinstance(document.get("result"), dict)
        ):
            return document["result"]
    raise ValueError(f"missing response {request_id}")


def _await_response(
    process: subprocess.Popen[str], request_id: int, deadline: float, buffer: bytearray
) -> object:
    """Read notifications past without logging them, returning the matching id."""

    assert process.stdout is not None
    selector = None
    reader = None
    if os.name == "nt":
        reader = getattr(process, "_autobuild_pipe_reader", None)
        if reader is None:
            frames: queue.Queue[bytes | None] = queue.Queue()
            def pump() -> None:
                try:
                    while True:
                        chunk = os.read(process.stdout.fileno(), 65536)
                        frames.put(chunk or None)
                        if not chunk:
                            return
                except OSError:
                    frames.put(None)
            threading.Thread(target=pump, daemon=True).start()
            reader = frames
            setattr(process, "_autobuild_pipe_reader", reader)
    else:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while True:
            # A notification and response often arrive in the same pipe write.
            # Drain frames already in memory before polling the OS descriptor.
            newline = buffer.find(b"\n")
            if newline < 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Codex app-server read timed out")
                if reader is not None:
                    try:
                        chunk = reader.get(timeout=remaining)
                    except queue.Empty as exc:
                        raise TimeoutError("Codex app-server read timed out") from exc
                else:
                    assert selector is not None
                    if not selector.select(remaining):
                        raise TimeoutError("Codex app-server read timed out")
                    chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Codex app-server closed its response pipe")
                buffer.extend(chunk)
                if len(buffer) > 2_097_152:
                    raise ValueError("Codex usage response exceeds frame limit")
                continue
            line = bytes(buffer[:newline])
            del buffer[:newline + 1]
            document = json.loads(line)
            if isinstance(document, dict) and document.get("id") == request_id:
                return document
    finally:
        if selector is not None:
            selector.close()


_WINDOW_KINDS = {300: UsageLimitKind.SESSION, 10080: UsageLimitKind.WEEKLY_ALL}


def _window(value: object, scope: str) -> UsageLimit:
    if not isinstance(value, dict):
        raise ValueError("missing rate-limit window")
    duration = value.get("windowDurationMins")
    kind = (
        _WINDOW_KINDS.get(duration)
        if isinstance(duration, int) and not isinstance(duration, bool)
        else None
    )
    percent, reset = value.get("usedPercent"), value.get("resetsAt")
    if (
        kind is None
        or isinstance(percent, bool)
        or not isinstance(percent, (int, float))
        or not math.isfinite(float(percent))
        or not 0 <= float(percent) <= 100
        or isinstance(reset, bool)
        or not isinstance(reset, (int, float))
    ):
        raise ValueError("invalid rate-limit window")
    reset_at = datetime.fromtimestamp(reset, UTC)
    if reset_at <= datetime.now(UTC):
        raise ValueError("rate-limit reset is not in the future")
    return UsageLimit(kind, float(percent), reset_at.isoformat(), scope)


def _windows(snapshot: dict[str, object]) -> tuple[UsageLimit, ...]:
    limits: list[UsageLimit] = []
    for name in ("primary", "secondary"):
        value = snapshot.get(name)
        if value is None:
            continue
        limits.append(_window(value, "codex"))
    if not limits:
        raise ValueError("no included rate-limit windows")
    if len({limit.kind for limit in limits}) != len(limits):
        raise ValueError("duplicate rate-limit windows")
    if not any(limit.kind is UsageLimitKind.WEEKLY_ALL for limit in limits):
        raise ValueError("missing weekly rate-limit window")
    return tuple(limits)


def _reading(
    account_reply: dict[str, object], limits_reply: dict[str, object], fetched_at: str
) -> UsageReading:
    account = account_reply.get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        return UsageReading.failed(
            CHATGPT_SUBSCRIPTION_METER,
            "Codex is not signed in through ChatGPT",
            fetched_at,
        )
    plan = account.get("planType")
    if not isinstance(plan, str) or plan.lower() not in _CHATGPT_INCLUDED_PLANS:
        return UsageReading.failed(
            CHATGPT_SUBSCRIPTION_METER,
            "Codex ChatGPT plan is not an included subscription",
            fetched_at,
        )
    if limits_reply.get("ordinaryUsageAllowed") is not True:
        return UsageReading.failed(
            CHATGPT_SUBSCRIPTION_METER,
            "ordinary included Codex usage is unavailable",
            fetched_at,
        )
    buckets = limits_reply.get("rateLimitsByLimitId")
    snapshot = (
        buckets.get("codex")
        if isinstance(buckets, dict)
        else limits_reply.get("rateLimits")
    )
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("rateLimitReachedType") is not None
        or snapshot.get("spendControlReached") not in {None, False}
    ):
        return UsageReading.failed(
            CHATGPT_SUBSCRIPTION_METER,
            "Codex usage is paid, stopped, or malformed",
            fetched_at,
        )
    # Included ChatGPT plans can report a positive credit balance while ordinary
    # subscription usage remains allowed. The server's explicit ordinary-usage
    # flag is authoritative; a balance alone is neither paid routing nor a stop.
    try:
        windows = _windows(snapshot)
    except ValueError as exc:
        return UsageReading.failed(CHATGPT_SUBSCRIPTION_METER, str(exc), fetched_at)
    return UsageReading(
        CHATGPT_SUBSCRIPTION_METER, True, windows, fetched_at=fetched_at
    )


class CompositeUsageMeterAdapter:
    """One port over disjoint provider meters, without provider fallback."""

    def __init__(self, *meters) -> None:
        self._meters = tuple(meters)
        self.meter_ids = frozenset().union(*(meter.meter_ids for meter in meters))

    def probe(self) -> ProbeResult:
        """Ready when it has a meter to route to. It never reads a provider:
        each lane's own probe does that, once, through the shared reading."""

        if not self._meters:
            return ProbeResult.unavailable("no subscription meter is configured")
        return ProbeResult.ready(
            AdapterIdentity("composite-usage-meter", "1", frozenset({"read-only"}))
        )

    def read(self, meter_id: str) -> UsageReading:
        for meter in self._meters:
            if meter_id in meter.meter_ids:
                return meter.read(meter_id)
        return UsageReading.failed(meter_id, "no subscription meter serves this lane")

    def sign_in_failure(self, meter_id: str) -> UsageReading | None:
        for meter in self._meters:
            if meter_id in meter.meter_ids and isinstance(meter, SignInStatePort):
                return meter.sign_in_failure(meter_id)
        return None
