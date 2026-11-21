from __future__ import annotations

import errno
import hashlib
import os
import threading
import time
from pathlib import Path

import pytest

from autobuild.adapters import FilesystemLeaseAdapter
from autobuild.domain import AdapterError, LeaseRequest, LeaseSurface, SurfaceKind


def _digest(surface: Path) -> str:
    return hashlib.sha256(
        os.path.normcase(str(surface.resolve(strict=False))).encode("utf-8")
    ).hexdigest()


@pytest.mark.skipif(os.name == "nt", reason="flock is the POSIX lock call")
def test_a_lock_error_other_than_contention_fails_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    calls: list[int] = []

    def broken(descriptor: int, operation: int) -> None:
        calls.append(operation)
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    monkeypatch.setattr(fcntl, "flock", broken)
    surface = tmp_path / "repo" / ".ergon"
    adapter = FilesystemLeaseAdapter(
        tmp_path / "scratch", lock_wait_seconds=5.0, lock_poll_seconds=0.05
    )
    started = time.monotonic()
    with pytest.raises(AdapterError) as excinfo:
        adapter.acquire(LeaseRequest(LeaseSurface(surface, SurfaceKind.TRACKER), "campaign-a"))
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert len(calls) == 1
    message = str(excinfo.value)
    assert f"{_digest(surface)}.lock" in message
    assert "was not free within" not in message
    assert isinstance(excinfo.value.__cause__, OSError)
    assert excinfo.value.__cause__.errno == errno.ENOLCK
    assert not (tmp_path / "scratch" / "leases" / f"{_digest(surface)}.json").exists()


def test_a_thread_waiting_on_a_surface_step_in_this_process_is_bounded(
    tmp_path: Path,
) -> None:
    surface = LeaseSurface(tmp_path / "repo" / ".ergon", SurfaceKind.TRACKER)
    adapter = FilesystemLeaseAdapter(
        tmp_path / "scratch", lock_wait_seconds=0.5, lock_poll_seconds=0.05
    )
    inside = threading.Event()
    leave = threading.Event()
    errors: list[BaseException] = []

    def hold() -> None:
        with adapter._exclusive(surface):
            inside.set()
            leave.wait(timeout=30)

    def attempt() -> None:
        try:
            adapter.acquire(LeaseRequest(surface, "campaign-b"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    holder = threading.Thread(target=hold, daemon=True)
    contender = threading.Thread(target=attempt, daemon=True)
    try:
        holder.start()
        assert inside.wait(timeout=10)
        contender.start()
        contender.join(timeout=5)
        assert not contender.is_alive(), "the in-process wait was not bounded"
        assert len(errors) == 1 and isinstance(errors[0], AdapterError)
        assert f"{_digest(surface.path)}.lock" in str(errors[0])
    finally:
        leave.set()
        holder.join(timeout=10)
        contender.join(timeout=10)


class _AnnouncingLock:
    def __init__(self, inner: threading.Lock, waiting: threading.Event) -> None:
        self._inner = inner
        self._waiting = waiting

    def acquire(self, *args: object, **kwargs: object) -> bool:
        self._waiting.set()
        return self._inner.acquire(*args, **kwargs)  # type: ignore[arg-type]

    def release(self) -> None:
        self._inner.release()


def test_a_thread_waiting_on_one_surface_never_blocks_a_step_on_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    surface_a = LeaseSurface(tmp_path / "repo-a" / ".ergon", SurfaceKind.TRACKER)
    surface_b = LeaseSurface(tmp_path / "repo-b" / ".ergon", SurfaceKind.WORKTREE)
    adapter = FilesystemLeaseAdapter(
        tmp_path / "scratch", lock_wait_seconds=60, lock_poll_seconds=0.05
    )
    inside = threading.Event()
    leave = threading.Event()
    waiting = threading.Event()
    released: list[bool] = []

    def hold() -> None:
        with adapter._exclusive(surface_a):
            inside.set()
            leave.wait(timeout=120)

    holder = threading.Thread(target=hold, daemon=True)
    waiter = threading.Thread(
        target=lambda: adapter.acquire(LeaseRequest(surface_a, "campaign-a")),
        daemon=True,
    )

    def steps_on_b() -> None:
        grant = adapter.acquire(LeaseRequest(surface_b, "campaign-b"))
        grant = adapter.renew(grant)
        released.append(adapter.release(grant).released)

    other = threading.Thread(target=steps_on_b, daemon=True)
    try:
        holder.start()
        assert inside.wait(timeout=10)
        digest_a = _digest(surface_a.path)
        original = adapter._surface_lock

        def announcing(digest: str) -> object:
            lock = original(digest)
            if digest == digest_a:
                return _AnnouncingLock(lock, waiting)
            return lock

        monkeypatch.setattr(adapter, "_surface_lock", announcing)
        waiter.start()
        assert waiting.wait(timeout=10), "the waiter never reached surface A's lock"
        other.start()
        other.join(timeout=10)
        assert not other.is_alive(), "a wait on surface A blocked a step on surface B"
        assert released == [True]
        assert waiter.is_alive()
        assert not leave.is_set()
    finally:
        leave.set()
        holder.join(timeout=10)
        waiter.join(timeout=10)
        other.join(timeout=70)
