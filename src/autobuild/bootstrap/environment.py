"""Temporary-work configuration shared by production and proof entry points."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from autobuild.adapters.harness_cli import scratch_environment


def configure_scratch_environment(root: Path | None = None) -> Path:
    """Use standard system temp unless the caller supplies a scratch root."""

    selected = root if root is not None else Path(tempfile.gettempdir()) / "autobuild"
    resolved = selected.expanduser().resolve(strict=False)
    os.environ.update(dict(scratch_environment(resolved)))
    return resolved
