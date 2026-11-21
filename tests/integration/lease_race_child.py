"""Contend for one lease surface in its own process for the lease race tests.

``race`` waits for ``--release``, then calls ``acquire`` once with a wrapped
``_read`` that writes ``--marker`` after reading and waits, at most two
seconds, for ``--rival``. Without a cross-process lock both rivals read the
surface as free before either writes. The outcome is written as JSON to
``--result`` and the process stays alive until ``--finish`` exists, so a winner
is still a live holder when its rival reads the record.

``hold`` takes the surface's cross-process lock through the adapter's own lock
step, writes ``--marker`` and waits until it is killed or ``--finish`` exists.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from autobuild.adapters import FilesystemLeaseAdapter  # noqa: E402
from autobuild.domain import LeaseHeld, LeaseRequest, LeaseSurface, SurfaceKind  # noqa: E402

_LIMIT_SECONDS = 60.0
_POLL_SECONDS = 0.02


def _wait_for(path: Path, limit: float) -> bool:
    deadline = time.monotonic() + limit
    while not path.exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SECONDS)
    return True


def _write_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def _race(arguments: argparse.Namespace, surface: LeaseSurface) -> None:
    marker = Path(arguments.marker)
    rival = Path(arguments.rival)
    reads: list[bool] = []
    original = FilesystemLeaseAdapter._read

    def wrapped(self: FilesystemLeaseAdapter, target: LeaseSurface):  # type: ignore[no-untyped-def]
        record = original(self, target)
        reads.append(True)
        marker.write_text("read", encoding="utf-8")
        _wait_for(rival, 2.0)
        return record

    FilesystemLeaseAdapter._read = wrapped  # type: ignore[method-assign]
    adapter = FilesystemLeaseAdapter(Path(arguments.scratch))
    if not _wait_for(Path(arguments.release), _LIMIT_SECONDS):
        raise SystemExit("release file never appeared")
    try:
        adapter.acquire(LeaseRequest(surface, arguments.campaign))
        outcome = "granted"
    except LeaseHeld:
        outcome = "held"
    _write_json(
        Path(arguments.result),
        {"outcome": outcome, "process_id": os.getpid(), "wrapped_read": bool(reads)},
    )
    _wait_for(Path(arguments.finish), _LIMIT_SECONDS)


def _hold(arguments: argparse.Namespace, surface: LeaseSurface) -> None:
    adapter = FilesystemLeaseAdapter(Path(arguments.scratch))
    with adapter._exclusive(surface):
        Path(arguments.marker).write_text("holding", encoding="utf-8")
        _wait_for(Path(arguments.finish), _LIMIT_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("race", "hold"))
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--surface", required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--finish", required=True)
    parser.add_argument("--rival")
    parser.add_argument("--release")
    parser.add_argument("--result")
    parser.add_argument("--campaign", default="campaign")
    arguments = parser.parse_args()
    surface = LeaseSurface(Path(arguments.surface), SurfaceKind.TRACKER)
    if arguments.mode == "race":
        _race(arguments, surface)
    else:
        _hold(arguments, surface)


if __name__ == "__main__":
    main()
