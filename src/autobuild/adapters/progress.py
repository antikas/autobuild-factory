"""First-party progress sinks behind the ``ProgressPort``.

Each sink is a mechanism: it turns one rendered progress line into a durable or
side-channel effect. No sink may raise into the application. A failing file,
stderr or command sink is swallowed; the command hook additionally counts its
failures so the composite can report them once on the completion line."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from autobuild.application.progress import CAMPAIGN_COMPLETED_PREFIX
from autobuild.domain import RunRecordRef


class FileProgressAdapter:
    """Append every line to ``progress.log`` under the run record.

    The log is opened once in ``begin`` and only ever appended to, so a reader
    that holds the file open cannot break the writer, and a detached launch keeps
    every line already written."""

    def __init__(self) -> None:
        self._handle = None

    def begin(self, record: RunRecordRef) -> None:
        try:
            path = record.root / "progress.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("a", encoding="utf-8", newline="\n")
        except OSError:
            self._handle = None

    def emit(self, line: str) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(line + "\n")
            self._handle.flush()
        except (OSError, ValueError):
            return


class StderrProgressAdapter:
    """Write and flush one line to stderr, so a detached launch with redirected
    stderr keeps every line even if the process is later killed."""

    def begin(self, record: RunRecordRef) -> None:
        return None

    def emit(self, line: str) -> None:
        try:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            return


class CommandHookProgressAdapter:
    """Run a human-approved command once per line with the line on stdin.

    The hook is not a workflow command and never touches ``CommandPort``. Every
    call has a hard timeout ceiling. A missing executable, a non-zero exit, a
    timeout or an encoding error is swallowed and counted in ``failures`` so the
    composite can report the total once on the completion line."""

    def __init__(self, command: tuple[str, ...], timeout_seconds: float = 5.0) -> None:
        self._command = tuple(command)
        self._timeout_seconds = timeout_seconds
        self.failures = 0

    def begin(self, record: RunRecordRef) -> None:
        return None

    def emit(self, line: str) -> None:
        if not self._command:
            return
        try:
            completed = subprocess.run(
                list(self._command),
                input=line + "\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self._timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
            self.failures += 1
            return
        if completed.returncode != 0:
            self.failures += 1


def is_completion_line(line: str) -> bool:
    """Return whether a rendered progress line is the campaign-completion line.

    The renderer prefixes the line with the event timestamp, so the campaign
    completion token appears after that stamp. A watcher stops once this line has
    been printed."""

    return CAMPAIGN_COMPLETED_PREFIX in line


class ProgressLogReader:
    """Follow the newline-terminated lines of a run's ``progress.log`` by offset.

    The reader holds a byte offset into the file and a buffer for the current
    partial line. Each ``poll`` reads only the bytes appended since the last call
    and returns the lines a newline has completed; a trailing partial line stays
    buffered until its newline arrives, so a reader never prints half a line. The
    reader only ever reads: it opens the file, seeks and reads, and never writes.
    A file that is missing or briefly unreadable yields no lines rather than
    raising, so a watcher can start before the writer creates the log."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._offset = 0
        self._pending = b""

    def poll(self) -> list[str]:
        try:
            with self._path.open("rb") as stream:
                stream.seek(self._offset)
                chunk = stream.read()
                self._offset = stream.tell()
        except OSError:
            return []
        if not chunk:
            return []
        self._pending += chunk
        lines: list[str] = []
        while True:
            newline = self._pending.find(b"\n")
            if newline < 0:
                break
            raw = self._pending[:newline]
            self._pending = self._pending[newline + 1 :]
            lines.append(raw.rstrip(b"\r").decode("utf-8", errors="replace"))
        return lines


class CompositeProgressAdapter:
    """Fan one call out to the configured sinks in order.

    On the campaign-completion line it appends the total swallowed hook-failure
    count once, reading it from any child sink that keeps a ``failures`` tally."""

    def __init__(self, adapters: tuple[object, ...]) -> None:
        self._adapters = tuple(adapters)

    def begin(self, record: RunRecordRef) -> None:
        for adapter in self._adapters:
            adapter.begin(record)

    def emit(self, line: str) -> None:
        _, _, body = line.partition(" ")
        if body.startswith(CAMPAIGN_COMPLETED_PREFIX):
            line = f"{line} progress hook failures: {self._hook_failures()}"
        for adapter in self._adapters:
            adapter.emit(line)

    def _hook_failures(self) -> int:
        return sum(int(getattr(adapter, "failures", 0)) for adapter in self._adapters)
