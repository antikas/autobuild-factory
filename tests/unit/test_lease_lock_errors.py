from __future__ import annotations

import errno
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from autobuild.adapters import FilesystemLeaseAdapter, lease
from autobuild.domain import AdapterError, LeaseRequest, LeaseSurface, SurfaceKind


@pytest.mark.parametrize(
    ("platform", "error_number", "contended"),
    [
        ("nt", errno.EACCES, True),
        ("nt", errno.EBADF, False),
        ("nt", errno.EINVAL, False),
        ("nt", errno.EAGAIN, False),
        ("nt", errno.EWOULDBLOCK, False),
        ("posix", errno.EAGAIN, True),
        ("posix", errno.EWOULDBLOCK, True),
        ("posix", errno.EACCES, True),
        ("posix", errno.ENOLCK, False),
    ],
)
def test_only_a_locked_region_counts_as_contention(
    platform: str, error_number: int, contended: bool
) -> None:
    assert lease._lock_contended(platform, error_number) is contended


def test_an_unknown_error_number_is_not_contention() -> None:
    assert lease._lock_contended("nt", None) is False
    assert lease._lock_contended("posix", None) is False


def _fake_msvcrt(error_number: int) -> types.ModuleType:
    module = types.ModuleType("msvcrt")

    def locking(descriptor: int, mode: int, size: int) -> None:
        raise OSError(error_number, os.strerror(error_number))

    module.locking = locking  # type: ignore[attr-defined]
    module.LK_NBLCK = 2  # type: ignore[attr-defined]
    module.LK_UNLCK = 0  # type: ignore[attr-defined]
    return module


def test_windows_lock_attempt_retries_only_on_a_locked_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = os.open(tmp_path / "surface.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        monkeypatch.setitem(sys.modules, "msvcrt", _fake_msvcrt(errno.EACCES))
        monkeypatch.setattr(lease.os, "name", "nt")
        assert lease._try_lock(descriptor) is False

        monkeypatch.setitem(sys.modules, "msvcrt", _fake_msvcrt(errno.EBADF))
        with pytest.raises(OSError) as excinfo:
            lease._try_lock(descriptor)
        assert excinfo.value.errno == errno.EBADF

        monkeypatch.setitem(sys.modules, "msvcrt", _fake_msvcrt(errno.EAGAIN))
        with pytest.raises(OSError) as excinfo:
            lease._try_lock(descriptor)
        assert excinfo.value.errno == errno.EAGAIN
    finally:
        monkeypatch.undo()
        os.close(descriptor)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _SlowLock:
    def __init__(self, clock: _FakeClock) -> None:
        self._clock = clock
        self.timeouts: list[float] = []

    def acquire(self, timeout: float = -1) -> bool:
        self.timeouts.append(timeout)
        self._clock.now += 0.4
        return True

    def release(self) -> None:
        pass


def test_the_in_process_wait_and_the_lock_poll_share_one_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(
        lease, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    )
    adapter = FilesystemLeaseAdapter(
        tmp_path / "scratch", lock_wait_seconds=0.5, lock_poll_seconds=0.05
    )
    thread_lock = _SlowLock(clock)
    monkeypatch.setattr(adapter, "_surface_lock", lambda digest: thread_lock)
    calls: list[int] = []

    def never_free(descriptor: Any) -> bool:
        calls.append(descriptor)
        return False

    monkeypatch.setattr(lease, "_try_lock", never_free)
    surface = LeaseSurface(tmp_path / "repo" / ".ergon", SurfaceKind.TRACKER)
    digest = adapter._lock_path(surface).stem

    with pytest.raises(AdapterError) as excinfo:
        adapter.acquire(LeaseRequest(surface, "campaign-a"))

    message = str(excinfo.value)
    assert "was not free within" in message
    assert f"{digest}.lock" in message
    assert thread_lock.timeouts == [0.5]
    assert len(calls) <= 4
    assert clock.now < 0.6
    assert not (tmp_path / "scratch" / "leases" / f"{digest}.json").exists()
