"""The filesystem lane state clears one lane's cooling under its lock."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from autobuild.adapters import FilesystemLaneStateAdapter
from autobuild.domain import LaneSignal, LaneSignalKind

NOW = datetime(2026, 11, 21, 7, 0, tzinfo=UTC)
RESET = "2026-11-23T06:00:00+00:00"


def adapter(root: Path) -> FilesystemLaneStateAdapter:
    return FilesystemLaneStateAdapter(root, cool_seconds=3600, now=lambda: NOW)


def test_clear_removes_one_lane_and_keeps_the_others(tmp_path: Path) -> None:
    state = adapter(tmp_path / "lanes")
    usage = state.cool("claude-code", LaneSignal(LaneSignalKind.USAGE, reset_at=RESET), "a")
    rate = state.cool("codex", LaneSignal(LaneSignalKind.RATE_LIMIT), "b")

    assert state.clear("claude-code", usage) is True

    assert state.active("claude-code") is None
    assert adapter(tmp_path / "lanes").active("codex") == rate
    stored = json.loads((tmp_path / "lanes" / "lanes.json").read_text(encoding="utf-8"))
    assert sorted(stored["lanes"]) == ["codex"]


def test_clear_keeps_a_cooling_written_after_the_one_judged(tmp_path: Path) -> None:
    state = adapter(tmp_path / "lanes")
    judged = state.cool("claude-code", LaneSignal(LaneSignalKind.USAGE, reset_at=RESET), "a")
    later = state.cool("claude-code", LaneSignal(LaneSignalKind.RATE_LIMIT), "b")

    assert state.clear("claude-code", judged) is False
    assert state.active("claude-code") == later
    assert state.clear("missing", judged) is False


def test_clear_waits_for_the_lane_state_lock(tmp_path: Path) -> None:
    root = tmp_path / "lanes"
    state = adapter(root)
    usage = state.cool("claude-code", LaneSignal(LaneSignalKind.USAGE, reset_at=RESET), "a")
    lock = root / "lanes.json.lock"
    lock.write_text(NOW.isoformat(), encoding="utf-8")
    result: list[bool] = []

    worker = threading.Thread(target=lambda: result.append(state.clear("claude-code", usage)))
    worker.start()
    worker.join(0.3)
    assert worker.is_alive(), "clear did not wait for the held lock"
    assert state.active("claude-code") == usage

    lock.unlink()
    worker.join(5.0)
    assert result == [True]
    assert state.active("claude-code") is None


def test_admission_stop_preserves_a_concurrent_auth_failure(tmp_path: Path) -> None:
    state = adapter(tmp_path)
    auth = state.cool("codex", LaneSignal(LaneSignalKind.AUTH), "other")
    observed = state.cool(
        "codex", LaneSignal(LaneSignalKind.USAGE, reset_at=RESET),
        "subscription-admission", only_if_clear=True,
    )
    assert observed == auth == state.active("codex")
