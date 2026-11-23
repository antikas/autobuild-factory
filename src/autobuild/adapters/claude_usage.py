"""Claude subscription usage meter, read through the command port.

The meter is the endpoint the Claude Code client reads for its own usage view:
``GET https://api.anthropic.com/api/oauth/usage`` with the subscription's OAuth
bearer token. The endpoint is undocumented and unsupported, and it may change
or stop working without notice, so every failure becomes a reading with
``ok=False`` and is never raised.

The token is read from the Claude Code credentials file on every call and handed
to curl on standard input through a pipe (curl reads the header with ``-H @-``).
It is never placed on a command line, written to disk, kept on the adapter or
included in an error. curl runs with ``--ssl-no-revoke`` and without
``SSLKEYLOGFILE``, so no Python TLS stack is on the meter path.

curl appends the response status and the ``retry-after`` value to the body; no
other response header and no request header is captured. The stored expiry of
the token is compared with the clock before a request, and an expired sign-in
makes none, because the endpoint can answer it as a rate limit.
"""

from __future__ import annotations

import json
import math
import re
import shutil
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from autobuild import __version__
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
from autobuild.ports.contracts import RATE_LIMIT_HOLD_SECONDS, SHARED_READING_SECONDS

# The meter id the Claude Code harness adapter declares for its lanes.
CLAUDE_SUBSCRIPTION_METER = "claude-subscription"
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
CURL_MAX_SECONDS = 8
COMMAND_TIMEOUT_SECONDS = 15.0
_ERROR_DETAIL_CHARACTERS = 120
_KINDS = {kind.value: kind for kind in UsageLimitKind}
_WRITE_OUT = "\\nautobuild-meter-status:%{http_code} retry-after:%header{retry-after}"
# A curl older than 7.84 prints the ``%header`` form as it stands, which reads as
# no retry time.
_TRAILER = re.compile(r"\nautobuild-meter-status:(\d{3}) retry-after:([^\n]*)\s*\Z")
_RATE_LIMIT_ERROR = "rate_limit_error"
SIGN_IN_EXPIRED = "sign-in expired: run Claude once in this venue to refresh it"


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


def _split_response(stdout: str) -> tuple[str, int | None, str]:
    """The body, the response status and the raw ``retry-after`` value."""

    match = _TRAILER.search(stdout)
    if match is None:
        return stdout, None, ""
    return stdout[: match.start()], int(match[1]), match[2].strip()


def _retry_seconds(value: str, fetched: datetime) -> float | None:
    """The wait a ``retry-after`` value asks for, in either of its two forms, or
    None when it gives none that can be read."""

    try:
        if value.isascii() and value.isdigit():
            return float(int(value))
        wait = (parsedate_to_datetime(value) - fetched).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0.0, wait)


def rate_limited_reading(meter_id: str, retry_after: str, fetched_at: str) -> UsageReading:
    """A rate-limited reading whose retry time is the meter's own, or the fixed
    hold's when it gave none, and never inside the sharing window."""

    fetched = datetime.fromisoformat(fetched_at)
    wait = _retry_seconds(retry_after, fetched)
    for seconds in (RATE_LIMIT_HOLD_SECONDS if wait is None else wait, RATE_LIMIT_HOLD_SECONDS):
        try:
            due = fetched + timedelta(seconds=max(SHARED_READING_SECONDS, seconds))
            if due.microsecond:
                due = due.replace(microsecond=0) + timedelta(seconds=1)
            break
        except OverflowError:
            continue
    return UsageReading.rate_limited(meter_id, due.isoformat(), fetched_at)


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
            "-w",
            _WRITE_OUT,
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
        token, expires_at = self._sign_in()
        expired = self._expired(meter_id, expires_at, fetched_at)
        if expired is not None:
            return expired
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
                meter_id, f"meter command failed: {type(exc).__name__}", fetched_at, UsageFailure.TRANSPORT
            )
        if result.timed_out or result.cancelled:
            return UsageReading.failed(
                meter_id, "meter command did not finish", fetched_at, UsageFailure.TRANSPORT
            )
        stdout, status, retry_after = _split_response(self._text(result.stdout_ref))
        if result.exit_code == 0 and status == 429:
            return rate_limited_reading(meter_id, retry_after, fetched_at)
        if result.exit_code == 0 and status is not None and status >= 500:
            return UsageReading.failed(
                meter_id, f"meter answered HTTP {status}", fetched_at, UsageFailure.TRANSPORT
            )
        if result.exit_code != 0 or not stdout.strip():
            detail = _redact(self._text(result.stderr_ref), token).strip()
            detail = detail[:_ERROR_DETAIL_CHARACTERS]
            message = f"meter transport failed (exit {result.exit_code})"
            return UsageReading.failed(
                meter_id,
                f"{message}: {detail}" if detail else message,
                fetched_at,
                UsageFailure.TRANSPORT if result.exit_code != 0 or status is None else None,
            )
        try:
            raw = json.loads(stdout)
        except ValueError:
            return UsageReading.failed(meter_id, "meter response is not JSON", fetched_at)
        error = raw.get("error") if isinstance(raw, dict) else None
        if isinstance(error, dict) and error.get("type") == _RATE_LIMIT_ERROR:
            return rate_limited_reading(meter_id, retry_after, fetched_at)
        reading = normalise_usage(meter_id, raw, fetched_at)
        if reading.ok and status is not None and not 200 <= status < 300:
            return UsageReading.failed(meter_id, f"meter answered HTTP {status}", fetched_at)
        return reading

    def sign_in_failure(self, meter_id: str) -> UsageReading | None:
        """The failed reading of an expired sign-in, from the stored expiry alone."""

        if meter_id not in self.meter_ids:
            return None
        try:
            return self._expired(meter_id, self._sign_in()[1], self._now().isoformat())
        except Exception:
            return None

    def _expired(
        self, meter_id: str, expires_at: datetime | None, fetched_at: str
    ) -> UsageReading | None:
        if expires_at is None or expires_at > self._now():
            return None
        return UsageReading.failed(
            meter_id,
            f"{SIGN_IN_EXPIRED} (expired at {expires_at.isoformat()})",
            fetched_at,
            UsageFailure.SIGN_IN_EXPIRED,
        )

    def _sign_in(self) -> tuple[str | None, datetime | None]:
        """The access token and the instant it expires, each None when the
        credentials file does not give it."""

        try:
            data = json.loads(self._credentials.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, None
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        if not isinstance(oauth, dict):
            return None, None
        token = oauth.get("accessToken")
        try:
            milliseconds = _number(oauth.get("expiresAt"))
            expires_at = (
                None if milliseconds is None else datetime.fromtimestamp(milliseconds / 1000, UTC)
            )
        except (OverflowError, OSError, ValueError):
            expires_at = None
        return (token if isinstance(token, str) and token.strip() else None), expires_at

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
