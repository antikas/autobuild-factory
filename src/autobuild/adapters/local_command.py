"""Host command adapters with argv-first execution and process-tree teardown."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from uuid import uuid4

from autobuild.domain import AdapterIdentity, CommandRequest, CommandResult, ProbeResult


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned or "command"


class LocalCommandAdapter:
    host_family = "local"

    def __init__(self, output_root: Path) -> None:
        self._output_root = output_root.resolve(strict=False)
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
                frozenset({"argv", "timeout", "cancel", "process-tree", "captured-output"}),
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
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                argv,
                cwd=request.cwd,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                **self._spawn_options(),
            )
            with self._lock:
                self._active[request.command_id] = process
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
        return CommandResult(
            request.command_id,
            process.returncode,
            str(stdout_path),
            str(stderr_path),
            started,
            datetime.now(UTC).isoformat(),
            timed_out=timed_out,
            cancelled=cancelled,
        )

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


class WindowsCommandAdapter(LocalCommandAdapter):
    host_family = "windows"

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
