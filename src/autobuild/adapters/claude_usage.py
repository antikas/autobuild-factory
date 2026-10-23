"""Claude subscription usage meter, read through the command port.

The meter is the endpoint the Claude Code client reads for its own usage view:
``GET https://api.anthropic.com/api/oauth/usage`` with the subscription's OAuth
bearer token. The endpoint is undocumented, so every failure becomes a reading
with ``ok=False`` and is never raised.

The token is read from the Claude Code credentials file on every call and handed
to curl on standard input through a pipe (curl reads the header with ``-H @-``).
It is never placed on a command line, written to disk, kept on the adapter or
included in an error. curl runs with ``--ssl-no-revoke`` and without
``SSLKEYLOGFILE``, so no Python TLS stack is on the meter path.
"""

from __future__ import annotations

import json
import math
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from autobuild import __version__
from autobuild.domain import (
    AdapterIdentity,
    CommandRequest,
    ProbeResult,
    UsageLimit,
    UsageLimitKind,
    UsageReading,
)
from autobuild.ports import CommandPort

# The meter id the Claude Code harness adapter declares for its lanes.
CLAUDE_SUBSCRIPTION_METER = "claude-subscription"
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
CURL_MAX_SECONDS = 8
COMMAND_TIMEOUT_SECONDS = 15.0
_ERROR_DETAIL_CHARACTERS = 120
_KINDS = {kind.value: kind for kind in UsageLimitKind}


def default_credentials_path() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def _now() -> datetime:
    return datetime.now(UTC)


class _Unusable(ValueError):
    """A limit entry the meter returned that cannot be read safely."""


def _utc(value: object) -> str | None:
    """An offset-aware UTC ISO 8601 instant, or None when the meter gave none.

    A present value that is not a string, has no offset, cannot be parsed or
    falls outside the representable range is unusable, never guessed."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise _Unusable("has a reset time that is not text")
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            raise _Unusable("has a reset time without a UTC offset")
        return parsed.astimezone(UTC).isoformat()
    except (ValueError, OverflowError) as exc:
        if isinstance(exc, _Unusable):
            raise
        raise _Unusable("has an unreadable reset time") from exc


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "<redacted>") if secret else text


def normalise_usage(meter_id: str, raw: Any, fetched_at: str) -> UsageReading:
    """Turn the endpoint's JSON body into a reading.

    A limit whose kind is an unknown string is ignored. Any other malformed
    entry (a kind that is not a string, a percent that is not a finite number,
    an unusable reset time, a scope that is not an object) makes the whole
    reading fail, so a malformed body never passes as a low reading. The
    function is total: it never raises."""

    try:
        return _normalise(meter_id, raw, fetched_at)
    except _Unusable as exc:
        return UsageReading.failed(meter_id, f"meter limit {exc}", fetched_at)
    except Exception as exc:
        return UsageReading.failed(
            meter_id, f"meter response could not be read: {type(exc).__name__}", fetched_at
        )


def _normalise(meter_id: str, raw: Any, fetched_at: str) -> UsageReading:
    if not isinstance(raw, dict):
        return UsageReading.failed(meter_id, "meter response is not a JSON object", fetched_at)
    entries = raw.get("limits")
    if not isinstance(entries, list):
        error = raw.get("error")
        kind = error.get("type") if isinstance(error, dict) else None
        detail = f"meter returned error {kind}" if isinstance(kind, str) else "meter response has no limits"
        return UsageReading.failed(meter_id, detail, fetched_at)
    limits: list[UsageLimit] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return UsageReading.failed(meter_id, "meter response has a malformed limit", fetched_at)
        raw_kind = entry.get("kind")
        if not isinstance(raw_kind, str):
            raise _Unusable("has a kind that is not text")
        kind = _KINDS.get(raw_kind)
        if kind is None:
            continue
        percent = _number(entry.get("percent"))
        if percent is None:
            raise _Unusable(f"{kind.value} has no finite numeric percent")
        try:
            resets_at = _utc(entry.get("resets_at"))
        except _Unusable as exc:
            raise _Unusable(f"{kind.value} {exc}") from exc
        scope = entry.get("scope")
        if scope is not None and not isinstance(scope, dict):
            raise _Unusable(f"{kind.value} has a scope that is not an object")
        model = scope.get("model") if isinstance(scope, dict) else None
        label = model.get("display_name") if isinstance(model, dict) else None
        limits.append(
            UsageLimit(
                kind,
                percent,
                resets_at,
                label.strip() if isinstance(label, str) and label.strip() else None,
            )
        )
    extra = raw.get("extra_usage")
    enabled = extra.get("is_enabled") if isinstance(extra, dict) else None
    return UsageReading(
        meter_id,
        True,
        tuple(limits),
        "",
        enabled if isinstance(enabled, bool) else None,
        fetched_at,
    )


class ClaudeUsageMeterAdapter:
    """Read the Claude subscription meter with curl through the command port."""

    meter_ids = frozenset({CLAUDE_SUBSCRIPTION_METER})

    def __init__(
        self,
        command_port: CommandPort,
        output_root: Path,
        *,
        credentials_path: Path | None = None,
        curl: tuple[str, ...] = ("curl",),
        now: Callable[[], datetime] = _now,
    ) -> None:
        if not curl:
            raise ValueError("the meter's curl command must not be empty")
        self._commands = command_port
        self._output_root = output_root.resolve(strict=False)
        self._credentials = credentials_path or default_credentials_path()
        self._curl = curl
        self._now = now

    def probe(self) -> ProbeResult:
        executable = self._curl[0]
        if shutil.which(executable) is None and not Path(executable).is_file():
            return ProbeResult.unavailable(f"{executable} executable was not found")
        if not self._credentials.is_file():
            return ProbeResult.unavailable("the Claude Code credentials file is missing")
        return ProbeResult.ready(
            AdapterIdentity("claude-usage-meter", "1", frozenset({"read-only"})),
            USAGE_ENDPOINT,
        )

    def curl_argv(self) -> tuple[str, ...]:
        # ``-q`` must be the first argument: it stops curl reading a user
        # ``.curlrc`` that could turn on verbose or trace output and print the
        # Authorization header.
        return (
            *self._curl,
            "-q",
            "-s",
            "--ssl-no-revoke",
            "--max-time",
            str(CURL_MAX_SECONDS),
            USAGE_ENDPOINT,
            "-H",
            "@-",
            "-H",
            "anthropic-beta: oauth-2025-04-20",
            "-H",
            "anthropic-version: 2023-06-01",
            "-H",
            f"User-Agent: autobuild/{__version__}",
        )

    def read(self, meter_id: str) -> UsageReading:
        """Read the meter once. Total by construction: any exception becomes a
        failed reading that names only the exception type."""

        try:
            fetched_at = self._now().isoformat()
        except Exception:
            fetched_at = ""
        try:
            return self._read(meter_id, fetched_at)
        except Exception as exc:
            return UsageReading.failed(
                meter_id, f"meter read failed: {type(exc).__name__}", fetched_at
            )

    def _read(self, meter_id: str, fetched_at: str) -> UsageReading:
        if meter_id not in self.meter_ids:
            return UsageReading.failed(meter_id, "this adapter does not serve the meter", fetched_at)
        token = self._token()
        if not token:
            return UsageReading.failed(
                meter_id, "no OAuth access token in the Claude Code credentials file", fetched_at
            )
        try:
            self._output_root.mkdir(parents=True, exist_ok=True)
            result = self._commands.run(
                CommandRequest(
                    command_id=f"usage-meter:{uuid4().hex[:8]}",
                    argv=self.curl_argv(),
                    cwd=self._output_root,
                    timeout_seconds=COMMAND_TIMEOUT_SECONDS,
                    remove_environment=("SSLKEYLOGFILE",),
                    stdin_text=f"Authorization: Bearer {token}\n",
                    redact=(token,),
                )
            )
        except Exception as exc:
            return UsageReading.failed(
                meter_id, f"meter command failed: {type(exc).__name__}", fetched_at
            )
        if result.timed_out or result.cancelled:
            return UsageReading.failed(meter_id, "meter command did not finish", fetched_at)
        stdout = self._text(result.stdout_ref)
        if result.exit_code != 0 or not stdout.strip():
            detail = _redact(self._text(result.stderr_ref), token).strip()
            detail = detail[:_ERROR_DETAIL_CHARACTERS]
            message = f"meter transport failed (exit {result.exit_code})"
            return UsageReading.failed(
                meter_id, f"{message}: {detail}" if detail else message, fetched_at
            )
        try:
            raw = json.loads(stdout)
        except ValueError:
            return UsageReading.failed(meter_id, "meter response is not JSON", fetched_at)
        return normalise_usage(meter_id, raw, fetched_at)

    def _token(self) -> str | None:
        try:
            data = json.loads(self._credentials.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        return token if isinstance(token, str) and token.strip() else None

    @staticmethod
    def _text(reference: str) -> str:
        try:
            return Path(reference).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


class NullUsageMeterAdapter:
    """The meter for a configuration that has no meter adapter for an id.

    Every reading fails, so the profile's ``on_probe_failure`` decides."""

    def probe(self) -> ProbeResult:
        return ProbeResult.ready(AdapterIdentity("null-usage-meter", "1", frozenset({"read-only"})))

    def read(self, meter_id: str) -> UsageReading:
        return UsageReading.failed(
            meter_id, "no usage meter adapter serves this meter", _now().isoformat()
        )
