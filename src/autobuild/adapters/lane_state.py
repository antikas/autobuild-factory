"""Machine-local, cross-campaign lane cooling under a single locked file.

One ``lanes.json`` below the lane state root holds a record per lane: the UTC
time the lane cools until, the structural signature that cooled it, when it last
failed and which campaign cooled it. Every read reflects the file on disk and
every write happens under an exclusive lock file, so concurrent campaigns on one
machine see each other's cooling.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable
from uuid import uuid4

from autobuild.domain import (
    AdapterError,
    AdapterIdentity,
    LaneCooling,
    LaneSignal,
    ProbeResult,
)

# The default cooling window applied when a vendor supplies no reset time.
DEFAULT_COOL_SECONDS = 3600.0
# How long a write waits for the lock before it treats the lock file as stale.
_LOCK_TIMEOUT_SECONDS = 5.0


def _now() -> datetime:
    return datetime.now(UTC)


def _parse(moment: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(moment)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class FilesystemLaneStateAdapter:
    """Persist and read lane cooling as one JSON file under a lock."""

    def __init__(
        self,
        lane_state_root: Path,
        *,
        cool_seconds: float = DEFAULT_COOL_SECONDS,
        now: Callable[[], datetime] = _now,
    ) -> None:
        self._root = lane_state_root.resolve(strict=False)
        self._path = self._root / "lanes.json"
        self._lock_path = self._root / "lanes.json.lock"
        self._cool_seconds = float(cool_seconds)
        self._now = now

    def probe(self) -> ProbeResult:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ProbeResult.unavailable(f"lane state root is unavailable: {exc}")
        return ProbeResult.ready(
            AdapterIdentity("filesystem-lane-state", "1", frozenset({"shared", "file-locked"})),
            str(self._path),
        )

    def active(self, lane: str) -> LaneCooling | None:
        record = self._read().get(lane)
        if record is None:
            return None
        cooled_until = _parse(record.cooled_until)
        if cooled_until is None or self._now() >= cooled_until:
            return None
        return record

    def cool(self, lane: str, signal: LaneSignal, campaign_id: str) -> LaneCooling:
        moment = self._now()
        cooled_until = self._cooled_until(signal, moment)
        record = LaneCooling(
            lane=lane,
            cooled_until=cooled_until.isoformat(),
            signature=signal.signature,
            last_failure_at=moment.isoformat(),
            campaign_id=campaign_id,
        )
        with self._locked():
            records = self._read()
            records[lane] = record
            self._write(records)
        return record

    def snapshot(self) -> tuple[LaneCooling, ...]:
        return tuple(self._read().values())

    # -- internals -------------------------------------------------------------

    def _cooled_until(self, signal: LaneSignal, moment: datetime) -> datetime:
        if signal.reset_at:
            reset = _parse(signal.reset_at)
            if reset is not None and reset > moment:
                return reset
        return moment + timedelta(seconds=self._cool_seconds)

    def _read(self) -> dict[str, LaneCooling]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise AdapterError(f"lane state is unreadable: {self._path}: {exc}") from exc
        try:
            payload = json.loads(raw)
            lanes = payload["lanes"]
            records: dict[str, LaneCooling] = {}
            for lane, entry in lanes.items():
                records[str(lane)] = LaneCooling(
                    lane=str(lane),
                    cooled_until=str(entry["cooled_until"]),
                    signature=str(entry["signature"]),
                    last_failure_at=str(entry["last_failure_at"]),
                    campaign_id=str(entry["campaign_id"]),
                )
            return records
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise AdapterError(f"lane state is malformed: {self._path}: {exc}") from exc

    def _write(self, records: dict[str, LaneCooling]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "autobuild.lanes.v1",
            "lanes": {
                lane: {
                    "cooled_until": record.cooled_until,
                    "signature": record.signature,
                    "last_failure_at": record.last_failure_at,
                    "campaign_id": record.campaign_id,
                }
                for lane, record in sorted(records.items())
            },
        }
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self._path)

    def _locked(self):
        return _FileLock(self._lock_path, self._now)


class _FileLock:
    """A cross-process exclusive lock held through an O_EXCL lock file.

    The lock file carries the acquiring time so a crashed writer's file is broken
    once it is older than the wait timeout, rather than deadlocking a later run.
    """

    def __init__(self, path: Path, now: Callable[[], datetime]) -> None:
        self._path = path
        self._now = now

    def __enter__(self) -> "_FileLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if time.monotonic() >= deadline and self._is_stale():
                    self._path.unlink(missing_ok=True)
                    continue
                time.sleep(0.01)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self._now().isoformat())
            return self

    def __exit__(self, *_exc: object) -> None:
        self._path.unlink(missing_ok=True)

    def _is_stale(self) -> bool:
        try:
            stamped = _parse(self._path.read_text(encoding="utf-8").strip())
        except OSError:
            return True
        if stamped is None:
            return True
        return (self._now() - stamped).total_seconds() > _LOCK_TIMEOUT_SECONDS
