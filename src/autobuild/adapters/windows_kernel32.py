"""Declared kernel32 signatures for the Windows adapters.

Without declared types ctypes returns a ``HANDLE`` as a C ``int`` and passes a
Python int as one, so on 64-bit Windows a handle above 32 bits is cut. This
module imports on any host; only ``load_kernel32`` needs Windows."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any, TypeVar

Library = TypeVar("Library")


def bind_kernel32(library: Library) -> Library:
    """Set ``argtypes`` and ``restype`` on ``library``'s functions and return it."""

    filetime = ctypes.POINTER(wintypes.FILETIME)
    kernel32: Any = library
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        filetime,
        filetime,
        filetime,
        filetime,
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    return library


def load_kernel32() -> Any:
    return bind_kernel32(ctypes.WinDLL("kernel32", use_last_error=True))  # type: ignore[attr-defined]
