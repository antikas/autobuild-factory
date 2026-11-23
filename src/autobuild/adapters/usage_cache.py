"""One shared usage meter reading per host, kept in a short-lived file.

Every reader on the host that wraps its meter in ``SharedUsageMeterCache`` at
the same scratch root shares one file per meter id at
``<scratch root>/usage-meter/<meter id>.json``. A reading fetched less than
``CACHE_SECONDS`` ago is served from the file; an older one is refreshed by one
reader under a cross-process lock while the others wait and then read the
refreshed file. Failed readings are shared the same way, so a rate-limited meter
is asked once per window.

The file holds only the normalised reading. A file that cannot be read, is torn,
is malformed, carries another schema or names another meter is ignored, and a
file that cannot be written never fails a reading.

A rate-limited answer is shared until the retry time it carries, and for
``RATE_LIMIT_HOLD_SECONDS`` when it carries none, so no reader on the host asks a
refusing meter again during the hold. Either lasts at least the sharing window.

Beside a rate-limited or transport failure the file keeps the last good reading.
``read`` never serves it in place of the failure; ``last_good`` hands it to a
caller that decides. Any other failure drops it. An expired sign-in is never
written, because a refreshed one must be read at once.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from autobuild.adapters.claude_usage import COMMAND_TIMEOUT_SECONDS
from autobuild.adapters.lane_state import _FileLock
from autobuild.domain import (
    ProbeResult,
    UsageFailure,
    UsageLimit,
    UsageLimitKind,
    UsageReading,
)
from autobuild.ports import UsageMeterPort
from autobuild.ports.contracts import (
    RATE_LIMIT_HOLD_SECONDS,
    SHARED_READING_SECONDS,
    SignInStatePort,
)

CACHE_SECONDS = SHARED_READING_SECONDS
_RATE_LIMIT_MARKER = "rate_limit"
CACHE_SCHEMA = "autobuild.usage-reading.v1"
CACHE_DIRECTORY = "usage-meter"
SOURCE_SHARED_FILE = "shared file"
SOURCE_METER = "meter"
_SAFE_METER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_KINDS = {kind.value: kind for kind in UsageLimitKind}


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SourcedReading:
    """A reading and where it came from: the shared file or the meter itself."""

    reading: UsageReading
    source: str
    path: Path | None = None

    @property
    def from_shared_file(self) -> bool:
        return self.source == SOURCE_SHARED_FILE


def reading_payload(reading: UsageReading) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": CACHE_SCHEMA,
        "meter_id": reading.meter_id,
        "ok": reading.ok,
        "limits": [
            {
                "kind": limit.kind.value,
                "percent": limit.percent,
                "resets_at": limit.resets_at,
                "scope": limit.scope,
            }
            for limit in reading.limits
        ],
        "error": reading.error,
        "extra_usage_enabled": reading.extra_usage_enabled,
        "fetched_at": reading.fetched_at,
    }
    if reading.failure is not None:
        payload["failure"] = reading.failure.value
    if reading.retry_at is not None:
        payload["retry_at"] = reading.retry_at
    return payload


def _instant(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _optional_text(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError("not text")


def parse_reading(raw: object, meter_id: str) -> UsageReading | None:
    """The reading a cache file holds for ``meter_id``, or None when it holds
    anything else."""

    if not isinstance(raw, dict) or raw.get("schema") != CACHE_SCHEMA:
        return None
    if raw.get("meter_id") != meter_id:
        return None
    ok, error, fetched_at = raw.get("ok"), raw.get("error"), raw.get("fetched_at")
    extra, entries = raw.get("extra_usage_enabled"), raw.get("limits")
    if not isinstance(ok, bool) or not isinstance(error, str) or not isinstance(fetched_at, str):
        return None
    if extra is not None and not isinstance(extra, bool):
        return None
    if not isinstance(entries, list):
        return None
    limits: list[UsageLimit] = []
    try:
        for entry in entries:
            kind = _KINDS[entry["kind"]]
            percent = entry["percent"]
            if (
                isinstance(percent, bool)
                or not isinstance(percent, (int, float))
                or not math.isfinite(float(percent))
                or not 0 <= float(percent) <= 100
            ):
                return None
            limits.append(
                UsageLimit(
                    kind,
                    float(percent),
                    _optional_text(entry.get("resets_at")),
                    _optional_text(entry.get("scope")),
                )
            )
        failure = UsageFailure(raw["failure"]) if raw.get("failure") is not None else None
        retry_at = _optional_text(raw.get("retry_at"))
    except (KeyError, TypeError, ValueError):
        return None
    if retry_at is not None and _instant(retry_at) is None:
        return None
    return UsageReading(meter_id, ok, tuple(limits), error, extra, fetched_at, failure, retry_at)


def share_seconds(reading: UsageReading) -> float:
    """How long a reading is served from the shared file before the meter is
    asked again: the sharing window, or the longer hold after a rate-limited
    answer, which ends at the meter's own retry time when it gave one."""

    if reading.ok:
        return CACHE_SECONDS
    if reading.failure is not UsageFailure.RATE_LIMITED and _RATE_LIMIT_MARKER not in reading.error:
        return CACHE_SECONDS
    retry, fetched = _instant(reading.retry_at), _instant(reading.fetched_at)
    if retry is None or fetched is None:
        return RATE_LIMIT_HOLD_SECONDS
    return max(CACHE_SECONDS, (retry - fetched).total_seconds())


def next_read_at(reading: UsageReading) -> str | None:
    """When the shared reading next asks the meter after a failed read, or None
    when the failure does not pass with time."""

    fetched = _instant(reading.fetched_at)
    if fetched is None or reading.failure is UsageFailure.SIGN_IN_EXPIRED:
        return None
    try:
        return reading.retry_at or (fetched + timedelta(seconds=share_seconds(reading))).isoformat()
    except OverflowError:
        return None


class SharedUsageMeterCache:
    """A usage meter port that shares one reading per meter id across processes."""

    def __init__(
        self,
        inner: UsageMeterPort,
        scratch_root: Path,
        *,
        now: Callable[[], datetime] = _now,
        lock_timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self._inner = inner
        self._root = scratch_root.resolve(strict=False) / CACHE_DIRECTORY
        self._now = now
        self._lock_timeout = float(lock_timeout_seconds)

    @property
    def meter_ids(self):
        return getattr(self._inner, "meter_ids", frozenset())

    def probe(self) -> ProbeResult:
        return self._inner.probe()

    def path(self, meter_id: str) -> Path | None:
        if not _SAFE_METER_ID.fullmatch(meter_id or ""):
            return None
        return self._root / f"{meter_id}.json"

    def read(self, meter_id: str) -> UsageReading:
        return self.read_sourced(meter_id).reading

    def read_sourced(self, meter_id: str) -> SourcedReading:
        path = self.path(meter_id)
        if path is None:
            return SourcedReading(self._inner.read(meter_id), SOURCE_METER)
        cached = self._shared(path, meter_id)
        if cached is not None:
            return cached
        lock = _FileLock(
            path.with_name(path.name + ".lock"), self._now, self._lock_timeout, give_up=True
        )
        try:
            lock.__enter__()
        except (OSError, TimeoutError):
            cached = self._shared(path, meter_id)
            if cached is not None:
                return cached
            reading = self._inner.read(meter_id)
            self._write(path, reading)
            return SourcedReading(reading, SOURCE_METER, path)
        try:
            cached = self._shared(path, meter_id)
            if cached is not None:
                return cached
            reading = self._inner.read(meter_id)
            self._write(path, reading)
            return SourcedReading(reading, SOURCE_METER, path)
        finally:
            try:
                lock.__exit__(None, None, None)
            except OSError:
                pass

    def sign_in_failure(self, meter_id: str) -> UsageReading | None:
        if isinstance(self._inner, SignInStatePort):
            return self._inner.sign_in_failure(meter_id)
        return None

    def last_good(self, meter_id: str) -> UsageReading | None:
        """The last good reading, whatever its age, unless a later failure said
        something about the account."""

        path = self.path(meter_id)
        return None if path is None else self._stored_good(path, meter_id)

    @staticmethod
    def _stored(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _stored_good(self, path: Path, meter_id: str) -> UsageReading | None:
        raw = self._stored(path)
        reading = parse_reading(raw, meter_id)
        if reading is None or reading.ok:
            return reading
        good = parse_reading(raw.get("last_good"), meter_id)
        return good if good is not None and good.ok else None

    def _shared(self, path: Path, meter_id: str) -> SourcedReading | None:
        cached = self._fresh(path, meter_id)
        if cached is None:
            return None
        if not cached.ok:
            expired = self.sign_in_failure(meter_id)
            if expired is not None:
                return SourcedReading(expired, SOURCE_METER, path)
        return SourcedReading(cached, SOURCE_SHARED_FILE, path)

    def _fresh(self, path: Path, meter_id: str) -> UsageReading | None:
        reading = parse_reading(self._stored(path), meter_id)
        if reading is None or not reading.fetched_at:
            return None
        try:
            fetched = datetime.fromisoformat(reading.fetched_at)
            if fetched.tzinfo is None:
                return None
            age = (self._now() - fetched).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
        return reading if 0 <= age < share_seconds(reading) else None

    def _write(self, path: Path, reading: UsageReading) -> None:
        if reading.failure is UsageFailure.SIGN_IN_EXPIRED:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = reading_payload(reading)
            good = self._stored_good(path, reading.meter_id) if reading.unread else None
            if good is not None:
                payload["last_good"] = reading_payload(good)
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_text(
                    json.dumps(payload, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        except Exception:
            return
