from __future__ import annotations

from pathlib import Path

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Give each pytest-xdist worker a short basetemp of its own.

    xdist names a worker's basetemp `<basetemp>/popen-gwN`. Several
    integration tests build run records and git objects deep under tmp_path,
    and on Windows those paths sit within a few characters of the 260-character
    path limit, so the extra segment can push them over. The worker number
    alone keeps the directory per worker while adding as little as possible.
    This runs before the tmp_path factory reads the option.
    """

    workerinput = getattr(config, "workerinput", None)
    basetemp = config.option.basetemp
    if workerinput is None or not basetemp:
        return
    path = Path(basetemp)
    worker = str(workerinput.get("workerid", ""))
    if path.name == f"popen-{worker}" and worker.startswith("gw"):
        config.option.basetemp = str(path.with_name(worker[2:]))
