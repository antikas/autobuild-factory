from __future__ import annotations

import ctypes
import subprocess
import types
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

import pytest

from autobuild.adapters import WindowsCommandAdapter
from autobuild.adapters import lease
from autobuild.adapters.windows_kernel32 import bind_kernel32

WIDE_HANDLE = 2**40 + 5
STILL_ACTIVE = 259
WAIT_TIMEOUT = 0x102

FILETIME_POINTER = ctypes.POINTER(wintypes.FILETIME)
EXPECTED_SIGNATURES = {
    "OpenProcess": ((wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE),
    "CloseHandle": ((wintypes.HANDLE,), wintypes.BOOL),
    "WaitForSingleObject": ((wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD),
    "GetExitCodeProcess": (
        (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)),
        wintypes.BOOL,
    ),
    "GetProcessTimes": (
        (
            wintypes.HANDLE,
            FILETIME_POINTER,
            FILETIME_POINTER,
            FILETIME_POINTER,
            FILETIME_POINTER,
        ),
        wintypes.BOOL,
    ),
}


class _Plain:
    pass


def test_bind_kernel32_declares_every_signature_exactly() -> None:
    fake = types.SimpleNamespace(**{name: _Plain() for name in EXPECTED_SIGNATURES})

    bound = bind_kernel32(fake)

    assert bound is fake
    for name, (argtypes, restype) in EXPECTED_SIGNATURES.items():
        function = getattr(fake, name)
        assert tuple(function.argtypes) == argtypes, name
        assert function.restype is restype, name


class FakeKernel32:
    """kernel32 stand-in whose functions are real ctypes foreign functions.

    Each callback uses a 64-bit handle and is reached through a generic
    ``CFUNCTYPE(c_int)`` pointer, so only the signatures set by
    ``bind_kernel32`` decide how a handle is marshalled."""

    def __init__(
        self,
        *,
        open_result: int = WIDE_HANDLE,
        wait_result: int = WAIT_TIMEOUT,
        exit_code: int = STILL_ACTIVE,
        exit_ok: bool = True,
        times_ok: bool = True,
        kernel_units: int = 0,
        user_units: int = 0,
    ) -> None:
        self.seen: dict[str, list[int]] = {}
        self._keep: list[Any] = []

        def record(name: str, handle: int) -> None:
            self.seen.setdefault(name, []).append(handle)

        def open_process(access: int, inherit: int, pid: int) -> int:
            return open_result

        def wait(handle: int, milliseconds: int) -> int:
            record("WaitForSingleObject", handle)
            return wait_result

        def exit_code_of(handle: int, out: Any) -> int:
            record("GetExitCodeProcess", handle)
            out[0] = exit_code
            return 1 if exit_ok else 0

        def process_times(handle: int, creation: Any, exited: Any, kernel: Any, user: Any) -> int:
            record("GetProcessTimes", handle)
            if not times_ok:
                return 0
            for pointer, units in ((kernel, kernel_units), (user, user_units)):
                pointer[0].dwLowDateTime = units & 0xFFFFFFFF
                pointer[0].dwHighDateTime = units >> 32
            return 1

        def close(handle: int) -> int:
            record("CloseHandle", handle)
            return 1

        u64, u32 = ctypes.c_uint64, ctypes.c_uint32
        filetime = ctypes.POINTER(wintypes.FILETIME)
        self.OpenProcess = self._generic(ctypes.CFUNCTYPE(u64, u32, ctypes.c_int, u32), open_process)
        self.WaitForSingleObject = self._generic(ctypes.CFUNCTYPE(u32, u64, u32), wait)
        self.GetExitCodeProcess = self._generic(
            ctypes.CFUNCTYPE(ctypes.c_int, u64, ctypes.POINTER(u32)), exit_code_of
        )
        self.GetProcessTimes = self._generic(
            ctypes.CFUNCTYPE(ctypes.c_int, u64, filetime, filetime, filetime, filetime),
            process_times,
        )
        self.CloseHandle = self._generic(ctypes.CFUNCTYPE(ctypes.c_int, u64), close)

    def _generic(self, prototype: Any, function: Callable[..., int]) -> Any:
        callback = prototype(function)
        self._keep.append(callback)
        address = ctypes.cast(callback, ctypes.c_void_p).value
        return ctypes.CFUNCTYPE(ctypes.c_int)(address)


def _never_called() -> int:
    raise AssertionError("last error read after a successful open")


def test_live_holder_keeps_its_wide_handle_when_the_wait_times_out() -> None:
    fake = FakeKernel32(wait_result=WAIT_TIMEOUT)

    alive = lease._windows_process_alive(
        4242, kernel32=bind_kernel32(fake), get_last_error=_never_called
    )

    assert alive is True
    assert fake.seen["WaitForSingleObject"] == [WIDE_HANDLE]
    assert fake.seen["CloseHandle"] == [WIDE_HANDLE]


def test_still_active_exit_code_is_alive_through_a_wide_handle() -> None:
    fake = FakeKernel32(wait_result=0, exit_code=STILL_ACTIVE)

    alive = lease._windows_process_alive(
        4242, kernel32=bind_kernel32(fake), get_last_error=_never_called
    )

    assert alive is True
    assert fake.seen["GetExitCodeProcess"] == [WIDE_HANDLE]
    assert fake.seen["CloseHandle"] == [WIDE_HANDLE]


def test_exited_process_is_dead() -> None:
    fake = FakeKernel32(wait_result=0, exit_code=0)

    alive = lease._windows_process_alive(
        4242, kernel32=bind_kernel32(fake), get_last_error=_never_called
    )

    assert alive is False
    assert fake.seen["CloseHandle"] == [WIDE_HANDLE]


@pytest.mark.parametrize(("last_error", "alive"), [(87, False), (5, True)])
def test_failed_open_decides_by_last_error(last_error: int, alive: bool) -> None:
    fake = FakeKernel32(open_result=0)

    result = lease._windows_process_alive(
        4242, kernel32=bind_kernel32(fake), get_last_error=lambda: last_error
    )

    assert result is alive
    assert "CloseHandle" not in fake.seen


def _process(pid: int = 4242) -> subprocess.Popen[bytes]:
    return types.SimpleNamespace(pid=pid)  # type: ignore[return-value]


def test_cpu_seconds_read_through_a_wide_handle(tmp_path: Path) -> None:
    fake = FakeKernel32(kernel_units=20_000_000, user_units=30_000_000)
    adapter = WindowsCommandAdapter(tmp_path / "commands")

    seconds = adapter._child_cpu_seconds(_process(), kernel32=bind_kernel32(fake))

    assert seconds == 5.0
    assert fake.seen["GetProcessTimes"] == [WIDE_HANDLE]
    assert fake.seen["CloseHandle"] == [WIDE_HANDLE]


def test_failed_process_times_is_unknown_and_still_closes(tmp_path: Path) -> None:
    fake = FakeKernel32(times_ok=False)
    adapter = WindowsCommandAdapter(tmp_path / "commands")

    seconds = adapter._child_cpu_seconds(_process(), kernel32=bind_kernel32(fake))

    assert seconds is None
    assert fake.seen["CloseHandle"] == [WIDE_HANDLE]


def test_failed_open_gives_unknown_cpu(tmp_path: Path) -> None:
    fake = FakeKernel32(open_result=0)
    adapter = WindowsCommandAdapter(tmp_path / "commands")

    assert adapter._child_cpu_seconds(_process(), kernel32=bind_kernel32(fake)) is None
    assert "CloseHandle" not in fake.seen


class _WinDLLFactory:
    def __init__(self, **fake_options: Any) -> None:
        self._options = fake_options
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.made: list[FakeKernel32] = []

    def __call__(self, *args: Any, **kwargs: Any) -> FakeKernel32:
        self.calls.append((args, kwargs))
        fake = FakeKernel32(**self._options)
        self.made.append(fake)
        return fake


def test_default_loader_binds_kernel32_for_lease_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _WinDLLFactory(wait_result=WAIT_TIMEOUT)
    monkeypatch.setattr(ctypes, "WinDLL", factory, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", _never_called, raising=False)

    assert lease._windows_process_alive(4242) is True

    assert factory.calls == [(("kernel32",), {"use_last_error": True})]
    (fake,) = factory.made
    assert fake.seen["WaitForSingleObject"] == [WIDE_HANDLE]
    assert fake.seen["CloseHandle"] == [WIDE_HANDLE]


@pytest.mark.parametrize(("last_error", "alive"), [(87, False), (5, True)])
def test_default_last_error_reader_decides_a_failed_open(
    monkeypatch: pytest.MonkeyPatch, last_error: int, alive: bool
) -> None:
    factory = _WinDLLFactory(open_result=0)
    monkeypatch.setattr(ctypes, "WinDLL", factory, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)

    assert lease._windows_process_alive(4242) is alive
    assert "CloseHandle" not in factory.made[0].seen


def test_default_loader_binds_kernel32_for_cpu_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = _WinDLLFactory(kernel_units=20_000_000, user_units=30_000_000)
    monkeypatch.setattr(ctypes, "WinDLL", factory, raising=False)
    adapter = WindowsCommandAdapter(tmp_path / "commands")

    assert adapter._child_cpu_seconds(_process()) == 5.0

    assert factory.calls == [(("kernel32",), {"use_last_error": True})]
    assert factory.made[0].seen["GetProcessTimes"] == [WIDE_HANDLE]
