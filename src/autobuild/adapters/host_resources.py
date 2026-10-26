"""Standard-library host resource readings for Windows, Linux and other hosts."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from autobuild.domain import AdapterIdentity, HostReading, ProbeResult

_CAPABILITIES = frozenset({"host-resources"})


def _logical_cpus() -> int | None:
    count = os.cpu_count()
    return count if count and count > 0 else None


def busy_percent(
    start: tuple[int, int], end: tuple[int, int]
) -> float | None:
    """CPU busy share between two ``(idle, total)`` counter pairs, or None when
    the window recorded no time."""

    idle = end[0] - start[0]
    total = end[1] - start[1]
    if total <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (total - idle) / total))


# --- Linux -----------------------------------------------------------------


def parse_proc_stat(text: str) -> tuple[int, int]:
    """``(idle, total)`` jiffies from the aggregate ``cpu`` line of /proc/stat.

    Idle counts ``idle`` and ``iowait``. Total sums user, nice, system, idle,
    iowait, irq, softirq and steal; guest time is already inside user and nice."""

    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == "cpu":
            values = [int(value) for value in parts[1:9]]
            if len(values) < 4:
                break
            values += [0] * (8 - len(values))
            return values[3] + values[4], sum(values)
    raise ValueError("/proc/stat has no aggregate cpu line")


def parse_proc_meminfo(text: str, strict_overcommit: bool) -> dict[str, int | None]:
    """Memory and commit bytes from /proc/meminfo.

    ``MemAvailable`` is required for available memory; an older kernel without
    it leaves the field unmeasured rather than estimated. The commit limit is
    enforced only under strict overcommit (``vm.overcommit_memory = 2``), so
    commit fields are unmeasured otherwise."""

    kib: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            kib[name.strip()] = int(fields[0]) * 1024
    commit_total = kib.get("CommitLimit") if strict_overcommit else None
    committed = kib.get("Committed_AS") if strict_overcommit else None
    commit_available = (
        None
        if commit_total is None or committed is None
        else max(0, commit_total - committed)
    )
    return {
        "memory_total_bytes": kib.get("MemTotal"),
        "memory_available_bytes": kib.get("MemAvailable"),
        "commit_total_bytes": commit_total if commit_available is not None else None,
        "commit_available_bytes": commit_available,
    }


class ProcHostResourceAdapter:
    """Reads /proc/stat, /proc/meminfo and the overcommit mode."""

    def __init__(
        self,
        proc_root: Path = Path("/proc"),
        sleep: Callable[[float], None] = time.sleep,
        logical_cpus: Callable[[], int | None] = _logical_cpus,
    ) -> None:
        self._root = proc_root
        self._sleep = sleep
        self._logical_cpus = logical_cpus

    def probe(self) -> ProbeResult:
        if not (self._root / "stat").is_file() or not (self._root / "meminfo").is_file():
            return ProbeResult.unavailable(f"{self._root} does not expose stat and meminfo")
        return ProbeResult.ready(AdapterIdentity("proc-host-resources", "1", _CAPABILITIES))

    def _counters(self) -> tuple[int, int]:
        return parse_proc_stat((self._root / "stat").read_text(encoding="ascii"))

    def read(self, sample_seconds: float) -> HostReading:
        diagnostics: list[str] = []
        busy: float | None = None
        try:
            start = self._counters()
            self._sleep(sample_seconds)
            busy = busy_percent(start, self._counters())
        except (OSError, OverflowError, ValueError) as exc:
            diagnostics.append(f"cpu busy unmeasured: {type(exc).__name__}: {exc}")
        try:
            mode = (self._root / "sys" / "vm" / "overcommit_memory").read_text(
                encoding="ascii"
            )
            strict = mode.strip() == "2"
        except OSError:
            strict = False
            diagnostics.append("commit unmeasured: overcommit mode unreadable")
        memory: dict[str, int | None] = dict.fromkeys(
            (
                "memory_total_bytes",
                "memory_available_bytes",
                "commit_total_bytes",
                "commit_available_bytes",
            )
        )
        try:
            memory = parse_proc_meminfo(
                (self._root / "meminfo").read_text(encoding="ascii"), strict
            )
        except (OSError, ValueError) as exc:
            diagnostics.append(f"memory unmeasured: {type(exc).__name__}: {exc}")
        return HostReading(
            self._logical_cpus(),
            busy,
            memory["memory_total_bytes"],
            memory["memory_available_bytes"],
            memory["commit_total_bytes"],
            memory["commit_available_bytes"],
            float(sample_seconds),
            tuple(diagnostics),
        )


# --- Windows ---------------------------------------------------------------


def filetime_counters(idle: int, kernel: int, user: int) -> tuple[int, int]:
    """``(idle, total)`` from ``GetSystemTimes`` values. Kernel time already
    includes idle time, so the total is kernel plus user only."""

    return idle, kernel + user


class _Kernel32:
    """``GetSystemTimes`` and ``GlobalMemoryStatusEx`` with explicit signatures."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        self._ctypes = ctypes
        self._filetime = FILETIME
        self._memory_status = MEMORYSTATUSEX
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetSystemTimes.argtypes = [ctypes.POINTER(FILETIME)] * 3
        kernel32.GetSystemTimes.restype = wintypes.BOOL
        kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
        kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
        self._kernel32 = kernel32

    def system_times(self) -> tuple[int, int]:
        """``(idle, total)`` in 100 ns units; kernel time already includes idle."""

        ctypes = self._ctypes
        idle, kernel, user = self._filetime(), self._filetime(), self._filetime()
        if not self._kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        def ticks(value) -> int:
            return (value.high << 32) | value.low

        return filetime_counters(ticks(idle), ticks(kernel), ticks(user))

    def memory_status(self) -> tuple[int, int, int, int]:
        """Total and available physical memory, then commit limit and headroom."""

        ctypes = self._ctypes
        status = self._memory_status()
        status.dwLength = ctypes.sizeof(self._memory_status)
        if not self._kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (
            int(status.ullTotalPhys),
            int(status.ullAvailPhys),
            int(status.ullTotalPageFile),
            int(status.ullAvailPageFile),
        )


class Kernel32HostResourceAdapter:
    """Reads CPU times and memory status through kernel32."""

    def __init__(
        self,
        sleep: Callable[[float], None] = time.sleep,
        logical_cpus: Callable[[], int | None] = _logical_cpus,
        kernel32_factory: Callable[[], object] = _Kernel32,
    ) -> None:
        self._sleep = sleep
        self._logical_cpus = logical_cpus
        self._factory = kernel32_factory
        self._kernel32: object | None = None
        self._load_error = ""

    def _api(self):
        if self._kernel32 is None and not self._load_error:
            try:
                self._kernel32 = self._factory()
            except (OSError, AttributeError, ImportError, ValueError) as exc:
                self._load_error = f"{type(exc).__name__}: {exc}"
        return self._kernel32

    def probe(self) -> ProbeResult:
        if self._api() is None:
            return ProbeResult.unavailable(f"kernel32 unavailable: {self._load_error}")
        return ProbeResult.ready(AdapterIdentity("kernel32-host-resources", "1", _CAPABILITIES))

    def read(self, sample_seconds: float) -> HostReading:
        api = self._api()
        if api is None:
            return HostReading.unmeasured(
                f"kernel32 unavailable: {self._load_error}",
                logical_cpus=self._logical_cpus(),
            )
        diagnostics: list[str] = []
        busy: float | None = None
        try:
            start = api.system_times()
            self._sleep(sample_seconds)
            busy = busy_percent(start, api.system_times())
        except (OSError, OverflowError, ValueError) as exc:
            diagnostics.append(f"cpu busy unmeasured: {type(exc).__name__}: {exc}")
        memory: tuple[int | None, ...] = (None, None, None, None)
        try:
            memory = api.memory_status()
        except OSError as exc:
            diagnostics.append(f"memory unmeasured: {type(exc).__name__}: {exc}")
        return HostReading(
            self._logical_cpus(),
            busy,
            *memory,
            sample_seconds=float(sample_seconds),
            diagnostics=tuple(diagnostics),
        )


# --- Other hosts -----------------------------------------------------------


class PortableHostResourceAdapter:
    """For a host without a reader here: the logical CPU count only, with every
    other field unmeasured, so capacity falls back to the CPU term."""

    def probe(self) -> ProbeResult:
        return ProbeResult.ready(AdapterIdentity("portable-host-resources", "1", _CAPABILITIES))

    def read(self, sample_seconds: float) -> HostReading:
        return HostReading.unmeasured(
            "cpu busy, memory and commit are not measured on this host",
            logical_cpus=_logical_cpus(),
        )
