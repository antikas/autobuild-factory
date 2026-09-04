"""Temporary-work configuration shared by production and proof entry points."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from autobuild.adapters.harness_cli import scratch_environment
from autobuild.bootstrap.profile import read_run_scratch_root

# Run records live under this subdirectory of the scratch root, created by the
# campaign's run-record adapter as ``<scratch>/runs/<run-id>``.
RUNS_SUBDIRECTORY = "runs"


def default_scratch_root() -> Path:
    """The scratch root ``configure_scratch_environment`` selects when the caller
    supplies none, resolved without creating directories or reading environment
    state, so a read-only watcher can find the runs without side effects."""

    return (Path(tempfile.gettempdir()) / "autobuild").expanduser().resolve(strict=False)


def configure_scratch_environment(root: Path | None = None) -> Path:
    """Use standard system temp unless the caller supplies a scratch root."""

    resolved = (
        root.expanduser().resolve(strict=False)
        if root is not None
        else default_scratch_root()
    )
    os.environ.update(dict(scratch_environment(resolved)))
    return resolved


def resolve_runs_root(
    repository: str | Path,
    profile_path: str | Path | None,
    scratch_override: str | None,
) -> Path:
    """Locate the directory that holds run records.

    The scratch root is ``--scratch-root`` when given, otherwise ``[run]
    scratch_root`` from the profile, otherwise the same default the campaign uses.
    Resolving it reads only ``[run] scratch_root`` and never requires a complete
    run profile. The runs root is ``<scratch>/runs`` under that scratch root."""

    scratch_root = read_run_scratch_root(repository, profile_path, scratch_override)
    if scratch_root is None:
        scratch_root = default_scratch_root()
    return scratch_root / RUNS_SUBDIRECTORY
