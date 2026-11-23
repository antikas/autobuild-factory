"""Real processes reading the usage meter at once through the shared reading
make one meter request between them."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CHILD = Path(__file__).with_name("usage_cache_child.py")
READERS = 6


def test_six_processes_reading_at_once_make_one_meter_request(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    calls = tmp_path / "calls"
    calls.mkdir()
    go = tmp_path / "go"
    results = [tmp_path / f"result-{index}.json" for index in range(READERS)]
    children = [
        subprocess.Popen(
            [
                sys.executable,
                str(CHILD),
                "--scratch", str(scratch),
                "--calls", str(calls),
                "--go", str(go),
                "--result", str(result),
            ]
        )
        for result in results
    ]
    try:
        go.write_text("", encoding="utf-8")
        codes = [child.wait(timeout=60) for child in children]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()

    assert codes == [0] * READERS
    assert len(list(calls.iterdir())) == 1
    readings = [json.loads(result.read_text(encoding="utf-8")) for result in results]
    assert all(reading == readings[0] for reading in readings)
    assert readings[0]["ok"] is True
