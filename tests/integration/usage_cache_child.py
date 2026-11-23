"""Read the usage meter once through the shared reading, in its own process.

The child waits for ``--go`` so every child reads at once. Its inner meter
records each call as a new file in ``--calls`` and takes one second to answer,
so readers that are not held by the lock all reach it. The reading is written
as JSON to ``--result``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from autobuild.adapters.usage_cache import SharedUsageMeterCache, reading_payload  # noqa: E402
from autobuild.domain import (  # noqa: E402
    AdapterIdentity,
    ProbeResult,
    UsageLimit,
    UsageLimitKind,
    UsageReading,
)

METER = "meter-a"
_LIMIT_SECONDS = 60.0


class CountingMeter:
    def __init__(self, calls: Path) -> None:
        self._calls = calls

    def probe(self) -> ProbeResult:
        return ProbeResult.ready(AdapterIdentity("counting-meter", "1", frozenset({"test"})))

    def read(self, meter_id: str) -> UsageReading:
        (self._calls / uuid4().hex).write_text("", encoding="utf-8")
        fetched_at = datetime.now(UTC).isoformat()
        time.sleep(1.0)
        return UsageReading(
            meter_id,
            True,
            (UsageLimit(UsageLimitKind.WEEKLY_ALL, 42.0, "2026-11-26T06:00:00+00:00"),),
            "",
            False,
            fetched_at,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--calls", required=True)
    parser.add_argument("--go", required=True)
    parser.add_argument("--result", required=True)
    arguments = parser.parse_args()
    go = Path(arguments.go)
    deadline = time.monotonic() + _LIMIT_SECONDS
    while not go.exists():
        if time.monotonic() >= deadline:
            return 2
        time.sleep(0.01)
    cache = SharedUsageMeterCache(CountingMeter(Path(arguments.calls)), Path(arguments.scratch))
    reading = cache.read(METER)
    Path(arguments.result).write_text(json.dumps(reading_payload(reading)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
