"""Host command adapters with argv-first execution and process-tree teardown."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from uuid import uuid4

from autobuild.domain import AdapterIdentity, CommandRequest, CommandResult, ProbeResult

# The three progress signals a stall decision considers, in a fixed order.
PROGRESS_SIGNALS = ("output", "worktree", "cpu")
# The command adapter never samples slower than this, and never slower than the
# stall deadline itself. It is a ceiling, not a target.
MAX_SAMPLE_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ProgressSample:
    """One reading of the three progress signals at a monotonic instant.

    ``cpu_seconds`` is ``None`` when the host cannot read the direct child's CPU
    time. That unknown signal never contributes to a kill."""

    at: float
    stdout_size: int
    stderr_size: int
    digest: str
    cpu_seconds: float | None


def _last_progress_times(samples: tuple[ProgressSample, ...]) -> dict[str, float] | None:
    """Return the last time (monotonic) each signal advanced, or ``None`` when the
    CPU signal is unobserved anywhere in the window.

    Silence alone never kills: a kill needs all three signals observed and flat,
    so an unknown CPU reading collapses the whole decision to "not stalled"."""

    if not samples or samples[0].cpu_seconds is None:
        return None
    base = samples[0].at
    times = {name: base for name in PROGRESS_SIGNALS}
    for previous, current in zip(samples, samples[1:]):
        if current.cpu_seconds is None:
            return None
        if (
            current.stdout_size != previous.stdout_size
            or current.stderr_size != previous.stderr_size
        ):
            times["output"] = current.at
        if current.digest != previous.digest:
            times["worktree"] = current.at
        if current.cpu_seconds > previous.cpu_seconds:
            times["cpu"] = current.at
    return times


def progress_stalled(
    samples: tuple[ProgressSample, ...], deadline_seconds: float
) -> bool:
    """Pure stall decision over sample tuples.

    A seat is stalled only when output, worktree and CPU have all been flat for
    at least ``deadline_seconds``. Any one signal advancing inside the window, or
    an unobserved CPU signal, prevents the kill."""

    if deadline_seconds <= 0 or len(samples) < 2:
        return False
    times = _last_progress_times(samples)
    if times is None:
        return False
    return (samples[-1].at - max(times.values())) >= deadline_seconds


def _sample_times_since_start(
    samples: tuple[ProgressSample, ...],
) -> tuple[tuple[str, float], ...]:
    """The last-progress time of each signal as seconds since the first sample."""

    times = _last_progress_times(samples)
    if times is None or not samples:
        return ()
    base = samples[0].at
    return tuple((name, times[name] - base) for name in PROGRESS_SIGNALS)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _digest_value(callable_or_none) -> str:
    if callable_or_none is None:
        return ""
    try:
        value = callable_or_none()
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned or "command"


class LocalCommandAdapter:
    host_family = "local"

    def __init__(
        self, output_root: Path, sample_interval_seconds: float = MAX_SAMPLE_INTERVAL_SECONDS
    ) -> None:
        self._output_root = output_root.resolve(strict=False)
        self._sample_interval_seconds = min(
            max(sample_interval_seconds, 0.01), MAX_SAMPLE_INTERVAL_SECONDS
        )
        self._active: dict[str, subprocess.Popen[bytes]] = {}
        self._cancelled: set[str] = set()
        self._lock = Lock()

    def probe(self) -> ProbeResult:
        try:
            self._output_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ProbeResult.unavailable(f"command output root is unavailable: {exc}")
        return ProbeResult.ready(
            AdapterIdentity(
                f"{self.host_family}-command",
                "1",
                frozenset(
                    {
                        "argv",
                        "stdin-file",
                        "timeout",
                        "cancel",
                        "process-tree",
                        "captured-output",
                    }
                ),
            ),
            str(self._output_root),
        )

    def run(self, request: CommandRequest) -> CommandResult:
        command_root = self._output_root / f"{_safe_name(request.command_id)}-{uuid4().hex[:8]}"
        command_root.mkdir(parents=True, exist_ok=False)
        stdout_path = command_root / "stdout.txt"
        stderr_path = command_root / "stderr.txt"
        environment = os.environ.copy()
        environment.update(dict(request.environment))
        argv = self._invocation(request)
        started = datetime.now(UTC).isoformat()
        timed_out = False
        stalled = False
        stall_sample_times: tuple[tuple[str, float], ...] = ()
        stdin = Path(request.stdin_ref).open("rb") if request.stdin_ref is not None else None
        try:
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                process = subprocess.Popen(
                    argv,
                    cwd=request.cwd,
                    env=environment,
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                    **self._spawn_options(),
                )
                with self._lock:
                    self._active[request.command_id] = process
                try:
                    if request.progress_deadline_seconds > 0:
                        timed_out, stalled, stall_sample_times = self._supervise(
                            process, request, stdout_path, stderr_path
                        )
                    else:
                        try:
                            process.wait(timeout=request.timeout_seconds)
                        except subprocess.TimeoutExpired:
                            timed_out = True
                            self._terminate_tree(process)
                            process.wait()
                finally:
                    with self._lock:
                        self._active.pop(request.command_id, None)
                        cancelled = request.command_id in self._cancelled
                        self._cancelled.discard(request.command_id)
        finally:
            if stdin is not None:
                stdin.close()
        return CommandResult(
            request.command_id,
            process.returncode,
            str(stdout_path),
            str(stderr_path),
            started,
            datetime.now(UTC).isoformat(),
            timed_out=timed_out,
            cancelled=cancelled,
            stalled=stalled,
            stall_sample_times=stall_sample_times,
        )

    def _supervise(
        self,
        process: subprocess.Popen[bytes],
        request: CommandRequest,
        stdout_path: Path,
        stderr_path: Path,
    ) -> tuple[bool, bool, tuple[tuple[str, float], ...]]:
        """Wait for the process while sampling the three progress signals.

        Returns ``(timed_out, stalled, sample_times)``. A cap kill sets
        ``timed_out``; a stall kill sets ``stalled`` and the per-signal times. A
        clean exit returns both false. The stall decision is the pure
        ``progress_stalled`` function so its logic is unit-tested with a fake
        clock."""

        deadline = request.progress_deadline_seconds
        cap = request.timeout_seconds
        interval = min(self._sample_interval_seconds, deadline)
        start = time.monotonic()
        samples = [self._sample(process, request, stdout_path, stderr_path, start)]
        while True:
            remaining_cap = cap - (time.monotonic() - start)
            if remaining_cap <= 0:
                self._terminate_tree(process)
                process.wait()
                return True, False, _sample_times_since_start(tuple(samples))
            try:
                process.wait(timeout=min(interval, remaining_cap))
                return False, False, ()
            except subprocess.TimeoutExpired:
                pass
            samples.append(self._sample(process, request, stdout_path, stderr_path, start))
            if progress_stalled(tuple(samples), deadline):
                self._terminate_tree(process)
                process.wait()
                return False, True, _sample_times_since_start(tuple(samples))

    def _sample(
        self,
        process: subprocess.Popen[bytes],
        request: CommandRequest,
        stdout_path: Path,
        stderr_path: Path,
        start: float,
    ) -> ProgressSample:
        return ProgressSample(
            at=time.monotonic(),
            stdout_size=_file_size(stdout_path),
            stderr_size=_file_size(stderr_path),
            digest=_digest_value(request.progress_digest),
            cpu_seconds=self._child_cpu_seconds(process),
        )

    def _child_cpu_seconds(self, process: subprocess.Popen[bytes]) -> float | None:
        """CPU time of the direct child in seconds, or ``None`` when this host has
        no standard-library way to read it. Subclasses override per host."""

        return None

    def cancel(self, command_id: str) -> None:
        with self._lock:
            process = self._active.get(command_id)
            if process is not None:
                self._cancelled.add(command_id)
        if process is not None:
            self._terminate_tree(process)

    def _invocation(self, request: CommandRequest) -> list[str] | str:
        if request.shell is None:
            return list(request.argv)
        return self._shell_invocation(request.shell, request.argv)

    def _shell_invocation(self, shell: Path, argv: tuple[str, ...]) -> list[str]:
        return [str(shell), "-lc", shlex.join(argv)]

    def _spawn_options(self) -> dict[str, object]:
        return {"start_new_session": True}

    def _terminate_tree(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            process.kill()
            return
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                if process.poll() is None:
                    process.kill()


class PosixCommandAdapter(LocalCommandAdapter):
    host_family = "posix"

    def _child_cpu_seconds(self, process: subprocess.Popen[bytes]) -> float | None:
        """Read the direct child's CPU time from ``/proc/<pid>/stat``.

        Hosts without ``/proc`` (for example macOS) return ``None``, which the
        deadline function treats as an unknown signal that never kills."""

        try:
            data = Path(f"/proc/{process.pid}/stat").read_bytes()
        except OSError:
            return None
        try:
            # Fields after the parenthesised comm: utime and stime are the 14th
            # and 15th overall, i.e. indices 11 and 12 of the post-comm split.
            fields = data[data.rfind(b")") + 2 :].split()
            ticks = int(fields[11]) + int(fields[12])
        except (ValueError, IndexError):
            return None
        per_second = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        return ticks / (per_second or 100)


class WindowsCommandAdapter(LocalCommandAdapter):
    host_family = "windows"

    def _child_cpu_seconds(self, process: subprocess.Popen[bytes]) -> float | None:
        """Read the direct child's CPU time through ``GetProcessTimes``.

        Uses only ``ctypes`` from the standard library. Any failure reports the
        CPU signal as unknown rather than as flat."""

        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, process.pid
        )
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not ok:
                return None
        finally:
            kernel32.CloseHandle(handle)

        def seconds(value: "wintypes.FILETIME") -> float:
            return ((value.dwHighDateTime << 32) | value.dwLowDateTime) / 1e7

        return seconds(kernel) + seconds(user)

    def _invocation(self, request: CommandRequest) -> list[str] | str:
        if request.shell is not None:
            return self._shell_invocation(request.shell, request.argv)
        resolved = shutil.which(request.argv[0]) or request.argv[0]
        argv = [resolved, *request.argv[1:]]
        if Path(resolved).suffix.casefold() in {".cmd", ".bat"}:
            command_shell = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
            body = " ".join(self._quote_batch_argument(value) for value in argv)
            prefix = subprocess.list2cmdline([command_shell, "/d", "/s", "/c"])
            return f'{prefix} "{body}"'
        return argv

    @staticmethod
    def _quote_batch_argument(value: str) -> str:
        return f'"{value.replace(chr(34), chr(34) * 2)}"'

    def _shell_invocation(self, shell: Path, argv: tuple[str, ...]) -> list[str]:
        return [str(shell), "/d", "/s", "/c", subprocess.list2cmdline(list(argv))]

    def _spawn_options(self) -> dict[str, object]:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

    def _terminate_tree(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        completed = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0 and process.poll() is None:
            process.kill()
