"""Filesystem single-writer lease under the scratch root.

One lease file guards one surface. The file name is the SHA-256 of the surface
path, so a tracker root and a worktree each map to a stable, collision-free
record regardless of how deep the path is. Liveness is a process-id check on the
holder's host plus a heartbeat age; there is no automatic take-over, so a live
foreign lease raises rather than being seized.

Each read-decide-write step on a surface runs under an operating-system
advisory lock on a sidecar ``<digest>.lock`` file, so two processes on one host
cannot both see a surface as free. The kernel drops that lock when its holder
exits, so a crash inside the step never leaves the surface locked.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterator
from uuid import uuid4

from autobuild.domain import (
    AdapterError,
    AdapterIdentity,
    LeaseGrant,
    LeaseHeld,
    LeaseRecord,
    LeaseRelease,
    LeaseRequest,
    LeaseSurface,
    ProbeResult,
    SurfaceKind,
)

# The default heartbeat age past which a lease is stale even when its process
# cannot be checked (for example a holder on another host).
DEFAULT_STALE_SECONDS = 1800.0
DEFAULT_LOCK_WAIT_SECONDS = 30.0
DEFAULT_LOCK_POLL_SECONDS = 0.05


def _now() -> datetime:
    return datetime.now(UTC)


def _lock_contended(platform: str, error_number: int | None) -> bool:
    """Whether a failed non-blocking lock attempt means another holder has the
    lock, as opposed to a real error that waiting cannot cure.

    ``_locking`` with ``LK_NBLCK`` reports a locked region as ``EACCES`` only;
    ``EDEADLOCK`` belongs to its blocking modes."""

    if error_number is None:
        return False
    if platform == "nt":
        return error_number == errno.EACCES
    return error_number in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES)


def _try_lock(descriptor: int) -> bool:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if _lock_contended("nt", exc.errno):
                return False
            raise
        return True
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if _lock_contended("posix", exc.errno):
            return False
        raise
    return True


def _unlock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def process_alive(process_id: int) -> bool:
    """Best-effort liveness of a process id on this host.

    A missing process is dead; a process this account cannot query is treated as
    alive so a lease is never reclaimed on a permission error alone."""

    if process_id <= 0:
        return False
    if os.name == "nt":
        return _windows_process_alive(process_id)
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _windows_process_alive(
    process_id: int,
    *,
    kernel32: Any = None,
    get_last_error: Callable[[], int] | None = None,
) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        from autobuild.adapters.windows_kernel32 import load_kernel32
    except Exception:
        return True
    synchronize = 0x00100000
    process_query_limited_information = 0x1000
    if kernel32 is None:
        kernel32 = load_kernel32()
    if get_last_error is None:
        get_last_error = ctypes.get_last_error
    handle = kernel32.OpenProcess(
        synchronize | process_query_limited_information, False, process_id
    )
    if not handle:
        # A missing process fails to open with ERROR_INVALID_PARAMETER (87).
        return get_last_error() not in (87,)
    try:
        wait_timeout = 0x00000102
        code = kernel32.WaitForSingleObject(handle, 0)
        if code == wait_timeout:
            return True
        exit_code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            still_active = 259
            return exit_code.value == still_active
        return False
    finally:
        kernel32.CloseHandle(handle)


class FilesystemLeaseAdapter:
    """Persist and check leases as JSON files below ``<scratch_root>/leases``."""

    def __init__(
        self,
        scratch_root: Path,
        *,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
        now: Callable[[], datetime] = _now,
        host: str | None = None,
        process_id: int | None = None,
        is_alive: Callable[[int], bool] = process_alive,
        lock_wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
        lock_poll_seconds: float = DEFAULT_LOCK_POLL_SECONDS,
    ) -> None:
        self._root = scratch_root.resolve(strict=False) / "leases"
        self._stale_seconds = float(stale_seconds)
        self._now = now
        self._host = host if host is not None else socket.gethostname()
        self._process_id = process_id if process_id is not None else os.getpid()
        self._is_alive = is_alive
        self._lock_wait_seconds = float(lock_wait_seconds)
        self._lock_poll_seconds = float(lock_poll_seconds)
        self._lock = Lock()
        self._surface_locks: dict[str, Lock] = {}

    def probe(self) -> ProbeResult:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ProbeResult.unavailable(f"lease root is unavailable: {exc}")
        return ProbeResult.ready(
            AdapterIdentity(
                "filesystem-lease",
                "1",
                frozenset({"single-writer", "stale-reclaim", "heartbeat"}),
            ),
            str(self._root),
        )

    def acquire(self, request: LeaseRequest) -> LeaseGrant:
        surface = request.surface
        with self._exclusive(surface):
            existing = self._read(surface)
            if existing is not None and self._is_live(existing):
                if not self._is_self(existing):
                    raise LeaseHeld(existing)
                reclaimed = None
            else:
                reclaimed = existing
            record = self._stamp(request.campaign_id, surface)
            self._write(surface, record)
            return LeaseGrant(surface, record, reclaimed)

    def renew(self, grant: LeaseGrant) -> LeaseGrant:
        surface = grant.surface
        with self._exclusive(surface):
            existing = self._read(surface)
            if (
                existing is not None
                and self._is_live(existing)
                and not self._is_self(existing)
            ):
                raise LeaseHeld(existing)
            started = grant.record.started_at
            record = self._stamp(grant.record.campaign_id, surface, started_at=started)
            self._write(surface, record)
            return LeaseGrant(surface, record, None)

    def release(self, grant: LeaseGrant) -> LeaseRelease:
        surface = grant.surface
        with self._exclusive(surface):
            existing = self._read(surface)
            if existing is None:
                return LeaseRelease(surface, False, ("no lease file was present",))
            if not self._is_self(existing):
                return LeaseRelease(
                    surface,
                    False,
                    (
                        "lease is held by another writer, not this process: "
                        f"campaign {existing.campaign_id} process {existing.process_id} "
                        f"on {existing.host}",
                    ),
                )
            self._path(surface).unlink(missing_ok=True)
            return LeaseRelease(surface, True, ())

    def live_holder(self, surface: LeaseSurface) -> LeaseRecord | None:
        with self._exclusive(surface):
            existing = self._read(surface)
        if existing is not None and self._is_live(existing):
            return existing
        return None

    # -- internals -------------------------------------------------------------

    def _digest(self, surface: LeaseSurface) -> str:
        return hashlib.sha256(
            os.path.normcase(str(surface.path.resolve(strict=False))).encode("utf-8")
        ).hexdigest()

    def _path(self, surface: LeaseSurface) -> Path:
        return self._root / f"{self._digest(surface)}.json"

    def _lock_path(self, surface: LeaseSurface) -> Path:
        return self._root / f"{self._digest(surface)}.lock"

    def _surface_lock(self, digest: str) -> Lock:
        with self._lock:
            lock = self._surface_locks.get(digest)
            if lock is None:
                lock = self._surface_locks[digest] = Lock()
            return lock

    def _not_free(self, path: Path) -> AdapterError:
        return AdapterError(
            f"lease lock file {path} was not free within "
            f"{self._lock_wait_seconds:g} seconds"
        )

    @contextmanager
    def _exclusive(self, surface: LeaseSurface) -> Iterator[None]:
        """Hold the surface's in-process lock and its cross-process lock for
        one read-decide-write step, never for the life of the lease.

        Both waits share one bound on the monotonic clock, never the injected
        ``now``, so a frozen or fake clock cannot make the wait unbounded."""

        deadline = time.monotonic() + self._lock_wait_seconds
        path = self._lock_path(surface)
        thread_lock = self._surface_lock(path.stem)
        if not thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise self._not_free(path)
        try:
            try:
                self._root.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            except OSError as exc:
                raise AdapterError(f"lease lock file cannot be opened: {path}: {exc}") from exc
            try:
                while True:
                    try:
                        taken = _try_lock(descriptor)
                    except OSError as exc:
                        raise AdapterError(
                            f"lease lock file {path} cannot be locked: {exc}"
                        ) from exc
                    if taken:
                        break
                    if time.monotonic() >= deadline:
                        raise self._not_free(path)
                    time.sleep(self._lock_poll_seconds)
                try:
                    yield
                finally:
                    _unlock(descriptor)
            finally:
                os.close(descriptor)
        finally:
            thread_lock.release()

    def _stamp(
        self, campaign_id: str, surface: LeaseSurface, *, started_at: str | None = None
    ) -> LeaseRecord:
        moment = self._now().isoformat()
        return LeaseRecord(
            campaign_id=campaign_id,
            process_id=self._process_id,
            host=self._host,
            started_at=started_at or moment,
            heartbeat_at=moment,
            surface_path=surface.path,
            surface_kind=surface.kind,
        )

    def _is_self(self, record: LeaseRecord) -> bool:
        return record.process_id == self._process_id and record.host == self._host

    def _is_live(self, record: LeaseRecord) -> bool:
        if self._heartbeat_age(record) > self._stale_seconds:
            return False
        if record.host == self._host and not self._is_alive(record.process_id):
            return False
        return True

    def _heartbeat_age(self, record: LeaseRecord) -> float:
        try:
            beat = datetime.fromisoformat(record.heartbeat_at)
        except (TypeError, ValueError):
            return float("inf")
        if beat.tzinfo is None:
            beat = beat.replace(tzinfo=UTC)
        return (self._now() - beat).total_seconds()

    def _read(self, surface: LeaseSurface) -> LeaseRecord | None:
        path = self._path(surface)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AdapterError(f"lease file is unreadable: {path}: {exc}") from exc
        try:
            payload = json.loads(raw)
            return LeaseRecord(
                campaign_id=str(payload["campaign_id"]),
                process_id=int(payload["process_id"]),
                host=str(payload["host"]),
                started_at=str(payload["started_at"]),
                heartbeat_at=str(payload["heartbeat_at"]),
                surface_path=Path(payload["surface_path"]),
                surface_kind=SurfaceKind(payload["surface_kind"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise AdapterError(f"lease file is malformed: {path}: {exc}") from exc

    def _write(self, surface: LeaseSurface, record: LeaseRecord) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(surface)
        payload = {
            "schema": "autobuild.lease.v1",
            "campaign_id": record.campaign_id,
            "process_id": record.process_id,
            "host": record.host,
            "started_at": record.started_at,
            "heartbeat_at": record.heartbeat_at,
            "surface_path": str(record.surface_path),
            "surface_kind": record.surface_kind.value,
        }
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        temporary.replace(path)
