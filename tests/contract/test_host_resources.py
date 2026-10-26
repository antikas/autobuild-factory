"""Host resource adapter contracts: parsers on committed fixtures, the
kernel32 adapter against a scripted API and, on Windows, against the host."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from autobuild.adapters.host_resources import (
    Kernel32HostResourceAdapter,
    PortableHostResourceAdapter,
    ProcHostResourceAdapter,
    busy_percent,
    filetime_counters,
    parse_proc_meminfo,
    parse_proc_stat,
)
from autobuild.domain import GIB, HostReading
from autobuild.ports import HostResourcePort

FIXTURES = Path(__file__).parent / "fixtures" / "host"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="ascii")


# Hand computation from the aggregate cpu lines of the two stat fixtures.
# Start: idle 80000 + iowait 1000 = 81000; total of user..steal
#   10000 + 500 + 3000 + 80000 + 1000 + 200 + 300 + 0 = 95000.
# End: idle 80900 + 1080 = 81980; total
#   10600 + 520 + 3300 + 80900 + 1080 + 220 + 330 + 50 = 97000 (guest 400 excluded).
# Window: idle 980 of 2000, busy 100 * 1020 / 2000 = 51.0.
# Counting guest time again would give 100 * 1420 / 2400 = 59.17.
START_COUNTERS = (81000, 95000)
END_COUNTERS = (81980, 97000)
EXPECTED_BUSY = 51.0

# meminfo.txt: MemTotal 65249280 kB, MemAvailable 29045964 kB,
# CommitLimit 40000000 kB, Committed_AS 30000000 kB; 1 kB = 1024 bytes.
EXPECTED_TOTAL = 66_815_262_720
EXPECTED_AVAILABLE = 29_743_067_136
EXPECTED_COMMIT_TOTAL = 40_960_000_000
EXPECTED_COMMIT_AVAILABLE = 10_240_000_000


def test_proc_stat_counters_match_the_hand_computed_values() -> None:
    assert parse_proc_stat(fixture("proc_stat_start.txt")) == START_COUNTERS
    assert parse_proc_stat(fixture("proc_stat_end.txt")) == END_COUNTERS
    assert busy_percent(START_COUNTERS, END_COUNTERS) == pytest.approx(EXPECTED_BUSY)


def test_proc_stat_without_an_aggregate_line_is_refused() -> None:
    with pytest.raises(ValueError, match="no aggregate cpu line"):
        parse_proc_stat("cpu0 1 2 3 4\nintr 5\n")


def test_a_window_with_no_elapsed_time_has_no_busy_percent() -> None:
    assert busy_percent((10, 100), (10, 100)) is None


def test_meminfo_under_strict_overcommit_reports_memory_and_commit() -> None:
    assert parse_proc_meminfo(fixture("meminfo.txt"), strict_overcommit=True) == {
        "memory_total_bytes": EXPECTED_TOTAL,
        "memory_available_bytes": EXPECTED_AVAILABLE,
        "commit_total_bytes": EXPECTED_COMMIT_TOTAL,
        "commit_available_bytes": EXPECTED_COMMIT_AVAILABLE,
    }
    assert EXPECTED_AVAILABLE / GIB == pytest.approx(27.70, abs=0.01)


def test_meminfo_without_strict_overcommit_leaves_commit_unmeasured() -> None:
    parsed = parse_proc_meminfo(fixture("meminfo.txt"), strict_overcommit=False)
    assert parsed["memory_available_bytes"] == EXPECTED_AVAILABLE
    assert parsed["commit_total_bytes"] is None
    assert parsed["commit_available_bytes"] is None


def test_meminfo_without_mem_available_is_unmeasured_not_estimated() -> None:
    # MemTotal 4046336 kB; CommitLimit 2023168 kB is below Committed_AS 2500000 kB.
    parsed = parse_proc_meminfo(
        fixture("meminfo_without_available.txt"), strict_overcommit=True
    )
    assert parsed == {
        "memory_total_bytes": 4_143_448_064,
        "memory_available_bytes": None,
        "commit_total_bytes": 2_071_724_032,
        "commit_available_bytes": 0,
    }


def _proc_root(tmp_path: Path, overcommit: str | None) -> Path:
    root = tmp_path / "proc"
    (root / "sys" / "vm").mkdir(parents=True)
    shutil.copyfile(FIXTURES / "proc_stat_start.txt", root / "stat")
    shutil.copyfile(FIXTURES / "meminfo.txt", root / "meminfo")
    if overcommit is not None:
        (root / "sys" / "vm" / "overcommit_memory").write_text(overcommit, encoding="ascii")
    return root


def test_the_proc_adapter_samples_cpu_over_the_window(tmp_path: Path) -> None:
    root = _proc_root(tmp_path, "2\n")
    windows: list[float] = []

    def advance(seconds: float) -> None:
        windows.append(seconds)
        shutil.copyfile(FIXTURES / "proc_stat_end.txt", root / "stat")

    adapter = ProcHostResourceAdapter(root, sleep=advance, logical_cpus=lambda: 24)
    reading = adapter.read(2.0)

    assert isinstance(adapter, HostResourcePort)
    assert adapter.probe().available
    assert windows == [2.0]
    assert reading == HostReading(
        24,
        pytest.approx(EXPECTED_BUSY),
        EXPECTED_TOTAL,
        EXPECTED_AVAILABLE,
        EXPECTED_COMMIT_TOTAL,
        EXPECTED_COMMIT_AVAILABLE,
        2.0,
        (),
    )
    assert all(reading.measured().values())


def test_the_proc_adapter_marks_unreadable_sources_unmeasured(tmp_path: Path) -> None:
    root = _proc_root(tmp_path, None)
    (root / "stat").unlink()
    adapter = ProcHostResourceAdapter(root, sleep=lambda _s: None, logical_cpus=lambda: 4)

    reading = adapter.read(1.0)

    assert not adapter.probe().available
    assert reading.cpu_busy_percent is None
    assert reading.memory_available_bytes == EXPECTED_AVAILABLE
    assert reading.commit_available_bytes is None
    assert reading.measured()["cpu_busy_percent"] is False
    assert any(note.startswith("cpu busy unmeasured") for note in reading.diagnostics)
    assert any(note.startswith("commit unmeasured") for note in reading.diagnostics)


class ScriptedKernel32:
    def __init__(self, times: list[tuple[int, int]], memory=None, fail_memory=False):
        self.times = times
        self.memory = memory
        self.fail_memory = fail_memory

    def system_times(self) -> tuple[int, int]:
        return self.times.pop(0)

    def memory_status(self):
        if self.fail_memory:
            raise OSError("status refused")
        return self.memory


def test_the_kernel32_adapter_maps_times_and_memory_status() -> None:
    api = ScriptedKernel32(
        [(1_000, 4_000), (1_600, 6_000)], memory=(64 * GIB, 27 * GIB, 100 * GIB, 50 * GIB)
    )
    adapter = Kernel32HostResourceAdapter(
        sleep=lambda _s: None, logical_cpus=lambda: 24, kernel32_factory=lambda: api
    )

    reading = adapter.read(0.5)

    # Idle 600 of 2000 elapsed: busy 70.0.
    assert reading == HostReading(24, 70.0, 64 * GIB, 27 * GIB, 100 * GIB, 50 * GIB, 0.5, ())


class RawSystemTimes(ScriptedKernel32):
    """Scripts raw ``GetSystemTimes`` values (idle, kernel including idle, user)."""

    def __init__(self, samples: list[tuple[int, int, int]]):
        super().__init__([], memory=(0, 0, 0, 0))
        self.samples = samples

    def system_times(self) -> tuple[int, int]:
        return filetime_counters(*self.samples.pop(0))


def test_the_kernel32_busy_percent_counts_idle_once() -> None:
    # Deltas: idle 600, kernel 800 (including that idle), user 200.
    # Busy = (800 + 200 - 600) / (800 + 200) = 40.0. Adding idle to the total
    # again would give (1600 - 600) / 1600 = 62.5.
    api = RawSystemTimes([(1_000, 3_000, 1_000), (1_600, 3_800, 1_200)])
    adapter = Kernel32HostResourceAdapter(
        sleep=lambda _s: None, logical_cpus=lambda: 24, kernel32_factory=lambda: api
    )
    assert adapter.read(1.0).cpu_busy_percent == pytest.approx(40.0)


def test_a_window_too_long_to_sleep_is_unmeasured_not_raised(tmp_path: Path) -> None:
    kernel = Kernel32HostResourceAdapter(
        logical_cpus=lambda: 4,
        kernel32_factory=lambda: ScriptedKernel32([(0, 10)], memory=(1, 1, 1, 1)),
    )
    proc = ProcHostResourceAdapter(_proc_root(tmp_path, "0\n"), logical_cpus=lambda: 4)

    for reading in (kernel.read(float("inf")), proc.read(float("inf"))):
        assert reading.cpu_busy_percent is None
        assert reading.memory_available_bytes is not None
        assert reading.diagnostics[0].startswith("cpu busy unmeasured: OverflowError")


def test_the_kernel32_adapter_never_raises_when_the_api_fails() -> None:
    def refuse():
        raise OSError("no kernel32 here")

    absent = Kernel32HostResourceAdapter(logical_cpus=lambda: 8, kernel32_factory=refuse)
    reading = absent.read(0.1)
    assert not absent.probe().available
    assert reading == HostReading.unmeasured(
        "kernel32 unavailable: OSError: no kernel32 here", logical_cpus=8
    )

    partial = Kernel32HostResourceAdapter(
        sleep=lambda _s: None,
        logical_cpus=lambda: 8,
        kernel32_factory=lambda: ScriptedKernel32([(0, 10), (5, 20)], fail_memory=True),
    )
    reading = partial.read(0.1)
    assert reading.cpu_busy_percent == 50.0
    assert reading.memory_available_bytes is None
    assert reading.diagnostics == ("memory unmeasured: OSError: status refused",)


def test_the_portable_adapter_reports_cpus_and_marks_the_rest_unmeasured() -> None:
    reading = PortableHostResourceAdapter().read(1.0)
    assert reading.logical_cpus == os.cpu_count()
    assert reading.measured() == {
        "logical_cpus": True,
        "cpu_busy_percent": False,
        "memory_total_bytes": False,
        "memory_available_bytes": False,
        "commit_total_bytes": False,
        "commit_available_bytes": False,
    }


@pytest.mark.skipif(os.name != "nt", reason="reads the live Windows host")
def test_the_kernel32_adapter_agrees_with_the_host_management_counters() -> None:
    adapter = Kernel32HostResourceAdapter()
    reading = adapter.read(0.5)
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory; "
            "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    after = adapter.read(0.0)
    free_kib, total_bytes = (int(line) for line in completed.stdout.split())

    # Other processes move available memory while the management query starts,
    # so the query must fall within 1 GiB of the span the adapter saw around it.
    seen = (reading.memory_available_bytes, after.memory_available_bytes)
    assert reading.diagnostics == ()
    assert 0.0 <= reading.cpu_busy_percent <= 100.0
    assert min(seen) - 1 * GIB <= free_kib * 1024 <= max(seen) + 1 * GIB
    assert abs(reading.memory_total_bytes - total_bytes) <= 0.1 * GIB
    assert reading.commit_available_bytes <= reading.commit_total_bytes
